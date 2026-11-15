# -*- coding: utf-8 -*-
"""Wiki SQLite 与提案目录备份。

- 用 SQLite backup API 生成一致性快照，默认输出到
  ``<db_dir>/backups/wiki-<utcstamp>.sqlite3``；
- 若 ``<db_dir>/proposals`` 存在，同时打包为 ``proposals-<utcstamp>.tar.gz``；
- 在输出目录写 ``manifest.json``：时间、源库/备份大小与 sha256、提案归档信息、
  vault git HEAD 与 status 摘要；
- 只做本地备份，不执行 git push，不修改源库。

用法：
    python -X utf8 scripts/backup_wiki.py
    python -X utf8 scripts/backup_wiki.py --db <wiki.sqlite3> --out <备份文件或目录>
    python -X utf8 scripts/backup_wiki.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb.wiki import constants as C  # noqa: E402
from kb.wiki.config import WikiConfig, WikiConfigError  # noqa: E402


class BackupError(RuntimeError):
    """备份输入或执行失败。"""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_info(path: str) -> dict[str, Any]:
    return {
        "path": os.path.abspath(path),
        "size_bytes": os.path.getsize(path),
        "sha256": sha256_file(path),
    }


def _readonly_connection(path: str) -> sqlite3.Connection:
    """优先以 read-only URI 打开；驱动不支持时回退普通连接（仍不写数据）。"""
    uri_path = Path(path).resolve().as_posix()
    uri = "file:" + urllib.parse.quote(uri_path, safe="/:") + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(path)


def _schema_versions(path: str) -> list[str]:
    try:
        conn = _readonly_connection(path)
    except sqlite3.Error:
        return []
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = ?",
            (C.SCHEMA_MIGRATIONS_TABLE,),
        ).fetchone()
        if row is None:
            return []
        return [
            str(record[0])
            for record in conn.execute(
                f"SELECT version FROM {C.SCHEMA_MIGRATIONS_TABLE} "
                "ORDER BY version"
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _backup_database(source_db: str, output_db: str) -> dict[str, Any]:
    if os.path.exists(output_db):
        raise BackupError(f"输出文件已存在，拒绝覆盖: {output_db}")
    os.makedirs(os.path.dirname(os.path.abspath(output_db)), exist_ok=True)

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = _readonly_connection(source_db)
        destination = sqlite3.connect(output_db)
        source.backup(destination)
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite backup API 失败: {exc}") from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()

    try:
        check_conn = sqlite3.connect(output_db)
        try:
            check = check_conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            check_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"备份文件无法打开: {exc}") from exc
    integrity = str(check[0]) if check is not None else "unknown"
    if integrity != "ok":
        raise BackupError(f"备份文件 integrity_check 未通过: {integrity}")

    info = _file_info(output_db)
    info["integrity_check"] = integrity
    info["schema_versions"] = _schema_versions(output_db)
    return info


def _count_files(directory: str) -> int:
    total = 0
    for _root, _dirs, files in os.walk(directory):
        total += sum(1 for name in files if os.path.isfile(os.path.join(_root, name)))
    return total


def _archive_proposals(
    proposals_dir: str, archive_path: str
) -> dict[str, Any] | None:
    if not os.path.isdir(proposals_dir):
        return None
    os.makedirs(os.path.dirname(os.path.abspath(archive_path)), exist_ok=True)
    if os.path.exists(archive_path):
        raise BackupError(f"提案归档已存在，拒绝覆盖: {archive_path}")
    try:
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(proposals_dir, arcname="proposals")
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"提案目录打包失败: {exc}") from exc
    info = _file_info(archive_path)
    info["source_dir"] = os.path.abspath(proposals_dir)
    info["file_count"] = _count_files(proposals_dir)
    return info


def _run_git(vault: str, *args: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", vault, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout, result.stderr


def vault_git_summary(vault: str) -> dict[str, Any]:
    """返回 vault 的 git HEAD 与 status 摘要；非 git 仓库只记录原因。"""
    summary: dict[str, Any] = {
        "path": os.path.abspath(vault),
        "exists": os.path.isdir(vault),
        "is_git_repo": False,
        "head": None,
        "branch": None,
        "status_summary": {
            "dirty": False,
            "changed_files": 0,
            "untracked": 0,
            "modified": 0,
            "added": 0,
            "deleted": 0,
            "renamed": 0,
            "conflicted": 0,
        },
        "status_sample": [],
    }
    if not summary["exists"]:
        summary["error"] = "vault 目录不存在"
        return summary

    code, stdout, stderr = _run_git(vault, "rev-parse", "--is-inside-work-tree")
    if code != 0 or stdout.strip() != "true":
        summary["error"] = (stderr.strip() or "不是 git 仓库")
        return summary
    summary["is_git_repo"] = True

    head_code, head_out, head_err = _run_git(vault, "rev-parse", "HEAD")
    summary["head"] = head_out.strip() if head_code == 0 else None
    if head_code != 0:
        summary["head_error"] = head_err.strip()
    branch_code, branch_out, _ = _run_git(
        vault, "rev-parse", "--abbrev-ref", "HEAD"
    )
    summary["branch"] = branch_out.strip() if branch_code == 0 else None

    status_code, status_out, status_err = _run_git(
        vault, "status", "--porcelain=v1", "--untracked-files=normal"
    )
    if status_code != 0:
        summary["status_error"] = status_err.strip()
        return summary

    lines = [line for line in status_out.splitlines() if line.strip()]
    counts = summary["status_summary"]
    conflicted_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    for line in lines:
        code = line[:2]
        if code == "??":
            counts["untracked"] += 1
            continue
        if code in conflicted_codes:
            counts["conflicted"] += 1
            continue
        if "R" in code:
            counts["renamed"] += 1
        if "A" in code:
            counts["added"] += 1
        if "D" in code:
            counts["deleted"] += 1
        if "M" in code:
            counts["modified"] += 1
    counts["changed_files"] = len(lines)
    counts["dirty"] = bool(lines)
    summary["status_sample"] = lines[:50]
    if len(lines) > 50:
        summary["status_truncated"] = True
    return summary


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".manifest.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
        raise


def _resolve_paths(args: argparse.Namespace) -> tuple[str, str]:
    try:
        env_config = WikiConfig.from_env()
    except WikiConfigError as exc:
        raise BackupError(f"读取 Wiki 配置失败: {exc}") from exc

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
    return vault, db_path


def _resolve_output(out: str | None, db_path: str, stamp: str) -> str:
    if out is None:
        return os.path.join(
            os.path.dirname(db_path), "backups", f"wiki-{stamp}.sqlite3"
        )
    raw = os.path.expanduser(out)
    absolute = os.path.abspath(raw)
    if os.path.isdir(absolute) or out.endswith(("/", "\\")):
        return os.path.join(absolute, f"wiki-{stamp}.sqlite3")
    return absolute


def run_backup(
    db_path: str,
    vault_path: str,
    out: str | None = None,
) -> dict[str, Any]:
    """执行备份并返回 manifest；失败时抛 BackupError。"""
    source_db = os.path.abspath(os.path.expanduser(db_path))
    if not os.path.exists(source_db):
        raise BackupError(f"源库不存在: {source_db}")
    if not os.path.isfile(source_db):
        raise BackupError(f"源库不是文件: {source_db}")

    stamp = utc_stamp()
    output_db = _resolve_output(out, source_db, stamp)
    output_dir = os.path.dirname(os.path.abspath(output_db))
    manifest_path = os.path.join(output_dir, "manifest.json")
    proposals_dir = os.path.join(os.path.dirname(source_db), "proposals")
    proposals_archive = os.path.join(
        output_dir, f"proposals-{stamp}.tar.gz"
    )

    created: list[str] = []
    try:
        source_info = _file_info(source_db)
        backup_info = _backup_database(source_db, output_db)
        created.append(output_db)

        proposals_info = _archive_proposals(proposals_dir, proposals_archive)
        if proposals_info is not None:
            created.append(proposals_archive)

        manifest = {
            "format_version": 1,
            "created_at": utc_now(),
            "source_db": source_info,
            "backup": backup_info,
            "proposals": proposals_info,
            "vault": vault_git_summary(vault_path),
            "manifest_path": os.path.abspath(manifest_path),
        }
        _write_json_atomic(manifest_path, manifest)
        created.append(manifest_path)
        return manifest
    except BaseException:
        for path in reversed(created):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        raise


def _render_text(manifest: dict[str, Any]) -> str:
    backup = manifest["backup"]
    proposals = manifest.get("proposals")
    vault = manifest.get("vault", {})
    lines = [
        "Wiki 备份完成",
        f"  时间: {manifest['created_at']}",
        f"  源库: {manifest['source_db']['path']} "
        f"({manifest['source_db']['size_bytes']} bytes, "
        f"sha256={manifest['source_db']['sha256']})",
        f"  备份: {backup['path']} ({backup['size_bytes']} bytes, "
        f"sha256={backup['sha256']}, integrity={backup['integrity_check']})",
        "  提案: "
        + (
            f"{proposals['path']} ({proposals['file_count']} files)"
            if proposals
            else "未找到 proposals 目录，已跳过"
        ),
        f"  清单: {manifest['manifest_path']}",
        "  vault git: "
        + (
            f"HEAD={vault.get('head')} branch={vault.get('branch')} "
            f"dirty={vault.get('status_summary', {}).get('dirty')}"
            if vault.get("is_git_repo")
            else f"不可用（{vault.get('error', '未知原因')}）"
        ),
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用 SQLite backup API 备份 Wiki 数据库与 proposals 目录",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Wiki SQLite 路径（默认 WIKI_DB_PATH 或 <vault>/.wiki/wiki.sqlite3）",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="vault 路径（用于默认库路径与 git 摘要，默认 WIKI_VAULT_PATH）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="备份文件路径或目录（默认 <db_dir>/backups/wiki-<utcstamp>.sqlite3）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出完整 manifest JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        vault_path, db_path = _resolve_paths(args)
        manifest = run_backup(db_path, vault_path, out=args.out)
    except BackupError as exc:
        print(f"backup_wiki: error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_text(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
