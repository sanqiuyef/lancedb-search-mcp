# -*- coding: utf-8 -*-
"""Wiki service 门面：只读查询、草稿写入、提案审核与两步抽取。

设计边界：
- 所有公开函数返回 ``dict``；可预期的输入/业务失败抛 ``WikiServiceError``，
  审核类决策（reject/stale）以 ``{"ok": false, "status": ...}`` 返回；
- 默认只产生 Draft / Proposal，不会自动发布页面；
- 服务端不内置 LLM、不调用外部 API；抽取由宿主 Agent 完成后提交；
- 所有写入都限制在 ``WikiConfig`` 解析出的 vault / SQLite 边界内。

连接与事务：
- 公开函数支持注入 ``config`` / ``conn``（关键字参数，测试用）；未注入时按
  ``WikiConfig.from_env()`` 打开独立连接并在结束时关闭；
- 实体/断言/关系与 page_suggestion 的 proposal 记录共用一个 SQLite 事务；
  提取阶段的意外异常整体回滚；proposal 自身的失败降级为 warning，不回滚
  已校验的抽取结果。
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping

from pydantic import ValidationError

from .. import search as kb_search
from . import constants as C
from . import entity_resolver
from . import evidence as wiki_evidence
from . import link_index
from . import navigation
from . import page_index
from . import recovery
from . import search as wiki_search
from . import validator as wiki_validator
from .config import WikiConfig, WikiDisabledError
from .contracts import (
    ExtractionCreated,
    ExtractionPayload,
    ExtractionRejection,
    ExtractionResult,
)
from .db import connect, db_status, run_migrations, transaction
from .models import EntityRecord, PageRecord, PageRevisionRecord
from .page_store import PageDraft, PageStore
from .proposals import ProposalService
from .repositories import claims as claims_repo
from .repositories import documents as documents_repo
from .repositories import entities as entities_repo
from .repositories import jobs as jobs_repo
from .repositories import pages as pages_repo
from .repositories import relations as relations_repo

MAX_PAGE_LIMIT = 200
MAX_RELATED_LIMIT = 200
MAX_REVIEW_LIMIT = 200
_DEFAULT_SUMMARY_CHARS = 120
_LIST_SPLIT_RE = re.compile(r"[,，;；、]+")
_MARKDOWN_PREFIX_RE = re.compile(r"^[#>*\-+\s\d.]+")


class WikiServiceError(RuntimeError):
    """Wiki service 统一异常；``code`` 便于 MCP/Agent 分支处理。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


def _service_call(func):
    """把内部异常统一包装为 WikiServiceError。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except WikiServiceError:
            raise
        except WikiDisabledError as exc:
            raise WikiServiceError(str(exc), code="wiki_disabled") from exc
        except Exception as exc:  # noqa: BLE001 - 统一 service 错误边界
            raise WikiServiceError(
                f"{func.__name__} 失败: {exc}", code=type(exc).__name__
            ) from exc

    return wrapper


# =============================================================
# 配置、连接与通用辅助
# =============================================================


def _resolve_config(config: WikiConfig | None) -> WikiConfig:
    if config is None:
        return WikiConfig.from_env()
    if not isinstance(config, WikiConfig):
        raise TypeError("config 必须是 WikiConfig")
    return config


@contextmanager
def _connection(
    config: WikiConfig,
    conn: sqlite3.Connection | None = None,
    *,
    migrate: bool = True,
) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
        return
    connection = connect(config.db_path, config)
    try:
        if migrate:
            run_migrations(connection)
        yield connection
    finally:
        connection.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    quoted = '"' + table.replace('"', '""') + '"'
    return int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])


def _group_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    if not _table_exists(conn, table):
        return {}
    quoted = '"' + table.replace('"', '""') + '"'
    quoted_column = '"' + column.replace('"', '""') + '"'
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT {quoted_column}, COUNT(*) FROM {quoted} GROUP BY {quoted_column}"
        )
    }


def _journal_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        buckets = recovery.scan_journal(conn)
    except Exception as exc:  # noqa: BLE001 - status 需尽量返回可用信息
        return {
            "unconverged": 0,
            "by_state": {state: 0 for state in recovery.OPEN_JOURNAL_STATES},
            "error": str(exc),
        }
    by_state = {state: len(items) for state, items in buckets.items()}
    return {"unconverged": sum(by_state.values()), "by_state": by_state}


def _journal_details(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        buckets = recovery.scan_journal(conn)
    except Exception as exc:  # noqa: BLE001
        return {"unconverged": 0, "by_state": {}, "items": [], "error": str(exc)}
    items: list[dict[str, Any]] = []
    for state in recovery.OPEN_JOURNAL_STATES:
        for record in buckets.get(state, []):
            items.append(record.model_dump(mode="json"))
    return {
        "unconverged": len(items),
        "by_state": {state: len(buckets.get(state, [])) for state in buckets},
        "items": items,
    }


def _split_list(value: Any, name: str) -> list[str]:
    """把逗号/顿号/分号分隔字符串或序列规整为非空、去重列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = _LIST_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        raise WikiServiceError(f"{name} 必须是字符串或字符串列表")
    result: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise WikiServiceError(f"{name}[{index}] 必须是字符串")
        cleaned = item.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _clamp_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise WikiServiceError(f"{name} 必须是整数") from exc
    return max(minimum, min(number, maximum))


def _require_text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WikiServiceError(f"{name} 必须是字符串")
    if not allow_empty and not value.strip():
        raise WikiServiceError(f"{name} 不能为空")
    return value


def _page_dict(page: PageRecord) -> dict[str, Any]:
    return page.model_dump(mode="json")


def _revision_dict(revision: PageRevisionRecord | None) -> dict[str, Any] | None:
    return revision.model_dump(mode="json") if revision is not None else None


def _page_path(config: WikiConfig, page: PageRecord) -> str:
    return os.path.join(
        config.vault_path, *page.relative_path.replace("\\", "/").split("/")
    )


