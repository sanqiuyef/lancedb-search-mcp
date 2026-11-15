# -*- coding: utf-8 -*-
"""实体规范化、候选召回、显式合并与页面关联。

设计约束：**不自动合并**。``upsert_entity`` 只做精确身份复用（canonical_name
或 alias 规范化后完全相等）；相似名称保留为独立实体，交给 ``merge_entity``
显式处理。

显式合并的持久化约定为：
- source 记录保留，``status`` 置 ``merged``，``merged_into_entity_id`` 写 target_id；
- target 接收 source 的 canonical_name 与全部别名（别名沿用原 alias_type）；
- 返回值 ``MergeResult.status == "merged"``、``merged_into == target_id``。
合并拒绝自环与合并链成环；已合并的 source 只能重复合并到同一 target。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from . import constants as C
from .db import transaction
from .ids import new_ulid
from .models import EntityRecord, PageEntityRecord
from .repositories import entities as entities_repo

_ACTIVE_STATUSES = ("active",)


class EntityResolutionError(RuntimeError):
    """实体解析/合并失败。"""


class EntityAmbiguityError(EntityResolutionError):
    """精确名称命中多个实体，需要显式选择或合并。"""

    def __init__(self, name: str, candidates: list[EntityRecord]) -> None:
        self.name = name
        self.candidates = candidates
        ids = ", ".join(entity.entity_id for entity in candidates)
        super().__init__(
            f"名称 {name!r} 精确命中多个实体（{ids}），请显式 merge 后再写入"
        )


class EntityMergeError(EntityResolutionError):
    """自环、环或已合并状态导致的显式合并失败。"""


@dataclass(frozen=True, slots=True)
class MergeResult:
    """显式合并结果；``status`` 与 ``source_status`` 均为 ``merged``。"""

    source_id: str
    target_id: str
    merged_into: str
    status: str
    source_status: str
    aliases_migrated: int
    redirect: dict[str, str]
    actor: str = ""
    source: EntityRecord | None = None
    target: EntityRecord | None = None


@contextmanager
def _transactional(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
    else:
        with transaction(conn):
            yield


def normalize_name(name: str) -> str:
    """casefold + NFKC，去掉标点、空白与控制字符，用于精确身份比较。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 必须是非空字符串")
    text = unicodedata.normalize("NFKC", name).casefold()
    kept: list[str] = []
    for char in text:
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("C"):
            continue
        kept.append(char)
    normalized = "".join(kept)
    if not normalized:
        raise ValueError(f"name 规范化后为空: {name!r}")
    return normalized


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_candidates(
    conn: sqlite3.Connection,
    name: str,
    entity_type: str | None = None,
) -> list[EntityRecord]:
    """规范化精确匹配 canonical/alias，再按 LIKE 召回相似名称。"""
    normalized = normalize_name(name)
    type_filter = entity_type or None
    candidates: list[EntityRecord] = []
    seen: set[str] = set()

    for entity in entities_repo.find_entities_by_normalized(
        conn, normalized, entity_type=type_filter
    ):
        if entity.entity_id not in seen:
            seen.add(entity.entity_id)
            candidates.append(entity)
    for entity, _alias in entities_repo.find_aliases_by_normalized(
        conn, normalized, entity_type=type_filter
    ):
        if entity.entity_id not in seen:
            seen.add(entity.entity_id)
            candidates.append(entity)

    pattern = f"%{_escape_like(normalized)}%"
    status_placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
    sql = (
        "SELECT DISTINCT e.* FROM entities AS e "
        "LEFT JOIN entity_aliases AS a ON a.entity_id = e.entity_id "
        f"WHERE e.status IN ({status_placeholders}) "
        "AND (e.normalized_name LIKE ? ESCAPE '\\' "
        "     OR a.normalized_alias LIKE ? ESCAPE '\\')"
    )
    params: list[Any] = [*_ACTIVE_STATUSES, pattern, pattern]
    if type_filter:
        sql += " AND e.entity_type = ?"
        params.append(type_filter)
    sql += (
        " ORDER BY CASE e.status WHEN 'active' THEN 0 ELSE 1 END, "
        "e.canonical_name ASC, e.entity_id ASC LIMIT ?"
    )
    params.append(20)
    for row in conn.execute(sql, params):
        entity = EntityRecord.model_validate(dict(row))
        if entity.entity_id not in seen:
            seen.add(entity.entity_id)
            candidates.append(entity)
    return candidates[:20]


