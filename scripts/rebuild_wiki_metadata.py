# -*- coding: utf-8 -*-
"""扫描 vault wiki 页面并补齐 SQLite pages/page_revisions 元数据。

安全边界：
- 只扫描 ``<vault>/wiki-维基/{concepts-概念,entities-实体,topics-主题}/*.md``；
- **绝不修改用户文件**：缺 page_id 的文件只报告，不自动补写 frontmatter；
- 默认 ``--dry-run``；``--apply`` 只插入缺失的 pages/page_revisions，
  不更新、不删除已有记录；
- DB 有记录但文件缺失时报告 orphan，不删除记录；
- 重复执行幂等：第二次 apply 不会重复登记。

用法：
    python -X utf8 scripts/rebuild_wiki_metadata.py                 # dry-run
    python -X utf8 scripts/rebuild_wiki_metadata.py --json
    python -X utf8 scripts/rebuild_wiki_metadata.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kb.wiki import constants as C  # noqa: E402
from kb.wiki import db as wiki_db  # noqa: E402
from kb.wiki import link_index  # noqa: E402
from kb.wiki.config import WikiConfig, WikiConfigError, WikiPathError, ensure_within  # noqa: E402
from kb.wiki.markdown import (  # noqa: E402
    FrontmatterError,
    FrontmatterValidationError,
    parse_frontmatter,
    validate_page_frontmatter,
)
from kb.wiki.models import PageRecord, PageRevisionRecord  # noqa: E402
from kb.wiki.repositories import pages as pages_repo  # noqa: E402


class RebuildError(RuntimeError):
    """重建输入或执行失败。"""


@dataclass(slots=True)
class ScannedPage:
    path: str
    relative_path: str
    page_type_dir: str
    sha256: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    issue: str = ""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp(value: Any, default: str | None = None) -> str:
    """把 frontmatter 的日期/时间值规整为 UTC ISO8601 文本。"""
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc)
        return (
            moment.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{moment.microsecond // 1000:03d}Z"
        )
    if isinstance(value, date):
        return value.isoformat() + "T00:00:00.000Z"
    text = str(value or "").strip()
    if not text:
        return default or utc_now()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text).isoformat() + "T00:00:00.000Z"
        except ValueError:
            return default or utc_now()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return (
        parsed.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{parsed.microsecond // 1000:03d}Z"
    )


def _scan_file(path: str, relative_path: str, page_type_dir: str) -> ScannedPage:
    with open(path, "rb") as handle:
        raw = handle.read()
    record = ScannedPage(
        path=path,
        relative_path=relative_path,
        page_type_dir=page_type_dir,
        sha256=_sha256_bytes(raw),
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        record.issue = "invalid_frontmatter"
        record.errors = (f"文件不是 UTF-8: {exc}",)
        return record

    try:
        frontmatter, _body = parse_frontmatter(text)
    except FrontmatterError as exc:
        record.issue = "invalid_frontmatter"
        record.errors = (str(exc),)
        return record
    record.frontmatter = frontmatter

    page_id = frontmatter.get("page_id")
    if not isinstance(page_id, str) or not page_id.strip():
        record.issue = "missing_page_id"
        return record

    try:
        validate_page_frontmatter(frontmatter)
    except FrontmatterValidationError as exc:
        record.issue = "invalid_frontmatter"
        record.errors = tuple(exc.errors)
        return record

    if frontmatter.get("page_type") != page_type_dir:
        record.issue = "page_type_directory_mismatch"
        return record

    filename = os.path.splitext(os.path.basename(path))[0]
    if frontmatter.get("slug") != filename:
        record.issue = "slug_filename_mismatch"
        return record

    return record


def scan_vault(config: WikiConfig) -> list[ScannedPage]:
    pages: list[ScannedPage] = []
    for page_type in C.PAGE_TYPES:
        directory = config.page_type_dir(page_type)
        if not os.path.isdir(directory):
            continue
        for filename in sorted(os.listdir(directory)):
            if not filename.lower().endswith(".md"):
                continue
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            relative = os.path.relpath(path, config.vault_path).replace(
                os.sep, "/"
            )
            pages.append(_scan_file(path, relative, page_type))
    return pages


def _absolute_page_path(config: WikiConfig, page: PageRecord) -> str | None:
    relative = page.relative_path.replace("\\", "/")
    if os.path.isabs(relative):
        return None
    try:
        candidate = os.path.join(config.vault_path, *relative.split("/"))
        return ensure_within(config.vault_path, candidate)
    except WikiPathError:
        return None


def _candidate_payload(record: ScannedPage) -> dict[str, Any]:
    frontmatter = record.frontmatter
    return {
        "page_id": frontmatter["page_id"],
        "slug": frontmatter["slug"],
        "title": frontmatter["title"],
        "page_type": frontmatter["page_type"],
        "status": frontmatter["status"],
        "revision_id": frontmatter["revision_id"],
        "relative_path": record.relative_path,
        "content_sha256": record.sha256,
    }


def _register_page(
    conn: sqlite3.Connection, config: WikiConfig, record: ScannedPage
) -> dict[str, Any]:
    frontmatter = record.frontmatter
    created_at = _timestamp(frontmatter.get("created"))
    updated_at = _timestamp(frontmatter.get("updated"), default=created_at)
    page = PageRecord(
        page_id=frontmatter["page_id"],
        slug=frontmatter["slug"],
        title=frontmatter["title"],
        page_type=frontmatter["page_type"],
        status=frontmatter["status"],
        current_revision_id=frontmatter["revision_id"],
        relative_path=record.relative_path,
        created_by="rebuild_wiki_metadata",
        created_at=created_at,
        updated_at=updated_at,
    )
    revision = PageRevisionRecord(
        revision_id=frontmatter["revision_id"],
        page_id=page.page_id,
        revision_no=1,
        relative_path=record.relative_path,
        content_sha256=record.sha256,
        git_commit="",
        summary=frontmatter["summary"],
        change_reason="由 rebuild_wiki_metadata 按 vault 文件登记",
        source_proposal_id=None,
        created_by="rebuild_wiki_metadata",
        created_at=updated_at,
    )
    pages_repo.insert_page(conn, page)
    pages_repo.insert_page_revision(conn, revision)
    return _candidate_payload(record)


def rebuild(
    conn: sqlite3.Connection,
    config: WikiConfig,
    apply: bool = False,
) -> dict[str, Any]:
    """扫描 vault 并（可选）登记缺失元数据；返回完整报告。"""
    scanned = scan_vault(config)
    pages = pages_repo.list_pages(conn, limit=None, order="created")
    pages_by_id = {page.page_id: page for page in pages}
    slugs = {page.slug.casefold(): page for page in pages}
    paths = {
        os.path.normcase(os.path.normpath(page.relative_path)): page
        for page in pages
    }

    report: dict[str, Any] = {
        "dry_run": not apply,
        "vault": config.vault_path,
        "page_root": config.page_root,
        "scanned": len(scanned),
        "register_candidates": [],
        "registered": [],
        "existing": [],
        "missing_page_id": [],
        "invalid_frontmatter": [],
        "slug_filename_mismatch": [],
        "page_type_directory_mismatch": [],
        "slug_conflict": [],
        "path_conflict": [],
        "revision_conflict": [],
        "orphan_page_records": [],
        "link_rebuild": None,
    }

    candidates: list[ScannedPage] = []
    for record in scanned:
        if record.issue == "missing_page_id":
            report["missing_page_id"].append(
                {
                    "relative_path": record.relative_path,
                    "title": record.frontmatter.get("title", ""),
                }
            )
            continue
        if record.issue == "invalid_frontmatter":
            report["invalid_frontmatter"].append(
                {
                    "relative_path": record.relative_path,
                    "errors": list(record.errors),
                }
            )
            continue
        if record.issue in (
            "slug_filename_mismatch",
            "page_type_directory_mismatch",
        ):
            report[record.issue].append(
                {
                    "relative_path": record.relative_path,
                    "issue": record.issue,
                }
            )
            continue

        frontmatter = record.frontmatter
        page_id = frontmatter["page_id"]
        slug = frontmatter["slug"]
        revision_id = frontmatter["revision_id"]
        if page_id in pages_by_id:
            existing = pages_by_id[page_id]
            if existing.relative_path == record.relative_path:
                report["existing"].append(
                    {
                        "page_id": page_id,
                        "slug": existing.slug,
                        "relative_path": existing.relative_path,
                    }
                )
            else:
                report["path_conflict"].append(
                    {
                        "page_id": page_id,
                        "db_relative_path": existing.relative_path,
                        "file_relative_path": record.relative_path,
                    }
                )
            continue
        other_slug = slugs.get(slug.casefold())
        if other_slug is not None:
            report["slug_conflict"].append(
                {
                    "slug": slug,
                    "page_id": page_id,
                    "existing_page_id": other_slug.page_id,
                }
            )
            continue
        path_key = os.path.normcase(os.path.normpath(record.relative_path))
        other_path = paths.get(path_key)
        if other_path is not None:
            report["path_conflict"].append(
                {
                    "page_id": page_id,
                    "existing_page_id": other_path.page_id,
                    "file_relative_path": record.relative_path,
                }
            )
            continue
        if pages_repo.get_page_revision(conn, revision_id) is not None:
            report["revision_conflict"].append(
                {
                    "revision_id": revision_id,
                    "page_id": page_id,
                    "file_relative_path": record.relative_path,
                }
            )
            continue
        report["register_candidates"].append(_candidate_payload(record))
        candidates.append(record)

    for page in pages:
        path = _absolute_page_path(config, page)
        if path is None or not os.path.isfile(path):
            report["orphan_page_records"].append(
                {
                    "page_id": page.page_id,
                    "slug": page.slug,
                    "relative_path": page.relative_path,
                }
            )

    if apply and candidates:
        context = nullcontext() if conn.in_transaction else wiki_db.transaction(conn)
        with context:
            for record in candidates:
                report["registered"].append(_register_page(conn, config, record))
    if apply:
        # 登记完成后全量重建出链/断链，保证导入的页面立即参与双链索引。
        report["link_rebuild"] = link_index.rebuild_all_page_links(conn, config)
    return report


def render_text(report: dict[str, Any]) -> str:
    mode = "dry-run（未写库）" if report["dry_run"] else "apply（已写库）"
    lines = [
        f"Wiki 元数据重建：{mode}",
        f"  vault: {report['vault']}",
        f"  页面根: {report['page_root']}",
        f"  扫描 Markdown: {report['scanned']}",
        f"  可登记: {len(report['register_candidates'])}",
        f"  已登记: {len(report['registered'])}",
        f"  库中已存在: {len(report['existing'])}",
        f"  缺 page_id（仅报告）: {len(report['missing_page_id'])}",
        f"  frontmatter 非法: {len(report['invalid_frontmatter'])}",
        f"  slug/文件名不一致: {len(report['slug_filename_mismatch'])}",
        f"  page_type/目录不一致: {len(report['page_type_directory_mismatch'])}",
        f"  slug 冲突: {len(report['slug_conflict'])}",
        f"  路径冲突: {len(report['path_conflict'])}",
        f"  revision 冲突: {len(report['revision_conflict'])}",
        f"  DB 记录但文件缺失: {len(report['orphan_page_records'])}",
    ]
    link_rebuild = report.get("link_rebuild")
    if link_rebuild is not None:
        lines.append(
            f"  链接重建: pages={link_rebuild['pages']}, "
            f"published={link_rebuild['published']}, "
            f"links={link_rebuild['links']}, broken={link_rebuild['broken']}"
        )
        for error in link_rebuild.get("errors") or []:
            lines.append(f"  - [link] {error}")
    for item in report["register_candidates"]:
        prefix = "已登记" if report["registered"] else "候选"
        lines.append(
            f"  - [{prefix}] {item['relative_path']} "
            f"(page_id={item['page_id']}, slug={item['slug']})"
        )
    for item in report["missing_page_id"]:
        lines.append(f"  - [缺 page_id] {item['relative_path']}")
    for item in report["orphan_page_records"]:
        lines.append(
            f"  - [orphan] {item['relative_path']} (page_id={item['page_id']})"
        )
    return "\n".join(lines)


def _resolve_paths(
    args: argparse.Namespace,
) -> tuple[WikiConfig, str, str]:
    try:
        env_config = WikiConfig.from_env()
    except WikiConfigError as exc:
        raise RebuildError(f"读取 Wiki 配置失败: {exc}") from exc
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
        description="从 vault 页面 frontmatter 补齐 Wiki pages/page_revisions 元数据",
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
        "--apply",
        action="store_true",
        help="写入缺失的 pages/page_revisions（默认 dry-run）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="显式 dry-run（默认行为；与 --apply 互斥）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 不能同时使用")
    conn: sqlite3.Connection | None = None
    try:
        env_config, vault_path, db_path = _resolve_paths(args)
        if not os.path.exists(db_path):
            raise RebuildError(f"Wiki 数据库不存在: {db_path}")
        if not os.path.isfile(db_path):
            raise RebuildError(f"Wiki 数据库路径不是文件: {db_path}")
        config = _build_config(env_config, args, vault_path, db_path)
        conn = wiki_db.connect(db_path, config)
        if not wiki_db.db_status(conn):
            raise RebuildError(
                f"数据库未执行 Wiki 迁移: {db_path}；先运行迁移"
            )
        report = rebuild(conn, config, apply=args.apply)
    except (RebuildError, WikiConfigError, sqlite3.Error) as exc:
        print(f"rebuild_wiki_metadata: error: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
