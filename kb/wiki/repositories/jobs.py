# -*- coding: utf-8 -*-
"""``wiki_jobs`` / ``write_journal`` 表读写与状态迁移。

``write_journal`` 用于记录跨文件/DB/git 的写入过程；PageStore 在每次写入时
创建 prepared 记录，并推进到 file_written / db_committed / completed；git 提交
失败时置 failed，但不会回滚已写入的文件与 DB 记录。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .. import constants as C
from ..models import WikiJobRecord, WriteJournalRecord

_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class JobTransitionError(ValueError):
    """非法的 job 状态迁移。"""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _json_text(value: Any, default: str = "{}") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            json.loads(value)
        except (TypeError, ValueError):
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(value, ensure_ascii=False)


def _load_json(text: str, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _job_from_row(row: sqlite3.Row) -> WikiJobRecord:
    return WikiJobRecord.model_validate(dict(row))


def _journal_from_row(row: sqlite3.Row) -> WriteJournalRecord:
    return WriteJournalRecord.model_validate(dict(row))


def create_job(
    conn: sqlite3.Connection,
    job_type: str,
    payload: Any = None,
    job_id: str | None = None,
) -> WikiJobRecord:
    if not isinstance(job_type, str) or not job_type.strip():
        raise ValueError("job_type 必须是非空字符串")
    now = utc_now()
    record = WikiJobRecord(
        job_id=job_id or WikiJobRecord.model_fields["job_id"].default_factory(),
        job_type=job_type.strip(),
        status="pending",
        payload_json=_json_text(payload),
        result_json="{}",
        error_message="",
        attempts=0,
        created_at=now,
        updated_at=now,
    )
    data = record.model_dump(mode="json")
    columns = list(WikiJobRecord.model_fields)
    conn.execute(
        f"INSERT INTO wiki_jobs ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(data[column] for column in columns),
    )
    return record


def get_job(conn: sqlite3.Connection, job_id: str) -> WikiJobRecord | None:
    row = conn.execute(
        "SELECT * FROM wiki_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return _job_from_row(row) if row is not None else None


def update_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    result: Any = None,
    error_message: str = "",
) -> WikiJobRecord:
    """按状态机更新 job；同状态重复调用幂等返回。"""
    if status not in C.JOB_STATUSES:
        raise ValueError(f"未知 job status: {status!r}")
    current = get_job(conn, job_id)
    if current is None:
        raise LookupError(f"job 不存在: {job_id}")
    if current.status == status:
        return current
    allowed = _JOB_TRANSITIONS.get(str(current.status), frozenset())
    if status not in allowed:
        raise JobTransitionError(
            f"job 状态不能从 {current.status} 迁移到 {status}"
        )

    now = utc_now()
    fields: dict[str, Any] = {"status": status, "updated_at": now}
    if status == "running" and current.started_at is None:
        fields["started_at"] = now
    if status in _TERMINAL_JOB_STATUSES:
        fields["finished_at"] = now
    if result is not None:
        fields["result_json"] = _json_text(result)
    if error_message:
        fields["error_message"] = error_message
    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(
        f"UPDATE wiki_jobs SET {assignments} WHERE job_id = ?",
        list(fields.values()) + [job_id],
    )
    updated = get_job(conn, job_id)
    assert updated is not None
    return updated


def claim_job(conn: sqlite3.Connection, job_id: str) -> WikiJobRecord:
    """把 pending job 置 running 并累加 attempts。"""
    current = get_job(conn, job_id)
    if current is None:
        raise LookupError(f"job 不存在: {job_id}")
    if current.status != "pending":
        raise JobTransitionError(
            f"job 当前状态为 {current.status}，不能 claim"
        )
    now = utc_now()
    conn.execute(
        "UPDATE wiki_jobs SET status = 'running', attempts = attempts + 1, "
        "started_at = COALESCE(started_at, ?), updated_at = ? "
        "WHERE job_id = ? AND status = 'pending'",
        (now, now, job_id),
    )
    updated = get_job(conn, job_id)
    assert updated is not None
    return updated


def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    job_type: str | None = None,
    limit: int | None = 50,
) -> list[WikiJobRecord]:
    if status is not None and status not in C.JOB_STATUSES:
        raise ValueError(f"未知 job status: {status!r}")
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if job_type is not None:
        where.append("job_type = ?")
        params.append(job_type)
    sql = "SELECT * FROM wiki_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC, job_id ASC"
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit 必须大于 0 或为 None")
        sql += " LIMIT ?"
        params.append(limit)
    return [_job_from_row(row) for row in conn.execute(sql, params)]


def create_journal(
    conn: sqlite3.Connection,
    *,
    job_id: str | None = None,
    proposal_id: str | None = None,
    page_id: str | None = None,
    target_path: str = "",
    revision_id: str = "",
    state: str = "prepared",
    detail: Any = None,
    error_message: str = "",
) -> WriteJournalRecord:
    if state not in C.JOURNAL_STATES:
        raise ValueError(f"未知 journal state: {state!r}")
    now = utc_now()
    record = WriteJournalRecord(
        job_id=job_id,
        proposal_id=proposal_id,
        page_id=page_id,
        target_path=target_path,
        revision_id=revision_id,
        state=state,
        detail_json=_json_text(detail),
        error_message=error_message,
        created_at=now,
        updated_at=now,
    )
    data = record.model_dump(mode="json")
    columns = list(WriteJournalRecord.model_fields)
    conn.execute(
        f"INSERT INTO write_journal ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(data[column] for column in columns),
    )
    return record


def get_journal(
    conn: sqlite3.Connection, journal_id: str
) -> WriteJournalRecord | None:
    row = conn.execute(
        "SELECT * FROM write_journal WHERE journal_id = ?", (journal_id,)
    ).fetchone()
    return _journal_from_row(row) if row is not None else None


def update_journal_state(
    conn: sqlite3.Connection,
    journal_id: str,
    state: str,
    *,
    error_message: str | None = None,
    detail: Mapping[str, Any] | None = None,
    page_id: str | None = None,
) -> WriteJournalRecord:
    if state not in C.JOURNAL_STATES:
        raise ValueError(f"未知 journal state: {state!r}")
    current = get_journal(conn, journal_id)
    if current is None:
        raise LookupError(f"journal 不存在: {journal_id}")
    merged_detail = _load_json(current.detail_json, {})
    if not isinstance(merged_detail, dict):
        merged_detail = {}
    if detail:
        merged_detail.update(detail)
    fields: dict[str, Any] = {
        "state": state,
        "detail_json": json.dumps(merged_detail, ensure_ascii=False, sort_keys=True),
        "updated_at": utc_now(),
    }
    if error_message is not None:
        fields["error_message"] = error_message
    if page_id is not None:
        fields["page_id"] = page_id
    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(
        f"UPDATE write_journal SET {assignments} WHERE journal_id = ?",
        list(fields.values()) + [journal_id],
    )
    updated = get_journal(conn, journal_id)
    assert updated is not None
    return updated


def list_journals(
    conn: sqlite3.Connection,
    page_id: str | None = None,
    state: str | None = None,
    limit: int | None = 50,
) -> list[WriteJournalRecord]:
    if state is not None and state not in C.JOURNAL_STATES:
        raise ValueError(f"未知 journal state: {state!r}")
    where: list[str] = []
    params: list[Any] = []
    if page_id is not None:
        where.append("page_id = ?")
        params.append(page_id)
    if state is not None:
        where.append("state = ?")
        params.append(state)
    sql = "SELECT * FROM write_journal"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC, journal_id ASC"
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit 必须大于 0 或为 None")
        sql += " LIMIT ?"
        params.append(limit)
    return [_journal_from_row(row) for row in conn.execute(sql, params)]


__all__ = [
    "JobTransitionError",
    "claim_job",
    "create_job",
    "create_journal",
    "get_job",
    "get_journal",
    "list_jobs",
    "list_journals",
    "update_job_status",
    "update_journal_state",
    "utc_now",
]
