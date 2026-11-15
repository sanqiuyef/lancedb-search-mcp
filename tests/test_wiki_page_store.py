# -*- coding: utf-8 -*-
"""PageStore：vault 路径、frontmatter、原子写、乐观锁、外部编辑与 git。"""

import glob
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import page_store as page_store_module
from kb.wiki.config import WikiConfig, WikiPathError
from kb.wiki.db import connect, run_migrations
from kb.wiki.markdown import parse_frontmatter, render_frontmatter
from kb.wiki.page_store import (
    GitCommitError,
    PageDraft,
    PageFileMissingError,
    PageStore,
    PageValidationError,
    PageWriteError,
    RevisionConflictError,
    atomic_write_text,
)
from kb.wiki.repositories import pages as pages_repo
from kb.wiki.repositories.jobs import (
    JobTransitionError,
    claim_job,
    create_job,
    create_journal,
    get_journal,
    list_jobs,
    update_job_status,
    update_journal_state,
)


class WikiPageStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)
        self.config = WikiConfig(
            enabled=True,
            vault_path=self.vault,
            db_path=os.path.join(self.root, "db", "wiki.sqlite3"),
        )
        self.conn = connect(self.config.db_path, self.config)
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)
        self.store = PageStore(self.config, self.conn)

    def init_git(self):
        commands = [
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "Wiki Test"],
            ["git", "config", "user.email", "wiki@test.local"],
            ["git", "config", "core.autocrlf", "false"],
            ["git", "config", "core.quotepath", "false"],
            ["git", "config", "commit.gpgsign", "false"],
        ]
        for command in commands:
            subprocess.run(command, cwd=self.vault, check=True)

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def make_draft(self, **overrides):
        values = {
            "title": "城市洪涝",
            "page_type": "concept",
            "summary": "城市洪涝的概念与建模思路摘要。",
            "body": "# 城市洪涝\n\n正文 [[相关页面]]。\n",
            "slug": "urban-flood",
            "tags": ["flood", "urban"],
            "aliases": ["内涝"],
            "status": "draft",
        }
        values.update(overrides)
        return PageDraft(**values)

    def page_path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def read_page_text(self, page):
        with open(self.page_path(page), "r", encoding="utf-8") as handle:
            return handle.read()

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def read_text_at(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def page_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    def revision_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM page_revisions").fetchone()[0]


class TestCreatePage(WikiPageStoreTestBase):
    def test_create_writes_vault_contract(self):
        page = self.store.create_page(self.make_draft())

        self.assertEqual(page.slug, "urban-flood")
        self.assertEqual(
            page.relative_path, "wiki-维基/concepts-概念/urban-flood.md"
        )
        self.assertEqual(str(page.status), "draft")
        self.assertIsNotNone(page.current_revision_id)
        path = self.page_path(page)
        self.assertTrue(os.path.isfile(path))

        text = self.read_page_text(page)
        frontmatter, body = parse_frontmatter(text)
        required = {
            "title",
            "tags",
            "created",
            "type",
            "summary",
            "page_id",
            "slug",
            "page_type",
            "status",
            "aliases",
            "revision_id",
            "updated",
        }
        self.assertTrue(required <= set(frontmatter))
        self.assertEqual(frontmatter["title"], "城市洪涝")
        self.assertEqual(frontmatter["type"], "permanent")
        self.assertEqual(frontmatter["page_id"], page.page_id)
        self.assertEqual(frontmatter["slug"], "urban-flood")
        self.assertEqual(frontmatter["page_type"], "concept")
        self.assertEqual(frontmatter["status"], "draft")
        self.assertEqual(frontmatter["tags"], ["flood", "urban"])
        self.assertEqual(frontmatter["aliases"], ["内涝"])
        self.assertRegex(str(frontmatter["created"]), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(str(frontmatter["updated"]), r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertEqual(body, "# 城市洪涝\n\n正文 [[相关页面]]。\n")

        revision = pages_repo.get_current_revision(self.conn, page.page_id)
        self.assertIsNotNone(revision)
        self.assertEqual(revision.revision_no, 1)
        self.assertEqual(revision.git_commit, "")
        raw = self.read_bytes(path)
        import hashlib

        self.assertEqual(revision.content_sha256, hashlib.sha256(raw).hexdigest())
        journal = self.conn.execute(
            "SELECT state FROM write_journal WHERE page_id = ?", (page.page_id,)
        ).fetchone()
        self.assertEqual(journal["state"], "completed")

        view = self.store.read_page(page.page_id)
        self.assertIsNotNone(view)
        self.assertEqual(view.content_sha256, revision.content_sha256)
        self.assertEqual(view.revision.revision_id, revision.revision_id)
        self.assertEqual(self.store.read_page("urban-flood").page.page_id, page.page_id)

    def test_generated_slug(self):
        page = self.store.create_page(self.make_draft(slug=None))
        self.assertRegex(page.slug, r"^p-[0-9A-HJKMNP-TV-Z]{8}$")
        self.assertTrue(os.path.isfile(self.page_path(page)))

    def test_conflicting_slug_gets_suffix(self):
        first = self.store.create_page(self.make_draft())
        second = self.store.create_page(self.make_draft(title="第二页"))
        third = self.store.create_page(self.make_draft(title="第三页"))
        self.assertEqual(first.slug, "urban-flood")
        self.assertEqual(second.slug, "urban-flood-2")
        self.assertEqual(third.slug, "urban-flood-3")
        for page in (first, second, third):
            self.assertTrue(os.path.isfile(self.page_path(page)))
        self.assertEqual(self.page_count(), 3)

    def test_slug_conflict_is_case_insensitive(self):
        self.store.create_page(self.make_draft(slug="Foo"))
        second = self.store.create_page(self.make_draft(slug="foo"))
        self.assertEqual(second.slug, "foo-2")

    def test_invalid_slug_and_page_type_rejected(self):
        for slug in ("../escape", "a/b", "a\\b", "CON", "..", "", "bad#anchor"):
            with self.subTest(slug=slug):
                with self.assertRaises(PageValidationError):
                    self.store.create_page(self.make_draft(slug=slug))
        with self.assertRaises(PageValidationError):
            self.store.create_page(self.make_draft(page_type="other"))
        self.assertEqual(self.page_count(), 0)
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "escape.md"))
        )

    def test_invalid_draft_frontmatter_rejected(self):
        with self.assertRaises(PageValidationError):
            self.store.create_page(self.make_draft(summary=""))
        with self.assertRaises(PageValidationError):
            self.store.create_page(self.make_draft(tags="not-a-list"))
        with self.assertRaises(PageValidationError):
            self.store.create_page(self.make_draft(body=b"bytes"))
        with self.assertRaises(PageValidationError):
            self.store.create_page(
                {
                    "title": "缺 summary",
                    "page_type": "concept",
                }
            )
        with self.assertRaises(PageValidationError):
            self.store.create_page(
                {
                    "title": "未知字段",
                    "page_type": "concept",
                    "summary": "摘要",
                    "unexpected": "x",
                }
            )
        self.assertEqual(self.page_count(), 0)

    def test_list_pages_filters(self):
        first = self.store.create_page(self.make_draft(slug="a", status="draft"))
        second = self.store.create_page(
            self.make_draft(slug="b", status="published", page_type="topic")
        )
        self.store.create_page(self.make_draft(slug="c", status="published"))
        drafts = self.store.list_pages(status="draft")
        self.assertEqual([page.slug for page in drafts], ["a"])
        published = self.store.list_pages(status="published", limit=1)
        self.assertEqual(len(published), 1)
        topics = self.store.list_pages(page_type="topic")
        self.assertEqual([page.slug for page in topics], ["b"])
        self.assertIn(first.page_id, {page.page_id for page in self.store.list_pages()})
        self.assertIn(second.page_id, {page.page_id for page in self.store.list_pages()})


