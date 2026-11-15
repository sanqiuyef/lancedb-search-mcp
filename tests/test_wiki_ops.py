# -*- coding: utf-8 -*-
"""运维脚本：备份 manifest、validate CLI 退出码、质量报告口径、元数据重建。"""

import glob
import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.evidence import quote_sha256
from kb.wiki.markdown import render_frontmatter
from kb.wiki.page_store import PageDraft, PageStore
from kb.wiki.repositories import jobs as jobs_repo
from kb.wiki.repositories import pages as pages_repo
from scripts import backup_wiki, rebuild_wiki_metadata, validate_wiki
from scripts import wiki_quality_report as quality


class WikiOpsTestBase(unittest.TestCase):
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
        self,
        slug,
        title=None,
        summary=None,
        body="正文。\n",
        status="published",
        aliases=None,
    ):
        return self.store.create_page(
            PageDraft(
                title=title or f"Title {slug}",
                page_type="concept",
                summary=summary or f"{slug} 的页面摘要。",
                body=body,
                slug=slug,
                aliases=aliases or [],
                status=status,
            ),
            commit=False,
        )

    def page_path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()


class TestBackupWiki(WikiOpsTestBase):
    def test_backup_creates_db_proposals_and_manifest(self):
        self.create_page("backup-page")
        proposals_dir = os.path.join(
            os.path.dirname(self.config.db_path), "proposals", "p-1"
        )
        os.makedirs(proposals_dir)
        draft_path = os.path.join(proposals_dir, "draft.md")
        with open(draft_path, "w", encoding="utf-8") as handle:
            handle.write("# 提案草稿\n")

        out = os.path.join(self.root, "backup-out", "wiki.sqlite3")
        with redirect_stdout(io.StringIO()):
            code = backup_wiki.main(
                [
                    "--db",
                    self.config.db_path,
                    "--vault",
                    self.vault,
                    "--out",
                    out,
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(out))
        manifest_path = os.path.join(os.path.dirname(out), "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)

        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(
            manifest["source_db"]["path"], os.path.abspath(self.config.db_path)
        )
        self.assertEqual(
            manifest["backup"]["sha256"], backup_wiki.sha256_file(out)
        )
        self.assertEqual(
            manifest["backup"]["size_bytes"], os.path.getsize(out)
        )
        self.assertEqual(manifest["backup"]["integrity_check"], "ok")
        self.assertGreater(manifest["backup"]["schema_versions"].__len__(), 0)

        # 备份库可打开且数据完整
        restored = sqlite3.connect(out)
        try:
            self.assertEqual(
                restored.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1
            )
            self.assertEqual(
                restored.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            restored.close()

        proposals = manifest["proposals"]
        self.assertIsNotNone(proposals)
        self.assertEqual(proposals["file_count"], 1)
        self.assertTrue(os.path.isfile(proposals["path"]))
        self.assertEqual(
            proposals["sha256"], backup_wiki.sha256_file(proposals["path"])
        )
        with tarfile.open(proposals["path"], "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("proposals/p-1/draft.md", names)
        self.assertFalse(manifest["vault"]["is_git_repo"])

    def test_default_output_directory(self):
        self.create_page("default-backup")
        with redirect_stdout(io.StringIO()):
            code = backup_wiki.main(
                ["--db", self.config.db_path, "--vault", self.vault]
            )
        self.assertEqual(code, 0)
        backups = glob.glob(
            os.path.join(
                os.path.dirname(self.config.db_path), "backups", "wiki-*.sqlite3"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertTrue(
            os.path.isfile(os.path.join(os.path.dirname(backups[0]), "manifest.json"))
        )

    def test_missing_source_returns_error_without_output(self):
        out = os.path.join(self.root, "should-not-exist.sqlite3")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = backup_wiki.main(
                [
                    "--db",
                    os.path.join(self.root, "missing.sqlite3"),
                    "--vault",
                    self.vault,
                    "--out",
                    out,
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("源库不存在", stderr.getvalue())
        self.assertFalse(os.path.exists(out))


class TestValidateWikiCLI(WikiOpsTestBase):
    def run_cli(self, *extra):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = validate_wiki.main(
                [
                    "--db",
                    self.config.db_path,
                    "--vault",
                    self.vault,
                    *extra,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_clean_state_json(self):
        self.create_page("clean-page")
        code, stdout, _stderr = self.run_cli("--json")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"]["total"], 0)
        self.assertEqual(payload["journal"]["total"], 0)

    def test_warning_only_passes_strict(self):
        self.create_page("warning-page", body="[[missing-target]]\n")
        code, stdout, _stderr = self.run_cli("--json", "--strict")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["counts"]["error"], 0)
        self.assertEqual(payload["counts"]["warning"], 1)
        self.assertEqual(payload["issues"][0]["code"], "broken_link")

    def test_error_fails_strict(self):
        page = self.create_page("error-page")
        os.unlink(self.page_path(page))
        code, stdout, _stderr = self.run_cli("--json", "--strict")
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertGreaterEqual(payload["counts"]["error"], 1)
        self.assertIn(
            "page_file_missing",
            {issue["code"] for issue in payload["issues"]},
        )

    def test_failed_journal_is_reported(self):
        page = self.create_page("journal-page")
        journal = jobs_repo.create_journal(
            self.conn,
            page_id=page.page_id,
            revision_id=page.current_revision_id,
            target_path=page.relative_path,
            state="failed",
            error_message="模拟失败",
        )
        code, stdout, _stderr = self.run_cli("--json")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        failed = payload["journal"]["failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["journal_id"], journal.journal_id)
        self.assertEqual(failed[0]["error_message"], "模拟失败")

    def test_missing_database_returns_two(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = validate_wiki.main(
                [
                    "--db",
                    os.path.join(self.root, "missing.sqlite3"),
                    "--vault",
                    self.vault,
                    "--json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("不存在", stderr.getvalue())

    def test_markdown_output_contains_sections(self):
        self.create_page("markdown-page")
        code, stdout, _stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("# Wiki 校验报告", stdout)
        self.assertIn("未发现问题", stdout)
        self.assertIn("未完成写入日志", stdout)


class TestQualityReport(WikiOpsTestBase):
    def build_fixture(self):
        self.p2 = self.create_page(
            "beta",
            title="Beta",
            summary="Beta 页面摘要，描述不同主题。",
            body="[[missing-page]]\n",
            aliases=["SharedAlias"],
        )
        self.p3 = self.create_page(
            "gamma",
            title="Gamma",
            summary="Gamma 页面摘要，描述另一个主题。",
            body="",
            aliases=["SharedAlias"],
        )
        self.p1 = self.create_page(
            "alpha",
            title="Alpha",
            summary="完全相同的摘要内容，用于重复候选检测。",
            body="[[beta]]\n",
            aliases=["SharedOne"],
        )
        self.p4 = self.create_page(
            "alpha-two",
            title="Alpha",
            summary="完全相同的摘要内容，用于重复候选检测。",
            body="[[alpha]]\n",
        )

        self.conn.execute(
            "INSERT INTO documents (document_id, source_type, canonical_uri, status) "
            "VALUES ('doc-1', 'file', 'doc-1.md', 'active')"
        )
        self.conn.execute(
            "INSERT INTO document_revisions "
            "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
            " processing_fingerprint, status) "
            "VALUES ('rev-1', 'doc-1', 1, 'raw', 'parsed', 'fp', 'indexed')"
        )
        self.conn.execute(
            "INSERT INTO chunk_refs (chunk_id, document_id, document_revision_id, "
            "chunk_index, text_sha256) VALUES ('chunk-1', 'doc-1', 'rev-1', 0, 'sha')"
        )

        for claim_id, claim_type, status in (
            ("fact-covered-1", "fact", "accepted"),
            ("fact-covered-2", "fact", "proposed"),
            ("fact-missing", "fact", "proposed"),
            ("hypothesis-1", "hypothesis", "accepted"),
        ):
            self.conn.execute(
                "INSERT INTO claims (claim_id, page_id, document_id, claim_text, "
                "claim_type, status) VALUES (?, ?, 'doc-1', ?, ?, ?)",
                (claim_id, self.p1.page_id, f"claim {claim_id}", claim_type, status),
            )
        self.conn.execute(
            "INSERT INTO claim_evidence "
            "(evidence_id, claim_id, chunk_id, quote, char_start, char_end, "
            " text_sha256) VALUES (?, ?, 'chunk-1', ?, 0, 4, ?)",
            ("evidence-valid", "fact-covered-1", "引文一", quote_sha256("引文一")),
        )
        self.conn.execute(
            "INSERT INTO claim_evidence "
            "(evidence_id, claim_id, chunk_id, quote, char_start, char_end, "
            " text_sha256) VALUES (?, ?, 'chunk-1', ?, 0, 4, ?)",
            ("evidence-invalid", "fact-covered-2", "引文二", "not-the-quote-hash"),
        )

        for entity_id, name, status in (
            ("entity-1", "Alpha Entity", "active"),
            ("entity-2", "Beta Entity", "active"),
            ("entity-3", "Legacy Entity", "deprecated"),
        ):
            self.conn.execute(
                "INSERT INTO entities (entity_id, canonical_name, normalized_name, "
                "status) VALUES (?, ?, ?, ?)",
                (entity_id, name, name.casefold(), status),
            )
        self.conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, normalized_alias, "
            "alias_type) VALUES ('entity-3', 'Legacy', 'legacy', 'alias')"
        )
        self.conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, normalized_alias, "
            "alias_type) VALUES ('entity-1', 'missing-entity', "
            "'missing-entity', 'redirect')"
        )
        self.conn.execute(
            "INSERT INTO relations (relation_id, subject_entity_id, predicate, "
            "object_entity_id, status, evidence, source_page_id) "
            "VALUES ('rel-unsourced', 'entity-1', 'uses', 'entity-2', "
            "'proposed', '', NULL)"
        )
        self.conn.execute(
            "INSERT INTO relations (relation_id, subject_entity_id, predicate, "
            "object_entity_id, status, evidence, source_page_id) "
            "VALUES ('rel-sourced', 'entity-2', 'uses', 'entity-1', "
            "'verified', '有来源', ?)",
            (self.p1.page_id,),
        )
        self.conn.execute(
            "INSERT INTO relations (relation_id, subject_entity_id, predicate, "
            "object_entity_id, status, evidence, source_page_id) "
            "VALUES ('rel-rejected', 'entity-1', 'based_on', 'entity-2', "
            "'rejected', '', NULL)"
        )

    def test_metric_values_and_scope_notes(self):
        self.build_fixture()
        report = quality.build_report(self.conn, self.config)
        metrics = report["metrics"]

        coverage = metrics["evidence_coverage"]
        self.assertEqual(coverage["applicable"], 3)
        self.assertEqual(coverage["covered"], 2)
        self.assertAlmostEqual(coverage["ratio"], 2 / 3)
        self.assertEqual(coverage["missing_claim_ids"], ["fact-missing"])

        quote = metrics["quote_validity"]
        self.assertEqual(quote["total"], 2)
        self.assertEqual(quote["valid"], 1)
        self.assertAlmostEqual(quote["ratio"], 0.5)
        self.assertEqual(
            quote["invalid_evidence"][0]["evidence_id"], "evidence-invalid"
        )

        broken = metrics["broken_link_rate"]
        self.assertEqual(broken["broken"], 1)
        self.assertEqual(broken["total"], 3)
        self.assertAlmostEqual(broken["ratio"], 1 / 3)

        orphan = metrics["orphan_pages"]
        self.assertEqual(orphan["published"], 4)
        self.assertEqual(orphan["orphan_count"], 1)
        self.assertEqual(orphan["pages"][0]["slug"], "gamma")

        duplicates = metrics["duplicate_page_candidates"]
        self.assertEqual(duplicates["group_count"], 2)
        self.assertGreaterEqual(duplicates["summary_pair_count"], 1)
        match_types = {
            match_type
            for group in duplicates["title_alias_groups"]
            for match_type in group["match_types"]
        }
        self.assertIn("title", match_types)
        self.assertIn("alias", match_types)

        unindexed = metrics["unindexed_published_pages"]
        self.assertEqual(unindexed["status"], "index_not_built")
        self.assertIn("索引未建立", unindexed["message"])

        self.assertEqual(metrics["unsourced_relations"]["count"], 1)
        self.assertEqual(
            metrics["unsourced_relations"]["relations"][0]["relation_id"],
            "rel-unsourced",
        )

        aliases = metrics["unresolved_aliases"]
        self.assertEqual(aliases["count"], 2)
        self.assertEqual(aliases["categories"]["inactive_owner"], 1)
        self.assertEqual(aliases["categories"]["dangling_redirect"], 1)
        self.assertEqual(aliases["categories"]["ambiguous_active_alias"], 0)

        self.assertTrue(
            any("关系正确率需人工抽样" in note for note in report["notes"])
        )
        self.assertTrue(
            any("Quote Validity" in note and "revalidate_evidence" in note
                for note in report["notes"])
        )

    def test_quote_validity_uses_verification_column_when_present(self):
        self.build_fixture()
        self.conn.execute(
            "ALTER TABLE claim_evidence ADD COLUMN verification TEXT NOT NULL "
            "DEFAULT ''"
        )
        self.conn.execute(
            "UPDATE claim_evidence SET verification = 'verified' "
            "WHERE evidence_id = 'evidence-valid'"
        )
        self.conn.execute(
            "UPDATE claim_evidence SET verification = 'not_found' "
            "WHERE evidence_id = 'evidence-invalid'"
        )
        quote = quality.build_report(self.conn, self.config)["metrics"][
            "quote_validity"
        ]
        self.assertEqual(quote["method"], "verification_column")
        self.assertEqual(quote["total"], 2)
        self.assertEqual(quote["valid"], 1)
        self.assertAlmostEqual(quote["ratio"], 0.5)
        self.assertEqual(
            quote["invalid_evidence"][0]["reason"], "verification_not_verified"
        )

    def test_index_table_presence_is_checked(self):
        self.build_fixture()
        self.conn.execute(
            "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY)"
        )
        self.conn.execute(
            "INSERT INTO wiki_pages (page_id) VALUES (?)", (self.p1.page_id,)
        )
        report = quality.build_report(self.conn, self.config)
        unindexed = report["metrics"]["unindexed_published_pages"]
        self.assertEqual(unindexed["status"], "ok")
        self.assertEqual(unindexed["published_count"], 4)
        self.assertEqual(unindexed["missing_count"], 3)
        self.assertEqual(unindexed["match_key"], "page_id")

    def test_cli_markdown_out_and_json(self):
        self.build_fixture()
        out = os.path.join(self.root, "docs", "quality-report.md")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = quality.main(
                [
                    "--db",
                    self.config.db_path,
                    "--vault",
                    self.vault,
                    "--out",
                    out,
                ]
            )
        self.assertEqual(code, 0)
        with open(out, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# Wiki 质量报告", text)
        self.assertIn("Evidence Coverage", text)
        self.assertIn("Quote Validity", text)
        self.assertIn("索引未建立", text)
        self.assertIn("关系正确率需人工抽样", text)
        self.assertIn("质量报告已写入", stdout.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = quality.main(
                [
                    "--db",
                    self.config.db_path,
                    "--vault",
                    self.vault,
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("metrics", payload)
        self.assertAlmostEqual(
            payload["metrics"]["evidence_coverage"]["ratio"], 2 / 3
        )


class TestRebuildMetadata(WikiOpsTestBase):
    def frontmatter(self, slug, page_id, revision_id, **overrides):
        data = {
            "title": f"Title {slug}",
            "tags": ["test"],
            "created": "2026-01-01",
            "type": "permanent",
            "summary": f"{slug} 摘要。",
            "page_id": page_id,
            "slug": slug,
            "page_type": "concept",
            "status": "published",
            "aliases": [],
            "revision_id": revision_id,
            "updated": "2026-01-02T00:00:00.000Z",
        }
        data.update(overrides)
        return data

    def write_page(self, slug, page_id, revision_id, **overrides):
        directory = self.config.page_type_dir("concept")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{slug}.md")
        data = self.frontmatter(slug, page_id, revision_id, **overrides)
        if page_id is None:
            data.pop("page_id", None)
        content = render_frontmatter(data, "正文。\n")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return path

    def test_dry_run_apply_idempotent_and_never_edits_files(self):
        valid_path = self.write_page(
            "valid-page",
            "01JABCDEFGHJKMNPQRSTVWXYZ1",
            "01JABCDEFGHJKMNPQRSTVWXYZ2",
        )
        missing_id_path = self.write_page(
            "no-id-page",
            None,
            "01JABCDEFGHJKMNPQRSTVWXYZ4",
        )
        directory = self.config.page_type_dir("concept")
        invalid_path = os.path.join(directory, "invalid-page.md")
        with open(invalid_path, "w", encoding="utf-8", newline="\n") as handle:
            data = self.frontmatter(
                "invalid-page",
                "01JABCDEFGHJKMNPQRSTVWXYZ5",
                "01JABCDEFGHJKMNPQRSTVWXYZ6",
            )
            data.pop("summary")
            handle.write(render_frontmatter(data, "正文。\n"))

        orphan = self.create_page("db-orphan")
        os.unlink(self.page_path(orphan))

        before_missing_id = self.read_bytes(missing_id_path)
        before_valid = self.read_bytes(valid_path)
        before_pages = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

        report = rebuild_wiki_metadata.rebuild(
            self.conn, self.config, apply=False
        )
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["scanned"], 3)
        self.assertEqual(len(report["register_candidates"]), 1)
        self.assertEqual(report["registered"], [])
        self.assertEqual(len(report["missing_page_id"]), 1)
        self.assertIn("no-id-page.md", report["missing_page_id"][0]["relative_path"])
        self.assertEqual(len(report["invalid_frontmatter"]), 1)
        self.assertEqual(
            len(report["orphan_page_records"]), 1
        )
        self.assertEqual(
            report["orphan_page_records"][0]["page_id"], orphan.page_id
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            before_pages,
        )

        applied = rebuild_wiki_metadata.rebuild(
            self.conn, self.config, apply=True
        )
        self.assertFalse(applied["dry_run"])
        self.assertEqual(len(applied["registered"]), 1)
        committed = pages_repo.get_page(
            self.conn, "01JABCDEFGHJKMNPQRSTVWXYZ1"
        )
        self.assertIsNotNone(committed)
        self.assertEqual(committed.slug, "valid-page")
        self.assertEqual(
            committed.relative_path,
            "wiki-维基/concepts-概念/valid-page.md",
        )
        self.assertEqual(
            committed.current_revision_id, "01JABCDEFGHJKMNPQRSTVWXYZ2"
        )
        revision = pages_repo.get_page_revision(
            self.conn, "01JABCDEFGHJKMNPQRSTVWXYZ2"
        )
        self.assertIsNotNone(revision)
        self.assertEqual(
            revision.content_sha256,
            hashlib.sha256(before_valid).hexdigest(),
        )
        self.assertEqual(self.read_bytes(missing_id_path), before_missing_id)
        self.assertEqual(self.read_bytes(valid_path), before_valid)

        pages_after_apply = self.conn.execute(
            "SELECT COUNT(*) FROM pages"
        ).fetchone()[0]
        revisions_after_apply = self.conn.execute(
            "SELECT COUNT(*) FROM page_revisions"
        ).fetchone()[0]
        again = rebuild_wiki_metadata.rebuild(
            self.conn, self.config, apply=True
        )
        self.assertEqual(again["registered"], [])
        self.assertEqual(again["register_candidates"], [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            pages_after_apply,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM page_revisions").fetchone()[0],
            revisions_after_apply,
        )

    def test_cli_json_dry_run_and_apply(self):
        self.write_page(
            "cli-page",
            "01JABCDEFGHJKMNPQRSTVWXYZ7",
            "01JABCDEFGHJKMNPQRSTVWXYZ8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = rebuild_wiki_metadata.main(
                [
                    "--db",
                    self.config.db_path,
                    "--vault",
                    self.vault,
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["register_candidates"]), 1)
        self.assertEqual(report["registered"], [])

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = rebuild_wiki_metadata.main(
                [
                    "--db",
                    self.config.db_path,
                    "--vault",
                    self.vault,
                    "--apply",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["dry_run"])
        self.assertEqual(len(report["registered"]), 1)
        self.assertIsNotNone(
            pages_repo.get_page(self.conn, "01JABCDEFGHJKMNPQRSTVWXYZ7")
        )

    def test_missing_database_returns_two(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = rebuild_wiki_metadata.main(
                [
                    "--db",
                    os.path.join(self.root, "missing.sqlite3"),
                    "--vault",
                    self.vault,
                    "--json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("不存在", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
