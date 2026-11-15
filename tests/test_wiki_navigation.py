# -*- coding: utf-8 -*-
"""导航层：purpose / index / log / overview / refresh_navigation。

所有用例都使用临时 vault、临时 SQLite，不接触真实知识库。
"""

import glob
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import navigation
from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.page_store import PageDraft, PageStore
from kb.wiki.repositories import pages as pages_repo


class WikiNavigationTestBase(unittest.TestCase):
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

    def make_page(
        self,
        title,
        *,
        page_type="concept",
        status="draft",
        slug=None,
        summary=None,
        body="",
        sources=None,
    ):
        values = {
            "title": title,
            "page_type": page_type,
            "summary": summary if summary is not None else f"{title} 的摘要。",
            "body": body,
            "slug": slug,
            "status": status,
        }
        if sources is not None:
            values["sources"] = sources
        return self.store.create_page(PageDraft(**values))

    def read_path(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def nav_text(self, name):
        return self.read_path(navigation.nav_paths(self.config)[name])

    def page_path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )

    def seed_evidence(self):
        """写入 1 个 document、1 个 revision、2 个 chunk、2 个 claim、2 条证据。"""
        self.conn.execute(
            "INSERT INTO documents "
            "(document_id, source_type, canonical_uri, display_name, category, status) "
            "VALUES ('doc-1', 'pdf', 'file:///doc-1.pdf', 'Doc 1', '', 'active')"
        )
        self.conn.execute(
            "INSERT INTO document_revisions "
            "(revision_id, document_id, revision_no, raw_sha256, parsed_sha256, "
            " processing_fingerprint) "
            "VALUES ('rev-1', 'doc-1', 1, 'raw', 'parsed', 'fingerprint')"
        )
        self.conn.execute(
            "INSERT INTO chunk_refs "
            "(chunk_id, document_id, document_revision_id, chunk_index, text_sha256) "
            "VALUES ('chunk-1', 'doc-1', 'rev-1', 0, 'chunk-sha-1')"
        )
        self.conn.execute(
            "INSERT INTO chunk_refs "
            "(chunk_id, document_id, document_revision_id, chunk_index, text_sha256) "
            "VALUES ('chunk-2', 'doc-1', 'rev-1', 1, 'chunk-sha-2')"
        )
        self.conn.execute(
            "INSERT INTO claims (claim_id, claim_text) "
            "VALUES ('claim-1', '第一项断言')"
        )
        self.conn.execute(
            "INSERT INTO claims (claim_id, claim_text) "
            "VALUES ('claim-2', '第二项断言')"
        )
        quote = "城市洪涝受降雨与排水能力共同影响。"
        quote_sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO claim_evidence "
            "(evidence_id, claim_id, chunk_id, quote, text_sha256, char_start, char_end) "
            "VALUES ('ev-1', 'claim-1', 'chunk-1', ?, ?, 0, 18)",
            (quote, quote_sha),
        )
        self.conn.execute(
            "INSERT INTO claim_evidence "
            "(evidence_id, claim_id, chunk_id, quote, text_sha256, char_start, char_end) "
            "VALUES ('ev-2', 'claim-2', 'chunk-2', '另一条证据', 'bad-sha', 0, 5)"
        )


class TestNavPaths(WikiNavigationTestBase):
    def test_nav_paths_are_absolute_and_under_page_root(self):
        paths = navigation.nav_paths(self.config)
        self.assertEqual(set(paths), {"index", "log", "purpose", "overview"})
        for key, filename in navigation.NAV_FILENAMES.items():
            self.assertTrue(os.path.isabs(paths[key]))
            self.assertEqual(os.path.dirname(paths[key]), self.config.page_root)
            self.assertEqual(os.path.basename(paths[key]), filename)
        self.assertEqual(
            paths["purpose"],
            os.path.join(self.config.page_root, "purpose.md"),
        )


class TestPurpose(WikiNavigationTestBase):
    def test_created_once_and_never_overwritten(self):
        path, created = navigation.ensure_purpose(self.config)
        self.assertTrue(created)
        self.assertEqual(path, navigation.nav_paths(self.config)["purpose"])
        self.assertTrue(os.path.isfile(path))

        content = self.read_path(path)
        for token in ("## 目标", "## 范围", "## 关键问题", "## 维护规则"):
            self.assertIn(token, content)

        modified = content + "\n人工补充：只记录可复用的结论。\n"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(modified)

        path_again, created_again = navigation.ensure_purpose(self.config)
        self.assertFalse(created_again)
        self.assertEqual(path_again, path)
        self.assertEqual(self.read_path(path), modified)


