# -*- coding: utf-8 -*-
"""kb_ingest：分块、类别推断、文本抽取。"""

import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg  # noqa: E402
from kb import ingest as kb_ingest  # noqa: E402
from kb import pdf_convert as kb_pdf_convert  # noqa: E402
from kb import schema as kb_schema  # noqa: E402
from kb import vault_source  # noqa: E402
from tests.kb_test_support import activate_fake_embedding, temp_db_env  # noqa: E402
from tests.test_vault_source import (  # noqa: E402
    fake_convert_pdf,
    fake_convert_pdf_with_markdown,
)


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


class TestLegacyBasenameClassification(unittest.TestCase):
    def test_basename_without_separator_is_legacy(self):
        self.assertTrue(kb_ingest.source_is_legacy_basename("report.md"))
        self.assertTrue(kb_ingest.source_is_legacy_basename("论文.pdf"))

    def test_relative_path_with_either_separator_is_not_legacy(self):
        self.assertFalse(kb_ingest.source_is_legacy_basename("notes/report.md"))
        self.assertFalse(kb_ingest.source_is_legacy_basename(r"notes\report.md"))

    def test_empty_source_is_not_legacy(self):
        self.assertFalse(kb_ingest.source_is_legacy_basename(""))
        self.assertFalse(kb_ingest.source_is_legacy_basename(None))


class IngestDbTestBase(unittest.TestCase):
    """临时库 + 假 embedding，覆盖入库身份/幂等/删除语义。"""

    def setUp(self):
        activate_fake_embedding()
        cfg.FTS_BASE_TOKENIZER = "simple"  # 测试环境不依赖 jieba 词典
        self.db_path = temp_db_env(self)
        self.vault = os.path.join(self.db_path, "vault")
        self.tmpdir = tempfile.mkdtemp(prefix="kb_ingest_docs_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def write_doc(self, relpath: str, content: str) -> str:
        path = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def write_vault_doc(self, relpath: str, content: str) -> str:
        path = os.path.join(self.vault, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def write_vault_binary(self, relpath: str, content: bytes = b"%PDF-1.4 fake") -> str:
        path = os.path.join(self.vault, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path


class TestAddSingleDocumentIdentity(IngestDbTestBase):
    def test_add_same_path_is_idempotent_vault_copy(self):
        path = self.write_doc("doc.txt", "幂等入库测试内容。" * 20)
        first = kb_ingest.add_single_document(path)
        self.assertNotIn("[ERROR]", first)
        expected = os.path.normpath(
            "raw-素材/converted-转换/doc-"
            f"{vault_source._path_digest(path)}.md")
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )
        count = kb_schema.row_count()
        self.assertGreater(count, 0)

        second = kb_ingest.add_single_document(path)
        self.assertNotIn("[ERROR]", second)
        self.assertIn("新增记录: 0", second)
        self.assertIn("替换旧记录: 0", second)
        self.assertEqual(kb_schema.row_count(), count)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )

    def test_same_basename_in_two_dirs_get_distinct_vault_copies(self):
        first = self.write_doc(os.path.join("dir1", "report.md"),
                               "第一份报告。" * 20)
        second = self.write_doc(os.path.join("dir2", "report.md"),
                                "第二份报告。" * 20)
        self.assertNotIn("[ERROR]", kb_ingest.add_single_document(first))

        result = kb_ingest.add_single_document(second)
        self.assertNotIn("[ERROR]", result)

        fresh = kb_schema.get_or_create_table()
        sources = kb_schema.existing_sources(fresh)
        self.assertEqual(len(sources), 2)
        first_source = os.path.normpath(
            "raw-素材/converted-转换/report-"
            f"{vault_source._path_digest(first)}.md")
        second_source = os.path.normpath(
            "raw-素材/converted-转换/report-"
            f"{vault_source._path_digest(second)}.md")
        self.assertEqual(sources, {first_source, second_source})

        arrow = fresh.to_lance().to_table(columns=["source", "text"])
        by_source: dict[str, list[str]] = {}
        for src, text in zip(arrow.column("source").to_pylist(),
                             arrow.column("text").to_pylist()):
            by_source.setdefault(src, []).append(text)
        self.assertTrue(any("第一份报告" in text
                            for text in by_source[first_source]))
        self.assertTrue(any("第二份报告" in text
                            for text in by_source[second_source]))

    def test_scan_dir_does_not_change_vault_source(self):
        path = self.write_doc(os.path.join("sub", "nested.txt"),
                              "相对路径来源测试。" * 16)
        expected = os.path.normpath(
            "raw-素材/converted-转换/nested-"
            f"{vault_source._path_digest(path)}.md")
        kb_ingest.add_single_document(path, scan_dir=self.tmpdir)
        sources = kb_schema.existing_sources(kb_schema.get_or_create_table())
        self.assertEqual(sources, {expected})
        self.assertNotIn(os.path.normpath("sub/nested.txt"), sources)
        count = kb_schema.row_count()

        second = kb_ingest.add_single_document(path, scan_dir=self.tmpdir)
        self.assertNotIn("[ERROR]", second)
        self.assertIn("新增记录: 0", second)
        self.assertIn("替换旧记录: 0", second)
        self.assertEqual(kb_schema.row_count(), count)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )

    def test_vault_root_file_is_idempotent_even_if_source_has_no_separator(self):
        path = self.write_vault_doc("rootreport.txt", "vault 根目录内容。" * 20)
        first = kb_ingest.add_single_document(path)
        self.assertNotIn("[ERROR]", first)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"rootreport.txt"},
        )
        count = kb_schema.row_count()

        second = kb_ingest.add_single_document(path)
        self.assertNotIn("[ERROR]", second)
        self.assertIn("新增记录: 0", second)
        self.assertIn("替换旧记录: 0", second)
        self.assertEqual(kb_schema.row_count(), count)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"rootreport.txt"},
        )

    def test_external_source_does_not_touch_legacy_basename_source(self):
        path = self.write_doc(os.path.join("sub", "report.txt"),
                              "子目录新内容。" * 20)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "旧 legacy 内容。" * 20,
            "source": "report.txt",
            "chunk_index": 0,
            "doc_id": "legacy-report",
        }])

        result = kb_ingest.add_single_document(path, scan_dir=self.tmpdir)
        self.assertNotIn("[ERROR]", result)

        fresh = kb_schema.get_or_create_table()
        sources = kb_schema.existing_sources(fresh)
        copied_source = os.path.normpath(
            "raw-素材/converted-转换/report-"
            f"{vault_source._path_digest(path)}.md")
        self.assertIn("report.txt", sources)
        self.assertIn(copied_source, sources)
        arrow = fresh.to_lance().to_table(columns=["source", "text"])
        by_source: dict[str, list[str]] = {}
        for src, text in zip(arrow.column("source").to_pylist(),
                             arrow.column("text").to_pylist()):
            by_source.setdefault(src, []).append(text)
        self.assertTrue(any("旧 legacy 内容" in t for t in by_source["report.txt"]))
        self.assertTrue(any(
            "子目录新内容" in t
            for t in by_source[copied_source]
        ))


