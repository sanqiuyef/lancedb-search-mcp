# -*- coding: utf-8 -*-
"""kb_ingest：分块、类别推断、文本抽取。"""

import fnmatch
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg  # noqa: E402
from kb import ingest as kb_ingest  # noqa: E402
from kb import schema as kb_schema  # noqa: E402
from tests.kb_test_support import activate_fake_embedding, temp_db_env  # noqa: E402


class TestPreSplit(unittest.TestCase):
    def test_short_paragraphs_untouched(self):
        text = "第一段。\n\n第二段。"
        self.assertEqual(kb_ingest._pre_split_long_paragraphs(text, 800), ["第一段。", "第二段。"])

    def test_long_paragraph_split_at_sentence(self):
        para = "。" * 10  # 10 句，每句 1 字符 + 句号
        para = ("这是一个测试句子。") * 100  # 900 字符无空行
        pieces = kb_ingest._pre_split_long_paragraphs(para, 800)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), 800)

    def test_oversized_sentence_hard_cut(self):
        para = "字" * 2000
        pieces = kb_ingest._pre_split_long_paragraphs(para, 800)
        self.assertGreaterEqual(len(pieces), 3)
        self.assertTrue(all(len(p) <= 800 for p in pieces))


class TestChunking(unittest.TestCase):
    def test_paragraph_chunk_basic_fields(self):
        text = "第一段内容。\n\n第二段内容。"
        chunks = kb_ingest._chunk_by_paragraph(text, "doc.txt", category="note")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["source"], "doc.txt")
        self.assertEqual(chunks[0]["chunk_index"], 0)
        self.assertEqual(chunks[0]["category"], "note")
        self.assertIn("第一段内容", chunks[0]["text"])

    def test_md_heading_chunking(self):
        text = "# 标题\n\n正文。\n\n## 小节一\n\n内容一。\n\n## 小节二\n\n内容二。"
        chunks = kb_ingest.chunk_text(text, "doc.md")
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(any("小节一" in c["text"] for c in chunks))
        self.assertEqual(chunks[0]["source"], "doc.md")

    def test_md_without_headings_falls_back(self):
        text = "只有段落没有标题。\n\n第二段。"
        chunks = kb_ingest.chunk_text(text, "doc.md")
        self.assertEqual(len(chunks), 1)
        self.assertIn("第二段", chunks[0]["text"])

    def test_chunk_indices_sequential(self):
        text = "\n\n".join(f"段落{i}。" + "内容" * 400 for i in range(5))
        chunks = kb_ingest.chunk_text(text, "doc.txt")
        self.assertEqual([c["chunk_index"] for c in chunks], list(range(len(chunks))))


class TestChunkOverlap(unittest.TestCase):
    @staticmethod
    def _long_text():
        """4 段各 300 字符（字符互不相同），足以触发多次分块。"""
        return "\n\n".join(
            "".join(chr(0x4E00 + part * 500 + i) for i in range(300))
            for part in range(4)
        )

    def test_default_overlap_is_not_half_chunk_size(self):
        chunks = kb_ingest._chunk_by_paragraph(self._long_text(), "doc.txt")
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(chunks[1]["text"].startswith(chunks[0]["text"][-100:]))
        self.assertFalse(chunks[1]["text"].startswith(chunks[0]["text"][-400:]))

    def test_overlap_uses_configured_char_count(self):
        with mock.patch.object(cfg, "CHUNK_OVERLAP", 50):
            chunks = kb_ingest._chunk_by_paragraph(self._long_text(), "doc.txt")
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(chunks[1]["text"].startswith(chunks[0]["text"][-50:]))
        self.assertFalse(chunks[1]["text"].startswith(chunks[0]["text"][-51:]))

    def test_zero_overlap_disables_prefix(self):
        with mock.patch.object(cfg, "CHUNK_OVERLAP", 0):
            chunks = kb_ingest._chunk_by_paragraph(self._long_text(), "doc.txt")
        self.assertGreaterEqual(len(chunks), 2)
        self.assertFalse(chunks[1]["text"].startswith(chunks[0]["text"][-10:]))


