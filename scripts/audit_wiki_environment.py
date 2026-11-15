"""Wiki V1 前置审计：只读检查运行环境、LanceDB 表结构与 source 形态。

严格只读：仅 connect / list_tables / open_table / to_lance / to_table，
不调用任何 add、delete、create_index、optimize、drop 或 update。

用法：
    python -X utf8 scripts/audit_wiki_environment.py
    python -X utf8 scripts/audit_wiki_environment.py --out docs/wiki-v1/repository-audit.md
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb import config as cfg  # noqa: E402
from kb.wiki import constants as wiki_constants  # noqa: E402


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "NOT INSTALLED"


def _table_stats(db, table_name: str) -> dict:
    if table_name not in db.list_tables().tables:
        return {"exists": False}
    table = db.open_table(table_name)
    info: dict = {"exists": True, "rows": table.count_rows(), "schema": {}}
    arrow_schema = table.schema
    for field in arrow_schema:
        info["schema"][field.name] = str(field.type)
    try:
        info["indices"] = [
            {"name": idx.name, "columns": idx.columns, "type": str(idx.index_type)}
            for idx in table.list_indices()
        ]
    except Exception as exc:  # pragma: no cover - 仅审计脚本
        info["indices_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _docs_stats(db) -> dict:
    """统计 my_docs 的 source 形态，用 lance 投影避免把向量读进内存。"""
    table = db.open_table(cfg.TABLE_NAME)
    total = table.count_rows()
    if total == 0:
        return {"rows": 0}

    dataset = table.to_lance()
    columns = ["source", "chunk_index", "doc_id", "ingested_at", "category"]
    arrow = dataset.to_table(columns=columns)
    sources = arrow.column("source").to_pylist()
    indices = arrow.column("chunk_index").to_pylist()
    doc_ids = arrow.column("doc_id").to_pylist()
    ingested = arrow.column("ingested_at").to_pylist()
    categories = arrow.column("category").to_pylist()

    def blank(value) -> bool:
        return value is None or str(value).strip() == ""

    source_counter = Counter(str(s) for s in sources if not blank(s))
    pair_counter = Counter(
        (str(s), i) for s, i in zip(sources, indices) if not blank(s)
    )
    doc_counter = Counter(str(d) for d in doc_ids if not blank(d))
    with_sep = [s for s in source_counter if "\\" in s or "/" in s]
    web_sources = [s for s in source_counter if s.startswith("网页-")]

    # 同一 source 是否出现多个 category
    source_categories: dict[str, set[str]] = {}
    for source, category in zip(sources, categories):
        if blank(source):
            continue
        source_categories.setdefault(str(source), set()).add(str(category or ""))
    multi_category = [
        {"source": s, "categories": sorted(cats)}
        for s, cats in source_categories.items()
        if len(cats) > 1
    ]

    ingested_clean = sorted(str(v) for v in ingested if not blank(v))
    return {
        "rows": total,
        "distinct_sources": len(source_counter),
        "sources_with_path_separator": len(with_sep),
        "sources_with_path_separator_ratio": round(len(with_sep) / max(len(source_counter), 1), 4),
        "basename_only_sources": len(source_counter) - len(with_sep),
        "web_sources": len(web_sources),
        "web_source_max_rows": max((source_counter[s] for s in web_sources), default=0),
        "empty_source_rows": sum(1 for s in sources if blank(s)),
        "empty_doc_id_rows": sum(1 for d in doc_ids if blank(d)),
        "duplicate_source_chunk_pairs": {
            "groups": sum(1 for count in pair_counter.values() if count > 1),
            "rows": sum(count for count in pair_counter.values() if count > 1),
        },
        "duplicate_doc_ids": {
            "groups": sum(1 for count in doc_counter.values() if count > 1),
            "rows": sum(count for count in doc_counter.values() if count > 1),
        },
        "ingested_at_min": ingested_clean[0] if ingested_clean else "",
        "ingested_at_max": ingested_clean[-1] if ingested_clean else "",
        "top_sources": source_counter.most_common(20),
        "top_categories": Counter(str(c or "") for c in categories).most_common(10),
        "sources_with_multiple_categories": len(multi_category),
        "multiple_category_examples": multi_category[:3],
        "sample_sources": [s for s, _ in source_counter.most_common(5)],
    }


def _vault_info() -> dict:
    vault = Path(os.environ.get("WIKI_VAULT_PATH") or r"D:\cherry-workplace\knowledge-hub\vault")
    wiki_root = vault / "wiki-维基"
    return {
        "vault_path": str(vault),
        "vault_exists": vault.is_dir(),
        "obsidian_marker": (vault / ".obsidian").is_dir(),
        "is_git_repo": (vault / ".git").exists(),
        "wiki_root": str(wiki_root),
        "wiki_root_exists": wiki_root.is_dir(),
        "wiki_subdirs": sorted(p.name for p in wiki_root.iterdir() if p.is_dir())
        if wiki_root.is_dir()
        else [],
        "existing_md_count": len(list(wiki_root.rglob("*.md"))) if wiki_root.is_dir() else 0,
    }


def _wiki_db_stats() -> dict:
    """只读统计 Wiki SQLite（不创建库、不执行迁移、不写任何表）。"""
    raw = os.environ.get("WIKI_DB_PATH", "").strip()
    if raw:
        db_path = Path(raw)
    else:
        vault = Path(
            os.environ.get("WIKI_VAULT_PATH") or wiki_constants.DEFAULT_VAULT_PATH
        )
        db_path = (
            vault
            / wiki_constants.DEFAULT_DB_SUBDIR
            / wiki_constants.DEFAULT_DB_FILENAME
        )

    info: dict = {"path": str(db_path), "exists": db_path.is_file()}
    if not info["exists"]:
        return info

    try:
        conn = sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro", uri=True
        )
    except sqlite3.Error as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        info["readable"] = True
        info["migration_versions"] = (
            [
                str(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            if "schema_migrations" in tables
            else []
        )
        counts = {}
        for table in (
            "documents",
            "document_revisions",
            "chunk_refs",
            "claims",
            "claim_evidence",
            "pages",
            "entities",
            "relations",
        ):
            counts[table] = (
                int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if table in tables
                else 0
            )
        info["table_counts"] = counts
        info["document_statuses"] = (
            {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM documents GROUP BY status"
                )
            }
            if "documents" in tables
            else {}
        )
        info["revision_statuses"] = (
            {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM document_revisions GROUP BY status"
                )
            }
            if "document_revisions" in tables
            else {}
        )
    except sqlite3.Error as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return info


def collect() -> dict:
    import lancedb

    db_path = Path(cfg.resolve_db_path())
    result: dict = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": {
            name: _pkg_version(name)
            for name in (
                "lancedb",
                "pyarrow",
                "pydantic",
                "mcp",
                "sentence-transformers",
                "torch",
                "watchdog",
            )
        },
        "db_path": str(db_path),
        "db_exists": db_path.is_dir(),
        "db_size_mb": round(sum(p.stat().st_size for p in db_path.rglob("*") if p.is_file()) / 1024 / 1024, 1)
        if db_path.is_dir()
        else 0,
        "env": {k: v for k, v in os.environ.items() if k.startswith("WIKI_") or k.startswith("LANCEDB_")},
        "vault": _vault_info(),
        "wiki_db": _wiki_db_stats(),
    }
    if not db_path.is_dir():
        return result

    db = lancedb.connect(str(db_path))
    result["tables"] = db.list_tables().tables
    result["table_stats"] = {name: _table_stats(db, name) for name in result["tables"]}
    result["my_docs_source_profile"] = _docs_stats(db)
    return result


def render_markdown(data: dict) -> str:
    lines = ["# Wiki V1 仓库与环境审计", "", "> 由 `scripts/audit_wiki_environment.py` 只读生成。", ""]
    lines += ["## 运行环境", "", f"- Python: `{data['python']}` (`{data['executable']}`)"]
    for name, ver in data["packages"].items():
        lines.append(f"- {name}: `{ver}`")
    lines += ["", "## LanceDB", "", f"- 路径: `{data['db_path']}`", f"- 存在: {data['db_exists']}", f"- 体积: {data['db_size_mb']} MB"]
    if data.get("tables"):
        lines += ["", "| 表 | 行数 | 索引 |", "|---|---|---|"]
        for name, stats in data["table_stats"].items():
            indices = stats.get("indices") or []
            index_text = ", ".join(f"{i['name']}({','.join(i['columns'])})" for i in indices) or "无"
            lines.append(f"| `{name}` | {stats.get('rows', 'N/A')} | {index_text} |")
    profile = data.get("my_docs_source_profile")
    if profile:
        lines += [
            "",
            "## `my_docs` source 形态",
            "",
            f"- 总行数: {profile['rows']}",
            f"- 去重 source: {profile['distinct_sources']}",
            f"- 含路径分隔符的 source: {profile['sources_with_path_separator']} ({profile['sources_with_path_separator_ratio']:.2%})",
            f"- 纯 basename source: {profile['basename_only_sources']}",
            f"- `网页-` source: {profile['web_sources']}，单 source 最大行数: {profile['web_source_max_rows']}",
            f"- 空 source 行: {profile['empty_source_rows']}，空 doc_id 行: {profile['empty_doc_id_rows']}",
            f"- 重复 `(source, chunk_index)`: {profile['duplicate_source_chunk_pairs']['groups']} 组 / {profile['duplicate_source_chunk_pairs']['rows']} 行",
            f"- 重复 `doc_id`: {profile['duplicate_doc_ids']['groups']} 组 / {profile['duplicate_doc_ids']['rows']} 行",
            f"- `ingested_at` 范围: {profile['ingested_at_min']} → {profile['ingested_at_max']}",
            f"- 同一 source 多 category: {profile['sources_with_multiple_categories']} 个",
            "",
            "### 行数最多的 source（前 20）",
            "",
            "| source | chunks |",
            "|---|---|",
        ]
        for source, count in profile["top_sources"]:
            lines.append(f"| `{source}` | {count} |")
    wiki_db = data.get("wiki_db")
    if wiki_db:
        lines += [
            "",
            "## Wiki SQLite",
            "",
            f"- 路径: `{wiki_db.get('path', '')}`",
            f"- 存在: {wiki_db.get('exists', False)}",
        ]
        if wiki_db.get("exists"):
            lines.append(f"- 可读: {wiki_db.get('readable', False)}")
            if wiki_db.get("error"):
                lines.append(f"- 读取错误: `{wiki_db['error']}`")
            versions = wiki_db.get("migration_versions") or []
            lines.append(f"- 迁移版本: {', '.join(versions) if versions else '无'}")
            counts = wiki_db.get("table_counts") or {}
            for table in (
                "documents",
                "document_revisions",
                "chunk_refs",
                "claims",
                "claim_evidence",
                "pages",
                "entities",
                "relations",
            ):
                lines.append(f"- `{table}`: {counts.get(table, 0)}")
            doc_statuses = wiki_db.get("document_statuses") or {}
            if doc_statuses:
                lines.append(
                    "- document 状态: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(doc_statuses.items()))
                )
            revision_statuses = wiki_db.get("revision_statuses") or {}
            if revision_statuses:
                lines.append(
                    "- revision 状态: "
                    + ", ".join(
                        f"{k}={v}" for k, v in sorted(revision_statuses.items())
                    )
                )
    lines += ["", "## Vault", "", "```json", json.dumps(data["vault"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="只读审计 Wiki V1 运行环境")
    parser.add_argument("--out", help="输出 Markdown 报告路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = REPO_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(data), encoding="utf-8")
        print(f"written: {out}")
    if not args.json and not args.out:
        print(render_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