class TestUpdateDocumentSource(IngestDbTestBase):
    def test_update_preserves_original_source(self):
        path = self.write_vault_doc(os.path.join("notes", "alpha.txt"),
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
        path = self.write_vault_doc(os.path.join("b", "report.md"),
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
        path = self.write_vault_doc(os.path.join("other", "report.md"),
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


class TestUpdateDocumentExplicitSource(IngestDbTestBase):
    def test_explicit_relative_source_updates_it_and_keeps_legacy(self):
        path = self.write_vault_doc(os.path.join("notes", "note.md"),
                                    "新的相对来源内容。" * 20)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "旧 legacy 内容。" * 20, "source": "note.md",
             "chunk_index": 0, "doc_id": "legacy-note"},
            {"text": "旧相对来源内容。" * 20, "source": "notes/note.md",
             "chunk_index": 0, "doc_id": "relative-note"},
        ])

        result = kb_ingest.update_document(path, source="notes/note.md")

        self.assertNotIn("[ERROR]", result)
        fresh = kb_schema.get_or_create_table()
        self.assertEqual(kb_schema.existing_sources(fresh),
                         {"note.md", "notes/note.md"})
        arrow = fresh.to_lance().to_table(columns=["source", "text"])
        by_source: dict[str, list[str]] = {}
        for src, text in zip(arrow.column("source").to_pylist(),
                             arrow.column("text").to_pylist()):
            by_source.setdefault(src, []).append(text)
        self.assertTrue(any("旧 legacy 内容" in t for t in by_source["note.md"]))
        self.assertTrue(any("新的相对来源内容" in t for t in by_source["notes/note.md"]))

    def test_explicit_legacy_source_is_refused(self):
        path = self.write_vault_doc(os.path.join("notes", "note.md"),
                                    "不应写入的内容。" * 20)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "旧 legacy 内容。" * 20, "source": "note.md",
            "chunk_index": 0, "doc_id": "legacy-note",
        }])
        before = kb_schema.row_count()

        result = kb_ingest.update_document(path, source="note.md")

        self.assertIn("[ERROR]", result)
        self.assertIn("legacy basename source", result)
        self.assertEqual(kb_schema.row_count(), before)

    def test_explicit_source_reuses_equivalent_db_string(self):
        path = self.write_vault_doc(os.path.join("notes", "note.md"),
                                    "分隔符归一后的新内容。" * 20)
        db_source = os.path.normpath("notes/note.md")
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "旧内容必须被替换。" * 20,
            "source": db_source,
            "chunk_index": 0,
            "doc_id": "old-note",
        }])

        result = kb_ingest.update_document(
            path, source="notes/note.md")

        self.assertNotIn("[ERROR]", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {db_source},
        )
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("分隔符归一后的新内容" in t for t in texts))
        self.assertFalse(any("旧内容必须被替换" in t for t in texts))

    def test_explicit_top_level_vault_source_is_allowed(self):
        path = self.write_vault_doc("root-note.md", "顶层新内容。" * 20)
        source = "root-note.md"
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "顶层旧内容。" * 20,
            "source": source,
            "chunk_index": 0,
            "doc_id": "old-root",
        }])

        result = kb_ingest.update_document(path, source=source)

        self.assertNotIn("[ERROR]", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {source},
        )
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("顶层新内容" in t for t in texts))
        self.assertFalse(any("顶层旧内容" in t for t in texts))

    def test_explicit_source_must_be_path_suffix(self):
        path = self.write_vault_doc(os.path.join("notes", "note.md"),
                                    "不应写入的内容。" * 20)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "其他来源旧内容。" * 20, "source": "other/note.md",
            "chunk_index": 0, "doc_id": "other-note",
        }])
        before = kb_schema.row_count()

        result = kb_ingest.update_document(path, source="other/note.md")

        self.assertIn("[ERROR]", result)
        self.assertEqual(kb_schema.row_count(), before)