def _derive_summary(content_md: str, fallback: str = "") -> str:
    """从正文提取首行有效文本作为 summary；失败时用 fallback。"""
    text = content_md or ""
    if text.lstrip().startswith(C.FRONTMATTER_DELIMITER):
        # 简单跳过 frontmatter，避免把元数据当摘要。
        parts = text.split(C.FRONTMATTER_DELIMITER, 2)
        if len(parts) == 3:
            text = parts[2]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("```", "---", "![", "|")):
            continue
        line = _MARKDOWN_PREFIX_RE.sub("", line).strip()
        if not line:
            continue
        line = re.sub(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]", r"\2", line)
        line = re.sub(r"\[\[([^\[\]]+)\]\]", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = line.replace("*", "").replace("`", "").strip()
        if line:
            return line[:_DEFAULT_SUMMARY_CHARS]
    return fallback.strip()[:_DEFAULT_SUMMARY_CHARS]


# =============================================================
# 1. status
# =============================================================


@_service_call
def status(
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """返回配置摘要、DB 版本、表计数、页面/提案/任务统计与未收敛 journal。"""
    cfg = _resolve_config(config)
    with _connection(cfg, conn, migrate=False) as connection:
        versions = db_status(connection)
        table_counts = {
            table: _count_rows(connection, table) for table in C.BUSINESS_TABLES
        }
        pages_by_status = _group_counts(connection, "pages", "status")
        pages_by_type = _group_counts(connection, "pages", "page_type")
        proposals_by_status = _group_counts(connection, "proposals", "status")
        jobs_by_status = _group_counts(connection, "wiki_jobs", "status")
        journal = _journal_summary(connection)
        try:
            index_status = page_index.index_status(connection, cfg)
        except Exception as exc:  # noqa: BLE001 - 状态查询不应整体失败
            index_status = {
                "table": cfg.page_index_table,
                "exists": False,
                "error": str(exc),
            }

    # 只暴露白名单配置项，不读取/回显任何环境变量或凭据。
    safe_config = {
        "vault_path": cfg.vault_path,
        "page_root": cfg.page_root,
        "db_path": cfg.db_path,
        "page_index_table": cfg.page_index_table,
        "auto_publish": cfg.auto_publish,
        "require_evidence": cfg.require_evidence,
        "min_entity_confidence": cfg.min_entity_confidence,
        "min_relation_confidence": cfg.min_relation_confidence,
        "max_auto_page_updates": cfg.max_auto_page_updates,
        "db_busy_timeout_ms": cfg.db_busy_timeout_ms,
        "git_commit": cfg.git_commit,
    }
    return {
        "ok": True,
        "enabled": cfg.enabled,
        "config": safe_config,
        "db": {
            "schema_versions": versions,
            "table_counts": table_counts,
            "business_table_count": len(C.BUSINESS_TABLES),
        },
        "pages": {
            "total": table_counts.get("pages", 0),
            "by_status": pages_by_status,
            "by_type": pages_by_type,
        },
        "proposals": {
            "total": table_counts.get("proposals", 0),
            "by_status": proposals_by_status,
        },
        "jobs": {
            "total": table_counts.get("wiki_jobs", 0),
            "by_status": jobs_by_status,
        },
        "page_index": index_status,
        "journal": journal,
    }


# =============================================================
# 2. search
# =============================================================


@_service_call
def search(
    query: str,
    mode: str = "page",
    limit: int = 10,
    page_type: str = "",
    tags: Any = "",
    source_filter: str = "",
    category_filter: str = "",
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """统一检索：page / evidence / hybrid / related。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    normalized_mode = (mode or "page").strip().lower()
    if normalized_mode not in ("page", "evidence", "hybrid", "related"):
        raise WikiServiceError(
            "mode 必须是 page/evidence/hybrid/related 之一"
            f"（当前值: {mode!r}）",
            code="invalid_mode",
        )
    if not isinstance(query, str) or not query.strip():
        raise WikiServiceError("query 不能为空")
    if page_type and page_type not in C.PAGE_TYPES:
        raise WikiServiceError(
            f"未知 page_type: {page_type!r}（允许: {', '.join(C.PAGE_TYPES)}）"
        )
    with _connection(cfg, conn) as connection:
        result = wiki_search.search_wiki(
            query,
            mode=normalized_mode,
            limit=limit,
            page_type=page_type,
            tags=_split_list(tags, "tags"),
            source_filter=source_filter,
            category_filter=category_filter,
            conn=connection,
            config=cfg,
        )
    return {"ok": True, **result}


# =============================================================
# 3. get_page
# =============================================================


def _link_dict(
    link, conn: sqlite3.Connection, *, outgoing: bool
) -> dict[str, Any]:
    item = link.model_dump(mode="json")
    if outgoing:
        target = (
            pages_repo.get_page(conn, link.target_page_id)
            if link.target_page_id
            else pages_repo.get_page_by_slug(conn, link.target_slug)
        )
        if target is not None:
            item["target_title"] = target.title
            item["target_page_type"] = str(target.page_type)
            item["target_status"] = str(target.status)
    else:
        source = pages_repo.get_page(conn, link.source_page_id)
        if source is not None:
            item["source_slug"] = source.slug
            item["source_title"] = source.title
            item["source_status"] = str(source.status)
    return item


def _page_evidence_summary(
    conn: sqlite3.Connection, page_id: str
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM claims WHERE page_id = ? ORDER BY created_at ASC, claim_id ASC",
        (page_id,),
    ).fetchall()
    claims: list[dict[str, Any]] = []
    evidence_total = 0
    for row in rows:
        claim = dict(row)
        evidence_rows = claims_repo.list_evidence(
            conn, claim_id=str(claim["claim_id"])
        )
        evidence_total += len(evidence_rows)
        evidence = []
        for evidence_row in evidence_rows:
            quote = str(evidence_row.get("quote") or "")
            stored_sha = str(evidence_row.get("text_sha256") or "")
            evidence.append(
                {
                    "evidence_id": evidence_row.get("evidence_id", ""),
                    "chunk_id": evidence_row.get("chunk_id", ""),
                    "quote": quote,
                    "char_start": evidence_row.get("char_start", 0),
                    "char_end": evidence_row.get("char_end", 0),
                    "confidence": evidence_row.get("confidence", 0.0),
                    "verified": bool(
                        stored_sha and stored_sha == wiki_evidence.quote_sha256(quote)
                    ),
                }
            )
        claims.append(
            {
                "claim_id": claim.get("claim_id", ""),
                "claim_text": claim.get("claim_text", ""),
                "claim_type": claim.get("claim_type", ""),
                "confidence": claim.get("confidence", 0.0),
                "status": claim.get("status", ""),
                "page_id": claim.get("page_id"),
                "document_id": claim.get("document_id"),
                "evidence": evidence,
            }
        )
    return {
        "claims": claims,
        "claim_count": len(claims),
        "evidence_count": evidence_total,
    }


@_service_call
def get_page(
    page_id_or_slug: str,
    include_sources: bool = True,
    include_backlinks: bool = True,
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """读取页面全文、当前修订、出链/反链与证据摘要。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    if not isinstance(page_id_or_slug, str) or not page_id_or_slug.strip():
        raise WikiServiceError("page_id_or_slug 不能为空")
    with _connection(cfg, conn) as connection:
        store = PageStore(cfg, connection)
        view = store.read_page(page_id_or_slug.strip())
        if view is None:
            raise WikiServiceError(
                f"页面不存在: {page_id_or_slug}", code="page_not_found"
            )
        links = [_link_dict(link, connection, outgoing=True) for link in view.links]
        backlinks = (
            [_link_dict(link, connection, outgoing=False) for link in view.backlinks]
            if include_backlinks
            else []
        )
        evidence_summary = (
            _page_evidence_summary(connection, view.page.page_id)
            if include_sources
            else None
        )
        sources = view.frontmatter.get("sources")
        if not isinstance(sources, list):
            sources = []
        return {
            "ok": True,
            "page": _page_dict(view.page),
            "frontmatter": dict(view.frontmatter),
            "body": view.body,
            "content": view.content,
            "content_sha256": view.content_sha256,
            "revision": _revision_dict(view.revision),
            "links": links,
            "backlinks": backlinks,
            "sources": sources,
            "evidence": evidence_summary,
        }


# =============================================================
# 4. list_pages
# =============================================================


def _encode_cursor(updated_at: str, page_id: str) -> str:
    raw = json.dumps(
        {"updated_at": updated_at, "page_id": page_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not cursor.strip():
        raise WikiServiceError("cursor 不能为空", code="invalid_cursor")
    try:
        padded = cursor.strip() + "=" * (-len(cursor.strip()) % 4)
        data = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
        updated_at = data["updated_at"]
        page_id = data["page_id"]
    except Exception as exc:  # noqa: BLE001
        raise WikiServiceError(
            f"cursor 非法: {cursor!r}", code="invalid_cursor"
        ) from exc
    if not isinstance(updated_at, str) or not isinstance(page_id, str):
        raise WikiServiceError("cursor 字段非法", code="invalid_cursor")
    return updated_at, page_id


@_service_call
def list_pages(
    page_type: str = "",
    status: str = "published",
    limit: int = 50,
    cursor: str = "",
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """按 updated_at DESC + page_id ASC 的键集分页列出页面。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    normalized_type = (page_type or "").strip()
    normalized_status = (status or "").strip()
    if normalized_type and normalized_type not in C.PAGE_TYPES:
        raise WikiServiceError(
            f"未知 page_type: {page_type!r}（允许: {', '.join(C.PAGE_TYPES)}）"
        )
    if normalized_status and normalized_status not in C.PAGE_STATUSES:
        raise WikiServiceError(
            f"未知 status: {status!r}（允许: {', '.join(C.PAGE_STATUSES)}）"
        )
    page_limit = _clamp_int(limit, "limit", 1, MAX_PAGE_LIMIT)

    where: list[str] = []
    params: list[Any] = []
    if normalized_type:
        where.append("page_type = ?")
        params.append(normalized_type)
    if normalized_status:
        where.append("status = ?")
        params.append(normalized_status)
    if cursor:
        last_updated, last_page_id = _decode_cursor(cursor)
        where.append("(updated_at < ? OR (updated_at = ? AND page_id > ?))")
        params.extend([last_updated, last_updated, last_page_id])

    sql = "SELECT * FROM pages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, page_id ASC LIMIT ?"
    params.append(page_limit + 1)

    with _connection(cfg, conn) as connection:
        rows = connection.execute(sql, params).fetchall()
        pages = [PageRecord.model_validate(dict(row)) for row in rows]
        has_more = len(pages) > page_limit
        pages = pages[:page_limit]
        next_cursor = (
            _encode_cursor(pages[-1].updated_at, pages[-1].page_id)
            if has_more and pages
            else ""
        )
    return {
        "ok": True,
        "pages": [_page_dict(page) for page in pages],
        "count": len(pages),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# =============================================================
# 5. get_related
# =============================================================


def _related_pages(
    conn: sqlite3.Connection, seed_page_id: str, depth: int, limit: int
) -> list[dict[str, Any]]:
    """在 page_links + page_entities 上做 1-2 跳 BFS。"""
    results: list[dict[str, Any]] = []
    visited = {seed_page_id}
    frontier = [seed_page_id]
    for current_depth in range(1, depth + 1):
        if not frontier or len(results) >= limit:
            break
        next_frontier: list[str] = []
        for via_page_id in frontier:
            neighbours: list[tuple[PageRecord, str, dict[str, Any]]] = []
            for link in link_index.outgoing_links(conn, via_page_id):
                target = (
                    pages_repo.get_page(conn, link.target_page_id)
                    if link.target_page_id
                    else pages_repo.get_page_by_slug(conn, link.target_slug)
                )
                if target is not None:
                    neighbours.append(
                        (
                            target,
                            "outgoing_link",
                            {
                                "target_slug": link.target_slug,
                                "display_text": link.display_text,
                            },
                        )
                    )
            for link in link_index.backlinks(conn, via_page_id):
                source = pages_repo.get_page(conn, link.source_page_id)
                if source is not None:
                    neighbours.append(
                        (
                            source,
                            "backlink",
                            {
                                "target_slug": link.target_slug,
                                "display_text": link.display_text,
                            },
                        )
                    )
            for page_entity in entities_repo.get_page_entities(conn, via_page_id):
                for linked in entities_repo.get_entity_pages(
                    conn, page_entity.entity_id
                ):
                    page = pages_repo.get_page(conn, linked.page_id)
                    if page is not None:
                        neighbours.append(
                            (
                                page,
                                "shared_entity",
                                {"entity_id": page_entity.entity_id},
                            )
                        )
            for page, kind, extra in neighbours:
                if page.page_id in visited:
                    continue
                visited.add(page.page_id)
                next_frontier.append(page.page_id)
                results.append(
                    {
                        "page_id": page.page_id,
                        "slug": page.slug,
                        "title": page.title,
                        "page_type": str(page.page_type),
                        "status": str(page.status),
                        "kind": kind,
                        "depth": current_depth,
                        "via_page_id": via_page_id,
                        **extra,
                    }
                )
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        frontier = next_frontier
    return results


@_service_call
def get_related(
    page_id: str = "",
    entity_id: str = "",
    depth: int = 1,
    limit: int = 30,
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """从页面/实体出发，BFS 返回 1-2 跳页面与关系邻域实体。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    page_key = (page_id or "").strip() or None
    entity_key = (entity_id or "").strip() or None
    if page_key is None and entity_key is None:
        raise WikiServiceError("page_id 与 entity_id 至少提供一个")
    try:
        depth_value = int(depth)
    except (TypeError, ValueError) as exc:
        raise WikiServiceError("depth 必须是整数 1 或 2") from exc
    if depth_value not in (1, 2):
        raise WikiServiceError("depth 只支持 1 或 2")
    limit_value = _clamp_int(limit, "limit", 1, MAX_RELATED_LIMIT)

    warnings: list[str] = []
    with _connection(cfg, conn) as connection:
        if page_key is not None and pages_repo.get_page(connection, page_key) is None:
            raise WikiServiceError(
                f"页面不存在: {page_key}", code="page_not_found"
            )
        if entity_key is not None and entities_repo.get_entity(
            connection, entity_key
        ) is None:
            raise WikiServiceError(
                f"实体不存在: {entity_key}", code="entity_not_found"
            )
        pages = (
            _related_pages(connection, page_key, depth_value, limit_value)
            if page_key
            else []
        )
        try:
            related_entities = relations_repo.get_related(
                connection,
                page_id=page_key,
                entity_id=entity_key,
                depth=depth_value,
                limit=limit_value,
            )
        except LookupError as exc:
            raise WikiServiceError(str(exc), code="entity_not_found") from exc
        entities = [
            {
                "entity_id": item.entity.entity_id,
                "canonical_name": item.entity.canonical_name,
                "entity_type": item.entity.entity_type,
                "status": str(item.entity.status),
                "confidence": float(item.entity.confidence),
                "predicate": item.predicate,
                "direction": item.direction,
                "depth": item.depth,
                "via_entity_id": item.via_entity_id,
                "relation_id": item.relation_id,
            }
            for item in related_entities
        ]
    return {
        "ok": True,
        "page_id": page_key or "",
        "entity_id": entity_key or "",
        "depth": depth_value,
        "pages": pages,
        "entities": entities,
        "warnings": warnings,
    }


# =============================================================
# 6. get_evidence
# =============================================================


def _evidence_item(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    chunk = documents_repo.get_chunk_ref(conn, str(row.get("chunk_id") or ""))
    revision = (
        documents_repo.get_revision(conn, str(chunk["document_revision_id"]))
        if chunk is not None
        else None
    )
    document = (
        documents_repo.get_document(conn, str(chunk["document_id"]))
        if chunk is not None
        else None
    )
    return {
        "evidence_id": row.get("evidence_id", ""),
        "claim_id": row.get("claim_id", ""),
        "chunk_id": row.get("chunk_id", ""),
        "quote": row.get("quote", ""),
        "char_start": row.get("char_start", 0),
        "char_end": row.get("char_end", 0),
        "text_sha256": row.get("text_sha256", ""),
        "confidence": row.get("confidence", 0.0),
        "created_at": row.get("created_at", ""),
        "chunk_ref": chunk,
        "document": document,
        "document_revision": revision,
    }


@_service_call
def get_evidence(
    claim_id: str = "",
    chunk_id: str = "",
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """claim → 证据链；chunk → evidence_bundle。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    claim_key = (claim_id or "").strip()
    chunk_key = (chunk_id or "").strip()
    if not claim_key and not chunk_key:
        raise WikiServiceError("claim_id 与 chunk_id 至少提供一个")
    with _connection(cfg, conn) as connection:
        if claim_key:
            claim = claims_repo.get_claim(connection, claim_key)
            if claim is None:
                raise WikiServiceError(
                    f"claim 不存在: {claim_key}", code="claim_not_found"
                )
            rows = claims_repo.list_evidence(connection, claim_id=claim_key)
            evidence = [_evidence_item(connection, row) for row in rows]
            return {
                "ok": True,
                "claim": claim,
                "evidence": evidence,
                "count": len(evidence),
            }
        bundle = wiki_evidence.evidence_bundle(connection, chunk_key)
        if bundle is None:
            raise WikiServiceError(
                f"chunk 不存在: {chunk_key}", code="chunk_not_found"
            )
        return {"ok": True, "chunk_id": chunk_key, "bundle": bundle}


# =============================================================
# 7. search_evidence
# =============================================================


@_service_call
def search_evidence(
    query: str,
    limit: int = 5,
    source_filter: str = "",
    category_filter: str = "",
    search_mode: str = "hybrid",
    *,
    config: WikiConfig | None = None,
) -> dict[str, Any]:
    """透传 kb.search 的证据检索，统一标注 ``score_source=rag``。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    if not isinstance(query, str) or not query.strip():
        raise WikiServiceError("query 不能为空")
    try:
        result = kb_search.search_structured(
            query,
            limit=limit,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
        )
    except Exception as exc:  # noqa: BLE001
        raise WikiServiceError(
            f"证据检索失败: {exc}", code="evidence_search_failed"
        ) from exc
    return {"ok": True, "score_source": "rag", **result}


# =============================================================
# 8. create_page
# =============================================================


@_service_call
def create_page(
    title: str,
    page_type: str,
    content_md: str,
    slug: str = "",
    aliases: Any = None,
    tags: Any = None,
    summary: str = "",
    sources: Any = None,
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """创建 vault 页面，状态固定为 draft；不发布、不做 git commit。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    title_text = _require_text("title", title)
    if page_type not in C.PAGE_TYPES:
        raise WikiServiceError(
            f"page_type 只接受 {', '.join(C.PAGE_TYPES)}（当前值: {page_type!r}）"
        )
    if not isinstance(content_md, str):
        raise WikiServiceError("content_md 必须是字符串")
    summary_text = summary.strip() if isinstance(summary, str) else ""
    if not summary_text:
        summary_text = _derive_summary(content_md, fallback=title_text)
    if not summary_text:
        raise WikiServiceError("summary 不能为空（且无法从 content_md 生成）")
    slug_text = slug.strip() if isinstance(slug, str) else ""
    alias_list = _split_list(aliases, "aliases")
    tag_list = _split_list(tags, "tags")
    source_list = _split_list(sources, "sources")

    with _connection(cfg, conn) as connection:
        store = PageStore(cfg, connection)
        page = store.create_page(
            PageDraft(
                title=title_text.strip(),
                page_type=page_type,
                summary=summary_text,
                body=content_md,
                slug=slug_text or None,
                aliases=alias_list,
                tags=tag_list,
                sources=source_list,
                status="draft",
                change_reason="service.create_page",
            ),
            actor="agent",
            commit=False,
        )
        absolute_path = _page_path(cfg, page)
    return {
        "ok": True,
        "page_id": page.page_id,
        "slug": page.slug,
        "path": page.relative_path,
        "absolute_path": absolute_path,
        "revision_id": page.current_revision_id or "",
        "status": str(page.status),
        "page_type": str(page.page_type),
        "title": page.title,
        "summary": summary_text,
        "git_commit": "",
        "committed": False,
    }


# =============================================================
# 9. propose_update
# =============================================================


@_service_call
def propose_update(
    page_id: str,
    proposed_content_md: str,
    rationale: str,
    expected_revision_id: str,
    evidence_refs: Any = None,
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """创建 ``update_page`` proposal；不改页面、不发布。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    page_key = _require_text("page_id", page_id)
    if not isinstance(proposed_content_md, str):
        raise WikiServiceError("proposed_content_md 必须是字符串")
    _require_text("rationale", rationale, allow_empty=True)
    if not isinstance(expected_revision_id, str):
        raise WikiServiceError("expected_revision_id 必须是字符串")
    if evidence_refs is None:
        evidence_list: list[Any] = []
    elif isinstance(evidence_refs, (list, tuple)):
        evidence_list = list(evidence_refs)
    else:
        raise WikiServiceError("evidence_refs 必须是列表或 None")

    with _connection(cfg, conn) as connection:
        service = ProposalService(cfg, connection)
        record = service.create_proposal(
            proposal_type="update_page",
            target_page_id=page_key,
            proposed_content=proposed_content_md,
            rationale=rationale,
            expected_revision_id=expected_revision_id,
            evidence_refs=evidence_list,
            created_by="agent",
        )
        detail = service.get_proposal(record.proposal_id)
        draft_path = os.path.join(
            service.proposals_root, record.proposal_id, "draft.md"
        )
        diff_path = os.path.join(
            service.proposals_root, record.proposal_id, "diff.md"
        )
        return {
            "ok": True,
            "proposal_id": record.proposal_id,
            "status": str(record.status),
            "page_id": record.page_id or "",
            "expected_revision_id": record.expected_revision_id,
            "rationale": record.rationale,
            "diff": detail.diff if detail is not None else "",
            "draft_path": draft_path,
            "diff_path": diff_path,
        }


# =============================================================
# 10. submit_extraction
# =============================================================


def _rejection(
    kind: str,
    index: int,
    reason: str,
    *,
    error: str = "",
    name: str = "",
    candidates: list[dict[str, Any]] | None = None,
    item: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return ExtractionRejection(
        kind=kind,  # type: ignore[arg-type]
        index=index,
        reason=reason,
        error=error,
        name=name,
        candidates=candidates or [],
        item=dict(item) if item is not None else None,
    ).model_dump(mode="json")


def _entity_candidate(entity: EntityRecord) -> dict[str, Any]:
    return {
        "entity_id": entity.entity_id,
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "status": str(entity.status),
        "confidence": float(entity.confidence),
    }


def _find_exact_entity(
    conn: sqlite3.Connection, normalized: str, entity_type: str = ""
) -> tuple[EntityRecord | None, str]:
    """按规范化 canonical/alias 精确查重；不做相似合并。"""
    type_filter = entity_type or None
    canonical = entities_repo.find_entities_by_normalized(
        conn, normalized, entity_type=type_filter
    )
    if len(canonical) == 1:
        return canonical[0], "canonical"
    if len(canonical) > 1:
        return None, ""
    unique: list[EntityRecord] = []
    seen: set[str] = set()
    for entity, _alias in entities_repo.find_aliases_by_normalized(
        conn, normalized, entity_type=type_filter
    ):
        if entity.entity_id not in seen:
            seen.add(entity.entity_id)
            unique.append(entity)
    if len(unique) == 1:
        return unique[0], "alias"
    return None, ""


def _resolve_entity_ref(
    conn: sqlite3.Connection, ref: str
) -> tuple[EntityRecord | None, list[EntityRecord]]:
    """把关系端点解析为 active 实体（先按 entity_id，再按名称/别名）。"""
    entity = entities_repo.get_entity(conn, ref)
    if entity is not None and str(entity.status) == "active":
        return entity, [entity]
    try:
        normalized = entity_resolver.normalize_name(ref)
    except ValueError:
        return None, []
    canonical = [
        item
        for item in entities_repo.find_entities_by_normalized(conn, normalized)
        if str(item.status) == "active"
    ]
    if len(canonical) == 1:
        return canonical[0], canonical
    if len(canonical) > 1:
        return None, canonical
    unique: list[EntityRecord] = []
    seen: set[str] = set()
    for item, _alias in entities_repo.find_aliases_by_normalized(conn, normalized):
        if str(item.status) != "active" or item.entity_id in seen:
            continue
        seen.add(item.entity_id)
        unique.append(item)
    if len(unique) == 1:
        return unique[0], unique
    return None, unique


def _unknown_evidence_ids(
    conn: sqlite3.Connection, evidence_ids: Iterable[str]
) -> list[str]:
    unknown: list[str] = []
    for evidence_id in evidence_ids:
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        key = evidence_id.strip()
        if (
            documents_repo.get_chunk_ref(conn, key) is None
            and claims_repo.get_evidence(conn, key) is None
        ):
            unknown.append(key)
    return unknown


def _process_entity(
    conn: sqlite3.Connection,
    cfg: WikiConfig,
    entity,
    index: int,
    created: dict[str, list[dict[str, Any]]],
    rejected: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    try:
        normalized = entity_resolver.normalize_name(entity.name)
    except ValueError as exc:
        rejected.append(
            _rejection(
                "entity",
                index,
                "invalid_name",
                error=str(exc),
                name=entity.name,
                item=entity.model_dump(mode="json"),
            )
        )
        return

    if entity.confidence < cfg.min_entity_confidence:
        candidates = [
            _entity_candidate(item)
            for item in entity_resolver.find_candidates(
                conn, entity.name, entity.entity_type or None
            )[:5]
        ]
        rejected.append(
            _rejection(
                "entity",
                index,
                "below_confidence_threshold",
                error=(
                    f"confidence={entity.confidence} < "
                    f"{cfg.min_entity_confidence}，仅返回候选"
                ),
                name=entity.name,
                candidates=candidates,
                item=entity.model_dump(mode="json"),
            )
        )
        return

    existing, matched_by = _find_exact_entity(
        conn, normalized, entity.entity_type
    )
    unknown = _unknown_evidence_ids(conn, entity.evidence_ids)
    if unknown:
        warnings.append(
            f"实体 {entity.name!r} 的 evidence_ids 不存在: {', '.join(unknown)}"
        )
    record = entity_resolver.upsert_entity(
        conn,
        canonical_name=entity.name,
        entity_type=entity.entity_type,
        description=entity.description,
        confidence=entity.confidence,
        aliases=entity.aliases,
        created_by="agent",
    )
    aliases = [
        alias.alias for alias in entities_repo.find_aliases_by_entity(conn, record.entity_id)
    ]
    created["entities"].append(
        {
            "entity_id": record.entity_id,
            "canonical_name": record.canonical_name,
            "normalized_name": record.normalized_name,
            "entity_type": record.entity_type,
            "description": record.description,
            "confidence": float(record.confidence),
            "status": str(record.status),
            "aliases": aliases,
            "created": existing is None,
            "matched_by": matched_by or ("none" if existing is None else "canonical"),
        }
    )


def _process_claim(
    conn: sqlite3.Connection,
    claim,
    index: int,
    document_id: str,
    chunk_texts: Mapping[str, str],
    created: dict[str, list[dict[str, Any]]],
    rejected: list[dict[str, Any]],
) -> None:
    if claim.is_factual and not claim.evidence:
        rejected.append(
            _rejection(
                "claim",
                index,
                "missing_evidence",
                error="事实型 claim（factual/definition/result）必须提供证据",
                name=claim.claim_text[:80],
                item=claim.model_dump(mode="json"),
            )
        )
        return

    verified: list[tuple[Any, str]] = []
    for evidence_input in claim.evidence:
        ref = documents_repo.get_chunk_ref(conn, evidence_input.chunk_id)
        if ref is None:
            rejected.append(
                _rejection(
                    "claim",
                    index,
                    "not_found",
                    error=f"chunk_ref 不存在: {evidence_input.chunk_id}",
                    name=claim.claim_text[:80],
                    item=claim.model_dump(mode="json"),
                )
            )
            return
        if evidence_input.chunk_id in chunk_texts:
            chunk_text = chunk_texts[evidence_input.chunk_id]
        else:
            try:
                chunk_text = wiki_evidence.resolve_chunk_text(
                    conn, evidence_input.chunk_id
                )
            except Exception as exc:  # noqa: BLE001 - LanceDB 不可用按 not_found
                rejected.append(
                    _rejection(
                        "claim",
                        index,
                        "not_found",
                        error=f"无法取得 chunk 原文: {exc}",
                        name=claim.claim_text[:80],
                        item=claim.model_dump(mode="json"),
                    )
                )
                return
        status, _start, _end = wiki_evidence.verify_quote(
            chunk_text, evidence_input.quote
        )
        if status != wiki_evidence.VERIFIED:
            rejected.append(
                _rejection(
                    "claim",
                    index,
                    "not_found",
                    error=f"引文未在 chunk 原文中找到: {evidence_input.quote[:80]!r}",
                    name=claim.claim_text[:80],
                    item=claim.model_dump(mode="json"),
                )
            )
            return
        verified.append((evidence_input, chunk_text))

    claim_id = claims_repo.insert_claim(
        conn,
        claim_text=claim.claim_text,
        claim_type=claim.db_claim_type,
        document_id=document_id or None,
        confidence=claim.confidence,
        status="proposed",
    )
    evidence_ids: list[str] = []
    for evidence_input, chunk_text in verified:
        result = wiki_evidence.add_claim_evidence(
            conn,
            claim_id,
            evidence_input.chunk_id,
            evidence_input.quote,
            role=evidence_input.evidence_role,
            page_number=evidence_input.page_number,
            section_path=evidence_input.section_path,
            chunk_text=chunk_text,
            confidence=claim.confidence,
        )
        evidence_ids.append(str(result["evidence_id"]))
    created["claims"].append(
        {
            "claim_id": claim_id,
            "claim_text": claim.claim_text,
            "claim_type": claim.db_claim_type,
            "source_claim_type": claim.claim_type,
            "confidence": claim.confidence,
            "status": "proposed",
            "document_id": document_id or "",
            "evidence_ids": evidence_ids,
            "evidence_count": len(verified),
        }
    )


def _process_relation(
    conn: sqlite3.Connection,
    cfg: WikiConfig,
    relation,
    index: int,
    created: dict[str, list[dict[str, Any]]],
    rejected: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    if relation.predicate not in relations_repo.PREDICATES:
        rejected.append(
            _rejection(
                "relation",
                index,
                "invalid_predicate",
                error=(
                    f"谓词不在白名单: {relation.predicate!r}；允许: "
                    + ", ".join(relations_repo.PREDICATES)
                ),
                name=f"{relation.subject} -{relation.predicate}-> {relation.object}",
                item=relation.model_dump(mode="json"),
            )
        )
        return

    subject, subject_candidates = _resolve_entity_ref(conn, relation.subject)
    obj, object_candidates = _resolve_entity_ref(conn, relation.object)
    if subject is None or obj is None:
        unresolved = [
            {"ref": relation.subject, "candidates": [_entity_candidate(x) for x in subject_candidates]},
            {"ref": relation.object, "candidates": [_entity_candidate(x) for x in object_candidates]},
        ]
        rejected.append(
            _rejection(
                "relation",
                index,
                "unresolved_entity",
                error="subject/object 必须解析到已存在的 active 实体（名称或 entity_id）",
                name=f"{relation.subject} -{relation.predicate}-> {relation.object}",
                candidates=unresolved,
                item=relation.model_dump(mode="json"),
            )
        )
        return
    if subject.entity_id == obj.entity_id:
        rejected.append(
            _rejection(
                "relation",
                index,
                "self_loop",
                error="关系不能指向自身",
                name=f"{relation.subject} -{relation.predicate}-> {relation.object}",
                item=relation.model_dump(mode="json"),
            )
        )
        return
    if relation.confidence < cfg.min_relation_confidence:
        rejected.append(
            _rejection(
                "relation",
                index,
                "below_confidence_threshold",
                error=(
                    f"confidence={relation.confidence} < "
                    f"{cfg.min_relation_confidence}，仅返回候选"
                ),
                name=f"{relation.subject} -{relation.predicate}-> {relation.object}",
                candidates=[_entity_candidate(subject), _entity_candidate(obj)],
                item=relation.model_dump(mode="json"),
            )
        )
        return

    unknown = _unknown_evidence_ids(conn, relation.evidence_ids)
    if unknown:
        warnings.append(
            "关系 "
            f"{relation.subject} -{relation.predicate}-> {relation.object} "
            f"的 evidence_ids 不存在: {', '.join(unknown)}"
        )
    existing = relations_repo.find_relation(
        conn, subject.entity_id, relation.predicate, obj.entity_id
    )
    record = relations_repo.create_relation(
        conn,
        subject.entity_id,
        relation.predicate,
        obj.entity_id,
        confidence=relation.confidence,
        evidence=json.dumps(relation.evidence_ids, ensure_ascii=False),
        created_by="agent",
        status="proposed",
    )
    created["relations"].append(
        {
            "relation_id": record.relation_id,
            "subject_entity_id": subject.entity_id,
            "subject_name": subject.canonical_name,
            "predicate": record.predicate,
            "object_entity_id": obj.entity_id,
            "object_name": obj.canonical_name,
            "confidence": float(record.confidence),
            "status": str(record.status),
            "evidence_ids": list(relation.evidence_ids),
            "created": existing is None,
        }
    )


def _create_suggestion_proposal(
    cfg: WikiConfig,
    conn: sqlite3.Connection,
    suggestion,
) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    if suggestion.action == "create":
        warnings.append(
            "page_suggestion.action=create 不会自动建页；请先调用 wiki_create_page "
            "生成 draft，再用 wiki_propose_update 提交内容"
        )
        return None, warnings
    page = pages_repo.get_page(conn, suggestion.page_id)
    if page is None:
        warnings.append(f"page_suggestion 目标页面不存在: {suggestion.page_id}")
        return None, warnings
    service = ProposalService(cfg, conn)
    try:
        record = service.create_proposal(
            proposal_type="update_page",
            target_page_id=page.page_id,
            proposed_content=suggestion.content_md,
            rationale=suggestion.rationale or "agent extraction",
            expected_revision_id=page.current_revision_id or "",
            evidence_refs=[],
            created_by="agent",
        )
        detail = service.get_proposal(record.proposal_id)
    except Exception as exc:  # noqa: BLE001 - 抽取已提交，提案失败只告警
        warnings.append(f"page_suggestion 生成 proposal 失败: {exc}")
        return None, warnings
    return (
        {
            "proposal_id": record.proposal_id,
            "status": str(record.status),
            "page_id": record.page_id or "",
            "expected_revision_id": record.expected_revision_id,
            "diff": detail.diff if detail is not None else "",
        },
        warnings,
    )


@_service_call
def submit_extraction(
    payload: ExtractionPayload | Mapping[str, Any],
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """校验并落库 Agent 抽取结果（实体/断言/关系），并按需生成页面提案。

    失败语义：
    - 单条语义问题（低置信度、事实断言缺证据、引文不匹配、谓词非法等）
      进入 ``rejected``，其余对象继续处理；
    - 意外/冲突异常（如别名冲突需要显式 merge、DB 错误）抛 WikiServiceError，
      同一事务内的所有写入整体回滚。
    """
    cfg = _resolve_config(config)
    cfg.require_enabled()
    if isinstance(payload, ExtractionPayload):
        parsed = payload
    elif isinstance(payload, Mapping):
        try:
            parsed = ExtractionPayload.model_validate(dict(payload))
        except ValidationError as exc:
            raise WikiServiceError(
                f"抽取 payload 校验失败: {exc}", code="invalid_payload"
            ) from exc
    else:
        raise WikiServiceError("payload 必须是字典或 ExtractionPayload")

    created: dict[str, list[dict[str, Any]]] = {
        "entities": [],
        "claims": [],
        "relations": [],
    }
    rejected: list[dict[str, Any]] = []
    warnings: list[str] = []

    with _connection(cfg, conn) as connection:
        if parsed.source_document_id:
            if documents_repo.get_document(
                connection, parsed.source_document_id
            ) is None:
                raise WikiServiceError(
                    f"source_document_id 不存在: {parsed.source_document_id}",
                    code="source_document_not_found",
                )
        for chunk_id in parsed.source_chunk_ids:
            if documents_repo.get_chunk_ref(connection, chunk_id) is None:
                warnings.append(f"source_chunk_ids 中的 chunk 不存在: {chunk_id}")

        proposal_info: dict[str, Any] | None = None
        with transaction(connection):
            for index, entity in enumerate(parsed.entities):
                _process_entity(
                    connection, cfg, entity, index, created, rejected, warnings
                )
            for index, claim in enumerate(parsed.claims):
                _process_claim(
                    connection,
                    claim,
                    index,
                    parsed.source_document_id,
                    parsed.chunk_texts,
                    created,
                    rejected,
                )
            for index, relation in enumerate(parsed.relations):
                _process_relation(
                    connection, cfg, relation, index, created, rejected, warnings
                )
            # 页面建议的 proposal 记录同事务提交；提案服务在已开启事务中复用
            # 当前事务（kb.wiki.proposals）。
            suggestion = parsed.page_suggestion
            if suggestion is not None:
                if suggestion.content_md.strip():
                    proposal_info, proposal_warnings = _create_suggestion_proposal(
                        cfg, connection, suggestion
                    )
                    warnings.extend(proposal_warnings)
                else:
                    warnings.append(
                        "page_suggestion 未提供 content_md，未生成 proposal"
                    )

    result = ExtractionResult(
        ok=True,
        created=ExtractionCreated(**created),
        rejected=[ExtractionRejection(**item) for item in rejected],
        warnings=warnings,
        proposal=proposal_info,
    )
    return result.model_dump(mode="json")


# =============================================================
# 11. get_proposal
# =============================================================


@_service_call
def get_proposal(
    proposal_id: str,
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """返回提案全文、diff 与目标页当前修订。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    proposal_key = _require_text("proposal_id", proposal_id)
    with _connection(cfg, conn) as connection:
        service = ProposalService(cfg, connection)
        detail = service.get_proposal(proposal_key)
        if detail is None:
            raise WikiServiceError(
                f"提案不存在: {proposal_key}", code="proposal_not_found"
            )
        target_page = (
            pages_repo.get_page(connection, detail.record.page_id)
            if detail.record.page_id
            else None
        )
        return {
            "ok": True,
            "proposal": detail.record.model_dump(mode="json"),
            "payload": detail.payload,
            "draft": detail.draft,
            "diff": detail.diff,
            "target_page": (
                {
                    "page_id": target_page.page_id,
                    "slug": target_page.slug,
                    "title": target_page.title,
                    "page_type": str(target_page.page_type),
                    "status": str(target_page.status),
                    "current_revision_id": target_page.current_revision_id or "",
                    "relative_path": target_page.relative_path,
                }
                if target_page is not None
                else None
            ),
        }


# =============================================================
# 12. review_queue
# =============================================================


@_service_call
def review_queue(
    status: str = "pending",
    limit: int = 20,
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """列出指定状态的提案（默认 pending），附带目标页摘要。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    status_key = (status or "pending").strip()
    if status_key not in C.PROPOSAL_STATUSES:
        raise WikiServiceError(
            f"未知 proposal status: {status_key!r}（允许: "
            + ", ".join(C.PROPOSAL_STATUSES)
            + "）"
        )
    limit_value = _clamp_int(limit, "limit", 1, MAX_REVIEW_LIMIT)
    with _connection(cfg, conn) as connection:
        service = ProposalService(cfg, connection)
        records = service.review_queue(status=status_key)[:limit_value]
        items: list[dict[str, Any]] = []
        for record in records:
            target = (
                pages_repo.get_page(connection, record.page_id)
                if record.page_id
                else None
            )
            items.append(
                {
                    "proposal_id": record.proposal_id,
                    "proposal_type": record.proposal_type,
                    "status": str(record.status),
                    "page_id": record.page_id or "",
                    "page_slug": target.slug if target is not None else "",
                    "page_title": target.title if target is not None else "",
                    "page_status": str(target.status) if target is not None else "",
                    "expected_revision_id": record.expected_revision_id,
                    "rationale": record.rationale,
                    "created_by": record.created_by,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                    "reviewed_by": record.reviewed_by,
                    "reviewed_at": record.reviewed_at,
                    "applied_at": record.applied_at,
                    "result_revision_id": record.result_revision_id,
                    "error_message": record.error_message,
                }
            )
    return {
        "ok": True,
        "status": status_key,
        "count": len(items),
        "proposals": items,
    }


# =============================================================
# 13. apply_proposal
# =============================================================


def _record_index_pending(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    page: PageRecord,
    revision: PageRevisionRecord,
    error: str,
) -> None:
    jobs_repo.create_journal(
        conn,
        proposal_id=proposal_id,
        page_id=page.page_id,
        target_path=page.relative_path,
        revision_id=revision.revision_id,
        state="index_pending",
        detail={
            "action": "apply_proposal",
            "stage": "page_index",
            "error": error,
        },
        error_message=error,
    )


@_service_call
def apply_proposal(
    proposal_id: str,
    decision: str = "approve",
    review_note: str = "",
    actor: str = "user",
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """审核提案：approve 写页面 + 刷新导航 + 更新页面索引；reject 不改页面。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    proposal_key = _require_text("proposal_id", proposal_id)
    decision_key = (decision or "").strip().lower()
    if decision_key not in ("approve", "reject"):
        return {
            "ok": False,
            "status": "invalid_decision",
            "error": f"decision 必须是 approve 或 reject（当前值: {decision!r}）",
            "proposal_id": proposal_key,
        }
    actor_text = (actor or "").strip() or "user"
    if not isinstance(review_note, str):
        raise WikiServiceError("review_note 必须是字符串")

    with _connection(cfg, conn) as connection:
        store = PageStore(cfg, connection)
        service = ProposalService(cfg, connection, store)
        result = service.apply_proposal(
            proposal_key,
            approve=(decision_key == "approve"),
            review_note=review_note,
            actor=actor_text,
        )
        if not result.ok:
            return {
                "ok": False,
                "status": str(result.status),
                "error": result.message,
                "message": result.message,
                "proposal_id": proposal_key,
                "page_id": result.proposal.page_id or "",
            }
        if str(result.status) == "rejected":
            return {
                "ok": True,
                "status": "rejected",
                "applied": False,
                "proposal_id": proposal_key,
                "page_id": result.proposal.page_id or "",
                "message": result.message or "提案已拒绝，页面未修改",
                "warnings": [],
            }

        page = result.page
        revision = result.revision
        if page is None or revision is None:  # pragma: no cover - 防御性分支
            raise WikiServiceError("提案已应用但缺少页面或修订信息")

        # 审核通过即发布：正文型提案不携带 frontmatter，页面仍是 draft。
        # 这里把 create_page/update_page 的页面推进到 published，后续才会入索引。
        if str(result.proposal.proposal_type) in ("create_page", "update_page") and str(
            page.status
        ) in ("draft", "review"):
            pages_repo.update_page(connection, page.page_id, status="published")
            refreshed = pages_repo.get_page(connection, page.page_id)
            if refreshed is not None:
                page = refreshed

        warnings: list[str] = []

        # 1) 重建导航（purpose/index/overview），失败只告警。
        try:
            nav_result = navigation.refresh_navigation(
                connection, cfg, actor=actor_text
            )
            paths = dict(nav_result.get("paths") or navigation.nav_paths(cfg))
        except Exception as exc:  # noqa: BLE001 - 页面已提交，导航失败不阻断
            warnings.append(f"导航刷新失败: {exc}")
            nav_result = {
                "paths": navigation.nav_paths(cfg),
                "errors": [str(exc)],
                "purpose_created": False,
            }
            paths = dict(nav_result["paths"])
        for error in nav_result.get("errors") or []:
            warnings.append(f"导航刷新失败: {error}")

        # 2) 追加操作日志。
        log_path = ""
        try:
            log_path = navigation.append_log(
                cfg,
                actor_text,
                "publish",
                page.page_id,
                f"proposal={proposal_key} rev={revision.revision_id}",
            )
        except Exception as exc:  # noqa: BLE001 - 页面已提交，日志失败不阻断
            warnings.append(f"追加 log.md 失败: {exc}")

        # 3) 提交导航文件（页面已由 ProposalService 提交，允许两个提交）。
        nav_commit = ""
        if cfg.git_commit:
            candidates = [paths.get("index"), log_path, paths.get("overview")]
            existing = [
                path for path in candidates if path and os.path.isfile(path)
            ]
            if existing:
                try:
                    nav_commit = store.commit_paths(
                        existing, "wiki: refresh navigation"
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"导航文件 git 提交失败: {exc}")
            else:
                warnings.append("导航文件不存在，跳过 git 提交")

        # 4) 更新页面索引；失败写 index_pending journal，不影响审核结果。
        index_info: dict[str, Any] | None = None
        index_error = ""
        try:
            view = store.read_page(page.page_id)
            if view is None:
                raise WikiServiceError(
                    f"页面索引前读取失败: {page.page_id}", code="page_not_found"
                )
            page_status = str(view.page.status)
            if page_status != "published":
                warnings.append(
                    f"页面状态为 {page_status}，跳过页面索引（仅 published 页面入索引）"
                )
            else:
                index_info = page_index.index_page(
                    view.page,
                    view.body,
                    view.frontmatter.get("aliases") or [],
                    view.frontmatter.get("tags") or [],
                    cfg,
                    summary=view.frontmatter.get("summary") or "",
                )
        except Exception as exc:  # noqa: BLE001 - 索引失败不失败整个 apply
            index_error = str(exc)
            warnings.append(f"页面索引失败: {index_error}")
            try:
                _record_index_pending(
                    connection,
                    proposal_id=proposal_key,
                    page=page,
                    revision=revision,
                    error=index_error,
                )
            except Exception as journal_exc:  # noqa: BLE001
                warnings.append(f"写入 index_pending journal 失败: {journal_exc}")

    return {
        "ok": True,
        "status": "applied",
        "applied": True,
        "proposal_id": proposal_key,
        "page_id": page.page_id,
        "slug": page.slug,
        "page_type": str(page.page_type),
        "page_status": str(page.status),
        "revision_id": revision.revision_id,
        "git_commit": revision.git_commit,
        "navigation": {
            "paths": paths,
            "errors": list(nav_result.get("errors") or []),
            "purpose_created": bool(nav_result.get("purpose_created")),
            "git_commit": nav_commit,
        },
        "page_index": index_info,
        "index_error": index_error,
        "warnings": warnings,
    }


# =============================================================
# 14. validate
# =============================================================


_VALIDATE_SCOPE_ALIASES = {
    "all": "all",
    "journal": "journal",
    "page": "pages",
    "pages": "pages",
    "link": "links",
    "links": "links",
    "claim": "claims",
    "claims": "claims",
    "evidence": "claims",
    "document": "documents",
    "documents": "documents",
    "source": "documents",
    "sources": "documents",
    "entity": "entities",
    "entities": "entities",
    "relation": "relations",
    "relations": "relations",
}
_SCOPE_ISSUE_PREFIXES = {
    "pages": (
        "page_",
        "frontmatter_",
        "revision_id_",
        "current_revision_",
        "content_hash_",
        "orphan_page_file",
    ),
    "links": ("broken_link",),
    "claims": ("fact_claim_missing_evidence", "claim_source_missing"),
    "documents": ("source_missing",),
    "entities": ("entity_",),
    "relations": ("relation_",),
}


def _issue_in_scope(issue: Mapping[str, Any], scope: str) -> bool:
    prefixes = _SCOPE_ISSUE_PREFIXES.get(scope, ())
    code = str(issue.get("code") or "")
    return any(code.startswith(prefix) for prefix in prefixes)


@_service_call
def validate(
    scope: str = "all",
    *,
    config: WikiConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """一致性校验 + 未收敛 journal，按 level/code 分组返回。"""
    cfg = _resolve_config(config)
    cfg.require_enabled()
    raw_scope = (scope or "all").strip().lower()
    scope_key = _VALIDATE_SCOPE_ALIASES.get(raw_scope)
    if scope_key is None:
        raise WikiServiceError(
            f"未知 scope: {scope!r}（允许: "
            + ", ".join(sorted(set(_VALIDATE_SCOPE_ALIASES)))
            + "）"
        )
    with _connection(cfg, conn) as connection:
        issues = wiki_validator.validate(connection, cfg)
        journal = _journal_details(connection)
    if scope_key == "journal":
        issues = []
    elif scope_key != "all":
        issues = [issue for issue in issues if _issue_in_scope(issue, scope_key)]

    level_counts = Counter(str(issue.get("level")) for issue in issues)
    code_counts = Counter(str(issue.get("code")) for issue in issues)
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for issue in issues:
        level = str(issue.get("level") or "info")
        code = str(issue.get("code") or "")
        groups.setdefault(level, {}).setdefault(code, []).append(issue)
    return {
        "ok": True,
        "scope": scope_key,
        "summary": {
            "total": len(issues),
            "error": level_counts.get("error", 0),
            "warning": level_counts.get("warning", 0),
            "info": level_counts.get("info", 0),
            "codes": dict(code_counts),
        },
        "issues": issues,
        "groups": groups,
        "journal": journal,
    }


__all__ = [
    "MAX_PAGE_LIMIT",
    "MAX_RELATED_LIMIT",
    "MAX_REVIEW_LIMIT",
    "WikiServiceError",
    "apply_proposal",
    "create_page",
    "get_evidence",
    "get_page",
    "get_proposal",
    "get_related",
    "list_pages",
    "propose_update",
    "review_queue",
    "search",
    "search_evidence",
    "status",
    "submit_extraction",
    "validate",
]
