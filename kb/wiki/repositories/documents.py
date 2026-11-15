# -*- coding: utf-8 -*-
"""documents / document_revisions / chunk_refs 的 SQLite 仓储层。

这里只放“按主键/唯一键读写单行”的薄函数，不做业务判断：
- 事务由调用方通过 ``kb.wiki.db.transaction(conn)`` 控制；
- 不自行 open/close 连接；
- 传入的 ``sqlite3.Connection`` 必须已执行过 wiki 迁移。

``ChunkDescriptor`` 是登记切片引用时的输入载体；``from_text`` 统一用
``normalize_text_sha256`` 计算文本指纹，保证入库钩子与迁移脚本一致。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..ids import normalize_text_sha256


def utc_now() -> str:
    """UTC ISO8601（毫秒 + Z），与迁移 SQL 的默认时间格式一致。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _require_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class ChunkDescriptor:
    """一条 chunk_ref 的登记输入（不含正文，只保留可定位与指纹字段）。"""

    chunk_index: int
    text_sha256: str
    legacy_doc_id: str = ""
    source_value: str = ""
    section_path: str = ""
    page_number: int | None = None
    char_start: int = 0
    char_end: int = 0
    locator_json: str = "{}"

    def __post_init__(self) -> None:
        _require_int("chunk_index", self.chunk_index)
        if not isinstance(self.text_sha256, str) or not self.text_sha256:
            raise ValueError("text_sha256 必须是非空字符串")
        for name in ("legacy_doc_id", "source_value", "section_path"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")
        if self.page_number is not None:
            _require_int("page_number", self.page_number, minimum=1)
        _require_int("char_start", self.char_start)
        _require_int("char_end", self.char_end)
        if self.char_end < self.char_start:
            raise ValueError("char_end 不能小于 char_start")
        if not isinstance(self.locator_json, str):
            raise ValueError("locator_json 必须是 JSON 字符串")

    @classmethod
    def from_text(
        cls,
        chunk_index: int,
        text: str,
        *,
        legacy_doc_id: str = "",
        source_value: str = "",
        section_path: str = "",
        page_number: int | None = None,
        char_start: int = 0,
        char_end: int = 0,
        locator_json: str = "{}",
    ) -> "ChunkDescriptor":
        """按 ``normalize_text_sha256(text)`` 构造；text 必须为字符串。"""
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        return cls(
            chunk_index=chunk_index,
            text_sha256=normalize_text_sha256(text),
            legacy_doc_id=legacy_doc_id,
            source_value=source_value,
            section_path=section_path,
            page_number=page_number,
            char_start=char_start,
            char_end=char_end,
            locator_json=locator_json,
        )


def normalize_locator(locator: Any) -> str:
    """dict/list 转紧凑 JSON；字符串原样返回；None 返回 ``{}``。"""
    if locator is None:
        return "{}"
    if isinstance(locator, str):
        return locator
    return json.dumps(locator, ensure_ascii=False, sort_keys=True, default=str)


# =============================================================
# documents
# =============================================================

def find_document(
    conn: sqlite3.Connection, source_type: str, canonical_uri: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE source_type = ? AND canonical_uri = ?",
        (source_type, canonical_uri),
    ).fetchone()
    return _as_dict(row)


def get_document(
    conn: sqlite3.Connection, document_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    return _as_dict(row)


def insert_document(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    source_type: str,
    canonical_uri: str,
    display_name: str = "",
    category: str = "",
    status: str = "active",
    now: str | None = None,
) -> bool:
    """INSERT OR IGNORE 一行 document；返回是否真的插入。"""
    timestamp = now or utc_now()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO documents "
        "(document_id, source_type, canonical_uri, display_name, category, "
        " status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_id,
            source_type,
            canonical_uri,
            display_name,
            category,
            status,
            timestamp,
            timestamp,
        ),
    )
    return cursor.rowcount > 0


def update_document_metadata(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    display_name: str | None = None,
    category: str | None = None,
    now: str | None = None,
) -> bool:
    """仅更新传入的非空字段；不触碰 created_at。"""
    assignments: list[str] = []
    params: list[Any] = []
    if display_name:
        assignments.append("display_name = ?")
        params.append(display_name)
    if category:
        assignments.append("category = ?")
        params.append(category)
    if not assignments:
        return False
    assignments.append("updated_at = ?")
    params.append(now or utc_now())
    params.append(document_id)
    cursor = conn.execute(
        f"UPDATE documents SET {', '.join(assignments)} WHERE document_id = ?",
        params,
    )
    return cursor.rowcount > 0


def set_document_status(
    conn: sqlite3.Connection,
    document_id: str,
    status: str,
    *,
    now: str | None = None,
) -> bool:
    cursor = conn.execute(
        "UPDATE documents SET status = ?, updated_at = ? WHERE document_id = ?",
        (status, now or utc_now(), document_id),
    )
    return cursor.rowcount > 0


def set_current_revision(
    conn: sqlite3.Connection,
    document_id: str,
    revision_id: str,
    *,
    now: str | None = None,
) -> bool:
    cursor = conn.execute(
        "UPDATE documents SET current_revision_id = ?, updated_at = ? "
        "WHERE document_id = ?",
        (revision_id, now or utc_now(), document_id),
    )
    return cursor.rowcount > 0


# =============================================================
# document_revisions
# =============================================================

