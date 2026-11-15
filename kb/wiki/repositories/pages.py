# -*- coding: utf-8 -*-
"""``pages`` / ``page_revisions`` 表的 SQL 读写。

本模块只负责数据库映射，不做文件 IO、不开启事务（由调用方控制事务边界）。
所有时间列为 UTC ISO8601 文本。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import constants as C
from ..models import PageRecord, PageRevisionRecord

_UPDATE_PAGE_FIELDS = {"slug", "title", "page_type", "status", "current_revision_id",
                       "relative_path", "updated_at"}


def utc_now() -> str:
    """当前 UTC 时间，毫秒精度，``...Z`` 结尾。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _page_from_row(row: sqlite3.Row) -> PageRecord:
    return PageRecord.model_validate(dict(row))


def _revision_from_row(row: sqlite3.Row) -> PageRevisionRecord:
    return PageRevisionRecord.model_validate(dict(row))


def insert_page(conn: sqlite3.Connection, record: PageRecord) -> PageRecord:
    """插入页面元数据，返回同一记录。"""
    data = record.model_dump(mode="json")
    columns = list(PageRecord.model_fields)
    conn.execute(
        f"INSERT INTO pages ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(data[column] for column in columns),
    )
    return record


def get_page(conn: sqlite3.Connection, page_id: str) -> PageRecord | None:
    row = conn.execute(
        "SELECT * FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    return _page_from_row(row) if row is not None else None


def get_page_by_slug(conn: sqlite3.Connection, slug: str) -> PageRecord | None:
    row = conn.execute(
        "SELECT * FROM pages WHERE slug = ? COLLATE NOCASE LIMIT 1", (slug,)
    ).fetchone()
    return _page_from_row(row) if row is not None else None


def get_page_by_relative_path(
    conn: sqlite3.Connection, relative_path: str
) -> PageRecord | None:
    row = conn.execute(
        "SELECT * FROM pages WHERE relative_path = ? LIMIT 1", (relative_path,)
    ).fetchone()
    return _page_from_row(row) if row is not None else None


def slug_exists(conn: sqlite3.Connection, slug: str) -> bool:
    """slug 在 SQLite 中大小写不敏感唯一。"""
    row = conn.execute(
        "SELECT 1 FROM pages WHERE slug = ? COLLATE NOCASE LIMIT 1", (slug,)
    ).fetchone()
    return row is not None


def list_pages(
    conn: sqlite3.Connection,
    status: str | None = None,
    page_type: str | None = None,
    limit: int | None = 50,
    order: str = "updated",
) -> list[PageRecord]:
    """列出页面；``limit=None`` 表示不限制。"""
    if status is not None and status not in C.PAGE_STATUSES:
        raise ValueError(f"未知 status: {status!r}")
    if page_type is not None and page_type not in C.PAGE_TYPES:
        raise ValueError(f"未知 page_type: {page_type!r}")
    if order not in {"updated", "created"}:
        raise ValueError("order 必须是 'updated' 或 'created'")
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit 必须大于 0 或为 None")

    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if page_type is not None:
        where.append("page_type = ?")
        params.append(page_type)
    sql = "SELECT * FROM pages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if order == "updated":
        sql += " ORDER BY updated_at DESC, page_id ASC"
    else:
        sql += " ORDER BY created_at ASC, page_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_page_from_row(row) for row in conn.execute(sql, params)]


def update_page(conn: sqlite3.Connection, page_id: str, **fields: Any) -> int:
    """按白名单更新页面字段，返回受影响行数。"""
    unknown = set(fields) - _UPDATE_PAGE_FIELDS
    if unknown:
        raise ValueError(f"不允许更新的 pages 字段: {', '.join(sorted(unknown))}")
    if not fields:
        return 0
    if "status" in fields and fields["status"] not in C.PAGE_STATUSES:
        raise ValueError(f"未知 status: {fields['status']!r}")
    if "page_type" in fields and fields["page_type"] not in C.PAGE_TYPES:
        raise ValueError(f"未知 page_type: {fields['page_type']!r}")
    fields.setdefault("updated_at", utc_now())

    assignments = ", ".join(f"{column} = ?" for column in fields)
    params = list(fields.values()) + [page_id]
    cursor = conn.execute(
        f"UPDATE pages SET {assignments} WHERE page_id = ?", params
    )
    return cursor.rowcount


def set_current_revision(
    conn: sqlite3.Connection,
    page_id: str,
    revision_id: str | None,
    status: str | None = None,
    updated_at: str | None = None,
) -> int:
    """更新 current_revision_id（可选同时更新 status/updated_at）。"""
    fields: dict[str, Any] = {"current_revision_id": revision_id}
    if status is not None:
        fields["status"] = status
    if updated_at is not None:
        fields["updated_at"] = updated_at
    return update_page(conn, page_id, **fields)


def insert_page_revision(
    conn: sqlite3.Connection, record: PageRevisionRecord
) -> PageRevisionRecord:
    data = record.model_dump(mode="json")
    columns = list(PageRevisionRecord.model_fields)
    conn.execute(
        f"INSERT INTO page_revisions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(data[column] for column in columns),
    )
    return record


def get_page_revision(
    conn: sqlite3.Connection, revision_id: str
) -> PageRevisionRecord | None:
    row = conn.execute(
        "SELECT * FROM page_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    return _revision_from_row(row) if row is not None else None


def get_current_revision(
    conn: sqlite3.Connection, page_id: str
) -> PageRevisionRecord | None:
    row = conn.execute(
        "SELECT r.* FROM page_revisions AS r "
        "JOIN pages AS p ON p.current_revision_id = r.revision_id "
        "WHERE p.page_id = ? LIMIT 1",
        (page_id,),
    ).fetchone()
    return _revision_from_row(row) if row is not None else None


def next_revision_no(conn: sqlite3.Connection, page_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM page_revisions "
        "WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    return int(row[0])


def list_page_revisions(
    conn: sqlite3.Connection, page_id: str
) -> list[PageRevisionRecord]:
    return [
        _revision_from_row(row)
        for row in conn.execute(
            "SELECT * FROM page_revisions WHERE page_id = ? "
            "ORDER BY revision_no ASC",
            (page_id,),
        )
    ]


def set_revision_git_commit(
    conn: sqlite3.Connection, revision_id: str, git_commit: str
) -> int:
    cursor = conn.execute(
        "UPDATE page_revisions SET git_commit = ? WHERE revision_id = ?",
        (git_commit, revision_id),
    )
    return cursor.rowcount


__all__ = [
    "get_current_revision",
    "get_page",
    "get_page_by_relative_path",
    "get_page_by_slug",
    "get_page_revision",
    "insert_page",
    "insert_page_revision",
    "list_page_revisions",
    "list_pages",
    "next_revision_no",
    "set_current_revision",
    "set_revision_git_commit",
    "slug_exists",
    "update_page",
    "utc_now",
]
