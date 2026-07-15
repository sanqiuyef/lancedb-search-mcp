# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

from content_graph import ContentGraphStore, HeuristicExtractor, OpenAICompatibleExtractor


class FakeExtractor:
    name = "fake"
    version = "1"

    def __init__(self):
        self.calls = 0

    def extract(self, chunks):
        self.calls += 1
        return {
            "entities": [
                {"label": "CNN-LSTM", "kind": "method", "chunk_index": 0, "confidence": 0.9},
                {"label": "Flood Prediction", "kind": "entity", "chunk_index": 0, "confidence": 0.8},
                {"label": "Flood Prediction", "kind": "entity", "chunk_index": 1, "confidence": 0.7},
            ],
            "claims": [{
                "text": "CNN-LSTM improves flood prediction accuracy.",
                "chunk_index": 1,
                "confidence": 0.85,
                "entities": ["CNN-LSTM", "Flood Prediction"],
            }],
            "relations": [{
                "source": "CNN-LSTM",
                "target": "Flood Prediction",
                "type": "supports",
                "chunk_index": 1,
                "confidence": 0.75,
                "note": "fake evidence",
            }],
        }


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": """```json
            {"entities":[
              {"label":"CNN-LSTM","kind":"method","chunk_index":0,"confidence":0.9},
              {"label":"invented","kind":"entity","chunk_index":999,"confidence":1.0}
            ],"claims":[],"relations":[]}
            ```"""}}]
        }


def sample_chunks(second="Results show that CNN-LSTM improves flood prediction accuracy."):
    return [
        {"chunk_index": 0, "text": "We propose a CNN-LSTM model for Flood Prediction.", "category": "paper"},
        {"chunk_index": 1, "text": second, "category": "paper"},
    ]


class ContentGraphTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.remove(self.path)
        self.store = ContentGraphStore(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_graph_is_content_addressed_and_reuses_extraction_after_move(self):
        extractor = FakeExtractor()
        first = self.store.index_document("papers", "old/paper.pdf", sample_chunks(), extractor)
        second = self.store.index_document("papers", "new/paper.pdf", sample_chunks(), extractor)

        self.assertEqual("indexed", first["status"])
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertEqual(1, extractor.calls)
        self.assertEqual(1, self.store.stats()["documents"])
        self.assertEqual(2, self.store.stats()["locations"])

    def test_changed_content_replaces_orphaned_document_for_same_location(self):
        extractor = FakeExtractor()
        first = self.store.index_document("papers", "paper.pdf", sample_chunks(), extractor)
        changed = self.store.index_document(
            "papers", "paper.pdf", sample_chunks("Results show a materially different conclusion."), extractor
        )

        self.assertNotEqual(first["document_id"], changed["document_id"])
        self.assertEqual(1, self.store.stats()["documents"])
        self.assertEqual(changed["document_id"], self.store.document_for_location("papers", "paper.pdf"))

    def test_document_graph_contains_evidence_backed_content_nodes(self):
        result = self.store.index_document("papers", "paper.pdf", sample_chunks(), FakeExtractor())
        graph = self.store.get_document_graph("papers", "paper.pdf")
        kinds = {node["kind"] for node in graph["nodes"]}
        relation_types = {edge["relation_type"] for edge in graph["edges"]}

        self.assertEqual(result["document_id"], graph["document_id"])
        self.assertTrue({"document", "chunk", "entity", "method", "claim"}.issubset(kinds))
        self.assertTrue({"contains", "mentions", "asserts", "about", "supports"}.issubset(relation_types))
        support = next(edge for edge in graph["edges"] if edge["relation_type"] == "supports")
        evidence = self.store.get_evidence(support["id"])
        self.assertEqual(1, evidence["chunk_index"])
        self.assertEqual("paper.pdf", evidence["source"])
        self.assertIn("improves", evidence["text_preview"].casefold())

    def test_offline_extractor_marks_claims_and_methods(self):
        extraction = HeuristicExtractor().extract(sample_chunks())
        self.assertTrue(any(item["kind"] == "method" for item in extraction["entities"]))
        self.assertTrue(extraction["claims"])

    def test_openai_compatible_extractor_keeps_only_valid_chunk_evidence(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        extractor = OpenAICompatibleExtractor(
            "https://example.test/v1/chat/completions", "test-model", "secret", post=post
        )
        result = extractor.extract(sample_chunks())

        self.assertEqual([0], [item["chunk_index"] for item in result["entities"]])
        self.assertEqual("Bearer secret", calls[0][1]["headers"]["Authorization"])
        self.assertEqual("test-model", calls[0][1]["json"]["model"])

    def test_search_nodes_returns_source_locations(self):
        self.store.index_document("papers", "paper.pdf", sample_chunks(), FakeExtractor())
        results = self.store.search_nodes("CNN-LSTM", "method")

        self.assertEqual("CNN-LSTM", results[0]["label"])
        self.assertEqual([{"knowledge_base": "papers", "source": "paper.pdf"}], results[0]["locations"])

    def test_legacy_knowledge_base_location_is_remapped(self):
        indexed = self.store.index_document("papers", "paper.pdf", sample_chunks(), FakeExtractor())

        moved = self.store.remap_knowledge_bases({"papers": "project-小论文（北松区）"})

        self.assertEqual(1, moved)
        self.assertIsNone(self.store.document_for_location("papers", "paper.pdf"))
        self.assertEqual(
            indexed["document_id"],
            self.store.document_for_location("project-小论文（北松区）", "paper.pdf"),
        )


if __name__ == "__main__":
    unittest.main()