class TestWriteThenCleanup(IngestDbTestBase):
    def test_stale_doc_ids_removed_and_all_new_ids_kept(self):
        path = self.write_vault_doc(
            os.path.join("notes", "report.txt"),
            ("甲" * 500) + "\n\n" + ("乙" * 500),
        )
        source = "notes/report.txt"
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

        result = kb_ingest.add_single_document(path)
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
        path = self.write_vault_doc(os.path.join("notes", "alpha.txt"),
                                    "待写入的新内容。" * 20)
        source = "notes/alpha.txt"
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
            result = kb_ingest.add_single_document(path)

        self.assertIn("[ERROR]", result)
        self.assertIn("injected failure", result)
        self.assertEqual(kb_schema.row_count(), before)
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("必须保留的旧内容" in t for t in texts))

    def test_update_failure_keeps_old_rows(self):
        path = self.write_vault_doc(os.path.join("notes", "beta.txt"),
                                    "待更新的新内容。" * 20)
        source = "notes/beta.txt"
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

    def test_brackets_are_literal_not_glob(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "a1", "source": "a1.md", "chunk_index": 0},
            {"text": "a[1]", "source": "a[1].md", "chunk_index": 0},
        ])

        result = kb_ingest.delete_documents("a[1].md")

        self.assertIn("删除记录数: 1", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"a1.md"},
        )


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

    def test_bracket_is_not_a_wildcard(self):
        self.assertEqual(
            kb_ingest._match_sources_for_delete({"a1.md"}, "a[1].md"), set())
        self.assertEqual(
            kb_ingest._match_sources_for_delete({"a[1].md"}, "a[1].md"),
            {"a[1].md"},
        )


