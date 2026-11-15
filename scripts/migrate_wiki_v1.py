# -*- coding: utf-8 -*-
"""把 LanceDB ``my_docs`` 的既有切片登记进 Wiki SQLite（Phase 2 迁移）。

原则：
- LanceDB 全程只读：只 connect / list_tables / open_table / count_rows /
  to_lance / scanner / to_table，绝不 add / delete / update / optimize；
- 默认 ``--dry-run``，只输出统计，不创建/不写入 SQLite；
- ``--apply`` 连接 ``WIKI_DB_PATH``（默认 ``<vault>/.wiki/wiki.sqlite3``），
  执行迁移后用 DocumentRegistry 登记，每 500 行一个事务，重复执行幂等；
- 旧行全部作为证据保留：source 不合并、行不删除；同一
  ``(source, chunk_index)`` 下文本不同的切片会各占一行 chunk_ref。

用法：
    python -X utf8 scripts/migrate_wiki_v1.py                 # dry-run
    python -X utf8 scripts/migrate_wiki_v1.py --json
    python -X utf8 scripts/migrate_wiki_v1.py --apply
    python -X utf8 scripts/migrate_wiki_v1.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb import config as kb_config  # noqa: E402
from kb.wiki import config as wiki_config  # noqa: E402
from kb.wiki import db as wiki_db  # noqa: E402
from kb.wiki import document_registry as registry  # noqa: E402
from kb.wiki.ids import chunk_id, document_id as make_document_id  # noqa: E402
from kb.wiki.ids import normalize_text_sha256  # noqa: E402

DEFAULT_BATCH_SIZE = 500
DEFAULT_PARSER_NAME = registry.LEGACY_PARSER_NAME
LANCE_COLUMNS = ["source", "chunk_index", "category", "doc_id", "text"]


class MigrationError(RuntimeError):
    """迁移输入/环境不满足只读迁移约束。"""


@dataclass(frozen=True, slots=True)
class RowRecord:
    """一行 LanceDB 数据经规范化后的迁移输入。"""

    source: str
    source_type: str
    canonical_uri: str
    category: str
    legacy_doc_id: str
    chunk_index: int
    text_sha256: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.source_type, self.canonical_uri


def _canonical_identity(source: str) -> tuple[str, str]:
    source_type = registry.infer_source_type(source)
    if source_type == "web":
        try:
            uri = registry.canonical_web_source(source)
        except ValueError:
            uri = source.strip()
    else:
        uri = registry.canonical_local_source(source) or source.strip()
    return source_type, uri


def _prepare_row(row: dict[str, Any]) -> tuple[RowRecord | None, str]:
    """把原始行转成 RowRecord；返回 (record, skip_reason)。"""
    source = str(row.get("source") or "").strip()
    if not source:
        return None, "empty_source"
    try:
        chunk_index = int(row.get("chunk_index") or 0)
    except (TypeError, ValueError):
        return None, "invalid_chunk_index"
    if chunk_index < 0:
        return None, "invalid_chunk_index"

    source_type, canonical_uri = _canonical_identity(source)
    text = row.get("text")
    text_sha256 = normalize_text_sha256("" if text is None else str(text))
    return (
        RowRecord(
            source=source,
            source_type=source_type,
            canonical_uri=canonical_uri,
            category=str(row.get("category") or ""),
            legacy_doc_id=str(row.get("doc_id") or ""),
            chunk_index=chunk_index,
            text_sha256=text_sha256,
        ),
        "",
    )


def _lance_counts(db_path: str) -> dict[str, int]:
    import lancedb

    path = Path(db_path)
    if not path.is_dir():
        raise MigrationError(f"LanceDB 路径不存在: {db_path}")
    db = lancedb.connect(str(path))
    tables = list(db.list_tables().tables)
    counts: dict[str, int] = {}
    for name in (kb_config.TABLE_NAME, kb_config.IMAGE_TABLE_NAME):
        if name in tables:
            counts[name] = int(db.open_table(name).count_rows())
    return counts


def read_my_docs_rows(
    db_path: str, limit: int | None = None
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """只读读取 my_docs 指定列，返回 (rows, my_docs 总行数, 表行数快照)。"""
    import lancedb

    path = Path(db_path)
    if not path.is_dir():
        raise MigrationError(f"LanceDB 路径不存在: {db_path}")
    db = lancedb.connect(str(path))
    tables = list(db.list_tables().tables)
    if kb_config.TABLE_NAME not in tables:
        raise MigrationError(f"LanceDB 中没有表 {kb_config.TABLE_NAME}: {db_path}")

    table = db.open_table(kb_config.TABLE_NAME)
    total = int(table.count_rows())
    dataset = table.to_lance()
    if limit is not None:
        arrow = dataset.scanner(columns=LANCE_COLUMNS, limit=limit).to_table()
    else:
        arrow = dataset.to_table(columns=LANCE_COLUMNS)

    columns = {
        name: arrow.column(name).to_pylist() for name in LANCE_COLUMNS
    }
    rows = [
        {name: columns[name][index] for name in LANCE_COLUMNS}
        for index in range(arrow.num_rows)
    ]
    return rows, total, _lance_counts(db_path)


def _iter_batches(rows: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _read_only_connection(path: str) -> sqlite3.Connection | None:
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True
        )
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def existing_wiki_state(wiki_db_path: str) -> dict[str, Any]:
    """只读读取既有 Wiki 库，供 dry-run 计算“还需创建多少”。"""
    state: dict[str, Any] = {
        "exists": os.path.isfile(wiki_db_path),
        "readable": False,
        "documents": {},
        "legacy_revisions": {},
        "chunk_ids": set(),
    }
    conn = _read_only_connection(wiki_db_path)
    if conn is None:
        return state
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        state["readable"] = True
        if "documents" in tables:
            for row in conn.execute(
                "SELECT document_id, source_type, canonical_uri FROM documents"
            ):
                state["documents"][(row["source_type"], row["canonical_uri"])] = str(
                    row["document_id"]
                )
        if "document_revisions" in tables:
            for row in conn.execute(
                "SELECT document_id, revision_id FROM document_revisions "
                "WHERE parser_name = ?",
                (DEFAULT_PARSER_NAME,),
            ):
                state["legacy_revisions"][str(row["document_id"])] = str(
                    row["revision_id"]
                )
        if "chunk_refs" in tables:
            state["chunk_ids"] = {
                str(row[0]) for row in conn.execute("SELECT chunk_id FROM chunk_refs")
            }
    except sqlite3.Error as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return state


def _identity_sort_key(identity: tuple[str, str]) -> tuple[str, str]:
    return identity


def analyze_rows(
    rows: Sequence[dict[str, Any]],
    *,
    total_rows: int,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算 dry-run 统计；不写任何库。"""
    sources: Counter[str] = Counter()
    source_chunk_pairs: Counter[tuple[str, int]] = Counter()
    doc_ids: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    identities: dict[tuple[str, str], dict[str, Any]] = OrderedDict()
    triples: Counter[tuple[tuple[str, str], int, str]] = Counter()
    canonical_sources: dict[tuple[str, str], set[str]] = {}
    skip_reasons: Counter[str] = Counter()
    valid_rows = 0

    for row in rows:
        record, reason = _prepare_row(row)
        if record is None:
            skip_reasons[reason] += 1
            continue
        valid_rows += 1
        sources[record.source] += 1
        source_chunk_pairs[(record.source, record.chunk_index)] += 1
        if record.category:
            categories[record.category] += 1
        if record.legacy_doc_id:
            doc_ids[record.legacy_doc_id] += 1
        triples[(record.identity, record.chunk_index, record.text_sha256)] += 1
        canonical_sources.setdefault(record.identity, set()).add(record.source)
        entry = identities.setdefault(
            record.identity,
            {
                "source_type": record.source_type,
                "canonical_uri": record.canonical_uri,
                "source": record.source,
                "categories": set(),
                "rows": 0,
            },
        )
        entry["rows"] += 1
        if record.category:
            entry["categories"].add(record.category)

    duplicate_pairs_groups = sum(
        1 for count in source_chunk_pairs.values() if count > 1
    )
    duplicate_pairs_rows = sum(
        count for count in source_chunk_pairs.values() if count > 1
    )
    duplicate_doc_groups = sum(1 for count in doc_ids.values() if count > 1)
    duplicate_doc_rows = sum(count for count in doc_ids.values() if count > 1)

    identity_items = sorted(identities.items(), key=lambda item: _identity_sort_key(item[0]))
    collision_examples = [
        {
            "source_type": identity[0],
            "canonical_uri": identity[1],
            "sources": sorted(canonical_sources[identity])[:5],
            "source_count": len(canonical_sources[identity]),
        }
        for identity, _ in identity_items
        if len(canonical_sources.get(identity, ())) > 1
    ]

    existing = existing or {}
    existing_documents: dict[tuple[str, str], str] = existing.get("documents", {})
    existing_revisions: dict[str, str] = existing.get("legacy_revisions", {})
    existing_chunk_ids: set[str] = existing.get("chunk_ids", set())

    documents_to_create = 0
    revisions_to_create = 0
    chunk_refs_to_create = 0
    triples_by_identity: dict[tuple[str, str], list[tuple[int, str, int]]] = {}
    for (triple_identity, chunk_index, text_sha256), count in triples.items():
        triples_by_identity.setdefault(triple_identity, []).append(
            (chunk_index, text_sha256, count)
        )
    for identity, entry in identity_items:
        document_id = existing_documents.get(identity)
        if document_id is None:
            document_id = make_document_id(identity[0], identity[1])
            documents_to_create += 1
        revision_id = existing_revisions.get(str(document_id))
        if revision_id is None:
            revisions_to_create += 1
        for chunk_index, text_sha256, count in triples_by_identity.get(identity, ()):
            if revision_id is None:
                chunk_refs_to_create += count
            elif chunk_id(revision_id, chunk_index, text_sha256) not in existing_chunk_ids:
                chunk_refs_to_create += count

    return {
        "rows": len(rows),
        "total_rows": total_rows,
        "examined_rows": valid_rows,
        "skipped_rows": sum(skip_reasons.values()),
        "skip_reasons": dict(skip_reasons),
        "distinct_sources": len(sources),
        "documents": len(identities),
        "revisions": len(identities),
        "chunk_refs": len(triples),
        "documents_to_create": documents_to_create,
        "documents_existing": len(identity_items) - documents_to_create,
        "revisions_to_create": revisions_to_create,
        "revisions_existing": len(identity_items) - revisions_to_create,
        "chunk_refs_to_create": chunk_refs_to_create,
        "chunk_refs_existing": valid_rows - chunk_refs_to_create,
        "duplicate_source_chunk_pairs": {
            "groups": duplicate_pairs_groups,
            "rows": duplicate_pairs_rows,
        },
        "duplicate_doc_ids": {
            "groups": duplicate_doc_groups,
            "rows": duplicate_doc_rows,
        },
        "empty_doc_id_rows": sum(
            1 for row in rows if not str(row.get("doc_id") or "").strip()
        ),
        "identical_chunk_ref_rows": valid_rows - len(triples),
        "canonical_collisions": {
            "groups": len(collision_examples),
            "examples": collision_examples[:10],
        },
        "multi_category_identities": sum(
            1 for entry in identities.values() if len(entry["categories"]) > 1
        ),
        "categories": dict(categories.most_common()),
        "top_sources": [[source, count] for source, count in sources.most_common(20)],
        "wiki_db": {
            "path": existing.get("path", ""),
            "exists": bool(existing.get("exists")),
            "readable": bool(existing.get("readable")),
        },
    }


