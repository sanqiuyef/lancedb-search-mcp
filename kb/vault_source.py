# -*- coding: utf-8 -*-
r"""Obsidian vault 唯一来源层：外部文件先落到 vault Markdown，再进入向量库。

规则（2026-11-15）：
- vault 内的 ``.md/.txt`` 等文本文件直接使用，source 为 vault 相对路径；
- vault 外文本文件复制到转换目录，source 指向 vault 内的副本；
- PDF 调 :func:`kb.pdf_convert.convert_pdf` 转 Markdown（不回退纯文本层）；
- ``.docx/.pptx/.xlsx`` 先 docling、失败再 markitdown，两者都失败抛异常；
- 生成的 Markdown 手工渲染 YAML frontmatter（source 用单引号包裹原始绝对路径）；
- 同名目标已存在且来源不同时追加 ``-<sha1[:8]>``，不覆盖用户文件。

本模块只负责路径与转换，不访问数据库，也不依赖 ``kb.ingest``，避免循环导入。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config as cfg

# 可直接复制进 vault 的文本格式（不改变内容，只改变存放位置）
TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".json",
    ".yaml", ".yml", ".toml", ".html", ".htm",
}
# 需要转成 Markdown 的二进制格式
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
BINARY_EXTENSIONS = {".pdf"} | OFFICE_EXTENSIONS

PAPER_DIR = ("08-literature-文献", "papers-论文全文")
WEB_DIR = ("00-inbox-收件箱", "from-web-网页")

GENERATOR = "lancedb-search"

_FRONTMATTER_RE = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?)(?:\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_YAML_KEY_RE = re.compile(r"^\s*[A-Za-z0-9_-]+\s*:")
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# MCP 工具线程与 watcher 线程可能同时准备同一个文件（PDF 转换耗时长）。
# 目标选择、旧文件备份与原子落盘必须在同一把锁内完成。
_PREPARE_LOCK = threading.RLock()


class VaultSourceError(RuntimeError):
    """vault 来源准备失败（转换失败、格式不支持、目标无法安全落盘等）。"""


@dataclass(frozen=True)
class PreparedSource:
    """准备进入向量库的 vault 内 Markdown/文本来源。"""

    md_path: str
    source: str
    converted: bool = False
    converter: str = ""
    original_path: str = ""
    # 本次调用是否实际重转/重写（复用已有产物时为 False）
    regenerated: bool = False
    # 原文件推断出的类别（paper/documentation/note...），分块时显式使用
    category: str = ""


# =============================================================
# vault 路径
# =============================================================

def vault_root() -> str:
    """当前 vault 根的绝对路径（每次调用重新读环境变量，不在 import 时固化）。"""
    return os.path.abspath(os.path.expanduser(cfg.resolve_vault_root()))


def _norm_path(value: "str | os.PathLike") -> str:
    """比较用归一：realpath 解析符号链接后 normcase（Windows 不区分大小写）。"""
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))
    )


def is_under_vault(path: "str | os.PathLike | None") -> bool:
    """path 是否位于 vault 内（含 vault 根本身，按 realpath 与目录边界判断）。"""
    if not path:
        return False
    root = _norm_path(vault_root())
    try:
        target = _norm_path(path)
        return os.path.commonpath([root, target]) == root
    except (TypeError, ValueError):  # Windows 不同盘符
        return False


def rel_to_vault(path: "str | os.PathLike") -> str:
    """path → vault 相对路径（OS 原生分隔符，与既有 my_docs.source 一致）。

    使用 realpath 判断归属，符号链接按实际目标计算相对路径。
    """
    target = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(path)))
    )
    if not is_under_vault(target):
        raise ValueError(f"路径不在 vault 内: {path}")
    return os.path.relpath(target, os.path.realpath(vault_root()))


def target_dir_for(category: str, source_path: str) -> Path:
    """按类别选择 vault 内的落点目录。

    - ``paper`` → ``08-literature-文献/papers-论文全文``
    - 网页（category=web/网页 或 source_path 是 URL）→ ``00-inbox-收件箱/from-web-网页``
    - 其他 → ``raw-素材/converted-转换``
    """
    category_value = (category or "").strip().lower()
    source_value = (source_path or "").strip().lower()
    if category_value in {"web", "web-clip", "网页"} or source_value.startswith(
        ("http://", "https://")
    ):
        return Path(vault_root()).joinpath(*WEB_DIR)
    if category_value == "paper":
        return Path(vault_root()).joinpath(*PAPER_DIR)
    return Path(vault_root()) / cfg.VAULT_CONVERT_DIR


# =============================================================
# YAML frontmatter（手工渲染，避免 PyYAML 默认风格改写 Windows 路径）
# =============================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _single_quote(value: object) -> str:
    """YAML 单引号标量：反斜杠保持字面量，内部单引号写两遍。"""
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return "'" + text.replace("'", "''") + "'"


def _render_frontmatter(category: str, original_path: str, ext: str,
                        converter: str, created: "str | None" = None,
                        summary: "str | None" = None) -> str:
    """渲染转换产物 frontmatter：paper → literature，其余 → converted/fleeting。"""
    ext_label = (ext or Path(original_path).suffix).lstrip(".").lower() or "file"
    title = Path(original_path).stem
    if summary is None:
        summary = f"由 {ext_label} 经 {converter} 转换"
    is_paper = (category or "").strip().lower() == "paper"
    lines = [
        "---",
        f"title: {_single_quote(title)}",
        f"generator: {GENERATOR}",
        f"tags: [{'literature' if is_paper else 'converted'}]",
        f"created: {created or _utc_now().strftime('%Y-%m-%d')}",
        f"type: {'literature' if is_paper else 'fleeting'}",
    ]
    if is_paper:
        lines.append("status: unread")
    lines.extend([
        f"source: {_single_quote(os.path.abspath(original_path))}",
        f"summary: {summary}",
        "---",
    ])
    return "\n".join(lines)


def _body_without_frontmatter(text: str) -> str:
    """取正文（去掉开头的生成 frontmatter），用于内容是否变化的比较。"""
    return strip_frontmatter(text or "").lstrip("\r\n")


def strip_frontmatter(text: str) -> str:
    """去掉 Markdown 开头的 YAML frontmatter；不像 YAML 时原样返回。

    Obsidian 笔记里 ``---`` 也常用作正文分隔线：只有空块，或块内至少有一行
    ``key:`` 形式时才按 frontmatter 处理，避免把正文首行误删。
    """
    if not text:
        return text
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    block = match.group(1) or ""
    lines = [line for line in block.splitlines() if line.strip()]
    if lines and not any(_YAML_KEY_RE.match(line) for line in lines):
        return text
    return text[match.end():].lstrip("\r\n")


def _frontmatter_fields(text: str) -> "dict[str, str]":
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}
    fields: "dict[str, str]" = {}
    for line in (match.group(1) or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        raw = value.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            quote = raw[0]
            raw = raw[1:-1]
            raw = raw.replace(quote * 2, quote) if quote == "'" else raw
        fields[key.strip().lower()] = raw
    return fields


def _frontmatter_source(text: str) -> str:
    return _frontmatter_fields(text).get("source", "")


def _frontmatter_generator(text: str) -> str:
    return _frontmatter_fields(text).get("generator", "")


def _is_generated_for(text: str, original: str) -> bool:
    """md 是否由本工具为同一原文件生成（generator 标记 + source 匹配）。"""
    return (
        _frontmatter_generator(text) == GENERATOR
        and _same_original(_frontmatter_source(text), original)
    )


def _frontmatter_converter(text: str) -> str:
    summary = _frontmatter_fields(text).get("summary", "").lower()
    for name in ("mineru", "docling", "markitdown"):
        if name in summary:
            return {"mineru": "MinerU", "docling": "docling",
                    "markitdown": "markitdown"}[name]
    return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _same_original(value: str, original: str) -> bool:
    if not value or not original:
        return False
    try:
        return _norm_path(value) == _norm_path(original)
    except (TypeError, ValueError):
        return False


def _is_fresh(target: Path, original: str) -> bool:
    """转换产物是否不早于原始文件；无法取 mtime 时按可复用处理。"""
    try:
        return target.stat().st_mtime >= Path(original).stat().st_mtime
    except OSError:
        return True


def _resolve_category(original: str, category_hint: str) -> str:
    """类别优先取调用方提示，否则按原文件路径推断（延迟导入避免循环依赖）。"""
    if category_hint:
        return category_hint
    try:
        from . import ingest as kb_ingest

        return kb_ingest.guess_category(original)
    except Exception:
        return ""


def _reuse_generated(target: Path, original: str,
                     category: str = "") -> "PreparedSource | None":
    """目标 md 由同一原文件生成且不过期时直接复用（regenerated=False）。"""
    if not target.is_file():
        return None
    text = _read_text(target)
    if not _is_generated_for(text, original):
        return None
    if not _is_fresh(target, original):
        return None
    return PreparedSource(
        md_path=str(target),
        source=rel_to_vault(target),
        converted=True,
        converter=_frontmatter_converter(text),
        original_path=os.path.abspath(original),
        regenerated=False,
        category=_resolve_category(original, category),
    )


def _generated_candidates(target_dir: Path, stem: str,
                          original: str) -> "list[Path]":
    """转换产物的候选路径：裸名 → 路径指纹后缀 → 内容指纹后缀。"""
    digest = _path_digest(original)
    return [
        target_dir / f"{stem}.md",
        target_dir / f"{stem}-{digest}.md",
        target_dir / f"{stem}-{digest}-{_file_sha1(original)[:8]}.md",
    ]


def _choose_generated_target(target_dir: Path, stem: str,
                             original: str) -> Path:
    """选定可写的转换产物路径；已有用户文件时加后缀，绝不覆盖。"""
    for candidate in _generated_candidates(target_dir, stem, original):
        if not candidate.exists():
            return candidate
        if _is_generated_for(_read_text(candidate), original):
            return candidate
    raise VaultSourceError(
        f"目标文件已存在且不是本工具的转换产物，拒绝覆盖: "
        f"{_generated_candidates(target_dir, stem, original)[-1]}"
        f"（来源 {original}）"
    )


def generated_markdown_candidates(filepath: str,
                                  category_hint: str = "") -> "list[str]":
    """返回某二进制文件对应的已有转换产物（仅本工具为该原文件生成的 md）。"""
    original = os.path.abspath(os.path.expanduser(str(filepath)))
    category = _resolve_category(original, category_hint)
    target_dir = target_dir_for(category, original)
    paths = _generated_candidates(target_dir, Path(original).stem, original)
    return [
        str(path) for path in paths
        if path.is_file() and _is_generated_for(_read_text(path), original)
    ]


def _same_volume(left: "str | os.PathLike", right: "str | os.PathLike") -> bool:
    try:
        return os.stat(left).st_dev == os.stat(right).st_dev
    except OSError:
        return False


def _temp_parent_for(target: Path) -> str:
    """选一个与目标同卷、且位于 vault 外的临时目录。

    优先系统 temp；跨盘时退到 vault 根的父目录（同盘且不在 vault 内）。
    极少数情况下两者都不可用（如 vault 根就是盘符根），最终回退目标目录
    下的隐藏 ``.tmp`` 文件——扩展名不受扫描器识别，不会被当作文档入库。
    """
    candidates = [tempfile.gettempdir(), os.path.dirname(vault_root())]
    for base in candidates:
        if not base or not os.path.isdir(base):
            continue
        try:
            if is_under_vault(base):
                continue
        except (OSError, ValueError):
            continue
        if _same_volume(base, target.parent):
            return base
    return str(target.parent)


def _atomic_write_text(target: Path, text: str,
                       guard: "Callable[[], None] | None" = None) -> None:
    """写临时文件后 os.replace 原子落盘；跨盘失败时改用同目录隐藏临时文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)

    def _write_temp(directory: str, prefix: str) -> str:
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
        return name

    temp_name = _write_temp(_temp_parent_for(target), "lancedb-write-")
    try:
        if guard is not None:
            guard()  # 最终替换前再确认目标没有被用户/其他进程改成外来文件
        try:
            os.replace(temp_name, target)
        except OSError:
            # Windows 跨盘 os.replace 不允许：退回目标同目录的隐藏临时文件。
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            temp_name = _write_temp(str(target.parent), ".lancedb-write-")
            if guard is not None:
                guard()
            os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _backup_target(target: Path) -> Path:
    """把旧文件重命名为 ``<stem>.bak-<UTC秒>.md``（同目录，不覆盖已有备份）。"""
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.stem}.bak-{stamp}.md")
    index = 1
    while backup.exists():
        backup = target.with_name(f"{target.stem}.bak-{stamp}-{index}.md")
        index += 1
    os.replace(target, backup)
    return backup