class TestDeleteSourceExact(IngestDbTestBase):
    def test_can_delete_last_source_without_confirm_all(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "唯一来源", "source": "only.md", "chunk_index": 0,
        }])

        removed = kb_ingest.delete_source_exact("only.md")

        self.assertEqual(removed, 1)
        self.assertEqual(kb_schema.row_count(), 0)

    def test_allow_last_false_refuses_to_empty_library(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "唯一来源", "source": "only.md", "chunk_index": 0,
        }])

        removed = kb_ingest.delete_source_exact("only.md", allow_last=False)

        self.assertEqual(removed, 0)
        self.assertEqual(kb_schema.row_count(), 1)

    def test_exact_delete_ignores_substring_and_other_sources(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "a", "source": "a.pdf", "chunk_index": 0},
            {"text": "d", "source": "docs/a.pdf", "chunk_index": 0},
            {"text": "x", "source": "data.pdf", "chunk_index": 0},
        ])

        removed = kb_ingest.delete_source_exact("a.pdf")

        self.assertEqual(removed, 1)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"docs/a.pdf", "data.pdf"},
        )


class TestResolveWritableSource(IngestDbTestBase):
    def test_no_match_returns_empty_source(self):
        self.assertEqual(
            kb_ingest.resolve_writable_source(
                os.path.join(self.tmpdir, "missing.md")),
            ("", ""),
        )

    def test_legacy_basename_is_reported_for_manual_confirmation(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "legacy", "source": "legacy.md", "chunk_index": 0},
        ])

        source, reason = kb_ingest.resolve_writable_source(
            os.path.join(self.tmpdir, "legacy.md"))

        self.assertEqual(source, "")
        self.assertIn("legacy basename source", reason)
        self.assertIn("需人工确认", reason)

    def test_relative_source_resolved_by_path_suffix(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "relative", "source": "notes/a.md", "chunk_index": 0},
        ])

        self.assertEqual(
            kb_ingest.resolve_writable_source(
                os.path.join(self.tmpdir, "notes", "a.md")),
            ("notes/a.md", ""),
        )

    def test_relative_source_wins_over_same_basename_legacy(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "legacy", "source": "note.md", "chunk_index": 0},
            {"text": "relative", "source": "notes/note.md", "chunk_index": 0},
        ])

        self.assertEqual(
            kb_ingest.resolve_writable_source(
                os.path.join(self.tmpdir, "notes", "note.md")),
            ("notes/note.md", ""),
        )

    def test_unrelated_relative_source_is_not_matched(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "other", "source": "other/report.md", "chunk_index": 0},
        ])

        self.assertEqual(
            kb_ingest.resolve_writable_source(
                os.path.join(self.tmpdir, "notes", "report.md")),
            ("", ""),
        )

    def test_top_level_vault_file_with_basename_source_is_writable(self):
        path = self.write_vault_doc("top.md", "顶层文件内容。" * 20)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "top", "source": "top.md", "chunk_index": 0},
        ])

        self.assertEqual(
            kb_ingest.resolve_writable_source(path),
            ("top.md", ""),
        )

    def test_multiple_path_suffix_matches_are_ambiguous(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [
            {"text": "a", "source": "a/report.md", "chunk_index": 0},
            {"text": "b", "source": "nested/a/report.md", "chunk_index": 0},
        ])

        source, error = kb_ingest.resolve_writable_source(
            os.path.join(self.tmpdir, "nested", "a", "report.md"))

        self.assertEqual(source, "")
        self.assertIn("[ERROR]", error)


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


