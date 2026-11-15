# -*- coding: utf-8 -*-
"""证据校验、Claim 证据落库与 LanceDB 原文回查。"""

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import document_registry as registry
from kb.wiki import evidence
from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.ids import chunk_id
from kb.wiki.repositories import claims as claim_repo
from kb.wiki.repositories import documents as doc_repo


class EvidenceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = WikiConfig(
            enabled=True, vault_path=os.path.join(self._tmp.name, "vault")
        )
        self.conn = connect(
            os.path.join(self._tmp.name, "wiki.sqlite3"), self.config
        )
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)

        self.document_id = registry.register_source(
            self.conn,
            registry.SourceDescriptor(
                source_type="file",
                canonical_uri="docs/a.md",
                display_name="a.md",
                category="note",
            ),
        )
        self.revision = registry.begin_revision(
            self.conn,
            self.document_id,
            registry.RevisionInput(
                raw_sha256="raw-a",
                parsed_sha256="parsed-a",
                parser_name="kb_ingest",
                parser_version="1",
                parser_config_sha256="cfg",
                source_path="docs/a.md",
            ),
        )

    def add_chunk(self, text, index=0, **kwargs):
        descriptor = registry.ChunkDescriptor.from_text(
            index, text, source_value=kwargs.pop("source_value", "docs/a.md"), **kwargs
        )
        registry.register_chunk_refs(
            self.conn, self.revision.revision_id, [descriptor]
        )
        return chunk_id(self.revision.revision_id, index, descriptor.text_sha256)

    def add_claim(self, text="测试事实", claim_type="fact", status="proposed"):
        return claim_repo.insert_claim(
            self.conn,
            claim_text=text,
            claim_type=claim_type,
            status=status,
            document_id=self.document_id,
        )


class TestNormalizeAndVerify(unittest.TestCase):
    def test_normalize_ws_collapses_all_whitespace(self):
        self.assertEqual(
            evidence.normalize_ws("  a\t\n b\u3000 c\r\n "), "a b c"
        )
        self.assertEqual(evidence.normalize_ws(""), "")
        self.assertEqual(evidence.normalize_ws(" \n\t "), "")
        with self.assertRaises(TypeError):
            evidence.normalize_ws(None)

    def test_exact_quote_verified(self):
        self.assertEqual(
            evidence.verify_quote("hello world", "hello world"),
            ("verified", 0, 11),
        )

    def test_whitespace_difference_verified_with_original_offsets(self):
        chunk = "Alpha \n\n beta\tgamma"
        status, start, end = evidence.verify_quote(chunk, "Alpha beta gamma")
        self.assertEqual(status, "verified")
        self.assertEqual(start, 0)
        self.assertEqual(end, len(chunk))
        self.assertEqual(
            evidence.normalize_ws(chunk[start:end]), "Alpha beta gamma"
        )

    def test_full_width_space_and_newlines(self):
        chunk = "城市\u3000内涝\n研究进展"
        status, start, end = evidence.verify_quote(chunk, "城市 内涝 研究进展")
        self.assertEqual(status, "verified")
        self.assertEqual(
            evidence.normalize_ws(chunk[start:end]), "城市 内涝 研究进展"
        )

    def test_quote_inside_larger_chunk_offsets(self):
        chunk = "前置说明。\n\n证据句子在这里。\n\n后续内容。"
        status, start, end = evidence.verify_quote(chunk, "证据句子在这里。")
        self.assertEqual(status, "verified")
        self.assertEqual(chunk[start:end], "证据句子在这里。")

    def test_tampered_quote_not_found(self):
        self.assertEqual(
            evidence.verify_quote("alpha beta gamma", "alpha beta delta"),
            ("not_found", 0, 0),
        )

    def test_empty_or_whitespace_quote_not_found(self):
        self.assertEqual(evidence.verify_quote("abc", ""), ("not_found", 0, 0))
        self.assertEqual(
            evidence.verify_quote("abc", " \n\t "), ("not_found", 0, 0)
        )

    def test_repeated_occurrence_returns_first(self):
        status, start, end = evidence.verify_quote("x ab x ab", "ab")
        self.assertEqual((status, start, end), ("verified", 2, 4))


