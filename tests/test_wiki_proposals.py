# -*- coding: utf-8 -*-
"""提案：draft/diff 落盘、队列、approve/reject、stale 与重复 apply。"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.markdown import render_frontmatter
from kb.wiki.page_store import (
    GitCommitError,
    PageDraft,
    PageStore,
    PageValidationError,
)
from kb.wiki.proposals import (
    ProposalError,
    ProposalNotFoundError,
    ProposalService,
)
from kb.wiki.repositories import pages as pages_repo
from kb.wiki.repositories import proposals as proposals_repo


class WikiProposalTestBase(unittest.TestCase):
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
        self.init_git()
        self.store = PageStore(self.config, self.conn)
        self.service = ProposalService(self.config, self.conn, self.store)

    def init_git(self):
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "Wiki Test"],
            ["git", "config", "user.email", "wiki@test.local"],
            ["git", "config", "core.autocrlf", "false"],
            ["git", "config", "core.quotepath", "false"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
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

    def create_page(
        self,
        body="旧内容\n",
        *,
        status="published",
        slug="proposal-page",
        title="提案页面",
    ):
        return self.store.create_page(
            PageDraft(
                title=title,
                page_type="concept",
                summary="用于提案测试的页面。",
                body=body,
                slug=slug,
                status=status,
            ),
            commit=False,
        )

    def path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def read_text(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def full_document(self, page, body, **overrides):
        view = self.store.read_page(page.page_id)
        frontmatter = dict(view.frontmatter)
        frontmatter.update(overrides)
        return render_frontmatter(frontmatter, body)

    def create_update_proposal(self, page, proposed, expected=None):
        return self.service.create_proposal(
            proposal_type="update_page",
            target_page_id=page.page_id,
            proposed_content=proposed,
            rationale="补充新内容",
            expected_revision_id=(
                page.current_revision_id if expected is None else expected
            ),
            evidence_refs=[{"chunk_id": "chunk-1"}],
            created_by="agent",
        )

    def proposal_files(self, proposal_id):
        directory = os.path.join(
            os.path.dirname(self.config.db_path), "proposals", proposal_id
        )
        return (
            os.path.join(directory, "draft.md"),
            os.path.join(directory, "diff.md"),
        )

    def revision_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM page_revisions"
        ).fetchone()[0]


class TestCreateAndReadProposal(WikiProposalTestBase):
    def test_create_writes_draft_diff_and_pending_row(self):
        page = self.create_page()
        proposed = self.full_document(page, "# 新内容\n\n更新后正文。\n")
        proposal = self.create_update_proposal(page, proposed)

        self.assertEqual(str(proposal.status), "pending")
        self.assertEqual(proposal.page_id, page.page_id)
        self.assertEqual(proposal.expected_revision_id, page.current_revision_id)
        draft_path, diff_path = self.proposal_files(proposal.proposal_id)
        self.assertTrue(os.path.isfile(draft_path))
        self.assertTrue(os.path.isfile(diff_path))
        self.assertEqual(self.read_text(draft_path), proposed)
        diff = self.read_text(diff_path)
        self.assertIn("--- a/", diff)
        self.assertIn("+++ b/", diff)
        self.assertIn("-旧内容", diff)
        self.assertIn("+# 新内容", diff)

        detail = self.service.get_proposal(proposal.proposal_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail.draft, proposed)
        self.assertEqual(detail.diff, diff)
        self.assertEqual(
            detail.payload["evidence_refs"], [{"chunk_id": "chunk-1"}]
        )
        queue = self.service.review_queue()
        self.assertEqual(
            [item.proposal_id for item in queue], [proposal.proposal_id]
        )
        self.assertIsNone(self.service.get_proposal("missing"))

    def test_proposal_files_live_outside_vault_page_dirs(self):
        page = self.create_page()
        self.create_update_proposal(page, self.full_document(page, "新内容\n"))
        proposals_root = os.path.realpath(self.service.proposals_root)
        page_root = os.path.realpath(self.config.page_root)
        self.assertFalse(
            proposals_root == page_root
            or proposals_root.startswith(page_root + os.sep)
        )
        for _root, _dirs, files in os.walk(page_root):
            self.assertNotIn("draft.md", files)
            self.assertNotIn("diff.md", files)

    def test_invalid_inputs_rejected(self):
        page = self.create_page()
        with self.assertRaises(ValueError):
            self.service.create_proposal(
                "", page.page_id, "x", "r", page.current_revision_id, [], "agent"
            )
        with self.assertRaises(ValueError):
            self.service.create_proposal(
                "update_page",
                page.page_id,
                "x",
                "r",
                page.current_revision_id,
                [object()],
                "agent",
            )
        with self.assertRaises(ProposalError):
            self.service.create_proposal(
                "update_page",
                "missing-page",
                "x",
                "r",
                "",
                [],
                "agent",
            )
        self.assertEqual(self.service.review_queue(status="cancelled"), [])
        with self.assertRaises(ValueError):
            self.service.review_queue(status="bogus")


class TestCancelProposal(WikiProposalTestBase):
    def test_cancel_pending_proposal_persists_cancelled(self):
        page = self.create_page()
        proposal = self.create_update_proposal(
            page, self.full_document(page, "不应写入\n")
        )

        decision = self.service.cancel_proposal(
            proposal.proposal_id, actor="human", reason="不再需要"
        )

        self.assertTrue(decision.ok)
        self.assertEqual(decision.status, "cancelled")
        self.assertEqual(str(decision.proposal.status), "cancelled")
        self.assertEqual(decision.proposal.reviewed_by, "human")
        self.assertIsNotNone(decision.proposal.reviewed_at)
        self.assertEqual(decision.proposal.error_message, "")
        detail = self.service.get_proposal(proposal.proposal_id)
        self.assertEqual(detail.payload["cancel_reason"], "不再需要")
        self.assertEqual(self.service.review_queue(), [])
        self.assertEqual(
            [item.proposal_id for item in self.service.review_queue("cancelled")],
            [proposal.proposal_id],
        )

        # 取消后的提案不能再审核，页面保持不变。
        applied = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertFalse(applied.ok)
        self.assertEqual(applied.status, "cancelled")
        self.assertEqual(self.store.read_page(page.page_id).body, "旧内容\n")
        self.assertEqual(self.revision_count(), 1)

    def test_cancel_non_pending_or_missing_rejected(self):
        page = self.create_page()
        proposal = self.create_update_proposal(
            page, self.full_document(page, "新内容\n")
        )
        self.service.apply_proposal(
            proposal.proposal_id, approve=False, actor="human"
        )
        decision = self.service.cancel_proposal(proposal.proposal_id, actor="human")
        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, "rejected")
        with self.assertRaises(ProposalNotFoundError):
            self.service.cancel_proposal("missing", actor="human")


class TestApplyProposal(WikiProposalTestBase):
    def test_approve_applies_page_and_commits(self):
        page = self.create_page()
        proposed = self.full_document(page, "# 新内容\n")
        proposal = self.create_update_proposal(page, proposed)

        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, review_note="LGTM", actor="human"
        )

        self.assertTrue(decision.ok)
        self.assertEqual(decision.status, "applied")
        self.assertIsNotNone(decision.page)
        self.assertIsNotNone(decision.revision)
        self.assertEqual(decision.revision.revision_no, 2)
        self.assertEqual(
            decision.revision.source_proposal_id, proposal.proposal_id
        )
        self.assertEqual(decision.page.current_revision_id, decision.revision.revision_id)

        view = self.store.read_page(page.page_id)
        self.assertEqual(view.body, "# 新内容\n")
        self.assertEqual(
            view.frontmatter["revision_id"], decision.revision.revision_id
        )
        self.assertEqual(self.store.detect_external_edit(page.page_id), "clean")

        updated = self.service.get_proposal(proposal.proposal_id).record
        self.assertEqual(str(updated.status), "applied")
        self.assertEqual(updated.result_revision_id, decision.revision.revision_id)
        self.assertEqual(updated.reviewed_by, "human")
        self.assertIsNotNone(updated.reviewed_at)
        self.assertIsNotNone(updated.applied_at)
        self.assertEqual(
            self.service.get_proposal(proposal.proposal_id).payload["review_note"],
            "LGTM",
        )
        self.assertEqual(self.service.review_queue(), [])

        log = self.git("log", "-1", "--pretty=%s")
        self.assertIn("update proposal-page", log.stdout)
        names = self.git("show", "--name-only", "--pretty=", "HEAD")
        self.assertIn("wiki-维基/concepts-概念/proposal-page.md", names.stdout)
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")

    def test_body_only_proposal_preserves_frontmatter(self):
        page = self.create_page()
        proposal = self.create_update_proposal(page, "# 仅替换正文\n")
        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertTrue(decision.ok)
        view = self.store.read_page(page.page_id)
        self.assertEqual(view.body, "# 仅替换正文\n")
        self.assertEqual(view.frontmatter["title"], "提案页面")
        self.assertEqual(view.frontmatter["summary"], "用于提案测试的页面。")

    def test_reject_does_not_change_page(self):
        page = self.create_page()
        before = self.read_text(self.path(page))
        before_revisions = self.revision_count()
        proposal = self.create_update_proposal(
            page, self.full_document(page, "不应写入\n")
        )

        decision = self.service.apply_proposal(
            proposal.proposal_id,
            approve=False,
            review_note="理由不足",
            actor="human",
        )

        self.assertTrue(decision.ok)
        self.assertEqual(decision.status, "rejected")
        self.assertEqual(self.read_text(self.path(page)), before)
        self.assertEqual(self.revision_count(), before_revisions)
        updated = self.service.get_proposal(proposal.proposal_id).record
        self.assertEqual(str(updated.status), "rejected")
        self.assertEqual(updated.reviewed_by, "human")
        self.assertIsNone(updated.applied_at)
        self.assertEqual(self.service.review_queue(), [])

    def test_stale_expected_revision_marks_stale(self):
        page = self.create_page()
        proposal = self.create_update_proposal(
            page,
            self.full_document(page, "不应写入\n"),
            expected="01JABCDEFGHJKMNPQRSTVWXYZ0",
        )
        before = self.read_text(self.path(page))
        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, "stale")
        self.assertIn("过期", decision.message)
        self.assertEqual(self.read_text(self.path(page)), before)
        updated = self.service.get_proposal(proposal.proposal_id).record
        self.assertEqual(str(updated.status), "stale")
        self.assertIn("过期", updated.error_message)
        self.assertEqual(self.service.review_queue(), [])

    def test_external_edit_before_apply_marks_stale(self):
        page = self.create_page()
        proposal = self.create_update_proposal(
            page, self.full_document(page, "提案内容\n")
        )
        with open(self.path(page), "a", encoding="utf-8") as handle:
            handle.write("\n手工编辑\n")

        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )

        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, "stale")
        self.assertIn("手工", self.read_text(self.path(page)))
        self.assertNotIn("提案内容", self.read_text(self.path(page)))
        self.assertEqual(self.revision_count(), 1)

    def test_duplicate_apply_rejected(self):
        page = self.create_page()
        proposal = self.create_update_proposal(
            page, self.full_document(page, "第一次\n")
        )
        first = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertTrue(first.ok)
        revisions_after_first = self.revision_count()
        second = self.service.apply_proposal(
            proposal.proposal_id, approve=False, actor="human"
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.status, "applied")
        self.assertEqual(self.revision_count(), revisions_after_first)
        self.assertEqual(self.store.read_page(page.page_id).body, "第一次\n")

    def test_missing_proposal_raises(self):
        with self.assertRaises(ProposalNotFoundError):
            self.service.apply_proposal("missing", approve=True)

    def test_invalid_full_document_does_not_change_page(self):
        page = self.create_page()
        bad_document = self.full_document(page, "新内容\n", slug="other-slug")
        proposal = self.create_update_proposal(page, bad_document)
        with self.assertRaises(PageValidationError):
            self.service.apply_proposal(
                proposal.proposal_id, approve=True, actor="human"
            )
        self.assertEqual(self.revision_count(), 1)
        self.assertEqual(
            self.store.read_page(page.page_id).body, "旧内容\n"
        )
        updated = self.service.get_proposal(proposal.proposal_id).record
        # P1-2：apply 失败不能永久停在 approved；恢复为可重试的 pending。
        self.assertEqual(str(updated.status), "pending")
        self.assertIn("slug", updated.error_message)
        self.assertEqual(
            [item.proposal_id for item in self.service.review_queue()],
            [proposal.proposal_id],
        )
        self.assertEqual(
            pages_repo.get_page(self.conn, page.page_id).current_revision_id,
            page.current_revision_id,
        )


class TestApplyPublishingAndFrontmatter(WikiProposalTestBase):
    def test_approve_publishes_through_page_store_atomically(self):
        target = self.create_page(
            body="目标正文\n", slug="target-page", title="目标页面"
        )
        source = self.create_page(
            body="[[target-page]]\n", status="draft", slug="source-page"
        )
        proposal = self.create_update_proposal(
            source,
            "# 新正文\n\n参见 [[target-page]]。\n",
        )

        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )

        self.assertTrue(decision.ok)
        self.assertEqual(decision.status, "applied")
        self.assertEqual(str(decision.page.status), "published")
        view = self.store.read_page(source.page_id)
        self.assertEqual(str(view.page.status), "published")
        self.assertEqual(view.frontmatter["status"], "published")
        self.assertEqual(
            view.frontmatter["revision_id"], decision.revision.revision_id
        )
        self.assertEqual(
            view.content_sha256, decision.revision.content_sha256
        )
        self.assertEqual(view.body, "# 新正文\n\n参见 [[target-page]]。\n")
        links = view.links
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_page_id, target.page_id)
        backlinks = self.store.read_page(target.page_id).backlinks
        self.assertEqual([item.source_page_id for item in backlinks], [source.page_id])

    def test_full_document_proposal_preserves_unknown_frontmatter(self):
        page = self.create_page()
        frontmatter = dict(self.store.read_page(page.page_id).frontmatter)
        frontmatter["cssclasses"] = ["wide"]
        frontmatter["cover"] = "assets/cover.png"
        frontmatter["rating"] = 4
        proposal = self.create_update_proposal(
            page, render_frontmatter(frontmatter, "新正文\n")
        )

        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )

        self.assertTrue(decision.ok)
        view = self.store.read_page(page.page_id)
        self.assertEqual(view.frontmatter["cssclasses"], ["wide"])
        self.assertEqual(view.frontmatter["cover"], "assets/cover.png")
        self.assertEqual(view.frontmatter["rating"], 4)
        self.assertEqual(view.body, "新正文\n")


class TestApplyFailureRecovery(WikiProposalTestBase):
    def test_update_page_exception_restores_pending_and_retry(self):
        page = self.create_page()
        proposal = self.create_update_proposal(page, "# 重试正文\n")

        with mock.patch.object(
            self.store, "update_page", side_effect=RuntimeError("update boom")
        ):
            with self.assertRaises(RuntimeError):
                self.service.apply_proposal(
                    proposal.proposal_id, approve=True, actor="human"
                )

        restored = self.service.get_proposal(proposal.proposal_id).record
        self.assertEqual(str(restored.status), "pending")
        self.assertIn("update boom", restored.error_message)
        self.assertEqual(self.store.read_page(page.page_id).body, "旧内容\n")
        self.assertEqual(
            [item.proposal_id for item in self.service.review_queue()],
            [proposal.proposal_id],
        )

        retried = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertTrue(retried.ok)
        self.assertEqual(retried.status, "applied")
        self.assertEqual(self.store.read_page(page.page_id).body, "# 重试正文\n")

    def test_git_commit_failure_restores_pending_and_retry(self):
        page = self.create_page()
        proposal = self.create_update_proposal(page, "# git 重试\n")
        real_commit = PageStore.git_commit_page
        calls = {"count": 0}

        def flaky_commit(store_self, relative_path, message):
            calls["count"] += 1
            if calls["count"] == 1:
                raise GitCommitError("git boom")
            return real_commit(store_self, relative_path, message)

        with mock.patch.object(PageStore, "git_commit_page", new=flaky_commit):
            first = self.service.apply_proposal(
                proposal.proposal_id, approve=True, actor="human"
            )

            self.assertFalse(first.ok)
            self.assertEqual(first.status, "pending")
            self.assertIn("git boom", first.message)
            restored = self.service.get_proposal(proposal.proposal_id).record
            self.assertEqual(str(restored.status), "pending")
            current = pages_repo.get_page(self.conn, page.page_id)
            self.assertEqual(
                restored.expected_revision_id, current.current_revision_id
            )

            retried = self.service.apply_proposal(
                proposal.proposal_id, approve=True, actor="human"
            )
        self.assertTrue(retried.ok)
        self.assertEqual(retried.status, "applied")
        self.assertEqual(calls["count"], 2)
        view = self.store.read_page(page.page_id)
        self.assertEqual(view.body, "# git 重试\n")
        self.assertEqual(view.revision.revision_no, 3)

    def test_status_update_failure_restores_pending_and_retry(self):
        page = self.create_page()
        proposal = self.create_update_proposal(page, "# 状态重试\n")
        real_update = proposals_repo.update_proposal

        def flaky_update(conn, proposal_id, *, expected_status=None, **fields):
            if fields.get("status") == "applied":
                raise RuntimeError("status boom")
            return real_update(
                conn, proposal_id, expected_status=expected_status, **fields
            )

        with mock.patch.object(
            proposals_repo, "update_proposal", side_effect=flaky_update
        ):
            with self.assertRaises(RuntimeError):
                self.service.apply_proposal(
                    proposal.proposal_id, approve=True, actor="human"
                )

        restored = self.service.get_proposal(proposal.proposal_id).record
        self.assertEqual(str(restored.status), "pending")
        self.assertIn("status boom", restored.error_message)

        retried = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertTrue(retried.ok)
        self.assertEqual(retried.status, "applied")


class TestProposalStatusCas(WikiProposalTestBase):
    def test_terminal_status_cannot_be_overwritten(self):
        page = self.create_page()
        proposal = self.create_update_proposal(page, "# 已应用\n")
        decision = self.service.apply_proposal(
            proposal.proposal_id, approve=True, actor="human"
        )
        self.assertTrue(decision.ok)

        self.assertEqual(
            proposals_repo.update_proposal(
                self.conn, proposal.proposal_id, status="pending"
            ),
            0,
        )
        self.assertEqual(
            proposals_repo.update_proposal(
                self.conn, proposal.proposal_id, status="rejected"
            ),
            0,
        )
        self.assertEqual(
            str(self.service.get_proposal(proposal.proposal_id).record.status),
            "applied",
        )

        # 非状态字段仍可更新，但不会把终态改回去。
        self.assertEqual(
            proposals_repo.update_proposal(
                self.conn, proposal.proposal_id, error_message="audit note"
            ),
            1,
        )
        record = self.service.get_proposal(proposal.proposal_id).record
        self.assertEqual(str(record.status), "applied")
        self.assertEqual(record.error_message, "audit note")

    def test_concurrent_cancel_after_approval_cannot_win(self):
        page = self.create_page()
        proposal = self.create_update_proposal(page, "# 并发\n")
        original_update = self.store.update_page

        def racing_update(*args, **kwargs):
            cancel = self.service.cancel_proposal(
                proposal.proposal_id, actor="human"
            )
            self.assertFalse(cancel.ok)
            self.assertEqual(cancel.status, "approved")
            return original_update(*args, **kwargs)

        with mock.patch.object(self.store, "update_page", side_effect=racing_update):
            decision = self.service.apply_proposal(
                proposal.proposal_id, approve=True, actor="human"
            )

        self.assertTrue(decision.ok)
        self.assertEqual(decision.status, "applied")
        self.assertEqual(
            str(self.service.get_proposal(proposal.proposal_id).record.status),
            "applied",
        )
        self.assertEqual(self.store.read_page(page.page_id).body, "# 并发\n")


if __name__ == "__main__":
    unittest.main()
