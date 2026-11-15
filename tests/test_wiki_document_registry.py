# -*- coding: utf-8 -*-
"""DocumentRegistry：source 身份、修订幂等、切片引用与旧源迁移登记。"""

import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import document_registry as registry
from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations, transaction
from kb.wiki.ids import chunk_id, document_id, normalize_text_sha256
from kb.wiki.repositories import documents as doc_repo


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = WikiConfig(
            enabled=True, vault_path=os.path.join(self._tmp.name, "vault")
        )
        self.db_path = os.path.join(self._tmp.name, "wiki.sqlite3")
        self.conn = connect(self.db_path, self.config)
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)

    def source(self, uri="docs/a.md", **kwargs):
        return registry.SourceDescriptor(source_type="file", canonical_uri=uri, **kwargs)

    def revision(self, **overrides):
        data = {
            "raw_sha256": "raw-1",
            "parsed_sha256": "parsed-1",
            "parser_name": "mineru",
            "parser_version": "1.0",
            "parser_config_sha256": "cfg-1",
            "source_path": "D:/docs/a.pdf",
        }
        data.update(overrides)
        return registry.RevisionInput(**data)

    def make_document(self, uri="docs/a.md"):
        return registry.register_source(self.conn, self.source(uri))

    def make_revision(self, document_id_value=None):
        document_id_value = document_id_value or self.make_document()
        return registry.begin_revision(
            self.conn, document_id_value, self.revision()
        )


class TestCanonicalSource(unittest.TestCase):
    def test_local_source_normalizes_slashes_and_whitespace(self):
        self.assertEqual(
            registry.canonical_local_source("  C:\\Docs\\A.MD  "),
            "C:/Docs/A.MD",
        )
        self.assertEqual(registry.canonical_local_source("a\\\\b"), "a/b")
        self.assertEqual(
            registry.canonical_local_source("\\\\server\\share\\file.md"),
            "//server/share/file.md",
        )
        self.assertEqual(registry.canonical_local_source("   "), "")

    def test_local_source_preserves_case(self):
        self.assertEqual(
            registry.canonical_local_source("Docs/Paper.MD"), "Docs/Paper.MD"
        )
        self.assertNotEqual(
            registry.canonical_local_source("Docs/Paper.MD"),
            registry.canonical_local_source("docs/paper.md"),
        )

    def test_local_source_rejects_non_string(self):
        with self.assertRaises(TypeError):
            registry.canonical_local_source(None)

    def test_web_source_reuses_canonicalize_url(self):
        self.assertEqual(
            registry.canonical_web_source(
                "HTTPS://Example.COM:443/Path?utm_source=x&a=1#frag"
            ),
            "https://example.com/Path?a=1",
        )

    def test_invalid_web_source_rejected(self):
        with self.assertRaises(ValueError):
            registry.canonical_web_source("ftp://example.com/a")

    def test_infer_source_type(self):
        self.assertEqual(registry.infer_source_type("https://a/b"), "web")
        self.assertEqual(registry.infer_source_type("网页-example-abc"), "web")
        self.assertEqual(registry.infer_source_type("docs/a.md"), "file")


class TestRegisterSource(RegistryTestBase):
    def test_get_or_create_uses_deterministic_document_id(self):
        uri = "docs/a.md"
        first = registry.register_source(self.conn, self.source(uri))
        second = registry.register_source(self.conn, self.source(uri))
        self.assertEqual(first, second)
        self.assertEqual(first, document_id("file", uri))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1
        )

    def test_existing_source_only_updates_non_empty_metadata(self):
        first = registry.register_source(
            self.conn,
            self.source(display_name="A", category="note"),
        )
        before = doc_repo.get_document(self.conn, first)
        registry.register_source(
            self.conn,
            self.source(display_name="", category="paper"),
        )
        after = doc_repo.get_document(self.conn, first)
        self.assertEqual(after["display_name"], "A")
        self.assertEqual(after["category"], "paper")
        self.assertEqual(after["created_at"], before["created_at"])

    def test_different_source_type_is_different_document(self):
        file_id = registry.register_source(self.conn, self.source("a.md"))
        web_id = registry.register_source(
            self.conn,
            registry.SourceDescriptor(source_type="web", canonical_uri="a.md"),
        )
        self.assertNotEqual(file_id, web_id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2
        )

    def test_blank_descriptor_rejected(self):
        with self.assertRaises(ValueError):
            registry.SourceDescriptor(source_type="", canonical_uri="a.md")
        with self.assertRaises(ValueError):
            registry.SourceDescriptor(source_type="file", canonical_uri=" ")


