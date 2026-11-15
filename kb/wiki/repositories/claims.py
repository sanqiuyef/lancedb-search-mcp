# -*- coding: utf-8 -*-
"""claims / claim_evidence 的 SQLite 仓储层。

薄读写函数，不管理事务与连接。证据校验、位置回填等业务逻辑在
``kb.wiki.evidence`` 中；这里只负责落库与查询。

注意：当前 0003 迁移的 ``claim_evidence`` 表没有 role/verification 列，
role 仅作为调用方语义参数由上层返回，不落库（见 ``evidence.add_claim_evidence``）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..ids import new_ulid
from .documents import utc_now


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def get_claim(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    return _as_dict(row)


def insert_claim(
    conn: sqlite3.Connection,
    *,
    claim_text: str,
    claim_type: str = "fact",
    claim_id: str | None = None,
    document_id: str | None = None,
    page_id: str | None = None,
    subject_entity_id: str | None = None,
    predicate: str = "",
    object_entity_id: str | None = None,
    object_text: str = "",
    confidence: float = 0.0,
    status: str = "proposed",
    now: str | None = None,
) -> str:
    """插入一条 claim 并返回 claim_id；claim_text 必填。"""
    if not isinstance(claim_text, str) or not claim_text.strip():
        raise ValueError("claim_text 必须是非空字符串")
    if claim_type not in ("fact", "hypothesis"):
        raise ValueError(f"非法 claim_type: {claim_type!r}")
    if status not in ("proposed", "accepted", "rejected", "disputed"):
        raise ValueError(f"非法 claim status: {status!r}")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence 必须在 0..1 之间")

    claim_id = claim_id or new_ulid()
    timestamp = now or utc_now()
    conn.execute(
        "INSERT INTO claims "
        "(claim_id, document_id, page_id, subject_entity_id, predicate, "
        " object_entity_id, object_text, claim_text, claim_type, confidence, "
        " status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim_id,
            document_id,
            page_id,
            subject_entity_id,
            predicate,
            object_entity_id,
            object_text,
            claim_text,
            claim_type,
            float(confidence),
            status,
            timestamp,
            timestamp,
        ),
    )
    return claim_id


def set_claim_status(
    conn: sqlite3.Connection, claim_id: str, status: str, *, now: str | None = None
) -> bool:
    if status not in ("proposed", "accepted", "rejected", "disputed"):
        raise ValueError(f"非法 claim status: {status!r}")
    cursor = conn.execute(
        "UPDATE claims SET status = ?, updated_at = ? WHERE claim_id = ?",
        (status, now or utc_now(), claim_id),
    )
    return cursor.rowcount > 0


def get_evidence(
    conn: sqlite3.Connection, evidence_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM claim_evidence WHERE evidence_id = ?", (evidence_id,)
    ).fetchone()
    return _as_dict(row)


def find_evidence(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    chunk_id: str,
    text_sha256: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM claim_evidence "
        "WHERE claim_id = ? AND chunk_id = ? AND text_sha256 = ?",
        (claim_id, chunk_id, text_sha256),
    ).fetchone()
    return _as_dict(row)


def insert_evidence(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    chunk_id: str,
    quote: str,
    text_sha256: str,
    char_start: int = 0,
    char_end: int = 0,
    confidence: float = 1.0,
    evidence_id: str | None = None,
    now: str | None = None,
) -> bool:
    if not isinstance(quote, str) or not quote:
        raise ValueError("quote 必须是非空字符串")
    if not isinstance(text_sha256, str):
        raise ValueError("text_sha256 必须是字符串")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence 必须在 0..1 之间")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO claim_evidence "
        "(evidence_id, claim_id, chunk_id, quote, char_start, char_end, "
        " text_sha256, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id or new_ulid(),
            claim_id,
            chunk_id,
            quote,
            char_start,
            char_end,
            text_sha256,
            float(confidence),
            now or utc_now(),
        ),
    )
    return cursor.rowcount > 0


def update_evidence_positions(
    conn: sqlite3.Connection,
    evidence_id: str,
    *,
    char_start: int,
    char_end: int,
    text_sha256: str,
) -> bool:
    cursor = conn.execute(
        "UPDATE claim_evidence SET char_start = ?, char_end = ?, text_sha256 = ? "
        "WHERE evidence_id = ?",
        (char_start, char_end, text_sha256, evidence_id),
    )
    return cursor.rowcount > 0


def list_evidence(
    conn: sqlite3.Connection,
    *,
    claim_id: str | None = None,
    chunk_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if claim_id is not None:
        clauses.append("claim_id = ?")
        params.append(claim_id)
    if chunk_id is not None:
        clauses.append("chunk_id = ?")
        params.append(chunk_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM claim_evidence {where} ORDER BY created_at, evidence_id",
        params,
    ).fetchall()
    return [_as_dict(row) for row in rows if row is not None]


def first_quote_for_chunk(conn: sqlite3.Connection, chunk_id: str) -> str:
    row = conn.execute(
        "SELECT quote FROM claim_evidence WHERE chunk_id = ? "
        "ORDER BY created_at DESC, evidence_id DESC LIMIT 1",
        (chunk_id,),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def fact_claims_missing_evidence(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """事实型、未拒绝且没有任何证据的 claim（按创建时间排序）。"""
    rows = conn.execute(
        "SELECT c.* FROM claims AS c "
        "WHERE c.claim_type = 'fact' "
        "  AND c.status IN ('proposed', 'accepted', 'disputed') "
        "  AND NOT EXISTS ("
        "      SELECT 1 FROM claim_evidence AS e WHERE e.claim_id = c.claim_id"
        "  ) "
        "ORDER BY c.created_at, c.claim_id"
    ).fetchall()
    return [_as_dict(row) for row in rows if row is not None]


__all__ = [
    "fact_claims_missing_evidence",
    "find_evidence",
    "first_quote_for_chunk",
    "get_claim",
    "get_evidence",
    "insert_claim",
    "insert_evidence",
    "list_evidence",
    "set_claim_status",
    "update_evidence_positions",
]
