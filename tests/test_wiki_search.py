# -*- coding: utf-8 -*-
"""Wiki 检索：页面 hybrid、过滤、证据直通、统一入口与图谱邻域。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.kb_test_support import activate_fake_embedding  # noqa: E402

activate_fake_embedding()

from kb import config as kb_config  # noqa: E402
from kb.wiki import page_index  # noqa: E402
from kb.wiki import search as wiki_search  # noqa: E402
from kb.wiki.config import WikiConfig  # noqa: E402
from kb.wiki.db import connect, run_migrations  # noqa: E402
from kb.wiki.models import EntityRecord, PageEntityRecord  # noqa: E402
from kb.wiki.page_store import PageDraft, PageStore  # noqa: E402
from kb.wiki.repositories import entities as entities_repo  # noqa: E402
from kb.wiki.repositories import relations as relations_repo  # noqa: E402

NOW = "2026-01-01T00:00:00.000Z"


class WikiSearchTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_tokenizer = kb_config.FTS_BASE_TOKENIZER
        kb_config.FTS_BASE_TOKENIZER = "simple"
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
        self.empty_lance_db = os.path.join(self.root, "empty-lancedb")
        page_index.reset_db()

    def tearDown(self):
        page_index.reset_db()

    # ── helpers ──
    def make_indexed_page(
        self,
        slug,
        title,
        page_type,
        body,
        tags=(),
        aliases=(),
        summary="",
    ):
        page = self.store.create_page(
            PageDraft(
                title=title,
                page_type=page_type,
                summary=summary or f"{title} summary",
                body=body,
                slug=slug,
                tags=list(tags),
                aliases=list(aliases),
                status="published",
            )
        )
        page_index.index_page(
            page,
            body,
            list(aliases),
            list(tags),
            self.config,
            summary=summary or f"{title} summary",
            db_path=self.lance_db,
        )
        return page

    def make_corpus(self):
        urban = self.make_indexed_page(
            "urban-flood",
            "Urban Flood",
            "concept",
            "urban flood simulation with SWMM model and rainfall runoff",
            tags=["flood", "urban"],
            aliases=["内涝"],
        )
        ifc = self.make_indexed_page(
            "ifc-parsing",
            "IFC Parsing",
            "entity",
            "IFC geometry parsing and attribute mapping for BIM models",
            tags=["bim"],
        )
        risk = self.make_indexed_page(
            "flood-risk",
            "Flood Risk",
            "topic",
            "flood risk assessment framework for city planning",
            tags=["flood"],
        )
        return urban, ifc, risk

    def search(self, query, **kwargs):
        kwargs.setdefault("config", self.config)
        kwargs.setdefault("db_path", self.lance_db)
        return wiki_search.search_pages(query, **kwargs)


class TestSearchPages(WikiSearchTestBase):
    def test_hybrid_returns_relevant_page_with_score_source(self):
        _urban, ifc, _risk = self.make_corpus()
        results = self.search("IFC geometry", limit=5, use_reranker=False)
        self.assertTrue(results)
        self.assertIn(ifc.page_id, [item["page_id"] for item in results])
        for item in results:
            self.assertEqual(
                set(item),
                {
                    "page_id",
                    "slug",
                    "title",
                    "page_type",
                    "revision_id",
                    "snippet",
                    "score",
                    "score_source",
                },
            )
            self.assertEqual(item["score_source"], "rrf")
            self.assertIsInstance(item["score"], float)

    def test_page_type_filter(self):
        self.make_corpus()
        results = self.search("IFC geometry", page_type="entity", use_reranker=False)
        self.assertTrue(results)
        self.assertTrue(all(item["page_type"] == "entity" for item in results))

    def test_tags_filter_string_and_list(self):
        urban, _ifc, risk = self.make_corpus()
        results = self.search("flood", tags="flood", use_reranker=False)
        ids = {item["page_id"] for item in results}
        self.assertEqual(ids, {urban.page_id, risk.page_id})

        results = self.search("flood", tags=["bim"], use_reranker=False)
        self.assertEqual(
            [item["slug"] for item in results],
            ["ifc-parsing"],
        )

    def test_tags_filter_escapes_like_wildcards(self):
        exact = self.make_indexed_page(
            "wildcard-exact",
            "Wildcard Exact",
            "concept",
            "wildcard token page",
            tags=["city_flood"],
        )
        self.make_indexed_page(
            "wildcard-prefix",
            "Wildcard Prefix",
            "concept",
            "wildcard token page",
            tags=["cityXflood"],
        )
        results = self.search(
            "wildcard", tags="city_flood", use_reranker=False
        )
        self.assertEqual([item["page_id"] for item in results], [exact.page_id])

    def test_snippet_centers_on_query(self):
        body = ("filler " * 80) + "needle keyword " + ("tail " * 80)
        page = self.make_indexed_page(
            "snippet-page", "Snippet", "concept", body
        )
        results = self.search("needle", use_reranker=False)
        self.assertEqual(results[0]["page_id"], page.page_id)
        self.assertIn("needle keyword", results[0]["snippet"])
        self.assertTrue(results[0]["snippet"].startswith("…"))

    def test_empty_table_returns_empty_list(self):
        self.assertEqual(
            wiki_search.search_pages(
                "anything",
                config=self.config,
                db_path=self.empty_lance_db,
            ),
            [],
        )

    def test_missing_fts_index_degrades_to_vector(self):
        self.make_corpus()
        table = page_index.get_table(
            self.config, self.lance_db, create=False
        )
        table.drop_index(page_index.PAGE_TEXT_INDEX)

        result = wiki_search.search_wiki(
            "IFC geometry",
            mode="page",
            use_reranker=False,
            config=self.config,
            db_path=self.lance_db,
        )
        self.assertTrue(result["pages"])
        self.assertTrue(
            any("回退向量检索" in warning for warning in result["warnings"])
        )
        self.assertTrue(
            all(
                item["score_source"] == "vector_similarity"
                for item in result["pages"]
            )
        )

    def test_reranker_cross_encoder_score_source(self):
        self.make_corpus()

        class FakeCrossEncoder:
            def predict(self, pairs, show_progress_bar=False):
                return [1.0 - 0.1 * index for index in range(len(pairs))]

        with mock.patch(
            "kb.search._get_cross_encoder", return_value=FakeCrossEncoder()
        ):
            results = self.search("IFC geometry", use_reranker=True)
        self.assertTrue(results)
        self.assertTrue(
            all(item["score_source"] == "cross_encoder" for item in results)
        )

    def test_reranker_model_failure_falls_back_to_rrf(self):
        self.make_corpus()
        with mock.patch(
            "kb.search._get_cross_encoder",
            side_effect=RuntimeError("model load failed"),
        ):
            results = self.search("IFC geometry", use_reranker=True)
        self.assertTrue(results)
        self.assertTrue(all(item["score_source"] == "rrf" for item in results))


class TestSearchEvidence(WikiSearchTestBase):
    def test_evidence_returns_raw_result_with_rag_source(self):
        payload = {
            "results": [
                {
                    "text": "evidence text",
                    "source": "doc/a.md",
                    "chunk_index": 2,
                    "category": "paper",
                    "relevance_score": 0.75,
                }
            ],
            "trace": {"mode": "hybrid", "result_count": 1},
        }
        with mock.patch(
            "kb.wiki.search.kb_search.search_structured", return_value=payload
        ) as search_structured:
            result = wiki_search.search_evidence(
                "query",
                limit=3,
                source_filter="doc",
                category_filter="paper",
                search_mode="hybrid",
            )
        search_structured.assert_called_once_with(
            "query",
            limit=3,
            use_reranker=True,
            source_filter="doc",
            category_filter="paper",
            search_mode="hybrid",
        )
        self.assertEqual(result["results"], payload["results"])
        self.assertEqual(result["trace"], payload["trace"])
        self.assertEqual(result["score_source"], "rag")

    def test_empty_knowledge_base_returns_warning(self):
        with mock.patch(
            "kb.wiki.search.kb_search.search_structured",
            side_effect=ValueError("知识库为空，请先添加文档。"),
        ):
            result = wiki_search.search_evidence("query")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["score_source"], "rag")
        self.assertTrue(result["warnings"])

    def test_empty_query_does_not_call_kb_search(self):
        with mock.patch(
            "kb.wiki.search.kb_search.search_structured"
        ) as search_structured:
            result = wiki_search.search_evidence("   ")
        search_structured.assert_not_called()
        self.assertEqual(result["results"], [])
        self.assertEqual(result["score_source"], "rag")


class TestSearchWiki(WikiSearchTestBase):
    def test_page_mode_does_not_call_evidence(self):
        self.make_corpus()
        with mock.patch.object(
            wiki_search,
            "search_evidence",
            side_effect=AssertionError("page 模式不应检索证据"),
        ):
            result = wiki_search.search_wiki(
                "IFC",
                mode="page",
                use_reranker=False,
                config=self.config,
                db_path=self.lance_db,
            )
        self.assertTrue(result["pages"])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["related"], [])
        self.assertTrue(
            all(item["source"] == "wiki_page" for item in result["pages"])
        )

    def test_evidence_mode_does_not_search_pages(self):
        payload = {
            "results": [{"text": "ev", "source": "s", "chunk_index": 0}],
            "trace": {"mode": "hybrid"},
            "score_source": "rag",
        }
        with mock.patch.object(
            wiki_search, "search_evidence", return_value=payload
        ), mock.patch.object(
            wiki_search,
            "_search_pages_impl",
            side_effect=AssertionError("evidence 模式不应检索页面"),
        ):
            result = wiki_search.search_wiki("query", mode="evidence")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["related"], [])
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["source"], "evidence")
        self.assertEqual(result["evidence"][0]["score_source"], "rag")

    def test_hybrid_mode_returns_both_with_sources(self):
        self.make_corpus()
        evidence_payload = {
            "results": [
                {
                    "text": "evidence text",
                    "source": "doc/a.md",
                    "chunk_index": 0,
                    "relevance_score": 0.5,
                }
            ],
            "trace": {"mode": "hybrid"},
            "score_source": "rag",
        }
        with mock.patch.object(
            wiki_search, "search_evidence", return_value=evidence_payload
        ):
            result = wiki_search.search_wiki(
                "IFC geometry",
                mode="hybrid",
                use_reranker=False,
                config=self.config,
                db_path=self.lance_db,
            )
        self.assertTrue(result["pages"])
        self.assertTrue(result["evidence"])
        self.assertTrue(
            all(item["source"] == "wiki_page" for item in result["pages"])
        )
        self.assertTrue(
            all(item["source"] == "evidence" for item in result["evidence"])
        )
        self.assertEqual(result["related"], [])
        self.assertEqual(set(result), {"query", "mode", "pages", "evidence", "related", "warnings"})

    def test_empty_index_returns_empty_with_warning(self):
        result = wiki_search.search_wiki(
            "anything",
            mode="page",
            config=self.config,
            db_path=self.empty_lance_db,
        )
        self.assertEqual(result["pages"], [])
        self.assertTrue(
            any("页面索引表不存在" in warning for warning in result["warnings"])
        )

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            wiki_search.search_wiki("query", mode="unknown")

    def test_empty_query_returns_empty(self):
        result = wiki_search.search_wiki("   ", mode="page")
        self.assertEqual(result["pages"], [])
        self.assertTrue(result["warnings"])


class TestRelatedMode(WikiSearchTestBase):
    def _make_graph(self):
        target = self.make_indexed_page(
            "page-b",
            "IFC Parsing",
            "entity",
            "IFC geometry parsing and attribute mapping",
            tags=["bim"],
        )
        seed = self.make_indexed_page(
            "page-a",
            "Urban Flood",
            "concept",
            "See [[page-b]] for details.",
            tags=["flood"],
        )
        first_entity = EntityRecord(
            canonical_name="SWMM",
            normalized_name="swmm",
            entity_type="model",
            status="accepted",
            created_at=NOW,
            updated_at=NOW,
        )
        second_entity = EntityRecord(
            canonical_name="IFC",
            normalized_name="ifc",
            entity_type="format",
            status="accepted",
            created_at=NOW,
            updated_at=NOW,
        )
        entities_repo.insert_entity(self.conn, first_entity)
        entities_repo.insert_entity(self.conn, second_entity)
        entities_repo.add_page_entity(
            self.conn,
            PageEntityRecord(
                page_id=seed.page_id,
                entity_id=first_entity.entity_id,
                role="mention",
                confidence=0.9,
                created_at=NOW,
            ),
        )
        entities_repo.add_page_entity(
            self.conn,
            PageEntityRecord(
                page_id=target.page_id,
                entity_id=second_entity.entity_id,
                role="mention",
                confidence=0.9,
                created_at=NOW,
            ),
        )
        relations_repo.create_relation(
            self.conn,
            first_entity.entity_id,
            "uses",
            second_entity.entity_id,
            confidence=0.9,
            source_page_id=seed.page_id,
            status="accepted",
        )
        return seed, target

    def test_related_uses_links_and_relations_without_vector(self):
        seed, target = self._make_graph()
        with mock.patch.object(
            wiki_search,
            "_search_pages_impl",
            side_effect=AssertionError("related 模式不应走向量检索"),
        ):
            result = wiki_search.search_wiki(
                seed.slug,
                mode="related",
                limit=10,
                conn=self.conn,
                config=self.config,
                db_path=self.lance_db,
            )
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["evidence"], [])
        related_pages = [
            item for item in result["related"] if item["source"] == "wiki_page"
        ]
        self.assertIn(target.page_id, [item["page_id"] for item in related_pages])
        self.assertTrue(
            any(
                item["kind"] in ("outgoing_link", "shared_entity")
                and item["score_source"] == "graph"
                for item in related_pages
            )
        )
        entity_items = [
            item for item in result["related"] if item["source"] == "entity"
        ]
        self.assertTrue(
            any(
                item["canonical_name"] == "IFC"
                and item["predicate"] == "uses"
                and item["depth"] == 1
                and item["score_source"] == "graph"
                for item in entity_items
            )
        )

    def test_related_resolves_link_slug_created_later(self):
        seed = self.make_indexed_page(
            "forward-seed",
            "Forward Seed",
            "concept",
            "See [[later-page]] for details.",
        )
        target = self.make_indexed_page(
            "later-page",
            "Later Page",
            "topic",
            "later page body",
        )
        link_row = self.conn.execute(
            "SELECT target_page_id FROM page_links WHERE source_page_id = ?",
            (seed.page_id,),
        ).fetchone()
        self.assertIsNone(link_row["target_page_id"])

        result = wiki_search.search_wiki(
            seed.slug,
            mode="related",
            conn=self.conn,
            config=self.config,
            db_path=self.lance_db,
        )
        self.assertIn(
            target.page_id,
            [
                item["page_id"]
                for item in result["related"]
                if item["source"] == "wiki_page"
            ],
        )

    def test_related_missing_page_returns_warning(self):
        result = wiki_search.search_wiki(
            "no-such-page",
            mode="related",
            conn=self.conn,
            config=self.config,
            db_path=self.lance_db,
        )
        self.assertEqual(result["related"], [])
        self.assertTrue(any("未找到页面" in warning for warning in result["warnings"]))

    def test_related_without_conn_returns_warning(self):
        result = wiki_search.search_wiki(
            "page-a",
            mode="related",
            config=self.config,
            db_path=self.lance_db,
        )
        self.assertEqual(result["related"], [])
        self.assertTrue(any("conn" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
