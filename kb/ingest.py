# -*- coding: utf-8 -*-
"""文档入库内核：解析（含 OCR 链）、分块、增删改查。

解析链（2026-10-04 起）：PDF 优先本地 MinerU 转 Markdown（公式 → LaTeX，带缓存，
见 kb.pdf_convert）→ 失败回退文本层（PyMuPDF → pdfminer → PyPDF2）→ 扫描件走
OCR 链（云 MinerU → 本地 Tesseract）。
分块沿用既有参数（800 字符 / 100 overlap）。MinerU 产物含标题结构，走 Markdown 感知
分块；其余按段落。
向量在 table.add() 时由 LanceModel 的 VectorField 自动计算（官方模式）。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Dict, List

from . import config as cfg
from . import schema as kb_schema
from .embeddings import embed_texts  # noqa: F401  （供脚本/桌面端复用批量向量化）


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================
# Wiki 登记钩子（可选，失败不影响入库）
# =============================================================

_WIKI_TRUE_VALUES = {"1", "true", "yes", "on"}
_WIKI_PARSER_NAME = "kb_ingest"
_WIKI_WEB_PARSER_NAME = "kb.web"
_WIKI_PARSER_VERSION = "1"


def _wiki_enabled() -> bool:
    return os.environ.get("WIKI_ENABLED", "").strip().lower() in _WIKI_TRUE_VALUES


def _file_sha256(filepath: str) -> str:
    digest = hashlib.sha256()
    with open(filepath, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wiki_parser_config_sha256() -> str:
    raw = f"{cfg.CHUNK_SIZE}\x00{cfg.CHUNK_OVERLAP}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_web_source(value: str) -> bool:
    return (value or "").strip().lower().startswith(("http://", "https://"))


def _register_wiki_safely(source: str, filepath: str, chunks: List[Dict],
                          category: str = "") -> "str | None":
    """成功入库后把 source/修订/切片引用登记进 Wiki SQLite。

    仅当 ``WIKI_ENABLED=1`` 且 Wiki 模块可导入时执行；任何异常只写 stderr，
    不改变入库返回值与统计。返回 document_id（未启用/失败时为 None）。
    """
    if not _wiki_enabled() or not chunks:
        return None
    try:
        return _register_wiki(source, filepath, chunks, category)
    except Exception as exc:  # 登记失败绝不影响入库结果
        print(f"[wiki] 登记失败（已忽略）: {source}: {exc}", file=sys.stderr)
        _record_wiki_failure(source, filepath, exc)
        return None


def _register_wiki(source: str, filepath: str, chunks: List[Dict],
                   category: str = "") -> str:
    from .wiki import config as wiki_config
    from .wiki import db as wiki_db
    from .wiki import document_registry as registry
    from .wiki.ids import content_sha256

    config = wiki_config.WikiConfig.from_env()
    config.require_enabled()
    conn = wiki_db.connect(config.db_path, config)
    try:
        wiki_db.run_migrations(conn)

        is_web = _is_web_source(filepath)
        if is_web:
            source_type = "web"
            canonical_uri = registry.canonical_web_source(filepath)
            parser_name = _WIKI_WEB_PARSER_NAME
        else:
            source_type = "file"
            canonical_uri = registry.canonical_local_source(source)
            parser_name = _WIKI_PARSER_NAME

        display_name = os.path.basename(source.replace("\\", "/")) or source
        if not category:
            category = next(
                (str(ch.get("category", "")) for ch in chunks if ch.get("category")),
                "",
            )
        descriptor = registry.SourceDescriptor(
            source_type=source_type,
            canonical_uri=canonical_uri,
            display_name=display_name,
            category=category,
            source_path=str(filepath),
        )

        raw_sha256 = (
            _file_sha256(filepath)
            if os.path.isfile(filepath)
            else content_sha256(str(filepath))
        )
        parsed_sha256 = content_sha256(
            "\n\n".join(str(ch.get("text", "")) for ch in chunks)
        )
        revision_input = registry.RevisionInput(
            raw_sha256=raw_sha256,
            parsed_sha256=parsed_sha256,
            parser_name=parser_name,
            parser_version=_WIKI_PARSER_VERSION,
            parser_config_sha256=_wiki_parser_config_sha256(),
            source_path=str(filepath),
        )
        descriptors = []
        for index, chunk in enumerate(chunks):
            descriptors.append(
                registry.ChunkDescriptor.from_text(
                    int(chunk.get("chunk_index", index)),
                    str(chunk.get("text", "")),
                    legacy_doc_id=str(chunk.get("doc_id", "")),
                    source_value=str(chunk.get("source", source)),
                    section_path=str(chunk.get("section_path", "")),
                    page_number=chunk.get("page_number"),
                )
            )

        with wiki_db.transaction(conn):
            document_id = registry.register_source(conn, descriptor)
            handle = registry.begin_revision(conn, document_id, revision_input)
            registry.register_chunk_refs(conn, handle.revision_id, descriptors)
            registry.complete_revision(
                conn,
                handle.revision_id,
                len(chunks),
                parsed_sha256=parsed_sha256,
            )
        return document_id
    finally:
        conn.close()


def _record_wiki_failure(source: str, filepath: str, error: BaseException) -> None:
    """登记失败时尽量写一条 wiki_jobs；DB 不可用则只记日志。"""
    try:
        import json

        from .wiki import config as wiki_config
        from .wiki import db as wiki_db
        from .wiki.ids import new_ulid

        config = wiki_config.WikiConfig.from_env()
        conn = wiki_db.connect(config.db_path, config)
        try:
            wiki_db.run_migrations(conn)
            now = datetime_now_iso()
            payload = json.dumps(
                {"source": source, "filepath": str(filepath)}, ensure_ascii=False
            )
            with wiki_db.transaction(conn):
                conn.execute(
                    "INSERT INTO wiki_jobs "
                    "(job_id, job_type, status, payload_json, error_message, "
                    " attempts, created_at, updated_at, finished_at) "
                    "VALUES (?, 'ingest_registration', 'failed', ?, ?, 1, ?, ?, ?)",
                    (new_ulid(), payload, str(error)[:2000], now, now, now),
                )
        finally:
            conn.close()
    except Exception as record_error:
        print(f"[wiki] 失败任务也无法写入 wiki_jobs: {record_error}", file=sys.stderr)


# =============================================================
# 扫描 PDF OCR 链
# =============================================================

def _pdf_text_needs_ocr(text: str) -> bool:
    """判断 PDF 文本层是否缺失或几乎只包含重复水印。"""
    lines = [re.sub(r"\s+", "", line) for line in text.splitlines()]
    lines = [line for line in lines if len(line) >= 3]
    compact_text = "".join(lines)
    if len(compact_text) < cfg.OCR_MIN_TEXT_CHARS:
        if len(lines) == 1 and len(lines[0]) >= 30:
            return False
        return True
    if len(lines) >= 8:
        repeated = Counter(lines).most_common(1)[0][1]
        if len(set(lines)) <= max(3, len(lines) // 8) or repeated / len(lines) >= 0.85:
            return True
    return False


def _ocr_pdf_mineru(filepath: str) -> str:
    """MinerU SDK 精准解析扫描件 PDF，返回 Markdown。失败抛异常由调用方回退。"""
    if not cfg._MINERU_SDK_AVAILABLE:
        raise RuntimeError("MinerU SDK 未安装，请执行: pip install mineru-open-sdk")
    if not cfg.MINERU_API_KEY:
        raise RuntimeError("未配置 MINERU_API_KEY")

    print(f"[MinerU] 开始精准解析: {os.path.basename(filepath)}", file=sys.stderr)
    client = cfg._MinerU(cfg.MINERU_API_KEY)
    result = client.extract(
        filepath,
        model=cfg.MINERU_API_MODEL,
        ocr=True,
        timeout=cfg.MINERU_POLL_TIMEOUT,
    )
    markdown = result.markdown
    print(f"[MinerU] 解析完成，{len(markdown)} 字符", file=sys.stderr)
    return markdown


def _ocr_pdf(filepath: str) -> str:
    """PyMuPDF 渲染 + 本地 Tesseract 逐页识别。"""
    if not os.path.isfile(cfg.OCR_TESSERACT_CMD):
        print(f"[OCR] 未找到 Tesseract: {cfg.OCR_TESSERACT_CMD}，跳过（保留文本层）", file=sys.stderr)
        return ""

    import fitz
    from io import BytesIO

    from PIL import Image
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = cfg.OCR_TESSERACT_CMD
    document = fitz.open(filepath)
    page_count = len(document)
    limit = min(page_count, cfg.OCR_MAX_PAGES) if cfg.OCR_MAX_PAGES > 0 else page_count
    scale = cfg.OCR_DPI / 72
    matrix = fitz.Matrix(scale, scale)
    pages = []

    try:
        for page_number in range(limit):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            with Image.open(BytesIO(pixmap.tobytes("png"))) as image:
                page_text = pytesseract.image_to_string(
                    image, lang=cfg.OCR_LANG, config="--psm 6"
                )
            if page_text.strip():
                pages.append(page_text)
            if (page_number + 1) % 10 == 0 or page_number + 1 == limit:
                print(f"[OCR] {os.path.basename(filepath)}: {page_number + 1}/{limit} 页", file=sys.stderr)
    finally:
        document.close()

    if limit < page_count:
        pages.append(f"\n\n[OCR 仅处理前 {limit}/{page_count} 页，受 LANCEDB_OCR_MAX_PAGES 限制]\n")
    return "\n\n".join(pages)


# =============================================================
# 文本抽取
# =============================================================

def _pdf_text_pymupdf(filepath: str) -> str:
    """PyMuPDF 文本层抽取（质量高于 pdfminer：λ/∈ 等符号可保，无字母打散）。"""
    import fitz

    with fitz.open(filepath) as doc:
        return "\n".join(doc.load_page(i).get_text() for i in range(doc.page_count))


def _pdf_text_layer(filepath: str) -> str:
    """PDF 文本层：PyMuPDF → pdfminer → PyPDF2 三级回退。"""
    try:
        text = _pdf_text_pymupdf(filepath)
        if text.strip():
            return text
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text as pdf_extract

        text = pdf_extract(filepath) or ""
        if text.strip():
            return text
    except Exception:
        pass
    try:
        import PyPDF2

        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception:
        return ""


def _pdf_via_ocr_chain(filepath: str) -> str:
    """扫描件/重复水印件的 OCR 回退链：云 MinerU（如已配置）→ 本地 Tesseract。"""
    if cfg.MINERU_API_KEY:
        try:
            ocr_text = _ocr_pdf_mineru(filepath)
            if len(ocr_text.strip()) >= 20:
                return ocr_text
            print(f"[MinerU] 未提取到有效文字，回退到 Tesseract: {filepath}", file=sys.stderr)
        except Exception as e:
            print(f"[MinerU] 识别失败，回退到 Tesseract {filepath}: {e}", file=sys.stderr)
    try:
        ocr_text = _ocr_pdf(filepath)
        if len(ocr_text.strip()) >= 20:
            return ocr_text
        print(f"[OCR] 未提取到有效文字，保留原文本层: {filepath}", file=sys.stderr)
    except Exception as e:
        print(f"[OCR] 识别失败，保留原文本层 {filepath}: {e}", file=sys.stderr)
    return ""


def extract_content(filepath: str, export_markdown_dir: str = "") -> "tuple[str, bool]":
    """从文件提取 (文本, 是否 Markdown)。

    PDF 优先走本地 MinerU 转 Markdown（公式 → LaTeX，带磁盘缓存，见 kb.pdf_convert）；
    MinerU 不可用或失败时回退文本层抽取，再回退 OCR 链（云 MinerU → Tesseract）。
    export_markdown_dir 非空时，MinerU 产物额外导出为标准布局
    （`<目录>/<文件名>.md` + `<文件名>_images/`），供阅读/归档/二次入库。
    """
    ext = Path(filepath).suffix.lower()
    try:
        if ext == ".pdf":
            if cfg.MINERU_ENABLED:
                try:
                    from . import pdf_convert

                    if pdf_convert.mineru_available():
                        info = pdf_convert.convert_pdf(
                            filepath, export_dir=export_markdown_dir or None)
                        md = Path(info["md_path"]).read_text(encoding="utf-8")
                        if len(md.strip()) >= 20:
                            return md, True
                except Exception as e:
                    print(f"[MinerU] 本地转换失败，回退文本层 {filepath}: {e}",
                          file=sys.stderr)
            from . import pdf_convert

            text = _pdf_text_layer(filepath)
            if cfg.OCR_ENABLED and _pdf_text_needs_ocr(text):
                print(f"[OCR] 检测到扫描件或重复水印，开始识别: {filepath}", file=sys.stderr)
                ocr_text = _pdf_via_ocr_chain(filepath)
                if ocr_text:
                    return pdf_convert.normalize_text(ocr_text), False
            return pdf_convert.normalize_text(text), False
        if ext in (".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(), ext == ".md"
        if ext == ".docx":
            from docx import Document

            doc = Document(filepath)
            return "\n".join(p.text for p in doc.paragraphs), False
        if ext in (".html", ".htm"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            try:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(raw, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                return soup.get_text(separator="\n", strip=True), False
            except ImportError:
                return raw, False
        return "", False
    except Exception:
        return "", False


def extract_text(filepath: str) -> str:
    """兼容接口：仅返回文本（新代码请用 extract_content 以获知是否 Markdown）。"""
    return extract_content(filepath)[0]


# =============================================================
# 分块
# =============================================================

def guess_category(source_path: str) -> str:
    """按路径中的目录名推断类别标签（仅看目录，不看文件名；支持名称包含匹配）。

    只看目录可避免文件名里的子串误命中（如 "rapid" 含 "api"）：
    如 08-literature-文献 → paper、docs/ → documentation。
    """
    parts = [p.lower() for p in Path(source_path).parts[:-1]]  # 排除文件名
    for key, category in cfg.CATEGORY_MAPPINGS.items():
        k = key.lower()
        if any(k == p or k in p for p in parts):
            return category
    return ""


def _pre_split_long_paragraphs(text: str, limit: int) -> List[str]:
    """无空行结构的超长段落（如知网 PDF）预切到 limit 以内。"""
    result = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            result.append(para)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。！？；!?;.])\s*", para):
            while len(sent) > limit:
                if buf:
                    result.append(buf)
                    buf = ""
                result.append(sent[:limit])
                sent = sent[limit:]
            if buf and len(buf) + len(sent) > limit:
                result.append(buf)
                buf = sent
            else:
                buf += sent
        if buf:
            result.append(buf)
    return result


def _chunk_by_paragraph(text: str, source: str, category: str = "",
                        prefix: str = "") -> List[Dict]:
    """按段落分块（尾部 overlap 按配置的字符数，最多半个 CHUNK_SIZE）。"""
    paragraphs = _pre_split_long_paragraphs(text, cfg.CHUNK_SIZE)
    overlap = min(cfg.CHUNK_OVERLAP, cfg.CHUNK_SIZE // 2)
    chunks: List[Dict] = []
    current = prefix or ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) < cfg.CHUNK_SIZE:
            current += para + "\n"
        else:
            if current.strip():
                chunks.append({
                    "text": current.strip(),
                    "source": source,
                    "chunk_index": len(chunks),
                    "category": category,
                })
                if overlap > 0:
                    stripped = current.strip()
                    overlap_text = (
                        stripped[-overlap:] if len(stripped) > overlap else stripped
                    )
                    current = (prefix or "") + overlap_text + "\n" + para + "\n"
                else:
                    current = (prefix or "") + para + "\n"
            else:
                current = (prefix or "") + para + "\n"

    if current.strip():
        chunks.append({
            "text": current.strip(),
            "source": source,
            "chunk_index": len(chunks),
            "category": category,
        })
    return chunks


def _chunk_markdown(text: str, source: str) -> List[Dict]:
    """Markdown 感知分块：按 #/##/### 标题分割保持语义完整。"""
    cat = guess_category(source)
    lines = text.split("\n")
    headings = []
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.+?)(?:\s+#+)?$", line.strip())
        if m:
            headings.append((len(m.group(1)), m.group(2).strip(), i))

    if len(headings) < 2:
        return _chunk_by_paragraph(text, source, cat)

    sections = []
    for j, (level, title, start) in enumerate(headings):
        end = headings[j + 1][2] if j + 1 < len(headings) else len(lines)
        sections.append((title, "\n".join(lines[start:end]).strip()))

    chunks: List[Dict] = []
    for title, section_text in sections:
        if not section_text:
            continue
        if len(section_text) <= cfg.CHUNK_SIZE * 1.5:
            chunks.append({
                "text": f"# {title}\n\n{section_text}",
                "source": source,
                "chunk_index": len(chunks),
                "category": cat,
            })
        else:
            chunks.extend(_chunk_by_paragraph(section_text, source, cat,
                                              prefix=f"# {title}\n\n"))
    return chunks


def chunk_text(text: str, source: str, is_markdown: "bool | None" = None) -> List[Dict]:
    """分块入口：Markdown（含 MinerU 转换的 PDF）走标题感知分块，其余按段落。

    is_markdown=None 时按 source 扩展名推断（向后兼容）。
    """
    if is_markdown is None:
        is_markdown = source.lower().endswith(".md")
    if is_markdown:
        return _chunk_markdown(text, source)
    return _chunk_by_paragraph(text, source, guess_category(source))


# =============================================================
# 向量化写入（LanceModel 自动向量化）
# =============================================================

def _rows_from_chunks(chunks: List[Dict]) -> List[Dict]:
    """chunk dict → 表行（vector 由 VectorField 自动补全）。"""
    now = datetime_now_iso()
    rows = []
    for ch in chunks:
        rows.append({
            "text": ch["text"],
            "source": ch["source"],
            "chunk_index": int(ch.get("chunk_index", 0)),
            "category": ch.get("category", ""),
            "doc_id": ch.get("doc_id", ""),
            "ingested_at": ch.get("ingested_at", now),
        })
    return rows


def _rows_with_vectors(rows: List[Dict]) -> List[Dict]:
    """主线程预计算向量后随行写入（显式向量列，lancedb 不再回调自动向量化）。"""
    from . import embeddings as kb_embeddings

    vectors = kb_embeddings.embed_texts([r["text"] for r in rows])
    return [{**r, "vector": v} for r, v in zip(rows, vectors)]


def _dedupe_chunks_by_doc_id(chunks: List[Dict]) -> List[Dict]:
    """批内按 doc_id 去重（保留首条）；空 doc_id 视为无指纹，不参与去重。"""
    seen: set = set()
    deduped: List[Dict] = []
    for chunk in chunks:
        doc_id = chunk.get("doc_id", "")
        if doc_id:
            if doc_id in seen:
                continue
            seen.add(doc_id)
        deduped.append(chunk)
    return deduped


def add_chunks(table, chunks: List[Dict], batch_size: int = 32,
               precompute_vectors: bool = True, write_batch: int = 256) -> int:
    """分块批量入库并返回写入行数（写入前按 doc_id 做批内去重）。

    默认主线程预计算向量并显式写入：绕开 LanceModel 自动向量化回调
    （该回调在 Python 3.13 + torch 下有原生崩溃记录：_PyThreadState_Attach），
    失败时自动回退官方自动向量化模式。

    嵌入按 batch_size 小批走（控制显存），写入按 write_batch 攒大批提交
    （减少 Lance 碎片，实测大表写入速度显著提升）。
    """
    chunks = _dedupe_chunks_by_doc_id(chunks)
    added = 0
    step = max(batch_size, write_batch if precompute_vectors else batch_size)
    for i in range(0, len(chunks), step):
        batch = chunks[i:i + step]
        rows = _rows_from_chunks(batch)
        if precompute_vectors:
            try:
                rows_with_vectors = []
                for j in range(0, len(rows), batch_size):
                    rows_with_vectors.extend(_rows_with_vectors(rows[j:j + batch_size]))
                table.add(rows_with_vectors)
                added += len(batch)
                continue
            except Exception as e:
                print(f"[ingest] 显式向量写入失败，回退自动向量化: {e}", file=sys.stderr)
                precompute_vectors = False
                step = batch_size
        for j in range(0, len(rows), batch_size):
            table.add(rows[j:j + batch_size])
            added += len(rows[j:j + batch_size])
    return added


# =============================================================
# 增删改查
# =============================================================

# 模块级写锁：watcher 线程与 MCP 线程都会执行「读旧状态 → 写新行 → 清理旧行」，
# 多步流程必须串行，避免交错删除对方刚写入的行。
_write_lock = threading.RLock()

_DOC_ID_DELETE_BATCH = 500  # IN 列表分批上限，控制单条 SQL 规模


def _basename_of(value: str) -> str:
    """跨平台取 basename（同时接受 / 与 \\ 分隔符）。"""
    return os.path.basename((value or "").replace("\\", "/"))


def _normalized_path(value: str) -> str:
    """路径后缀比较用归一：normcase 后统一为 / 分隔。"""
    return os.path.normcase(value or "").replace("\\", "/")


def _path_is_suffix(filepath: str, source: str) -> bool:
    """source 是否是 filepath 的完整路径后缀（按目录边界，不是子串）。"""
    fp = _normalized_path(filepath).rstrip("/")
    sp = _normalized_path(source).rstrip("/")
    if not sp or sp in (".", ".."):
        return False
    return fp == sp or fp.endswith("/" + sp)


def _ambiguity_message(filepath: str, candidates: "list[str]") -> str:
    listed = "、".join(f"「{src}」" for src in sorted(candidates))
    return (
        f"[ERROR] 文件「{_basename_of(filepath)}」匹配到多个知识库来源，"
        f"无法唯一确定更新目标，未执行任何删除或写入：{listed}\n"
        "请先用 delete_documents 清理重名来源，或把文件放到可唯一判定路径后缀的位置。"
    )


def _match_existing_source(all_sources: set[str], filepath: str) -> "tuple[str, str]":
    """按完整 filepath 为更新操作确定唯一 source，返回 (source, error)。

    优先级：整值精确 → 整值 normcase → 整值大小写不敏感 → basename
    （原样/normcase/大小写不敏感，每级用路径后缀消歧）。同层出现多个候选
    且后缀无法判定唯一时返回错误信息，调用方不得删除或写入任何行。
    """
    candidates = sorted(all_sources)
    if not candidates:
        return "", ""

    exact = [src for src in candidates if src == filepath]
    if len(exact) == 1:
        return exact[0], ""

    target_norm = os.path.normcase(filepath)
    exact_norm = [src for src in candidates if os.path.normcase(src) == target_norm]
    if len(exact_norm) == 1:
        return exact_norm[0], ""
    if len(exact_norm) > 1:
        return "", _ambiguity_message(filepath, exact_norm)

    target_fold = target_norm.casefold()
    exact_fold = [
        src for src in candidates
        if os.path.normcase(src).casefold() == target_fold
    ]
    if len(exact_fold) == 1:
        return exact_fold[0], ""
    if len(exact_fold) > 1:
        return "", _ambiguity_message(filepath, exact_fold)

    fname = _basename_of(filepath)
    base_norm = os.path.normcase(fname)
    base_fold = base_norm.casefold()
    tiers = (
        [src for src in candidates if _basename_of(src) == fname],
        [src for src in candidates
         if os.path.normcase(_basename_of(src)) == base_norm],
        [src for src in candidates
         if os.path.normcase(_basename_of(src)).casefold() == base_fold],
    )
    for group in tiers:
        if not group:
            continue
        if len(group) == 1:
            return group[0], ""
        suffix_hits = [src for src in group if _path_is_suffix(filepath, src)]
        if len(suffix_hits) == 1:
            return suffix_hits[0], ""
        return "", _ambiguity_message(filepath, group)

    return "", ""


def _match_sources_for_delete(all_sources: set[str], pattern: str) -> set[str]:
    """删除用来源匹配：先 normcase 精确相等，未命中且含通配符时走 fnmatch。

    不使用子串匹配；pattern 无 * ? [ 且不精确相等时不删除任何行。
    fnmatch 保持原有平台语义（Windows 下不区分大小写）。
    """
    target = os.path.normcase(pattern)
    matched = {src for src in all_sources if os.path.normcase(src) == target}
    if not matched and any(ch in pattern for ch in "*?["):
        matched = {src for src in all_sources if fnmatch.fnmatch(src, pattern)}
    return matched


def _source_doc_ids(table, source: str) -> set[str]:
    """写入前记录某 source 的旧 doc_id 集合（空 doc_id 原样保留）。"""
    where = "source = '" + source.replace("'", "''") + "'"
    arrow = table.to_lance().scanner(
        columns=["doc_id"], filter=where).to_table()
    return {
        value if isinstance(value, str) else str(value or "")
        for value in arrow.column("doc_id").to_pylist()
    }


def _delete_source_rows(table, source: str, doc_ids: "list[str]") -> int:
    """按 source + doc_id 批量删除，返回 DeleteResult.num_deleted_rows 合计。"""
    safe_source = source.replace("'", "''")
    removed = 0
    for i in range(0, len(doc_ids), _DOC_ID_DELETE_BATCH):
        batch = doc_ids[i:i + _DOC_ID_DELETE_BATCH]
        where = (
            "source = '" + safe_source + "' AND doc_id IN ("
            + ",".join("'" + d.replace("'", "''") + "'" for d in batch)
            + ")"
        )
        removed += int(table.delete(where).num_deleted_rows)
    return removed


def _delete_stale_source_rows(table, source: str, old_doc_ids: set[str],
                              new_doc_ids: set[str]) -> int:
    """新行写入成功后清理旧版本：删除旧 doc_id 中不在新集合里的行。

    doc_id 仍在新集合中的旧行内容未变，保留不重复写；空 doc_id 的历史行
    无法判断内容，一并清理。
    """
    stale = sorted(doc_id for doc_id in old_doc_ids
                   if doc_id and doc_id not in new_doc_ids)
    removed = _delete_source_rows(table, source, stale) if stale else 0
    if "" in old_doc_ids:
        where = "source = '" + source.replace("'", "''") + "' AND doc_id = ''"
        removed += int(table.delete(where).num_deleted_rows)
    return removed


def _delete_source_exact(table, source: str) -> int:
    """按 source 精确删除并返回删除行数（单次 delete，不做 count 预扫）。"""
    where = "source = '" + source.replace("'", "''") + "'"
    return int(table.delete(where).num_deleted_rows)


def scan_files(scan_dir: str) -> List[str]:
    """按扩展名扫描目录（跳过排除目录与超限文件）。"""
    filepaths = []
    for root, dirs, files in os.walk(scan_dir):
        dirs[:] = [d for d in dirs if d not in cfg.EXCLUDE_DIRS]
        for f in files:
            if Path(f).suffix.lower() not in cfg.EXTENSIONS:
                continue
            fp = os.path.join(root, f)
            try:
                if os.path.getsize(fp) > cfg.MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            filepaths.append(fp)
    return sorted(set(filepaths))


def add_documents(scan_dir: str, reindex_all: bool = False,
                  progress=None, export_markdown_dir: str = "") -> str:
    """扫描目录并增量入库。progress: optional callable(done_files, total_files)。

    export_markdown_dir 非空时，PDF 的 MinerU 转换产物（md + 图片）同步导出到该目录。
    """
    if not os.path.isdir(scan_dir):
        return f"❌ 目录不存在: {scan_dir}"

    table = kb_schema.get_or_create_table()
    existing = kb_schema.existing_sources(table) if not reindex_all else set()

    if reindex_all:
        db = kb_schema.get_db()
        db.drop_table(cfg.TABLE_NAME)
        table = kb_schema.get_or_create_table()

    filepaths = scan_files(scan_dir)
    new_files = [
        fp for fp in filepaths
        if os.path.relpath(fp, scan_dir) not in existing
    ]
    if not new_files:
        msg = f"扫描「{scan_dir}」，共 {len(filepaths)} 个文件，没有新文件需要添加。"
        if existing:
            msg += f"（已有 {len(existing)} 个文件在库中）"
        return msg

    all_chunks: List[Dict] = []
    skipped: List[str] = []
    source_filepaths: Dict[str, str] = {}
    for done, fp in enumerate(new_files, 1):
        text, is_md = extract_content(fp, export_markdown_dir=export_markdown_dir)
        if not text or len(text) < 20:
            skipped.append(os.path.basename(fp))
            continue
        rel_path = os.path.relpath(fp, scan_dir)
        source_filepaths[rel_path] = fp
        chunks = chunk_text(text, rel_path, is_md)
        for ch in chunks:
            ch["doc_id"] = kb_schema.chunk_doc_id(ch["text"], ch["source"], ch["chunk_index"])
            ch["ingested_at"] = datetime_now_iso()
        all_chunks.extend(chunks)
        if progress:
            progress(done, len(new_files))

    if not all_chunks:
        return "扫描完成，但没有提取到有效文本内容。"

    added = add_chunks(table, all_chunks)
    kb_schema.ensure_vector_index(table)
    kb_schema.ensure_fts_index(table)
    kb_schema.fold_new_rows(table)
    kb_schema.update_db_readme()

    chunks_by_source: Dict[str, List[Dict]] = {}
    for chunk in all_chunks:
        chunks_by_source.setdefault(str(chunk.get("source", "")), []).append(chunk)
    for batch_source, batch_chunks in chunks_by_source.items():
        if not batch_source:
            continue
        _register_wiki_safely(
            batch_source,
            source_filepaths.get(batch_source, batch_source),
            batch_chunks,
            str(batch_chunks[0].get("category", "")) if batch_chunks else "",
        )

    extra = f"（跳过 {len(existing)} 个已有文件）" if existing and not reindex_all else ""
    if export_markdown_dir:
        extra += "\nMarkdown 导出目录: " + export_markdown_dir
    lines = [
        f"✅ **文档添加完成！**",
        f"{'─' * 40}",
        f"扫描目录: {scan_dir}",
        f"新扫描文件: {len(new_files)}",
        f"新提取文本块: {len(all_chunks)}",
        f"已向量化入库: {added}",
        f"知识库总块数: {len(table)}",
        extra,
    ]
    if skipped:
        lines.append(f"⚠️ 无法提取文本（已跳过）: {', '.join(skipped[:5])}"
                     + (f" 等共 {len(skipped)} 个" if len(skipped) > 5 else ""))
    return "\n".join(lines)


def add_single_document(filepath: str, scan_dir: str = "",
                        export_markdown_dir: str = "") -> str:
    """向量化单个文档；先成功写入新行，再清理该 source 的旧行。

    source 为 scan_dir 下的相对路径时按其幂等替换旧版本；未传 scan_dir
    （或文件不在其中）时 source 退化为 basename，若库中已有同名 source
    则拒绝覆盖，避免两个目录下的同名文件静默互相替换。

    export_markdown_dir 非空时，PDF 的 MinerU 转换产物（md + 图片）同步导出到该目录。
    """
    if not os.path.isfile(filepath):
        return f"[ERROR] 文件不存在: {filepath}"
    ext = Path(filepath).suffix.lower()
    if ext not in cfg.EXTENSIONS:
        return f"[ERROR] 不支持的文件格式: {ext}"
    if os.path.getsize(filepath) > cfg.MAX_FILE_SIZE:
        return f"[ERROR] 文件过大: {os.path.getsize(filepath) / 1024 / 1024:.1f}MB"

    source = os.path.basename(filepath)
    source_is_relative = False
    if scan_dir and os.path.isdir(scan_dir):
        rel = os.path.relpath(filepath, scan_dir)
        if not rel.startswith(".." + os.sep) and rel != "..":
            source = rel
            source_is_relative = True

    text, is_md = extract_content(filepath, export_markdown_dir=export_markdown_dir)
    if not text or len(text) < 20:
        return f"[ERROR] 无法从文件中提取有效文本: {os.path.basename(filepath)}"

    chunks = chunk_text(text, source, is_md)
    if not chunks:
        return f"[ERROR] 分块后无有效内容: {source}"
    for ch in chunks:
        ch["doc_id"] = kb_schema.chunk_doc_id(ch["text"], ch["source"], ch["chunk_index"])
        ch["ingested_at"] = datetime_now_iso()

    with _write_lock:
        table = kb_schema.get_or_create_table()
        if not source_is_relative:
            existing = kb_schema.existing_sources(table)
            if any(os.path.normcase(src) == os.path.normcase(source) for src in existing):
                return (
                    f"[ERROR] 来源「{source}」已存在于知识库，未执行覆盖。"
                    "请传入包含该文件的 scan_dir（以相对路径区分同名文件），"
                    "或使用 update_document 更新已有文档。"
                )

        old_doc_ids = _source_doc_ids(table, source)
        new_doc_ids = {ch["doc_id"] for ch in chunks}
        pending = [ch for ch in chunks if ch["doc_id"] not in old_doc_ids]
        try:
            added = add_chunks(table, pending)
        except Exception as e:
            return f"[ERROR] 写入失败，旧记录未删除: {e}"
        replaced = _delete_stale_source_rows(table, source, old_doc_ids, new_doc_ids)
        kb_schema.ensure_vector_index(table)
        kb_schema.ensure_fts_index(table)
        kb_schema.fold_new_rows(table)
        kb_schema.update_db_readme()

    _register_wiki_safely(
        source,
        filepath,
        chunks,
        str(chunks[0].get("category", "")) if chunks else "",
    )

    extra = f"\nMarkdown 导出: {export_markdown_dir}" if export_markdown_dir else ""
    return (
        f"✅ 文档已入库!\n"
        f"文件: {source}\n"
        f"新增记录: {added}\n"
        f"替换旧记录: {replaced}\n"
        f"知识库总块数: {len(table)}{extra}"
    )


def update_document(filepath: str) -> str:
    """更新文档：按完整路径唯一匹配已有 source，写入新行后再清理旧行。

    同名 basename 匹配到多个 source 且无法用路径后缀判定唯一时返回
    [ERROR] 并列出候选，不执行任何删除/写入。
    """
    if not os.path.isfile(filepath):
        return f"[ERROR] 文件不存在: {filepath}"
    ext = Path(filepath).suffix.lower()
    if ext not in cfg.EXTENSIONS:
        return f"[ERROR] 不支持的文件格式: {ext}"

    fname = os.path.basename(filepath)
    text, is_md = extract_content(filepath)
    if not text or len(text) < 20:
        return f"[ERROR] 无法从文件中提取有效文本: {fname}"

    with _write_lock:
        table = kb_schema.get_or_create_table()
        matched_source, error = _match_existing_source(
            kb_schema.existing_sources(table), filepath)
        if error:
            return error

        source = matched_source or fname
        chunks = chunk_text(text, source, is_md)
        if not chunks:
            return f"[ERROR] 分块后无有效内容: {source}"
        for ch in chunks:
            ch["doc_id"] = kb_schema.chunk_doc_id(
                ch["text"], ch["source"], ch["chunk_index"])
            ch["ingested_at"] = datetime_now_iso()

        old_doc_ids = _source_doc_ids(table, source)
        new_doc_ids = {ch["doc_id"] for ch in chunks}
        pending = [ch for ch in chunks if ch["doc_id"] not in old_doc_ids]
        try:
            added = add_chunks(table, pending)
        except Exception as e:
            return f"[ERROR] 写入失败，旧记录未删除: {e}"
        deleted_rows = _delete_stale_source_rows(
            table, source, old_doc_ids, new_doc_ids)
        kb_schema.ensure_vector_index(table)
        kb_schema.ensure_fts_index(table)
        kb_schema.fold_new_rows(table)
        kb_schema.update_db_readme()

    _register_wiki_safely(
        source,
        filepath,
        chunks,
        str(chunks[0].get("category", "")) if chunks else "",
    )

    return (
        f"✅ 文档已更新!\n"
        f"文件名: {fname}\n"
        f"删除旧记录: {deleted_rows}\n"
        f"新增记录: {added}\n"
        f"知识库总块数: {kb_schema.row_count()}"
    )


def delete_documents(source_pattern: str = "",
                     confirm_all: bool = False) -> str:
    """按来源精确匹配删除（支持 * ? [ 通配），或 confirm_all=True 清空整个知识库。

    匹配集合若覆盖全部 distinct source，必须显式 confirm_all=True，
    避免用 "*" 等通配绕过清空确认。
    """
    with _write_lock:
        if not kb_schema.table_exists():
            return "知识库为空，无需删除。"
        table = kb_schema.get_or_create_table()
        total_before = len(table)

        if confirm_all:
            db = kb_schema.get_db()
            db.drop_table(cfg.TABLE_NAME)
            kb_schema.update_db_readme()
            return f"🗑️ **知识库已清空**\n{'─' * 40}\n删除记录数: {total_before}"

        if source_pattern:
            all_sources = kb_schema.existing_sources(table)
            matched = _match_sources_for_delete(all_sources, source_pattern)
            if not matched:
                return f"未找到匹配「{source_pattern}」的文档。知识库中共有 {total_before} 条记录。"

            if matched == all_sources:
                return (
                    f"[ERROR] 模式「{source_pattern}」匹配全部 {len(all_sources)} 个来源，"
                    "已拒绝执行。\n"
                    "如确认清空整个知识库，请设置 confirm_all=True。"
                )

            deleted = 0
            for src in sorted(matched):
                deleted += _delete_source_exact(table, src)
            kb_schema.fold_new_rows(table)
            kb_schema.update_db_readme()
            remaining = kb_schema.row_count()
            return (
                f"🗑️ **文档删除完成**\n"
                f"{'─' * 40}\n"
                f"匹配模式: {source_pattern}\n"
                f"匹配到的来源: {len(matched)} 个\n"
                f"删除记录数: {deleted}\n"
                f"剩余记录数: {remaining}\n"
                f"匹配文件: {', '.join(sorted(matched)[:10])}"
                + (f"\n... 等共 {len(matched)} 个" if len(matched) > 10 else "")
            )

        return (
            f"⚠️ 请指定 source_pattern 进行选择性删除，或设置 confirm_all=True 清空。\n"
            f"知识库共 {total_before} 条记录。"
        )


def list_documents(category_filter: str = "", limit: int = 50) -> str:
    """按文档聚合列出知识库内容与统计。"""
    limit = min(max(limit, 1), 200)
    if not kb_schema.table_exists():
        return "❌ 知识库为空，没有文档。"

    table = kb_schema.get_or_create_table()
    total = len(table)
    arrow = table.to_lance().to_table(columns=["source", "category"])

    doc_groups: dict[str, dict] = {}
    cat_counts: dict[str, int] = {}
    for src, cat in zip(
        arrow.column("source").to_pylist(),
        arrow.column("category").to_pylist(),
    ):
        if doc_groups.get(src) is None:
            doc_groups[src] = {"chunk_count": 0, "category": cat}
        doc_groups[src]["chunk_count"] += 1
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

    rows = [
        {"source": src, **info}
        for src, info in doc_groups.items()
        if not category_filter or (info["category"] or "").lower() == category_filter.lower()
    ]
    rows.sort(key=lambda r: -r["chunk_count"])
    total_docs = len(rows)
    display = rows[:limit]

    cat_summary = " | ".join(
        f"{k}: {v}" for k, v in sorted(cat_counts.items(), key=lambda x: -x[1])
    )
    lines = [
        f"📄 **文档列表**（共 {total_docs} 个，显示前 {min(limit, total_docs)} 个）",
        f"{'─' * 40}",
        f"知识库总块数: {total}",
    ]
    if cat_summary:
        lines.append(f"类别分布: {cat_summary}")
    lines.append("")
    for row in display:
        cat = row["category"]
        cat_str = f" [{cat}]" if cat else ""
        lines.append(f"  {row['source']}{cat_str} — {row['chunk_count']} 块")
    if total_docs > limit:
        lines.append(f"  ... 等共 {total_docs} 个")
    return "\n".join(lines)


def fetch_document_text(table, matched_sources: set[str]) -> "list[dict]":
    """按来源集合拉取行（text/chunk_index），按 source 分组。"""
    from collections import OrderedDict

    src_list = sorted(matched_sources)
    arrow = table.to_lance().scanner(
        columns=["source", "text", "chunk_index"],
        filter="source IN (" + ",".join(
            "'" + s.replace("'", "''") + "'" for s in src_list
        ) + ")",
    ).to_table()
    matched = [
        {"source": src, "text": text or "", "chunk_index": int(idx)}
        for src, text, idx in zip(
            arrow.column("source").to_pylist(),
            arrow.column("text").to_pylist(),
            arrow.column("chunk_index").to_pylist(),
        )
    ]
    groups: "OrderedDict[str, list]" = OrderedDict()
    for m in matched:
        groups.setdefault(m["source"], []).append(m)
    for src in groups:
        groups[src].sort(key=lambda x: x["chunk_index"])
    return groups
