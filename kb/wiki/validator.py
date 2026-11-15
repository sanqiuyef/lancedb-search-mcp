# -*- coding: utf-8 -*-
"""Wiki 一致性校验：只报告，不自动修复，不写文件/DB。

覆盖：
- frontmatter 完整性与元数据一致性；
- pages.current_revision_id 归属；
- 当前 revision 的 content_sha256 与磁盘文件 hash；
- 断链（page_links.target_page_id IS NULL）；
- fact claim 缺证据（只读 claims / claim_evidence）；
- relation 自环与谓词白名单；
- documents.status='missing' 的来源；
- 页面文件缺失与页面目录中的孤儿 Markdown。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any

from .config import WikiConfig, WikiPathError, ensure_within
from .link_index import broken_links
from .markdown import (
    FrontmatterError,
    FrontmatterValidationError,
    parse_frontmatter,
    validate_page_frontmatter,
)
from .repositories import pages as pages_repo
from .repositories.relations import PREDICATES

_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def _issue(
    level: str, code: str, message: str, page_id: str | None = None
) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "message": message,
        "page_id": page_id,
    }


def _page_path(config: WikiConfig, page: Any) -> str:
    relative = page.relative_path.replace("\\", "/")
    if os.path.isabs(relative):
        raise WikiPathError(f"页面 relative_path 不能是绝对路径: {relative}")
    candidate = os.path.join(config.vault_path, *relative.split("/"))
    ensure_within(config.vault_path, candidate)
    return ensure_within(config.page_root, candidate)


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _check_pages(
    conn: sqlite3.Connection, config: WikiConfig
) -> tuple[list[dict[str, Any]], set[str]]:
    issues: list[dict[str, Any]] = []
    known_paths: set[str] = set()
    for page in pages_repo.list_pages(conn, limit=None, order="created"):
        try:
            path = _page_path(config, page)
        except WikiPathError as exc:
            issues.append(
                _issue("error", "page_path_invalid", str(exc), page.page_id)
            )
            continue
        known_paths.add(os.path.normcase(os.path.normpath(path)))

        disk_hash: str | None = None
        if not os.path.isfile(path):
            issues.append(
                _issue(
                    "error",
                    "page_file_missing",
                    f"页面文件缺失: {page.relative_path}",
                    page.page_id,
                )
            )
        else:
            try:
                with open(path, "rb") as handle:
                    raw = handle.read()
                disk_hash = hashlib.sha256(raw).hexdigest()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                issues.append(
                    _issue(
                        "error",
                        "page_file_unreadable",
                        f"页面文件无法读取: {page.relative_path}: {exc}",
                        page.page_id,
                    )
                )
                text = None
            if text is not None:
                try:
                    frontmatter, _body = parse_frontmatter(text)
                    frontmatter = validate_page_frontmatter(frontmatter)
                except (FrontmatterError, FrontmatterValidationError) as exc:
                    issues.append(
                        _issue(
                            "error",
                            "frontmatter_invalid",
                            f"{page.relative_path}: {exc}",
                            page.page_id,
                        )
                    )
                else:
                    mismatches: list[str] = []
                    if frontmatter.get("page_id") != page.page_id:
                        mismatches.append("page_id")
                    if frontmatter.get("slug") != page.slug:
                        mismatches.append("slug")
                    if frontmatter.get("page_type") != str(page.page_type):
                        mismatches.append("page_type")
                    if frontmatter.get("status") != str(page.status):
                        mismatches.append("status")
                    if mismatches:
                        issues.append(
                            _issue(
                                "error",
                                "frontmatter_metadata_mismatch",
                                f"{page.relative_path}: frontmatter 与库不一致: "
                                f"{', '.join(mismatches)}",
                                page.page_id,
                            )
                        )
                    if frontmatter.get("revision_id") != (
                        page.current_revision_id or ""
                    ):
                        issues.append(
                            _issue(
                                "error",
                                "revision_id_mismatch",
                                f"{page.relative_path}: frontmatter revision_id="
                                f"{frontmatter.get('revision_id')!r} 与 current="
                                f"{page.current_revision_id!r} 不一致",
                                page.page_id,
                            )
                        )

        current = (
            pages_repo.get_page_revision(conn, page.current_revision_id)
            if page.current_revision_id
            else None
        )
        if not page.current_revision_id:
            issues.append(
                _issue(
                    "error",
                    "current_revision_missing",
                    f"页面 {page.slug} 没有 current_revision_id",
                    page.page_id,
                )
            )
        elif current is None or current.page_id != page.page_id:
            issues.append(
                _issue(
                    "error",
                    "current_revision_invalid",
                    f"页面 {page.slug} 的 current_revision_id="
                    f"{page.current_revision_id} 不属于该页或不存在",
                    page.page_id,
                )
            )
        elif disk_hash is not None and disk_hash != current.content_sha256:
            issues.append(
                _issue(
                    "error",
                    "content_hash_mismatch",
                    f"页面 {page.slug} 磁盘 hash 与当前 revision 不一致",
                    page.page_id,
                )
            )

    return issues, known_paths


def _check_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for link in broken_links(conn):
        issues.append(
            _issue(
                "warning",
                "broken_link",
                f"断链: {link.source_page_id} -> [[{link.target_slug}]]",
                link.source_page_id,
            )
        )
    return issues


def _check_claims(
    conn: sqlite3.Connection, config: WikiConfig
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    level = "error" if config.require_evidence else "warning"
    rows = conn.execute(
        "SELECT c.claim_id, c.page_id, c.claim_text FROM claims AS c "
        "LEFT JOIN claim_evidence AS e ON e.claim_id = c.claim_id "
        "WHERE c.claim_type = 'fact' AND e.claim_id IS NULL "
        "ORDER BY c.claim_id ASC"
    ).fetchall()
    for row in rows:
        issues.append(
            _issue(
                level,
                "fact_claim_missing_evidence",
                f"fact claim {row['claim_id']} 缺少证据: "
                f"{_short(row['claim_text'])}",
                row["page_id"],
            )
        )

    claim_rows = conn.execute(
        "SELECT c.claim_id, c.page_id, c.document_id, d.status AS document_status "
        "FROM claims AS c LEFT JOIN documents AS d "
        "ON d.document_id = c.document_id "
        "WHERE c.document_id IS NOT NULL AND d.status = 'missing' "
        "ORDER BY c.claim_id ASC"
    ).fetchall()
    for row in claim_rows:
        issues.append(
            _issue(
                "warning",
                "claim_source_missing",
                f"claim {row['claim_id']} 引用的来源 {row['document_id']} "
                "状态为 missing",
                row["page_id"],
            )
        )
    return issues


def _check_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT document_id, source_type, canonical_uri, display_name "
        "FROM documents WHERE status = 'missing' "
        "ORDER BY document_id ASC"
    ).fetchall()
    for row in rows:
        name = row["display_name"] or row["canonical_uri"]
        issues.append(
            _issue(
                "warning",
                "source_missing",
                f"来源缺失: {row['document_id']} ({row['source_type']}: {name})",
            )
        )
    return issues


def _check_relations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT relation_id, subject_entity_id, predicate, object_entity_id "
        "FROM relations ORDER BY relation_id ASC"
    ).fetchall()
    for row in rows:
        if row["subject_entity_id"] == row["object_entity_id"]:
            issues.append(
                _issue(
                    "error",
                    "relation_self_loop",
                    f"relation {row['relation_id']} 是自环: "
                    f"{row['subject_entity_id']} -{row['predicate']}-> "
                    f"{row['object_entity_id']}",
                )
            )
        if row["predicate"] not in PREDICATES:
            issues.append(
                _issue(
                    "error",
                    "relation_invalid_predicate",
                    f"relation {row['relation_id']} 谓词不在白名单: "
                    f"{row['predicate']!r}",
                )
            )
    return issues


def _check_orphans(
    config: WikiConfig, known_paths: set[str]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for page_type in ("concept", "entity", "topic"):
        directory = config.page_type_dir(page_type)
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory, followlinks=False):
            for filename in files:
                if not filename.lower().endswith(".md"):
                    continue
                path = os.path.join(root, filename)
                if os.path.normcase(os.path.normpath(path)) in known_paths:
                    continue
                relative = os.path.relpath(path, config.vault_path).replace(
                    os.sep, "/"
                )
                issues.append(
                    _issue(
                        "warning",
                        "orphan_page_file",
                        f"孤儿页面文件（pages 表无记录）: {relative}",
                    )
                )
    return issues


def validate(
    conn: sqlite3.Connection, config: WikiConfig
) -> list[dict[str, Any]]:
    """返回按 level/code/page_id 排序的问题列表；不修改任何数据。"""
    page_issues, known_paths = _check_pages(conn, config)
    issues: list[dict[str, Any]] = []
    issues.extend(page_issues)
    issues.extend(_check_links(conn))
    issues.extend(_check_claims(conn, config))
    issues.extend(_check_documents(conn))
    issues.extend(_check_relations(conn))
    issues.extend(_check_orphans(config, known_paths))
    issues.sort(
        key=lambda item: (
            _LEVEL_ORDER.get(item["level"], 99),
            item["code"],
            item["page_id"] or "",
            item["message"],
        )
    )
    return issues


__all__ = ["validate"]