class TestAddClaimEvidence(EvidenceTestBase):
    def test_adds_verified_evidence_with_quote_hash(self):
        chunk = self.add_chunk("Evidence text with spaces.")
        claim = self.add_claim()
        result = evidence.add_claim_evidence(
            self.conn,
            claim,
            chunk,
            "Evidence text with spaces.",
            chunk_text="Evidence text with spaces.",
            role="supports",
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["char_start"], 0)
        self.assertEqual(result["char_end"], len("Evidence text with spaces."))
        self.assertEqual(
            result["quote_sha256"],
            hashlib.sha256(
                evidence.normalize_ws("Evidence text with spaces.").encode("utf-8")
            ).hexdigest(),
        )
        row = self.conn.execute("SELECT * FROM claim_evidence").fetchone()
        self.assertEqual(row["evidence_id"], result["evidence_id"])
        self.assertEqual(row["quote"], "Evidence text with spaces.")
        self.assertEqual(row["text_sha256"], result["quote_sha256"])
        self.assertEqual(row["char_start"], 0)
        self.assertAlmostEqual(row["confidence"], 1.0)

    def test_duplicate_is_idempotent(self):
        chunk = self.add_chunk("alpha beta")
        claim = self.add_claim()
        first = evidence.add_claim_evidence(
            self.conn, claim, chunk, "alpha beta", chunk_text="alpha beta"
        )
        second = evidence.add_claim_evidence(
            self.conn, claim, chunk, "alpha   beta", chunk_text="alpha beta"
        )
        self.assertFalse(second["created"])
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence"
            ).fetchone()[0],
            1,
        )

    def test_tampered_quote_rejected_and_not_stored(self):
        chunk = self.add_chunk("alpha beta")
        claim = self.add_claim()
        with self.assertRaises(evidence.EvidenceVerificationError) as ctx:
            evidence.add_claim_evidence(
                self.conn, claim, chunk, "alpha delta", chunk_text="alpha beta"
            )
        self.assertEqual(ctx.exception.status, "not_found")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence"
            ).fetchone()[0],
            0,
        )

    def test_unknown_claim_or_chunk_rejected(self):
        chunk = self.add_chunk("alpha beta")
        with self.assertRaises(ValueError):
            evidence.add_claim_evidence(
                self.conn, "missing-claim", chunk, "alpha", chunk_text="alpha beta"
            )
        claim = self.add_claim()
        with self.assertRaises(ValueError):
            evidence.add_claim_evidence(
                self.conn, claim, "missing-chunk", "alpha", chunk_text="alpha beta"
            )

    def test_location_enrichment_only_fills_empty_fields(self):
        chunk = self.add_chunk("定位文本内容")
        claim = self.add_claim()
        result = evidence.add_claim_evidence(
            self.conn,
            claim,
            chunk,
            "定位文本内容",
            chunk_text="定位文本内容",
            page_number=3,
            section_path="第一章",
            locator={"bbox": [1, 2, 3, 4]},
        )
        self.assertTrue(result["created"])
        row = doc_repo.get_chunk_ref(self.conn, chunk)
        self.assertEqual(row["page_number"], 3)
        self.assertEqual(row["section_path"], "第一章")
        self.assertEqual(row["locator_json"], '{"bbox": [1, 2, 3, 4]}')

        # 后续证据不覆盖已有定位
        evidence.add_claim_evidence(
            self.conn,
            claim,
            chunk,
            "文本内容",
            chunk_text="定位文本内容",
            page_number=99,
            section_path="第二章",
        )
        row = doc_repo.get_chunk_ref(self.conn, chunk)
        self.assertEqual(row["page_number"], 3)
        self.assertEqual(row["section_path"], "第一章")

    def test_role_is_returned_even_though_schema_has_no_role_column(self):
        chunk = self.add_chunk("支持与反驳")
        claim = self.add_claim()
        result = evidence.add_claim_evidence(
            self.conn,
            claim,
            chunk,
            "支持与反驳",
            chunk_text="支持与反驳",
            role="contradicts",
        )
        self.assertEqual(result["role"], "contradicts")
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(claim_evidence)")
        }
        self.assertNotIn("role", columns)


