# -*- coding: utf-8 -*-
"""validator：逐类构造问题，断言 code/level，且验证只读行为。"""

import glob
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.markdown import parse_frontmatter, render_frontmatter
from kb.wiki.page_store import PageDraft, PageStore, atomic_write_text
from kb.wiki.validator import validate


class WikiValidatorTestBase(unittest.TestCase):
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

    def create_page(
        self, slug="page-1", title=None, body="正文\n", status="published"
    ):
        return self.store.create_page(
            PageDraft(
                title=title or f"Title {slug}",
                page_type="concept",
                summary="校验测试页面摘要。",
                body=body,
                slug=slug,
                status=status,
            ),
            commit=False,
        )

    def path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def read_text(self, page):
        with open(self.path(page), "r", encoding="utf-8") as handle:
            return handle.read()

    def rewrite_frontmatter(self, page, **changes):
        text = self.read_text(page)
        frontmatter, body = parse_frontmatter(text)
        frontmatter.update(changes)
        atomic_write_text(self.path(page), render_frontmatter(frontmatter, body))

    def codes(self, issues):
        return {(issue["level"], issue["code"]) for issue in issues}

    def insert_document(self, document_id="doc-1", status="active"):
        self.conn.execute(
            "INSERT INTO documents "
            "(document_id, source_type, canonical_uri, display_name, status) "
            "VALUES (?, 'file', ?, ?, ?)",
            (document_id, f"{document_id}.md", document_id, status),
        )
        return document_id

    def insert_claim(
        self,
        claim_id="claim-1",
        page_id=None,
        document_id=None,
        claim_type="fact",
    ):
        self.conn.execute(
            "INSERT INTO claims (claim_id, page_id, document_id, claim_text, "
            "claim_type, status) VALUES (?, ?, ?, '测试 claim', ?, 'accepted')",
            (claim_id, page_id, document_id, claim_type),
        )
        return claim_id

    def insert_evidence(self, claim_id="claim-1"):
        self.conn.execute(
            "INSERT INTO document_revisions "
            "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
            " processing_fingerprint) VALUES ('rev-1', 'doc-1', 1, 'raw', 'parsed', 'fp')"
        )
        self.conn.execute(
            "INSERT INTO chunk_refs (chunk_id, document_id, document_revision_id, "
            "chunk_index, text_sha256) VALUES ('chunk-1', 'doc-1', 'rev-1', 0, 'sha')"
        )
        self.conn.execute(
            "INSERT INTO claim_evidence (evidence_id, claim_id, chunk_id, quote) "
            "VALUES ('evidence-1', ?, 'chunk-1', 'quote')",
            (claim_id,),
        )

    def insert_entity(self, entity_id="entity-1", name=None):
        self.conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (entity_id, name or entity_id, (name or entity_id).casefold()),
        )


