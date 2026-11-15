# -*- coding: utf-8 -*-
"""scripts/migrate_wiki_v1.py：dry-run 只读、apply 幂等、行数断言与 limit。"""

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from kb import config as cfg
from kb import ingest as kb_ingest
from kb import schema as kb_schema
from kb.wiki.ids import normalize_text_sha256
from tests.kb_test_support import activate_fake_embedding

import migrate_wiki_v1 as migrate


DATA = [
    ("a.md", 0, "a-0-一"),
    ("a.md", 0, "a-0-二"),
    ("a.md", 1, "a-1-一"),
    ("a.md", 1, "a-1-二"),
    ("a.md", 2, "a-2"),
    ("b.md", 0, "b-0"),
    ("b.md", 1, "b-1"),
    ("b.md", 2, "b-2"),
    ("b.md", 3, "b-3"),
    ("b.md", 4, "b-4"),
    ("c.md", 0, "c-0-一"),
    ("c.md", 0, "c-0-二"),
    ("c.md", 0, "c-0-三"),
    ("c.md", 1, "c-1-一"),
    ("c.md", 1, "c-1-二"),
    ("d.md", 0, "d-0"),
    ("d.md", 1, "d-1"),
    ("d.md", 2, "d-2"),
    ("d.md", 3, "d-3"),
    ("d.md", 4, "d-4"),
]


