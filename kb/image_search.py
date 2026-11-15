# -*- coding: utf-8 -*-
"""图像向量检索：jina-clip-v2 图文对齐嵌入（2026-10-06 新增）。

独立于文本链路：不走 lancedb 嵌入注册表（其 SourceField/VectorField 接口仅
面向文本 source 列），本模块自管权重与生命周期（复用 kb_model_lifecycle 的
闲置卸载），向量在入库时显式计算，存入 image_chunks 表的定长 Vector 列。

- 嵌入模型：jinaai/jina-clip-v2（0.9B；文本塔 Jina XLM-RoBERTa，视觉塔
  EVA02-L14；图文同空间 → 以文搜图/以图搜图同用一套向量；CC-BY-NC-4.0，
  仅限个人学术用途）
- 图像预处理：processor 自带 512×512 中心裁剪，无需手工缩放
- 检索打分：查询与图像向量均 L2 归一化，cosine 距离 = 1 - 余弦相似度
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import List

import numpy as np

from . import config as cfg
from . import model_lifecycle as lifecycle
from .schema import get_db

# 权重持有器：与 embeddings.py 同款设计——表对象会长期持有本模块引用，
# 权重放模块持有器才能在闲置卸载时被释放。
_img_holder: dict = {"model": None, "processor": None}
_holder_lock = threading.Lock()


def _resolve_device() -> str:
    if cfg.LOCAL_MODEL_DEVICE != "auto":
        return cfg.LOCAL_MODEL_DEVICE
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _local_snapshot(model_id: str) -> str:
    """解析模型路径：优先自包含本地目录（离线保底），其次 HF 缓存快照，最后 hub id。

    自包含目录 = 权重 + 内联实现代码 + auto_map 改写为本地引用（2026-10-06 一次性
    预处理产物）；HF 缓存的动态模块解析在 transformers v5 下离线不稳定，不作依赖。
    """
    if cfg.IMAGE_EMBED_LOCAL_DIR and Path(cfg.IMAGE_EMBED_LOCAL_DIR).exists():
        return cfg.IMAGE_EMBED_LOCAL_DIR
    hub = Path(os.environ.get("HF_HOME", "")) / "hub" / ("models--" + model_id.replace("/", "--"))
    if hub.exists():
        snaps = sorted((hub / "snapshots").glob("*"))
        if snaps:
            return str(snaps[-1])
    return model_id


def _shim_transformers_v5_clip() -> None:
    """transformers v5 移除了 clip_loss；jina 自定义 modeling_clip 顶层导入它。

    推理路径（get_image_features/get_text_features）从不调用该损失，但导入必须
    成功——用 v5 现存的 contrastive_loss 拼回 v4 等价实现（(caption+image)/2）。
    """
    try:
        import transformers.models.clip.modeling_clip as _tf_clip
    except ImportError:
        return
    if getattr(_tf_clip, "clip_loss", None) is not None:
        return

    def clip_loss(similarity):
        return (_tf_clip.contrastive_loss(similarity) + _tf_clip.contrastive_loss(similarity.t())) / 2.0

    _tf_clip.clip_loss = clip_loss


def _load_image_model():
    import torch
    from transformers import AutoModel, AutoProcessor

    src = _local_snapshot(cfg.LOCAL_IMAGE_EMBED_MODEL)
    _shim_transformers_v5_clip()  # 必须在动态模块导入前打上
    # 注意：不要手动 AutoConfig 后再传入——transformers v5 的动态模块按文件内容
    # 分目录缓存，两次解析会得到不同类对象，触发 isinstance 校验失败。
    # 本地目录的 config.json 已预写 use_flash_attn=false（flash-attn 未安装），
    # 文本塔 hf_model_name_or_path 已指向 v3 本地快照。
    # dtype 必须传字符串：transformers v5 会把 dtype kwarg 归一化进 config 再走
    # 动态模块，jina 自带 configuration_clip.__init__ 里 hasattr(torch, torch_dtype)
    # 只接受字符串，传 torch.dtype 对象在同进程二次加载时抛
    # "attribute name must be string, not 'torch.dtype'"。
    try:
        model = AutoModel.from_pretrained(src, trust_remote_code=True, dtype="float16")
    except TypeError as error:  # transformers v4 参数名（仅认 kwarg 不存在，防深层 TypeError 误兜底）
        if "unexpected keyword argument" not in str(error):
            raise
        model = AutoModel.from_pretrained(src, trust_remote_code=True, torch_dtype="float16")
    device = _resolve_device()
    model = model.to(device).eval()
    processor = AutoProcessor.from_pretrained(src, trust_remote_code=True)
    lifecycle.touch()
    return model, processor, device


def _unload_image_model() -> bool:
    with _holder_lock:
        had = _img_holder["model"] is not None or _img_holder["processor"] is not None
        _img_holder["model"] = None
        _img_holder["processor"] = None
    # transformers v5 对 trust_remote_code 动态模块按内容 hash 分目录缓存，sys.modules
    # 里的残留与重载出的新类对象签名不一致，同进程二次加载会连环炸
    # （isinstance 失配 / "unexpected keyword argument"）。卸载时把动态模块整体请出
    # sys.modules，使下次加载条件与首载等价。
    import sys

    for name in [m for m in sys.modules if m.startswith("transformers_modules.")]:
        sys.modules.pop(name, None)
    return had


lifecycle.register_unloader("image-embedding:" + cfg.LOCAL_IMAGE_EMBED_MODEL, _unload_image_model)


def _get_model():
    with _holder_lock:
        if _img_holder["model"] is None:
            _img_holder["model"], _img_holder["processor"], _device = _load_image_model()
            _img_holder["device"] = _device
        lifecycle.touch()
        return _img_holder["model"], _img_holder["processor"], _img_holder["device"]


# =============================================================
# 嵌入
# =============================================================

def embed_images(image_paths: List[str], batch_size: int | None = None) -> "list[tuple[str, np.ndarray]]":
    """批量图像 → [(路径, L2 归一化向量)]；损坏图片跳过并在 stderr 告警。"""
    import torch
    from PIL import Image

    bs = batch_size or cfg.IMAGE_BATCH_SIZE
    out: list[tuple[str, np.ndarray]] = []
    model, processor, device = _get_model()
    for start in range(0, len(image_paths), bs):
        chunk = image_paths[start : start + bs]
        imgs, good = [], []
        for p in chunk:
            try:
                imgs.append(Image.open(p).convert("RGB"))
                good.append(p)
            except Exception as e:  # 坏图跳过，不阻塞整批
                print(f"[image] 跳过损坏图片 {p}: {e}", file=sys.stderr)
        if not imgs:
            continue
        with torch.no_grad():
            inputs = processor(images=imgs, return_tensors="pt")
            feats = model.get_image_features(**{k: v.to(device) for k, v in inputs.items()})
        feats = (feats / feats.norm(p=2, dim=-1, keepdim=True)).float().cpu().numpy()
        out.extend(zip(good, feats))
    return out


def embed_query_text(text: str) -> np.ndarray:
    """查询文本 → (dim,) L2 归一化 float32（文本塔，中文可用）。"""
    import torch

    model, processor, device = _get_model()
    with torch.no_grad():
        inputs = processor(
            text=[text], padding=True, truncation=True, max_length=8192, return_tensors="pt"
        )
        feats = model.get_text_features(**{k: v.to(device) for k, v in inputs.items()})
    feats = (feats / feats.norm(p=2, dim=-1, keepdim=True)).float().cpu().numpy()
    return feats[0]


# =============================================================
# 表
# =============================================================

_IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def _table_model():
    from lancedb.pydantic import LanceModel, Vector

    class ImageChunk(LanceModel):
        image_path: str
        file_name: str
        source_doc: str  # 来源目录名（citekey）
        caption: str
        mtime_ns: int
        vector: Vector(cfg.LOCAL_IMAGE_EMBED_DIM)

    return ImageChunk


def get_image_table():
    db = get_db()
    name = cfg.IMAGE_TABLE_NAME
    if name in db.table_names():
        return db.open_table(name)
    return db.create_table(name, schema=_table_model(), mode="create")


def image_table_exists() -> bool:
    return cfg.IMAGE_TABLE_NAME in get_db().table_names()


def image_count() -> int:
    if not image_table_exists():
        return 0
    return len(get_image_table())


# =============================================================
# 入库
# =============================================================

def _scan_images(scan_dir: str) -> List[str]:
    root = Path(scan_dir)
    if not root.exists():
        return []
    return sorted(str(p) for p in root.rglob("*") if p.suffix.lower() in _IMAGE_EXT and p.is_file())


def plan_image_ingest(
    existing: dict[str, list[int]],
    scanned: list[tuple[str, int]],
) -> dict:
    """把扫描结果 (路径, mtime_ns) 分类为 new/changed/skipped/duplicates。

    existing 为「路径 → 该路径全部现有行的 mtime_ns 列表」：同一路径出现多行时，
    无论 mtime 是否等于当前值都强制归为 changed，由 ingest_images 替换掉全部旧行，
    以清理历史重复行。同批扫描中重复出现的路径只按首次出现分类，后续计入 duplicates。
    """
    plan = {"new": [], "changed": [], "skipped": [], "duplicates": []}
    seen: set[str] = set()
    for path, mtime_ns in scanned:
        if path in seen:
            plan["duplicates"].append(path)
            continue
        seen.add(path)
        previous = existing.get(path)
        if not previous:
            plan["new"].append(path)
        elif len(previous) > 1 or previous[0] != mtime_ns:
            plan["changed"].append(path)
        else:
            plan["skipped"].append(path)
    return plan


def _load_image_state(
    table,
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """读取现有图片行：(路径 → [mtime_ns...], 路径 → [_rowid...])。

    _rowid 是 Lance 行地址，供「先插后删」精确定位旧行；当前版本拿不到时第二项
    返回空 dict，调用方退回按 image_path 删除并报告并发风险。
    """
    try:
        arrow = table.to_lance().to_table(
            columns=["image_path", "mtime_ns"], with_row_id=True
        )
    except Exception as error:  # 旧版 pylance 无 with_row_id 等
        print(f"[image] 读取 _rowid 失败，回退按 image_path 删除: {error}", file=sys.stderr)
        arrow = table.to_lance().to_table(columns=["image_path", "mtime_ns"])
        has_rowid = False
    else:
        has_rowid = "_rowid" in arrow.column_names

    paths = arrow.column("image_path").to_pylist()
    mtimes = arrow.column("mtime_ns").to_pylist()
    existing: dict[str, list[int]] = {}
    for path, mtime_ns in zip(paths, mtimes):
        existing.setdefault(str(path), []).append(int(mtime_ns))

    rowids: dict[str, list[int]] = {}
    if has_rowid:
        ids = arrow.column("_rowid").to_pylist()
        for path, rowid in zip(paths, ids):
            rowids.setdefault(str(path), []).append(int(rowid))
    return existing, rowids


def _image_paths_where(paths: list[str]) -> str:
    quoted = ",".join(f"'{_sql_quote(path)}'" for path in sorted(paths))
    return f"image_path IN ({quoted})"


def ingest_images(scan_dir: str = "", rebuild: bool = False, batch_size: int | None = None) -> str:
    """扫描目录向量化入库；按 (image_path, mtime_ns) 增量跳过未变更图片。

    changed 图片先成功写入新行，再按改前抓取的 _rowid 一次性删除旧行（同路径多行
    全部替换）。若拿不到 _rowid，退回旧行为：按 image_path 先删后插，并在结果中
    报告并发误删风险。
    """
    scan_dir = scan_dir or cfg.IMAGE_SCAN_DIR_DEFAULT
    paths = _scan_images(scan_dir)
    if not paths:
        return f"❌ 目录无图片可入库: {scan_dir}"

    table = get_image_table()
    if rebuild:
        table.delete("true")

    existing: dict[str, list[int]] = {}
    rowids_by_path: dict[str, list[int]] = {}
    if not rebuild and image_count() > 0:
        existing, rowids_by_path = _load_image_state(table)

    scanned = [(path, os.stat(path).st_mtime_ns) for path in paths]
    plan = plan_image_ingest(existing, scanned)
    todo = plan["new"] + plan["changed"]
    skipped = len(plan["skipped"])
    if not todo:
        return f"✅ 无新增/变更图片（共 {len(paths)} 张，全部已入库）"

    mtime_by_path = dict(scanned)
    changed = set(plan["changed"])
    # 只有 changed 路径都存在可用的旧 _rowid 时才走「先插后删」。
    use_rowid = bool(changed) and all(rowids_by_path.get(path) for path in changed)
    fallback = bool(changed) and not use_rowid
    if fallback:
        print(
            "[image] 未能取得全部旧行 _rowid，回退为按 image_path 先删后插；"
            "并发写入时可能误删同路径新行。",
            file=sys.stderr,
        )

    bs = batch_size or cfg.IMAGE_BATCH_SIZE
    added = failed = 0
    added_paths: set[str] = set()
    for start in range(0, len(todo), bs):
        chunk = todo[start : start + bs]
        embedded = embed_images(chunk, bs)
        rows = []
        for path, vec in embedded:
            rows.append({
                "image_path": path,
                "file_name": Path(path).name,
                "source_doc": Path(path).parent.name,
                "caption": "",
                "mtime_ns": mtime_by_path[path],
                "vector": vec.tolist(),
            })
        failed += len(chunk) - len(rows)
        if rows:
            if fallback:  # 无 _rowid：只能先删旧行，避免新行被一并删掉
                changed_here = {row["image_path"] for row in rows} & changed
                if changed_here:
                    table.delete(_image_paths_where(list(changed_here)))
            table.add(rows)
        added += len(rows)
        added_paths.update(row["image_path"] for row in rows)
        print(f"[image] 入库进度 {added}/{len(todo)}", file=sys.stderr)

    replaced = 0
    if use_rowid:
        old_rowids = sorted({
            rowid
            for path in added_paths
            for rowid in rowids_by_path.get(path, [])
        })
        if old_rowids:
            deleted = table.delete(
                "_rowid IN (" + ",".join(str(rowid) for rowid in old_rowids) + ")"
            )
            replaced = int(getattr(deleted, "num_deleted_rows", 0) or 0)

    total = image_count()
    skip_note = f"，损坏跳过 {failed} 张" if failed else ""
    dup_note = f"，批内重复 {len(plan['duplicates'])} 张" if plan["duplicates"] else ""
    replace_note = f"，替换旧行 {replaced}" if replaced else ""
    risk_note = "，未取得 _rowid：旧行按路径删除，存在并发误删风险" if fallback else ""
    return (
        f"✅ 图像入库完成：新增/更新 {added} 张，未变更跳过 {skipped} 张"
        f"{dup_note}{skip_note}{replace_note}{risk_note}，表内共 {total} 张"
        f"（库: {cfg.resolve_db_path()} / 表: {cfg.IMAGE_TABLE_NAME}）"
    )


# =============================================================
# 检索
# =============================================================

def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _like_escape(value: str) -> str:
    """LIKE 模式转义：先转义反斜杠本身，再转义 % 与 _，最后处理单引号。"""
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("'", "''")


def search_images_structured(
    query: str,
    limit: int = 8,
    source_filter: str = "",
) -> dict:
    """以文搜图。返回 structured：results[{image_path, source_doc, similarity,...}] + trace。"""
    limit = min(max(limit, 1), 50)
    vec = embed_query_text(query)
    q = get_image_table().search(vec, vector_column_name="vector").metric("cosine").limit(limit * 4)
    if source_filter:
        pattern = _like_escape(source_filter)
        q = q.where(
            f"image_path LIKE '%{pattern}%' ESCAPE '\\' OR "
            f"source_doc LIKE '%{pattern}%' ESCAPE '\\'"
        )
    rows = q.to_list()

    results = []
    for r in rows:
        dist = float(r.get("_distance", 1.0))
        results.append({
            "image_path": r["image_path"],
            "file_name": r["file_name"],
            "source_doc": r["source_doc"],
            "caption": r.get("caption", ""),
            "similarity": round(max(0.0, 1.0 - dist), 4),
        })
        if len(results) >= limit:
            break
    return {
        "results": results,
        "trace": {
            "mode": "image",
            "model": cfg.LOCAL_IMAGE_EMBED_MODEL,
            "dim": cfg.LOCAL_IMAGE_EMBED_DIM,
            "table_rows": image_count(),
        },
    }


def image_identity() -> str:
    return f"local / {cfg.LOCAL_IMAGE_EMBED_MODEL} / {cfg.LOCAL_IMAGE_EMBED_DIM} 维 / 表 {cfg.IMAGE_TABLE_NAME}"
