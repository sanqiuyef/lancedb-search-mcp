# -*- coding: utf-8 -*-
"""``entities`` / ``entity_aliases`` / ``page_entities`` 表的 SQL 读写。

``entities.status`` 只允许 active/merged/deprecated；显式合并由
``entity_resolver.merge_entity`` 把 source 置为 ``merged`` 并写入
``merged_into_entity_id``，target 接收 source 的名称与别名。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import constants as C
from ..models import EntityAliasRecord, EntityRecord, PageEntityRecord

_UPDATE_ENTITY_FIELDS = {
    "canonical_name",
    "normalized_name",
    "entity_type",
    "description",
    "confidence",
    "status",
    "updated_at",
    "merged_into_entity_id",
}


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _entity_from_row(row: sqlite3.Row) -> EntityRecord:
    return EntityRecord.model_validate(dict(row))


def _alias_from_row(row: sqlite3.Row) -> EntityAliasRecord:
    return EntityAliasRecord.model_validate(dict(row))


def _page_entity_from_row(row: sqlite3.Row) -> PageEntityRecord:
    return PageEntityRecord.model_validate(dict(row))


def insert_entity(conn: sqlite3.Connection, record: EntityRecord) -> EntityRecord:
    data = record.model_dump(mode="json")
    columns = list(EntityRecord.model_fields)
    conn.execute(
        f"INSERT INTO entities ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(data[column] for column in columns),
    )
    return record


def get_entity(conn: sqlite3.Connection, entity_id: str) -> EntityRecord | None:
    row = conn.execute(
        "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return _entity_from_row(row) if row is not None else None


def update_entity(conn: sqlite3.Connection, entity_id: str, **fields: Any) -> int:
    unknown = set(fields) - _UPDATE_ENTITY_FIELDS
    if unknown:
        raise ValueError(
            f"不允许更新的 entities 字段: {', '.join(sorted(unknown))}"
        )
    if not fields:
        return 0
    if "status" in fields and fields["status"] not in C.ENTITY_STATUSES:
        raise ValueError(f"未知 entity status: {fields['status']!r}")
    fields.setdefault("updated_at", utc_now())
    assignments = ", ".join(f"{column} = ?" for column in fields)
    params = list(fields.values()) + [entity_id]
    return conn.execute(
        f"UPDATE entities SET {assignments} WHERE entity_id = ?", params
    ).rowcount


def list_entities(
    conn: sqlite3.Connection,
    status: str | None = None,
    entity_type: str | None = None,
    limit: int | None = 50,
) -> list[EntityRecord]:
    if status is not None and status not in C.ENTITY_STATUSES:
        raise ValueError(f"未知 entity status: {status!r}")
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if entity_type is not None:
        where.append("entity_type = ?")
        params.append(entity_type)
    sql = "SELECT * FROM entities"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY canonical_name ASC, entity_id ASC"
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit 必须大于 0 或为 None")
        sql += " LIMIT ?"
        params.append(limit)
    return [_entity_from_row(row) for row in conn.execute(sql, params)]


def find_entities_by_normalized(
    conn: sqlite3.Connection,
    normalized_name: str,
    entity_type: str | None = None,
    statuses: tuple[str, ...] = ("active",),
    limit: int = 20,
) -> list[EntityRecord]:
    """按规范化 canonical_name 精确匹配。"""
    placeholders = ", ".join("?" for _ in statuses) if statuses else ""
    sql = "SELECT * FROM entities WHERE normalized_name = ? COLLATE NOCASE"
    params: list[Any] = [normalized_name]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    if statuses:
        sql += f" AND status IN ({placeholders})"
        params.extend(statuses)
    sql += (
        " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
        "canonical_name ASC, entity_id ASC LIMIT ?"
    )
    params.append(limit)
    return [_entity_from_row(row) for row in conn.execute(sql, params)]


def find_aliases_by_normalized(
    conn: sqlite3.Connection,
    normalized_alias: str,
    entity_type: str | None = None,
    statuses: tuple[str, ...] = ("active",),
    limit: int = 20,
) -> list[tuple[EntityRecord, EntityAliasRecord]]:
    """按规范化 alias 精确匹配，返回 (entity, alias) 列表。"""
    placeholders = ", ".join("?" for _ in statuses) if statuses else ""
    sql = (
        "SELECT e.*, a.alias AS alias_alias, a.normalized_alias AS alias_normalized, "
        "a.alias_type AS alias_alias_type, a.created_at AS alias_created_at "
        "FROM entity_aliases AS a JOIN entities AS e ON e.entity_id = a.entity_id "
        "WHERE a.normalized_alias = ? COLLATE NOCASE"
    )
    params: list[Any] = [normalized_alias]
    if entity_type:
        sql += " AND e.entity_type = ?"
        params.append(entity_type)
    if statuses:
        sql += f" AND e.status IN ({placeholders})"
        params.extend(statuses)
    sql += (
        " ORDER BY CASE e.status WHEN 'active' THEN 0 ELSE 1 END, "
        "e.canonical_name ASC, e.entity_id ASC LIMIT ?"
    )
    params.append(limit)

    results: list[tuple[EntityRecord, EntityAliasRecord]] = []
    for row in conn.execute(sql, params):
        data = dict(row)
        alias_record = EntityAliasRecord(
            entity_id=data["entity_id"],
            alias=data["alias_alias"],
            normalized_alias=data["alias_normalized"],
            alias_type=data["alias_alias_type"],
            created_at=data["alias_created_at"],
        )
        entity_data = {
            key: value
            for key, value in data.items()
            if key
            not in {
                "alias_alias",
                "alias_normalized",
                "alias_alias_type",
                "alias_created_at",
            }
        }
        results.append((EntityRecord.model_validate(entity_data), alias_record))
    return results


def find_aliases_by_entity(
    conn: sqlite3.Connection, entity_id: str
) -> list[EntityAliasRecord]:
    return [
        _alias_from_row(row)
        for row in conn.execute(
            "SELECT * FROM entity_aliases WHERE entity_id = ? "
            "ORDER BY alias_type ASC, alias ASC",
            (entity_id,),
        )
    ]


def insert_alias(
    conn: sqlite3.Connection,
    entity_id: str,
    alias: str,
    normalized_alias: str,
    alias_type: str = "alias",
    created_at: str | None = None,
    *,
    ignore: bool = True,
) -> bool:
    """插入别名；``ignore=True`` 时同一 (entity_id, normalized_alias) 冲突返回 False。"""
    if alias_type not in C.ALIAS_TYPES:
        raise ValueError(f"未知 alias_type: {alias_type!r}")
    verb = "INSERT OR IGNORE" if ignore else "INSERT"
    cursor = conn.execute(
        f"{verb} INTO entity_aliases "
        "(entity_id, alias, normalized_alias, alias_type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity_id, alias, normalized_alias, alias_type, created_at or utc_now()),
    )
    return cursor.rowcount > 0


def get_merge_target(
    conn: sqlite3.Connection, entity_id: str
) -> str | None:
    """返回实体 ``merged_into_entity_id``；未合并或实体不存在返回 None。"""
    row = conn.execute(
        "SELECT merged_into_entity_id FROM entities WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def add_page_entity(
    conn: sqlite3.Connection,
    record: PageEntityRecord,
) -> PageEntityRecord:
    conn.execute(
        "INSERT INTO page_entities "
        "(page_id, entity_id, role, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (page_id, entity_id) DO UPDATE SET "
        "role = excluded.role, confidence = excluded.confidence",
        (
            record.page_id,
            record.entity_id,
            record.role,
            record.confidence,
            record.created_at,
        ),
    )
    return record


def get_page_entities(
    conn: sqlite3.Connection, page_id: str
) -> list[PageEntityRecord]:
    return [
        _page_entity_from_row(row)
        for row in conn.execute(
            "SELECT * FROM page_entities WHERE page_id = ? "
            "ORDER BY entity_id ASC",
            (page_id,),
        )
    ]


def get_entity_pages(
    conn: sqlite3.Connection, entity_id: str
) -> list[PageEntityRecord]:
    return [
        _page_entity_from_row(row)
        for row in conn.execute(
            "SELECT * FROM page_entities WHERE entity_id = ? "
            "ORDER BY page_id ASC",
            (entity_id,),
        )
    ]


__all__ = [
    "add_page_entity",
    "find_aliases_by_entity",
    "find_aliases_by_normalized",
    "find_entities_by_normalized",
    "get_entity",
    "get_entity_pages",
    "get_merge_target",
    "get_page_entities",
    "insert_alias",
    "insert_entity",
    "list_entities",
    "update_entity",
    "utc_now",
]
