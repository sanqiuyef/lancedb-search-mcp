# -*- coding: utf-8 -*-
"""recovery：六种 journal 状态、只读扫描、apply 仅收敛 completed、不删文件。"""

import glob
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.page_store import PageDraft, PageStore
from kb.wiki.recovery import (
    ACTION_COMPLETE,
    ACTION_MANUAL_REVIEW,
    ACTION_MARK_FAILED,
    ACTION_NEEDS_REGISTRATION,
    ACTION_REBUILD_INDEX,
    ACTION_REPORT_ONLY,
    ACTION_UPDATE_INDEX,
    OPEN_JOURNAL_STATES,
    recover,
    scan_journal,
)
from kb.wiki.repositories import jobs as jobs_repo


class WikiRecoveryTestBase(unittest.TestCase):
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

    def create_page(self, slug="recovery-page", body="恢复测试正文。\n"):
        return self.store.create_page(
            PageDraft(
                title=f"Title {slug}",
                page_type="concept",
                summary="恢复测试页面摘要。",
                body=body,
                slug=slug,
                status="published",
            ),
            commit=False,
        )

    def page_path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def create_journal(self, state, page=None, **overrides):
        values = {
            "page_id": page.page_id if page is not None else None,
            "revision_id": page.current_revision_id if page is not None else "",
            "target_path": page.relative_path if page is not None else "",
        }
        values.update(overrides)
        return jobs_repo.create_journal(self.conn, state=state, **values)

    def journal_state(self, journal_id):
        return str(jobs_repo.get_journal(self.conn, journal_id).state)

    def journal_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM write_journal").fetchone()[0]

    def vault_files(self):
        return sorted(
            os.path.relpath(path, self.vault)
            for path in glob.glob(
                os.path.join(self.vault, "**", "*.md"), recursive=True
            )
        )


class TestScanJournal(WikiRecoveryTestBase):
    def test_scan_classifies_open_states_and_excludes_completed(self):
        page = self.create_page()
        journals = {}
        for state in OPEN_JOURNAL_STATES:
            journals[state] = self.create_journal(state, page)
        completed = self.create_journal("completed", page)

        scanned = scan_journal(self.conn)

        self.assertEqual(tuple(scanned), OPEN_JOURNAL_STATES)
        for state in OPEN_JOURNAL_STATES:
            self.assertEqual(len(scanned[state]), 1)
            self.assertEqual(str(scanned[state][0].state), state)
            self.assertEqual(scanned[state][0].journal_id, journals[state].journal_id)
        all_ids = {
            record.journal_id
            for state in OPEN_JOURNAL_STATES
            for record in scanned[state]
        }
        self.assertNotIn(completed.journal_id, all_ids)


class TestPrepared(WikiRecoveryTestBase):
    def test_prepared_missing_file_reports_mark_failed_without_write(self):
        journal = self.create_journal(
            "prepared",
            target_path="wiki-维基/concepts-概念/never-written.md",
        )

        actions = recover(self.conn, self.config, apply=False)
        action = next(
            item for item in actions if item["journal_id"] == journal.journal_id
        )
        self.assertEqual(set(action), {"journal_id", "state", "action", "detail"})
        self.assertEqual(action["state"], "prepared")
        self.assertEqual(action["action"], ACTION_MARK_FAILED)
        self.assertEqual(self.journal_state(journal.journal_id), "prepared")

        recover(self.conn, self.config, apply=True)
        self.assertEqual(self.journal_state(journal.journal_id), "prepared")

    def test_prepared_existing_file_and_db_converge_completes(self):
        page = self.create_page()
        journal = self.create_journal("prepared", page)

        actions = recover(self.conn, self.config, apply=False)
        action = next(
            item for item in actions if item["journal_id"] == journal.journal_id
        )
        self.assertEqual(action["action"], ACTION_COMPLETE)
        self.assertEqual(self.journal_state(journal.journal_id), "prepared")

        recover(self.conn, self.config, apply=True)
        self.assertEqual(self.journal_state(journal.journal_id), "completed")


