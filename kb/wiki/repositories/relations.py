# -*- coding: utf-8 -*-
"""``relations`` 表读写、谓词白名单与实体邻域 BFS。

关系身份由 ``UNIQUE (subject_entity_id, predicate, object_entity_id)`` 保证；
插入冲突时返回已有记录。状态默认 proposed，verified 只用于显式校验之后。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import constants as C
from ..models import EntityRecord, RelationRecord
from . import entities as entities_repo

# 谓词白名单：新增谓词必须同时更新本表、测试与文档。
PREDICATES = (
    "contains_component",
    "uses",
    "based_on",
    "improves",
    "part_of",
    "predicts",
    "evaluated_on",
    "compared_with",
    "supports",
    "contradicts",
    "related_to",
)

RELATION_STATUSES = C.RELATION_STATUSES
_ACTIVE_STATUSES = ("proposed", "verified")


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _relation_from_row(row: sqlite3.Row) -> RelationRecord:
    return RelationRecord.model_validate(dict(row))


def validate_predicate(predicate: str) -> str:
    if predicate not in PREDICATES:
        allowed = ", ".join(PREDICATES)
        raise ValueError(f"未知关系谓词: {predicate!r}（允许: {allowed}）")
    return predicate


def get_relation(
    conn: sqlite3.Connection, relation_id: str
) -> RelationRecord | None:
    row = conn.execute(
        "SELECT * FROM relations WHERE relation_id = ?", (relation_id,)
    ).fetchone()
    return _relation_from_row(row) if row is not None else None


def find_relation(
    conn: sqlite3.Connection,
    subject_entity_id: str,
    predicate: str,
    object_entity_id: str,
) -> RelationRecord | None:
    row = conn.execute(
        "SELECT * FROM relations WHERE subject_entity_id = ? AND predicate = ? "
        "AND object_entity_id = ? ORDER BY created_at ASC, relation_id ASC LIMIT 1",
        (subject_entity_id, predicate, object_entity_id),
    ).fetchone()
    return _relation_from_row(row) if row is not None else None


def insert_relation(
    conn: sqlite3.Connection, record: RelationRecord
) -> RelationRecord:
    """插入关系；subject/predicate/object 已存在时返回已有记录。"""
    data = record.model_dump(mode="json")
    columns = list(RelationRecord.model_fields)
    cursor = conn.execute(
        f"INSERT INTO relations ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        "ON CONFLICT (subject_entity_id, predicate, object_entity_id) "
        "DO NOTHING",
        tuple(data[column] for column in columns),
    )
    if cursor.rowcount == 0:
        existing = find_relation(
            conn,
            record.subject_entity_id,
            record.predicate,
            record.object_entity_id,
        )
        if existing is None:  # pragma: no cover - 冲突行必然可查回
            raise sqlite3.IntegrityError("关系插入冲突但未找到已有记录")
        return existing
    return record


def create_relation(
    conn: sqlite3.Connection,
    subject_entity_id: str,
    predicate: str,
    object_entity_id: str,
    confidence: float = 0.0,
    source_page_id: str | None = None,
    evidence: str = "",
    created_by: str = "",
    status: str = "proposed",
) -> RelationRecord:
    """创建关系；同一 subject/predicate/object 已存在时返回已有记录。"""
    validate_predicate(predicate)
    if not subject_entity_id or not object_entity_id:
        raise ValueError("subject_entity_id 与 object_entity_id 必须非空")
    if subject_entity_id == object_entity_id:
        raise ValueError("关系不能指向自身（自环）")
    if status not in RELATION_STATUSES:
        raise ValueError(f"未知 relation status: {status!r}")

    now = utc_now()
    record = RelationRecord(
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        object_entity_id=object_entity_id,
        status=status,
        confidence=confidence,
        source_page_id=source_page_id,
        evidence=evidence,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    return insert_relation(conn, record)


def list_relations(
    conn: sqlite3.Connection,
    subject_entity_id: str | None = None,
    object_entity_id: str | None = None,
    predicate: str | None = None,
    status: str | None = None,
    limit: int | None = 100,
) -> list[RelationRecord]:
    if predicate is not None:
        validate_predicate(predicate)
    if status is not None and status not in RELATION_STATUSES:
        raise ValueError(f"未知 relation status: {status!r}")
    where: list[str] = []
    params: list[Any] = []
    if subject_entity_id is not None:
        where.append("subject_entity_id = ?")
        params.append(subject_entity_id)
    if object_entity_id is not None:
        where.append("object_entity_id = ?")
        params.append(object_entity_id)
    if predicate is not None:
        where.append("predicate = ?")
        params.append(predicate)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM relations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC, relation_id ASC"
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit 必须大于 0 或为 None")
        sql += " LIMIT ?"
        params.append(limit)
    return [_relation_from_row(row) for row in conn.execute(sql, params)]


def update_relation_status(
    conn: sqlite3.Connection, relation_id: str, status: str
) -> RelationRecord | None:
    if status not in RELATION_STATUSES:
        raise ValueError(f"未知 relation status: {status!r}")
    cursor = conn.execute(
        "UPDATE relations SET status = ?, updated_at = ? WHERE relation_id = ?",
        (status, utc_now(), relation_id),
    )
    if cursor.rowcount == 0:
        return None
    return get_relation(conn, relation_id)


@dataclass(frozen=True, slots=True)
class RelatedEntity:
    """BFS 命中的一个邻域实体。"""

    entity: EntityRecord
    depth: int
    relation_id: str
    predicate: str
    direction: str  # "outgoing" | "incoming"
    via_entity_id: str


def _page_seed_entities(
    conn: sqlite3.Connection, page_id: str
) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT entity_id FROM page_entities WHERE page_id = ? "
            "ORDER BY entity_id ASC",
            (page_id,),
        )
    ]


def _neighbour_relations(
    conn: sqlite3.Connection, entity_ids: Iterable[str]
) -> list[RelationRecord]:
    ids = list(dict.fromkeys(entity_ids))
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    status_placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
    sql = (
        f"SELECT * FROM relations WHERE "
        f"(subject_entity_id IN ({placeholders}) "
        f" OR object_entity_id IN ({placeholders})) "
        f"AND status IN ({status_placeholders}) "
        f"ORDER BY created_at ASC, relation_id ASC"
    )
    params = ids + ids + list(_ACTIVE_STATUSES)
    return [_relation_from_row(row) for row in conn.execute(sql, params)]


def get_related(
    conn: sqlite3.Connection,
    page_id: str | None = None,
    entity_id: str | None = None,
    depth: int = 1,
    limit: int = 30,
) -> list[RelatedEntity]:
    """从页面关联实体或指定实体出发，BFS 返回 1-2 跳邻域实体。

    只沿 proposed/verified 的关系扩展；merged/deprecated 实体不进入结果。
    """
    if page_id is None and entity_id is None:
        raise ValueError("page_id 与 entity_id 至少提供一个")
    if depth not in (1, 2):
        raise ValueError("depth 只支持 1 或 2")
    limit = int(limit)
    if limit <= 0:
        raise ValueError("limit 必须大于 0")

    seeds: list[str] = []
    if entity_id is not None:
        if entities_repo.get_entity(conn, entity_id) is None:
            raise LookupError(f"实体不存在: {entity_id}")
        seeds.append(entity_id)
    if page_id is not None:
        seeds.extend(_page_seed_entities(conn, page_id))
    seeds = list(dict.fromkeys(seeds))
    if not seeds:
        return []

    visited = set(seeds)
    frontier = list(seeds)
    results: list[RelatedEntity] = []

    for current_depth in range(1, depth + 1):
        if not frontier or len(results) >= limit:
            break
        next_frontier: list[str] = []
        for relation in _neighbour_relations(conn, frontier):
            if relation.subject_entity_id in frontier:
                neighbour_id = relation.object_entity_id
                direction = "outgoing"
                via_id = relation.subject_entity_id
            elif relation.object_entity_id in frontier:
                neighbour_id = relation.subject_entity_id
                direction = "incoming"
                via_id = relation.object_entity_id
            else:  # pragma: no cover - SQL 已限制
                continue
            if neighbour_id in visited:
                continue

            neighbour = entities_repo.get_entity(conn, neighbour_id)
            if neighbour is None or str(neighbour.status) != "active":
                continue
            visited.add(neighbour_id)
            next_frontier.append(neighbour_id)
            results.append(
                RelatedEntity(
                    entity=neighbour,
                    depth=current_depth,
                    relation_id=relation.relation_id,
                    predicate=relation.predicate,
                    direction=direction,
                    via_entity_id=via_id,
                )
            )
            if len(results) >= limit:
                break
        frontier = next_frontier

    results.sort(
        key=lambda item: (
            item.depth,
            item.entity.canonical_name.casefold(),
            item.entity.entity_id,
        )
    )
    return results


__all__ = [
    "PREDICATES",
    "RELATION_STATUSES",
    "RelatedEntity",
    "create_relation",
    "find_relation",
    "get_related",
    "get_relation",
    "insert_relation",
    "list_relations",
    "update_relation_status",
    "utc_now",
    "validate_predicate",
]