def _install_generated_text(target: Path, body: str, category: str,
                            original: str, ext: str, converter: str,
                            summary: "str | None" = None) -> Path:
    """备份旧产物后原子写入新产物；用户文件一律拒绝覆盖。

    目标已存在且最终内容（含 frontmatter）完全一致时只刷新 mtime，避免
    无变化时反复备份；内容有任何差异则先备份再写。
    """
    body = (body or "").lstrip("\r\n")
    front = _render_frontmatter(
        category, original, ext, converter,
        created=_utc_now().strftime("%Y-%m-%d"), summary=summary,
    )
    text = front + "\n" + body

    if target.exists():
        current = _read_text(target)
        if not _is_generated_for(current, original):
            raise VaultSourceError(
                f"目标 md 不是本工具生成的产物，拒绝覆盖: {target}"
                f"（来源 {original}）"
            )
        if current == text:
            try:
                os.utime(target, None)
            except OSError:
                pass
            return target
        _backup_target(target)

    def guard() -> None:
        if target.exists() and not _is_generated_for(_read_text(target), original):
            raise VaultSourceError(
                f"目标 md 在写入前被改为非生成文件，拒绝覆盖: {target}"
                f"（来源 {original}）"
            )

    _atomic_write_text(target, text, guard=guard)
    return target


