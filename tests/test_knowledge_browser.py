# -*- coding: utf-8 -*-

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa

from knowledge_browser_core import (
    KnowledgeBrowserData,
    RelationStore,
    SourceResolver,
    load_kb_config,
    search_with_trace,
)
from mineru_pdf_splitter import split_pdf_for_mineru


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, _value):
        return self

    def to_list(self):
        return [dict(row) for row in self.rows]


class FakeSearchTable:
    def __init__(self):
        self.vector_rows = [
            {"source": "a.pdf", "chunk_index": 1, "text": "A", "category": "paper", "_distance": 0.1},
            {"source": "b.md", "chunk_index": 2, "text": "B", "category": "code", "_distance": 0.2},
        ]
        self.fts_rows = [
            {"source": "b.md", "chunk_index": 2, "text": "B", "category": "code"},
            {"source": "c.txt", "chunk_index": 0, "text": "C", "category": "paper"},
        ]

    def search(self, _query, query_type=None):
        return FakeQuery(self.fts_rows if query_type == "fts" else self.vector_rows)


class FakeSearchDb:
    def open_table(self, _name):
        return FakeSearchTable()


class FakeDataset:
    def __init__(self):
        self.columns_seen = []

    def to_table(self, columns):
        self.columns_seen.append(columns)
        return pa.table({
            "source": ["x.md", "x.md", "y.pdf"],
            "category": ["note", "note", "paper"],
        })


class FakeDocumentTable:
    def __init__(self):
        self.dataset = FakeDataset()

    def to_lance(self):
        return self.dataset


