# -*- coding: utf-8 -*-
"""Wiki 页面写入存储：原子文件写、frontmatter 适配、SQLite 元数据与 git 提交。

vault 契约（见 vault/AGENTS.md 与 SCHEMA.md）：
- 页面位于 ``<vault>/<page_root>/{concepts-概念|entities-实体|topics-主题}/<slug>.md``；
- frontmatter 五字段 title/tags/created/type/summary，追加 page_id/slug/page_type/
  status/aliases/revision_id/updated；``type`` 固定 permanent，``created`` 为
  ``YYYY-MM-DD``，``updated`` 为 UTC ISO8601。

写入顺序：同目录临时文件 → flush+fsync → 重读校验 sha256 → os.replace →
SQLite 事务（pages + page_revisions）→ 可选 git add/commit。git 失败时不回滚
已写文件与 DB，仅把 write_journal 置 failed。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping

from . import constants as C
from . import link_index
from .config import WikiConfig, WikiPathError, ensure_within
from .db import transaction
from .ids import content_sha256, new_ulid
from .markdown import (
    FrontmatterError,
    FrontmatterValidationError,
    parse_frontmatter,
    render_frontmatter,
    validate_page_frontmatter,
)
from .models import PageLinkRecord, PageRecord, PageRevisionRecord
from .repositories import jobs as jobs_repo
from .repositories import pages as pages_repo

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INVALID_SLUG_CHARS = set('<>:"/\\|?*#[]^\x00')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_MAX_SLUG_LENGTH = 120


class PageStoreError(RuntimeError):
    """PageStore 基类错误。"""


class PageValidationError(PageStoreError, ValueError):
    """页面内容或 frontmatter 不满足契约。"""


class PageNotFoundError(PageStoreError, LookupError):
    """页面元数据不存在。"""


class PageFileMissingError(PageStoreError, FileNotFoundError):
    """页面元数据存在但磁盘文件缺失。"""


class RevisionConflictError(PageStoreError):
    """expected_revision_id 与当前 revision 不一致。"""

    def __init__(self, page_id: str, expected: str | None, current: str | None) -> None:
        self.page_id = page_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"页面 {page_id} 修订冲突：expected={expected!r}, current={current!r}"
        )


class PageWriteError(PageStoreError, OSError):
    """原子写失败（临时文件/校验/replace）。"""


class GitCommitError(PageStoreError):
    """git add/commit 失败。"""


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return _sha256_bytes(handle.read())


def atomic_write_text(path: str, text: str) -> str:
    """同目录临时文件原子写入 UTF-8 文本，返回内容 sha256。

    临时文件写入后 flush+fsync，再重读校验 sha256，最后 os.replace 覆盖目标。
    任一步失败都不会覆盖原目标文件，临时文件会被清理。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    data = text.encode("utf-8")
    expected = _sha256_bytes(data)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        actual = _sha256_file(temp_path)
        if actual != expected:
            raise PageWriteError(
                f"临时文件重读 sha256 不一致: {temp_path} "
                f"(expected={expected}, actual={actual})"
            )
        os.replace(temp_path, path)
        return expected
    except BaseException:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
        raise


def _format_created(value: Any) -> str:
    """把 frontmatter created 值规整为 YYYY-MM-DD。"""
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if _DATE_RE.match(text):
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError as exc:
                raise PageValidationError(f"created 不是合法日期: {value!r}") from exc
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PageValidationError(
                f"created 必须是 YYYY-MM-DD（当前值: {value!r}）"
            ) from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date().isoformat()
    raise PageValidationError(f"created 类型不支持: {type(value).__name__}")


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not slug.strip():
        raise PageValidationError("slug 必须是非空字符串")
    if slug != slug.strip():
        raise PageValidationError("slug 不能包含首尾空白")
    if slug in {".", ".."}:
        raise PageValidationError("slug 不能是 '.' 或 '..'")
    if len(slug) > _MAX_SLUG_LENGTH:
        raise PageValidationError(
            f"slug 过长（>{_MAX_SLUG_LENGTH} 字符）: {slug!r}"
        )
    if any(ord(char) < 32 for char in slug):
        raise PageValidationError("slug 不能包含控制字符")
    if any(char in _INVALID_SLUG_CHARS for char in slug):
        raise PageValidationError(
            "slug 不能包含路径分隔符或 Obsidian/Windows 非法字符: "
            f"{slug!r}"
        )
    if slug.endswith((".", " ")):
        raise PageValidationError("slug 不能以 '.' 或空格结尾")
    stem = slug.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise PageValidationError(f"slug 是 Windows 保留名: {slug!r}")
    return slug


