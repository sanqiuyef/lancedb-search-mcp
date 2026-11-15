# -*- coding: utf-8 -*-
"""把 my_docs 中旧 basename source 重指向 vault 内论文 Markdown（只改来源）。

背景：2026-11-15 来源规则要求 ``source`` 只能是 vault 相对路径。真实库仍有
两个无目录分隔符的旧 source；本脚本按 ``Path(source).stem`` 在
``<vault>/08-literature-文献/papers-论文全文`` 下查找同名 ``.md``，唯一命中时
把 LanceDB 行的 source 原地更新为 vault 相对路径，并同步 Wiki SQLite 的
``documents`` / ``chunk_refs`` 指向。

约束：
- 默认 dry-run，只读；``--apply`` 才写库；
- 只更新 source 列，绝不删除 LanceDB 行，也不动其他 source；
- ``--mark-missing`` 只把无匹配来源对应 documents.status 置 ``missing``；
- 重复执行幂等：已相对路径不会再被扫描。

用法：
  D:/anaconda3/python.exe -X utf8 scripts/relink_sources_to_vault.py \
    --db "<lancedb>" --vault "<vault>" [--apply] [--mark-missing] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kb import config as cfg  # noqa: E402

PAPER_REL_DIR = Path("08-literature-文献", "papers-论文全文")


# =============================================================
# 路径与 source 工具
# =============================================================

def _canonical_source(value: str) -> str:
    """与 kb.wiki.document_registry.canonical_local_source 一致的斜杠形式。"""
    return (value or "").strip().replace("\\", "/")


def _norm_source(value: str) -> str:
    return os.path.normcase(os.path.normpath(value or ""))


def _sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def _is_web_like(source: str) -> bool:
    value = (source or "").strip()
    return value.lower().startswith(("http://", "https://")) or value.startswith("网页-")


def _row_count(table) -> int:
    try:
        return int(table.count_rows())
    except Exception:
        return len(table)


def _all_sources(table) -> Counter:
    arrow = table.to_lance().to_table(columns=["source"])
    return Counter(
        value if isinstance(value, str) else str(value or "")
        for value in arrow.column("source").to_pylist()
    )


def _paper_markdown_candidates(paper_dir: Path, stem: str) -> list[Path]:
    """论文目录下 stem 相同的 `.md`（递归；大小写按平台归一）。"""
    if not paper_dir.is_dir():
        return []
    target = os.path.normcase(stem)
    return sorted(
        path for path in paper_dir.rglob("*.md")
        if path.is_file() and os.path.normcase(path.stem) == target
    )


def _resolve_wiki_db(vault: str, explicit: str) -> str:
    if explicit:
        return explicit
    env_value = os.environ.get("WIKI_DB_PATH", "").strip()
    if env_value:
        return env_value
    return os.path.join(vault, ".wiki", "wiki.sqlite3")


# =============================================================
# Wiki SQLite（只读写 documents / chunk_refs，不跑迁移）
# =============================================================

def _connect_wiki(path: str, write: bool) -> sqlite3.Connection | None:
    if not os.path.isfile(path):
        return None
    if write:
        conn = sqlite3.connect(path, timeout=10)
    else:
        try:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10)
        except sqlite3.Error:
            # 只读 WAL 打不开时退回普通连接；本脚本 dry-run 不执行任何写 SQL。
            conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _wiki_has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _wiki_documents_for_source(conn: sqlite3.Connection,
                               source: str) -> list[sqlite3.Row]:
    canonical = _canonical_source(source)
    return list(conn.execute(
        "SELECT document_id, canonical_uri, display_name, status "
        "FROM documents "
        "WHERE source_type = 'file' "
        "AND (canonical_uri IN (?, ?) OR display_name IN (?, ?))",
        (canonical, source, canonical, source),
    ).fetchall())


def _wiki_canonical_conflict(conn: sqlite3.Connection, new_source: str,
                             document_ids: set[str]) -> bool:
    canonical = _canonical_source(new_source)
    rows = conn.execute(
        "SELECT document_id FROM documents "
        "WHERE source_type = 'file' AND canonical_uri = ?",
        (canonical,),
    ).fetchall()
    return any(str(row["document_id"]) not in document_ids for row in rows)


def _wiki_apply_relink(conn: sqlite3.Connection, document_ids: list[str],
                       old_source: str, new_source: str) -> dict:
    canonical = _canonical_source(new_source)
    display_name = os.path.basename(new_source.replace("\\", "/"))
    documents_updated = 0
    chunk_refs_updated = 0
    for document_id in document_ids:
        cursor = conn.execute(
            "UPDATE documents SET canonical_uri = ?, display_name = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE document_id = ?",
            (canonical, display_name, document_id),
        )
        documents_updated += int(cursor.rowcount or 0)
        cursor = conn.execute(
            "UPDATE chunk_refs SET source_value = ? "
            "WHERE document_id = ? AND (source_value = ? OR source_value = ?)",
            (new_source, document_id, old_source, _canonical_source(old_source)),
        )
        chunk_refs_updated += int(cursor.rowcount or 0)
    return {
        "documents_updated": documents_updated,
        "chunk_refs_updated": chunk_refs_updated,
    }


def _wiki_apply_missing(conn: sqlite3.Connection,
                        document_ids: list[str]) -> int:
    updated = 0
    for document_id in document_ids:
        cursor = conn.execute(
            "UPDATE documents SET status = 'missing', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE document_id = ? AND status <> 'missing'",
            (document_id,),
        )
        updated += int(cursor.rowcount or 0)
    return updated


# =============================================================
# 扫描与输出
# =============================================================

def _scan_sources(counts: Counter, vault: str, paper_dir: Path) -> dict:
    sources = set(counts)
    legacy = sorted(
        source for source in sources
        if source and "/" not in source and "\\" not in source
    )
    already_relative = sorted(source for source in sources if source not in legacy)
    web_sources = [source for source in legacy if _is_web_like(source)]

    relinkable = []
    no_match = []
    for source in legacy:
        if _is_web_like(source):
            continue
        stem = Path(source).stem
        candidates = _paper_markdown_candidates(paper_dir, stem)
        item = {
            "source": source,
            "rows": int(counts[source]),
            "stem": stem,
        }
        if len(candidates) == 1:
            new_source = os.path.relpath(candidates[0], vault)
            item.update({
                "new_source": new_source,
                "markdown": str(candidates[0]),
            })
            relinkable.append(item)
        else:
            item.update({
                "reason": "ambiguous_markdown" if candidates else "no_markdown",
                "candidates": [os.path.relpath(path, vault) for path in candidates],
            })
            no_match.append(item)

    return {
        "total_rows": sum(counts.values()),
        "distinct_sources": len(sources),
        "already_relative": already_relative,
        "legacy_basename": legacy,
        "web_sources": web_sources,
        "relinkable": relinkable,
        "no_match": no_match,
    }


def _markdown_report(result: dict, apply: bool, wiki: dict | None,
                     rows_after: int | None) -> str:
    mode = "apply" if apply else "dry-run"
    lines = [
        f"my_docs source 重指向（{mode}）",
        "=" * 48,
        f"目标库: {result['db']}",
        f"vault: {result['vault']}",
        f"论文目录: {result['paper_dir']}",
        f"Wiki DB: {result['wiki_db'] or '(不存在)'}",
        "",
        f"总行数: {result['rows_before']}"
        + (f" → {rows_after}" if rows_after is not None else ""),
        f"distinct source: {result['distinct_sources']}",
        f"已是 vault 相对路径: {len(result['already_relative'])}",
        f"旧 basename source: {len(result['legacy_basename'])}"
        + (f"（跳过 web 形态 {len(result['web_sources'])} 个）"
           if result["web_sources"] else ""),
        f"可重指向: {len(result['relinkable'])}",
    ]
    for item in result["relinkable"]:
        lines.append(
            f"  - {item['source']}  →  {item['new_source']}（{item['rows']} 行）"
        )
    lines.append(f"无匹配: {len(result['no_match'])}")
    for item in result["no_match"]:
        detail = "未找到同名 .md" if item["reason"] == "no_markdown" else \
            f"同名 .md 不唯一（{len(item['candidates'])} 个）"
        lines.append(f"  - {item['source']}（{item['rows']} 行）: {detail}")
    for item in result.get("conflicts", []):
        lines.append(
            f"  ! 跳过 {item['source']}: {item['reason']}"
            + (f"（{item.get('detail')}）" if item.get("detail") else "")
        )

    if wiki is not None:
        lines.extend(["", "Wiki SQLite:"])
        if not wiki.get("available"):
            lines.append(f"  未找到或无法打开: {wiki.get('error', '不存在')}")
        else:
            if apply:
                lines.append(
                    f"  documents 更新: {wiki.get('documents_updated', 0)}"
                )
                lines.append(
                    f"  chunk_refs 更新: {wiki.get('chunk_refs_updated', 0)}"
                )
                lines.append(
                    f"  status=missing 更新: {wiki.get('documents_marked_missing', 0)}"
                )
            else:
                lines.append(
                    f"  可联动 documents: {wiki.get('documents_to_relink', 0)}"
                )
                lines.append(
                    f"  可标记 missing: {wiki.get('documents_to_mark_missing', 0)}"
                )
            if wiki.get("conflicts"):
                for item in wiki["conflicts"]:
                    lines.append(
                        f"  ! Wiki 跳过 {item['source']}: {item['reason']}"
                    )
            if wiki.get("missing_document_records"):
                lines.append(
                    "  无 Wiki documents 记录的来源: "
                    + "、".join(wiki["missing_document_records"])
                )

    lines.append("")
    if apply:
        if result.get("row_count_unchanged", True):
            lines.append("行数前后一致；已调用 table.optimize()。")
        else:
            lines.append(
                f"[ERROR] 行数发生变化: {result['rows_before']} → {rows_after}"
            )
    else:
        lines.append("（dry-run 结束，未写入任何内容）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 my_docs 旧 basename source 重指向 vault 相对路径")
    parser.add_argument("--db", default="", help="LanceDB 库路径（默认 kb.config）")
    parser.add_argument("--vault", default="", help="vault 根目录（默认 kb.config）")
    parser.add_argument("--wiki-db", default="",
                        help="Wiki SQLite（默认 WIKI_DB_PATH 或 <vault>/.wiki/wiki.sqlite3）")
    parser.add_argument("--apply", action="store_true", help="执行更新（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写入（默认）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--mark-missing", action="store_true",
                        help="无匹配来源对应的 documents.status 置 missing")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        print("--apply 与 --dry-run 不能同时使用", file=sys.stderr)
        return 2
    apply = bool(args.apply)

    db_path = os.path.abspath(args.db or cfg.resolve_db_path())
    vault = os.path.abspath(args.vault or cfg.resolve_vault_root())
    wiki_db = _resolve_wiki_db(vault, args.wiki_db)
    paper_dir = Path(vault) / PAPER_REL_DIR

    try:
        import lancedb
    except Exception as exc:  # 测试环境缺少 lancedb 时明确报错
        print(f"[ERROR] 无法导入 lancedb: {exc}", file=sys.stderr)
        return 1

    try:
        db = lancedb.connect(db_path)
        if cfg.TABLE_NAME not in db.table_names():
            print(f"[ERROR] 表不存在: {cfg.TABLE_NAME}（{db_path}）", file=sys.stderr)
            return 1
        table = db.open_table(cfg.TABLE_NAME)
        rows_before = _row_count(table)
        counts = _all_sources(table)
    except Exception as exc:
        print(f"[ERROR] 无法读取 LanceDB: {exc}", file=sys.stderr)
        return 1

    scanned = _scan_sources(counts, vault, paper_dir)
    all_norm = {_norm_source(source) for source in counts}

    # LanceDB 目标 source 已存在（且不是自身）时拒绝合并，避免制造重复来源。
    conflicts = []
    relinkable = []
    for item in scanned["relinkable"]:
        new_norm = _norm_source(item["new_source"])
        if new_norm in all_norm and new_norm != _norm_source(item["source"]):
            conflicts.append({
                "source": item["source"],
                "reason": "target_source_already_present",
                "detail": item["new_source"],
            })
        else:
            relinkable.append(item)
    scanned["relinkable"] = relinkable
    scanned["conflicts"] = conflicts

    # Wiki 计划：先只读匹配 documents；冲突的条目不进入 apply。
    wiki_path = wiki_db
    wiki_stats: dict = {
        "path": wiki_path,
        "available": False,
        "documents_to_relink": 0,
        "documents_to_mark_missing": 0,
        "documents_updated": 0,
        "chunk_refs_updated": 0,
        "documents_marked_missing": 0,
        "conflicts": [],
        "missing_document_records": [],
    }
    planned_relink: list[tuple[dict, list[str]]] = []
    planned_missing: list[str] = []
    chunk_refs_available = False
    conn = _connect_wiki(wiki_path, write=False)
    if conn is None:
        wiki_stats["error"] = "不存在"
    else:
        try:
            if not _wiki_has_table(conn, "documents"):
                wiki_stats["error"] = "缺少 documents 表"
            else:
                wiki_stats["available"] = True
                chunk_refs_available = _wiki_has_table(conn, "chunk_refs")
                effective_relinkable = []
                for item in scanned["relinkable"]:
                    docs = _wiki_documents_for_source(conn, item["source"])
                    if not docs:
                        wiki_stats["missing_document_records"].append(
                            item["source"])
                        effective_relinkable.append(item)
                        continue
                    document_ids = {str(doc["document_id"]) for doc in docs}
                    if _wiki_canonical_conflict(
                            conn, item["new_source"], document_ids):
                        wiki_stats["conflicts"].append({
                            "source": item["source"],
                            "reason": "wiki_canonical_uri_conflict",
                        })
                        continue
                    planned_relink.append(
                        (item, [str(doc["document_id"]) for doc in docs]))
                    wiki_stats["documents_to_relink"] += len(document_ids)
                    effective_relinkable.append(item)
                scanned["relinkable"] = effective_relinkable
                for item in scanned["no_match"]:
                    docs = _wiki_documents_for_source(conn, item["source"])
                    planned_missing.extend(
                        str(doc["document_id"]) for doc in docs
                        if str(doc["status"]) != "missing"
                    )
                wiki_stats["documents_to_mark_missing"] = len(planned_missing)
        except sqlite3.Error as exc:
            wiki_stats["available"] = False
            wiki_stats["error"] = str(exc)
        finally:
            conn.close()

    result = {
        "mode": "apply" if apply else "dry-run",
        "db": db_path,
        "vault": vault,
        "paper_dir": str(paper_dir),
        "wiki_db": wiki_path,
        "rows_before": rows_before,
        "rows_after": rows_before,
        "row_count_unchanged": True,
        **scanned,
        "wiki": wiki_stats,
    }

    if not apply:
        result["rows_after"] = rows_before
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(_markdown_report(result, False, wiki_stats, None))
        return 0

    # apply：先更新 LanceDB source，成功后再同步 Wiki，避免 Wiki 先改而 Lance 失败。
    applied = 0
    applied_sources: set[str] = set()
    for item in scanned["relinkable"]:
        where = f"source = {_sql_literal(item['source'])}"
        try:
            table.update(where=where, values={"source": item["new_source"]})
        except Exception as exc:
            item["error"] = str(exc)
            print(f"[ERROR] 更新失败 {item['source']}: {exc}", file=sys.stderr)
            break
        applied += 1
        applied_sources.add(item["source"])
    if applied:
        try:
            table.optimize()
        except Exception as exc:
            print(f"[WARN] table.optimize() 失败: {exc}", file=sys.stderr)

    if (planned_relink or (args.mark_missing and planned_missing)) and wiki_stats["available"]:
        write_conn = _connect_wiki(wiki_path, write=True)
        if write_conn is None:
            wiki_stats["error"] = "写入时无法打开 Wiki DB"
        else:
            try:
                if planned_relink and not chunk_refs_available:
                    wiki_stats["error"] = "缺少 chunk_refs 表，未写 Wiki"
                    print("[WARN] Wiki 缺少 chunk_refs 表，跳过 documents/chunk_refs 同步",
                          file=sys.stderr)
                else:
                    with write_conn:
                        for item, document_ids in planned_relink:
                            if item["source"] not in applied_sources:
                                continue
                            stats = _wiki_apply_relink(
                                write_conn, document_ids,
                                item["source"], item["new_source"])
                            wiki_stats["documents_updated"] += stats["documents_updated"]
                            wiki_stats["chunk_refs_updated"] += stats["chunk_refs_updated"]
                        if args.mark_missing:
                            wiki_stats["documents_marked_missing"] = \
                                _wiki_apply_missing(write_conn, planned_missing)
            except sqlite3.Error as exc:
                wiki_stats["error"] = str(exc)
                print(f"[WARN] Wiki 同步失败: {exc}", file=sys.stderr)
            finally:
                write_conn.close()

    rows_after = _row_count(table)
    result["rows_after"] = rows_after
    result["row_count_unchanged"] = rows_after == rows_before
    result["applied"] = applied
    if not result["row_count_unchanged"]:
        print(
            f"[ERROR] 行数发生变化: {rows_before} → {rows_after}",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_markdown_report(result, True, wiki_stats, rows_after))
    return 0 if result["row_count_unchanged"] else 2


if __name__ == "__main__":
    sys.exit(main())
