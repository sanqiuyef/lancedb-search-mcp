# -*- coding: utf-8 -*-
"""Wiki 页面向量索引（LanceDB）。

设计要点：
- 索引表沿用主知识库的 LanceDB 库路径（``kb.config.resolve_db_path()``），
  表名取 ``WikiConfig.page_index_table``（默认 ``wiki_pages``），与 ``my_docs``
  互不影响；
- ``WikiPage`` 用官方 ``LanceModel`` 定义，``text`` 为 SourceField、``vector``
  为 VectorField，写入与查询时由当前激活的 embedding function 自动向量化；
- 只索引 ``status=published`` 的页面；写入采用「先按 page_id 删除再 add」，
  重复索引同一页面不会产生重复行；
- FTS 索引固定名 ``wiki_page_text_idx``（jieba/default），向量索引固定名
  ``wiki_page_vector_idx``（行数 >= 1000 时才建 IVF_PQ cosine）；
- 索引失败向上抛出，由调用方（Phase 5 service）决定是否记 ``index_pending``。

本模块不加载真实 embedding 模型：``get_page_model()`` 只读取 ndims()，
测试可注入假 embedding；真正加载发生在 LanceDB 自动向量化时。
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any

import lancedb
from lancedb.pydantic import LanceModel, Vector

from .. import config as kb_config
from .. import embeddings as kb_embeddings
from .config import WikiConfig
from .models import PageRecord
from .repositories import pages as pages_repo

PAGE_TEXT_INDEX = "wiki_page_text_idx"
PAGE_VECTOR_INDEX = "wiki_page_vector_idx"
PAGE_VECTOR_INDEX_MIN_ROWS = 1000


class PageIndexError(RuntimeError):
    """页面索引操作失败。"""


class PageNotPublishedError(PageIndexError, ValueError):
    """试图索引非 published 页面。"""


_db = None
_db_path = ""
_page_model = None
_model_lock = threading.Lock()
_write_lock = threading.RLock()


# =============================================================
# 连接与模型
# =============================================================

def reset_db() -> None:
    """测试/切库用：清空进程级 LanceDB 连接缓存。"""
    global _db, _db_path
    _db = None
    _db_path = ""


def reset_page_model() -> None:
    """测试用：清空进程级 WikiPage LanceModel 缓存。"""
    global _page_model
    with _model_lock:
        _page_model = None


def resolve_index_db_path(db_path: str | os.PathLike[str] | None = None) -> str:
    """解析页面索引库路径（显式参数 > LANCEDB_DB_PATH > 默认库）。"""
    raw = os.fspath(db_path) if db_path is not None else kb_config.resolve_db_path()
    if raw == ":memory:" or raw.startswith("memory:"):
        return raw
    return os.path.abspath(raw)


def _connect(path: str):
    global _db, _db_path
    if _db is not None and _db_path == path:
        return _db
    if path != ":memory:" and not path.startswith("memory:"):
        os.makedirs(path, exist_ok=True)
    _db = lancedb.connect(path)
    _db_path = path
    return _db


def get_db(db_path: str | os.PathLike[str] | None = None, *, create: bool = True):
    """返回 LanceDB 连接；``create=False`` 时库目录不存在则返回 None。"""
    path = resolve_index_db_path(db_path)
    if not create and path != ":memory:" and not path.startswith("memory:"):
        if not os.path.isdir(path):
            return None
    return _connect(path)


def get_page_model():
    """按当前激活 embedding function 构建 WikiPage LanceModel（进程内缓存）。"""
    global _page_model
    if _page_model is not None:
        return _page_model
    with _model_lock:
        if _page_model is not None:
            return _page_model
        func = kb_embeddings.get_embedding_function()
        dims = int(func.ndims())

        class WikiPage(LanceModel):
            page_id: str
            revision_id: str
            title: str
            text: str = func.SourceField()
            slug: str = ""
            aliases_text: str = ""
            summary: str = ""
            tags_text: str = ""
            page_type: str = ""
            status: str = ""
            updated_at: str = ""
            vector: Vector(dims) = func.VectorField()

        _page_model = WikiPage
        return WikiPage


def table_name(config: WikiConfig | None = None) -> str:
    resolved = _resolve_config(config)
    return resolved.page_index_table


def get_table(
    config: WikiConfig | None = None,
    db_path: str | os.PathLike[str] | None = None,
    *,
    create: bool = False,
):
    """打开页面索引表；不存在且 ``create=False`` 时返回 None。"""
    name = table_name(config)
    db = get_db(db_path, create=create)
    if db is None:
        return None
    try:
        existing = set(db.list_tables().tables)
    except Exception as exc:  # pragma: no cover - 损坏的库
        raise PageIndexError(f"无法列出 LanceDB 表: {exc}") from exc
    if name in existing:
        return db.open_table(name)
    if not create:
        return None
    return db.create_table(name, schema=get_page_model(), mode="create")


def _resolve_config(config: WikiConfig | None) -> WikiConfig:
    if config is not None:
        if not isinstance(config, WikiConfig):
            raise TypeError("config 必须是 WikiConfig")
        return config
    return WikiConfig.from_env()


# =============================================================
# SQL 转义与输入规整
# =============================================================

def sql_escape(value: Any) -> str:
    """LanceDB SQL 字符串字面量转义（单引号双写）。"""
    return str(value).replace("'", "''")


def _eq(column: str, value: Any) -> str:
    return f"{column} = '{sql_escape(value)}'"


def _string_list(name: str, value: Any) -> list[str]:
    """把 aliases/tags 规整为非空字符串列表；字符串按逗号/顿号/分号拆分。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,，;；、]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise TypeError(f"{name} 必须是字符串或字符串列表")
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"{name} 的元素必须是字符串")
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return result


