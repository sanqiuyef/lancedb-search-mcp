# -*- coding: utf-8 -*-
"""Wiki 页面索引：LanceModel 写入、发布过滤、幂等、状态、重建与 CLI dry-run。"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.kb_test_support import activate_fake_embedding  # noqa: E402

activate_fake_embedding()

from kb import config as kb_config  # noqa: E402
from kb.wiki import page_index  # noqa: E402
from kb.wiki.config import WikiConfig  # noqa: E402
from kb.wiki.db import connect, run_migrations  # noqa: E402
from kb.wiki.models import PageRecord  # noqa: E402
from kb.wiki.page_store import PageDraft, PageStore  # noqa: E402
from kb.wiki.repositories import pages as pages_repo  # noqa: E402

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "rebuild_wiki_page_index.py",
)


class WikiPageIndexTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_tokenizer = kb_config.FTS_BASE_TOKENIZER
        kb_config.FTS_BASE_TOKENIZER = "simple"  # 测试环境不依赖 jieba 词典
        activate_fake_embedding()
        page_index.reset_page_model()

    @classmethod
    def tearDownClass(cls):
        kb_config.FTS_BASE_TOKENIZER = cls._original_tokenizer
        page_index.reset_db()
        page_index.reset_page_model()

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
        self.lance_db = os.path.join(self.root, "lancedb")
        page_index.reset_db()

    def tearDown(self):
        page_index.reset_db()

    # ── helpers ──
    def make_page(
        self,
        slug="urban-flood",
        status="published",
        page_type="concept",
        title=None,
        body=None,
        tags=None,
        aliases=None,
    ):
        draft = PageDraft(
            title=title or slug,
            page_type=page_type,
            summary=f"{slug} summary",
            body=body if body is not None else f"{slug} body text",
            slug=slug,
            tags=tags or [],
            aliases=aliases or [],
            status=status,
        )
        return self.store.create_page(draft)

    def index(self, page, body, aliases=None, tags=None, summary=""):
        return page_index.index_page(
            page,
            body,
            aliases or [],
            tags or [],
            self.config,
            summary=summary,
            db_path=self.lance_db,
        )

    def table(self):
        return page_index.get_table(
            self.config, self.lance_db, create=False
        )

    def rows(self):
        table = self.table()
        if table is None:
            return []
        return table.to_arrow().to_pylist()

    def page_path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )


class TestIndexPage(WikiPageIndexTestBase):
    def test_index_only_published(self):
        published = self.make_page("published-page", status="published")
        draft = self.make_page("draft-page", status="draft")

        info = self.index(published, "published body", ["别名"], ["tag1"])
        self.assertEqual(info["row_count"], 1)
        rows = self.rows()
        self.assertEqual([row["page_id"] for row in rows], [published.page_id])
        self.assertEqual(rows[0]["status"], "published")

        with self.assertRaises(page_index.PageNotPublishedError):
            self.index(draft, "draft body", [], [])
        self.assertEqual(len(self.rows()), 1)

    def test_index_stores_fields(self):
        page = self.make_page("field-page", title="Field Page")
        self.index(
            page,
            "body with keyword",
            aliases=["内涝", "urban flood"],
            tags=["flood", "urban"],
            summary="custom summary",
        )
        row = self.rows()[0]
        self.assertEqual(row["page_id"], page.page_id)
        self.assertEqual(row["revision_id"], page.current_revision_id)
        self.assertEqual(row["slug"], page.slug)
        self.assertEqual(row["title"], "Field Page")
        self.assertEqual(row["text"], "body with keyword")
        self.assertEqual(row["aliases_text"], "内涝 urban flood")
        self.assertEqual(row["tags_text"], "|flood|urban|")
        self.assertEqual(row["summary"], "custom summary")
        self.assertEqual(row["page_type"], "concept")
        self.assertEqual(row["updated_at"], page.updated_at)

    def test_duplicate_index_replaces_row(self):
        page = self.make_page("duplicate-page")
        self.index(page, "first body", ["a"], ["one"])
        self.index(page, "second body", ["b"], ["two"])

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "second body")
        self.assertEqual(rows[0]["aliases_text"], "b")
        self.assertEqual(rows[0]["tags_text"], "|two|")

    def test_sql_escaping_page_id_and_revision(self):
        page = PageRecord(
            page_id="page'with-quote",
            slug="quote-page",
            title="Quote Page",
            page_type="concept",
            status="published",
            current_revision_id="revision'1",
            relative_path="wiki-维基/concepts-概念/quote-page.md",
            created_at="2026-01-01T00:00:00.000Z",
            updated_at="2026-01-01T00:00:00.000Z",
        )
        self.index(page, "quoted body", [], [])
        self.index(page, "quoted body updated", [], [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["page_id"], "page'with-quote")
        self.assertEqual(rows[0]["revision_id"], "revision'1")
        self.assertEqual(rows[0]["text"], "quoted body updated")

        removed = page_index.remove_page(
            "page'with-quote", self.config, db_path=self.lance_db
        )
        self.assertEqual(removed, 1)
        self.assertEqual(self.rows(), [])

    def test_remove_page_missing_returns_zero(self):
        self.assertEqual(
            page_index.remove_page(
                "does-not-exist", self.config, db_path=self.lance_db
            ),
            0,
        )

    def test_text_index_created_vector_index_threshold(self):
        self.assertEqual(page_index.PAGE_VECTOR_INDEX_MIN_ROWS, 1000)
        page = self.make_page("index-page")
        self.index(page, "index body", [], [])
        names = {item.name for item in self.table().list_indices()}
        self.assertIn(page_index.PAGE_TEXT_INDEX, names)
        self.assertNotIn(page_index.PAGE_VECTOR_INDEX, names)

    def test_drop_page_index_explicit(self):
        page = self.make_page("drop-page")
        self.index(page, "drop body", [], [])
        self.assertTrue(
            page_index.drop_page_index(self.config, db_path=self.lance_db)
        )
        self.assertFalse(
            page_index.drop_page_index(self.config, db_path=self.lance_db)
        )
        self.assertIsNone(self.table())


class TestIndexStatus(WikiPageIndexTestBase):
    def test_status_before_index(self):
        page = self.make_page("status-page")
        status = page_index.index_status(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertFalse(status["exists"])
        self.assertEqual(status["row_count"], 0)
        self.assertEqual(status["published_count"], 1)
        self.assertEqual(status["missing_page_ids"], [page.page_id])

    def test_status_after_index_and_missing(self):
        first = self.make_page("status-first")
        second = self.make_page("status-second")
        self.index(first, "status body", [], [])

        status = page_index.index_status(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertTrue(status["exists"])
        self.assertEqual(status["row_count"], 1)
        self.assertEqual(status["published_count"], 2)
        self.assertEqual(status["missing_page_ids"], [second.page_id])

        self.index(second, "second status body", [], [])
        status = page_index.index_status(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(status["missing_page_ids"], [])

    def test_status_detects_stale_rows(self):
        page = self.make_page("stale-page")
        demoted = self.make_page("demoted-page")
        self.index(page, "stale body", [], [])
        self.index(demoted, "demoted body", [], [])
        pages_repo.update_page(self.conn, demoted.page_id, status="draft")
        self.conn.execute("DELETE FROM pages WHERE page_id = ?", (page.page_id,))

        status = page_index.index_status(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(
            status["stale_page_ids"], sorted([page.page_id, demoted.page_id])
        )
        self.assertEqual(status["published_count"], 0)

    def test_status_without_conn(self):
        page = self.make_page("status-noconn")
        self.index(page, "status body", [], [])
        status = page_index.index_status(
            None, self.config, db_path=self.lance_db
        )
        self.assertTrue(status["exists"])
        self.assertEqual(status["row_count"], 1)
        self.assertIsNone(status["published_count"])
        self.assertEqual(status["missing_page_ids"], [])


class TestRebuildAll(WikiPageIndexTestBase):
    def test_rebuild_is_incremental_and_prunes(self):
        first = self.make_page("rebuild-first")
        second = self.make_page("rebuild-second")
        draft = self.make_page("rebuild-draft", status="draft")

        stats = page_index.rebuild_all(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(stats["total_published"], 2)
        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["row_count"], 2)
        self.assertEqual(
            {row["page_id"] for row in self.rows()},
            {first.page_id, second.page_id},
        )
        self.assertNotIn(draft.page_id, {row["page_id"] for row in self.rows()})

        stats = page_index.rebuild_all(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(stats["row_count"], 2)

        pages_repo.update_page(self.conn, first.page_id, status="draft")
        stats = page_index.rebuild_all(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(stats["total_published"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["removed_stale"], 1)
        self.assertEqual(stats["row_count"], 1)
        self.assertEqual([row["page_id"] for row in self.rows()], [second.page_id])

        stats = page_index.rebuild_all(
            self.conn, self.config, db_path=self.lance_db, force=True
        )
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["row_count"], 1)

    def test_rebuild_records_failure_and_continues(self):
        first = self.make_page("failure-first")
        second = self.make_page("failure-second")
        os.remove(self.page_path(first))

        stats = page_index.rebuild_all(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(stats["total_published"], 2)
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["failures"][0]["page_id"], first.page_id)
        self.assertEqual([row["page_id"] for row in self.rows()], [second.page_id])

    def test_rebuild_no_published_pages(self):
        self.make_page("only-draft", status="draft")
        stats = page_index.rebuild_all(
            self.conn, self.config, db_path=self.lance_db
        )
        self.assertEqual(stats["total_published"], 0)
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["row_count"], 0)
        self.assertIsNone(self.table())


class TestRebuildScript(WikiPageIndexTestBase):
    def test_script_compiles(self):
        with open(SCRIPT_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        compile(source, SCRIPT_PATH, "exec")

    def _run_dry_run(self, *db_args):
        env = os.environ.copy()
        env["WIKI_VAULT_PATH"] = self.vault
        env["WIKI_DB_PATH"] = self.config.db_path
        env["LANCEDB_DB_PATH"] = self.lance_db
        env["LOCAL_MODEL_IDLE_UNLOAD"] = "0"
        command = [
            sys.executable,
            "-X",
            "utf8",
            SCRIPT_PATH,
            "--dry-run",
            *db_args,
            "--vault",
            self.vault,
            "--lance-db",
            self.lance_db,
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )

    def test_dry_run_does_not_write(self):
        self.make_page("dry-run-page")
        result = self._run_dry_run("--db", self.config.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("published 页面: 1", result.stdout)
        self.assertIn("dry-run 结束", result.stdout)
        self.assertIn("缺失索引页面: 1", result.stdout)
        # dry-run 不得创建 LanceDB 目录或索引表。
        self.assertFalse(os.path.exists(self.lance_db))

    def test_dry_run_accepts_wiki_db_alias(self):
        self.make_page("alias-page")
        result = self._run_dry_run("--wiki-db", self.config.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("published 页面: 1", result.stdout)


if __name__ == "__main__":
    unittest.main()