class TestBeginRevision(RegistryTestBase):
    def test_first_revision_is_processing(self):
        doc_id = self.make_document()
        handle = registry.begin_revision(self.conn, doc_id, self.revision())
        self.assertTrue(handle.created)
        self.assertIsInstance(handle, str)
        self.assertEqual(handle, handle.revision_id)
        self.assertEqual(handle.revision_no, 1)
        self.assertEqual(handle.status, "processing")
        self.assertEqual(
            handle.processing_fingerprint,
            registry.processing_fingerprint(
                "raw-1", "parsed-1", "mineru", "1.0", "cfg-1"
            ),
        )
        self.assertEqual(
            handle.processing_fingerprint,
            hashlib.sha256(
                ("raw-1" + "parsed-1" + "mineru" + "1.0" + "cfg-1").encode("utf-8")
            ).hexdigest(),
        )
        row = doc_repo.get_revision(self.conn, handle.revision_id)
        self.assertEqual(row["document_id"], doc_id)
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["chunk_count"], 0)

    def test_same_fingerprint_is_idempotent(self):
        doc_id = self.make_document()
        first = registry.begin_revision(self.conn, doc_id, self.revision())
        second = registry.begin_revision(self.conn, doc_id, self.revision())
        self.assertFalse(second.created)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertEqual(first.revision_no, second.revision_no)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM document_revisions"
            ).fetchone()[0],
            1,
        )

    def test_raw_parser_or_config_change_creates_new_revision(self):
        doc_id = self.make_document()
        first = registry.begin_revision(self.conn, doc_id, self.revision())
        raw_changed = registry.begin_revision(
            self.conn, doc_id, self.revision(raw_sha256="raw-2")
        )
        parsed_changed = registry.begin_revision(
            self.conn, doc_id, self.revision(parsed_sha256="parsed-2")
        )
        parser_changed = registry.begin_revision(
            self.conn, doc_id, self.revision(parser_version="2.0")
        )
        config_changed = registry.begin_revision(
            self.conn, doc_id, self.revision(parser_config_sha256="cfg-2")
        )
        self.assertEqual(
            [first.revision_no, raw_changed.revision_no,
             parsed_changed.revision_no, parser_changed.revision_no,
             config_changed.revision_no],
            [1, 2, 3, 4, 5],
        )
        self.assertTrue(all(
            handle.created
            for handle in (raw_changed, parsed_changed, parser_changed, config_changed)
        ))

    def test_unknown_document_rejected(self):
        with self.assertRaises(ValueError):
            registry.begin_revision(self.conn, "missing-doc", self.revision())


class TestRevisionLifecycle(RegistryTestBase):
    def test_complete_sets_status_count_and_current_revision(self):
        doc_id = self.make_document()
        handle = registry.begin_revision(self.conn, doc_id, self.revision())
        registry.complete_revision(
            self.conn,
            handle,  # RevisionHandle 本身是 revision_id 字符串
            chunk_count=3,
            parsed_sha256="parsed-final",
            parsed_markdown_path="D:/cache/a.md",
        )
        revision = doc_repo.get_revision(self.conn, handle.revision_id)
        self.assertEqual(revision["status"], "indexed")
        self.assertEqual(revision["chunk_count"], 3)
        self.assertEqual(revision["parsed_sha256"], "parsed-final")
        self.assertEqual(revision["parsed_markdown_path"], "D:/cache/a.md")
        document = doc_repo.get_document(self.conn, doc_id)
        self.assertEqual(document["current_revision_id"], handle.revision_id)

    def test_complete_without_optional_paths_keeps_existing_values(self):
        doc_id = self.make_document()
        handle = registry.begin_revision(
            self.conn,
            doc_id,
            self.revision(parsed_markdown_path="D:/cache/original.md"),
        )
        registry.complete_revision(self.conn, handle.revision_id, chunk_count=1)
        revision = doc_repo.get_revision(self.conn, handle.revision_id)
        self.assertEqual(revision["parsed_markdown_path"], "D:/cache/original.md")
        self.assertEqual(revision["status"], "indexed")

    def test_fail_sets_failed_and_error(self):
        doc_id = self.make_document()
        handle = registry.begin_revision(self.conn, doc_id, self.revision())
        registry.fail_revision(self.conn, handle.revision_id, "parse boom")
        revision = doc_repo.get_revision(self.conn, handle.revision_id)
        self.assertEqual(revision["status"], "failed")
        self.assertEqual(revision["error_message"], "parse boom")

    def test_complete_or_fail_unknown_revision_rejected(self):
        with self.assertRaises(ValueError):
            registry.complete_revision(self.conn, "missing", chunk_count=1)
        with self.assertRaises(ValueError):
            registry.fail_revision(self.conn, "missing", "boom")


