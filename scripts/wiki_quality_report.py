# -*- coding: utf-8 -*-
"""Wiki 质量报告：从 SQLite + vault 计算可复核的指标并输出 Markdown/JSON。

指标口径（详见 docs/wiki-v1/operations.md 与报告末尾说明）：

- Evidence Coverage：``claim_type`` 属于 fact/factual/definition/result 且状态
  非 rejected 的 claim 中，至少有一条 claim_evidence 的比例。当前 schema 只
  持久化 fact/hypothesis，definition/result 尚未作为独立 claim_type 落库。
- Quote Validity：``claim_evidence`` 无 verification 列，且切片原文不在 SQLite；
  这里按 ``evidence.quote_sha256(quote)`` 与 ``text_sha256`` 比对、位置字段
  合法（char_end >= char_start）做结构校验，不伪造内容级通过率。
- Broken Link Rate：``page_links.target_page_id IS NULL`` / 总链接数。
- Orphan Pages：published 页面中既无入链也无出链的页面。
- Duplicate Page Candidates：标题/别名归一后重复，以及摘要高相似；只报告候选。
- Unindexed Published Pages：published 页面在 ``WIKI_PAGE_INDEX_TABLE`` 缺失；
  索引表不存在时明确输出“索引未建立”。
- Unsourced Relations：proposed/verified 关系既无 evidence 也无 source_page_id。
- Unresolved Aliases：redirect 目标不存在、别名所属实体 merged/deprecated、
  或同一 normalized_alias 被多个 active 实体共享。

关系正确率需要人工抽样，脚本不计算、不编造。

用法：
    python -X utf8 scripts/wiki_quality_report.py
    python -X utf8 scripts/wiki_quality_report.py --json
    python -X utf8 scripts/wiki_quality_report.py --out docs/wiki-v1/quality-report.md
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import sys
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb.wiki import constants as C  # noqa: E402
from kb.wiki import db as wiki_db  # noqa: E402
from kb.wiki.config import WikiConfig, WikiConfigError, WikiPathError, ensure_within  # noqa: E402
from kb.wiki.evidence import normalize_ws, quote_sha256  # noqa: E402
from kb.wiki.markdown import FrontmatterError, parse_frontmatter  # noqa: E402
from kb.wiki.models import PageRecord  # noqa: E402

DEFAULT_SIMILARITY_THRESHOLD = 0.82
LIST_LIMIT = 50
COVERAGE_CLAIM_TYPES = ("fact", "factual", "definition", "result")
ACTIVE_CLAIM_STATUSES = ("proposed", "accepted", "disputed")
ACTIVE_RELATION_STATUSES = ("proposed", "verified")
INACTIVE_ENTITY_STATUSES = ("merged", "deprecated")


class QualityReportError(RuntimeError):
    """质量报告运行环境不满足要求。"""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    quoted = '"' + table.replace('"', '""') + '"'
    return {
        str(row[1]).lower()
        for row in conn.execute(f"PRAGMA table_info({quoted})")
    }


def _limited(items: list[Any], limit: int = LIST_LIMIT) -> tuple[list[Any], bool]:
    return items[:limit], len(items) > limit


def _percent(ratio: float | None) -> str:
    return "N/A" if ratio is None else f"{ratio * 100:.1f}%"


def _normalize_key(text: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(
        char for char in normalized if char.isalnum()
    )


def _page_path(config: WikiConfig, page: PageRecord) -> str | None:
    relative = page.relative_path.replace("\\", "/")
    if os.path.isabs(relative):
        return None
    try:
        candidate = os.path.join(config.vault_path, *relative.split("/"))
        return ensure_within(config.page_root, candidate)
    except WikiPathError:
        return None


def _frontmatter_data(
    config: WikiConfig, pages: list[PageRecord]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    data: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for page in pages:
        path = _page_path(config, page)
        if path is None or not os.path.isfile(path):
            data[page.page_id] = {}
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                frontmatter, _body = parse_frontmatter(handle.read())
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            errors.append(
                {
                    "page_id": page.page_id,
                    "relative_path": page.relative_path,
                    "error": str(exc),
                }
            )
            data[page.page_id] = {}
            continue
        data[page.page_id] = frontmatter
    return data, errors


def evidence_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    if not (
        _table_exists(conn, "claims") and _table_exists(conn, "claim_evidence")
    ):
        return {
            "applicable": 0,
            "covered": 0,
            "ratio": None,
            "missing_claim_ids": [],
            "missing_truncated": False,
            "status": "tables_missing",
            "claim_types": list(COVERAGE_CLAIM_TYPES),
            "status_scope": list(ACTIVE_CLAIM_STATUSES),
        }
    type_placeholders = ", ".join("?" for _ in COVERAGE_CLAIM_TYPES)
    status_placeholders = ", ".join("?" for _ in ACTIVE_CLAIM_STATUSES)
    params = (*COVERAGE_CLAIM_TYPES, *ACTIVE_CLAIM_STATUSES)
    applicable = int(
        conn.execute(
            f"SELECT COUNT(*) FROM claims WHERE claim_type IN "
            f"({type_placeholders}) AND status IN ({status_placeholders})",
            params,
        ).fetchone()[0]
    )
    covered = int(
        conn.execute(
            f"SELECT COUNT(DISTINCT c.claim_id) FROM claims AS c "
            f"JOIN claim_evidence AS e ON e.claim_id = c.claim_id "
            f"WHERE c.claim_type IN ({type_placeholders}) "
            f"AND c.status IN ({status_placeholders})",
            params,
        ).fetchone()[0]
    )
    missing = [
        str(row[0])
        for row in conn.execute(
            f"SELECT c.claim_id FROM claims AS c "
            f"WHERE c.claim_type IN ({type_placeholders}) "
            f"AND c.status IN ({status_placeholders}) "
            f"AND NOT EXISTS (SELECT 1 FROM claim_evidence AS e "
            f"                WHERE e.claim_id = c.claim_id) "
            f"ORDER BY c.claim_id ASC",
            params,
        )
    ]
    ratio = covered / applicable if applicable else None
    missing_sample, truncated = _limited(missing)
    return {
        "applicable": applicable,
        "covered": covered,
        "ratio": ratio,
        "missing_claim_ids": missing_sample,
        "missing_truncated": truncated,
        "claim_types": list(COVERAGE_CLAIM_TYPES),
        "status_scope": list(ACTIVE_CLAIM_STATUSES),
        "status": "ok",
    }


def quote_validity(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "claim_evidence"):
        return {
            "total": 0,
            "valid": 0,
            "ratio": None,
            "invalid_evidence": [],
            "invalid_truncated": False,
            "status": "table_missing",
            "method": "structural_fingerprint",
        }
    columns = _table_columns(conn, "claim_evidence")
    has_verification = "verification" in columns
    select_columns = (
        "evidence_id, quote, text_sha256, char_start, char_end, verification"
        if has_verification
        else "evidence_id, quote, text_sha256, char_start, char_end"
    )
    rows = conn.execute(
        f"SELECT {select_columns} FROM claim_evidence ORDER BY evidence_id ASC"
    ).fetchall()
    invalid: list[dict[str, str]] = []
    valid = 0
    for row in rows:
        quote = str(row["quote"] or "")
        stored_sha = str(row["text_sha256"] or "")
        position_ok = int(row["char_end"]) >= int(row["char_start"])
        if has_verification:
            verification = str(row["verification"] or "").strip().lower()
            verified = verification == "verified"
            reason = "" if verified else "verification_not_verified"
        else:
            verified = bool(stored_sha) and stored_sha == quote_sha256(quote)
            reason = (
                "empty_quote"
                if not quote.strip()
                else "position_invalid"
                if not position_ok
                else "sha256_mismatch"
            )
        if verified and quote.strip() and position_ok:
            valid += 1
        else:
            invalid.append(
                {
                    "evidence_id": str(row["evidence_id"]),
                    "reason": reason or "empty_quote",
                }
            )
    total = len(rows)
    invalid_sample, truncated = _limited(invalid)
    return {
        "total": total,
        "valid": valid,
        "ratio": valid / total if total else None,
        "invalid_evidence": invalid_sample,
        "invalid_truncated": truncated,
        "method": (
            "verification_column" if has_verification else "structural_fingerprint"
        ),
        "status": "ok",
    }


def broken_link_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "page_links"):
        return {
            "broken": 0,
            "total": 0,
            "ratio": None,
            "broken_links": [],
            "truncated": False,
            "status": "table_missing",
        }
    total = int(conn.execute("SELECT COUNT(*) FROM page_links").fetchone()[0])
    rows = conn.execute(
        "SELECT source_page_id, target_slug FROM page_links "
        "WHERE target_page_id IS NULL "
        "ORDER BY source_page_id ASC, target_slug ASC"
    ).fetchall()
    broken_sample, truncated = _limited(
        [
            {
                "source_page_id": str(row["source_page_id"]),
                "target_slug": str(row["target_slug"]),
            }
            for row in rows
        ]
    )
    return {
        "broken": len(rows),
        "total": total,
        "ratio": len(rows) / total if total else None,
        "broken_links": broken_sample,
        "truncated": truncated,
        "status": "ok",
    }


def orphan_pages(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "pages"):
        return {
            "published": 0,
            "orphan_count": 0,
            "pages": [],
            "status": "table_missing",
        }
    published = [
        PageRecord.model_validate(dict(row))
        for row in conn.execute(
            "SELECT * FROM pages WHERE status = 'published' "
            "ORDER BY slug COLLATE NOCASE ASC, page_id ASC"
        )
    ]
    outbound: set[str] = set()
    inbound: set[str] = set()
    if _table_exists(conn, "page_links"):
        outbound = {
            str(row[0])
            for row in conn.execute("SELECT DISTINCT source_page_id FROM page_links")
        }
        inbound = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT target_page_id FROM page_links "
                "WHERE target_page_id IS NOT NULL"
            )
        }
    orphans = [
        {
            "page_id": page.page_id,
            "slug": page.slug,
            "title": page.title,
        }
        for page in published
        if page.page_id not in outbound and page.page_id not in inbound
    ]
    sample, truncated = _limited(orphans)
    return {
        "published": len(published),
        "orphan_count": len(orphans),
        "pages": sample,
        "truncated": truncated,
        "status": "ok",
    }


def duplicate_page_candidates(
    conn: sqlite3.Connection,
    config: WikiConfig,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any]:
    if not _table_exists(conn, "pages"):
        return {
            "group_count": 0,
            "title_alias_groups": [],
            "groups_truncated": False,
            "summary_pair_count": 0,
            "summary_pairs": [],
            "pairs_truncated": False,
            "threshold": threshold,
            "status": "table_missing",
        }
    pages = [
        PageRecord.model_validate(dict(row))
        for row in conn.execute(
            "SELECT * FROM pages WHERE status <> 'archived' "
            "ORDER BY page_id ASC"
        )
    ]
    frontmatter, _errors = _frontmatter_data(config, pages)

    groups: dict[str, dict[str, Any]] = {}
    for page in pages:
        terms: list[tuple[str, str]] = [("title", page.title)]
        aliases = frontmatter.get(page.page_id, {}).get("aliases")
        if isinstance(aliases, list):
            terms.extend(
                ("alias", str(alias))
                for alias in aliases
                if isinstance(alias, str) and alias.strip()
            )
        for match_type, term in terms:
            key = _normalize_key(term)
            if not key:
                continue
            group = groups.setdefault(
                key, {"key": key, "pages": {}, "match_types": set(), "terms": set()}
            )
            group["pages"][page.page_id] = {
                "page_id": page.page_id,
                "slug": page.slug,
                "title": page.title,
            }
            group["match_types"].add(match_type)
            group["terms"].add(str(term).strip())

    duplicate_groups = [
        {
            "key": group["key"],
            "match_types": sorted(group["match_types"]),
            "terms": sorted(group["terms"]),
            "pages": [
                group["pages"][page_id]
                for page_id in sorted(group["pages"])
            ],
        }
        for group in groups.values()
        if len(group["pages"]) > 1
    ]
    duplicate_groups.sort(key=lambda item: item["key"])

    summaries: list[tuple[PageRecord, str]] = []
    for page in pages:
        summary = frontmatter.get(page.page_id, {}).get("summary")
        if isinstance(summary, str) and summary.strip():
            summaries.append((page, normalize_ws(summary)))
    summary_pairs: list[dict[str, Any]] = []
    for index, (first, first_summary) in enumerate(summaries):
        for second, second_summary in summaries[index + 1 :]:
            ratio = difflib.SequenceMatcher(
                None, first_summary, second_summary
            ).ratio()
            if ratio >= threshold:
                summary_pairs.append(
                    {
                        "page_id_a": first.page_id,
                        "page_id_b": second.page_id,
                        "slug_a": first.slug,
                        "slug_b": second.slug,
                        "similarity": round(ratio, 4),
                    }
                )
    summary_pairs.sort(
        key=lambda item: (-item["similarity"], item["page_id_a"], item["page_id_b"])
    )
    group_sample, group_truncated = _limited(duplicate_groups)
    pair_sample, pair_truncated = _limited(summary_pairs)
    return {
        "group_count": len(duplicate_groups),
        "title_alias_groups": group_sample,
        "groups_truncated": group_truncated,
        "summary_pair_count": len(summary_pairs),
        "summary_pairs": pair_sample,
        "pairs_truncated": pair_truncated,
        "threshold": threshold,
        "status": "ok",
        "note": "只报告候选，不自动合并页面。",
    }


def unindexed_published_pages(
    conn: sqlite3.Connection, config: WikiConfig
) -> dict[str, Any]:
    table = config.page_index_table
    if not _table_exists(conn, table):
        return {
            "status": "index_not_built",
            "index_table": table,
            "message": f"索引未建立（{table} 表缺失）",
            "published_count": None,
            "missing_count": None,
            "pages": [],
        }
    if not _table_exists(conn, "pages"):
        return {
            "status": "pages_table_missing",
            "index_table": table,
            "message": "pages 表缺失，无法比较",
            "published_count": None,
            "missing_count": None,
            "pages": [],
        }

    published = [
        PageRecord.model_validate(dict(row))
        for row in conn.execute(
            "SELECT * FROM pages WHERE status = 'published' "
            "ORDER BY slug COLLATE NOCASE ASC, page_id ASC"
        )
    ]
    columns = _table_columns(conn, table)
    quoted = '"' + table.replace('"', '""') + '"'
    if "page_id" in columns:
        key = "page_id"
        indexed = {
            str(row[0])
            for row in conn.execute(f"SELECT page_id FROM {quoted}")
        }
        missing = [
            {"page_id": page.page_id, "slug": page.slug, "title": page.title}
            for page in published
            if page.page_id not in indexed
        ]
    elif "slug" in columns:
        key = "slug"
        indexed = {
            str(row[0]).casefold()
            for row in conn.execute(f"SELECT slug FROM {quoted}")
        }
        missing = [
            {"page_id": page.page_id, "slug": page.slug, "title": page.title}
            for page in published
            if page.slug.casefold() not in indexed
        ]
    else:
        return {
            "status": "unsupported_schema",
            "index_table": table,
            "message": f"索引表 {table} 不含 page_id 或 slug 列，无法比较",
            "published_count": len(published),
            "missing_count": None,
            "pages": [],
        }

    sample, truncated = _limited(missing)
    return {
        "status": "ok",
        "index_table": table,
        "match_key": key,
        "published_count": len(published),
        "indexed_count": len(indexed),
        "missing_count": len(missing),
        "pages": sample,
        "truncated": truncated,
    }


def unsourced_relations(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "relations"):
        return {
            "count": 0,
            "relations": [],
            "truncated": False,
            "status": "table_missing",
        }
    placeholders = ", ".join("?" for _ in ACTIVE_RELATION_STATUSES)
    rows = conn.execute(
        f"SELECT relation_id, subject_entity_id, predicate, object_entity_id "
        f"FROM relations "
        f"WHERE status IN ({placeholders}) "
        f"AND COALESCE(TRIM(evidence), '') = '' "
        f"AND source_page_id IS NULL "
        f"ORDER BY relation_id ASC",
        ACTIVE_RELATION_STATUSES,
    ).fetchall()
    items = [
        {
            "relation_id": str(row["relation_id"]),
            "subject_entity_id": str(row["subject_entity_id"]),
            "predicate": str(row["predicate"]),
            "object_entity_id": str(row["object_entity_id"]),
        }
        for row in rows
    ]
    sample, truncated = _limited(items)
    return {
        "count": len(items),
        "relations": sample,
        "truncated": truncated,
        "status": "ok",
    }


def unresolved_aliases(conn: sqlite3.Connection) -> dict[str, Any]:
    if not (
        _table_exists(conn, "entities") and _table_exists(conn, "entity_aliases")
    ):
        return {
            "count": 0,
            "categories": {},
            "items": [],
            "truncated": False,
            "status": "tables_missing",
        }
    entity_status = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT entity_id, status FROM entities")
    }
    rows = conn.execute(
        "SELECT entity_id, alias, normalized_alias, alias_type "
        "FROM entity_aliases ORDER BY entity_id ASC, normalized_alias ASC"
    ).fetchall()

    items: list[dict[str, Any]] = []
    category_counts = {
        "dangling_redirect": 0,
        "inactive_owner": 0,
        "ambiguous_active_alias": 0,
    }
    active_groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        entity_id = str(row["entity_id"])
        alias = str(row["alias"])
        normalized = str(row["normalized_alias"])
        alias_type = str(row["alias_type"])
        status = entity_status.get(entity_id)
        if alias_type == "redirect":
            if alias not in entity_status:
                category_counts["dangling_redirect"] += 1
                items.append(
                    {
                        "entity_id": entity_id,
                        "alias": alias,
                        "normalized_alias": normalized,
                        "alias_type": alias_type,
                        "category": "dangling_redirect",
                        "detail": f"redirect 目标实体不存在: {alias}",
                    }
                )
            continue
        if status in INACTIVE_ENTITY_STATUSES:
            category_counts["inactive_owner"] += 1
            items.append(
                {
                    "entity_id": entity_id,
                    "alias": alias,
                    "normalized_alias": normalized,
                    "alias_type": alias_type,
                    "category": "inactive_owner",
                    "detail": f"别名所属实体状态为 {status}",
                }
            )
        if status == "active" and normalized:
            group = active_groups.setdefault(
                normalized.casefold(), {"entity_ids": set(), "rows": []}
            )
            group["entity_ids"].add(entity_id)
            group["rows"].append(
                {
                    "entity_id": entity_id,
                    "alias": alias,
                    "normalized_alias": normalized,
                    "alias_type": alias_type,
                    "category": "ambiguous_active_alias",
                    "detail": "同一 normalized_alias 被多个 active 实体共享",
                }
            )

    for group in active_groups.values():
        if len(group["entity_ids"]) < 2:
            continue
        category_counts["ambiguous_active_alias"] += len(group["rows"])
        items.extend(group["rows"])

    items.sort(
        key=lambda item: (
            item["category"],
            item["normalized_alias"],
            item["entity_id"],
        )
    )
    sample, truncated = _limited(items)
    return {
        "count": len(items),
        "categories": category_counts,
        "items": sample,
        "truncated": truncated,
        "status": "ok",
        "note": (
            "Unresolved = redirect 目标实体不存在 / 别名所属实体 archived|rejected / "
            "同一 normalized_alias 被多个 active 实体共享。"
        ),
    }


def build_report(
    conn: sqlite3.Connection,
    config: WikiConfig,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any]:
    if not 0.0 <= float(similarity_threshold) <= 1.0:
        raise ValueError("similarity_threshold 必须在 0..1 之间")
    metrics = {
        "evidence_coverage": evidence_coverage(conn),
        "quote_validity": quote_validity(conn),
        "broken_link_rate": broken_link_rate(conn),
        "orphan_pages": orphan_pages(conn),
        "duplicate_page_candidates": duplicate_page_candidates(
            conn, config, similarity_threshold
        ),
        "unindexed_published_pages": unindexed_published_pages(conn, config),
        "unsourced_relations": unsourced_relations(conn),
        "unresolved_aliases": unresolved_aliases(conn),
    }
    return {
        "generated_at": utc_now(),
        "db_path": config.db_path,
        "vault_path": config.vault_path,
        "page_index_table": config.page_index_table,
        "similarity_threshold": similarity_threshold,
        "metrics": metrics,
        "notes": [
            "Evidence Coverage 口径：claim_type 属于 fact/factual/definition/"
            "result 且状态非 rejected 的 claim；当前 schema 仅持久化 "
            "fact/hypothesis，definition/result 需未来扩展 claim_type 后生效。",
            "Quote Validity 口径：claim_evidence 若含 verification 列，按 "
            "verification='verified' 统计；当前 schema 无该列，则按 "
            "quote_sha256(quote) 与 text_sha256 比对及位置字段做结构校验，"
            "内容级校验需通过 evidence.revalidate_evidence 回查 LanceDB。",
            "Broken Link Rate = target_page_id IS NULL 的链接数 / 总链接数；"
            "无链接时比率为 null。",
            "Duplicate Page Candidates 只报告候选，不合并页面。",
            "Unindexed Published Pages 依赖 WIKI_PAGE_INDEX_TABLE；表缺失时输出"
            "“索引未建立”。",
            "关系正确率需人工抽样，本脚本不计算、不编造。",
        ],
    }


def _metric_value_line(value: float | None) -> str:
    return _percent(value)


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    coverage = metrics["evidence_coverage"]
    quote = metrics["quote_validity"]
    broken = metrics["broken_link_rate"]
    orphan = metrics["orphan_pages"]
    duplicates = metrics["duplicate_page_candidates"]
    unindexed = metrics["unindexed_published_pages"]
    unsourced = metrics["unsourced_relations"]
    aliases = metrics["unresolved_aliases"]

    unindexed_value = (
        "索引未建立"
        if unindexed["status"] == "index_not_built"
        else "N/A"
        if unindexed["missing_count"] is None
        else str(unindexed["missing_count"])
    )

    lines = [
        "# Wiki 质量报告",
        "",
        f"- 生成时间: {report['generated_at']}",
        f"- 数据库: `{report['db_path']}`",
        f"- vault: `{report['vault_path']}`",
        f"- 页面索引表: `{report['page_index_table']}`",
        "",
        "## 指标摘要",
        "",
        "| 指标 | 数值 | 口径/说明 |",
        "|---|---|---|",
        f"| Evidence Coverage | {_metric_value_line(coverage['ratio'])} "
        f"({coverage['covered']}/{coverage['applicable']}) | "
        "factual/definition/result 类 claim 中有 supporting evidence 的比例 |",
        f"| Quote Validity | {_metric_value_line(quote['ratio'])} "
        f"({quote['valid']}/{quote['total']}) | "
        "结构校验（quote_sha256 与位置字段），非内容级复核 |",
        f"| Broken Link Rate | {_metric_value_line(broken['ratio'])} "
        f"({broken['broken']}/{broken['total']}) | "
        "target_page_id 为空的链接 / 总链接 |",
        f"| Orphan Pages | {orphan['orphan_count']}/{orphan['published']} | "
        "published 页面中无入链且无出链 |",
        f"| Duplicate Page Candidates | {duplicates['group_count']} 组 / "
        f"{duplicates['summary_pair_count']} 对 | 标题别名重复 + 摘要高相似 |",
        f"| Unindexed Published Pages | {unindexed_value} | "
        "published 页面在页面索引中的缺失数 |",
        f"| Unsourced Relations | {unsourced['count']} | "
        "无 evidence 且无 source_page_id 的 proposed/verified 关系 |",
        f"| Unresolved Aliases | {aliases['count']} | "
        "redirect 目标缺失 / inactive 实体别名 / active 实体别名冲突 |",
        "",
        "## 明细",
        "",
        "### Evidence Coverage",
        "",
        f"- 适用 claim（claim_type ∈ "
        f"{', '.join(coverage['claim_types'])}，状态属于 "
        f"{', '.join(coverage['status_scope'])}）: {coverage['applicable']}",
        f"- 已有证据: {coverage['covered']}",
        f"- 覆盖率: {_percent(coverage['ratio'])}",
    ]
    if coverage["missing_claim_ids"]:
        lines.append(
            "- 缺证据 claim: "
            + ", ".join(coverage["missing_claim_ids"])
            + ("（已截断）" if coverage["missing_truncated"] else "")
        )

    lines.extend(
        [
            "",
            "### Quote Validity",
            "",
            f"- 证据总数: {quote['total']}",
            f"- 结构校验通过: {quote['valid']}",
            f"- 通过率: {_percent(quote['ratio'])}",
            f"- 校验方法: `{quote['method']}`"
            + (
                "（claim_evidence.verification 列）"
                if quote["method"] == "verification_column"
                else "（claim_evidence 无 verification 列，切片原文不在 SQLite；"
                "内容级校验需回查 LanceDB）"
            ),
        ]
    )
    if quote["invalid_evidence"]:
        lines.append(
            "- 未通过 evidence_id: "
            + ", ".join(
                f"{item['evidence_id']}({item['reason']})"
                for item in quote["invalid_evidence"]
            )
            + ("（已截断）" if quote["invalid_truncated"] else "")
        )

    lines.extend(
        [
            "",
            "### Broken Link Rate",
            "",
            f"- 断链: {broken['broken']} / 总链接 {broken['total']}",
            f"- 断链率: {_percent(broken['ratio'])}",
        ]
    )
    if broken["broken_links"]:
        lines.append(
            "- 断链样本: "
            + "; ".join(
                f"{item['source_page_id']} -> [[{item['target_slug']}]]"
                for item in broken["broken_links"]
            )
            + ("（已截断）" if broken["truncated"] else "")
        )

    lines.extend(
        [
            "",
            "### Orphan Pages",
            "",
            f"- published 页面: {orphan['published']}",
            f"- 孤儿页面: {orphan['orphan_count']}",
        ]
    )
    for item in orphan["pages"]:
        lines.append(
            f"  - `{item['slug']}` ({item['page_id']}) {item['title']}"
        )
    if orphan["truncated"]:
        lines.append("  - （列表已截断）")

    lines.extend(
        [
            "",
            "### Duplicate Page Candidates",
            "",
            f"- 标题/别名归一后重复组: {duplicates['group_count']}",
            f"- 摘要相似对（阈值 {duplicates['threshold']}）: "
            f"{duplicates['summary_pair_count']}",
            "- 只报告候选，不自动合并。",
        ]
    )
    for group in duplicates["title_alias_groups"]:
        lines.append(
            f"  - 组 `{group['key']}`（{', '.join(group['match_types'])}）: "
            + ", ".join(
                f"{page['slug']}({page['page_id']})" for page in group["pages"]
            )
        )
    for pair in duplicates["summary_pairs"]:
        lines.append(
            f"  - 摘要相似 {pair['similarity']}: "
            f"{pair['slug_a']} <-> {pair['slug_b']}"
        )
    if duplicates["groups_truncated"] or duplicates["pairs_truncated"]:
        lines.append("  - （候选列表已截断）")

    lines.extend(
        [
            "",
            "### Unindexed Published Pages",
            "",
            f"- 状态: `{unindexed['status']}`",
        ]
    )
    if unindexed.get("message"):
        lines.append(f"- 说明: {unindexed['message']}")
    if unindexed["missing_count"] is not None:
        lines.append(f"- 缺失页面: {unindexed['missing_count']}")
        for item in unindexed["pages"]:
            lines.append(f"  - `{item['slug']}` ({item['page_id']})")
        if unindexed.get("truncated"):
            lines.append("  - （列表已截断）")

    lines.extend(
        [
            "",
            "### Unsourced Relations",
            "",
            f"- 数量: {unsourced['count']}",
        ]
    )
    for item in unsourced["relations"]:
        lines.append(
            f"  - {item['relation_id']}: {item['subject_entity_id']} "
            f"-{item['predicate']}-> {item['object_entity_id']}"
        )
    if unsourced["truncated"]:
        lines.append("  - （列表已截断）")

    lines.extend(
        [
            "",
            "### Unresolved Aliases",
            "",
            f"- 数量: {aliases['count']}",
            "- 分类: "
            + ", ".join(
                f"{category}={count}"
                for category, count in aliases["categories"].items()
            ),
        ]
    )
    for item in aliases["items"]:
        lines.append(
            f"  - [{item['category']}] {item['alias']} "
            f"(entity {item['entity_id']}): {item['detail']}"
        )
    if aliases["truncated"]:
        lines.append("  - （列表已截断）")

    lines.extend(["", "## 口径与限制", ""])
    lines.extend(f"- {note}" for note in report["notes"])
    lines.append("")
    return "\n".join(lines)


def _write_text(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _resolve_paths(
    args: argparse.Namespace,
) -> tuple[WikiConfig, str, str]:
    try:
        env_config = WikiConfig.from_env()
    except WikiConfigError as exc:
        raise QualityReportError(f"读取 Wiki 配置失败: {exc}") from exc
    vault = (
        os.path.realpath(os.path.abspath(os.path.expanduser(args.vault)))
        if args.vault
        else env_config.vault_path
    )
    if args.db:
        db_path = os.path.realpath(os.path.abspath(os.path.expanduser(args.db)))
    elif args.vault:
        db_path = os.path.join(
            vault, C.DEFAULT_DB_SUBDIR, C.DEFAULT_DB_FILENAME
        )
    else:
        db_path = env_config.db_path
    return env_config, vault, db_path


def _build_config(
    env_config: WikiConfig,
    args: argparse.Namespace,
    vault_path: str,
    db_path: str,
) -> WikiConfig:
    if args.vault:
        return replace(
            env_config,
            vault_path=vault_path,
            page_root=C.DEFAULT_PAGE_ROOT,
            db_path=db_path,
        )
    return replace(env_config, db_path=db_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 Wiki 质量报告（证据覆盖、链接、孤儿页、重复候选等）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Wiki SQLite 路径（默认 WIKI_DB_PATH 或 <vault>/.wiki/wiki.sqlite3）",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="vault 路径（默认 WIKI_VAULT_PATH）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="写入文件（默认 stdout）；例如 docs/wiki-v1/quality-report.md",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON（默认 Markdown）",
    )
    output_group.add_argument(
        "--format",
        choices=("markdown", "json"),
        default=None,
        help="输出格式（与 --json 二选一）",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=DEFAULT_SIMILARITY_THRESHOLD,
        help=f"摘要相似判定阈值（默认 {DEFAULT_SIMILARITY_THRESHOLD}）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        env_config, vault_path, db_path = _resolve_paths(args)
        if not os.path.exists(db_path):
            raise QualityReportError(f"Wiki 数据库不存在: {db_path}")
        if not os.path.isfile(db_path):
            raise QualityReportError(f"Wiki 数据库路径不是文件: {db_path}")
        config = _build_config(env_config, args, vault_path, db_path)
        conn = wiki_db.connect(db_path, config)
        if not wiki_db.db_status(conn):
            raise QualityReportError(
                f"数据库未执行 Wiki 迁移: {db_path}；先运行迁移"
            )
        report = build_report(conn, config, args.similarity_threshold)
    except (QualityReportError, WikiConfigError, ValueError, sqlite3.Error) as exc:
        print(f"wiki_quality_report: error: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    if args.json or args.format == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        text = render_markdown(report)

    if args.out:
        output_path = os.path.abspath(os.path.expanduser(args.out))
        _write_text(output_path, text)
        print(f"质量报告已写入: {output_path}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
