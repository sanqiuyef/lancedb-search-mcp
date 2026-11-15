# -*- coding: utf-8 -*-
"""``proposals`` 表的 SQL 读写。

status CHECK 为 pending/rejected/stale/approved/applied/cancelled，
与 ``constants.PROPOSAL_STATUSES`` 及服务层状态机保持一致。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import constants as C
from ..models import ProposalRecord

_UPDATE_PROPOSAL_FIELDS = {
    "proposal_type",
    "status",
    "page_id",
    "expected_revision_id",
    "result_revision_id",
    "payload_json",
    "rationale",
    "created_by",
    "created_at",
    "updated_at",
    "reviewed_by",
    "reviewed_at",
    "applied_at",
    "error_message",
}


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _proposal_from_row(row: sqlite3.Row) -> ProposalRecord:
    return ProposalRecord.model_validate(dict(row))


def insert_proposal(
    conn: sqlite3.Connection, record: ProposalRecord
) -> ProposalRecord:
    data = record.model_dump(mode="json")
    columns = list(ProposalRecord.model_fields)
    conn.execute(
        f"INSERT INTO proposals ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(data[column] for column in columns),
    )
    return record


def get_proposal(
    conn: sqlite3.Connection, proposal_id: str
) -> ProposalRecord | None:
    row = conn.execute(
        "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    return _proposal_from_row(row) if row is not None else None


def list_proposals(
    conn: sqlite3.Connection,
    status: str | None = None,
    limit: int | None = 50,
) -> list[ProposalRecord]:
    if status is not None and status not in C.PROPOSAL_STATUSES:
        raise ValueError(f"未知 proposal status: {status!r}")
    sql = "SELECT * FROM proposals"
    params: list[Any] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at ASC, proposal_id ASC"
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit 必须大于 0 或为 None")
        sql += " LIMIT ?"
        params.append(limit)
    return [_proposal_from_row(row) for row in conn.execute(sql, params)]


def update_proposal(
    conn: sqlite3.Connection, proposal_id: str, **fields: Any
) -> int:
    unknown = set(fields) - _UPDATE_PROPOSAL_FIELDS
    if unknown:
        raise ValueError(
            f"不允许更新的 proposals 字段: {', '.join(sorted(unknown))}"
        )
    if not fields:
        return 0
    if "status" in fields and fields["status"] not in C.PROPOSAL_STATUSES:
        raise ValueError(
            f"未知 proposal status: {fields['status']!r}；"
            f"当前 schema 允许: {', '.join(C.PROPOSAL_STATUSES)}"
        )
    fields.setdefault("updated_at", utc_now())
    assignments = ", ".join(f"{column} = ?" for column in fields)
    params = list(fields.values()) + [proposal_id]
    return conn.execute(
        f"UPDATE proposals SET {assignments} WHERE proposal_id = ?", params
    ).rowcount


__all__ = [
    "get_proposal",
    "insert_proposal",
    "list_proposals",
    "update_proposal",
    "utc_now",
]