class TestFileWritten(WikiRecoveryTestBase):
    def test_matching_hash_completes_only_with_apply(self):
        page = self.create_page()
        journal = self.create_journal("file_written", page)
        before_files = self.vault_files()
        before_journals = self.journal_count()

        actions = recover(self.conn, self.config, apply=False)
        action = next(
            item for item in actions if item["journal_id"] == journal.journal_id
        )
        self.assertEqual(action["action"], ACTION_COMPLETE)
        self.assertEqual(self.journal_state(journal.journal_id), "file_written")

        recover(self.conn, self.config, apply=True)
        self.assertEqual(self.journal_state(journal.journal_id), "completed")
        self.assertEqual(self.vault_files(), before_files)
        self.assertEqual(self.journal_count(), before_journals)

    def test_hash_mismatch_requires_manual_review(self):
        page = self.create_page()
        journal = self.create_journal("file_written", page)
        with open(self.page_path(page), "a", encoding="utf-8") as handle:
            handle.write("\n手工修改\n")

        actions = recover(self.conn, self.config, apply=False)
        action = next(
            item for item in actions if item["journal_id"] == journal.journal_id
        )
        self.assertEqual(action["action"], ACTION_MANUAL_REVIEW)
        self.assertIn("sha256", action["detail"])

        recover(self.conn, self.config, apply=True)
        self.assertEqual(self.journal_state(journal.journal_id), "file_written")

    def test_missing_db_record_needs_registration(self):
        page = self.create_page()
        journal = self.create_journal(
            "file_written",
            page,
            revision_id="01JABCDEFGHJKMNPQRSTVWXYZ9",
        )

        actions = recover(self.conn, self.config, apply=False)
        action = next(
            item for item in actions if item["journal_id"] == journal.journal_id
        )
        self.assertEqual(action["action"], ACTION_NEEDS_REGISTRATION)
        self.assertIn("revision_id", action["detail"])

        recover(self.conn, self.config, apply=True)
        self.assertEqual(self.journal_state(journal.journal_id), "file_written")


class TestOtherStates(WikiRecoveryTestBase):
    def test_db_committed_index_pending_failed_actions(self):
        page = self.create_page()
        db_committed = self.create_journal("db_committed", page)
        index_pending = self.create_journal("index_pending", page)
        failed = self.create_journal(
            "failed", page, error_message="git commit 失败"
        )

        actions = recover(self.conn, self.config, apply=False)
        by_id = {item["journal_id"]: item for item in actions}
        self.assertEqual(by_id[db_committed.journal_id]["action"], ACTION_UPDATE_INDEX)
        self.assertEqual(
            by_id[index_pending.journal_id]["action"], ACTION_REBUILD_INDEX
        )
        self.assertEqual(by_id[failed.journal_id]["action"], ACTION_REPORT_ONLY)
        self.assertIn("git commit 失败", by_id[failed.journal_id]["detail"])

        recover(self.conn, self.config, apply=True)
        self.assertEqual(self.journal_state(db_committed.journal_id), "db_committed")
        self.assertEqual(
            self.journal_state(index_pending.journal_id), "index_pending"
        )
        self.assertEqual(self.journal_state(failed.journal_id), "failed")

    def test_apply_completes_only_convergent_and_never_deletes(self):
        page = self.create_page()
        convergent = self.create_journal("file_written", page)
        prepared_missing = self.create_journal(
            "prepared",
            target_path="wiki-维基/concepts-概念/missing.md",
        )
        db_committed = self.create_journal("db_committed", page)
        index_pending = self.create_journal("index_pending", page)
        failed = self.create_journal("failed", page, error_message="boom")
        before_files = self.vault_files()
        before_journals = self.journal_count()
        before_revisions = self.conn.execute(
            "SELECT COUNT(*) FROM page_revisions"
        ).fetchone()[0]

        actions = recover(self.conn, self.config, apply=True)

        completed_actions = [
            item for item in actions if item["action"] == ACTION_COMPLETE
        ]
        self.assertEqual(
            [item["journal_id"] for item in completed_actions],
            [convergent.journal_id],
        )
        self.assertIn("已标 completed", completed_actions[0]["detail"])
        self.assertEqual(self.journal_state(convergent.journal_id), "completed")
        for journal in (prepared_missing, db_committed, index_pending, failed):
            self.assertNotEqual(self.journal_state(journal.journal_id), "completed")
        self.assertEqual(self.journal_state(prepared_missing.journal_id), "prepared")
        self.assertEqual(self.journal_state(db_committed.journal_id), "db_committed")
        self.assertEqual(self.journal_state(index_pending.journal_id), "index_pending")
        self.assertEqual(self.journal_state(failed.journal_id), "failed")

        self.assertEqual(self.vault_files(), before_files)
        self.assertEqual(self.journal_count(), before_journals)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM page_revisions").fetchone()[0],
            before_revisions,
        )


if __name__ == "__main__":
    unittest.main()
