# -*- coding: utf-8 -*-
"""Wiki service 门面：只读查询、草稿/提案/审核、两步抽取与校验。

所有用例使用临时 vault、临时 Git 仓库、临时 SQLite 与临时 LanceDB 路径，
不访问真实知识库；页面索引通过 monkeypatch 隔离，不加载 embedding 模型。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import entity_resolver, service  # noqa: E402
from kb.wiki import evidence as wiki_evidence  # noqa: E402
from kb.wiki.config import WikiConfig  # noqa: E402
from kb.wiki.db import connect, run_migrations  # noqa: E402
from kb.wiki.ids import normalize_text_sha256  # noqa: E402
from kb.wiki.models import PageEntityRecord  # noqa: E402
from kb.wiki.page_store import PageDraft, PageStore  # noqa: E402
from kb.wiki.repositories import entities as entities_repo  # noqa: E402
from kb.wiki.repositories import jobs as jobs_repo  # noqa: E402
from kb.wiki.repositories import relations as relations_repo  # noqa: E402
from kb import ingest as kb_ingest  # noqa: E402
from kb import schema as kb_schema  # noqa: E402
from tests.kb_test_support import activate_fake_embedding  # noqa: E402

CHUNK_TEXT = "城市洪涝受降雨与排水能力共同影响，SWMM 模型常用于模拟。"


class WikiServiceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)
        self.lance_db = os.path.join(self.root, "lancedb")
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("LANCEDB_DB_PATH", "WIKI_ENABLED")
        }
        os.environ["LANCEDB_DB_PATH"] = self.lance_db
        os.environ["WIKI_ENABLED"] = "1"
        self.addCleanup(self._restore_env)

        self.config = WikiConfig(
            enabled=True,
            vault_path=self.vault,
            db_path=os.path.join(self.root, "db", "wiki.sqlite3"),
            min_entity_confidence=0.5,
            min_relation_confidence=0.5,
        )
        self.conn = connect(self.config.db_path, self.config)
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)
        self.store = PageStore(self.config, self.conn)
        self.init_git()

        self.index_patcher = mock.patch(
            "kb.wiki.service.page_index.index_page",
            side_effect=self._index_stub,
        )
        self.index_mock = self.index_patcher.start()
        self.addCleanup(self.index_patcher.stop)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── 环境辅助 ──
    def init_git(self):
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "Wiki Service Test"],
            ["git", "config", "user.email", "wiki-service@test.local"],
            ["git", "config", "core.autocrlf", "false"],
            ["git", "config", "core.quotepath", "false"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(command, cwd=self.vault, check=True)

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.vault,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _index_stub(self, *args, **kwargs):
        page = args[0]
        return {
            "page_id": page.page_id,
            "revision_id": page.current_revision_id or "",
            "table": self.config.page_index_table,
            "row_count": 1,
            "text_index": "stub",
            "vector_index": "stub",
        }

    def make_page(
        self,
        title="页面",
        *,
        page_type="concept",
        status="draft",
        slug=None,
        body="",
        tags=None,
        aliases=None,
        commit=False,
    ):
        return self.store.create_page(
            PageDraft(
                title=title,
                page_type=page_type,
                summary=f"{title} 的摘要。",
                body=body,
                slug=slug,
                tags=list(tags or []),
                aliases=list(aliases or []),
                status=status,
            ),
            actor="test",
            commit=commit,
        )

    def page_path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def read_text(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def seed_source(
        self,
        *,
        document_id="doc-1",
        revision_id="rev-1",
        chunk_id="chunk-1",
        text=CHUNK_TEXT,
        source_value="paper.pdf",
        chunk_index=0,
        category="paper",
    ):
        now = "2026-01-01T00:00:00.000Z"
        self.conn.execute(
            "INSERT OR IGNORE INTO documents "
            "(document_id, source_type, canonical_uri, display_name, category, "
            " status, created_at, updated_at) "
            "VALUES (?, 'file', ?, 'paper.pdf', ?, 'active', ?, ?)",
            (document_id, f"file:///{source_value}", category or "", now, now),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO document_revisions "
            "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
            " processing_fingerprint, status, created_at) "
            "VALUES (?, ?, 1, 'raw', 'parsed', ?, 'indexed', ?)",
            (revision_id, document_id, f"fp-{revision_id}", now),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO chunk_refs "
            "(chunk_id, document_id, document_revision_id, chunk_index, "
            " source_value, text_sha256, char_start, char_end, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                chunk_id,
                document_id,
                revision_id,
                chunk_index,
                source_value,
                normalize_text_sha256(text),
                len(text),
                now,
            ),
        )
        return {"document_id": document_id, "chunk_id": chunk_id, "text": text}

    def base_extraction(self, **overrides):
        payload = {
            "analysis": "资料说明了城市洪涝与 SWMM 的关系。",
            "source_document_id": "doc-1",
            "source_chunk_ids": ["chunk-1"],
            "entities": [],
            "claims": [],
            "relations": [],
            "chunk_texts": {"chunk-1": CHUNK_TEXT},
        }
        payload.update(overrides)
        return payload


class TestStatusAndRead(WikiServiceTestBase):
    def test_status_reports_config_counts_and_journal(self):
        service.create_page(
            "状态页面",
            "concept",
            "正文",
            slug="status-page",
            config=self.config,
            conn=self.conn,
        )
        jobs_repo.create_journal(
            self.conn,
            state="index_pending",
            target_path="wiki-维基/concepts-概念/status-page.md",
            detail={"test": True},
        )
        result = service.status(config=self.config, conn=self.conn)

        self.assertTrue(result["ok"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["config"]["vault_path"], self.vault)
        self.assertEqual(result["config"]["page_index_table"], "wiki_pages")
        self.assertEqual(
            set(result["config"]),
            {
                "vault_path",
                "page_root",
                "db_path",
                "page_index_table",
                "auto_publish",
                "require_evidence",
                "min_entity_confidence",
                "min_relation_confidence",
                "max_auto_page_updates",
                "db_busy_timeout_ms",
                "git_commit",
            },
        )
        self.assertIn("0001", result["db"]["schema_versions"])
        self.assertEqual(
            result["db"]["table_counts"]["pages"], 1
        )
        self.assertEqual(result["pages"]["total"], 1)
        self.assertEqual(result["pages"]["by_status"].get("draft"), 1)
        self.assertEqual(result["pages"]["by_type"].get("concept"), 1)
        self.assertEqual(result["journal"]["unconverged"], 1)
        self.assertEqual(result["journal"]["by_state"]["index_pending"], 1)
        self.assertEqual(result["page_index"]["table"], "wiki_pages")
        self.assertFalse(result["page_index"]["exists"])

    def test_list_pages_filters_and_cursor(self):
        published_one = self.make_page(
            "已发布一", status="published", slug="published-one"
        )
        published_two = self.make_page(
            "已发布二", page_type="entity", status="published", slug="published-two"
        )
        self.make_page("草稿", status="draft", slug="draft-one")

        first = service.list_pages(
            status="published", limit=1, config=self.config, conn=self.conn
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first["count"], 1)
        self.assertTrue(first["has_more"])
        self.assertTrue(first["next_cursor"])

        second = service.list_pages(
            status="published",
            limit=1,
            cursor=first["next_cursor"],
            config=self.config,
            conn=self.conn,
        )
        self.assertEqual(second["count"], 1)
        returned = {first["pages"][0]["page_id"], second["pages"][0]["page_id"]}
        self.assertEqual(
            returned, {published_one.page_id, published_two.page_id}
        )
        self.assertFalse(second["has_more"])
        self.assertEqual(second["next_cursor"], "")

        filtered = service.list_pages(
            page_type="entity",
            status="published",
            config=self.config,
            conn=self.conn,
        )
        self.assertEqual(
            [item["page_id"] for item in filtered["pages"]],
            [published_two.page_id],
        )

        everything = service.list_pages(
            status="", config=self.config, conn=self.conn
        )
        self.assertEqual(everything["count"], 3)

        with self.assertRaises(service.WikiServiceError):
            service.list_pages(
                status="bogus", config=self.config, conn=self.conn
            )
        with self.assertRaises(service.WikiServiceError):
            service.list_pages(
                cursor="not-a-cursor", config=self.config, conn=self.conn
            )

    def test_get_page_includes_links_backlinks_and_evidence(self):
        target = self.make_page(
            "目标页", status="published", slug="target-page"
        )
        source = self.make_page(
            "来源页",
            status="published",
            slug="source-page",
            body="参见 [[target-page]]。\n",
        )
        self.seed_source()
        claim_id = "claim-1"
        self.conn.execute(
            "INSERT INTO claims (claim_id, page_id, claim_text, claim_type) "
            "VALUES (?, ?, '城市洪涝与排水能力有关。', 'fact')",
            (claim_id, source.page_id),
        )
        wiki_evidence.add_claim_evidence(
            self.conn,
            claim_id,
            "chunk-1",
            "城市洪涝受降雨与排水能力共同影响",
            chunk_text=CHUNK_TEXT,
        )

        view = service.get_page(
            source.slug, config=self.config, conn=self.conn
        )
        self.assertTrue(view["ok"])
        self.assertEqual(view["page"]["page_id"], source.page_id)
        self.assertEqual(view["body"], "参见 [[target-page]]。\n")
        self.assertEqual(len(view["links"]), 1)
        self.assertEqual(view["links"][0]["target_slug"], "target-page")
        self.assertEqual(view["links"][0]["target_page_id"], target.page_id)
        self.assertEqual(view["links"][0]["target_title"], "目标页")
        self.assertEqual(view["evidence"]["claim_count"], 1)
        self.assertEqual(view["evidence"]["evidence_count"], 1)
        self.assertTrue(view["evidence"]["claims"][0]["evidence"][0]["verified"])

        target_view = service.get_page(
            target.page_id, config=self.config, conn=self.conn
        )
        self.assertEqual(len(target_view["backlinks"]), 1)
        self.assertEqual(
            target_view["backlinks"][0]["source_slug"], "source-page"
        )

        without_extras = service.get_page(
            source.slug,
            include_sources=False,
            include_backlinks=False,
            config=self.config,
            conn=self.conn,
        )
        self.assertIsNone(without_extras["evidence"])
        self.assertEqual(target_view["backlinks"][0]["source_title"], "来源页")
        target_without = service.get_page(
            target.slug,
            include_backlinks=False,
            config=self.config,
            conn=self.conn,
        )
        self.assertEqual(target_without["backlinks"], [])

        with self.assertRaises(service.WikiServiceError):
            service.get_page("missing-page", config=self.config, conn=self.conn)


class TestCreateAndReview(WikiServiceTestBase):
    def test_create_page_only_draft(self):
        result = service.create_page(
            "草稿页面",
            "concept",
            "# 标题\n\n正文内容。\n",
            slug="draft-page",
            aliases=["别名一", "别名二"],
            tags="tag1, tag2",
            summary="",
            sources="paper.pdf",
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["slug"], "draft-page")
        self.assertIn("wiki-维基/concepts-概念/draft-page.md", result["path"])
        self.assertTrue(os.path.isfile(os.path.join(self.vault, *result["path"].split("/"))))
        content = self.read_text(
            os.path.join(self.vault, *result["path"].split("/"))
        )
        self.assertIn("status: draft", content)
        self.assertIn("aliases:", content)
        self.assertEqual(result["git_commit"], "")

        page = service.get_page(
            "draft-page", config=self.config, conn=self.conn
        )
        self.assertEqual(page["page"]["status"], "draft")
        self.assertEqual(page["sources"], ["paper.pdf"])

        with self.assertRaises(service.WikiServiceError):
            service.create_page(
                "非法类型", "source", "正文", config=self.config, conn=self.conn
            )
        with self.assertRaises(service.WikiServiceError):
            service.create_page(
                "", "concept", "正文", config=self.config, conn=self.conn
            )

    def test_propose_update_review_queue_apply_approve(self):
        page = self.make_page(
            "提案页面", status="published", slug="proposal-page", body="旧内容\n"
        )
        proposal = service.propose_update(
            page.page_id,
            "# 新内容\n",
            "补充新内容",
            page.current_revision_id,
            evidence_refs=[{"chunk_id": "chunk-1"}],
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(proposal["ok"])
        self.assertEqual(proposal["status"], "pending")
        self.assertIn("+# 新内容", proposal["diff"])
        self.assertTrue(os.path.isfile(proposal["draft_path"]))
        self.assertTrue(os.path.isfile(proposal["diff_path"]))

        detail = service.get_proposal(
            proposal["proposal_id"], config=self.config, conn=self.conn
        )
        self.assertEqual(detail["target_page"]["page_id"], page.page_id)
        self.assertEqual(detail["draft"], "# 新内容\n")
        self.assertEqual(
            detail["payload"]["evidence_refs"], [{"chunk_id": "chunk-1"}]
        )

        queue = service.review_queue(config=self.config, conn=self.conn)
        self.assertEqual(queue["count"], 1)
        item = queue["proposals"][0]
        self.assertEqual(item["proposal_id"], proposal["proposal_id"])
        self.assertEqual(item["page_slug"], "proposal-page")

        applied = service.apply_proposal(
            proposal["proposal_id"],
            "approve",
            "LGTM",
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["page_id"], page.page_id)
        self.assertTrue(applied["git_commit"])
        self.assertEqual(applied["index_error"], "")
        self.assertIsNotNone(applied["page_index"])
        self.assertEqual(self.index_mock.call_count, 1)

        view = self.store.read_page(page.page_id)
        self.assertEqual(view.body, "# 新内容\n")
        self.assertEqual(str(view.page.status), "published")
        self.assertEqual(str(self.store.read_page(page.page_id).page.status), "published")

        index_path = os.path.join(self.vault, "wiki-维基", "index.md")
        log_path = os.path.join(self.vault, "wiki-维基", "log.md")
        overview_path = os.path.join(self.vault, "wiki-维基", "overview.md")
        self.assertTrue(os.path.isfile(index_path))
        self.assertTrue(os.path.isfile(log_path))
        self.assertTrue(os.path.isfile(overview_path))
        self.assertIn("proposal=", self.read_text(log_path))

        subjects = self.git("log", "--pretty=%s").stdout
        self.assertIn("wiki: update", subjects)
        self.assertIn("wiki: refresh navigation", subjects)

        updated = service.get_proposal(
            proposal["proposal_id"], config=self.config, conn=self.conn
        )
        self.assertEqual(updated["proposal"]["status"], "applied")
        self.assertEqual(
            updated["proposal"]["result_revision_id"], applied["revision_id"]
        )
        self.assertEqual(
            service.review_queue(config=self.config, conn=self.conn)["count"], 0
        )

    def test_apply_full_document_publishes_draft(self):
        from kb.wiki.markdown import render_frontmatter

        page = self.make_page(
            "待发布页面",
            status="draft",
            slug="publish-page",
            body="草稿正文\n",
        )
        view = self.store.read_page(page.page_id)
        frontmatter = dict(view.frontmatter)
        frontmatter["status"] = "published"

        proposal = service.propose_update(
            page.page_id,
            render_frontmatter(frontmatter, "# 发布正文\n"),
            "审核通过后发布",
            page.current_revision_id,
            config=self.config,
            conn=self.conn,
        )
        applied = service.apply_proposal(
            proposal["proposal_id"],
            "approve",
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["page_status"], "published")
        updated = self.store.read_page(page.page_id)
        self.assertEqual(str(updated.page.status), "published")
        self.assertEqual(updated.body, "# 发布正文\n")
        self.assertEqual(self.index_mock.call_count, 1)

    def test_apply_body_only_update_publishes_draft(self):
        page = self.make_page(
            "正文型提案页面",
            status="draft",
            slug="body-only-publish",
            body="草稿正文\n",
        )
        proposal = service.propose_update(
            page.page_id,
            "# 审核后的正文\n",
            "正文型提案，不带 frontmatter",
            page.current_revision_id,
            config=self.config,
            conn=self.conn,
        )
        applied = service.apply_proposal(
            proposal["proposal_id"],
            "approve",
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["page_status"], "published")
        updated = self.store.read_page(page.page_id)
        self.assertEqual(str(updated.page.status), "published")
        self.assertEqual(updated.body, "# 审核后的正文\n")
        self.assertEqual(self.index_mock.call_count, 1)

    def test_apply_reject_does_not_change_page(self):
        page = self.make_page(
            "拒绝页面",
            status="published",
            slug="reject-page",
            body="保持内容\n",
            commit=True,
        )
        head_before = self.git("rev-parse", "HEAD").stdout.strip()
        path = self.page_path(page)
        before = self.read_text(path)
        revisions_before = self.conn.execute(
            "SELECT COUNT(*) FROM page_revisions"
        ).fetchone()[0]
        proposal = service.propose_update(
            page.page_id,
            "不应写入\n",
            "理由不足",
            page.current_revision_id,
            config=self.config,
            conn=self.conn,
        )

        rejected = service.apply_proposal(
            proposal["proposal_id"],
            "reject",
            "理由不足",
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(rejected["ok"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(rejected["applied"])
        self.assertEqual(self.read_text(path), before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM page_revisions").fetchone()[0],
            revisions_before,
        )
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_before)
        self.assertEqual(self.index_mock.call_count, 0)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "wiki-维基", "log.md")))

    def test_stale_expected_revision_marks_stale(self):
        page = self.make_page(
            "过期页面", status="published", slug="stale-page", body="旧内容\n"
        )
        prefix = page.current_revision_id[:8]
        stale_revision = ("0" * 26) if prefix != "0" * 8 else ("1" * 26)
        proposal = service.propose_update(
            page.page_id,
            "不应写入\n",
            "过期提案",
            stale_revision,
            config=self.config,
            conn=self.conn,
        )
        result = service.apply_proposal(
            proposal["proposal_id"],
            "approve",
            config=self.config,
            conn=self.conn,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "stale")
        self.assertIn("过期", result["error"])
        self.assertEqual(self.store.read_page(page.page_id).body, "旧内容\n")
        updated = service.get_proposal(
            proposal["proposal_id"], config=self.config, conn=self.conn
        )
        self.assertEqual(updated["proposal"]["status"], "stale")

    def test_apply_index_failure_records_warning_and_journal(self):
        self.index_mock.side_effect = RuntimeError("index boom")
        page = self.make_page(
            "索引失败页面",
            status="published",
            slug="index-fail-page",
            body="旧内容\n",
        )
        proposal = service.propose_update(
            page.page_id,
            "新内容\n",
            "测试索引失败",
            page.current_revision_id,
            config=self.config,
            conn=self.conn,
        )
        result = service.apply_proposal(
            proposal["proposal_id"],
            "approve",
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "applied")
        self.assertIsNone(result["page_index"])
        self.assertIn("index boom", result["index_error"])
        self.assertTrue(any("页面索引失败" in item for item in result["warnings"]))
        self.assertEqual(
            self.store.read_page(page.page_id).body, "新内容\n"
        )
        journal_rows = self.conn.execute(
            "SELECT proposal_id, page_id, state, error_message FROM write_journal "
            "WHERE state = 'index_pending'"
        ).fetchall()
        self.assertEqual(len(journal_rows), 1)
        self.assertEqual(journal_rows[0]["proposal_id"], proposal["proposal_id"])
        self.assertEqual(journal_rows[0]["page_id"], page.page_id)
        self.assertIn("index boom", journal_rows[0]["error_message"])

    def test_apply_invalid_decision(self):
        result = service.apply_proposal(
            "whatever", "maybe", config=self.config, conn=self.conn
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_decision")

    def test_apply_missing_proposal_raises(self):
        with self.assertRaises(service.WikiServiceError):
            service.apply_proposal(
                "missing-proposal", config=self.config, conn=self.conn
            )


class TestExtraction(WikiServiceTestBase):
    def test_submit_extraction_persists_dedups_and_proposes(self):
        self.seed_source()
        payload = self.base_extraction(
            entities=[
                {
                    "name": "Urban Flood",
                    "entity_type": "concept",
                    "aliases": ["内涝"],
                    "description": "城市内涝",
                    "confidence": 0.95,
                },
                {
                    "name": "urban-flood",
                    "entity_type": "concept",
                    "confidence": 0.9,
                },
                {
                    "name": "SWMM",
                    "entity_type": "tool",
                    "confidence": 0.95,
                },
            ],
            claims=[
                {
                    "claim_text": "城市洪涝受降雨与排水能力共同影响。",
                    "claim_type": "factual",
                    "evidence": [
                        {
                            "chunk_id": "chunk-1",
                            "quote": "城市洪涝受降雨与排水能力共同影响",
                            "page_number": 3,
                            "section_path": "1.1",
                        }
                    ],
                    "confidence": 0.9,
                },
                {
                    "claim_text": "SWMM 可能适合该场景。",
                    "claim_type": "hypothesis",
                    "confidence": 0.6,
                },
            ],
            relations=[
                {
                    "subject": "Urban Flood",
                    "predicate": "uses",
                    "object": "SWMM",
                    "evidence_ids": ["chunk-1"],
                    "confidence": 0.9,
                }
            ],
        )
        result = service.submit_extraction(
            payload, config=self.config, conn=self.conn
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]["entities"]), 3)
        self.assertEqual(len(result["created"]["claims"]), 2)
        self.assertEqual(len(result["created"]["relations"]), 1)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["warnings"], [])

        urban_entries = [
            item
            for item in result["created"]["entities"]
            if item["normalized_name"] == "urbanflood"
        ]
        self.assertEqual(len(urban_entries), 2)
        self.assertEqual(
            {item["entity_id"] for item in urban_entries},
            {urban_entries[0]["entity_id"]},
        )
        self.assertEqual(
            sorted(item["created"] for item in urban_entries), [False, True]
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 2
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 2
        )
        evidence_row = self.conn.execute(
            "SELECT * FROM claim_evidence"
        ).fetchone()
        self.assertIsNotNone(evidence_row)
        self.assertEqual(evidence_row["chunk_id"], "chunk-1")
        relation_row = self.conn.execute(
            "SELECT * FROM relations"
        ).fetchone()
        self.assertEqual(relation_row["predicate"], "uses")
        self.assertEqual(relation_row["status"], "proposed")
        self.assertEqual(relation_row["confidence"], 0.9)

    def test_submit_extraction_rejections(self):
        self.seed_source()
        payload = self.base_extraction(
            entities=[
                {"name": "低置信实体", "confidence": 0.1},
                {"name": "Alpha", "confidence": 0.9},
                {"name": "Beta", "confidence": 0.9},
            ],
            claims=[
                {
                    "claim_text": "无证据事实。",
                    "claim_type": "result",
                    "confidence": 0.9,
                },
                {
                    "claim_text": "引文对不上。",
                    "claim_type": "factual",
                    "evidence": [
                        {"chunk_id": "chunk-1", "quote": "这句话不在原文里"}
                    ],
                    "confidence": 0.9,
                },
                {
                    "claim_text": "可以没有证据的假设。",
                    "claim_type": "interpretation",
                    "confidence": 0.5,
                },
            ],
            relations=[
                {
                    "subject": "Alpha",
                    "predicate": "not_a_predicate",
                    "object": "Beta",
                    "confidence": 0.9,
                },
                {
                    "subject": "Ghost",
                    "predicate": "uses",
                    "object": "Beta",
                    "confidence": 0.9,
                },
                {
                    "subject": "Alpha",
                    "predicate": "uses",
                    "object": "Beta",
                    "confidence": 0.1,
                },
            ],
        )
        result = service.submit_extraction(
            payload, config=self.config, conn=self.conn
        )
        self.assertTrue(result["ok"])
        reasons = [item["reason"] for item in result["rejected"]]
        self.assertIn("below_confidence_threshold", reasons)
        self.assertIn("missing_evidence", reasons)
        self.assertIn("not_found", reasons)
        self.assertIn("invalid_predicate", reasons)
        self.assertIn("unresolved_entity", reasons)
        self.assertEqual(len(result["created"]["claims"]), 1)
        self.assertEqual(result["created"]["relations"], [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE canonical_name = '低置信实体'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1
        )
        low_relation = [
            item
            for item in result["rejected"]
            if item["kind"] == "relation"
            and item["reason"] == "below_confidence_threshold"
        ]
        self.assertEqual(len(low_relation), 1)
        self.assertEqual(len(low_relation[0]["candidates"]), 2)

    def test_submit_extraction_transaction_rollback_on_alias_conflict(self):
        payload = self.base_extraction(
            entities=[
                {"name": "Alpha", "confidence": 0.9},
                {"name": "Beta", "aliases": ["Alpha"], "confidence": 0.9},
            ]
        )
        with self.assertRaises(service.WikiServiceError):
            service.submit_extraction(
                payload, config=self.config, conn=self.conn
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0
        )

    def test_submit_extraction_source_document_missing(self):
        payload = self.base_extraction(source_document_id="missing-doc")
        with self.assertRaises(service.WikiServiceError):
            service.submit_extraction(
                payload, config=self.config, conn=self.conn
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0
        )

    def test_submit_extraction_page_suggestion(self):
        self.seed_source()
        page = service.create_page(
            "建议页面",
            "topic",
            "旧正文\n",
            slug="suggestion-page",
            config=self.config,
            conn=self.conn,
        )
        before = self.store.read_page(page["page_id"]).body
        payload = self.base_extraction(
            page_suggestion={
                "action": "update",
                "page_id": page["page_id"],
                "content_md": "# 新正文\n",
                "rationale": "抽取生成",
            }
        )
        original_create = service.ProposalService.create_proposal
        observed_transaction_state = []

        def tracking_create(proposal_service, *args, **kwargs):
            observed_transaction_state.append(proposal_service.conn.in_transaction)
            return original_create(proposal_service, *args, **kwargs)

        with mock.patch.object(
            service.ProposalService, "create_proposal", new=tracking_create
        ):
            result = service.submit_extraction(
                payload, config=self.config, conn=self.conn
            )
        self.assertEqual(observed_transaction_state, [True])
        self.assertIsNotNone(result["proposal"])
        self.assertEqual(result["proposal"]["page_id"], page["page_id"])
        self.assertEqual(
            self.store.read_page(page["page_id"]).body, before
        )
        queue = service.review_queue(config=self.config, conn=self.conn)
        self.assertEqual(queue["count"], 1)

        create_suggestion = self.base_extraction(
            page_suggestion={
                "action": "create",
                "title": "新页面",
                "content_md": "正文",
            }
        )
        create_result = service.submit_extraction(
            create_suggestion, config=self.config, conn=self.conn
        )
        self.assertIsNone(create_result["proposal"])
        self.assertTrue(
            any("不会自动建页" in item for item in create_result["warnings"])
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1
        )

    def test_submit_extraction_invalid_payload(self):
        with self.assertRaises(service.WikiServiceError):
            service.submit_extraction(
                {"analysis": "", "entities": []},
                config=self.config,
                conn=self.conn,
            )
        with self.assertRaises(service.WikiServiceError):
            service.submit_extraction(
                {
                    "analysis": "分析",
                    "claims": [{"claim_text": "x", "claim_type": "bogus"}],
                },
                config=self.config,
                conn=self.conn,
            )


class TestEvidenceRelatedValidateSearch(WikiServiceTestBase):
    def test_get_evidence_claim_and_chunk(self):
        self.seed_source()
        self.conn.execute(
            "INSERT INTO claims (claim_id, document_id, claim_text, claim_type) "
            "VALUES ('claim-1', 'doc-1', '事实断言', 'fact')"
        )
        wiki_evidence.add_claim_evidence(
            self.conn,
            "claim-1",
            "chunk-1",
            "城市洪涝受降雨与排水能力共同影响",
            chunk_text=CHUNK_TEXT,
        )

        by_claim = service.get_evidence(
            claim_id="claim-1", config=self.config, conn=self.conn
        )
        self.assertTrue(by_claim["ok"])
        self.assertEqual(by_claim["count"], 1)
        item = by_claim["evidence"][0]
        self.assertEqual(item["chunk_ref"]["chunk_id"], "chunk-1")
        self.assertEqual(item["document"]["document_id"], "doc-1")
        self.assertEqual(item["document_revision"]["revision_id"], "rev-1")

        by_chunk = service.get_evidence(
            chunk_id="chunk-1", config=self.config, conn=self.conn
        )
        self.assertEqual(by_chunk["bundle"]["chunk_id"], "chunk-1")
        self.assertEqual(by_chunk["bundle"]["document_id"], "doc-1")
        self.assertIn("城市洪涝", by_chunk["bundle"]["quote"])

        with self.assertRaises(service.WikiServiceError):
            service.get_evidence(config=self.config, conn=self.conn)
        with self.assertRaises(service.WikiServiceError):
            service.get_evidence(
                claim_id="missing", config=self.config, conn=self.conn
            )

    def test_get_related_pages_and_entities(self):
        target = self.make_page(
            "邻域目标", status="published", slug="related-target"
        )
        source = self.make_page(
            "邻域来源",
            status="published",
            slug="related-source",
            body="[[related-target]]\n",
        )
        alpha = entity_resolver.upsert_entity(
            self.conn, "Alpha", entity_type="concept", confidence=0.95
        )
        beta = entity_resolver.upsert_entity(
            self.conn, "Beta", entity_type="concept", confidence=0.95
        )
        entities_repo.add_page_entity(
            self.conn,
            PageEntityRecord(
                page_id=source.page_id,
                entity_id=alpha.entity_id,
                role="mention",
                confidence=0.9,
                created_at=entities_repo.utc_now(),
            ),
        )
        relations_repo.create_relation(
            self.conn,
            alpha.entity_id,
            "uses",
            beta.entity_id,
            confidence=0.9,
            status="proposed",
        )

        result = service.get_related(
            page_id=source.page_id, depth=1, config=self.config, conn=self.conn
        )
        self.assertTrue(result["ok"])
        page_ids = {item["page_id"] for item in result["pages"]}
        self.assertIn(target.page_id, page_ids)
        self.assertTrue(
            any(item["kind"] == "outgoing_link" for item in result["pages"])
        )
        self.assertEqual(
            [item["entity_id"] for item in result["entities"]],
            [beta.entity_id],
        )
        self.assertEqual(result["entities"][0]["predicate"], "uses")

        by_entity = service.get_related(
            entity_id=alpha.entity_id,
            depth=2,
            config=self.config,
            conn=self.conn,
        )
        self.assertEqual(
            [item["entity_id"] for item in by_entity["entities"]],
            [beta.entity_id],
        )

        with self.assertRaises(service.WikiServiceError):
            service.get_related(config=self.config, conn=self.conn)
        with self.assertRaises(service.WikiServiceError):
            service.get_related(
                page_id="missing-page", config=self.config, conn=self.conn
            )

    def test_validate_groups_issues_and_journal(self):
        page = self.make_page(
            "待校验页面", status="published", slug="validate-page"
        )
        os.remove(self.page_path(page))
        jobs_repo.create_journal(
            self.conn,
            state="index_pending",
            target_path=page.relative_path,
            detail={"test": True},
        )

        result = service.validate(config=self.config, conn=self.conn)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["summary"]["error"], 1)
        self.assertIn("page_file_missing", result["summary"]["codes"])
        self.assertIn(
            "page_file_missing", result["groups"]["error"]
        )
        self.assertEqual(result["journal"]["unconverged"], 1)
        self.assertEqual(result["journal"]["by_state"]["index_pending"], 1)

        pages_only = service.validate(
            scope="pages", config=self.config, conn=self.conn
        )
        self.assertTrue(
            all(issue["code"].startswith(("page_", "frontmatter_"))
                or issue["code"] == "orphan_page_file"
                for issue in pages_only["issues"])
        )
        journal_only = service.validate(
            scope="journal", config=self.config, conn=self.conn
        )
        self.assertEqual(journal_only["issues"], [])
        self.assertEqual(journal_only["journal"]["unconverged"], 1)

        with self.assertRaises(service.WikiServiceError):
            service.validate(scope="bogus", config=self.config, conn=self.conn)

    def test_search_delegates_and_validates_mode(self):
        expected = {
            "query": "q",
            "mode": "page",
            "pages": [],
            "evidence": [],
            "related": [],
            "warnings": [],
        }
        with mock.patch(
            "kb.wiki.service.wiki_search.search_wiki", return_value=expected
        ) as search_mock:
            result = service.search(
                "q",
                mode="page",
                tags="a,b",
                config=self.config,
                conn=self.conn,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["query"], "q")
        kwargs = search_mock.call_args.kwargs
        self.assertEqual(kwargs["tags"], ["a", "b"])
        self.assertEqual(kwargs["mode"], "page")
        self.assertIsNotNone(kwargs["conn"])

        with self.assertRaises(service.WikiServiceError):
            service.search(
                "q", mode="bogus", config=self.config, conn=self.conn
            )

    def test_search_evidence_marks_score_source(self):
        with mock.patch(
            "kb.wiki.service.kb_search.search_structured",
            return_value={"results": [{"text": "x"}], "trace": {"mode": "hybrid"}},
        ) as search_mock:
            result = service.search_evidence(
                "城市洪涝", config=self.config
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["score_source"], "rag")
        self.assertEqual(result["results"], [{"text": "x"}])
        self.assertEqual(search_mock.call_args.args[0], "城市洪涝")

        with self.assertRaises(service.WikiServiceError):
            service.search_evidence("", config=self.config)


class WikiServiceLanceTestBase(WikiServiceTestBase):
    """带真实临时 LanceDB 的 service 测试（fake embedding，不访问真实库）。"""

    def setUp(self):
        super().setUp()
        activate_fake_embedding()
        kb_schema.reset_db()
        self.addCleanup(kb_schema.reset_db)

    def seed_lance(self, text, source="paper.pdf", index=0, doc_id=""):
        table = kb_schema.get_or_create_table()
        kb_ingest.add_chunks(
            table,
            [
                {
                    "text": text,
                    "source": source,
                    "chunk_index": index,
                    "category": "paper",
                    "doc_id": doc_id,
                    "ingested_at": "2026-11-15T00:00:00Z",
                }
            ],
        )


class TestExtractionEvidenceBinding(WikiServiceLanceTestBase):
    def test_library_text_wins_over_forged_chunk_texts(self):
        self.seed_lance(CHUNK_TEXT)
        self.seed_source(text=CHUNK_TEXT)
        forged = "伪造原文：这句话只存在于调用方提供的 chunk_texts。"
        forged_claim = {
            "claim_text": "伪造引文不应落库。",
            "claim_type": "factual",
            "evidence": [{"chunk_id": "chunk-1", "quote": "伪造原文"}],
            "confidence": 0.9,
        }

        rejected = service.submit_extraction(
            self.base_extraction(
                claims=[forged_claim], chunk_texts={"chunk-1": forged}
            ),
            config=self.config,
            conn=self.conn,
        )

        self.assertEqual(rejected["created"]["claims"], [])
        self.assertIn("not_found", [item["reason"] for item in rejected["rejected"]])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0
        )

        accepted = service.submit_extraction(
            self.base_extraction(
                claims=[
                    {
                        "claim_text": "城市洪涝受降雨与排水能力共同影响。",
                        "claim_type": "factual",
                        "evidence": [
                            {
                                "chunk_id": "chunk-1",
                                "quote": "城市洪涝受降雨与排水能力共同影响",
                            }
                        ],
                        "confidence": 0.9,
                    }
                ],
                chunk_texts={"chunk-1": forged},
            ),
            config=self.config,
            conn=self.conn,
        )
        self.assertEqual(len(accepted["created"]["claims"]), 1)
        self.assertEqual(
            accepted["created"]["claims"][0]["verification"], "verified"
        )

    def test_missing_chunk_in_available_library_rejects_caller_text(self):
        self.seed_lance(CHUNK_TEXT)
        self.seed_source(text=CHUNK_TEXT, source_value="other.pdf")

        result = service.submit_extraction(
            self.base_extraction(
                claims=[
                    {
                        "claim_text": "库内查不到的切片。",
                        "claim_type": "factual",
                        "evidence": [
                            {"chunk_id": "chunk-1", "quote": "库内查不到"}
                        ],
                        "confidence": 0.9,
                    }
                ]
            ),
            config=self.config,
            conn=self.conn,
        )

        self.assertEqual(result["created"]["claims"], [])
        self.assertIn("not_found", [item["reason"] for item in result["rejected"]])

    def test_offline_verification_is_marked_unverified_in_get_page(self):
        self.seed_source()
        page = self.make_page(
            "离线证据页面", status="draft", slug="offline-evidence-page"
        )
        payload = self.base_extraction(
            claims=[
                {
                    "claim_text": "城市洪涝受降雨与排水能力共同影响。",
                    "claim_type": "factual",
                    "evidence": [
                        {
                            "chunk_id": "chunk-1",
                            "quote": "城市洪涝受降雨与排水能力共同影响",
                        }
                    ],
                    "confidence": 0.9,
                }
            ],
            page_suggestion={
                "action": "update",
                "page_id": page.page_id,
                "content_md": "# 页面正文\n",
                "rationale": "离线校验测试",
            },
        )

        with mock.patch.object(
            service.wiki_evidence,
            "resolve_chunk_text",
            side_effect=wiki_evidence.ChunkTextUnavailable("offline"),
        ), mock.patch.object(
            service, "_lancedb_available", return_value=False
        ):
            result = service.submit_extraction(
                payload, config=self.config, conn=self.conn
            )

        self.assertEqual(len(result["created"]["claims"]), 1)
        claim_info = result["created"]["claims"][0]
        self.assertEqual(claim_info["verification"], "offline_unverified")
        self.assertEqual(claim_info["page_id"], page.page_id)

        chunk_ref = self.conn.execute(
            "SELECT locator_json FROM chunk_refs WHERE chunk_id = 'chunk-1'"
        ).fetchone()
        self.assertIn("offline_unverified", chunk_ref["locator_json"])

        view = service.get_page(
            page.page_id, config=self.config, conn=self.conn
        )
        self.assertEqual(view["evidence"]["claim_count"], 1)
        evidence_item = view["evidence"]["claims"][0]["evidence"][0]
        self.assertFalse(evidence_item["verified"])
        self.assertEqual(
            evidence_item["verification"], "offline_unverified"
        )

        # 同一 chunk 后续在线校验的证据不应被旧离线标记连带标成未验证。
        self.seed_lance(CHUNK_TEXT)
        online = service.submit_extraction(
            self.base_extraction(
                claims=[
                    {
                        "claim_text": "在线校验的同一 chunk 证据。",
                        "claim_type": "factual",
                        "evidence": [
                            {
                                "chunk_id": "chunk-1",
                                "quote": "城市洪涝受降雨与排水能力共同影响",
                            }
                        ],
                        "confidence": 0.9,
                    }
                ],
                page_suggestion={
                    "action": "update",
                    "page_id": page.page_id,
                    "content_md": "# 页面正文\n",
                    "rationale": "在线校验测试",
                },
            ),
            config=self.config,
            conn=self.conn,
        )
        self.assertEqual(
            online["created"]["claims"][0]["verification"], "verified"
        )
        view = service.get_page(
            page.page_id, config=self.config, conn=self.conn
        )
        by_text = {
            claim["claim_text"]: claim for claim in view["evidence"]["claims"]
        }
        self.assertFalse(
            by_text["城市洪涝受降雨与排水能力共同影响。"]["evidence"][0][
                "verified"
            ]
        )
        self.assertTrue(
            by_text["在线校验的同一 chunk 证据。"]["evidence"][0]["verified"]
        )

    def test_page_suggestion_binds_claims_entities_and_relations(self):
        self.seed_lance(CHUNK_TEXT)
        self.seed_source(text=CHUNK_TEXT)
        page = self.make_page(
            "证据绑定页面", status="draft", slug="binding-page"
        )
        payload = self.base_extraction(
            entities=[
                {"name": "Alpha", "entity_type": "concept", "confidence": 0.9},
                {"name": "Beta", "entity_type": "concept", "confidence": 0.9},
            ],
            claims=[
                {
                    "claim_text": "城市洪涝受降雨与排水能力共同影响。",
                    "claim_type": "factual",
                    "evidence": [
                        {
                            "chunk_id": "chunk-1",
                            "quote": "城市洪涝受降雨与排水能力共同影响",
                        }
                    ],
                    "confidence": 0.9,
                }
            ],
            relations=[
                {
                    "subject": "Alpha",
                    "predicate": "uses",
                    "object": "Beta",
                    "confidence": 0.9,
                }
            ],
            page_suggestion={
                "action": "update",
                "page_id": page.page_id,
                "content_md": "# 页面正文\n",
                "rationale": "页面级证据绑定",
            },
        )

        result = service.submit_extraction(
            payload, config=self.config, conn=self.conn
        )

        self.assertEqual(len(result["created"]["claims"]), 1)
        self.assertEqual(result["created"]["claims"][0]["page_id"], page.page_id)
        self.assertEqual(
            result["created"]["relations"][0]["source_page_id"], page.page_id
        )
        claim_row = self.conn.execute("SELECT page_id FROM claims").fetchone()
        self.assertEqual(claim_row["page_id"], page.page_id)
        relation_row = self.conn.execute(
            "SELECT source_page_id FROM relations"
        ).fetchone()
        self.assertEqual(relation_row["source_page_id"], page.page_id)
        page_entities = entities_repo.get_page_entities(self.conn, page.page_id)
        self.assertEqual(len(page_entities), 2)

        view = service.get_page(
            page.page_id, config=self.config, conn=self.conn
        )
        self.assertEqual(view["evidence"]["claim_count"], 1)
        self.assertEqual(
            view["evidence"]["claims"][0]["page_id"], page.page_id
        )
        self.assertTrue(view["evidence"]["claims"][0]["evidence"][0]["verified"])

    def test_chunk_resolution_happens_before_write_transaction(self):
        self.seed_lance(CHUNK_TEXT)
        self.seed_source(text=CHUNK_TEXT)
        real_resolve = wiki_evidence.resolve_chunk_text
        observed: list[bool] = []

        def spy(conn, chunk_id, **kwargs):
            observed.append(bool(conn.in_transaction))
            return real_resolve(conn, chunk_id, **kwargs)

        payload = self.base_extraction(
            claims=[
                {
                    "claim_text": "城市洪涝受降雨与排水能力共同影响。",
                    "claim_type": "factual",
                    "evidence": [
                        {
                            "chunk_id": "chunk-1",
                            "quote": "城市洪涝受降雨与排水能力共同影响",
                        }
                    ],
                    "confidence": 0.9,
                }
            ]
        )
        with mock.patch.object(
            wiki_evidence, "resolve_chunk_text", side_effect=spy
        ):
            result = service.submit_extraction(
                payload, config=self.config, conn=self.conn
            )

        self.assertEqual(len(result["created"]["claims"]), 1)
        self.assertEqual(observed, [False])


if __name__ == "__main__":
    unittest.main()