class TestEvidenceBundle(EvidenceTestBase):
    def test_bundle_returns_full_provenance(self):
        chunk = self.add_chunk(
            "证据原文",
            page_number=5,
            section_path="第二章/方法",
        )
        claim = self.add_claim()
        evidence.add_claim_evidence(
            self.conn, claim, chunk, "证据原文", chunk_text="证据原文"
        )
        bundle = evidence.evidence_bundle(self.conn, chunk)
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["chunk_id"], chunk)
        self.assertEqual(bundle["document_id"], self.document_id)
        self.assertEqual(
            bundle["document_revision_id"], self.revision.revision_id
        )
        self.assertEqual(bundle["source"], "docs/a.md")
        self.assertEqual(bundle["source_value"], "docs/a.md")
        self.assertEqual(bundle["page_number"], 5)
        self.assertEqual(bundle["section_path"], "第二章/方法")
        self.assertEqual(bundle["quote"], "证据原文")
        self.assertIn("证据原文", bundle["quotes"])
        self.assertEqual(bundle["source_type"], "file")

    def test_bundle_accepts_candidate_quote(self):
        chunk = self.add_chunk("候选引文内容")
        bundle = evidence.evidence_bundle(self.conn, chunk, quote="候选引文")
        self.assertEqual(bundle["quote"], "候选引文")
        self.assertEqual(bundle["quotes"], [])

    def test_missing_chunk_returns_none(self):
        self.assertIsNone(evidence.evidence_bundle(self.conn, "missing-chunk"))


class TestClaimsMissingEvidence(EvidenceTestBase):
    def test_only_fact_claims_without_evidence(self):
        no_evidence = self.add_claim("事实无证据", claim_type="fact")
        hypothesis = self.add_claim("假设无证据", claim_type="hypothesis")
        rejected = self.add_claim(
            "已拒绝事实", claim_type="fact", status="rejected"
        )
        disputed = self.add_claim(
            "争议事实", claim_type="fact", status="disputed"
        )
        with_evidence = self.add_claim("事实有证据", claim_type="fact")

        chunk = self.add_chunk("证据内容")
        evidence.add_claim_evidence(
            self.conn, with_evidence, chunk, "证据内容", chunk_text="证据内容"
        )

        missing_ids = {
            row["claim_id"]
            for row in evidence.claims_missing_evidence(self.conn)
        }
        self.assertIn(no_evidence, missing_ids)
        self.assertIn(disputed, missing_ids)
        self.assertNotIn(hypothesis, missing_ids)
        self.assertNotIn(rejected, missing_ids)
        self.assertNotIn(with_evidence, missing_ids)