def format_tags_text(tags: list[str]) -> str:
    """tags_text 用 ``|`` 包裹，便于检索时做精确 token 匹配。"""
    if not tags:
        return ""
    return "|" + "|".join(tags) + "|"


def _coerce_page(page: Any) -> PageRecord:
    if isinstance(page, PageRecord):
        return page
    page_record = getattr(page, "page", None)
    if isinstance(page_record, PageRecord):
        return page_record
    if isinstance(page, Mapping):
        try:
            return PageRecord.model_validate(dict(page))
        except Exception as exc:
            raise TypeError(f"page 映射不是合法 PageRecord: {exc}") from exc
    raise TypeError("page 必须是 PageRecord / PageView / 映射")


def _resolve_summary(page: Any, summary: str) -> str:
    if summary:
        return str(summary)
    frontmatter = getattr(page, "frontmatter", None)
    if isinstance(frontmatter, Mapping):
        value = frontmatter.get("summary")
        if value is not None:
            return str(value)
    if isinstance(page, Mapping):
        value = page.get("summary")
        if value is not None:
            return str(value)
    return str(getattr(page, "summary", "") or "")


# =============================================================
# 索引维护
# =============================================================

def _index_names(table) -> set[str]:
    try:
        return {str(index.name) for index in table.list_indices()}
    except Exception as exc:  # pragma: no cover - 损坏的索引
        raise PageIndexError(f"无法读取索引列表: {exc}") from exc


def ensure_text_index(table) -> str:
    """确保 FTS 索引存在（固定名，jieba/default）；返回动作说明。"""
    if PAGE_TEXT_INDEX in _index_names(table):
        return "FTS 索引已存在"
    try:
        table.create_fts_index(
            "text",
            base_tokenizer=kb_config.FTS_BASE_TOKENIZER,
            replace=True,
            name=PAGE_TEXT_INDEX,
        )
    except Exception as exc:
        raise PageIndexError(
            f"创建 FTS 索引失败（tokenizer={kb_config.FTS_BASE_TOKENIZER}）: {exc}"
        ) from exc
    return f"FTS 索引已创建（tokenizer={kb_config.FTS_BASE_TOKENIZER}）"


def ensure_vector_index(table) -> str:
    """行数达到阈值时创建 IVF_PQ cosine 向量索引（固定名）。"""
    rows = len(table)
    if rows < PAGE_VECTOR_INDEX_MIN_ROWS:
        return f"向量索引未创建（{rows} 行 < {PAGE_VECTOR_INDEX_MIN_ROWS}）"
    if PAGE_VECTOR_INDEX in _index_names(table):
        return "向量索引已存在"
    partitions = min(max(int(math.sqrt(rows)), 16), 256)
    try:
        table.create_index(
            index_type="IVF_PQ",
            metric="cosine",
            num_partitions=partitions,
            num_sub_vectors=16,
            replace=True,
            name=PAGE_VECTOR_INDEX,
        )
    except Exception as exc:
        raise PageIndexError(f"创建向量索引失败: {exc}") from exc
    return f"IVF_PQ 向量索引已创建（partitions={partitions}）"