def _string_list(name: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PageValidationError(f"{name} 必须是字符串列表")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise PageValidationError(f"{name}[{index}] 必须是非空字符串")
        result.append(item.strip())
    return result


@dataclass(slots=True)
class PageDraft:
    """新建页面输入。``title`` / ``page_type`` / ``summary`` 必填。"""

    title: str
    page_type: str
    summary: str
    body: str = ""
    slug: str | None = None
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    status: str = "draft"
    created: Any = None
    change_reason: str = ""
    sources: list[str] = field(default_factory=list)


def _coerce_draft(draft: PageDraft | Mapping[str, Any]) -> PageDraft:
    if isinstance(draft, PageDraft):
        return draft
    if not isinstance(draft, Mapping):
        raise PageValidationError("draft 必须是 PageDraft 或映射")
    allowed = {field_info.name for field_info in PageDraft.__dataclass_fields__.values()}
    unknown = set(draft) - allowed
    if unknown:
        raise PageValidationError(
            f"draft 包含未知字段: {', '.join(sorted(unknown))}"
        )
    try:
        return PageDraft(**dict(draft))
    except TypeError as exc:
        raise PageValidationError(f"draft 缺少必填字段: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PageView:
    page: PageRecord
    frontmatter: dict[str, Any]
    body: str
    content: str
    content_sha256: str
    revision: PageRevisionRecord | None
    links: list[PageLinkRecord]
    backlinks: list[PageLinkRecord]

    def __getattr__(self, name: str) -> Any:
        """未定义属性代理到 PageRecord（如 page_id/slug/status）。"""
        return getattr(self.page, name)


@dataclass(frozen=True, slots=True)
class UpdateResult:
    status: str
    page: PageRecord
    revision: PageRevisionRecord | None = None
    content_sha256: str = ""
    git_commit: str = ""
    message: str = ""
    current_revision_id: str | None = None
    disk_sha256: str = ""


class PageStore:
    """页面文件 + SQLite 元数据 + git 的写入入口。"""

    def __init__(self, config: WikiConfig, conn: sqlite3.Connection) -> None:
        if not isinstance(config, WikiConfig):
            raise TypeError("config 必须是 WikiConfig")
        self.config = config
        self.conn = conn

    # ── 路径与文件 ──
    def _relative_path(self, page_type: str, slug: str) -> str:
        directory = self.config.page_type_dir(page_type)
        absolute = ensure_within(
            self.config.page_root, os.path.join(directory, f"{slug}.md")
        )
        ensure_within(self.config.vault_path, absolute)
        relative = os.path.relpath(absolute, self.config.vault_path)
        return relative.replace(os.sep, "/")

    def _page_path(self, page: PageRecord) -> str:
        relative = page.relative_path.replace("\\", "/")
        if os.path.isabs(relative):
            raise WikiPathError(f"页面 relative_path 不能是绝对路径: {relative}")
        candidate = os.path.join(self.config.vault_path, *relative.split("/"))
        ensure_within(self.config.vault_path, candidate)
        return ensure_within(self.config.page_root, candidate)

    def _dir_slug_taken(self, directory: str, slug: str) -> bool:
        if not os.path.isdir(directory):
            return False
        expected = f"{slug}.md".casefold()
        for name in os.listdir(directory):
            if name.casefold() == expected:
                return True
        return False

    def _unique_slug(self, base_slug: str, page_type: str) -> str:
        directory = self.config.page_type_dir(page_type)
        base = _validate_slug(base_slug)
        for index in range(1, 1000):
            candidate = base if index == 1 else f"{base}-{index}"
            if (
                not pages_repo.slug_exists(self.conn, candidate)
                and not self._dir_slug_taken(directory, candidate)
            ):
                return candidate
        raise PageStoreError(f"无法为 slug {base!r} 找到可用名称")

    def _read_page_file(self, page: PageRecord) -> tuple[str, str, str]:
        path = self._page_path(page)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError as exc:
            raise PageFileMissingError(f"页面文件缺失: {path}") from exc
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PageValidationError(f"页面文件不是 UTF-8: {path}") from exc
        return content, _sha256_bytes(raw), path

    # ── 创建 ──
    def create_page(
        self,
        draft: PageDraft | Mapping[str, Any],
        actor: str = "agent",
        commit: bool = False,
    ) -> PageRecord:
        draft = _coerce_draft(draft)
        if not isinstance(draft.title, str) or not draft.title.strip():
            raise PageValidationError("title 必须是非空字符串")
        if not isinstance(draft.summary, str) or not draft.summary.strip():
            raise PageValidationError("summary 必须是非空字符串（vault 契约）")
        if not isinstance(draft.body, str):
            raise PageValidationError("body 必须是字符串")
        status = (draft.status or "draft").strip()
        if status not in C.PAGE_STATUSES:
            raise PageValidationError(f"未知 status: {status!r}")
        page_type = draft.page_type
        if page_type not in C.PAGE_TYPES:
            raise PageValidationError(f"未知 page_type: {page_type!r}")
        tags = _string_list("tags", draft.tags)
        aliases = _string_list("aliases", draft.aliases)
        sources = _string_list("sources", draft.sources)
        created = _format_created(draft.created)

        base_slug = (
            _validate_slug(draft.slug)
            if draft.slug is not None
            else f"p-{new_ulid()[:8]}"
        )
        page_id = new_ulid()
        revision_id = new_ulid()
        now = utc_now()
        provisional_path = self._relative_path(page_type, base_slug)

        journal = jobs_repo.create_journal(
            self.conn,
            page_id=None,  # pages 行尚未插入，FK 要求先留空
            revision_id=revision_id,
            target_path=provisional_path,
            state="prepared",
            detail={
                "action": "create_page",
                "actor": actor,
                "slug": base_slug,
                "page_id": page_id,
            },
        )

        written_path: str | None = None
        committed = False
        try:
            with transaction(self.conn):
                slug = self._unique_slug(base_slug, page_type)
                relative_path = self._relative_path(page_type, slug)
                frontmatter = {
                    "title": draft.title.strip(),
                    "tags": tags,
                    "created": date.fromisoformat(created),
                    "type": C.WIKI_PAGE_TYPE,
                    "summary": draft.summary.strip(),
                    "page_id": page_id,
                    "slug": slug,
                    "page_type": page_type,
                    "status": status,
                    "aliases": aliases,
                    "revision_id": revision_id,
                    "updated": now,
                }
                if sources:
                    frontmatter["sources"] = sources
                frontmatter = validate_page_frontmatter(frontmatter)
                content = render_frontmatter(frontmatter, draft.body)
                digest = content_sha256(content)
                path = self._page_path(
                    PageRecord(
                        page_id=page_id,
                        slug=slug,
                        title=frontmatter["title"],
                        page_type=page_type,
                        status=status,
                        current_revision_id=revision_id,
                        relative_path=relative_path,
                        created_by=actor,
                        created_at=now,
                        updated_at=now,
                    )
                )
                atomic_write_text(path, content)
                written_path = path

                page = PageRecord(
                    page_id=page_id,
                    slug=slug,
                    title=frontmatter["title"],
                    page_type=page_type,
                    status=status,
                    current_revision_id=revision_id,
                    relative_path=relative_path,
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                revision = PageRevisionRecord(
                    revision_id=revision_id,
                    page_id=page_id,
                    revision_no=1,
                    relative_path=relative_path,
                    content_sha256=digest,
                    git_commit="",
                    summary="",
                    change_reason=draft.change_reason,
                    created_by=actor,
                    created_at=now,
                )
                pages_repo.insert_page(self.conn, page)
                pages_repo.insert_page_revision(self.conn, revision)
                jobs_repo.update_journal_state(
                    self.conn,
                    journal.journal_id,
                    "file_written",
                    detail={"relative_path": relative_path},
                    page_id=page_id,
                )
                self._sync_links(page, draft.body)
            committed = True
            jobs_repo.update_journal_state(
                self.conn, journal.journal_id, "db_committed"
            )

            git_commit = ""
            if commit and self.config.git_commit:
                git_commit = self.git_commit_page(
                    page.relative_path, f"wiki: create {page.slug}"
                )
                if git_commit:
                    pages_repo.set_revision_git_commit(
                        self.conn, revision.revision_id, git_commit
                    )
            jobs_repo.update_journal_state(
                self.conn,
                journal.journal_id,
                "completed",
                detail={
                    "git_commit": git_commit,
                    "git_skipped": not (commit and self.config.git_commit),
                },
            )
            return page
        except BaseException as exc:
            if not committed and written_path is not None:
                try:
                    if os.path.exists(written_path):
                        os.unlink(written_path)
                except OSError:
                    pass
            try:
                jobs_repo.update_journal_state(
                    self.conn,
                    journal.journal_id,
                    "failed",
                    error_message=str(exc),
                )
            except Exception:
                pass
            raise

    # ── 更新 ──
    def update_page(
        self,
        page_id: str,
        content: str,
        expected_revision_id: str,
        actor: str = "agent",
        commit: bool = True,
        *,
        source_proposal_id: str | None = None,
        change_reason: str = "",
    ) -> UpdateResult:
        if not isinstance(content, str):
            raise PageValidationError("content 必须是字符串")
        if not isinstance(expected_revision_id, str):
            raise PageValidationError("expected_revision_id 必须是字符串")
        page = pages_repo.get_page(self.conn, page_id)
        if page is None:
            raise PageNotFoundError(f"页面不存在: {page_id}")

        old_content, disk_digest, path = self._read_page_file(page)
        current = (
            pages_repo.get_page_revision(self.conn, page.current_revision_id)
            if page.current_revision_id
            else None
        )
        if current is None or disk_digest != current.content_sha256:
            return UpdateResult(
                status="needs_review",
                page=page,
                revision=current,
                message="磁盘内容与当前 revision 不一致（疑似手工编辑），未覆盖",
                current_revision_id=page.current_revision_id,
                disk_sha256=disk_digest,
            )
        if page.current_revision_id != expected_revision_id:
            raise RevisionConflictError(
                page_id, expected_revision_id, page.current_revision_id
            )

        new_revision_id = new_ulid()
        now = utc_now()
        journal = jobs_repo.create_journal(
            self.conn,
            page_id=page_id,
            revision_id=new_revision_id,
            target_path=page.relative_path,
            state="prepared",
            detail={"action": "update_page", "actor": actor},
        )

        written = False
        committed = False
        needs_review: UpdateResult | None = None
        try:
            with transaction(self.conn):
                locked_page = pages_repo.get_page(self.conn, page_id)
                if locked_page is None:
                    raise PageNotFoundError(f"页面不存在: {page_id}")
                if locked_page.current_revision_id != expected_revision_id:
                    raise RevisionConflictError(
                        page_id,
                        expected_revision_id,
                        locked_page.current_revision_id,
                    )

                locked_content, locked_digest, locked_path = self._read_page_file(
                    locked_page
                )
                locked_current = (
                    pages_repo.get_page_revision(
                        self.conn, locked_page.current_revision_id
                    )
                    if locked_page.current_revision_id
                    else None
                )
                if (
                    locked_current is None
                    or locked_digest != locked_current.content_sha256
                ):
                    needs_review = UpdateResult(
                        status="needs_review",
                        page=locked_page,
                        revision=locked_current,
                        message="磁盘内容与当前 revision 不一致（疑似手工编辑），未覆盖",
                        current_revision_id=locked_page.current_revision_id,
                        disk_sha256=locked_digest,
                    )
                else:
                    (
                        frontmatter,
                        body,
                        status,
                    ) = self._build_update_document(
                        locked_page,
                        locked_content,
                        content,
                        revision_id=new_revision_id,
                        updated=now,
                    )
                    rendered = render_frontmatter(frontmatter, body)
                    digest = content_sha256(rendered)
                    revision_no = pages_repo.next_revision_no(
                        self.conn, page_id
                    )
                    atomic_write_text(locked_path, rendered)
                    written = True
                    jobs_repo.update_journal_state(
                        self.conn,
                        journal.journal_id,
                        "file_written",
                        detail={"revision_no": revision_no},
                    )
                    revision = PageRevisionRecord(
                        revision_id=new_revision_id,
                        page_id=page_id,
                        revision_no=revision_no,
                        relative_path=locked_page.relative_path,
                        content_sha256=digest,
                        git_commit="",
                        summary="",
                        change_reason=change_reason,
                        source_proposal_id=source_proposal_id,
                        created_by=actor,
                        created_at=now,
                    )
                    pages_repo.insert_page_revision(self.conn, revision)
                    pages_repo.set_current_revision(
                        self.conn,
                        page_id,
                        new_revision_id,
                        status=status,
                        updated_at=now,
                    )
                    updated_page = pages_repo.get_page(self.conn, page_id)
                    assert updated_page is not None
                    self._sync_links(updated_page, body)
            committed = True
            if needs_review is not None:
                jobs_repo.update_journal_state(
                    self.conn,
                    journal.journal_id,
                    "failed",
                    error_message=needs_review.message,
                )
                return needs_review

            jobs_repo.update_journal_state(
                self.conn, journal.journal_id, "db_committed"
            )
            updated_page = pages_repo.get_page(self.conn, page_id)
            assert updated_page is not None
            revision = pages_repo.get_page_revision(
                self.conn, new_revision_id
            )
            assert revision is not None

            git_commit = ""
            if commit and self.config.git_commit:
                git_commit = self.git_commit_page(
                    updated_page.relative_path,
                    f"wiki: update {updated_page.slug} (r{revision.revision_no})",
                )
                if git_commit:
                    pages_repo.set_revision_git_commit(
                        self.conn, revision.revision_id, git_commit
                    )
                    revision = pages_repo.get_page_revision(
                        self.conn, new_revision_id
                    )
            jobs_repo.update_journal_state(
                self.conn,
                journal.journal_id,
                "completed",
                detail={
                    "git_commit": git_commit,
                    "git_skipped": not (commit and self.config.git_commit),
                },
            )
            assert revision is not None
            return UpdateResult(
                status="updated",
                page=updated_page,
                revision=revision,
                content_sha256=revision.content_sha256,
                git_commit=git_commit,
                current_revision_id=updated_page.current_revision_id,
            )
        except BaseException as exc:
            if not committed and written:
                try:
                    atomic_write_text(path, old_content)
                except Exception:
                    pass
            try:
                jobs_repo.update_journal_state(
                    self.conn,
                    journal.journal_id,
                    "failed",
                    error_message=str(exc),
                )
            except Exception:
                pass
            raise

    def _build_update_document(
        self,
        page: PageRecord,
        old_content: str,
        new_content: str,
        *,
        revision_id: str,
        updated: str,
    ) -> tuple[dict[str, Any], str, str]:
        """把更新输入适配为 frontmatter + body。

        - ``new_content`` 以 ``---`` 开头时视为完整文档，frontmatter 必须合法且
          page_id/slug/page_type 与库中记录一致；
        - 否则视为 body，保留磁盘上现有 frontmatter（title/tags/summary/aliases/
          created），status 沿用数据库当前值。
        """
        try:
            existing_fm, existing_body = parse_frontmatter(old_content)
            validate_page_frontmatter(existing_fm)
        except (FrontmatterError, FrontmatterValidationError) as exc:
            raise PageValidationError(f"现有页面 frontmatter 非法: {exc}") from exc

        cleaned = (
            new_content[1:] if new_content.startswith("\ufeff") else new_content
        )
        first_lines = cleaned.splitlines()
        has_frontmatter = bool(first_lines) and (
            first_lines[0].strip() == C.FRONTMATTER_DELIMITER
        )

        if has_frontmatter:
            try:
                provided_fm, body = parse_frontmatter(new_content)
                provided_fm = validate_page_frontmatter(provided_fm)
            except (FrontmatterError, FrontmatterValidationError) as exc:
                raise PageValidationError(f"新内容 frontmatter 非法: {exc}") from exc
            for key, expected in (
                ("page_id", page.page_id),
                ("slug", page.slug),
                ("page_type", page.page_type),
            ):
                if provided_fm.get(key) != expected:
                    raise PageValidationError(
                        f"更新内容不能修改 {key}（库中={expected!r}, "
                        f"提供={provided_fm.get(key)!r}）"
                    )
            title = provided_fm["title"]
            tags = _string_list("tags", provided_fm.get("tags"))
            summary = provided_fm["summary"]
            aliases = _string_list("aliases", provided_fm.get("aliases"))
            status = provided_fm["status"]
            sources = _string_list(
                "sources",
                provided_fm.get("sources")
                if "sources" in provided_fm
                else existing_fm.get("sources"),
            )
        else:
            title = existing_fm["title"]
            tags = _string_list("tags", existing_fm.get("tags"))
            summary = existing_fm["summary"]
            aliases = _string_list("aliases", existing_fm.get("aliases"))
            body = new_content
            status = str(page.status)
            sources = _string_list("sources", existing_fm.get("sources"))

        created = _format_created(
            existing_fm.get("created")
            or (page.created_at[:10] if page.created_at else None)
        )
        frontmatter = validate_page_frontmatter(
            {
                "title": title,
                "tags": tags,
                "created": date.fromisoformat(created),
                "type": C.WIKI_PAGE_TYPE,
                "summary": summary,
                "page_id": page.page_id,
                "slug": page.slug,
                "page_type": page.page_type,
                "status": status,
                "aliases": aliases,
                "revision_id": revision_id,
                "updated": updated,
                **({"sources": sources} if sources else {}),
            }
        )
        return frontmatter, body, str(status)

    # ── 读取 ──
    def read_page(self, page_id_or_slug: str) -> PageView | None:
        if not isinstance(page_id_or_slug, str) or not page_id_or_slug.strip():
            raise ValueError("page_id_or_slug 必须是非空字符串")
        page = pages_repo.get_page(self.conn, page_id_or_slug)
        if page is None:
            page = pages_repo.get_page_by_slug(self.conn, page_id_or_slug)
        if page is None:
            return None
        content, digest, _ = self._read_page_file(page)
        try:
            frontmatter, body = parse_frontmatter(content)
            frontmatter = validate_page_frontmatter(frontmatter)
        except (FrontmatterError, FrontmatterValidationError) as exc:
            raise PageValidationError(f"页面 frontmatter 非法: {exc}") from exc
        revision = (
            pages_repo.get_page_revision(self.conn, page.current_revision_id)
            if page.current_revision_id
            else None
        )
        return PageView(
            page=page,
            frontmatter=frontmatter,
            body=body,
            content=content,
            content_sha256=digest,
            revision=revision,
            links=link_index.outgoing_links(self.conn, page.page_id),
            backlinks=link_index.backlinks(self.conn, page.page_id),
        )

    def list_pages(
        self,
        status: str | None = None,
        page_type: str | None = None,
        limit: int = 50,
    ) -> list[PageRecord]:
        return pages_repo.list_pages(
            self.conn, status=status, page_type=page_type, limit=limit
        )

    def detect_external_edit(self, page_id: str) -> str:
        """返回 clean / manual_edit / missing。"""
        page = pages_repo.get_page(self.conn, page_id)
        if page is None:
            raise PageNotFoundError(f"页面不存在: {page_id}")
        path = self._page_path(page)
        if not os.path.isfile(path):
            return "missing"
        current = (
            pages_repo.get_page_revision(self.conn, page.current_revision_id)
            if page.current_revision_id
            else None
        )
        if current is None:
            return "manual_edit"
        return (
            "clean"
            if _sha256_file(path) == current.content_sha256
            else "manual_edit"
        )

    # ── 内部 ──
    def _sync_links(self, page: PageRecord, body: str) -> None:
        if str(page.status) == "published":
            link_index.rebuild_page_links(
                self.conn, page.page_id, body, config=self.config
            )
        else:
            link_index.delete_page_links(self.conn, page.page_id)

    # ── git ──
    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", self.config.vault_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    def _is_git_repo(self) -> bool:
        try:
            result = self._run_git(["rev-parse", "--is-inside-work-tree"])
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _commit_relative_path(
        self, path: str, *, require_exists: bool = True
    ) -> str:
        """把相对/绝对路径规范为 vault 相对路径，并校验边界（可选文件存在）。"""
        if not isinstance(path, str) or not path.strip():
            raise GitCommitError("待提交路径必须是非空字符串")
        if os.path.isabs(path):
            absolute = ensure_within(self.config.vault_path, path)
        else:
            normalized = path.replace("\\", "/")
            absolute = ensure_within(
                self.config.vault_path,
                os.path.join(self.config.vault_path, *normalized.split("/")),
            )
        relative = os.path.relpath(absolute, self.config.vault_path).replace(
            os.sep, "/"
        )
        if relative in ("", "."):
            raise GitCommitError("不能提交 vault 根目录")
        if require_exists and not os.path.isfile(absolute):
            raise GitCommitError(f"待提交文件不存在: {absolute}")
        return relative

    def commit_paths(self, paths: list[str], message: str) -> str:
        """一次提交指定路径；未初始化 git 时返回 ""，失败抛 GitCommitError。

        所有路径必须位于 vault 内。git 仓库内还要求路径是已存在的文件；
        只 ``git add`` 并 ``git commit --only`` 这些路径，不触碰其他
        已暂存/未暂存的文件。
        """
        if not isinstance(message, str) or not message.strip():
            raise GitCommitError("git commit message 不能为空")
        if isinstance(paths, (str, bytes)) or not isinstance(paths, (list, tuple)):
            raise GitCommitError("paths 必须是路径列表")
        if not paths:
            raise GitCommitError("paths 不能为空")

        relatives: list[str] = []
        seen: set[str] = set()
        for path in paths:
            relative = self._commit_relative_path(path, require_exists=False)
            if relative not in seen:
                seen.add(relative)
                relatives.append(relative)

        if not self._is_git_repo():
            return ""

        for relative in relatives:
            absolute = os.path.join(
                self.config.vault_path, *relative.split("/")
            )
            if not os.path.isfile(absolute):
                raise GitCommitError(f"待提交文件不存在: {absolute}")

        added = self._run_git(["add", "--", *relatives])
        if added.returncode != 0:
            raise GitCommitError(f"git add 失败: {added.stderr.strip()}")
        staged = self._run_git(
            ["diff", "--cached", "--quiet", "--", *relatives]
        )
        if staged.returncode == 0:
            return ""
        if staged.returncode != 1:
            raise GitCommitError(
                f"git diff --cached 失败: {staged.stderr.strip()}"
            )
        committed = self._run_git(
            ["commit", "--only", "-m", message, "--", *relatives]
        )
        if committed.returncode != 0:
            stderr = committed.stderr.strip()
            if "nothing to commit" in stderr.lower():
                return ""
            raise GitCommitError(f"git commit 失败: {stderr}")
        head = self._run_git(["rev-parse", "HEAD"])
        if head.returncode != 0:
            raise GitCommitError(f"git rev-parse HEAD 失败: {head.stderr.strip()}")
        return head.stdout.strip()

    def git_commit_page(self, relative_path: str, message: str) -> str:
        """只提交指定页面文件；未初始化 git 时返回 ""，失败抛 GitCommitError。"""
        if not isinstance(message, str) or not message.strip():
            raise GitCommitError("git commit message 不能为空")
        relative = str(relative_path).replace("\\", "/")
        if os.path.isabs(relative) or ".." in relative.split("/"):
            raise WikiPathError(f"非法的页面相对路径: {relative_path!r}")
        absolute = ensure_within(
            self.config.vault_path,
            os.path.join(self.config.vault_path, *relative.split("/")),
        )
        if not os.path.isfile(absolute):
            raise GitCommitError(f"待提交文件不存在: {absolute}")
        return self.commit_paths([relative], message)


__all__ = [
    "GitCommitError",
    "PageDraft",
    "PageFileMissingError",
    "PageNotFoundError",
    "PageStore",
    "PageStoreError",
    "PageValidationError",
    "PageView",
    "PageWriteError",
    "RevisionConflictError",
    "UpdateResult",
    "atomic_write_text",
    "utc_now",
]
