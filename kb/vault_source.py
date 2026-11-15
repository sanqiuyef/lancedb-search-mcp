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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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

_FRONTMATTER_RE = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?)(?:\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


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


# =============================================================
# vault 路径
# =============================================================

def vault_root() -> str:
    """当前 vault 根的绝对路径（每次调用重新读环境变量，不在 import 时固化）。"""
    return os.path.abspath(os.path.expanduser(cfg.resolve_vault_root()))


def _norm_path(value: "str | os.PathLike") -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))


def is_under_vault(path: "str | os.PathLike | None") -> bool:
    """path 是否位于 vault 内（含 vault 根本身，按目录边界，不区分大小写）。"""
    if not path:
        return False
    root = _norm_path(vault_root())
    try:
        target = _norm_path(path)
        return os.path.commonpath([root, target]) == root
    except (TypeError, ValueError):  # Windows 不同盘符
        return False


def rel_to_vault(path: "str | os.PathLike") -> str:
    """path → vault 相对路径（统一正斜杠，便于 Obsidian 与跨平台比较）。"""
    target = os.path.abspath(os.path.expanduser(str(path)))
    if not is_under_vault(target):
        raise ValueError(f"路径不在 vault 内: {path}")
    relative = os.path.relpath(target, vault_root())
    return relative.replace("\\", "/")


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
                        converter: str, created: "str | None" = None) -> str:
    """渲染转换产物 frontmatter：paper → literature，其余 → converted/fleeting。"""
    ext_label = (ext or Path(original_path).suffix).lstrip(".").lower() or "file"
    title = Path(original_path).stem
    summary = f"由 {ext_label} 经 {converter} 转换"
    is_paper = (category or "").strip().lower() == "paper"
    lines = [
        "---",
        f"title: {_single_quote(title)}",
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


def _apply_frontmatter(md_path: "str | Path", category: str, original_path: str,
                       ext: str, converter: str) -> Path:
    """把手工 frontmatter 写到转换产物开头（已有 frontmatter 先剥离，避免重复）。"""
    path = Path(md_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    body = strip_frontmatter(text).lstrip("\r\n")
    front = _render_frontmatter(
        category, original_path, ext, converter,
        created=_utc_now().strftime("%Y-%m-%d"),
    )
    path.write_text(front + "\n" + body, encoding="utf-8")
    return path


def strip_frontmatter(text: str) -> str:
    """去掉 Markdown 开头的 YAML frontmatter；没有 frontmatter 时原样返回。"""
    if not text:
        return text
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    return text[match.end():].lstrip("\r\n")


def _frontmatter_fields(text: str) -> "dict[str, str]":
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}
    fields: "dict[str, str]" = {}
    for line in match.group(1).splitlines():
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


def _target_is_foreign(target: Path, original: str) -> bool:
    """目标 md 是否存在且属于另一来源（没有可识别 frontmatter 的文件也算外来）。"""
    if not target.is_file():
        return False
    return not _same_original(_frontmatter_source(_read_text(target)), original)


def _is_fresh(target: Path, original: str) -> bool:
    """转换产物是否不早于原始文件；无法取 mtime 时按可复用处理。"""
    try:
        return target.stat().st_mtime >= Path(original).stat().st_mtime
    except OSError:
        return True


def _reuse_generated(target: Path, original: str) -> "PreparedSource | None":
    """目标 md 由同一原文件生成且不过期时直接复用。"""
    if not target.is_file():
        return None
    text = _read_text(target)
    if not _same_original(_frontmatter_source(text), original):
        return None
    if not _is_fresh(target, original):
        return None
    return PreparedSource(
        md_path=str(target),
        source=rel_to_vault(target),
        converted=True,
        converter=_frontmatter_converter(text),
        original_path=os.path.abspath(original),
    )


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


def _same_file_content(left: "str | Path", right: "str | Path") -> bool:
    try:
        if os.path.getsize(left) != os.path.getsize(right):
            return False
        return _file_sha1(left) == _file_sha1(right)
    except OSError:
        return False


def _copy_text_into_vault(source_path: str, target_dir: Path) -> Path:
    """复制 vault 外文本文件；同名不同内容时追加路径指纹，绝不覆盖已有文件。"""
    source = Path(source_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix
    stem = source.stem
    digest = _path_digest(source)

    candidates = [
        target_dir / f"{stem}{suffix}",
        target_dir / f"{stem}-{digest}{suffix}",
        target_dir / f"{stem}-{digest}-{_file_sha1(source)[:8]}{suffix}",
    ]
    for candidate in candidates:
        if not candidate.exists():
            shutil.copy2(source, candidate)
            return candidate
        if _same_file_content(source, candidate):
            return candidate

    raise VaultSourceError(
        f"目标文件已存在且内容不同，拒绝覆盖: {candidates[-1]}"
        f"（来源 {source_path}）"
    )


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


def _export_to_target(exported_md: Path, target_dir: Path, stem: str) -> Path:
    """把转换产物落到目标目录的 ``<stem>.md`` + ``<stem>_images/`` 布局。"""
    target = target_dir / f"{stem}.md"
    if _norm_path(exported_md) == _norm_path(target):
        return target

    from . import pdf_convert

    return Path(pdf_convert.export_copy(exported_md, target_dir, stem))


def _prepare_pdf(filepath: str, category_hint: str) -> PreparedSource:
    original = os.path.abspath(filepath)
    target_dir = target_dir_for(category_hint, original)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(original).stem
    desired = target_dir / f"{stem}.md"

    reused = _reuse_generated(desired, original)
    if reused is not None:
        return reused

    collision = _target_is_foreign(desired, original)
    final_stem = f"{stem}-{_path_digest(original)}" if collision else stem
    if collision:
        reused = _reuse_generated(target_dir / f"{final_stem}.md", original)
        if reused is not None:
            return reused
        # 已存在的同名 md 属于其他来源：先导到临时目录，再以带后缀的名字落盘。
        staging = target_dir / f".converting-{_path_digest(original)}"
        shutil.rmtree(staging, ignore_errors=True)
        export_dir = staging
    else:
        export_dir = target_dir

    try:
        info = _call_convert_pdf(original, export_dir)
        exported = _exported_markdown(info, export_dir, stem, original)
        final_md = _export_to_target(exported, target_dir, final_stem)
    finally:
        if collision:
            shutil.rmtree(export_dir, ignore_errors=True)

    _apply_frontmatter(final_md, category_hint, original, ".pdf", "MinerU")
    return PreparedSource(
        md_path=str(final_md),
        source=rel_to_vault(final_md),
        converted=True,
        converter="MinerU",
        original_path=original,
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
    target_dir = target_dir_for(category_hint, original)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(original).stem
    desired = target_dir / f"{stem}.md"

    reused = _reuse_generated(desired, original)
    if reused is not None:
        return reused

    markdown, converter = _convert_office(original)

    final_stem = stem
    if _target_is_foreign(desired, original):
        final_stem = f"{stem}-{_path_digest(original)}"
        reused = _reuse_generated(target_dir / f"{final_stem}.md", original)
        if reused is not None:
            return reused
    final_md = target_dir / f"{final_stem}.md"
    if final_md.exists() and _target_is_foreign(final_md, original):
        final_md = target_dir / (
            f"{stem}-{_path_digest(original)}-{_file_sha1(original)[:8]}.md"
        )
        if _target_is_foreign(final_md, original):
            raise VaultSourceError(
                f"目标文件已存在且来源不同，拒绝覆盖: {final_md}（来源 {original}）"
            )

    _apply_frontmatter(
        _write_converted_markdown(final_md, markdown),
        category_hint, original, ext, converter,
    )
    return PreparedSource(
        md_path=str(final_md),
        source=rel_to_vault(final_md),
        converted=True,
        converter=converter,
        original_path=original,
    )


def _write_converted_markdown(target: Path, markdown: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    body = strip_frontmatter(markdown).lstrip("\r\n")
    target.write_text(body, encoding="utf-8")
    return target


# =============================================================
# 统一入口
# =============================================================

def prepare_markdown(filepath: str, category_hint: str = "") -> PreparedSource:
    """把任意受支持文件准备成 vault 内 Markdown/文本来源。

    - vault 内文本：原地使用；
    - vault 外文本：复制到 target_dir_for(category_hint)；
    - PDF：MinerU 转 Markdown 到 target_dir；
    - docx/pptx/xlsx：docling → markitdown；
    - ``.doc`` 明确报错并提示另存为 docx。
    """
    original = os.path.abspath(os.path.expanduser(str(filepath)))
    if not os.path.isfile(original):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    ext = Path(original).suffix.lower()
    if ext == ".doc":
        raise VaultSourceError(
            f"不支持 .doc 格式，请另存为 .docx 后再入库: {original}"
        )

    if ext in TEXT_EXTENSIONS:
        if is_under_vault(original):
            return PreparedSource(
                md_path=original,
                source=rel_to_vault(original),
                converted=False,
                converter="",
                original_path=original,
            )
        copied = _copy_text_into_vault(original, target_dir_for(category_hint, original))
        return PreparedSource(
            md_path=str(copied),
            source=rel_to_vault(copied),
            converted=False,
            converter="",
            original_path=original,
        )

    if ext == ".pdf":
        return _prepare_pdf(original, category_hint)
    if ext in OFFICE_EXTENSIONS:
        return _prepare_office(original, category_hint)

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
        "tags: [web-clip]",
        f"created: {now.strftime('%Y-%m-%d')}",
        "type: fleeting",
        f"source: {_single_quote(canonical or url)}",
        f"url: {_single_quote(url)}",
        f"clipped: {_single_quote(now.strftime('%Y-%m-%dT%H:%M:%SZ'))}",
        "---",
    ])
    body = strip_frontmatter(text or "").lstrip("\r\n")
    target.write_text(front + "\n" + body, encoding="utf-8")
    return target