# =============================================================
# 文本复制
# =============================================================

def _file_sha1(path: "str | Path") -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_digest(path: "str | Path") -> str:
    raw = os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]


def _copy_text_into_vault(source_path: str, target_dir: Path,
                          category: str = "") -> "tuple[Path, bool]":
    """把 vault 外文本复制成固定目标 ``<stem>-<path_digest>.md``。

    同一绝对路径始终对应同一目标；内容变化时先备份旧文件再原子更新。
    目标没有 generator 标记或 source 不匹配时视为用户文件，拒绝覆盖。
    """
    source = Path(source_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}-{_path_digest(source)}.md"

    raw = _read_text(source)
    body = (
        _body_without_frontmatter(raw)
        if source.suffix.lower() == ".md" else raw
    )
    ext = source.suffix
    ext_label = (ext or "").lstrip(".").lower() or "file"

    if target.exists():
        current = _read_text(target)
        if not _is_generated_for(current, source):
            raise VaultSourceError(
                f"目标文件已存在且不是本工具生成的副本，拒绝覆盖: {target}"
                f"（来源 {source_path}）"
            )
        if _body_without_frontmatter(current) == body:
            return target, False
        _backup_target(target)

    front = _render_frontmatter(
        category, str(source.resolve()), ext, "copy",
        created=_utc_now().strftime("%Y-%m-%d"),
        summary=f"由 {ext_label} 复制入库",
    )

    def guard() -> None:
        if target.exists() and not _is_generated_for(_read_text(target), source):
            raise VaultSourceError(
                f"目标文件在写入前被改为用户文件，拒绝覆盖: {target}"
                f"（来源 {source_path}）"
            )

    _atomic_write_text(target, front + "\n" + body, guard=guard)
    return target, True