class BrowserCoreTests(unittest.TestCase):
    def test_registry_repairs_workspace_and_database_aliases(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root, "小论文（北松区）")
            direct = workspace / "knowledge"
            reasonix = workspace / ".reasonix" / "knowledge"
            direct.mkdir(parents=True)
            reasonix.mkdir(parents=True)
            config_path = Path(root, "kb-config.json")
            config_path.write_text(json.dumps({
                "knowledge_bases": {
                    "papers": {
                        "path": str(direct),
                        "source_roots": [str(workspace)],
                    },
                    "project-小论文": {
                        "path": str(reasonix),
                        "source_roots": [str(workspace)],
                    },
                    "project-小论文（北松区）": {
                        "path": str(reasonix),
                        "source_roots": [str(workspace)],
                    },
                },
                "default": "papers",
            }, ensure_ascii=False), encoding="utf-8")

            repaired, aliases = load_kb_config(str(config_path), repair=True)

            self.assertEqual(["project-小论文（北松区）"], list(repaired["knowledge_bases"]))
            self.assertEqual(str(reasonix), repaired["knowledge_bases"]["project-小论文（北松区）"]["path"])
            self.assertEqual("project-小论文（北松区）", repaired["default"])
            self.assertEqual({
                "papers": "project-小论文（北松区）",
                "project-小论文": "project-小论文（北松区）",
            }, aliases)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired, saved)

    def test_registry_rejects_two_workspaces_pointing_to_one_database(self):
        with tempfile.TemporaryDirectory() as root:
            shared_database = Path(root, "shared-db")
            first_workspace = Path(root, "First")
            second_workspace = Path(root, "Second")
            shared_database.mkdir()
            first_workspace.mkdir()
            second_workspace.mkdir()
            config_path = Path(root, "kb-config.json")
            config_path.write_text(json.dumps({
                "knowledge_bases": {
                    "project-First": {
                        "path": str(shared_database),
                        "source_roots": [str(first_workspace)],
                    },
                    "project-Second": {
                        "path": str(shared_database),
                        "source_roots": [str(second_workspace)],
                    },
                },
                "default": "project-Second",
            }), encoding="utf-8")

            repaired, aliases = load_kb_config(str(config_path), repair=True)

            self.assertEqual(["project-First"], list(repaired["knowledge_bases"]))
            self.assertEqual([str(first_workspace)], repaired["knowledge_bases"]["project-First"]["source_roots"])
            self.assertEqual({"project-Second": "project-First"}, aliases)
            self.assertEqual("project-First", repaired["default"])

    def test_document_listing_never_requests_vector_column(self):
        data = object.__new__(KnowledgeBrowserData)
        data._bases = {"kb": {"name": "kb", "path": "unused", "description": "", "source_roots": []}}
        data.resolver = SourceResolver()
        table = FakeDocumentTable()
        data._table = lambda _name: table
        result = data.list_documents("kb")
        self.assertEqual(2, result["total_documents"])
        self.assertEqual(3, result["total_chunks"])
        self.assertEqual([["source", "category"]], table.dataset.columns_seen)

    def test_hybrid_trace_retains_all_ranking_stages(self):
        def reranker(_query, documents, top_n):
            return [{**item, "relevance_score": 0.9 - index * 0.1}
                    for index, item in enumerate(reversed(documents[:top_n]))]

        result = search_with_trace(
            FakeSearchDb(), "query", mode="hybrid", limit=3, embedder=lambda _query: [0.0],
            reranker=reranker, category_router=lambda _query: {"paper": 1.1},
        )
        self.assertEqual(3, result["candidate_count"])
        self.assertEqual("ok", result["reranker_status"])
        self.assertTrue(all(item["rrf_score"] is not None for item in result["results"]))
        self.assertEqual([1, 2, 3], [item["final_rank"] for item in result["results"]])
        self.assertTrue(any(item["vector_rank"] and item["fts_rank"] for item in result["results"]))

    def test_reranker_failure_is_visible_and_falls_back(self):
        def fail(*_args):
            raise TimeoutError("timeout")

        result = search_with_trace(FakeSearchDb(), "query", mode="vector", limit=2,
                                   embedder=lambda _query: [0.0], reranker=fail)
        self.assertEqual("fallback", result["reranker_status"])
        self.assertIn("已回退", result["warning"])
        self.assertEqual([1, 2], [item["final_rank"] for item in result["results"]])

    def test_source_resolver_distinguishes_unique_ambiguous_and_missing(self):
        with tempfile.TemporaryDirectory() as root:
            first = Path(root, "first")
            second = Path(root, "second")
            first.mkdir()
            second.mkdir()
            Path(first, "unique.pdf").write_bytes(b"x")
            Path(first, "same.pdf").write_bytes(b"x")
            Path(second, "same.pdf").write_bytes(b"y")
            resolver = SourceResolver()
            self.assertEqual("resolved", resolver.resolve("unique.pdf", [root])["status"])
            self.assertEqual("ambiguous", resolver.resolve("same.pdf", [root])["status"])
            self.assertEqual("missing", resolver.resolve("absent.pdf", [root])["status"])

    def test_relation_v1_migrates_without_deleting_backup(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "relations.sqlite3")
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE relations (source_id TEXT, target_id TEXT, PRIMARY KEY(source_id,target_id))")
                connection.execute("INSERT INTO relations VALUES ('a','b')")
                connection.commit()
            finally:
                connection.close()
            store = RelationStore(database)
            relation = store.list_for_document("a")[0]
            self.assertEqual("related", relation["relation_type"])
            connection = sqlite3.connect(database)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                connection.close()
            self.assertIn("relations_legacy_v1", tables)

    def test_relation_store_remaps_legacy_knowledge_base_ids(self):
        with tempfile.TemporaryDirectory() as root:
            store = RelationStore(os.path.join(root, "relations.sqlite3"))
            store.add("papers::a.pdf", "project-通用::b.md", "supports", "evidence")

            moved = store.remap_knowledge_bases({"papers": "project-小论文（北松区）"})

            self.assertEqual(1, moved)
            relations = store.list_for_document("project-小论文（北松区）::a.pdf")
            self.assertEqual(1, len(relations))
            self.assertEqual("project-小论文（北松区）::a.pdf", relations[0]["source_id"])
            self.assertFalse(store.list_for_document("papers::a.pdf"))

    def test_pdf_splitter_caps_segments_at_200_pages_and_writes_offsets(self):
        import fitz

        with tempfile.TemporaryDirectory() as root:
            source_path = os.path.join(root, "large.pdf")
            output = os.path.join(root, "parts")
            with fitz.open() as document:
                for _ in range(401):
                    document.new_page()
                document.save(source_path)
            manifest = split_pdf_for_mineru(source_path, output)
            self.assertEqual([200, 200, 1], [item["page_count"] for item in manifest["segments"]])
            self.assertEqual([0, 200, 400], [item["page_offset"] for item in manifest["segments"]])
            with open(manifest["manifest_path"], "r", encoding="utf-8") as stream:
                saved = json.load(stream)
            self.assertEqual(401, saved["total_pages"])
            self.assertTrue(all(os.path.isfile(item["path"]) for item in saved["segments"]))


if __name__ == "__main__":
    unittest.main()