class IngestDbTestBase(unittest.TestCase):
    """临时库 + 假 embedding，覆盖入库身份/幂等/删除语义。"""

    def setUp(self):
        activate_fake_embedding()
        cfg.FTS_BASE_TOKENIZER = "simple"  # 测试环境不依赖 jieba 词典
        self.db_path = temp_db_env()
        self.tmpdir = tempfile.mkdtemp(prefix="kb_ingest_docs_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def write_doc(self, relpath: str, content: str) -> str:
        path = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path


class TestAddSingleDocumentIdentity(IngestDbTestBase):
    def test_add_same_path_without_scan_dir_refuses_overwrite(self):
        path = self.write_doc("doc.txt", "幂等入库测试内容。" * 20)
        first = kb_ingest.add_single_document(path)
        self.assertNotIn("[ERROR]", first)
        count = kb_schema.row_count()
        self.assertGreater(count, 0)

        second = kb_ingest.add_single_document(path)
        self.assertIn("[ERROR]", second)
        self.assertIn("scan_dir", second)
        self.assertEqual(kb_schema.row_count(), count)

    def test_same_basename_in_two_dirs_refused_without_scan_dir(self):
        first = self.write_doc(os.path.join("dir1", "report.md"),
                               "第一份报告。" * 20)
        second = self.write_doc(os.path.join("dir2", "report.md"),
                                "第二份报告。" * 20)
        self.assertNotIn("[ERROR]", kb_ingest.add_single_document(first))

        result = kb_ingest.add_single_document(second)
        self.assertIn("[ERROR]", result)
        self.assertIn("update_document", result)

        table = kb_schema.get_or_create_table()
        self.assertEqual(kb_schema.existing_sources(table), {"report.md"})
        texts = table.to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("第一份报告" in text for text in texts))
        self.assertFalse(any("第二份报告" in text for text in texts))

    def test_scan_dir_source_stays_stable(self):
        path = self.write_doc(os.path.join("sub", "nested.txt"),
                              "相对路径来源测试。" * 16)
        expected = os.path.relpath(path, self.tmpdir)
        kb_ingest.add_single_document(path, scan_dir=self.tmpdir)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )
        count = kb_schema.row_count()

        # 相对 source 的重复入库保持幂等：内容未变的块复用旧行，不重复写。
        second = kb_ingest.add_single_document(path, scan_dir=self.tmpdir)
        self.assertNotIn("[ERROR]", second)
        self.assertIn("新增记录: 0", second)
        self.assertIn("替换旧记录: 0", second)
        self.assertEqual(kb_schema.row_count(), count)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )


class TestUpdateDocumentSource(IngestDbTestBase):
    def test_update_preserves_original_source(self):
        path = self.write_doc(os.path.join("notes", "alpha.txt"),
                              "更新后的内容。" * 20)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "旧版本文档内容。" * 20,
            "source": "notes/Alpha.TXT",
            "chunk_index": 0,
            "category": "note",
        }])
        self.assertEqual(kb_schema.row_count(), 1)

        result = kb_ingest.update_document(path)
        self.assertNotIn("[ERROR]", result)
        self.assertIn("删除旧记录: 1", result)
        fresh = kb_schema.get_or_create_table()
        self.assertEqual(kb_schema.existing_sources(fresh), {"notes/Alpha.TXT"})
        texts = fresh.to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("更新后的内容" in t for t in texts))
        self.assertFalse(any("旧版本文档内容" in t for t in texts))

    def test_update_unique_suffix_matches_correct_duplicate_basename(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "A 目录旧内容。" * 20, "source": "a/report.md",
             "chunk_index": 0},
            {"text": "B 目录旧内容。" * 20, "source": "b/report.md",
             "chunk_index": 0},
        ])
        path = self.write_doc(os.path.join("b", "report.md"),
                              "B 目录新内容。" * 20)

        result = kb_ingest.update_document(path)
        self.assertNotIn("[ERROR]", result)

        fresh = kb_schema.get_or_create_table()
        self.assertEqual(kb_schema.existing_sources(fresh),
                         {"a/report.md", "b/report.md"})
        arrow = fresh.to_lance().to_table(columns=["source", "text"])
        by_source: dict[str, list[str]] = {}
        for src, text in zip(arrow.column("source").to_pylist(),
                             arrow.column("text").to_pylist()):
            by_source.setdefault(src, []).append(text)
        self.assertTrue(any("B 目录新内容" in t for t in by_source["b/report.md"]))
        self.assertFalse(any("B 目录旧内容" in t for t in by_source["b/report.md"]))
        self.assertTrue(any("A 目录旧内容" in t for t in by_source["a/report.md"]))

    def test_update_ambiguous_basename_refuses_without_touching_rows(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "A 目录旧内容。" * 20, "source": "a/report.md",
             "chunk_index": 0},
            {"text": "B 目录旧内容。" * 20, "source": "b/report.md",
             "chunk_index": 0},
        ])
        before = kb_schema.row_count()
        path = self.write_doc(os.path.join("other", "report.md"),
                              "无法消歧的新内容。" * 20)

        result = kb_ingest.update_document(path)
        self.assertIn("[ERROR]", result)
        self.assertIn("a/report.md", result)
        self.assertIn("b/report.md", result)
        # 候选按排序输出，便于人工确认
        self.assertLess(result.index("a/report.md"), result.index("b/report.md"))
        self.assertEqual(kb_schema.row_count(), before)
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertFalse(any("无法消歧的新内容" in t for t in texts))