class TestChunkRefs(RegistryTestBase):
    def setUp(self):
        super().setUp()
        self.doc_id = self.make_document()
        self.revision = self.make_revision(self.doc_id)

    def descriptor(self, index=0, text="chunk text", **kwargs):
        return registry.ChunkDescriptor.from_text(
            index, text, source_value="docs/a.md", **kwargs
        )

    def test_same_index_different_text_and_idempotent_insert(self):
        first = self.descriptor(0, "first text")
        second = self.descriptor(0, "second text")
        inserted = registry.register_chunk_refs(
            self.conn, self.revision.revision_id, [first, second]
        )
        self.assertEqual(inserted, 2)
        repeated = registry.register_chunk_refs(
            self.conn, self.revision.revision_id, [first, second]
        )
        self.assertEqual(repeated, 0)
        rows = self.conn.execute(
            "SELECT chunk_id, chunk_index, text_sha256 FROM chunk_refs "
            "ORDER BY text_sha256"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["chunk_id"] for row in rows},
            {
                chunk_id(self.revision.revision_id, 0, first.text_sha256),
                chunk_id(self.revision.revision_id, 0, second.text_sha256),
            },
        )

    def test_descriptor_fields_are_preserved(self):
        descriptor = registry.ChunkDescriptor.from_text(
            2,
            "定位文本",
            legacy_doc_id="legacy-id",
            source_value="legacy\\source.md",
            section_path="第一章/背景",
            page_number=7,
            char_start=10,
            char_end=20,
            locator_json='{"bbox": [1, 2]}',
        )
        registry.register_chunk_refs(
            self.conn, self.revision.revision_id, [descriptor]
        )
        row = self.conn.execute("SELECT * FROM chunk_refs").fetchone()
        self.assertEqual(row["legacy_doc_id"], "legacy-id")
        self.assertEqual(row["source_value"], "legacy\\source.md")
        self.assertEqual(row["section_path"], "第一章/背景")
        self.assertEqual(row["page_number"], 7)
        self.assertEqual(row["char_start"], 10)
        self.assertEqual(row["char_end"], 20)
        self.assertEqual(row["locator_json"], '{"bbox": [1, 2]}')
        self.assertEqual(
            row["text_sha256"], normalize_text_sha256("定位文本")
        )

    def test_invalid_descriptor_rejected(self):
        with self.assertRaises(ValueError):
            registry.ChunkDescriptor(chunk_index=0, text_sha256="")
        with self.assertRaises(ValueError):
            registry.ChunkDescriptor(
                chunk_index=-1, text_sha256="abc"
            )
        with self.assertRaises(ValueError):
            registry.ChunkDescriptor(
                chunk_index=0,
                text_sha256="abc",
                page_number=0,
            )
        with self.assertRaises(ValueError):
            registry.ChunkDescriptor(
                chunk_index=0, text_sha256="abc", char_start=5, char_end=4
            )

    def test_unknown_revision_rejected(self):
        with self.assertRaises(ValueError):
            registry.register_chunk_refs(
                self.conn, "missing-revision", [self.descriptor()]
            )

    def test_mark_source_missing_keeps_history(self):
        registry.register_chunk_refs(
            self.conn, self.revision.revision_id, [self.descriptor()]
        )
        self.assertTrue(registry.mark_source_missing(self.conn, self.doc_id))
        document = doc_repo.get_document(self.conn, self.doc_id)
        self.assertEqual(document["status"], "missing")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM document_revisions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM chunk_refs").fetchone()[0], 1
        )
        # 重复标记幂等
        self.assertTrue(registry.mark_source_missing(self.conn, self.doc_id))

    def test_mark_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            registry.mark_source_missing(self.conn, "missing-doc")