def get_revision(
    conn: sqlite3.Connection, revision_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM document_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    return _as_dict(row)


def find_revision_by_fingerprint(
    conn: sqlite3.Connection, document_id: str, processing_fingerprint: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM document_revisions "
        "WHERE document_id = ? AND processing_fingerprint = ?",
        (document_id, processing_fingerprint),
    ).fetchone()
    return _as_dict(row)


def next_revision_no(conn: sqlite3.Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM document_revisions "
        "WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return int(row[0])


def insert_revision(
    conn: sqlite3.Connection,
    *,
    revision_id: str,
    document_id: str,
    revision_no: int,
    raw_sha256: str,
    parsed_sha256: str,
    processing_fingerprint: str,
    parser_name: str = "",
    parser_version: str = "",
    parser_config_sha256: str = "",
    source_path: str = "",
    parsed_markdown_path: str = "",
    chunk_count: int = 0,
    status: str = "processing",
    error_message: str = "",
    now: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO document_revisions "
        "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
        " processing_fingerprint, parser_name, parser_version, "
        " parser_config_sha256, source_path, parsed_markdown_path, chunk_count, "
        " status, error_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            revision_id,
            document_id,
            revision_no,
            raw_sha256,
            parsed_sha256,
            processing_fingerprint,
            parser_name,
            parser_version,
            parser_config_sha256,
            source_path,
            parsed_markdown_path,
            chunk_count,
            status,
            error_message,
            now or utc_now(),
        ),
    )


def complete_revision_row(
    conn: sqlite3.Connection,
    revision_id: str,
    *,
    chunk_count: int,
    parsed_sha256: str = "",
    parsed_markdown_path: str = "",
) -> bool:
    cursor = conn.execute(
        "UPDATE document_revisions SET "
        "status = 'indexed', chunk_count = ?, error_message = '', "
        "parsed_sha256 = CASE WHEN ? <> '' THEN ? ELSE parsed_sha256 END, "
        "parsed_markdown_path = CASE WHEN ? <> '' THEN ? ELSE parsed_markdown_path END "
        "WHERE revision_id = ?",
        (
            chunk_count,
            parsed_sha256,
            parsed_sha256,
            parsed_markdown_path,
            parsed_markdown_path,
            revision_id,
        ),
    )
    return cursor.rowcount > 0


def fail_revision_row(
    conn: sqlite3.Connection, revision_id: str, error_message: str
) -> bool:
    cursor = conn.execute(
        "UPDATE document_revisions SET status = 'failed', error_message = ? "
        "WHERE revision_id = ?",
        (error_message, revision_id),
    )
    return cursor.rowcount > 0


def set_revision_status(
    conn: sqlite3.Connection, revision_id: str, status: str
) -> bool:
    cursor = conn.execute(
        "UPDATE document_revisions SET status = ? WHERE revision_id = ?",
        (status, revision_id),
    )
    return cursor.rowcount > 0


# =============================================================
# chunk_refs
# =============================================================

def get_chunk_ref(
    conn: sqlite3.Connection, chunk_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM chunk_refs WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    return _as_dict(row)


def insert_chunk_ref(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str,
    document_revision_id: str,
    chunk_index: int,
    text_sha256: str,
    legacy_doc_id: str = "",
    source_value: str = "",
    section_path: str = "",
    page_number: int | None = None,
    char_start: int = 0,
    char_end: int = 0,
    locator_json: str = "{}",
    now: str | None = None,
) -> bool:
    """INSERT OR IGNORE；返回是否插入了新行（唯一三元组冲突时返回 False）。"""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO chunk_refs "
        "(chunk_id, document_id, document_revision_id, legacy_doc_id, "
        " chunk_index, source_value, section_path, page_number, text_sha256, "
        " char_start, char_end, locator_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            document_id,
            document_revision_id,
            legacy_doc_id,
            chunk_index,
            source_value,
            section_path,
            page_number,
            text_sha256,
            char_start,
            char_end,
            locator_json,
            now or utc_now(),
        ),
    )
    return cursor.rowcount > 0


def update_chunk_ref_location(
    conn: sqlite3.Connection,
    chunk_id: str,
    *,
    page_number: int | None = None,
    section_path: str = "",
    locator_json: str = "",
) -> bool:
    """证据附带的位置信息补齐到 chunk_refs（仅覆盖传入的非空项）。"""
    assignments: list[str] = []
    params: list[Any] = []
    if page_number is not None:
        assignments.append("page_number = ?")
        params.append(page_number)
    if section_path:
        assignments.append("section_path = ?")
        params.append(section_path)
    if locator_json and locator_json != "{}":
        assignments.append("locator_json = ?")
        params.append(locator_json)
    if not assignments:
        return False
    params.append(chunk_id)
    cursor = conn.execute(
        f"UPDATE chunk_refs SET {', '.join(assignments)} WHERE chunk_id = ?",
        params,
    )
    return cursor.rowcount > 0


__all__ = [
    "ChunkDescriptor",
    "complete_revision_row",
    "fail_revision_row",
    "find_document",
    "find_revision_by_fingerprint",
    "get_chunk_ref",
    "get_document",
    "get_revision",
    "insert_chunk_ref",
    "insert_document",
    "insert_revision",
    "next_revision_no",
    "normalize_locator",
    "set_current_revision",
    "set_document_status",
    "set_revision_status",
    "update_chunk_ref_location",
    "update_document_metadata",
    "utc_now",
]
