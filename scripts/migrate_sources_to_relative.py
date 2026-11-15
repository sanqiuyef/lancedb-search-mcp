"""把 my_docs 的 legacy basename source 改写为 vault 相对路径。

不重算向量：只更新 ``source`` 列（LanceDB ``table.update``），随后 optimize。
默认 dry-run；``--apply`` 才写库。匹配规则：basename 在 ``--root`` 下唯一命中
且扩展名在白名单内；多义或无命中一律跳过并在报告中列出。

用法：
    python -X utf8 scripts/migrate_sources_to_relative.py --root D:\\...\\vault
    python -X utf8 scripts/migrate_sources_to_relative.py --root D:\\...\\vault --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb import config as cfg  # noqa: E402

EXTS = {".md", ".pdf", ".docx", ".txt"}


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _scan_sources(db_path: str) -> tuple[list[str], int]:
    import lancedb

    db = lancedb.connect(db_path)
    table = db.open_table(cfg.TABLE_NAME)
    total = table.count_rows()
    arrow = table.to_lance().to_table(columns=["source"])
    sources = sorted({str(s) for s in arrow.column("source").to_pylist() if s})
    return sources, total


def _build_index(root: Path) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in EXTS:
            continue
        index[path.name].append(os.path.normpath(str(path.relative_to(root))))
    return index


def plan(root: Path, sources: list[str]) -> dict:
    index = _build_index(root)
    rewritable: list[dict] = []
    ambiguous: list[dict] = []
    unmatched: list[str] = []
    already_relative: list[str] = []
    for source in sources:
        if "/" in source or "\\" in source:
            already_relative.append(source)
            continue
        matches = index.get(source, [])
        if len(matches) == 1:
            rewritable.append({"old": source, "new": matches[0]})
        elif len(matches) > 1:
            ambiguous.append({"source": source, "matches": matches[:5]})
        else:
            unmatched.append(source)
    return {
        "root": str(root),
        "sources": len(sources),
        "already_relative": len(already_relative),
        "rewritable": len(rewritable),
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "rewritable_items": rewritable,
    }


def apply_plan(db_path: str, items: list[dict]) -> dict:
    import lancedb

    db = lancedb.connect(db_path)
    table = db.open_table(cfg.TABLE_NAME)
    before = table.count_rows()
    updated = 0
    for item in items:
        where = f"source = '{_sql_quote(item['old'])}'"
        result = table.update(where=where, values={"source": item["new"]})
        updated += int(getattr(result, "num_updated_rows", 0) or 0)
    table.optimize()
    after = table.count_rows()
    return {"rows_before": before, "rows_after": after, "rows_updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description="legacy source -> vault 相对路径")
    parser.add_argument("--db", default=None, help="LanceDB 目录（默认按 kb.config）")
    parser.add_argument("--root", required=True, help="相对路径的根目录（通常是 vault）")
    parser.add_argument("--apply", action="store_true", help="实际写入；默认 dry-run")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 条，便于试跑")
    args = parser.parse_args()

    db_path = args.db or cfg.resolve_db_path()
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[ERROR] root 不存在: {root}", file=sys.stderr)
        return 2

    sources, total = _scan_sources(db_path)
    plan_result = plan(root, sources)
    items = plan_result["rewritable_items"]
    if args.limit > 0:
        items = items[: args.limit]
    plan_result["rewritable_items"] = items
    plan_result["planned"] = len(items)

    output: dict = {"mode": "apply" if args.apply else "dry-run", "rows": total, **plan_result}
    if args.apply:
        output["apply"] = apply_plan(db_path, items)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"mode={output['mode']} rows={total} sources={plan_result['sources']}")
        print(f"already_relative={plan_result['already_relative']} rewritable={plan_result['rewritable']} "
              f"ambiguous={len(plan_result['ambiguous'])} unmatched={len(plan_result['unmatched'])}")
        for item in items[:10]:
            print(f"  {item['old']} -> {item['new']}")
        if len(items) > 10:
            print(f"  ... {len(items) - 10} more")
        if plan_result["unmatched"]:
            print("unmatched:")
            for source in plan_result["unmatched"]:
                print(f"  {source}")
        if plan_result["ambiguous"]:
            print("ambiguous:")
            for entry in plan_result["ambiguous"]:
                print(f"  {entry['source']}: {entry['matches']}")
        if args.apply:
            print(f"apply={output['apply']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