class TestWriteThenCleanup(IngestDbTestBase):
    def test_stale_doc_ids_removed_and_all_new_ids_kept(self):
        path = self.write_doc(
            os.path.join("notes", "report.txt"),
            ("甲" * 500) + "\n\n" + ("乙" * 500),
        )
        source = os.path.relpath(path, self.tmpdir)
        text, is_md = kb_ingest.extract_content(path)
        chunks = kb_ingest.chunk_text(text, source, is_md)
        self.assertEqual(len(chunks), 2)
        new_ids = [
            kb_schema.chunk_doc_id(ch["text"], source, ch["chunk_index"])
            for ch in chunks
        ]

        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": chunks[0]["text"], "source": source,
             "chunk_index": 0, "doc_id": new_ids[0]},
            {"text": "旧版本多余内容", "source": source,
             "chunk_index": 99, "doc_id": "stale-only"},
            {"text": "无指纹历史内容", "source": source,
             "chunk_index": 98, "doc_id": ""},
        ])
        self.assertEqual(kb_schema.row_count(), 3)

        result = kb_ingest.add_single_document(path, scan_dir=self.tmpdir)
        self.assertNotIn("[ERROR]", result)
        self.assertIn("替换旧记录: 2", result)

        fresh = kb_schema.get_or_create_table()
        ids = fresh.to_lance().to_table(
            columns=["doc_id"]).column("doc_id").to_pylist()
        self.assertEqual(set(ids), set(new_ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(kb_schema.row_count(), 2)


class TestWriteFailureKeepsOldRows(IngestDbTestBase):
    def test_add_single_failure_keeps_old_rows(self):
        path = self.write_doc(os.path.join("notes", "alpha.txt"),
                              "待写入的新内容。" * 20)
        source = os.path.relpath(path, self.tmpdir)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "必须保留的旧内容。" * 20,
            "source": source,
            "chunk_index": 0,
            "doc_id": "old-alpha",
        }])
        before = kb_schema.row_count()

        with mock.patch.object(kb_ingest, "add_chunks",
                               side_effect=RuntimeError("injected failure")):
            result = kb_ingest.add_single_document(path, scan_dir=self.tmpdir)

        self.assertIn("[ERROR]", result)
        self.assertIn("injected failure", result)
        self.assertEqual(kb_schema.row_count(), before)
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("必须保留的旧内容" in t for t in texts))

    def test_update_failure_keeps_old_rows(self):
        path = self.write_doc(os.path.join("notes", "beta.txt"),
                              "待更新的新内容。" * 20)
        source = os.path.relpath(path, self.tmpdir)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "必须保留的旧内容。" * 20,
            "source": source,
            "chunk_index": 0,
            "doc_id": "old-beta",
        }])
        before = kb_schema.row_count()

        with mock.patch.object(kb_ingest, "add_chunks",
                               side_effect=RuntimeError("injected failure")):
            result = kb_ingest.update_document(path)

        self.assertIn("[ERROR]", result)
        self.assertIn("injected failure", result)
        self.assertEqual(kb_schema.row_count(), before)
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("必须保留的旧内容" in t for t in texts))


class TestDeleteDocumentsMatching(IngestDbTestBase):
    def _seed(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "a", "source": "a.pdf", "chunk_index": 0},
            {"text": "d", "source": "data.pdf", "chunk_index": 0},
            {"text": "n", "source": "docs/a.pdf", "chunk_index": 0},
            {"text": "r", "source": "notes/readme.md", "chunk_index": 0},
        ])

    def test_exact_match_does_not_delete_substrings(self):
        self._seed()
        result = kb_ingest.delete_documents("a.pdf")
        self.assertIn("删除记录数: 1", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"data.pdf", "docs/a.pdf", "notes/readme.md"},
        )

    def test_glob_match_on_distinct_sources(self):
        self._seed()
        result = kb_ingest.delete_documents("*.pdf")
        self.assertIn("匹配到的来源: 3 个", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"notes/readme.md"},
        )

    def test_glob_matching_all_sources_requires_confirm(self):
        self._seed()
        before = kb_schema.row_count()
        result = kb_ingest.delete_documents("*")
        self.assertIn("[ERROR]", result)
        self.assertIn("confirm_all", result)
        self.assertEqual(kb_schema.row_count(), before)

    def test_extension_glob_covering_all_sources_requires_confirm(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "a", "source": "a.md", "chunk_index": 0},
            {"text": "b", "source": "b.md", "chunk_index": 0},
        ])
        before = kb_schema.row_count()
        result = kb_ingest.delete_documents("*.md")
        self.assertIn("[ERROR]", result)
        self.assertEqual(kb_schema.row_count(), before)

    def test_confirmed_glob_clears_whole_knowledge_base(self):
        self._seed()
        result = kb_ingest.delete_documents("*", confirm_all=True)
        self.assertIn("知识库已清空", result)
        self.assertEqual(kb_schema.row_count(), 0)