class TestRevalidateEvidence(EvidenceTestBase):
    def test_revalidate_updates_positions_and_reports_not_found(self):
        chunk = self.add_chunk("alpha beta")
        claim = self.add_claim()
        first = evidence.add_claim_evidence(
            self.conn, claim, chunk, "alpha beta", chunk_text="alpha beta"
        )
        self.assertEqual(first["char_end"], 10)

        stats = evidence.revalidate_evidence(
            self.conn, provider=lambda ref: "alpha   beta"
        )
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["verified"], 1)
        self.assertEqual(stats["not_found"], 0)
        self.assertEqual(stats["updated"], 1)
        row = self.conn.execute("SELECT * FROM claim_evidence").fetchone()
        self.assertEqual(row["char_end"], 12)

        stats = evidence.revalidate_evidence(
            self.conn, provider=lambda ref: "alpha gamma"
        )
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence"
            ).fetchone()[0],
            1,
        )

    def test_revalidate_skips_unavailable_text(self):
        chunk = self.add_chunk("alpha beta")
        claim = self.add_claim()
        evidence.add_claim_evidence(
            self.conn, claim, chunk, "alpha beta", chunk_text="alpha beta"
        )

        def unavailable(ref):
            raise evidence.ChunkTextUnavailable("no text")

        stats = evidence.revalidate_evidence(self.conn, provider=unavailable)
        self.assertEqual(stats["skipped_no_text"], 1)
        self.assertEqual(stats["verified"], 0)

    def test_revalidate_filters_by_claim(self):
        chunk = self.add_chunk("共同证据")
        claim_one = self.add_claim("claim one")
        claim_two = self.add_claim("claim two")
        evidence.add_claim_evidence(
            self.conn, claim_one, chunk, "共同证据", chunk_text="共同证据"
        )
        evidence.add_claim_evidence(
            self.conn, claim_two, chunk, "共同证据", chunk_text="共同证据"
        )
        stats = evidence.revalidate_evidence(
            self.conn,
            claim_id=claim_one,
            provider=lambda ref: "共同证据",
        )
        self.assertEqual(stats["checked"], 1)


class TestLanceChunkTextLookup(EvidenceTestBase):
    def setUp(self):
        super().setUp()
        from kb import schema as kb_schema
        from tests.kb_test_support import activate_fake_embedding

        activate_fake_embedding()
        self._lance_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._lance_tmp.cleanup)
        previous = os.environ.get("LANCEDB_DB_PATH")
        os.environ["LANCEDB_DB_PATH"] = os.path.join(
            self._lance_tmp.name, "lancedb"
        )
        kb_schema.reset_db()

        def cleanup():
            kb_schema.reset_db()
            if previous is None:
                os.environ.pop("LANCEDB_DB_PATH", None)
            else:
                os.environ["LANCEDB_DB_PATH"] = previous

        self.addCleanup(cleanup)

    def add_lance_row(self, text, source="docs/a.md", index=0, doc_id=""):
        from kb import ingest as kb_ingest
        from kb import schema as kb_schema

        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(
            table,
            [
                {
                    "text": text,
                    "source": source,
                    "chunk_index": index,
                    "category": "",
                    "doc_id": doc_id,
                    "ingested_at": "2026-11-15T00:00:00Z",
                }
            ],
        )

    def test_resolves_by_legacy_doc_id(self):
        text = "Lance 原文 alpha beta"
        self.add_lance_row(text, doc_id="lance-doc-1")
        chunk = self.add_chunk(text, legacy_doc_id="lance-doc-1")
        claim = self.add_claim()

        resolved = evidence.resolve_chunk_text(self.conn, chunk)
        self.assertEqual(resolved, text)

        result = evidence.add_claim_evidence(
            self.conn, claim, chunk, "Lance 原文 alpha beta"
        )
        self.assertTrue(result["created"])

    def test_resolves_duplicate_index_by_text_sha_when_legacy_id_missing(self):
        first_text = "同 index 的第一段"
        second_text = "同 index 的第二段"
        self.add_lance_row(first_text, index=0, doc_id="lance-a")
        self.add_lance_row(second_text, index=0, doc_id="lance-b")
        chunk = self.add_chunk(second_text, index=0, legacy_doc_id="")

        self.assertEqual(evidence.resolve_chunk_text(self.conn, chunk), second_text)

    def test_unresolvable_chunk_raises(self):
        chunk = self.add_chunk("不存在于 LanceDB 的文本", legacy_doc_id="missing")
        with self.assertRaises(evidence.ChunkTextUnavailable):
            evidence.resolve_chunk_text(self.conn, chunk)


if __name__ == "__main__":
    unittest.main()