def index_page(
    page: Any,
    body: str,
    aliases: Any = None,
    tags: Any = None,
    config: WikiConfig | None = None,
    *,
    summary: str = "",
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """索引一个 published 页面（先删同 page_id 行，再 add）。

    非 published 页面抛 ``PageNotPublishedError``；索引/建索引失败抛
    ``PageIndexError``。返回写入摘要，供调用方记录 journal/统计。
    """
    if not isinstance(body, str):
        raise TypeError("body 必须是字符串")
    resolved_config = _resolve_config(config)
    record = _coerce_page(page)
    status = str(getattr(record, "status", "") or "").strip().lower()
    if status != "published":
        raise PageNotPublishedError(
            f"只索引 published 页面，当前 page_id={record.page_id!r} status={status!r}"
        )

    alias_list = _string_list("aliases", aliases)
    tag_list = _string_list("tags", tags)
    row = {
        "page_id": record.page_id,
        "revision_id": record.current_revision_id or "",
        "title": record.title,
        "text": body,
        "slug": record.slug,
        "aliases_text": " ".join(alias_list),
        "summary": _resolve_summary(page, summary),
        "tags_text": format_tags_text(tag_list),
        "page_type": str(record.page_type),
        "status": status,
        "updated_at": record.updated_at,
    }

    with _write_lock:
        table = get_table(resolved_config, db_path, create=True)
        assert table is not None
        try:
            table.delete(_eq("page_id", record.page_id))
            table.add([row])
            text_action = ensure_text_index(table)
            vector_action = ensure_vector_index(table)
        except PageIndexError:
            raise
        except Exception as exc:
            raise PageIndexError(
                f"索引页面失败 page_id={record.page_id!r}: {exc}"
            ) from exc
        return {
            "page_id": record.page_id,
            "revision_id": row["revision_id"],
            "table": resolved_config.page_index_table,
            "row_count": len(table),
            "text_index": text_action,
            "vector_index": vector_action,
        }


def remove_page(
    page_id: str,
    config: WikiConfig | None = None,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """删除页面的索引行；表或行不存在时返回 0。"""
    if not isinstance(page_id, str) or not page_id.strip():
        raise ValueError("page_id 必须是非空字符串")
    resolved_config = _resolve_config(config)
    with _write_lock:
        table = get_table(resolved_config, db_path, create=False)
        if table is None:
            return 0
        try:
            result = table.delete(_eq("page_id", page_id))
        except Exception as exc:
            raise PageIndexError(f"删除页面索引失败 page_id={page_id!r}: {exc}") from exc
        deleted = getattr(result, "num_deleted_rows", None)
        return int(deleted) if deleted is not None else 0


def drop_page_index(
    config: WikiConfig | None = None,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """显式删除整个页面索引表；返回是否确实删除。"""
    resolved_config = _resolve_config(config)
    name = resolved_config.page_index_table
    with _write_lock:
        db = get_db(db_path, create=False)
        if db is None:
            return False
        if name not in set(db.list_tables().tables):
            return False
        db.drop_table(name)
        return True


def _indexed_revision_map(table) -> dict[str, str]:
    if table is None or len(table) == 0:
        return {}
    arrow = table.to_lance().to_table(columns=["page_id", "revision_id"])
    return {
        str(page_id): str(revision or "")
        for page_id, revision in zip(
            arrow.column("page_id").to_pylist(),
            arrow.column("revision_id").to_pylist(),
        )
    }


def _indexed_page_ids(table) -> list[str]:
    if table is None or len(table) == 0:
        return []
    arrow = table.to_lance().to_table(columns=["page_id"])
    return [str(value) for value in arrow.column("page_id").to_pylist()]


def index_status(
    conn: sqlite3.Connection | None = None,
    config: WikiConfig | None = None,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """返回索引状态：表是否存在、行数、published 数、缺失/多余 page_id。"""
    resolved_config = _resolve_config(config)
    name = resolved_config.page_index_table
    path = resolve_index_db_path(db_path)
    try:
        table = get_table(resolved_config, db_path, create=False)
    except Exception as exc:
        return {
            "table": name,
            "db_path": path,
            "exists": False,
            "row_count": 0,
            "published_count": None,
            "missing_page_ids": [],
            "stale_page_ids": [],
            "error": f"打开页面索引失败: {exc}",
        }
    indexed_ids = _indexed_page_ids(table)

    published_ids: list[str] | None = None
    missing: list[str] = []
    stale: list[str] = []
    if conn is not None:
        try:
            published = pages_repo.list_pages(conn, status="published", limit=None)
            published_ids = [page.page_id for page in published]
        except sqlite3.Error as exc:
            return {
                "table": name,
                "db_path": path,
                "exists": table is not None,
                "row_count": len(table) if table is not None else 0,
                "published_count": None,
                "missing_page_ids": [],
                "stale_page_ids": [],
                "error": f"读取 wiki SQLite 失败: {exc}",
            }
        indexed_set = set(indexed_ids)
        published_set = set(published_ids)
        missing = sorted(published_set - indexed_set)
        stale = sorted(indexed_set - published_set)
    else:
        return {
            "table": name,
            "db_path": path,
            "exists": table is not None,
            "row_count": len(table) if table is not None else 0,
            "published_count": None,
            "missing_page_ids": [],
            "stale_page_ids": [],
        }

    return {
        "table": name,
        "db_path": path,
        "exists": table is not None,
        "row_count": len(table) if table is not None else 0,
        "published_count": len(published_ids),
        "missing_page_ids": missing,
        "stale_page_ids": stale,
    }


def _read_page_document(config: WikiConfig, conn: sqlite3.Connection, page: PageRecord):
    """用 PageStore 读取页面正文与 frontmatter，返回 (body, aliases, tags, summary)。"""
    from .page_store import PageStore

    store = PageStore(config, conn)
    view = store.read_page(page.page_id)
    if view is None:
        raise PageIndexError(f"页面读取失败（元数据不存在）: {page.page_id}")
    frontmatter = view.frontmatter
    aliases = frontmatter.get("aliases") or []
    tags = frontmatter.get("tags") or []
    summary = frontmatter.get("summary") or ""
    return view.body, aliases, tags, summary


def rebuild_all(
    conn: sqlite3.Connection,
    config: WikiConfig,
    *,
    db_path: str | os.PathLike[str] | None = None,
    force: bool = False,
    prune_stale: bool = True,
) -> dict[str, Any]:
    """重建全部 published 页面索引；单页失败记录后继续。

    - 默认增量：索引中 revision_id 与 pages.current_revision_id 一致的页面跳过；
    - ``prune_stale=True`` 时删除索引里已不属于任何页面（或已非 published）的行；
    - 返回 ``{table,total_published,indexed,skipped,failed,failures,removed_stale,row_count}``。
    """
    if conn is None:
        raise TypeError("conn 不能为 None")
    resolved_config = _resolve_config(config)
    table = get_table(resolved_config, db_path, create=False)
    indexed = _indexed_revision_map(table)
    published = pages_repo.list_pages(conn, status="published", limit=None, order="created")

    indexed_count = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    for page in published:
        current_revision = page.current_revision_id or ""
        if not force and indexed.get(page.page_id, None) == current_revision:
            skipped += 1
            continue
        try:
            body, aliases, tags, summary = _read_page_document(
                resolved_config, conn, page
            )
            index_page(
                page,
                body,
                aliases,
                tags,
                resolved_config,
                summary=summary,
                db_path=db_path,
            )
            indexed_count += 1
        except Exception as exc:
            failures.append(
                {
                    "page_id": page.page_id,
                    "slug": page.slug,
                    "relative_path": page.relative_path,
                    "error": str(exc),
                }
            )

    removed_stale = 0
    if prune_stale:
        published_set = {page.page_id for page in published}
        for page_id in sorted(set(indexed) - published_set):
            try:
                remove_page(page_id, resolved_config, db_path=db_path)
                removed_stale += 1
            except Exception as exc:
                failures.append(
                    {
                        "page_id": page_id,
                        "slug": "",
                        "relative_path": "",
                        "error": f"清理陈旧索引失败: {exc}",
                    }
                )

    final_table = get_table(resolved_config, db_path, create=False)
    return {
        "table": resolved_config.page_index_table,
        "total_published": len(published),
        "indexed": indexed_count,
        "skipped": skipped,
        "failed": len(failures),
        "failures": failures,
        "removed_stale": removed_stale,
        "row_count": len(final_table) if final_table is not None else 0,
    }


__all__ = [
    "PAGE_TEXT_INDEX",
    "PAGE_VECTOR_INDEX",
    "PAGE_VECTOR_INDEX_MIN_ROWS",
    "PageIndexError",
    "PageNotPublishedError",
    "drop_page_index",
    "ensure_text_index",
    "ensure_vector_index",
    "format_tags_text",
    "get_db",
    "get_page_model",
    "get_table",
    "index_page",
    "index_status",
    "rebuild_all",
    "remove_page",
    "reset_db",
    "reset_page_model",
    "resolve_index_db_path",
    "sql_escape",
    "table_name",
]
