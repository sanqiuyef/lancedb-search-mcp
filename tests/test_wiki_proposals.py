# -*- coding: utf-8 -*-
"""提案：draft/diff 落盘、队列、approve/reject、stale 与重复 apply。"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.markdown import render_frontmatter
from kb.wiki.page_store import PageDraft, PageStore, PageValidationError
from kb.wiki.proposals import (
    ProposalError,
    ProposalNotFoundError,
    ProposalService,
)
from kb.wiki.repositories import pages as pages_repo


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

    def create_page(self, body="旧内容\n"):
        return self.store.create_page(
            PageDraft(
                title="提案页面",
                page_type="concept",
                summary="用于提案测试的页面。",
                body=body,
                slug="proposal-page",
                status="published",
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
        with self.assertRaises(ValueError):
            self.service.review_queue(status="cancelled")


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
        self.assertEqual(str(updated.status), "approved")
        self.assertIn("slug", updated.error_message)
        self.assertEqual(
            pages_repo.get_page(self.conn, page.page_id).current_revision_id,
            page.current_revision_id,
        )


if __name__ == "__main__":
    unittest.main()