def _exact_identity(
    conn: sqlite3.Connection,
    normalized: str,
    entity_type: str | None,
) -> EntityRecord | None:
    canonical = entities_repo.find_entities_by_normalized(
        conn, normalized, entity_type=entity_type
    )
    if len(canonical) > 1:
        raise EntityAmbiguityError(normalized, canonical)
    if canonical:
        return canonical[0]

    alias_matches = entities_repo.find_aliases_by_normalized(
        conn, normalized, entity_type=entity_type
    )
    unique: list[EntityRecord] = []
    seen: set[str] = set()
    for entity, _alias in alias_matches:
        if entity.entity_id not in seen:
            seen.add(entity.entity_id)
            unique.append(entity)
    if len(unique) > 1:
        raise EntityAmbiguityError(normalized, unique)
    return unique[0] if unique else None


def get_merge_target(
    conn: sqlite3.Connection, entity_id: str
) -> str | None:
    """source 实体显式合并后的 target_id；未合并返回 None。"""
    return entities_repo.get_merge_target(conn, entity_id)


def _assert_alias_not_taken(
    conn: sqlite3.Connection,
    target_entity: EntityRecord,
    alias: str,
    normalized_alias: str,
) -> None:
    if normalized_alias == target_entity.normalized_name:
        return
    others = entities_repo.find_entities_by_normalized(
        conn, normalized_alias
    )
    conflicts = [
        entity for entity in others if entity.entity_id != target_entity.entity_id
    ]
    if conflicts:
        raise EntityAmbiguityError(alias, conflicts)
    alias_matches = entities_repo.find_aliases_by_normalized(
        conn, normalized_alias
    )
    for entity, _record in alias_matches:
        if entity.entity_id != target_entity.entity_id:
            raise EntityAmbiguityError(alias, [entity])


