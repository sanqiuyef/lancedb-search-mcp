# -*- coding: utf-8 -*-
"""SQLite 连接/事务/约束与 Pydantic 模型一致性。"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import constants as C
from kb.wiki import db as wiki_db
from kb.wiki import models as M
from kb.wiki.config import WikiConfig
from kb.wiki.db import (
    WikiDBError,
    connect,
    db_status,
    run_migrations,
    transaction,
)
from kb.wiki.ids import chunk_id, content_sha256

_MODEL_TABLE_MAP = {
    "documents": M.DocumentRecord,
    "document_revisions": M.DocumentRevisionRecord,
    "pages": M.PageRecord,
    "page_revisions": M.PageRevisionRecord,
    "entities": M.EntityRecord,
    "entity_aliases": M.EntityAliasRecord,
    "page_entities": M.PageEntityRecord,
    "page_links": M.PageLinkRecord,
    "relations": M.RelationRecord,
    "chunk_refs": M.ChunkRefRecord,
    "claims": M.ClaimRecord,
    "claim_evidence": M.ClaimEvidenceRecord,
    "proposals": M.ProposalRecord,
    "wiki_jobs": M.WikiJobRecord,
    "write_journal": M.WriteJournalRecord,
}


class WikiDBTestBase(unittest.TestCase):
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

    # ── 常用插入 ──
    def insert_document(self, document_id="doc-1", source_type="file", uri="a.md"):
        self.conn.execute(
            "INSERT INTO documents (document_id, source_type, canonical_uri) "
            "VALUES (?, ?, ?)",
            (document_id, source_type, uri),
        )

    def insert_revision(self, revision_id="rev-1", document_id="doc-1"):
        self.conn.execute(
            "INSERT INTO document_revisions "
            "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
            " processing_fingerprint) VALUES (?, ?, 1, 'raw', 'parsed', 'fp')",
            (revision_id, document_id),
        )

    def insert_page(self, page_id="page-1", slug="Page", relative_path=None):
        self.conn.execute(
            "INSERT INTO pages (page_id, slug, title, page_type, relative_path) "
            "VALUES (?, ?, 'Title', 'concept', ?)",
            (page_id, slug, relative_path or f"concepts-概念/{slug}.md"),
        )

    def insert_entity(self, entity_id="entity-1", name="Entity"):
        self.conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (entity_id, name, name.lower()),
        )


class TestConnection(WikiDBTestBase):
    def test_pragmas_and_row_factory(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(
            str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal"
        )
        self.assertEqual(self.conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        row = self.conn.execute("SELECT 1 AS value").fetchone()
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual(row["value"], 1)

    def test_connect_creates_parent_directory(self):
        nested = os.path.join(self._tmp.name, "nested", "deep", "wiki.sqlite3")
        conn = connect(nested, self.config)
        self.addCleanup(conn.close)
        self.assertTrue(os.path.isfile(nested))

    def test_busy_timeout_from_config(self):
        cfg = WikiConfig(
            enabled=True,
            vault_path=os.path.join(self._tmp.name, "vault2"),
            db_busy_timeout_ms=1234,
        )
        other = os.path.join(self._tmp.name, "other.sqlite3")
        conn = connect(other, cfg)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1234)

    def test_db_status_empty_before_migrations(self):
        other = os.path.join(self._tmp.name, "empty.sqlite3")
        conn = connect(other, self.config)
        self.addCleanup(conn.close)
        self.assertEqual(db_status(conn), [])


class TestSchema(WikiDBTestBase):
    def test_table_set_and_business_count(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        names = {row["name"] for row in rows}
        self.assertEqual(names, set(C.TABLE_NAMES))
        self.assertEqual(len(C.BUSINESS_TABLES), 15)
        self.assertEqual(
            len(names - {C.SCHEMA_MIGRATIONS_TABLE}), 15
        )

    def test_migrations_recorded(self):
        self.assertEqual(db_status(self.conn), ["0001", "0002", "0003", "0004"])
        rows = self.conn.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["name"], "0001_core.sql")
        self.assertRegex(rows[0]["checksum"], r"^[0-9a-f]{64}$")
        self.assertRegex(rows[0]["applied_at"], r"^\d{4}-\d{2}-\d{2}T.*Z$")

    def test_each_table_has_indexes(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        self.assertGreater(len(rows), 20)

    def test_models_match_table_columns(self):
        for table, model in _MODEL_TABLE_MAP.items():
            columns = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            self.assertEqual(
                columns,
                set(model.model_fields),
                f"{table} 与 {model.__name__} 字段不一致",
            )

    def test_enum_values_match_check_constants(self):
        checks = (
            (M.DocumentStatus, C.DOCUMENT_STATUSES),
            (M.DocumentRevisionStatus, C.DOCUMENT_REVISION_STATUSES),
            (M.PageType, C.PAGE_TYPES),
            (M.PageStatus, C.PAGE_STATUSES),
            (M.EntityStatus, C.ENTITY_STATUSES),
            (M.RelationStatus, C.RELATION_STATUSES),
            (M.AliasType, C.ALIAS_TYPES),
            (M.ClaimType, C.CLAIM_TYPES),
            (M.ClaimStatus, C.CLAIM_STATUSES),
            (M.ProposalStatus, C.PROPOSAL_STATUSES),
            (M.JobStatus, C.JOB_STATUSES),
            (M.JournalState, C.JOURNAL_STATES),
        )
        for enum_cls, expected in checks:
            self.assertEqual(
                tuple(item.value for item in enum_cls), expected, enum_cls.__name__
            )


class TestConstraints(WikiDBTestBase):
    def test_foreign_key_violation_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO document_revisions "
                "(revision_id, document_id, revision_no, raw_sha256, "
                " parsed_sha256, processing_fingerprint) "
                "VALUES ('rev-x', 'missing-doc', 1, 'raw', 'parsed', 'fp')"
            )

    def test_document_identity_unique(self):
        self.insert_document("doc-1", "file", "a.md")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_document("doc-2", "file", "a.md")
        self.insert_document("doc-3", "web", "a.md")  # source_type 不同可共存

    def test_page_slug_is_case_insensitive_unique(self):
        self.insert_page("page-1", "Foo", "concepts-概念/Foo.md")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_page("page-2", "foo", "concepts-概念/foo.md")

    def test_page_relative_path_unique(self):
        self.insert_page("page-1", "Foo", "concepts-概念/Foo.md")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_page("page-2", "Bar", "concepts-概念/Foo.md")

    def test_chunk_ref_unique_triple_allows_same_index_different_text(self):
        self.insert_document()
        self.insert_revision()
        sha_a = content_sha256("a")
        sha_b = content_sha256("b")
        self.conn.execute(
            "INSERT INTO chunk_refs (chunk_id, document_id, document_revision_id, "
            "chunk_index, text_sha256) VALUES (?, 'doc-1', 'rev-1', 0, ?)",
            (chunk_id("rev-1", 0, sha_a), sha_a),
        )
        self.conn.execute(
            "INSERT INTO chunk_refs (chunk_id, document_id, document_revision_id, "
            "chunk_index, text_sha256) VALUES (?, 'doc-1', 'rev-1', 0, ?)",
            (chunk_id("rev-1", 0, sha_b), sha_b),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM chunk_refs").fetchone()[0], 2
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO chunk_refs (chunk_id, document_id, document_revision_id, "
                "chunk_index, text_sha256) VALUES ('dup-id', 'doc-1', 'rev-1', 0, ?)",
                (sha_a,),
            )

    def test_relation_subject_object_check(self):
        self.insert_entity("entity-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO relations "
                "(relation_id, subject_entity_id, predicate, object_entity_id) "
                "VALUES ('rel-1', 'entity-1', 'same_as', 'entity-1')"
            )

    def test_relation_invalid_status_rejected(self):
        self.insert_entity("entity-1", "One")
        self.insert_entity("entity-2", "Two")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO relations "
                "(relation_id, subject_entity_id, predicate, object_entity_id, status) "
                "VALUES ('rel-1', 'entity-1', 'related_to', 'entity-2', 'bogus')"
            )
        self.conn.execute(
            "INSERT INTO relations "
            "(relation_id, subject_entity_id, predicate, object_entity_id) "
            "VALUES ('rel-2', 'entity-1', 'related_to', 'entity-2')"
        )
        status = self.conn.execute(
            "SELECT status FROM relations WHERE relation_id = 'rel-2'"
        ).fetchone()[0]
        self.assertEqual(status, "proposed")

    def test_document_invalid_status_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO documents (document_id, source_type, canonical_uri, status) "
                "VALUES ('doc-1', 'file', 'a.md', 'bogus')"
            )

    def test_page_invalid_type_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO pages (page_id, slug, title, page_type, relative_path) "
                "VALUES ('page-1', 'P', 'T', 'bogus', 'x.md')"
            )

    def test_journal_invalid_state_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO write_journal (journal_id, state) VALUES ('j-1', 'bogus')"
            )

    def test_negative_chunk_count_rejected(self):
        self.insert_document()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO document_revisions "
                "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
                " processing_fingerprint, chunk_count) "
                "VALUES ('rev-bad', 'doc-1', 1, 'raw', 'parsed', 'fp', -1)"
            )

    def test_chunk_ref_char_range_check(self):
        self.insert_document()
        self.insert_revision()
        sha = content_sha256("a")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO chunk_refs (chunk_id, document_id, document_revision_id, "
                "chunk_index, text_sha256, char_start, char_end) "
                "VALUES (?, 'doc-1', 'rev-1', 0, ?, 10, 5)",
                (chunk_id("rev-1", 0, sha), sha),
            )


class TestTransactions(WikiDBTestBase):
    def test_commit(self):
        with transaction(self.conn) as conn:
            conn.execute(
                "INSERT INTO documents (document_id, source_type, canonical_uri) "
                "VALUES ('doc-1', 'file', 'a.md')"
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1
        )

    def test_rollback_on_exception(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.conn) as conn:
                conn.execute(
                    "INSERT INTO documents (document_id, source_type, canonical_uri) "
                    "VALUES ('doc-1', 'file', 'a.md')"
                )
                raise RuntimeError("boom")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
        )

    def test_nested_transaction_rejected(self):
        with transaction(self.conn):
            with self.assertRaises(WikiDBError):
                with transaction(self.conn):
                    pass


class TestRecordModels(unittest.TestCase):
    def test_extra_fields_rejected(self):
        with self.assertRaises(ValueError):
            M.DocumentRecord(
                document_id="doc-1",
                source_type="file",
                canonical_uri="a.md",
                created_at="2026-11-15T00:00:00Z",
                updated_at="2026-11-15T00:00:00Z",
                unexpected="x",
            )

    def test_relation_subject_object_validator(self):
        with self.assertRaises(ValueError):
            M.RelationRecord(
                subject_entity_id="e1",
                predicate="same_as",
                object_entity_id="e1",
                created_at="2026-11-15T00:00:00Z",
                updated_at="2026-11-15T00:00:00Z",
            )

    def test_chunk_ref_validators(self):
        with self.assertRaises(ValueError):
            M.ChunkRefRecord(
                chunk_id="c1",
                document_id="doc-1",
                document_revision_id="rev-1",
                chunk_index=0,
                text_sha256="sha",
                char_start=10,
                char_end=5,
                created_at="2026-11-15T00:00:00Z",
            )

    def test_valid_records_get_default_ulids(self):
        page = M.PageRecord(
            slug="p",
            title="P",
            page_type=M.PageType.CONCEPT,
            relative_path="concepts-概念/p.md",
            created_at="2026-11-15T00:00:00Z",
            updated_at="2026-11-15T00:00:00Z",
        )
        self.assertEqual(len(page.page_id), 26)
        self.assertEqual(page.status, M.PageStatus.DRAFT)


if __name__ == "__main__":
    unittest.main()