class TestVaultSourceIngest(IngestDbTestBase):
    def test_add_single_external_pdf_uses_vault_markdown_source(self):
        pdf = self.write_doc("paper.pdf", "%PDF-1.4 fake")
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            result = kb_ingest.add_single_document(pdf)

        self.assertNotIn("[ERROR]", result)
        self.assertIn("原始文件:", result)
        self.assertIn("转换后 md:", result)
        expected = os.path.normpath("raw-素材/converted-转换/paper.md")
        fresh = kb_schema.get_or_create_table()
        self.assertEqual(kb_schema.existing_sources(fresh), {expected})
        self.assertNotIn(".pdf", expected.lower())
        sources = fresh.to_lance().to_table(
            columns=["source"]).column("source").to_pylist()
        self.assertTrue(all(src == expected for src in sources))

    def test_export_markdown_dir_is_extra_copy_only(self):
        pdf = self.write_doc("extra.pdf", "%PDF-1.4 fake")
        export_dir = os.path.join(self.tmpdir, "export")
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            result = kb_ingest.add_single_document(
                pdf, export_markdown_dir=export_dir)

        self.assertNotIn("[ERROR]", result)
        expected = os.path.normpath("raw-素材/converted-转换/extra.md")
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )
        self.assertTrue(os.path.isfile(os.path.join(export_dir, "extra.md")))
        self.assertTrue(os.path.isfile(
            os.path.join(export_dir, "extra_images", "img-001.png")))

    def test_update_document_pdf_goes_through_conversion(self):
        pdf = self.write_doc("update.pdf", "%PDF-1.4 fake")
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            result = kb_ingest.update_document(pdf)

        self.assertNotIn("[ERROR]", result)
        self.assertIn("文档已入库", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {os.path.normpath("raw-素材/converted-转换/update.md")},
        )

    def test_add_documents_rejects_directory_outside_vault(self):
        self.write_doc("outside.md", "库外目录。" * 20)
        result = kb_ingest.add_documents(self.tmpdir)

        self.assertIn("[ERROR]", result)
        self.assertIn("只接受 vault 内目录", result)
        self.assertIn("add_single_document", result)
        self.assertEqual(kb_schema.row_count(), 0)

    def test_add_documents_scans_vault_relative_sources(self):
        self.write_vault_doc(os.path.join("sub", "a.md"),
                             "库内目录内容。" * 40)
        result = kb_ingest.add_documents(os.path.join(self.vault, "sub"))

        self.assertNotIn("[ERROR]", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {os.path.normpath("sub/a.md")},
        )

    def test_add_documents_converts_binary_and_skips_generated_md(self):
        self.write_vault_binary(os.path.join("inbox", "paper.pdf"))
        target = mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf)

        with target as converter:
            first = kb_ingest.add_documents(self.vault)
            self.assertNotIn("[ERROR]", first)
            self.assertEqual(converter.call_count, 1)

            expected = os.path.normpath("raw-素材/converted-转换/paper.md")
            fresh = kb_schema.get_or_create_table()
            self.assertEqual(kb_schema.existing_sources(fresh), {expected})
            count = kb_schema.row_count()
            self.assertGreater(count, 0)

            # reindex_all 会清库，但已有生成的 md 仍在扫描范围内；
            # 本次转换后必须跳过该 md，不能产生重复切片。
            second = kb_ingest.add_documents(self.vault, reindex_all=True)

        self.assertNotIn("[ERROR]", second)
        fresh = kb_schema.get_or_create_table()
        self.assertEqual(kb_schema.existing_sources(fresh), {expected})
        arrow = fresh.to_lance().to_table(columns=["doc_id", "source"])
        ids = arrow.column("doc_id").to_pylist()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), count)
        self.assertTrue(all(src == expected
                            for src in arrow.column("source").to_pylist()))

    def test_reindex_all_outside_vault_root_is_refused(self):
        self.write_vault_doc(os.path.join("sub", "a.md"), "已有内容。" * 30)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "必须保留。" * 30, "source": "keep.md", "chunk_index": 0,
        }])
        before = kb_schema.row_count()

        result = kb_ingest.add_documents(
            os.path.join(self.vault, "sub"), reindex_all=True)

        self.assertIn("[ERROR]", result)
        self.assertIn("只允许扫描 vault 根目录", result)
        self.assertEqual(kb_schema.row_count(), before)

    def test_reindex_all_with_empty_scan_is_refused(self):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "必须保留。" * 30, "source": "keep.md", "chunk_index": 0,
        }])
        before = kb_schema.row_count()

        result = kb_ingest.add_documents(self.vault, reindex_all=True)

        self.assertIn("[ERROR]", result)
        self.assertIn("没有任何可入库文件", result)
        self.assertEqual(kb_schema.row_count(), before)

    def test_reindex_all_text_only_replaces_database(self):
        self.write_vault_doc("a.md", "# 标题\n\n新内容。" * 40)
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "旧库内容。" * 30, "source": "keep.md", "chunk_index": 0,
        }])

        result = kb_ingest.add_documents(self.vault, reindex_all=True)

        self.assertNotIn("[ERROR]", result)
        self.assertIn("文档添加完成", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {"a.md"},
        )

    def test_reindex_all_with_unreadable_text_is_refused_before_drop(self):
        self.write_vault_doc("short.md", "太短")
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "必须保留。" * 30, "source": "keep.md", "chunk_index": 0,
        }])
        before = kb_schema.row_count()

        result = kb_ingest.add_documents(self.vault, reindex_all=True)

        self.assertIn("没有成功入库", result)
        self.assertEqual(kb_schema.row_count(), before)

    def test_converted_pdf_replaces_old_pdf_source(self):
        pdf = self.write_vault_binary(os.path.join("inbox", "paper.pdf"))
        old_source = os.path.normpath("inbox/paper.pdf")
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(table, [{
            "text": "旧 PDF 直接切片内容。" * 20,
            "source": old_source,
            "chunk_index": 0,
            "doc_id": "old-pdf",
        }])

        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            result = kb_ingest.add_single_document(pdf)

        self.assertNotIn("[ERROR]", result)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {os.path.normpath("raw-素材/converted-转换/paper.md")},
        )
        self.assertEqual(kb_schema.row_count(), 1)

    def test_add_documents_counts_conversion_failure_and_skips_old_md(self):
        pdf = self.write_vault_binary(os.path.join("inbox", "paper.pdf"))
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            first = kb_ingest.add_documents(self.vault)
        self.assertNotIn("[ERROR]", first)
        expected = os.path.normpath("raw-素材/converted-转换/paper.md")
        before = kb_schema.row_count()
        self.assertGreater(before, 0)

        future = time.time() + 30
        os.utime(pdf, (future, future))
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf",
            side_effect=RuntimeError("mineru down"),
        ):
            second = kb_ingest.add_documents(self.vault, reindex_all=True)

        self.assertIn("没有成功入库", second)
        self.assertIn("转换/写入失败: 1", second)
        self.assertIn("mineru down", second)
        self.assertNotIn("✅", second)
        # 转换全部失败时不清库；旧 paper.md 同一次扫描中也必须跳过，
        # 不能因为它还在磁盘上就再当普通文本写一份。
        self.assertEqual(kb_schema.row_count(), before)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )

    def test_add_documents_reindexes_regenerated_binary(self):
        pdf = self.write_vault_binary(os.path.join("inbox", "paper.pdf"))
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            first = kb_ingest.add_documents(self.vault)
        self.assertNotIn("[ERROR]", first)
        expected = os.path.normpath("raw-素材/converted-转换/paper.md")
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )

        future = time.time() + 30
        os.utime(pdf, (future, future))
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf",
            side_effect=fake_convert_pdf_with_markdown(
                "# paper\n\n重新生成后的正文 " + ("新内容" * 40)),
        ):
            second = kb_ingest.add_documents(self.vault)

        self.assertNotIn("[ERROR]", second)
        self.assertIn("文档添加完成", second)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected},
        )
        texts = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["text"]).column("text").to_pylist()
        self.assertTrue(any("重新生成后的正文" in t for t in texts))
        self.assertFalse(any("正文内容" in t for t in texts))

    def test_converted_category_comes_from_original_path(self):
        pdf = self.write_doc(os.path.join("docs", "manual.pdf"),
                             "%PDF-1.4 fake")
        with mock.patch.object(
            kb_pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            result = kb_ingest.add_single_document(pdf)

        self.assertNotIn("[ERROR]", result)
        categories = kb_schema.get_or_create_table().to_lance().to_table(
            columns=["category"]).column("category").to_pylist()
        self.assertEqual(set(categories), {"documentation"})

    def test_scan_files_skips_symlink_files_and_dirs(self):
        outside = tempfile.mkdtemp(prefix="kb_link_target_")
        self.addCleanup(shutil.rmtree, outside, True)
        outside_file = os.path.join(outside, "outside.md")
        with open(outside_file, "w", encoding="utf-8") as handle:
            handle.write("外部内容。" * 20)
        file_link = os.path.join(self.vault, "linked.md")
        dir_link = os.path.join(self.vault, "linked-dir")
        file_link_created = True
        try:
            os.symlink(outside_file, file_link)
        except (OSError, NotImplementedError):
            file_link_created = False
        dir_link_created = True
        try:
            os.symlink(outside, dir_link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # Windows 无开发者模式时退回 junction（无需管理员权限）。
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", dir_link, outside],
                capture_output=True,
            )
            dir_link_created = result.returncode == 0
        if not file_link_created and not dir_link_created:
            self.skipTest("当前环境不允许创建符号链接或 junction")

        found = kb_ingest.scan_files(self.vault)

        if file_link_created:
            self.assertNotIn(file_link, found)
        if dir_link_created:
            self.assertFalse(any(path.startswith(dir_link) for path in found))


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

    def test_md_frontmatter_is_stripped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as f:
            f.write("---\ntitle: '笔记'\ntags: [literature]\n---\n\n正文内容。")
            path = f.name
        try:
            self.assertEqual(kb_ingest.extract_text(path), "正文内容。")
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