# =============================================================
# PDF / Office 转换
# =============================================================

def _call_convert_pdf(original: str, export_dir: Path) -> dict:
    from . import pdf_convert

    try:
        return pdf_convert.convert_pdf(original, export_dir=export_dir)
    except Exception as exc:  # 转换失败必须显式上报，不回退纯文本层
        raise VaultSourceError(
            f"PDF 转 Markdown 失败（{original}）: {exc}"
        ) from exc


def _exported_markdown(info: dict, export_dir: Path, stem: str,
                       original: str) -> Path:
    """取转换产物路径；只有 md_path 时补一次标准布局导出。"""
    if not isinstance(info, dict):
        raise VaultSourceError(f"PDF 转换返回结果无效（{original}）")

    exported = info.get("exported_md")
    if exported and Path(exported).is_file():
        return Path(exported)

    md_path = info.get("md_path")
    if md_path and Path(md_path).is_file():
        from . import pdf_convert

        try:
            return Path(pdf_convert.export_copy(Path(md_path), export_dir, stem))
        except Exception as exc:
            raise VaultSourceError(
                f"PDF 转换产物导出失败（{original}）: {exc}"
            ) from exc

    raise VaultSourceError(f"PDF 转换未产出 Markdown（{original}）")


def _install_images(exported_md: Path, target_dir: Path, final_stem: str) -> None:
    """把转换产物旁的 ``<stem>_images/`` 复制到最终目标目录。"""
    source_images = exported_md.parent / f"{exported_md.stem}_images"
    if source_images.is_dir():
        shutil.copytree(
            source_images,
            target_dir / f"{final_stem}_images",
            dirs_exist_ok=True,
        )