def upsert_entity(
    conn: sqlite3.Connection,
    canonical_name: str,
    entity_type: str = "",
    description: str = "",
    confidence: float = 0.0,
    status: str = "active",
    aliases: list[str] | tuple[str, ...] | None = None,
    page_id: str | None = None,
    role: str = "mention",
    page_confidence: float | None = None,
    created_by: str = "",
) -> EntityRecord:
    """按规范名/别名精确复用实体，否则新建；可同时写别名与 page_entities。"""
    if not isinstance(canonical_name, str) or not canonical_name.strip():
        raise ValueError("canonical_name 必须是非空字符串")
    if status not in C.ENTITY_STATUSES:
        raise ValueError(f"未知 entity status: {status!r}")
    if entity_type and not isinstance(entity_type, str):
        raise ValueError("entity_type 必须是字符串")
    normalized = normalize_name(canonical_name)
    alias_values: list[tuple[str, str]] = []
    for index, alias in enumerate(aliases or []):
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"aliases[{index}] 必须是非空字符串")
        alias_values.append((alias.strip(), normalize_name(alias)))

    with _transactional(conn):
        entity = _exact_identity(conn, normalized, entity_type or None)
        now = entities_repo.utc_now()
        if entity is None:
            entity = EntityRecord(
                entity_id=new_ulid(),
                canonical_name=canonical_name.strip(),
                normalized_name=normalized,
                entity_type=entity_type or "",
                description=description or "",
                confidence=confidence,
                status=status,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            entities_repo.insert_entity(conn, entity)
        else:
            updates: dict[str, Any] = {}
            if entity_type and not entity.entity_type:
                updates["entity_type"] = entity_type
            if description and description != entity.description:
                updates["description"] = description
            if confidence > entity.confidence:
                updates["confidence"] = confidence
            if updates:
                updates["updated_at"] = now
                entities_repo.update_entity(conn, entity.entity_id, **updates)
                refreshed = entities_repo.get_entity(conn, entity.entity_id)
                assert refreshed is not None
                entity = refreshed

        for alias, normalized_alias in alias_values:
            if normalized_alias == entity.normalized_name:
                continue
            _assert_alias_not_taken(conn, entity, alias, normalized_alias)
            entities_repo.insert_alias(
                conn,
                entity.entity_id,
                alias,
                normalized_alias,
                alias_type="alias",
                created_at=now,
                ignore=True,
            )

        if page_id is not None:
            entities_repo.add_page_entity(
                conn,
                PageEntityRecord(
                    page_id=page_id,
                    entity_id=entity.entity_id,
                    role=role,
                    confidence=(
                        entity.confidence
                        if page_confidence is None
                        else page_confidence
                    ),
                    created_at=now,
                ),
            )

    refreshed = entities_repo.get_entity(conn, entity.entity_id)
    assert refreshed is not None
    return refreshed


def merge_entity(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    actor: str = "agent",
) -> MergeResult:
    """显式合并 source → target；不自动调用，拒绝自环与合并链环。"""
    if not source_id or not target_id:
        raise ValueError("source_id 与 target_id 必须非空")
    if source_id == target_id:
        raise EntityMergeError("不能把实体合并到自身（自环）")

    source = entities_repo.get_entity(conn, source_id)
    target = entities_repo.get_entity(conn, target_id)
    if source is None:
        raise LookupError(f"source 实体不存在: {source_id}")
    if target is None:
        raise LookupError(f"target 实体不存在: {target_id}")

    existing_target = entities_repo.get_merge_target(conn, source_id)
    if existing_target is not None:
        if existing_target == target_id:
            return MergeResult(
                source_id=source_id,
                target_id=target_id,
                merged_into=target_id,
                status="merged",
                source_status=str(source.status),
                aliases_migrated=0,
                redirect={
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "from": source.canonical_name,
                    "to": target.canonical_name,
                },
                actor=actor,
                source=source,
                target=target,
            )
        raise EntityMergeError(
            f"source 已合并到 {existing_target}，不能重复合并到 {target_id}"
        )

    # 沿 target 的合并链检查环；target 本身已合并时拒绝隐式穿透。
    cursor = target_id
    visited: set[str] = set()
    for _ in range(50):
        if cursor == source_id:
            raise EntityMergeError(
                f"合并会形成合并链环: {source_id} -> {target_id}"
            )
        if cursor in visited:
            raise EntityMergeError(f"target 合并链存在环: {target_id}")
        visited.add(cursor)
        next_id = entities_repo.get_merge_target(conn, cursor)
        if next_id is None:
            break
        cursor = next_id
    else:  # pragma: no cover - 50 跳保护
        raise EntityMergeError("target 合并链过长")
    if cursor != target_id:
        raise EntityMergeError(
            f"target 已合并到 {cursor}，请直接合并到最终目标"
        )

    now = entities_repo.utc_now()
    migrated = 0
    with _transactional(conn):
        for alias_record in entities_repo.find_aliases_by_entity(conn, source_id):
            inserted = entities_repo.insert_alias(
                conn,
                target_id,
                alias_record.alias,
                alias_record.normalized_alias,
                alias_type=alias_record.alias_type,
                created_at=now,
                ignore=True,
            )
            migrated += 1 if inserted else 0

        if entities_repo.insert_alias(
            conn,
            target_id,
            source.canonical_name,
            source.normalized_name,
            alias_type="alias",
            created_at=now,
            ignore=True,
        ):
            migrated += 1

        entities_repo.update_entity(
            conn,
            source_id,
            status="merged",
            merged_into_entity_id=target_id,
            updated_at=now,
        )

    refreshed_source = entities_repo.get_entity(conn, source_id)
    refreshed_target = entities_repo.get_entity(conn, target_id)
    assert refreshed_source is not None and refreshed_target is not None
    return MergeResult(
        source_id=source_id,
        target_id=target_id,
        merged_into=target_id,
        status="merged",
        source_status=str(refreshed_source.status),
        aliases_migrated=migrated,
        redirect={
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "from": refreshed_source.canonical_name,
            "to": refreshed_target.canonical_name,
        },
        actor=actor,
        source=refreshed_source,
        target=refreshed_target,
    )


__all__ = [
    "EntityAmbiguityError",
    "EntityMergeError",
    "EntityResolutionError",
    "MergeResult",
    "find_candidates",
    "get_merge_target",
    "merge_entity",
    "normalize_name",
    "upsert_entity",
]
