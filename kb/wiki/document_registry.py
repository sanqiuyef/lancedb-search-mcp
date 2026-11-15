# -*- coding: utf-8 -*-
"""DocumentRegistry：文档身份、修订与旧切片引用的登记服务。

设计约束：
- 所有函数接收调用方的 ``sqlite3.Connection``，不自行 open/close；
- 事务由调用方控制，推荐写法::

      from kb.wiki.db import transaction

      with transaction(conn):
          document_id = register_source(conn, descriptor)
          revision = begin_revision(conn, document_id, revision_input)
          register_chunk_refs(conn, revision.revision_id, chunks)
          complete_revision(conn, revision.revision_id, len(chunks))

- ``register_source`` 按 ``(source_type, canonical_uri)`` get-or-create；
- ``begin_revision`` 以 processing_fingerprint 幂等：相同输入直接返回既有修订；
- 迁移用 ``register_legacy_source`` 绝不伪造 raw_sha256（固定空字符串）。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from .ids import chunk_id
from .ids import document_id as make_document_id
from .ids import new_ulid
from .repositories import documents as doc_repo
from .repositories.documents import ChunkDescriptor

LEGACY_PARSER_NAME = "legacy_import"

_UNC_PREFIX = "//"


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value


def canonical_local_source(source: str) -> str:
    """本地 source 的规范形式：去首尾空白、反斜杠转正斜杠。

    大小写保持不变——Windows 下是否同一路径由调用方用 ``os.path.normcase``
    比对，本函数不替调用方做平台判断。连续斜杠折叠为单个（保留 UNC 前缀）。
    """
    if not isinstance(source, str):
        raise TypeError("source 必须是字符串")
    value = source.strip().replace("\\", "/")
    if not value:
        return ""
    if value.startswith(_UNC_PREFIX):
        return _UNC_PREFIX + re.sub(r"/+", "/", value[len(_UNC_PREFIX):])
    return re.sub(r"/+", "/", value)


def canonical_web_source(url: str) -> str:
    """网页 source 的规范形式：复用 ``kb.web.canonicalize_url``。

    非法 URL 抛 ``ValueError``；懒导入避免本地文件场景依赖 requests。
    """
    if not isinstance(url, str):
        raise TypeError("url 必须是字符串")
    from ..web import canonicalize_url

    return canonicalize_url(url)


def infer_source_type(source: str) -> str:
    """按 source 形态推断来源类型：URL/``网页-`` 前缀为 web，其余为 file。"""
    value = (source or "").strip()
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")) or value.startswith("网页-"):
        return "web"
    return "file"


def processing_fingerprint(
    raw_sha256: str,
    parsed_sha256: str,
    parser_name: str,
    parser_version: str,
    parser_config_sha256: str,
) -> str:
    """处理指纹：sha256(raw_sha256 + parsed_sha256 + parser_name + parser_version + parser_config_sha256)。

    五个入参都必须是字符串；按契约直接顺序拼接后取 sha256。
    """
    parts = (raw_sha256, parsed_sha256, parser_name, parser_version, parser_config_sha256)
    for name, value in zip(
        ("raw_sha256", "parsed_sha256", "parser_name", "parser_version",
         "parser_config_sha256"),
        parts,
    ):
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是字符串")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """内容源身份与展示信息。"""

    source_type: str
    canonical_uri: str
    display_name: str = ""
    category: str = ""
    source_path: str = ""

    def __post_init__(self) -> None:
        _require_text("source_type", self.source_type)
        _require_text("canonical_uri", self.canonical_uri)
        for name in ("display_name", "category", "source_path"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")


@dataclass(frozen=True, slots=True)
class RevisionInput:
    """一次修订的处理输入；parsed_sha256 可在 begin 时为空、complete 时补齐。"""

    raw_sha256: str = ""
    parsed_sha256: str = ""
    parser_name: str = ""
    parser_version: str = ""
    parser_config_sha256: str = ""
    source_path: str = ""
    parsed_markdown_path: str = ""

    def __post_init__(self) -> None:
        for name in (
            "raw_sha256",
            "parsed_sha256",
            "parser_name",
            "parser_version",
            "parser_config_sha256",
            "source_path",
            "parsed_markdown_path",
        ):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")

    @property
    def fingerprint(self) -> str:
        return processing_fingerprint(
            self.raw_sha256,
            self.parsed_sha256,
            self.parser_name,
            self.parser_version,
            self.parser_config_sha256,
        )


class RevisionHandle(str):
    """begin_revision 的结果。

    同时是 ``revision_id`` 字符串（可直接传给 complete_revision /
    register_chunk_refs，也兼容 ``handle == revision_id`` 的比较），并带有
    ``created`` 等幂等诊断字段。
    """

    def __new__(
        cls,
        revision_id: str,
        document_id: str,
        revision_no: int,
        processing_fingerprint: str,
        status: str,
        created: bool,
    ) -> "RevisionHandle":
        handle = super().__new__(cls, revision_id)
        handle.document_id = document_id
        handle.revision_no = revision_no
        handle.processing_fingerprint = processing_fingerprint
        handle.status = status
        handle.created = created
        return handle

    @property
    def revision_id(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return (
            f"RevisionHandle({str(self)!r}, document_id={self.document_id!r}, "
            f"revision_no={self.revision_no}, status={self.status!r}, "
            f"created={self.created})"
        )


@dataclass(frozen=True, slots=True)
class LegacyRegistration:
    """register_legacy_source 的结果与幂等标记。"""

    document_id: str
    revision_id: str
    revision_no: int
    revision_status: str
    chunk_refs_inserted: int
    chunk_ref_count: int
    document_created: bool
    revision_created: bool


def register_source(conn: sqlite3.Connection, desc: SourceDescriptor) -> str:
    """按 ``(source_type, canonical_uri)`` get-or-create，返回 document_id。

    已存在时只更新非空 ``display_name`` / ``category`` 并刷新 updated_at，
    不覆盖 created_at，也不改 status（缺失标记需显式重新登记才能恢复）。
    """
    if not isinstance(desc, SourceDescriptor):
        raise TypeError("desc 必须是 SourceDescriptor")

    existing = doc_repo.find_document(conn, desc.source_type, desc.canonical_uri)
    if existing is not None:
        doc_repo.update_document_metadata(
            conn,
            existing["document_id"],
            display_name=desc.display_name,
            category=desc.category,
        )
        return str(existing["document_id"])

    document_id = make_document_id(desc.source_type, desc.canonical_uri)
    doc_repo.insert_document(
        conn,
        document_id=document_id,
        source_type=desc.source_type,
        canonical_uri=desc.canonical_uri,
        display_name=desc.display_name,
        category=desc.category,
    )
    # 并发下 INSERT OR IGNORE 可能未插入：以唯一键回读真实行。
    row = doc_repo.find_document(conn, desc.source_type, desc.canonical_uri)
    if row is None:
        raise RuntimeError(
            f"documents 登记失败: ({desc.source_type}, {desc.canonical_uri})"
        )
    return str(row["document_id"])


def begin_revision(
    conn: sqlite3.Connection, document_id: str, revision: RevisionInput
) -> RevisionHandle:
    """创建（或按 fingerprint 复用）一次修订，状态默认 processing。"""
    if not isinstance(revision, RevisionInput):
        raise TypeError("revision 必须是 RevisionInput")
    document = doc_repo.get_document(conn, document_id)
    if document is None:
        raise ValueError(f"document 不存在: {document_id}")

    fingerprint = revision.fingerprint
    existing = doc_repo.find_revision_by_fingerprint(conn, document_id, fingerprint)
    if existing is not None:
        return RevisionHandle(
            revision_id=str(existing["revision_id"]),
            document_id=document_id,
            revision_no=int(existing["revision_no"]),
            processing_fingerprint=fingerprint,
            status=str(existing["status"]),
            created=False,
        )

    revision_no = doc_repo.next_revision_no(conn, document_id)
    revision_id = new_ulid()
    try:
        doc_repo.insert_revision(
            conn,
            revision_id=revision_id,
            document_id=document_id,
            revision_no=revision_no,
            raw_sha256=revision.raw_sha256,
            parsed_sha256=revision.parsed_sha256,
            processing_fingerprint=fingerprint,
            parser_name=revision.parser_name,
            parser_version=revision.parser_version,
            parser_config_sha256=revision.parser_config_sha256,
            source_path=revision.source_path,
            parsed_markdown_path=revision.parsed_markdown_path,
            status="processing",
        )
    except sqlite3.IntegrityError:
        existing = doc_repo.find_revision_by_fingerprint(
            conn, document_id, fingerprint
        )
        if existing is None:
            raise
        return RevisionHandle(
            revision_id=str(existing["revision_id"]),
            document_id=document_id,
            revision_no=int(existing["revision_no"]),
            processing_fingerprint=fingerprint,
            status=str(existing["status"]),
            created=False,
        )

    return RevisionHandle(
        revision_id=revision_id,
        document_id=document_id,
        revision_no=revision_no,
        processing_fingerprint=fingerprint,
        status="processing",
        created=True,
    )


def complete_revision(
    conn: sqlite3.Connection,
    revision_id: str,
    chunk_count: int,
    parsed_sha256: str = "",
    parsed_markdown_path: str = "",
) -> None:
    """标记修订 indexed，写入切片数，并把文档 current_revision_id 指向它。"""
    if isinstance(chunk_count, bool) or not isinstance(chunk_count, int):
        raise ValueError("chunk_count 必须是整数")
    if chunk_count < 0:
        raise ValueError("chunk_count 不能为负数")
    if not isinstance(parsed_sha256, str) or not isinstance(parsed_markdown_path, str):
        raise ValueError("parsed_sha256 / parsed_markdown_path 必须是字符串")

    revision = doc_repo.get_revision(conn, revision_id)
    if revision is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    doc_repo.complete_revision_row(
        conn,
        revision_id,
        chunk_count=chunk_count,
        parsed_sha256=parsed_sha256,
        parsed_markdown_path=parsed_markdown_path,
    )
    doc_repo.set_current_revision(conn, str(revision["document_id"]), revision_id)


def fail_revision(conn: sqlite3.Connection, revision_id: str, error: str) -> None:
    """标记修订 failed；不清空历史切片引用。"""
    if not isinstance(error, str):
        raise TypeError("error 必须是字符串")
    if doc_repo.get_revision(conn, revision_id) is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    doc_repo.fail_revision_row(conn, revision_id, error)


def register_chunk_refs(
    conn: sqlite3.Connection,
    revision_id: str,
    chunks: Sequence[ChunkDescriptor],
) -> int:
    """批量登记切片引用，返回新插入行数。

    依赖 UNIQUE(document_revision_id, chunk_index, text_sha256) 做 INSERT OR
    IGNORE：同一 index 不同文本会各占一行，重复调用不新增。legacy_doc_id /
    source_value 原样保留，供证据回查 LanceDB 原文。
    """
    revision = doc_repo.get_revision(conn, revision_id)
    if revision is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    document_id = str(revision["document_id"])

    inserted = 0
    for chunk in chunks:
        if not isinstance(chunk, ChunkDescriptor):
            raise TypeError("chunks 元素必须是 ChunkDescriptor")
        if doc_repo.insert_chunk_ref(
            conn,
            chunk_id=_chunk_id(revision_id, chunk),
            document_id=document_id,
            document_revision_id=revision_id,
            chunk_index=chunk.chunk_index,
            text_sha256=chunk.text_sha256,
            legacy_doc_id=chunk.legacy_doc_id,
            source_value=chunk.source_value,
            section_path=chunk.section_path,
            page_number=chunk.page_number,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            locator_json=chunk.locator_json,
        ):
            inserted += 1
    return inserted


def mark_source_missing(
    conn: sqlite3.Connection, document_id: str
) -> bool:
    """把文档标记为 missing；不删除任何 revision / chunk_ref 历史。"""
    if doc_repo.get_document(conn, document_id) is None:
        raise ValueError(f"document 不存在: {document_id}")
    return doc_repo.set_document_status(conn, document_id, "missing")


def register_legacy_source(
    conn: sqlite3.Connection,
    source: str,
    rows: Sequence[ChunkDescriptor],
    *,
    category: str = "",
    source_type: str = "",
) -> LegacyRegistration:
    """迁移旧 LanceDB 行：建 document + legacy_unverified 修订 + 切片引用。

    - raw_sha256 / parsed_sha256 固定为空字符串，绝不伪造原始文件指纹；
    - parser_name 固定 ``legacy_import``，因此同一 source 重复调用会命中
      processing_fingerprint 幂等分支；
    - 只有文档尚无 current_revision_id 时才把指针指向 legacy 修订，
      避免覆盖后续真实入库产生的新修订。
    """
    _require_text("source", source)
    if not isinstance(category, str):
        raise ValueError("category 必须是字符串")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("rows 必须是 ChunkDescriptor 序列")

    effective_type = source_type or infer_source_type(source)
    if effective_type == "web":
        try:
            canonical_uri = canonical_web_source(source)
        except ValueError:
            canonical_uri = source.strip()
    else:
        canonical_uri = canonical_local_source(source) or source.strip()

    desc = SourceDescriptor(
        source_type=effective_type,
        canonical_uri=canonical_uri,
        display_name=source.strip(),
        category=category,
        source_path=source,
    )
    existing_document = doc_repo.find_document(
        conn, desc.source_type, desc.canonical_uri
    )
    document_created = existing_document is None
    document_id = register_source(conn, desc)

    revision = begin_revision(
        conn,
        document_id,
        RevisionInput(
            raw_sha256="",
            parsed_sha256="",
            parser_name=LEGACY_PARSER_NAME,
            parser_version="",
            parser_config_sha256="",
            source_path=source,
        ),
    )
    revision_created = revision.created
    if revision_created or revision.status != "legacy_unverified":
        doc_repo.set_revision_status(
            conn, revision.revision_id, "legacy_unverified"
        )
        revision_status = "legacy_unverified"
    else:
        revision_status = revision.status

    inserted = register_chunk_refs(conn, revision.revision_id, rows)
    chunk_ref_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM chunk_refs WHERE document_revision_id = ?",
            (revision.revision_id,),
        ).fetchone()[0]
    )
    if chunk_ref_count:
        conn.execute(
            "UPDATE document_revisions SET chunk_count = ? WHERE revision_id = ?",
            (chunk_ref_count, revision.revision_id),
        )

    document = doc_repo.get_document(conn, document_id)
    if document is not None and not document.get("current_revision_id"):
        doc_repo.set_current_revision(
            conn, document_id, revision.revision_id
        )

    return LegacyRegistration(
        document_id=document_id,
        revision_id=revision.revision_id,
        revision_no=revision.revision_no,
        revision_status=revision_status,
        chunk_refs_inserted=inserted,
        chunk_ref_count=chunk_ref_count,
        document_created=document_created,
        revision_created=revision_created,
    )


def _chunk_id(revision_id: str, chunk: ChunkDescriptor) -> str:
    return chunk_id(revision_id, chunk.chunk_index, chunk.text_sha256)


__all__ = [
    "ChunkDescriptor",
    "LEGACY_PARSER_NAME",
    "LegacyRegistration",
    "RevisionHandle",
    "RevisionInput",
    "SourceDescriptor",
    "begin_revision",
    "canonical_local_source",
    "canonical_web_source",
    "complete_revision",
    "fail_revision",
    "infer_source_type",
    "mark_source_missing",
    "processing_fingerprint",
    "register_chunk_refs",
    "register_legacy_source",
    "register_source",
]