class TestIndex(WikiNavigationTestBase):
    def test_groups_sorts_and_links_published_pages(self):
        self.make_page(
            "Zeta 城市洪涝",
            page_type="concept",
            status="published",
            slug="zeta",
            summary="Zeta 的概念摘要。",
        )
        self.make_page(
            "Alpha 城市洪涝",
            page_type="concept",
            status="published",
            slug="alpha",
            summary="Alpha 的概念摘要。",
        )
        self.make_page(
            "Beta 实体",
            page_type="entity",
            status="published",
            slug="beta-entity",
            summary="Beta 的实体摘要。",
        )
        self.make_page(
            "Gamma 主题",
            page_type="topic",
            status="published",
            slug="gamma-topic",
            summary="Gamma 的主题摘要。",
        )
        self.make_page("Hidden Draft", status="draft", slug="hidden")

        path = navigation.rebuild_index(self.conn, self.config)
        self.assertEqual(path, navigation.nav_paths(self.config)["index"])
        text = self.read_path(path)

        self.assertTrue(text.startswith("# 知识索引\n"))
        self.assertIn("更新时间：", text)
        self.assertIn("## 概念（concept）", text)
        self.assertIn("## 实体（entity）", text)
        self.assertIn("## 主题（topic）", text)
        self.assertIn(
            "- [[alpha|Alpha 城市洪涝]] — Alpha 的概念摘要。", text
        )
        self.assertIn("- [[zeta|Zeta 城市洪涝]] — Zeta 的概念摘要。", text)
        self.assertIn("- [[beta-entity|Beta 实体]] — Beta 的实体摘要。", text)
        self.assertIn("- [[gamma-topic|Gamma 主题]] — Gamma 的主题摘要。", text)
        self.assertLess(text.index("[[alpha|"), text.index("[[zeta|"))
        self.assertLess(text.index("[[alpha|"), text.index("[[beta-entity|"))
        self.assertLess(text.index("[[beta-entity|"), text.index("[[gamma-topic|"))
        self.assertNotIn("Hidden Draft", text)

        self.assertEqual(
            glob.glob(os.path.join(self.config.page_root, ".*.tmp")), []
        )

    def test_empty_state(self):
        navigation.rebuild_index(self.conn, self.config)
        text = self.nav_text("index")
        self.assertIn(navigation.EMPTY_INDEX_TEXT, text)
        self.assertNotIn("[[", text)

    def test_missing_page_file_only_emits_link(self):
        page = self.make_page(
            "无文件页面",
            status="published",
            slug="no-file",
            summary="摘要存在于文件中。",
        )
        os.unlink(self.page_path(page))

        navigation.rebuild_index(self.conn, self.config)
        text = self.nav_text("index")
        self.assertIn("- [[no-file|无文件页面]]", text)
        self.assertNotIn("—", text)

    def test_atomic_write_keeps_previous_index_on_failure(self):
        self.make_page("保留旧索引", status="published", slug="keep-old")
        navigation.rebuild_index(self.conn, self.config)
        before = self.nav_text("index")

        with mock.patch(
            "kb.wiki.page_store.os.replace", side_effect=OSError("replace boom")
        ):
            with self.assertRaises(OSError):
                navigation.rebuild_index(self.conn, self.config)

        self.assertEqual(self.nav_text("index"), before)
        self.assertEqual(
            glob.glob(os.path.join(self.config.page_root, ".*.tmp")), []
        )


class TestLog(WikiNavigationTestBase):
    def test_header_once_then_append_in_order(self):
        path = navigation.append_log(
            self.config, "agent", "create", "page-1", "created"
        )
        self.assertEqual(path, navigation.nav_paths(self.config)["log"])
        lines = self.read_path(path).splitlines()
        self.assertEqual(lines[0], "# 操作日志")
        self.assertEqual(lines[1], "")
        self.assertEqual(len(lines), 3)
        self.assertRegex(
            lines[2],
            r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
            r" \| agent \| create \| page-1 \| created$",
        )

        navigation.append_log(
            self.config, "agent", "update", "page-1", "updated"
        )
        text = self.read_path(path)
        lines = text.splitlines()
        self.assertEqual(text.count("# 操作日志"), 1)
        self.assertEqual(len(lines), 4)
        self.assertIn("| create |", lines[2])
        self.assertIn("| update |", lines[3])
        self.assertLess(text.index("| create |"), text.index("| update |"))

    def test_newlines_and_pipes_are_escaped(self):
        navigation.append_log(
            self.config,
            "agent|x",
            "create\npage",
            "obj|1",
            "line1\nline2\r\nline3 | tail",
        )
        lines = self.read_path(
            navigation.nav_paths(self.config)["log"]
        ).splitlines()
        line = lines[-1]
        self.assertIn(
            "| agent\\|x | create page | obj\\|1 | line1 line2 line3 \\| tail",
            line,
        )
        self.assertNotIn("\n", line)