def _sqlite_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("documents", "document_revisions", "chunk_refs"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        counts[table] = (
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if exists is not None
            else 0
        )
    return counts


def apply_migration(
    rows: Sequence[dict[str, Any]],
    *,
    wiki_db_path: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """把 rows 登记进 Wiki SQLite；每 batch_size 行一个写事务。"""
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    config = wiki_config.WikiConfig.from_env()
    conn = wiki_db.connect(wiki_db_path, config)
    cache: dict[tuple[str, str], registry.LegacyRegistration] = {}
    stats = {
        "rows": len(rows),
        "rows_processed": 0,
        "skipped_rows": 0,
        "skip_reasons": Counter(),
        "documents_created": 0,
        "documents_existing": 0,
        "revisions_created": 0,
        "revisions_existing": 0,
        "chunk_refs_inserted": 0,
    }
    try:
        applied = wiki_db.run_migrations(conn, db_path=wiki_db_path)
        before_counts = _sqlite_table_counts(conn)

        for batch in _iter_batches(rows, batch_size):
            grouped: "OrderedDict[tuple[str, str], list[RowRecord]]" = OrderedDict()
            for raw_row in batch:
                record, reason = _prepare_row(raw_row)
                if record is None:
                    stats["skipped_rows"] += 1
                    stats["skip_reasons"][reason] += 1
                    continue
                grouped.setdefault(record.identity, []).append(record)

            with wiki_db.transaction(conn):
                for identity, records in grouped.items():
                    descriptors = [
                        registry.ChunkDescriptor(
                            chunk_index=record.chunk_index,
                            text_sha256=record.text_sha256,
                            legacy_doc_id=record.legacy_doc_id,
                            source_value=record.source,
                        )
                        for record in records
                    ]
                    cached = cache.get(identity)
                    if cached is None:
                        category = next(
                            (record.category for record in records if record.category),
                            "",
                        )
                        registration = registry.register_legacy_source(
                            conn,
                            records[0].source,
                            descriptors,
                            category=category,
                            source_type=identity[0],
                        )
                        cache[identity] = registration
                        if registration.document_created:
                            stats["documents_created"] += 1
                        else:
                            stats["documents_existing"] += 1
                        if registration.revision_created:
                            stats["revisions_created"] += 1
                        else:
                            stats["revisions_existing"] += 1
                        stats["chunk_refs_inserted"] += registration.chunk_refs_inserted
                    else:
                        stats["chunk_refs_inserted"] += registry.register_chunk_refs(
                            conn, cached.revision_id, descriptors
                        )
                    stats["rows_processed"] += len(records)

        after_counts = _sqlite_table_counts(conn)
        return {
            "migrations_applied": applied,
            "rows": stats["rows"],
            "rows_processed": stats["rows_processed"],
            "skipped_rows": stats["skipped_rows"],
            "skip_reasons": dict(stats["skip_reasons"]),
            "documents_created": after_counts["documents"] - before_counts["documents"],
            "documents_existing": len(cache) - (
                after_counts["documents"] - before_counts["documents"]
            ),
            "revisions_created": (
                after_counts["document_revisions"] - before_counts["document_revisions"]
            ),
            "revisions_existing": len(cache) - (
                after_counts["document_revisions"]
                - before_counts["document_revisions"]
            ),
            "chunk_refs_inserted": (
                after_counts["chunk_refs"] - before_counts["chunk_refs"]
            ),
            "sqlite_counts_before": before_counts,
            "sqlite_counts_after": after_counts,
        }
    finally:
        conn.close()


def _resolve_paths(args: argparse.Namespace) -> tuple[str, str]:
    db_path = args.db or kb_config.resolve_db_path()
    if args.wiki_db:
        wiki_db_path = args.wiki_db
    else:
        wiki_db_path = wiki_config.WikiConfig.from_env().db_path
    return str(db_path), str(wiki_db_path)


def _print_human(stats: dict[str, Any]) -> None:
    mode = stats.get("mode", "")
    print(f"# Wiki V1 迁移（{mode}）")
    print(f"LanceDB: {stats.get('db_path', '')}")
    print(f"Wiki DB: {stats.get('wiki_db_path', '')}")
    print(
        f"my_docs 总行数={stats.get('total_rows', 0)}，"
        f"本次检查={stats.get('rows', 0)}，有效={stats.get('examined_rows', 0)}，"
        f"跳过={stats.get('skipped_rows', 0)}"
    )
    print(
        f"source={stats.get('distinct_sources', 0)}，"
        f"documents={stats.get('documents', 0)}，"
        f"revisions={stats.get('revisions', 0)}，"
        f"chunk_refs={stats.get('chunk_refs', 0)}"
    )
    duplicates = stats.get("duplicate_source_chunk_pairs", {})
    print(
        f"重复 (source, chunk_index): {duplicates.get('groups', 0)} 组 / "
        f"{duplicates.get('rows', 0)} 行"
    )
    if mode == "dry-run":
        print(
            f"待创建: documents={stats.get('documents_to_create', 0)}，"
            f"revisions={stats.get('revisions_to_create', 0)}，"
            f"chunk_refs={stats.get('chunk_refs_to_create', 0)}"
        )
    else:
        print(
            f"已创建: documents={stats.get('documents_created', 0)}，"
            f"revisions={stats.get('revisions_created', 0)}，"
            f"chunk_refs={stats.get('chunk_refs_inserted', 0)}"
        )
    counts_before = stats.get("lance_counts_before")
    counts_after = stats.get("lance_counts_after")
    if counts_before is not None:
        print(f"LanceDB 行数 before={counts_before} after={counts_after}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path, wiki_db_path = _resolve_paths(args)
    limit = None if not args.limit else int(args.limit)
    if limit is not None and limit < 0:
        raise MigrationError("--limit 不能为负数")

    rows, total_rows, counts_before = read_my_docs_rows(db_path, limit=limit)
    if limit is None and len(rows) != total_rows:
        raise MigrationError(
            f"读取行数 {len(rows)} 与 count_rows {total_rows} 不一致，已中止"
        )
    if limit is not None and len(rows) > total_rows:
        raise MigrationError("--limit 超过表行数，已中止")

    if not args.apply:
        existing = existing_wiki_state(wiki_db_path)
        existing["path"] = wiki_db_path
        stats = analyze_rows(
            rows, total_rows=total_rows, existing=existing
        )
        stats.update(
            {
                "mode": "dry-run",
                "db_path": db_path,
                "wiki_db_path": wiki_db_path,
                "lance_counts_before": counts_before,
                "lance_counts_after": counts_before,
                "lance_unchanged": True,
            }
        )
        return stats

    applied = apply_migration(rows, wiki_db_path=wiki_db_path)
    counts_after = _lance_counts(db_path)
    if counts_after != counts_before:
        raise MigrationError(
            f"LanceDB 行数发生变化（只读断言失败）: {counts_before} -> {counts_after}"
        )
    stats = analyze_rows(rows, total_rows=total_rows)
    stats.update(
        {
            "mode": "apply",
            "db_path": db_path,
            "wiki_db_path": wiki_db_path,
            "lance_counts_before": counts_before,
            "lance_counts_after": counts_after,
            "lance_unchanged": True,
        }
    )
    stats.update(applied)
    for stale_key in (
        "documents_to_create",
        "revisions_to_create",
        "chunk_refs_to_create",
        "chunk_refs_existing",
    ):
        stats.pop(stale_key, None)
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 LanceDB my_docs 登记进 Wiki SQLite（默认 dry-run）"
    )
    parser.add_argument(
        "--apply", action="store_true", help="实际写入 Wiki SQLite（默认只 dry-run）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="显式 dry-run（默认行为）"
    )
    parser.add_argument("--db", default="", help="LanceDB 路径（默认按 kb.config 解析）")
    parser.add_argument(
        "--wiki-db", default="", help="Wiki SQLite 路径（默认 WIKI_DB_PATH 或 <vault>/.wiki/wiki.sqlite3）"
    )
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 行（0=全部）")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.dry_run:
        print("[ERROR] --apply 与 --dry-run 不能同时使用", file=sys.stderr)
        return 2
    try:
        stats = run(args)
    except MigrationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
