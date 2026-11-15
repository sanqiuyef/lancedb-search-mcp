# -*- coding: utf-8 -*-
"""Wiki 一致性校验 CLI：validator.validate + recovery.scan_journal。

只读打开 SQLite（``mode=ro``），不执行迁移、不修复问题、不写 vault。
默认输出 Markdown；``--json`` 输出结构化结果；``--strict`` 时存在 error 级
问题则退出码 1。运行失败（库缺失/未迁移等）退出码 2。

用法：
    python -X utf8 scripts/validate_wiki.py
    python -X utf8 scripts/validate_wiki.py --db <wiki.sqlite3> --json --strict
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb.wiki import constants as C  # noqa: E402
from kb.wiki import db as wiki_db  # noqa: E402
from kb.wiki import recovery  # noqa: E402
from kb.wiki import validator  # noqa: E402
from kb.wiki.config import WikiConfig, WikiConfigError  # noqa: E402
from kb.wiki.models import WriteJournalRecord  # noqa: E402


class ValidateError(RuntimeError):
    """校验运行环境不满足要求。"""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _resolve_paths(
    args: argparse.Namespace,
) -> tuple[WikiConfig, str, str]:
    try:
        env_config = WikiConfig.from_env()
    except WikiConfigError as exc:
        raise ValidateError(f"读取 Wiki 配置失败: {exc}") from exc

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
    """继承未显式覆盖的环境配置；覆盖 vault 时重置相对 page_root。"""
    if args.vault:
        return replace(
            env_config,
            vault_path=vault_path,
            page_root=C.DEFAULT_PAGE_ROOT,
            db_path=db_path,
        )
    return replace(env_config, db_path=db_path)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise ValidateError(f"Wiki 数据库不存在: {db_path}")
    if not os.path.isfile(db_path):
        raise ValidateError(f"Wiki 数据库路径不是文件: {db_path}")
    uri_path = Path(db_path).resolve().as_posix()
    uri = "file:" + urllib.parse.quote(uri_path, safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ValidateError(f"无法只读打开 Wiki 数据库: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _load_detail(text: str) -> Any:
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return text


def _journal_dict(record: WriteJournalRecord) -> dict[str, Any]:
    return {
        "journal_id": record.journal_id,
        "page_id": record.page_id,
        "proposal_id": record.proposal_id,
        "revision_id": record.revision_id,
        "target_path": record.target_path,
        "state": str(record.state),
        "updated_at": record.updated_at,
        "error_message": record.error_message,
        "detail": _load_detail(record.detail_json),
    }


def _collect(conn: sqlite3.Connection, config: WikiConfig) -> dict[str, Any]:
    issues = validator.validate(conn, config)
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue["level"]] = counts.get(issue["level"], 0) + 1
    counts["total"] = len(issues)

    scanned = recovery.scan_journal(conn)
    journal: dict[str, Any] = {
        state: [_journal_dict(record) for record in scanned[state]]
        for state in recovery.OPEN_JOURNAL_STATES
    }
    journal["total"] = sum(
        len(journal[state]) for state in recovery.OPEN_JOURNAL_STATES
    )
    return {
        "generated_at": utc_now(),
        "db_path": config.db_path,
        "vault_path": config.vault_path,
        "ok": counts["error"] == 0,
        "counts": counts,
        "issues": issues,
        "journal": journal,
    }


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_markdown(result: dict[str, Any], strict: bool) -> str:
    counts = result["counts"]
    status = "通过" if counts["error"] == 0 else "未通过"
    lines = [
        "# Wiki 校验报告",
        "",
        f"- 生成时间: {result['generated_at']}",
        f"- 数据库: `{result['db_path']}`",
        f"- vault: `{result['vault_path']}`",
        f"- 状态: {status}（error={counts['error']}, "
        f"warning={counts['warning']}, info={counts['info']}）",
        f"- strict 模式: {'是' if strict else '否'}",
        "",
        "## 问题统计",
        "",
        "| 级别 | 数量 |",
        "|---|---:|",
        f"| error | {counts['error']} |",
        f"| warning | {counts['warning']} |",
        f"| info | {counts['info']} |",
        f"| 合计 | {counts['total']} |",
        "",
        "## 问题列表",
        "",
    ]
    if result["issues"]:
        lines.extend(
            [
                "| 级别 | code | page_id | 说明 |",
                "|---|---|---|---|",
            ]
        )
        for issue in result["issues"]:
            lines.append(
                f"| {_cell(issue['level'])} | {_cell(issue['code'])} | "
                f"{_cell(issue['page_id'] or '-')} | {_cell(issue['message'])} |"
            )
    else:
        lines.append("未发现问题。")

    lines.extend(["", "## 未完成写入日志", ""])
    journal = result["journal"]
    if journal["total"] == 0:
        lines.append("无未完成写入日志。")
    else:
        lines.append(f"共 {journal['total']} 条非 completed journal：")
        lines.append("")
        lines.extend(
            [
                "| state | journal_id | page_id | target_path | updated_at | 错误 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for state in recovery.OPEN_JOURNAL_STATES:
            for item in journal[state]:
                lines.append(
                    f"| {_cell(state)} | {_cell(item['journal_id'])} | "
                    f"{_cell(item['page_id'] or '-')} | "
                    f"{_cell(item['target_path'])} | "
                    f"{_cell(item['updated_at'])} | "
                    f"{_cell(item['error_message'] or '-')} |"
                )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读校验 Wiki SQLite 元数据、vault 页面与写入日志",
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
        "--strict",
        action="store_true",
        help="存在 error 级问题时退出码 1",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        env_config, vault_path, db_path = _resolve_paths(args)
        config = _build_config(env_config, args, vault_path, db_path)
        conn = _open_readonly(db_path)
        applied = wiki_db.db_status(conn)
        if not applied:
            raise ValidateError(
                f"数据库未执行 Wiki 迁移: {db_path}；"
                "先运行迁移或设置正确的 --db"
            )
        result = _collect(conn, config)
    except (ValidateError, WikiConfigError, recovery.RecoveryError) as exc:
        print(f"validate_wiki: error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"validate_wiki: error: 读取数据库失败: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    if args.json or args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(result, args.strict), end="")

    if args.strict and result["counts"]["error"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