class MigrateTestBase(unittest.TestCase):
    def setUp(self):
        activate_fake_embedding()
        cfg.FTS_BASE_TOKENIZER = "simple"
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lance_dir = os.path.join(self._tmp.name, "lancedb")
        self.wiki_path = os.path.join(self._tmp.name, "wiki", "wiki.sqlite3")

        previous_db = os.environ.get("LANCEDB_DB_PATH")
        previous_vault = os.environ.get("WIKI_VAULT_PATH")
        os.environ["LANCEDB_DB_PATH"] = self.lance_dir
        os.environ["WIKI_VAULT_PATH"] = os.path.join(self._tmp.name, "vault")
        kb_schema.reset_db()

        def cleanup_env():
            kb_schema.reset_db()
            for key, previous in (
                ("LANCEDB_DB_PATH", previous_db),
                ("WIKI_VAULT_PATH", previous_vault),
            ):
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

        self.addCleanup(cleanup_env)
        self._build_lance_db()

    def _build_lance_db(self):
        import lancedb

        table = kb_schema.get_or_create_table()
        chunks = [
            {
                "text": text,
                "source": source,
                "chunk_index": index,
                "category": "paper",
                "doc_id": kb_schema.chunk_doc_id(text, source, index),
                "ingested_at": "2026-11-15T00:00:00Z",
            }
            for source, index, text in DATA
        ]
        kb_ingest.add_chunks(table, chunks)
        db = lancedb.connect(self.lance_dir)
        db.create_table(
            "image_chunks",
            data=[{"id": index, "text": f"image-{index}"} for index in range(3)],
            mode="overwrite",
        )

    def run_main(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = ["--db", self.lance_dir, "--wiki-db", self.wiki_path, "--json"]
        argv.extend(args)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = migrate.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, *args):
        code, stdout, stderr = self.run_main(*args)
        self.assertEqual(code, 0, stderr)
        import json

        return json.loads(stdout)

    def lance_snapshot(self):
        import lancedb

        table = lancedb.connect(self.lance_dir).open_table("my_docs")
        arrow = table.to_lance().to_table(
            columns=["source", "chunk_index", "doc_id", "text"]
        )
        columns = [
            arrow.column(name).to_pylist()
            for name in ("source", "chunk_index", "doc_id", "text")
        ]
        return sorted(zip(*columns))

    def wiki_rows(self, sql, params=()):
        conn = sqlite3.connect(self.wiki_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def wiki_scalar(self, sql, params=()):
        rows = self.wiki_rows(sql, params)
        return rows[0][0] if rows else None


class TestDryRun(MigrateTestBase):
    def test_dry_run_reports_stats_and_writes_nothing(self):
        stats = self.run_json("--dry-run")
        self.assertEqual(stats["mode"], "dry-run")
        self.assertEqual(stats["rows"], 20)
        self.assertEqual(stats["total_rows"], 20)
        self.assertEqual(stats["examined_rows"], 20)
        self.assertEqual(stats["distinct_sources"], 4)
        self.assertEqual(stats["documents"], 4)
        self.assertEqual(stats["revisions"], 4)
        self.assertEqual(stats["chunk_refs"], 20)
        self.assertEqual(stats["documents_to_create"], 4)
        self.assertEqual(stats["revisions_to_create"], 4)
        self.assertEqual(stats["chunk_refs_to_create"], 20)
        self.assertEqual(
            stats["duplicate_source_chunk_pairs"], {"groups": 4, "rows": 9}
        )
        self.assertEqual(stats["duplicate_doc_ids"], {"groups": 0, "rows": 0})
        self.assertEqual(stats["identical_chunk_ref_rows"], 0)
        self.assertEqual(
            stats["lance_counts_before"],
            {"my_docs": 20, "image_chunks": 3},
        )
        self.assertEqual(
            stats["lance_counts_after"], stats["lance_counts_before"]
        )
        self.assertTrue(stats["lance_unchanged"])
        self.assertFalse(os.path.exists(self.wiki_path))

    def test_dry_run_limit(self):
        stats = self.run_json("--dry-run", "--limit", "5")
        self.assertEqual(stats["rows"], 5)
        self.assertEqual(stats["total_rows"], 20)
        self.assertEqual(stats["examined_rows"], 5)
        self.assertLessEqual(stats["chunk_refs_to_create"], 5)
        self.assertFalse(os.path.exists(self.wiki_path))

    def test_apply_and_dry_run_conflict(self):
        code, _, stderr = self.run_main("--apply", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("不能同时使用", stderr)
        self.assertFalse(os.path.exists(self.wiki_path))


class TestApply(MigrateTestBase):
    def test_apply_creates_legacy_registry_and_is_idempotent(self):
        before_snapshot = self.lance_snapshot()
        stats = self.run_json("--apply")
        self.assertEqual(stats["mode"], "apply")
        self.assertEqual(
            stats["migrations_applied"], ["0001", "0002", "0003", "0004"]
        )
        self.assertEqual(stats["rows_processed"], 20)
        self.assertEqual(stats["documents_created"], 4)
        self.assertEqual(stats["revisions_created"], 4)
        self.assertEqual(stats["chunk_refs_inserted"], 20)
        self.assertTrue(stats["lance_unchanged"])
        self.assertEqual(
            stats["lance_counts_before"],
            stats["lance_counts_after"],
        )
        self.assertEqual(before_snapshot, self.lance_snapshot())

        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM documents"), 4
        )
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM document_revisions"), 4
        )
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM chunk_refs"), 20
        )

        revisions = self.wiki_rows("SELECT * FROM document_revisions")
        for revision in revisions:
            self.assertEqual(revision["status"], "legacy_unverified")
            self.assertEqual(revision["parser_name"], "legacy_import")
            self.assertEqual(revision["raw_sha256"], "")
            self.assertEqual(revision["parsed_sha256"], "")
            self.assertGreater(revision["chunk_count"], 0)

        documents = self.wiki_rows("SELECT * FROM documents")
        for document in documents:
            self.assertIsNotNone(document["current_revision_id"])

        actual = {
            (row["source_value"], row["chunk_index"], row["text_sha256"])
            for row in self.wiki_rows(
                "SELECT source_value, chunk_index, text_sha256 FROM chunk_refs"
            )
        }
        expected = {
            (source, index, normalize_text_sha256(text))
            for source, index, text in DATA
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 20)

        # 第二次 apply：不新增任何行
        second = self.run_json("--apply")
        self.assertEqual(second["documents_created"], 0)
        self.assertEqual(second["revisions_created"], 0)
        self.assertEqual(second["chunk_refs_inserted"], 0)
        self.assertEqual(second["documents_existing"], 4)
        self.assertEqual(second["revisions_existing"], 4)
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM documents"), 4
        )
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM document_revisions"), 4
        )
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM chunk_refs"), 20
        )

    def test_limit_applies_subset_then_resumes(self):
        partial = self.run_json("--apply", "--limit", "7")
        self.assertEqual(partial["rows_processed"], 7)
        self.assertEqual(partial["chunk_refs_inserted"], 7)
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM chunk_refs"), 7
        )
        self.assertLessEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM documents"), 4
        )

        dry = self.run_json("--dry-run")
        existing_refs = self.wiki_scalar("SELECT COUNT(*) FROM chunk_refs")
        existing_docs = self.wiki_scalar("SELECT COUNT(*) FROM documents")
        self.assertEqual(dry["chunk_refs_to_create"], 20 - existing_refs)
        self.assertEqual(dry["documents_to_create"], 4 - existing_docs)

        full = self.run_json("--apply")
        self.assertEqual(full["chunk_refs_inserted"], 20 - existing_refs)
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM chunk_refs"), 20
        )
        self.assertEqual(
            self.wiki_scalar("SELECT COUNT(*) FROM documents"), 4
        )

        again = self.run_json("--apply")
        self.assertEqual(again["chunk_refs_inserted"], 0)

    def test_apply_fails_when_lance_row_count_changes(self):
        real_counts = migrate._lance_counts
        calls = {"count": 0}

        def fake_counts(db_path):
            calls["count"] += 1
            result = real_counts(db_path)
            if calls["count"] == 2:
                result = dict(result)
                result["my_docs"] += 1
            return result

        with mock.patch.object(migrate, "_lance_counts", side_effect=fake_counts):
            code, _, stderr = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("只读断言失败", stderr)


if __name__ == "__main__":
    unittest.main()
