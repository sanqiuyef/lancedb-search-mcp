# -*- coding: utf-8 -*-
"""Wiki 统一检索入口：页面索引、证据检索与图谱邻域。

三种来源各自带明确分数来源，不把相似度/融合分数解释为概率：

- ``search_pages``：页面索引 hybrid（向量 + FTS），优先用
  ``kb.search.LocalCrossEncoderReranker`` 精排，模型不可用时由本地
  reranker 回退 RRF；hybrid 本身失败时依次降级到向量、FTS。
- ``search_evidence``：直接调用 ``kb.search.search_structured``，分数来源标
  ``rag``（其内部已带 BM25/距离/精排语义）。
- ``search_wiki(mode="related")``：只查 SQLite 的 pages/page_links/
  page_entities/relations，depth=1，不走向量。

所有函数在索引表/知识库为空时返回空列表并附带 warning，而不是抛异常。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from lancedb.rerankers import Reranker, RRFReranker

from .. import search as kb_search
from . import link_index
from . import page_index
from .config import WikiConfig
from .models import PageRecord
from .repositories import entities as entities_repo
from .repositories import pages as pages_repo
from .repositories import relations as relations_repo

MAX_LIMIT = 100
DEFAULT_SNIPPET_CHARS = 160
_TERM_SPLIT_RE = re.compile(r"[,，;；、\s]+")
_WHITESPACE_RE = re.compile(r"\s+")


# =============================================================
# 本地 reranker 包装：记录是否回退 RRF
# =============================================================

class _TrackingReranker(Reranker):
    """委托 ``LocalCrossEncoderReranker`` 精排，并记录模型失败后的 RRF 回退。"""

    def __init__(self) -> None:
        super().__init__()
        self._inner = kb_search.LocalCrossEncoderReranker()
        self.used_fallback = False
        self.fallback_reason = ""

    def rerank_hybrid(self, query, vector_results, fts_results):
        try:
            return self._inner._rerank(query, vector_results, fts_results)
        except Exception as exc:
            self.used_fallback = True
            self.fallback_reason = str(exc)
            return self._inner.fallback.rerank_hybrid(
                query, vector_results, fts_results
            )


# =============================================================
# 公共辅助
# =============================================================

def _resolve_config(config: WikiConfig | None) -> WikiConfig:
    if config is not None:
        if not isinstance(config, WikiConfig):
            raise TypeError("config 必须是 WikiConfig")
        return config
    return WikiConfig.from_env()


def _normalize_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, MAX_LIMIT))


def _split_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [term for term in _TERM_SPLIT_RE.split(value) if term]
    if isinstance(value, (list, tuple, set)):
        terms: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                terms.append(text)
        return terms
    raise TypeError("tags 必须是字符串或字符串列表")


def _like_escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace("'", "''")
    )


def _page_where(page_type: str = "", tags: Any = None) -> str:
    clauses: list[str] = []
    if page_type:
        clauses.append(f"page_type = '{page_index.sql_escape(page_type)}'")
    for tag in _split_terms(tags):
        # tags_text 形如 "|flood|urban|"，用竖线边界避免前缀误匹配。
        clauses.append(
            "tags_text LIKE '%|" + _like_escape(tag) + "|%' ESCAPE '\\'"
        )
    return " AND ".join(clauses)


def _make_snippet(
    text: str, query: str, max_chars: int = DEFAULT_SNIPPET_CHARS
) -> str:
    if not text:
        return ""
    cleaned = _WHITESPACE_RE.sub(" ", str(text)).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    needle = (query or "").strip()
    index = cleaned.casefold().find(needle.casefold()) if needle else -1
    if index < 0:
        return cleaned[:max_chars].rstrip() + "…"
    start = max(0, index - max_chars // 3)
    end = min(len(cleaned), start + max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(cleaned) else ""
    return prefix + cleaned[start:end].strip() + suffix


def _row_score(row: Mapping[str, Any]) -> float | None:
    if "_relevance_score" in row and row["_relevance_score"] is not None:
        return round(float(row["_relevance_score"]), 6)
    if "_distance" in row and row["_distance"] is not None:
        # 余弦/向量距离只做相似度展示，不是概率。
        return round(1.0 - float(row["_distance"]), 6)
    if "_score" in row and row["_score"] is not None:
        return round(float(row["_score"]), 6)
    return None


def _row_to_page_result(
    row: Mapping[str, Any], query: str, score_source: str
) -> dict[str, Any] | None:
    page_id = str(row.get("page_id") or "").strip()
    if not page_id:
        return None
    text = str(row.get("text") or "")
    title = str(row.get("title") or "")
    summary = str(row.get("summary") or "")
    snippet_source = text or summary or title
    return {
        "page_id": page_id,
        "slug": str(row.get("slug") or ""),
        "title": title,
        "page_type": str(row.get("page_type") or ""),
        "revision_id": str(row.get("revision_id") or ""),
        "snippet": _make_snippet(snippet_source, query),
        "score": _row_score(row),
        "score_source": score_source,
    }


# =============================================================
# 页面检索
# =============================================================

def _search_pages_impl(
    query: str,
    limit: int = 10,
    page_type: str = "",
    tags: Any = None,
    *,
    config: WikiConfig | None = None,
    db_path: str | None = None,
    use_reranker: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not isinstance(query, str) or not query.strip():
        return [], ["query 为空"]
    resolved_config = _resolve_config(config)
    limit = _normalize_limit(limit)

    try:
        table = page_index.get_table(resolved_config, db_path, create=False)
    except Exception as exc:
        return [], [f"打开页面索引失败: {exc}"]
    if table is None:
        return [], [f"页面索引表不存在: {resolved_config.page_index_table}"]
    try:
        if len(table) == 0:
            return [], []
    except Exception as exc:
        return [], [f"读取页面索引行数失败: {exc}"]

    where = _page_where(page_type, tags)
    score_source = "cross_encoder" if use_reranker else "rrf"
    tracker: _TrackingReranker | None = None
    try:
        builder = table.search(query, query_type="hybrid")
        if use_reranker:
            tracker = _TrackingReranker()
            builder = builder.rerank(tracker)
        else:
            builder = builder.rerank(RRFReranker())
        if where:
            builder = builder.where(where, prefilter=True)
        rows = builder.limit(limit).to_list()
        if tracker is not None and tracker.used_fallback:
            score_source = "rrf"
            warnings.append(
                "本地 reranker 不可用，已回退 RRF: " + tracker.fallback_reason
            )
    except Exception as exc:
        warnings.append(f"hybrid 检索失败，回退向量检索: {exc}")
        try:
            builder = table.search(query)
            if where:
                builder = builder.where(where, prefilter=True)
            rows = builder.limit(limit).to_list()
            score_source = "vector_similarity"
        except Exception as vector_exc:
            warnings.append(f"向量检索失败，回退 FTS: {vector_exc}")
            try:
                builder = table.search(query, query_type="fts")
                if where:
                    builder = builder.where(where, prefilter=True)
                rows = builder.limit(limit).to_list()
                score_source = "bm25"
            except Exception as fts_exc:
                warnings.append(f"FTS 检索失败: {fts_exc}")
                return [], warnings

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = _row_to_page_result(row, query, score_source)
        if item is None or item["page_id"] in seen:
            continue
        seen.add(item["page_id"])
        results.append(item)
        if len(results) >= limit:
            break
    return results, warnings


def search_pages(
    query: str,
    limit: int = 10,
    page_type: str = "",
    tags: Any = None,
    *,
    config: WikiConfig | None = None,
    db_path: str | None = None,
    use_reranker: bool = True,
) -> list[dict[str, Any]]:
    """检索 published 页面，返回带 score_source 的结果列表（空表返回 []）。"""
    results, _warnings = _search_pages_impl(
        query,
        limit=limit,
        page_type=page_type,
        tags=tags,
        config=config,
        db_path=db_path,
        use_reranker=use_reranker,
    )
    return results


# =============================================================
# 证据检索
# =============================================================

def search_evidence(
    query: str,
    limit: int = 5,
    source_filter: str = "",
    category_filter: str = "",
    search_mode: str = "hybrid",
    *,
    use_reranker: bool = True,
) -> dict[str, Any]:
    """直接调用 ``kb.search.search_structured``，返回原始结果 + score_source。"""
    if not isinstance(query, str) or not query.strip():
        return {
            "results": [],
            "trace": {"mode": search_mode, "limit": limit, "warning": "query 为空"},
            "score_source": "rag",
            "warnings": ["query 为空"],
        }
    try:
        result = kb_search.search_structured(
            query,
            limit=limit,
            use_reranker=use_reranker,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
        )
    except Exception as exc:
        return {
            "results": [],
            "trace": {
                "mode": search_mode,
                "limit": limit,
                "reranker": "none",
                "warning": str(exc),
                "result_count": 0,
            },
            "score_source": "rag",
            "warnings": [str(exc)],
        }
    output = dict(result)
    output["score_source"] = "rag"
    return output


# =============================================================
# 图谱邻域（related）
# =============================================================

def _resolve_seed_page(conn: sqlite3.Connection, query: str) -> PageRecord | None:
    page = pages_repo.get_page(conn, query)
    if page is None:
        page = pages_repo.get_page_by_slug(conn, query)
    if page is not None:
        return page
    row = conn.execute(
        "SELECT * FROM pages WHERE title = ? COLLATE NOCASE LIMIT 1", (query,)
    ).fetchone()
    return PageRecord.model_validate(dict(row)) if row is not None else None


def _page_item(page: PageRecord, kind: str, **extra: Any) -> dict[str, Any]:
    item = {
        "source": "wiki_page",
        "kind": kind,
        "page_id": page.page_id,
        "slug": page.slug,
        "title": page.title,
        "page_type": str(page.page_type),
        "status": str(page.status),
        "revision_id": page.current_revision_id or "",
        "score": None,
        "score_source": "graph",
    }
    item.update(extra)
    return item


def _entity_item(related: relations_repo.RelatedEntity) -> dict[str, Any]:
    entity = related.entity
    return {
        "source": "entity",
        "kind": "relation",
        "entity_id": entity.entity_id,
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "status": str(entity.status),
        "confidence": float(entity.confidence),
        "predicate": related.predicate,
        "direction": related.direction,
        "depth": related.depth,
        "via_entity_id": related.via_entity_id,
        "relation_id": related.relation_id,
        "score": None,
        "score_source": "graph",
    }


def _related_items(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        seed = _resolve_seed_page(conn, query)
    except sqlite3.Error as exc:
        return [], [f"查询页面图谱失败: {exc}"]
    if seed is None:
        return [], [f"未找到页面: {query}"]

    page_items: list[dict[str, Any]] = []
    seen_pages: set[str] = set()

    def add_page(page: PageRecord | None, kind: str, **extra: Any) -> None:
        if page is None or page.page_id == seed.page_id:
            return
        if page.page_id in seen_pages:
            return
        seen_pages.add(page.page_id)
        page_items.append(_page_item(page, kind, **extra))

    try:
        for link in link_index.outgoing_links(conn, seed.page_id):
            target = (
                pages_repo.get_page(conn, link.target_page_id)
                if link.target_page_id
                else pages_repo.get_page_by_slug(conn, link.target_slug)
            )
            if target is not None:
                add_page(
                    target,
                    "outgoing_link",
                    target_slug=link.target_slug,
                    display_text=link.display_text,
                )
        for link in link_index.backlinks(conn, seed.page_id):
            add_page(
                pages_repo.get_page(conn, link.source_page_id),
                "backlink",
                target_slug=link.target_slug,
                display_text=link.display_text,
            )
        for page_entity in entities_repo.get_page_entities(conn, seed.page_id):
            for link in entities_repo.get_entity_pages(
                conn, page_entity.entity_id
            ):
                add_page(
                    pages_repo.get_page(conn, link.page_id),
                    "shared_entity",
                    entity_id=page_entity.entity_id,
                )
    except sqlite3.Error as exc:
        warnings.append(f"查询页面链接/实体失败: {exc}")

    entity_items: list[dict[str, Any]] = []
    try:
        related_entities = relations_repo.get_related(
            conn, page_id=seed.page_id, depth=1, limit=limit
        )
        entity_items = [_entity_item(item) for item in related_entities]
    except (sqlite3.Error, LookupError, ValueError) as exc:
        warnings.append(f"查询关系邻域失败: {exc}")

    related = page_items[:limit] + entity_items[:limit]
    return related, warnings


# =============================================================
# 统一入口
# =============================================================

def search_wiki(
    query: str,
    mode: str = "page",
    limit: int = 10,
    *,
    page_type: str = "",
    tags: Any = None,
    source_filter: str = "",
    category_filter: str = "",
    search_mode: str = "hybrid",
    conn: sqlite3.Connection | None = None,
    config: WikiConfig | None = None,
    db_path: str | None = None,
    use_reranker: bool = True,
) -> dict[str, Any]:
    """统一检索入口，返回 ``{query, mode, pages, evidence, related, warnings}``。

    - ``page``：仅页面索引；
    - ``evidence``：仅知识库证据；
    - ``hybrid``：页面 + 证据，分别标记 source=wiki_page/evidence；
    - ``related``：从 pages/page_links/relations 查 depth=1 邻域，需要 ``conn``。
    """
    normalized_mode = (mode or "page").strip().lower()
    if normalized_mode not in ("page", "evidence", "hybrid", "related"):
        raise ValueError(
            "mode 必须是 page/evidence/hybrid/related 之一"
            f"（当前值: {mode!r}）"
        )

    output: dict[str, Any] = {
        "query": query if isinstance(query, str) else "",
        "mode": normalized_mode,
        "pages": [],
        "evidence": [],
        "related": [],
        "warnings": [],
    }
    if not isinstance(query, str) or not query.strip():
        output["warnings"].append("query 为空")
        return output

    resolved_config = _resolve_config(config)
    limit = _normalize_limit(limit)

    if normalized_mode in ("page", "hybrid"):
        page_results, page_warnings = _search_pages_impl(
            query,
            limit=limit,
            page_type=page_type,
            tags=tags,
            config=resolved_config,
            db_path=db_path,
            use_reranker=use_reranker,
        )
        output["pages"] = [
            {**item, "source": "wiki_page"} for item in page_results
        ]
        output["warnings"].extend(page_warnings)

    if normalized_mode in ("evidence", "hybrid"):
        evidence_result = search_evidence(
            query,
            limit=limit,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
            use_reranker=use_reranker,
        )
        output["evidence"] = [
            {**item, "source": "evidence", "score_source": "rag"}
            for item in evidence_result.get("results", [])
        ]
        output["warnings"].extend(evidence_result.get("warnings", []))
        trace_warning = evidence_result.get("trace", {}).get("warning")
        if trace_warning and trace_warning not in output["warnings"]:
            output["warnings"].append(trace_warning)

    if normalized_mode == "related":
        if conn is None:
            output["warnings"].append(
                "related 模式需要 SQLite 连接（conn），已返回空邻域"
            )
        else:
            related, related_warnings = _related_items(conn, query, limit)
            output["related"] = related
            output["warnings"].extend(related_warnings)

    return output


__all__ = [
    "MAX_LIMIT",
    "search_evidence",
    "search_pages",
    "search_wiki",
]