class TestUpdatePage(WikiPageStoreTestBase):
    def test_body_update_creates_revision_and_preserves_metadata(self):
        page = self.store.create_page(self.make_draft())
        old_view = self.store.read_page(page.page_id)
        new_body = "# 新正文\n\n更新后的内容。\n"

        result = self.store.update_page(
            page.page_id,
            new_body,
            expected_revision_id=page.current_revision_id,
            commit=False,
        )

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.revision.revision_no, 2)
        self.assertNotEqual(result.revision.revision_id, old_view.revision.revision_id)
        self.assertEqual(result.page.current_revision_id, result.revision.revision_id)
        view = self.store.read_page(page.page_id)
        self.assertEqual(view.body, new_body)
        self.assertEqual(view.frontmatter["created"], old_view.frontmatter["created"])
        self.assertEqual(view.frontmatter["title"], "城市洪涝")
        self.assertEqual(view.frontmatter["tags"], ["flood", "urban"])
        self.assertEqual(view.frontmatter["aliases"], ["内涝"])
        self.assertEqual(view.frontmatter["revision_id"], result.revision.revision_id)
        self.assertEqual(view.content_sha256, result.revision.content_sha256)
        self.assertEqual(self.revision_count(), 2)

    def test_full_document_update_can_change_title_and_status(self):
        page = self.store.create_page(self.make_draft())
        view = self.store.read_page(page.page_id)
        frontmatter = dict(view.frontmatter)
        frontmatter["title"] = "新标题"
        frontmatter["summary"] = "新的摘要。"
        frontmatter["status"] = "published"
        new_content = render_frontmatter(frontmatter, "# 新标题\n")

        result = self.store.update_page(
            page.page_id,
            new_content,
            expected_revision_id=page.current_revision_id,
            commit=False,
        )

        self.assertEqual(result.status, "updated")
        view = self.store.read_page(page.page_id)
        self.assertEqual(view.frontmatter["title"], "新标题")
        self.assertEqual(view.frontmatter["summary"], "新的摘要。")
        self.assertEqual(str(view.page.status), "published")
        self.assertEqual(view.body, "# 新标题\n")

    def test_stale_expected_revision_rejected(self):
        page = self.store.create_page(self.make_draft())
        with self.assertRaises(RevisionConflictError):
            self.store.update_page(
                page.page_id,
                "新内容",
                expected_revision_id="01JABCDEFGHJKMNPQRSTVWXYZ0",
                commit=False,
            )
        self.assertEqual(self.revision_count(), 1)
        self.assertEqual(
            pages_repo.get_page(self.conn, page.page_id).current_revision_id,
            page.current_revision_id,
        )

    def test_full_document_cannot_change_identity(self):
        page = self.store.create_page(self.make_draft())
        view = self.store.read_page(page.page_id)
        frontmatter = dict(view.frontmatter)
        frontmatter["slug"] = "other-slug"
        content = render_frontmatter(frontmatter, "正文\n")
        with self.assertRaises(PageValidationError):
            self.store.update_page(
                page.page_id,
                content,
                expected_revision_id=page.current_revision_id,
                commit=False,
            )
        self.assertEqual(self.revision_count(), 1)
        self.assertEqual(
            self.store.read_page(page.page_id).body,
            "# 城市洪涝\n\n正文 [[相关页面]]。\n",
        )

    def test_manual_edit_returns_needs_review_without_overwriting(self):
        page = self.store.create_page(self.make_draft())
        path = self.page_path(page)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n手工追加\n")

        result = self.store.update_page(
            page.page_id,
            "覆盖内容",
            expected_revision_id=page.current_revision_id,
            commit=False,
        )

        self.assertEqual(result.status, "needs_review")
        self.assertIn("手工", self.read_text_at(path))
        self.assertNotIn("覆盖内容", self.read_text_at(path))
        self.assertEqual(self.revision_count(), 1)
        self.assertEqual(self.store.detect_external_edit(page.page_id), "manual_edit")

    def test_detect_external_edit_and_missing_file(self):
        page = self.store.create_page(self.make_draft())
        self.assertEqual(self.store.detect_external_edit(page.page_id), "clean")
        os.unlink(self.page_path(page))
        self.assertEqual(self.store.detect_external_edit(page.page_id), "missing")
        with self.assertRaises(PageFileMissingError):
            self.store.read_page(page.page_id)
        with self.assertRaises(PageFileMissingError):
            self.store.update_page(
                page.page_id,
                "新内容",
                expected_revision_id=page.current_revision_id,
                commit=False,
            )

    def test_atomic_write_failure_does_not_overwrite(self):
        page = self.store.create_page(self.make_draft())
        path = self.page_path(page)
        before = self.read_bytes(path)

        with mock.patch.object(
            page_store_module.os, "fsync", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.store.update_page(
                    page.page_id,
                    "新内容",
                    expected_revision_id=page.current_revision_id,
                    commit=False,
                )

        self.assertEqual(self.read_bytes(path), before)
        self.assertEqual(self.revision_count(), 1)
        self.assertEqual(
            glob.glob(os.path.join(os.path.dirname(path), ".*.tmp")), []
        )

    def test_sha_mismatch_does_not_overwrite(self):
        page = self.store.create_page(self.make_draft())
        path = self.page_path(page)
        before = self.read_bytes(path)

        with mock.patch.object(
            page_store_module, "_sha256_file", return_value="0" * 64
        ):
            with self.assertRaises(PageWriteError):
                self.store.update_page(
                    page.page_id,
                    "新内容",
                    expected_revision_id=page.current_revision_id,
                    commit=False,
                )

        self.assertEqual(self.read_bytes(path), before)
        self.assertEqual(self.revision_count(), 1)
        self.assertEqual(
            glob.glob(os.path.join(os.path.dirname(path), ".*.tmp")), []
        )

    def test_relative_path_escape_rejected_on_read(self):
        page = self.store.create_page(self.make_draft())
        self.conn.execute(
            "UPDATE pages SET relative_path = '../outside.md' WHERE page_id = ?",
            (page.page_id,),
        )
        with self.assertRaises(WikiPathError):
            self.store.read_page(page.page_id)


class TestGitCommit(WikiPageStoreTestBase):
    def test_commit_create_and_update(self):
        self.init_git()
        page = self.store.create_page(self.make_draft(), commit=True)
        revision = pages_repo.get_current_revision(self.conn, page.page_id)
        self.assertRegex(revision.git_commit, r"^[0-9a-f]{40}$")
        log = self.git("log", "-1", "--pretty=%s")
        self.assertIn("create urban-flood", log.stdout)
        tracked = self.git("ls-files", "--error-unmatch", page.relative_path)
        self.assertEqual(tracked.returncode, 0)

        result = self.store.update_page(
            page.page_id,
            "# 更新后\n",
            expected_revision_id=page.current_revision_id,
            commit=True,
        )
        self.assertRegex(result.git_commit, r"^[0-9a-f]{40}$")
        log = self.git("log", "-1", "--pretty=%s")
        self.assertIn("update urban-flood", log.stdout)
        names = self.git("show", "--name-only", "--pretty=", "HEAD")
        self.assertIn(page.relative_path, names.stdout.replace("\\", "/"))
        status = self.git("status", "--porcelain")
        self.assertEqual(status.stdout.strip(), "")

    def test_chinese_slug_commits(self):
        self.init_git()
        page = self.store.create_page(
            self.make_draft(slug="城市洪涝-概念"), commit=True
        )
        self.assertEqual(
            page.relative_path, "wiki-维基/concepts-概念/城市洪涝-概念.md"
        )
        self.assertTrue(os.path.isfile(self.page_path(page)))
        revision = pages_repo.get_current_revision(self.conn, page.page_id)
        self.assertRegex(revision.git_commit, r"^[0-9a-f]{40}$")
        tracked = self.git("ls-files", "--error-unmatch", page.relative_path)
        self.assertEqual(tracked.returncode, 0)

    def test_commit_false_skips_git(self):
        self.init_git()
        page = self.store.create_page(self.make_draft(), commit=False)
        revision = pages_repo.get_current_revision(self.conn, page.page_id)
        self.assertEqual(revision.git_commit, "")
        log = self.git("log", "--oneline")
        self.assertNotEqual(log.returncode, 0)  # 尚无提交

    def test_no_git_repo_skips_and_completes_journal(self):
        page = self.store.create_page(self.make_draft(), commit=True)
        revision = pages_repo.get_current_revision(self.conn, page.page_id)
        self.assertEqual(revision.git_commit, "")
        journal = self.conn.execute(
            "SELECT state, detail_json FROM write_journal WHERE page_id = ?",
            (page.page_id,),
        ).fetchone()
        self.assertEqual(journal["state"], "completed")
        self.assertIn('"git_commit": ""', journal["detail_json"])

    def test_git_failure_keeps_file_db_and_marks_journal_failed(self):
        self.init_git()
        with mock.patch.object(
            PageStore,
            "git_commit_page",
            side_effect=GitCommitError("commit boom"),
        ):
            with self.assertRaises(GitCommitError):
                self.store.create_page(self.make_draft(), commit=True)

        page = self.conn.execute("SELECT * FROM pages").fetchone()
        self.assertIsNotNone(page)
        self.assertTrue(os.path.isfile(self.page_path(
            type("P", (), {"relative_path": page["relative_path"]})()
        )))
        self.assertEqual(self.revision_count(), 1)
        journal = self.conn.execute(
            "SELECT state, error_message FROM write_journal"
        ).fetchone()
        self.assertEqual(journal["state"], "failed")
        self.assertIn("commit boom", journal["error_message"])


class TestAtomicWriteHelper(WikiPageStoreTestBase):
    def test_atomic_write_creates_parent_and_returns_hash(self):
        target = os.path.join(self.root, "new", "deep", "file.md")
        digest = atomic_write_text(target, "内容\n")
        self.assertEqual(self.read_text_at(target), "内容\n")
        import hashlib

        self.assertEqual(digest, hashlib.sha256("内容\n".encode("utf-8")).hexdigest())
        self.assertEqual(
            glob.glob(os.path.join(os.path.dirname(target), ".*.tmp")), []
        )


class TestJobsRepository(WikiPageStoreTestBase):
    def test_job_lifecycle_and_invalid_transition(self):
        job = create_job(self.conn, "rebuild_links", {"scope": "all"})
        self.assertEqual(str(job.status), "pending")
        self.assertEqual(job.attempts, 0)

        claimed = claim_job(self.conn, job.job_id)
        self.assertEqual(str(claimed.status), "running")
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNotNone(claimed.started_at)

        finished = update_job_status(
            self.conn, job.job_id, "succeeded", result={"ok": True}
        )
        self.assertEqual(str(finished.status), "succeeded")
        self.assertIsNotNone(finished.finished_at)
        self.assertIn('"ok": true', finished.result_json)
        self.assertEqual(
            [item.job_id for item in list_jobs(self.conn, status="succeeded")],
            [job.job_id],
        )
        with self.assertRaises(JobTransitionError):
            update_job_status(self.conn, job.job_id, "running")

    def test_journal_state_and_detail_merge(self):
        journal = create_journal(
            self.conn, target_path="wiki-维基/concepts-概念/a.md", detail={"a": 1}
        )
        self.assertEqual(str(journal.state), "prepared")
        update_journal_state(
            self.conn, journal.journal_id, "file_written", detail={"b": 2}
        )
        update_journal_state(
            self.conn,
            journal.journal_id,
            "failed",
            error_message="boom",
            detail={"c": 3},
        )
        record = get_journal(self.conn, journal.journal_id)
        self.assertEqual(str(record.state), "failed")
        self.assertEqual(record.error_message, "boom")
        self.assertEqual(record.detail_json, '{"a": 1, "b": 2, "c": 3}')
        with self.assertRaises(ValueError):
            create_journal(self.conn, state="bogus")
        with self.assertRaises(ValueError):
            update_journal_state(self.conn, journal.journal_id, "bogus")


if __name__ == "__main__":
    unittest.main()
