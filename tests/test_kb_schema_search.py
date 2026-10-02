# -*- coding: utf-8 -*-
"""kb_schema + kb_search：临时库端到端（LanceModel 自动向量化、FTS、hybrid、过滤、reranker 回退）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.kb_test_support import activate_fake_embedding, temp_db_env  # noqa: E402

activate_fake_embedding()

import kb_ingest  # noqa: E402
import kb_schema  # noqa: E402
import kb_search  # noqa: E402
import kb_config as cfg  # noqa: E402


SAMPLE_DOCS = [
    ("BIMbase 是国产 BIM 图形平台，支持 IFC 标准与 LOD 分级。", "doc/bim.md", 0, "documentation", "BIMbase"),
    ("IFC 文件解析需要处理几何实体与属性映射。", "doc/ifc.md", 0, "documentation", "BIMbase"),
    ("城市洪涝模拟采用 SWMM 模型与神经网络代理模型耦合。", "doc/flood.md", 0, "paper", "小论文（北松区）"),
]


class KBEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = temp_db_env()
        cfg.FTS_BASE_TOKENIZER = "simple"  # 测试环境不依赖 jieba 词典
        cls.table = kb_schema.get_or_create_table()
        for text, source, idx, cat, proj in SAMPLE_DOCS:
            kb_ingest.add_chunks(
                cls.table,
                [{"text": text, "source": source, "chunk_index": idx,
                  "category": cat, "project": proj}],
                proj,
            )
        cls.table = kb_schema.get_or_create_table()  # 取新表对象再建索引
        kb_schema.ensure_fts_index(cls.table)

    def test_schema_fields(self):
        names = {f.name for f in self.table.schema}
        self.assertLessEqual(
            {"text", "vector", "source", "chunk_index", "category", "project",
             "doc_id", "ingested_at"}, names,
        )

    def test_fts_search(self):
        r = kb_search.search_structured("IFC 解析", search_mode="fts", limit=3)
        sources = [x["source"] for x in r["results"]]
        self.assertIn("doc/ifc.md", sources)
        self.assertEqual(r["trace"]["mode"], "text")  # fts 为 text 的兼容别名

    def test_vector_search_with_project_filter(self):
        r = kb_search.search_structured("平台", search_mode="vector",
                                        limit=5, project="BIMbase")
        for item in r["results"]:
            self.assertEqual(item["project"], "BIMbase")

    def test_hybrid_search_with_rrf(self):
        r = kb_search.search_structured("BIMbase 平台", search_mode="hybrid",
                                        limit=3, use_reranker=False)
        self.assertEqual(r["trace"]["mode"], "hybrid")
        self.assertIn("RRF", r["trace"]["reranker"])
        self.assertTrue(r["results"])
        self.assertIn("relevance_score", r["results"][0])

    def test_source_and_category_filter(self):
        r = kb_search.search_structured("模型", search_mode="vector", limit=5,
                                        category_filter="paper")
        self.assertTrue(all(x["category"] == "paper" for x in r["results"]))
        r = kb_search.search_structured("模型", search_mode="vector", limit=5,
                                        source_filter="flood")
        self.assertTrue(all("flood" in x["source"] for x in r["results"]))

    def test_siliconflow_reranker_falls_back_on_api_error(self):
        class Boom:
            def post(self, *a, **k):
                raise ConnectionError("api down")

        import kb_search as ks

        original = ks.requests
        try:
            class FakeRequests:
                post = staticmethod(lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))

            ks.requests = FakeRequests
            reranker = ks.SiliconFlowReranker()
            vector_results = self.table.search("BIMbase").with_row_id(True).limit(2).to_arrow()
            fts_results = self.table.search("BIMbase", query_type="fts").with_row_id(True).limit(2).to_arrow()
            merged = reranker.rerank_hybrid("BIMbase", vector_results, fts_results)
            self.assertGreater(merged.num_rows, 0)
        finally:
            ks.requests = original

    def test_empty_db_raises(self):
        import tempfile

        os.environ["LANCEDB_DB_PATH"] = tempfile.mkdtemp(prefix="kb_empty_")
        kb_schema.reset_db()
        with self.assertRaises(ValueError):
            kb_search.search_structured("anything")
        # 恢复主测试库
        os.environ["LANCEDB_DB_PATH"] = self.db_path
        kb_schema.reset_db()

    def test_delete_and_row_count(self):
        kb_ingest.delete_documents("flood.md", project="小论文（北松区）")
        self.assertEqual(kb_schema.row_count(), 2)
        fresh = kb_schema.get_or_create_table()  # 重开表避免旧对象缓存
        sources = kb_schema.existing_sources(fresh, "小论文（北松区）")
        self.assertNotIn("doc/flood.md", sources)


if __name__ == "__main__":
    unittest.main()