class TestOverview(WikiNavigationTestBase):
    def _seed_pages_with_times(self):
        pages = {
            "p1": self.make_page(
                "页面一", status="published", slug="p1", page_type="concept"
            ),
            "p2": self.make_page(
                "页面二", status="published", slug="p2", page_type="entity"
            ),
            "p3": self.make_page(
                "页面三", status="published", slug="p3", page_type="topic"
            ),
            "p4": self.make_page(
                "页面四", status="draft", slug="p4", page_type="concept"
            ),
            "p5": self.make_page(
                "页面五", status="draft", slug="p5", page_type="entity"
            ),
            "p6": self.make_page(
                "页面六", status="review", slug="p6", page_type="concept"
            ),
        }
        timestamps = {
            "p1": "2026-11-01T00:00:00.000Z",
            "p2": "2026-11-03T00:00:00.000Z",
            "p3": "2026-11-02T00:00:00.000Z",
            "p4": "2026-10-31T00:00:00.000Z",
            "p5": "2026-10-30T00:00:00.000Z",
            "p6": "2026-10-29T00:00:00.000Z",
        }
        for key, page in pages.items():
            pages_repo.update_page(
                self.conn, page.page_id, updated_at=timestamps[key]
            )
        return pages

    def test_counts_recent_order_and_navigation_links(self):
        self._seed_pages_with_times()
        self.seed_evidence()

        path = navigation.rebuild_overview(self.conn, self.config)
        self.assertEqual(path, navigation.nav_paths(self.config)["overview"])
        text = self.read_path(path)

        self.assertTrue(text.startswith("# 知识库概览\n"))
        self.assertIn("- 页面总数：6", text)
        self.assertIn("- 已发布：3", text)
        self.assertIn("- 草稿：2", text)
        self.assertIn("- 待审核：1", text)
        self.assertIn("- 文档：1", text)
        self.assertIn("- 切片引用：2", text)
        self.assertIn("- Claims：2", text)
        self.assertIn("- 证据：2", text)
        self.assertIn("- 已验证：1", text)
        self.assertIn("- 未验证：1", text)
        self.assertIn("claim_evidence 无 verification 列", text)
        self.assertIn("| 概念（concept） | 3 |", text)
        self.assertIn("| 实体（entity） | 2 |", text)
        self.assertIn("| 主题（topic） | 1 |", text)
        self.assertIn("- [[index]]", text)
        self.assertIn("- [[log]]", text)
        self.assertIn("- [[purpose]]", text)

        # updated_at desc：p2 > p3 > p1 > p4 > p5 > p6
        positions = [
            text.index(f"[[{slug}|") for slug in ("p2", "p3", "p1", "p4", "p5", "p6")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("[[p2|", text)
        self.assertIn("2026-11-03T00:00:00.000Z", text)

        self.assertEqual(
            glob.glob(os.path.join(self.config.page_root, ".*.tmp")), []
        )

    def test_recent_pages_limited_to_ten(self):
        for index in range(1, 13):
            page = self.make_page(
                f"Page {index:02d}",
                status="published",
                slug=f"p{index:02d}",
            )
            pages_repo.update_page(
                self.conn,
                page.page_id,
                updated_at=f"2026-11-{index:02d}T00:00:00.000Z",
            )

        navigation.rebuild_overview(self.conn, self.config)
        text = self.nav_text("overview")

        positions = [
            text.index(f"[[p{index:02d}|")
            for index in range(12, 2, -1)
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("[[p02|", text)
        self.assertNotIn("[[p01|", text)

    def test_uses_verification_column_when_present(self):
        self.seed_evidence()
        self.conn.execute(
            "ALTER TABLE claim_evidence "
            "ADD COLUMN verification TEXT NOT NULL DEFAULT ''"
        )
        self.conn.execute(
            "UPDATE claim_evidence SET verification = 'verified' "
            "WHERE evidence_id = 'ev-1'"
        )
        self.conn.execute(
            "UPDATE claim_evidence SET verification = 'not_found' "
            "WHERE evidence_id = 'ev-2'"
        )

        navigation.rebuild_overview(self.conn, self.config)
        text = self.nav_text("overview")
        self.assertIn("- 已验证：1", text)
        self.assertIn("- 未验证：1", text)
        self.assertIn("claim_evidence.verification = 'verified'", text)


class TestRefreshNavigation(WikiNavigationTestBase):
    def test_refresh_creates_purpose_index_and_overview(self):
        self.make_page("已发布页面", status="published", slug="published")

        result = navigation.refresh_navigation(self.conn, self.config)

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["purpose_created"])
        for key in ("purpose", "index", "overview"):
            self.assertTrue(os.path.isfile(result["paths"][key]))
        self.assertIn("[[published|已发布页面]]", self.nav_text("index"))

    def test_failure_in_one_step_collects_error_and_continues(self):
        with mock.patch.object(
            navigation,
            "rebuild_index",
            side_effect=RuntimeError("index boom"),
        ):
            result = navigation.refresh_navigation(self.conn, self.config)

        self.assertTrue(result["errors"])
        self.assertTrue(
            any("index" in error and "index boom" in error for error in result["errors"])
        )
        self.assertTrue(os.path.isfile(result["paths"]["purpose"]))
        self.assertTrue(os.path.isfile(result["paths"]["overview"]))
        self.assertFalse(os.path.isfile(result["paths"]["index"]))


if __name__ == "__main__":
    unittest.main()