def _prepare_pdf(filepath: str, category_hint: str) -> PreparedSource:
    original = os.path.abspath(filepath)
    category = _resolve_category(original, category_hint)
    target_dir = target_dir_for(category, original)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(original).stem

    # 先看裸名与路径指纹后缀是否有可复用产物，都不可复用才真正转换。
    desired, suffixed, _ = _generated_candidates(target_dir, stem, original)
    for candidate in (desired, suffixed):
        reused = _reuse_generated(candidate, original, category)
        if reused is not None:
            return reused

    final_md = _choose_generated_target(target_dir, stem, original)
    staging = Path(tempfile.mkdtemp(prefix="lancedb-pdf-"))
    try:
        info = _call_convert_pdf(original, staging)
        exported = _exported_markdown(info, staging, stem, original)
        body = _body_without_frontmatter(_read_text(exported))
        if exported.stem != final_md.stem:
            body = body.replace(
                f"{exported.stem}_images/", f"{final_md.stem}_images/"
            )
        _install_images(exported, target_dir, final_md.stem)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _install_generated_text(
        final_md, body, category, original, ".pdf", "MinerU")
    return PreparedSource(
        md_path=str(final_md),
        source=rel_to_vault(final_md),
        converted=True,
        converter="MinerU",
        original_path=original,
        regenerated=True,
        category=category,
    )


def _docling_markdown(filepath: str) -> str:
    """docling 转换入口（独立函数便于测试替换）。"""
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(filepath)
    return result.document.export_to_markdown()


def _markitdown_text(filepath: str) -> str:
    """markitdown 回退入口（独立函数便于测试替换）。"""
    from markitdown import MarkItDown

    return MarkItDown().convert(filepath).text_content


def _convert_office(filepath: str) -> "tuple[str, str]":
    """docx/pptx/xlsx → (Markdown 文本, 转换器名)；docling 失败回退 markitdown。"""
    errors = []
    try:
        markdown = _docling_markdown(filepath)
        if markdown and markdown.strip():
            return markdown, "docling"
        errors.append("docling: 输出为空")
    except Exception as exc:
        errors.append(f"docling: {exc}")

    try:
        markdown = _markitdown_text(filepath)
        if markdown and markdown.strip():
            return markdown, "markitdown"
        errors.append("markitdown: 输出为空")
    except Exception as exc:
        errors.append(f"markitdown: {exc}")

    raise VaultSourceError(
        f"Office 文档转换失败（{filepath}）：" + "；".join(errors)
    )


def _prepare_office(filepath: str, category_hint: str) -> PreparedSource:
    original = os.path.abspath(filepath)
    ext = Path(original).suffix.lower()
    category = _resolve_category(original, category_hint)
    target_dir = target_dir_for(category, original)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(original).stem

    # 复用检查必须先于 docling/markitdown：裸名与路径指纹后缀都检查完，
    # 都不可复用才调用昂贵的转换器。
    desired, suffixed, _ = _generated_candidates(target_dir, stem, original)
    for candidate in (desired, suffixed):
        reused = _reuse_generated(candidate, original, category)
        if reused is not None:
            return reused

    final_md = _choose_generated_target(target_dir, stem, original)
    markdown, converter = _convert_office(original)
    _install_generated_text(
        final_md,
        _body_without_frontmatter(markdown),
        category, original, ext, converter,
    )
    return PreparedSource(
        md_path=str(final_md),
        source=rel_to_vault(final_md),
        converted=True,
        converter=converter,
        original_path=original,
        regenerated=True,
        category=category,
    )


