# -*- coding: utf-8 -*-
"""kb_ingest：分块、类别推断、文本抽取。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import ingest as kb_ingest


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
