# -*- coding: utf-8 -*-
"""重建 Wiki 页面索引（LanceDB ``wiki_pages``）。

默认增量：只重索引 revision 发生变化的 published 页面，并清理已不再 published
的陈旧索引行；``--drop`` 才会显式删除整张索引表后重建。任何页面失败都会继续
处理后续页面，最后输出统计与失败清单。

用法（项目根目录）：
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_page_index.py --dry-run
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_page_index.py
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_page_index.py --drop
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_page_index.py \\
      --db D:/vault/.wiki/wiki.sqlite3 --vault D:/vault \\
      --lance-db D:/knowledge-hub/lancedb
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# 批处理进程关闭闲置卸载看门狗，规避 Python 3.13 + torch 的多线程状态崩溃。
os.environ.setdefault("LOCAL_MODEL_IDLE_UNLOAD", "0")
os.environ.setdefault("TQDM_DISABLE", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kb.wiki import config as wiki_config  # noqa: E402
from kb.wiki import db as wiki_db  # noqa: E402
from kb.wiki import page_index  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="重建 Wiki 页面索引（默认增量，不清表）"
    )
    parser.add_argument(
        "--db",
        "--wiki-db",
        dest="db_path",
        default="",
        help="wiki SQLite 路径（缺省读 WIKI_DB_PATH / WikiConfig 默认值）",
    )
    parser.add_argument(
        "--vault",
        default="",
        help="vault 根路径（缺省读 WIKI_VAULT_PATH；用于定位页面 Markdown）",
    )
    parser.add_argument(
        "--lance-db",
        dest="lance_db",
        default="",
        help="LanceDB 库路径（缺省读 LANCEDB_DB_PATH / kb.config 默认值）",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="先删除整张页面索引表再重建（显式指定才执行）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计 published 页面与索引状态，不写文件/不建表",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 revision 未变化，全部重新索引",
    )
    return parser


def _resolve_config(args: argparse.Namespace) -> wiki_config.WikiConfig:
    env = dict(os.environ)
    if args.vault:
        env["WIKI_VAULT_PATH"] = args.vault
    if args.db_path:
        env["WIKI_DB_PATH"] = args.db_path
    return wiki_config.WikiConfig.from_env(env)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_pages_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'pages'"
    ).fetchone()
    return row is not None


def _published_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE status = 'published'"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _print_status(status: dict) -> None:
    print(f"  索引表: {status['table']}")
    print(f"  表存在: {status['exists']} | 行数: {status['row_count']}")
    published = status.get("published_count")
    print(
        "  published 页面数: "
        + ("未知（未连接 SQLite）" if published is None else str(published))
    )
    missing = status.get("missing_page_ids") or []
    stale = status.get("stale_page_ids") or []
    print(f"  缺失索引页面: {len(missing)}{(' ' + str(missing[:10])) if missing else ''}")
    print(f"  陈旧索引页面: {len(stale)}{(' ' + str(stale[:10])) if stale else ''}")
    if status.get("error"):
        print(f"  状态读取警告: {status['error']}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = _resolve_config(args)
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    if not os.path.isfile(config.db_path):
        print(f"wiki SQLite 不存在: {config.db_path}", file=sys.stderr)
        return 1

    conn = None
    try:
        conn = (
            _open_readonly(config.db_path)
            if args.dry_run
            else wiki_db.connect(config.db_path, config)
        )
        if not _has_pages_table(conn):
            print(
                f"wiki SQLite 尚未迁移（缺少 pages 表）: {config.db_path}",
                file=sys.stderr,
            )
            return 1

        published = _published_count(conn)
        lance_path = page_index.resolve_index_db_path(args.lance_db or None)
        print(f"Wiki SQLite: {config.db_path}")
        print(f"LanceDB: {lance_path}")
        print(f"published 页面: {published}")

        if args.dry_run:
            if args.drop:
                print("dry-run：忽略 --drop（未删除任何表）")
            status = page_index.index_status(
                conn, config, db_path=args.lance_db or None
            )
            _print_status(status)
            print("（dry-run 结束，未写入任何数据）")
            return 0

        if args.drop:
            dropped = page_index.drop_page_index(
                config, db_path=args.lance_db or None
            )
            print(f"--drop: {'已删除页面索引表' if dropped else '索引表不存在，无需删除'}")

        stats = page_index.rebuild_all(
            conn,
            config,
            db_path=args.lance_db or None,
            force=args.force,
            prune_stale=True,
        )
        print(
            "重建完成: "
            f"published={stats['total_published']} "
            f"indexed={stats['indexed']} "
            f"skipped={stats['skipped']} "
            f"failed={stats['failed']} "
            f"removed_stale={stats['removed_stale']} "
            f"rows={stats['row_count']}"
        )
        if stats["failures"]:
            print("失败页面:")
            for failure in stats["failures"]:
                slug = failure.get("slug") or "-"
                print(
                    f"  - {failure.get('page_id', '')} ({slug}): "
                    f"{failure.get('error', '')}"
                )
            return 1
        return 0
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"重建失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