# =============================================================
# 统一入口
# =============================================================

def prepare_markdown(filepath: str, category_hint: str = "") -> PreparedSource:
    """把任意受支持文件准备成 vault 内 Markdown/文本来源。

    - vault 内文本：原地使用；
    - vault 外文本：复制为固定 ``<stem>-<path_digest>.md``；
    - PDF：MinerU 转 Markdown 到 target_dir；
    - docx/pptx/xlsx：docling → markitdown；
    - ``.doc`` 明确报错并提示另存为 docx。

    整个准备过程持有模块级 RLock，避免 MCP 与 watcher 并发覆盖同一目标。
    """
    with _PREPARE_LOCK:
        return _prepare_markdown_locked(filepath, category_hint)


def _prepare_markdown_locked(filepath: str,
                             category_hint: str) -> PreparedSource:
    original = os.path.abspath(os.path.expanduser(str(filepath)))
    if not os.path.isfile(original):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    ext = Path(original).suffix.lower()
    if ext == ".doc":
        raise VaultSourceError(
            f"不支持 .doc 格式，请另存为 .docx 后再入库: {original}"
        )

    category = _resolve_category(original, category_hint)

    if ext in TEXT_EXTENSIONS:
        if is_under_vault(original):
            return PreparedSource(
                md_path=original,
                source=rel_to_vault(original),
                converted=False,
                converter="",
                original_path=original,
                regenerated=False,
                category=category,
            )
        copied, regenerated = _copy_text_into_vault(
            original, target_dir_for(category, original), category)
        return PreparedSource(
            md_path=str(copied),
            source=rel_to_vault(copied),
            converted=False,
            converter="",
            original_path=original,
            regenerated=regenerated,
            category=category,
        )

    if ext == ".pdf":
        return _prepare_pdf(original, category)
    if ext in OFFICE_EXTENSIONS:
        return _prepare_office(original, category)

    raise VaultSourceError(
        f"不支持的文件格式: {ext or '(无扩展名)'}；请先转换为 Markdown"
        f"或 PDF/docx/pptx/xlsx: {original}"
    )


# =============================================================
# 网页剪藏（供 kb.web 后续接入）
# =============================================================

def _safe_filename_stem(title: str, fallback: str = "web-clip") -> str:
    stem = _ILLEGAL_FILENAME.sub("-", title or "").strip(" .-")
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        return fallback
    return stem[:80].rstrip(" .-") or fallback


def _canonical_url(url: str) -> str:
    try:
        from .web import canonicalize_url

        return canonicalize_url(url)
    except Exception:
        return (url or "").strip()


def save_web_markdown(url: str, title: str, text: str) -> Path:
    """把网页正文写成 vault 剪藏 Markdown，返回落盘路径。

    文件名 ``<safe-title>-<sha1(canonical)[:8]>.md``；同一 URL 重复剪藏
    会覆盖同一文件（幂等）。frontmatter 记录标题、URL 与剪藏时间。
    """
    canonical = _canonical_url(url)
    digest = hashlib.sha1(
        (canonical or url or "").encode("utf-8", errors="replace")
    ).hexdigest()[:8]
    target_dir = target_dir_for("web", url)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_safe_filename_stem(title)}-{digest}.md"

    now = _utc_now()
    front = "\n".join([
        "---",
        f"title: {_single_quote(title or '')}",
        f"generator: {GENERATOR}",
        "tags: [web-clip]",
        f"created: {now.strftime('%Y-%m-%d')}",
        "type: fleeting",
        f"source: {_single_quote(canonical or url)}",
        f"url: {_single_quote(url)}",
        f"clipped: {_single_quote(now.strftime('%Y-%m-%dT%H:%M:%SZ'))}",
        "---",
    ])
    body = strip_frontmatter(text or "").lstrip("\r\n")
    with _PREPARE_LOCK:
        _atomic_write_text(target, front + "\n" + body)
    return target