class TestLegacyRegistration(RegistryTestBase):
    def legacy_rows(self):
        return [
            registry.ChunkDescriptor.from_text(
                0, "旧切片一", legacy_doc_id="doc-a", source_value="legacy\\Paper.md"
            ),
            registry.ChunkDescriptor.from_text(
                0, "旧切片二", legacy_doc_id="doc-b", source_value="legacy\\Paper.md"
            ),
            registry.ChunkDescriptor.from_text(
                1, "旧切片三", legacy_doc_id="doc-c", source_value="legacy\\Paper.md"
            ),
        ]

    def test_creates_legacy_revision_without_fake_hashes(self):
        result = registry.register_legacy_source(
            self.conn, "legacy\\Paper.md", self.legacy_rows(), category="paper"
        )
        self.assertTrue(result.document_created)
        self.assertTrue(result.revision_created)
        self.assertEqual(result.revision_status, "legacy_unverified")
        self.assertEqual(result.chunk_refs_inserted, 3)
        self.assertEqual(result.chunk_ref_count, 3)

        document = doc_repo.get_document(self.conn, result.document_id)
        self.assertEqual(document["canonical_uri"], "legacy/Paper.md")
        self.assertEqual(document["category"], "paper")
        self.assertEqual(document["current_revision_id"], result.revision_id)

        revision = doc_repo.get_revision(self.conn, result.revision_id)
        self.assertEqual(revision["raw_sha256"], "")
        self.assertEqual(revision["parsed_sha256"], "")
        self.assertEqual(revision["parser_name"], "legacy_import")
        self.assertEqual(revision["status"], "legacy_unverified")
        self.assertEqual(revision["revision_no"], 1)
        self.assertEqual(revision["chunk_count"], 3)

        rows = self.conn.execute(
            "SELECT legacy_doc_id, source_value FROM chunk_refs "
            "ORDER BY legacy_doc_id"
        ).fetchall()
        self.assertEqual(
            [(row["legacy_doc_id"], row["source_value"]) for row in rows],
            [
                ("doc-a", "legacy\\Paper.md"),
                ("doc-b", "legacy\\Paper.md"),
                ("doc-c", "legacy\\Paper.md"),
            ],
        )

    def test_repeat_is_idempotent(self):
        first = registry.register_legacy_source(
            self.conn, "legacy\\Paper.md", self.legacy_rows()
        )
        second = registry.register_legacy_source(
            self.conn, "legacy\\Paper.md", self.legacy_rows()
        )
        self.assertEqual(first.document_id, second.document_id)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertFalse(second.document_created)
        self.assertFalse(second.revision_created)
        self.assertEqual(second.chunk_refs_inserted, 0)
        self.assertEqual(second.chunk_ref_count, 3)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM document_revisions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM chunk_refs").fetchone()[0], 3
        )

    def test_web_legacy_source_uses_web_identity(self):
        result = registry.register_legacy_source(
            self.conn,
            "网页-example-abc123",
            [registry.ChunkDescriptor.from_text(0, "网页内容")],
        )
        document = doc_repo.get_document(self.conn, result.document_id)
        self.assertEqual(document["source_type"], "web")
        self.assertEqual(document["canonical_uri"], "网页-example-abc123")


class TestTransactionContract(RegistryTestBase):
    def test_caller_transaction_rolls_back_registry_writes(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.conn):
                registry.register_source(self.conn, self.source("rollback.md"))
                raise RuntimeError("boom")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
        )


class TestIngestHook(RegistryTestBase):
    def _hook_env(self, enabled=True):
        values = {
            "WIKI_ENABLED": "1" if enabled else "0",
            "WIKI_DB_PATH": self.db_path,
            "WIKI_VAULT_PATH": os.path.join(self._tmp.name, "vault"),
        }
        patcher = mock.patch.dict(os.environ, values, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _chunks(self, source="docs/hook.md"):
        return [
            {
                "text": "钩子切片一",
                "source": source,
                "chunk_index": 0,
                "category": "note",
                "doc_id": "hook-doc-1",
            },
            {
                "text": "钩子切片二",
                "source": source,
                "chunk_index": 1,
                "category": "note",
                "doc_id": "hook-doc-2",
            },
        ]

    def test_hook_registers_document_revision_and_refs(self):
        from kb import ingest as kb_ingest

        self._hook_env(enabled=True)
        source_path = os.path.join(self._tmp.name, "hook.md")
        with open(source_path, "w", encoding="utf-8") as handle:
            handle.write("钩子切片一\n\n钩子切片二")
        doc_id = kb_ingest._register_wiki_safely(
            "docs/hook.md", source_path, self._chunks(), "note"
        )
        self.assertIsNotNone(doc_id)
        document = doc_repo.get_document(self.conn, doc_id)
        self.assertEqual(document["canonical_uri"], "docs/hook.md")
        self.assertEqual(document["category"], "note")
        revisions = self.conn.execute(
            "SELECT * FROM document_revisions"
        ).fetchall()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["status"], "indexed")
        self.assertEqual(revisions[0]["chunk_count"], 2)
        self.assertEqual(revisions[0]["parser_name"], "kb_ingest")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM chunk_refs").fetchone()[0], 2
        )

        # 再登记一次：fingerprint 相同，复用修订，切片引用不重复。
        same_doc_id = kb_ingest._register_wiki_safely(
            "docs/hook.md", source_path, self._chunks(), "note"
        )
        self.assertEqual(same_doc_id, doc_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM document_revisions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM chunk_refs").fetchone()[0], 2
        )

    def test_hook_disabled_is_noop(self):
        from kb import ingest as kb_ingest

        self._hook_env(enabled=False)
        self.assertIsNone(
            kb_ingest._register_wiki_safely(
                "docs/hook.md", "D:/missing.md", self._chunks()
            )
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
