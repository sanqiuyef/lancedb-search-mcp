# -*- coding: utf-8 -*-
"""Wiki 双链索引：出链重建、解析与反链查询。

链接语法为 Obsidian 的 ``[[target]]`` / ``[[target|alias]]``；target 可以带
``#小节`` 锚点。解析顺序：slug → title → 页面 frontmatter aliases（大小写不敏感）。
无法解析的目标保留 ``target_slug``，``target_page_id`` 置 NULL，作为断链报告给
validator，不删除链接（vault 契约允许悬空链接）。
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import WikiConfig, WikiPathError, ensure_within
from .db import transaction
from .markdown import FrontmatterError, parse_frontmatter
from .models import PageLinkRecord, PageRecord
from .repositories import pages as pages_repo

_LINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


class LinkIndexError(RuntimeError):
    """链接索引操作失败。"""


class LinkPageNotFoundError(LinkIndexError, LookupError):
    """链接 source 页面不存在。"""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@contextmanager
def _transactional(conn: sqlite3.Connection) -> Iterator[None]:
    """调用方已在事务中时复用，否则自开事务。"""
    if conn.in_transaction:
        yield
    else:
        with transaction(conn):
            yield


def _page_path(config: WikiConfig, page: PageRecord) -> str:
    relative = page.relative_path.replace("\\", "/")
    candidate = os.path.join(config.vault_path, *relative.split("/"))
    ensure_within(config.vault_path, candidate)
    return ensure_within(config.page_root, candidate)


def _read_page_aliases(config: WikiConfig, page: PageRecord) -> list[str]:
    path = _page_path(config, page)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        data, _ = parse_frontmatter(text)
    except (OSError, UnicodeDecodeError, FrontmatterError, WikiPathError):
        return []
    aliases = data.get("aliases")
    if not isinstance(aliases, list):
        return []
    return [
        alias.strip()
        for alias in aliases
        if isinstance(alias, str) and alias.strip()
    ]


class _PageLookup:
    """一次重建内的页面 slug/title/alias 解析表。"""

    def __init__(self, conn: sqlite3.Connection, config: WikiConfig | None) -> None:
        self._slug: dict[str, PageRecord] = {}
        self._title: dict[str, PageRecord] = {}
        self._alias: dict[str, PageRecord] = {}
        pages = pages_repo.list_pages(conn, limit=None, order="created")
        for page in pages:
            self._slug.setdefault(page.slug.casefold(), page)
            self._title.setdefault(page.title.casefold(), page)
        if config is not None:
            for page in pages:
                for alias in _read_page_aliases(config, page):
                    self._alias.setdefault(alias.casefold(), page)

    def resolve(self, target: str) -> PageRecord | None:
        key = target.casefold()
        return self._slug.get(key) or self._title.get(key) or self._alias.get(key)


def iter_wiki_links(body: str) -> list[tuple[str, str]]:
    """返回 ``[(target_without_anchor, display_text), ...]``，保持出现顺序。"""
    if not isinstance(body, str):
        raise TypeError("body 必须是字符串")
    links: list[tuple[str, str]] = []
    for match in _LINK_RE.finditer(body):
        raw = match.group(1)
        target_part, separator, alias_part = raw.partition("|")
        target = target_part.strip()
        if not target:
            continue
        base = target.split("#", 1)[0].strip()
        if not base:
            continue
        display = alias_part.strip() if separator else base
        links.append((base, display or base))
    return links


def delete_page_links(conn: sqlite3.Connection, page_id: str) -> int:
    """删除某页的全部出链。"""
    return conn.execute(
        "DELETE FROM page_links WHERE source_page_id = ?", (page_id,)
    ).rowcount


def _collect_link_records(
    page_id: str,
    body: str,
    lookup: _PageLookup,
    now: str,
) -> list[PageLinkRecord]:
    records: list[PageLinkRecord] = []
    seen: set[str] = set()
    for target_slug, display_text in iter_wiki_links(body):
        key = target_slug.casefold()
        if key in seen:
            continue
        seen.add(key)
        target = lookup.resolve(target_slug)
        records.append(
            PageLinkRecord(
                source_page_id=page_id,
                target_slug=target_slug,
                target_page_id=target.page_id if target is not None else None,
                display_text=display_text,
                created_at=now,
            )
        )
    return records


def _write_page_links(
    conn: sqlite3.Connection, page_id: str, records: list[PageLinkRecord]
) -> None:
    delete_page_links(conn, page_id)
    for record in records:
        conn.execute(
            "INSERT INTO page_links "
            "(source_page_id, target_slug, target_page_id, display_text, "
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (
                record.source_page_id,
                record.target_slug,
                record.target_page_id,
                record.display_text,
                record.created_at,
            ),
        )


def rebuild_page_links(
    conn: sqlite3.Connection,
    page_id: str,
    body: str,
    config: WikiConfig | None = None,
) -> list[PageLinkRecord]:
    """重建某页出链；未解析目标保留 target_slug 与 NULL target_page_id。"""
    if pages_repo.get_page(conn, page_id) is None:
        raise LinkPageNotFoundError(f"页面不存在: {page_id}")
    lookup = _PageLookup(conn, config)
    now = utc_now()
    records = _collect_link_records(page_id, body, lookup, now)

    with _transactional(conn):
        _write_page_links(conn, page_id, records)
    return records


def rebuild_all_page_links(
    conn: sqlite3.Connection,
    config: WikiConfig,
) -> dict[str, Any]:
    """全量重建所有页面的出链并回填 target_page_id。

    解析规则与单页重建一致（slug → title → alias，大小写不敏感）；只有
    ``published`` 页面写入链接，草稿/待审/归档页面的出链会被清空，与
    PageStore 的发布语义保持一致。文件缺失或 frontmatter 非法的页面会记录
    到 ``errors`` 并保留其原有链接（不静默删除）。返回值包含页面数、链接数
    与断链明细。

    所有文件读取与解析在写事务开始前完成，事务内只做 SQL 删除/插入。
    """
    if not isinstance(config, WikiConfig):
        raise TypeError("config 必须是 WikiConfig")
    pages = pages_repo.list_pages(conn, limit=None, order="created")
    lookup = _PageLookup(conn, config)
    now = utc_now()

    stats: dict[str, Any] = {
        "pages": len(pages),
        "published": 0,
        "unpublished": 0,
        "links": 0,
        "broken": 0,
        "broken_links": [],
        "errors": [],
        "missing_files": [],
    }
    prepared: list[tuple[str, list[PageLinkRecord]]] = []
    clear_page_ids: list[str] = []
    for page in pages:
        if str(page.status) != "published":
            clear_page_ids.append(page.page_id)
            stats["unpublished"] += 1
            continue
        path = _page_path(config, page)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            _frontmatter, body = parse_frontmatter(text)
        except FileNotFoundError:
            stats["missing_files"].append(
                {"page_id": page.page_id, "relative_path": page.relative_path}
            )
            stats["errors"].append(
                f"页面文件缺失，保留旧链接: {page.relative_path}"
            )
            continue
        except (OSError, UnicodeDecodeError, FrontmatterError, WikiPathError) as exc:
            stats["errors"].append(
                f"页面不可解析，保留旧链接: {page.relative_path}: {exc}"
            )
            continue
        records = _collect_link_records(page.page_id, body, lookup, now)
        prepared.append((page.page_id, records))
        stats["published"] += 1
        stats["links"] += len(records)
        for record in records:
            if record.target_page_id is None:
                stats["broken"] += 1
                stats["broken_links"].append(
                    {
                        "source_page_id": record.source_page_id,
                        "target_slug": record.target_slug,
                    }
                )

    with _transactional(conn):
        for page_id in clear_page_ids:
            delete_page_links(conn, page_id)
        for page_id, records in prepared:
            _write_page_links(conn, page_id, records)
    return stats


def outgoing_links(
    conn: sqlite3.Connection, page_id: str
) -> list[PageLinkRecord]:
    return [
        PageLinkRecord.model_validate(dict(row))
        for row in conn.execute(
            "SELECT * FROM page_links WHERE source_page_id = ? "
            "ORDER BY target_slug COLLATE NOCASE ASC",
            (page_id,),
        )
    ]


def backlinks(conn: sqlite3.Connection, page_id: str) -> list[PageLinkRecord]:
    return [
        PageLinkRecord.model_validate(dict(row))
        for row in conn.execute(
            "SELECT * FROM page_links WHERE target_page_id = ? "
            "ORDER BY source_page_id ASC, target_slug ASC",
            (page_id,),
        )
    ]


def broken_links(
    conn: sqlite3.Connection, page_id: str | None = None
) -> list[PageLinkRecord]:
    sql = "SELECT * FROM page_links WHERE target_page_id IS NULL"
    params: list[str] = []
    if page_id is not None:
        sql += " AND source_page_id = ?"
        params.append(page_id)
    sql += " ORDER BY source_page_id ASC, target_slug ASC"
    return [
        PageLinkRecord.model_validate(dict(row))
        for row in conn.execute(sql, params)
    ]


__all__ = [
    "LinkIndexError",
    "LinkPageNotFoundError",
    "backlinks",
    "broken_links",
    "delete_page_links",
    "iter_wiki_links",
    "outgoing_links",
    "rebuild_all_page_links",
    "rebuild_page_links",
]