class TestValidatorIssues(WikiValidatorTestBase):
    def test_clean_state_reports_nothing(self):
        page = self.create_page()
        self.assertEqual(validate(self.conn, self.config), [])
        self.assertEqual(self.store.detect_external_edit(page.page_id), "clean")

    def test_frontmatter_invalid(self):
        page = self.create_page()
        text = self.read_text(page)
        frontmatter, body = parse_frontmatter(text)
        frontmatter.pop("summary")
        atomic_write_text(self.path(page), render_frontmatter(frontmatter, body))

        issues = validate(self.conn, self.config)
        self.assertIn(("error", "frontmatter_invalid"), self.codes(issues))
        issue = next(item for item in issues if item["code"] == "frontmatter_invalid")
        self.assertEqual(issue["page_id"], page.page_id)

    def test_content_hash_mismatch(self):
        page = self.create_page()
        atomic_write_text(self.path(page), self.read_text(page) + "\n手工编辑\n")
        issues = validate(self.conn, self.config)
        self.assertIn(("error", "content_hash_mismatch"), self.codes(issues))
        self.assertEqual(
            next(
                item for item in issues if item["code"] == "content_hash_mismatch"
            )["page_id"],
            page.page_id,
        )

    def test_current_revision_missing(self):
        page = self.create_page()
        self.conn.execute(
            "UPDATE pages SET current_revision_id = NULL WHERE page_id = ?",
            (page.page_id,),
        )
        issues = validate(self.conn, self.config)
        self.assertIn(("error", "current_revision_missing"), self.codes(issues))

    def test_current_revision_belongs_to_another_page(self):
        first = self.create_page("first")
        second = self.create_page("second")
        self.conn.execute(
            "UPDATE pages SET current_revision_id = ? WHERE page_id = ?",
            (second.current_revision_id, first.page_id),
        )
        issues = validate(self.conn, self.config)
        self.assertIn(("error", "current_revision_invalid"), self.codes(issues))
        self.assertIn(("error", "revision_id_mismatch"), self.codes(issues))

    def test_frontmatter_metadata_and_revision_id_mismatch(self):
        page = self.create_page()
        self.rewrite_frontmatter(
            page,
            status="archived",
            revision_id="01JABCDEFGHJKMNPQRSTVWXYZ9",
        )
        codes = self.codes(validate(self.conn, self.config))
        self.assertIn(("error", "frontmatter_metadata_mismatch"), codes)
        self.assertIn(("error", "revision_id_mismatch"), codes)

    def test_broken_link_is_warning(self):
        target = self.create_page("target", body="目标\n")
        source = self.create_page(
            "source", body="[[target]] [[missing]]\n"
        )
        issues = validate(self.conn, self.config)
        broken = [item for item in issues if item["code"] == "broken_link"]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["level"], "warning")
        self.assertIn("missing", broken[0]["message"])
        self.assertEqual(broken[0]["page_id"], source.page_id)
        self.assertFalse(
            any(
                item["code"] == "broken_link"
                and "[[target]]" in item["message"]
                for item in issues
            )
        )
        self.assertIsNotNone(target.page_id)

    def test_fact_claim_missing_evidence_level_depends_on_config(self):
        page = self.create_page()
        self.insert_document()
        self.insert_claim("fact-claim", page_id=page.page_id, document_id="doc-1")
        self.insert_claim(
            "hypothesis-claim",
            page_id=page.page_id,
            document_id="doc-1",
            claim_type="hypothesis",
        )

        issues = validate(self.conn, self.config)
        missing = [item for item in issues if item["code"] == "fact_claim_missing_evidence"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["level"], "error")
        self.assertEqual(missing[0]["page_id"], page.page_id)

        loose_config = WikiConfig(
            enabled=True,
            vault_path=self.vault,
            db_path=self.config.db_path,
            require_evidence=False,
        )
        missing = [
            item
            for item in validate(self.conn, loose_config)
            if item["code"] == "fact_claim_missing_evidence"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["level"], "warning")

    def test_fact_claim_with_evidence_is_clean(self):
        page = self.create_page()
        self.insert_document()
        self.insert_claim("fact-claim", page_id=page.page_id, document_id="doc-1")
        self.insert_evidence("fact-claim")
        codes = self.codes(validate(self.conn, self.config))
        self.assertNotIn(("error", "fact_claim_missing_evidence"), codes)

    def test_source_missing_and_claim_source_missing(self):
        page = self.create_page()
        self.insert_document("doc-missing", status="missing")
        self.insert_claim(
            "claim-source",
            page_id=page.page_id,
            document_id="doc-missing",
            claim_type="hypothesis",
        )
        codes = self.codes(validate(self.conn, self.config))
        self.assertIn(("warning", "source_missing"), codes)
        self.assertIn(("warning", "claim_source_missing"), codes)

    def test_relation_invalid_predicate_and_self_loop(self):
        self.insert_entity("entity-1", "Alpha")
        self.insert_entity("entity-2", "Beta")
        self.conn.execute(
            "INSERT INTO relations "
            "(relation_id, subject_entity_id, predicate, object_entity_id) "
            "VALUES ('rel-bad', 'entity-1', 'bogus_predicate', 'entity-2')"
        )
        self.conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            self.conn.execute(
                "INSERT INTO relations "
                "(relation_id, subject_entity_id, predicate, object_entity_id) "
                "VALUES ('rel-self', 'entity-1', 'uses', 'entity-1')"
            )
        finally:
            self.conn.execute("PRAGMA ignore_check_constraints = OFF")

        codes = self.codes(validate(self.conn, self.config))
        self.assertIn(("error", "relation_invalid_predicate"), codes)
        self.assertIn(("error", "relation_self_loop"), codes)

    def test_page_file_missing_and_orphan_file(self):
        page = self.create_page()
        os.unlink(self.path(page))

        orphan_dir = self.config.page_type_dir("concept")
        os.makedirs(orphan_dir, exist_ok=True)
        orphan = {
            "title": "Orphan",
            "tags": [],
            "created": "2026-01-01",
            "type": "permanent",
            "summary": "孤儿页面。",
            "page_id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
            "slug": "orphan",
            "page_type": "concept",
            "status": "draft",
            "aliases": [],
            "revision_id": "01JABCDEFGHJKMNPQRSTVWXYZ1",
            "updated": "2026-01-01T00:00:00Z",
        }
        atomic_write_text(
            os.path.join(orphan_dir, "orphan.md"),
            render_frontmatter(orphan, "孤儿\n"),
        )

        issues = validate(self.conn, self.config)
        codes = self.codes(issues)
        self.assertIn(("error", "page_file_missing"), codes)
        self.assertIn(("warning", "orphan_page_file"), codes)
        orphan_issue = next(
            item for item in issues if item["code"] == "orphan_page_file"
        )
        self.assertIsNone(orphan_issue["page_id"])
        self.assertIn("orphan.md", orphan_issue["message"])

    def test_issue_shape_and_sort_order(self):
        page = self.create_page()
        self.rewrite_frontmatter(page, revision_id="01JABCDEFGHJKMNPQRSTVWXYZ9")
        issues = validate(self.conn, self.config)
        for issue in issues:
            self.assertEqual(
                set(issue), {"level", "code", "message", "page_id"}
            )
            self.assertIn(issue["level"], {"error", "warning", "info"})
        levels = [issue["level"] for issue in issues]
        self.assertEqual(
            levels, sorted(levels, key={"error": 0, "warning": 1, "info": 2}.get)
        )

    def test_validate_is_read_only(self):
        page = self.create_page(body="[[missing]]\n")
        self.insert_document()
        self.insert_claim("fact-claim", page_id=page.page_id, document_id="doc-1")
        before_counts = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("pages", "page_revisions", "page_links", "claims", "documents")
        }
        before_text = self.read_text(page)
        before_files = sorted(
            os.path.relpath(path, self.vault)
            for path in glob.glob(os.path.join(self.vault, "**", "*.md"), recursive=True)
        )

        validate(self.conn, self.config)

        after_counts = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("pages", "page_revisions", "page_links", "claims", "documents")
        }
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(self.read_text(page), before_text)
        after_files = sorted(
            os.path.relpath(path, self.vault)
            for path in glob.glob(os.path.join(self.vault, "**", "*.md"), recursive=True)
        )
        self.assertEqual(before_files, after_files)


if __name__ == "__main__":
    unittest.main()