class TestSourceMatchingCase(IngestDbTestBase):
    def test_exact_delete_uses_normcase(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "p", "source": "paper.pdf", "chunk_index": 0},
            {"text": "q", "source": "docs/paper.pdf", "chunk_index": 0},
            {"text": "n", "source": "notes/readme.md", "chunk_index": 0},
        ])

        result = kb_ingest.delete_documents("Paper.PDF")
        sources_after = kb_schema.existing_sources(
            kb_schema.get_or_create_table())
        if os.path.normcase("Paper.PDF") == os.path.normcase("paper.pdf"):
            self.assertIn("删除记录数: 1", result)
            self.assertEqual(sources_after,
                             {"docs/paper.pdf", "notes/readme.md"})
        else:
            self.assertIn("未找到匹配", result)
            self.assertEqual(sources_after,
                             {"paper.pdf", "docs/paper.pdf", "notes/readme.md"})

    def test_exact_and_glob_case_follow_normcase_and_fnmatch(self):
        sources = {"paper.pdf", "docs/paper.pdf", "paper.txt"}

        exact = kb_ingest._match_sources_for_delete(sources, "Paper.PDF")
        if os.path.normcase("Paper.PDF") == os.path.normcase("paper.pdf"):
            self.assertEqual(exact, {"paper.pdf"})
        else:
            self.assertEqual(exact, set())

        glob = kb_ingest._match_sources_for_delete(sources, "*.PDF")
        self.assertEqual(
            glob,
            {src for src in sources if fnmatch.fnmatch(src, "*.PDF")},
        )


class TestAddChunksBatchDedupe(IngestDbTestBase):
    def test_doc_id_deduped_within_batch_keeping_first(self):
        table = kb_schema.get_or_create_table()
        chunks = [
            {"text": "keep", "source": "s", "chunk_index": 0, "doc_id": "d1"},
            {"text": "drop1", "source": "s", "chunk_index": 1, "doc_id": "d1"},
            {"text": "keep2", "source": "s", "chunk_index": 2, "doc_id": "d2"},
            {"text": "drop2", "source": "s", "chunk_index": 3, "doc_id": "d1"},
        ]
        added = kb_ingest.add_chunks(table, chunks)
        self.assertEqual(added, 2)
        self.assertEqual(kb_schema.row_count(), 2)
        texts = table.to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertEqual(texts, ["keep", "keep2"])


class TestGuessCategory(unittest.TestCase):
    def test_path_mapping(self):
        self.assertEqual(kb_ingest.guess_category(r"D:\work\论文\2024\a.pdf"), "paper")
        self.assertEqual(kb_ingest.guess_category(r"D:\work\notes\b.md"), "note")
        self.assertEqual(kb_ingest.guess_category(r"D:\work\docs\c.txt"), "documentation")

    def test_no_match_empty(self):
        self.assertEqual(kb_ingest.guess_category(r"D:\work\other\d.txt"), "")


class TestExtractText(unittest.TestCase):
    def test_txt(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write("你好，知识库。")
            path = f.name
        try:
            self.assertEqual(kb_ingest.extract_text(path), "你好，知识库。")
        finally:
            os.unlink(path)

    def test_unknown_ext_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".xyz", delete=False) as f:
            path = f.name
        try:
            self.assertEqual(kb_ingest.extract_text(path), "")
        finally:
            os.unlink(path)

    def test_rows_have_all_schema_fields(self):
        rows = kb_ingest._rows_from_chunks(
            [{"text": "t", "source": "s", "chunk_index": 0}])
        self.assertEqual(
            set(rows[0].keys()),
            {"text", "source", "chunk_index", "category", "doc_id", "ingested_at"},
        )


if __name__ == "__main__":
    unittest.main()
