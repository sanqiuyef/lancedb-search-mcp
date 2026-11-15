# -*- coding: utf-8 -*-
"""scripts/relink_sources_to_vault.py：唯一匹配、行数不变、幂等与 missing 标记。"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import lancedb  # noqa: E402

import relink_sources_to_vault as relink  # noqa: E402

PAPER_REL = os.path.join("08-literature-文献", "papers-论文全文")


class RelinkTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_dir = os.path.join(self._tmp.name, "lancedb")
        self.vault = os.path.join(self._tmp.name, "vault")
        self.paper_dir = os.path.join(self.vault, PAPER_REL)
        os.makedirs(self.paper_dir, exist_ok=True)
        self.wiki_path = os.path.join(self.vault, ".wiki", "wiki.sqlite3")
        os.makedirs(os.path.dirname(self.wiki_path), exist_ok=True)

        self.paper_md = os.path.join(self.paper_dir, "Paper A.md")
        with open(self.paper_md, "w", encoding="utf-8") as handle:
            handle.write("# Paper A\n\n正文。\n")

        db = lancedb.connect(self.db_dir)
        db.create_table("my_docs", data=[
            {"text": "paper-a-0", "source": "Paper A.pdf", "chunk_index": 0},
            {"text": "paper-a-1", "source": "Paper A.pdf", "chunk_index": 1},
            {"text": "missing", "source": "Missing.pdf", "chunk_index": 0},
            {"text": "existing", "source": os.path.join(
                PAPER_REL, "Existing.md"), "chunk_index": 0},
        ])
        self._create_wiki()

    def _create_wiki(self):
        conn = sqlite3.connect(self.wiki_path)
        try:
            conn.executescript(
                """
                CREATE TABLE documents (
                    document_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    canonical_uri TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (source_type, canonical_uri)
                );
                CREATE TABLE chunk_refs (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    source_value TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.executemany(
                "INSERT INTO documents "
                "(document_id, source_type, canonical_uri, display_name, status) "
                "VALUES (?, 'file', ?, ?, 'active')",
                [
                    ("doc-a", "Paper A.pdf", "Paper A.pdf"),
                    ("doc-missing", "Missing.pdf", "Missing.pdf"),
                ],
            )
            conn.executemany(
                "INSERT INTO chunk_refs (chunk_id, document_id, source_value) "
                "VALUES (?, ?, ?)",
                [
                    ("c-a-0", "doc-a", "Paper A.pdf"),
                    ("c-a-1", "doc-a", "Paper A.pdf"),
                    ("c-missing", "doc-missing", "Missing.pdf"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def run_main(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "--db", self.db_dir,
            "--vault", self.vault,
            "--wiki-db", self.wiki_path,
            "--json",
        ]
        argv.extend(args)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = relink.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, *args):
        code, stdout, stderr = self.run_main(*args)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def lance_snapshot(self):
        table = lancedb.connect(self.db_dir).open_table("my_docs")
        arrow = table.to_lance().to_table(columns=["source", "text", "chunk_index"])
        columns = [
            arrow.column(name).to_pylist()
            for name in ("source", "text", "chunk_index")
        ]
        return sorted(zip(*columns))

    def sources(self):
        table = lancedb.connect(self.db_dir).open_table("my_docs")
        arrow = table.to_lance().to_table(columns=["source"])
        return sorted(arrow.column("source").to_pylist())

    def wiki_rows(self, sql, params=()):
        conn = sqlite3.connect(self.wiki_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def wiki_document(self, document_id):
        rows = self.wiki_rows(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        )
        return rows[0] if rows else None


class TestDryRun(RelinkTestBase):
    def test_dry_run_reports_categories_and_writes_nothing(self):
        before_snapshot = self.lance_snapshot()
        before_docs = self.wiki_rows(
            "SELECT document_id, canonical_uri, display_name, status "
            "FROM documents ORDER BY document_id")
        before_refs = self.wiki_rows(
            "SELECT chunk_id, document_id, source_value "
            "FROM chunk_refs ORDER BY chunk_id")

        stats = self.run_json("--dry-run")

        self.assertEqual(stats["mode"], "dry-run")
        self.assertEqual(stats["rows_before"], 4)
        self.assertEqual(stats["rows_after"], 4)
        self.assertEqual(stats["distinct_sources"], 3)
        self.assertEqual(len(stats["already_relative"]), 1)
        self.assertEqual(
            set(stats["legacy_basename"]),
            {"Paper A.pdf", "Missing.pdf"},
        )
        self.assertEqual(len(stats["relinkable"]), 1)
        self.assertEqual(stats["relinkable"][0]["source"], "Paper A.pdf")
        self.assertEqual(
            stats["relinkable"][0]["new_source"],
            os.path.relpath(self.paper_md, self.vault),
        )
        self.assertEqual(stats["relinkable"][0]["rows"], 2)
        self.assertEqual(len(stats["no_match"]), 1)
        self.assertEqual(stats["no_match"][0]["source"], "Missing.pdf")
        self.assertEqual(stats["no_match"][0]["reason"], "no_markdown")
        self.assertTrue(stats["wiki"]["available"])
        self.assertEqual(stats["wiki"]["documents_to_relink"], 1)
        self.assertEqual(stats["wiki"]["documents_to_mark_missing"], 1)

        self.assertEqual(self.lance_snapshot(), before_snapshot)
        self.assertEqual(
            self.wiki_rows(
                "SELECT document_id, canonical_uri, display_name, status "
                "FROM documents ORDER BY document_id"),
            before_docs,
        )
        self.assertEqual(
            self.wiki_rows(
                "SELECT chunk_id, document_id, source_value "
                "FROM chunk_refs ORDER BY chunk_id"),
            before_refs,
        )


class TestApply(RelinkTestBase):
    def test_apply_relinks_unique_match_and_keeps_row_count(self):
        before_count = len(self.lance_snapshot())
        new_source = os.path.relpath(self.paper_md, self.vault)

        stats = self.run_json("--apply")

        self.assertEqual(stats["mode"], "apply")
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["rows_before"], before_count)
        self.assertEqual(stats["rows_after"], before_count)
        self.assertTrue(stats["row_count_unchanged"])
        self.assertEqual(stats["wiki"]["documents_updated"], 1)
        self.assertEqual(stats["wiki"]["chunk_refs_updated"], 2)

        self.assertEqual(len(self.lance_snapshot()), before_count)
        sources = self.sources()
        self.assertEqual(sources.count(new_source), 2)
        self.assertNotIn("Paper A.pdf", sources)
        self.assertIn("Missing.pdf", sources)
        self.assertEqual(
            sources.count(os.path.join(PAPER_REL, "Existing.md")), 1
        )
        texts = [row[1] for row in self.lance_snapshot()]
        self.assertIn("paper-a-0", texts)
        self.assertIn("paper-a-1", texts)

        document = self.wiki_document("doc-a")
        self.assertIsNotNone(document)
        self.assertEqual(document["document_id"], "doc-a")
        self.assertEqual(document["canonical_uri"], new_source.replace("\\", "/"))
        self.assertEqual(document["display_name"], "Paper A.md")
        self.assertEqual(document["status"], "active")
        self.assertEqual(
            self.wiki_rows(
                "SELECT source_value FROM chunk_refs WHERE document_id = 'doc-a'"
            )[0]["source_value"],
            new_source,
        )
        self.assertEqual(
            [row["source_value"] for row in self.wiki_rows(
                "SELECT source_value FROM chunk_refs "
                "WHERE document_id = 'doc-missing'")],
            ["Missing.pdf"],
        )
        self.assertEqual(self.wiki_document("doc-missing")["status"], "active")

    def test_apply_is_idempotent(self):
        self.run_json("--apply")
        snapshot = self.lance_snapshot()

        stats = self.run_json("--apply")

        self.assertEqual(stats["applied"], 0)
        self.assertEqual(stats["relinkable"], [])
        self.assertEqual(stats["rows_after"], stats["rows_before"])
        self.assertTrue(stats["row_count_unchanged"])
        self.assertEqual(stats["wiki"]["documents_updated"], 0)
        self.assertEqual(stats["wiki"]["chunk_refs_updated"], 0)
        self.assertEqual(self.lance_snapshot(), snapshot)

    def test_mark_missing_only_changes_status(self):
        before_missing = dict(self.wiki_document("doc-missing"))

        stats = self.run_json("--apply", "--mark-missing")

        self.assertEqual(stats["wiki"]["documents_marked_missing"], 1)
        document = self.wiki_document("doc-missing")
        self.assertEqual(document["document_id"], "doc-missing")
        self.assertEqual(document["canonical_uri"], "Missing.pdf")
        self.assertEqual(document["display_name"], "Missing.pdf")
        self.assertEqual(document["status"], "missing")
        self.assertTrue(document["updated_at"])
        self.assertEqual(before_missing["status"], "active")
        # LanceDB 行保持原样，不删除。
        self.assertIn("Missing.pdf", self.sources())
        self.assertEqual(len(self.lance_snapshot()), 4)

        second = self.run_json("--apply", "--mark-missing")
        self.assertEqual(second["wiki"]["documents_marked_missing"], 0)

    def test_apply_without_mark_missing_leaves_status(self):
        self.run_json("--apply")
        self.assertEqual(self.wiki_document("doc-missing")["status"], "active")


class TestAmbiguousMatch(RelinkTestBase):
    def test_multiple_same_stem_markdown_is_reported_not_changed(self):
        os.makedirs(os.path.join(self.paper_dir, "sub"), exist_ok=True)
        for path in (
            os.path.join(self.paper_dir, "Dup.md"),
            os.path.join(self.paper_dir, "sub", "Dup.md"),
        ):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# Dup\n")
        db = lancedb.connect(self.db_dir)
        db.open_table("my_docs").add([
            {"text": "dup", "source": "Dup.pdf", "chunk_index": 0},
        ])

        stats = self.run_json("--dry-run")

        ambiguous = [
            item for item in stats["no_match"] if item["source"] == "Dup.pdf"
        ]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["reason"], "ambiguous_markdown")
        self.assertEqual(len(ambiguous[0]["candidates"]), 2)
        self.assertIn("Dup.pdf", self.sources())


class TestManualMap(RelinkTestBase):
    def test_manual_map_can_use_different_stem(self):
        manual_md = os.path.join(self.paper_dir, "Manual Target.md")
        with open(manual_md, "w", encoding="utf-8") as handle:
            handle.write("# Manual\n")
        mapping = "Paper A.pdf=" + os.path.relpath(manual_md, self.vault)
        before = len(self.lance_snapshot())

        dry = self.run_json("--dry-run", "--map", mapping)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertEqual(len(dry["relinkable"]), 1)
        self.assertTrue(dry["relinkable"][0]["manual"])
        self.assertEqual(
            dry["relinkable"][0]["new_source"],
            os.path.relpath(manual_md, self.vault),
        )

        stats = self.run_json("--apply", "--map", mapping)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(len(self.lance_snapshot()), before)
        sources = self.sources()
        self.assertEqual(sources.count(os.path.relpath(manual_md, self.vault)), 2)
        self.assertNotIn("Paper A.pdf", sources)

    def test_manual_map_missing_target_is_reported(self):
        mapping = "Paper A.pdf=" + os.path.join(PAPER_REL, "Not There.md")
        stats = self.run_json("--dry-run", "--map", mapping)
        self.assertEqual(stats["relinkable"], [])
        item = next(x for x in stats["no_match"] if x["source"] == "Paper A.pdf")
        self.assertEqual(item["reason"], "manual_target_missing")


if __name__ == "__main__":
    unittest.main()
