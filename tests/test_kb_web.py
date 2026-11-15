# -*- coding: utf-8 -*-
"""kb_web：网页抓取、vault 剪藏与幂等入库（mock HTTP，不联网）。"""

import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg  # noqa: E402
from kb import schema as kb_schema  # noqa: E402
from kb import vault_source  # noqa: E402
from kb import web as kb_web  # noqa: E402
from tests.kb_test_support import activate_fake_embedding, temp_db_env  # noqa: E402

HTML = """
<html><head><title>测试页面</title></head>
<body>
  <nav>导航</nav>
  <script>var x = 1;</script>
  <article><p>这是正文第一段，内容足够长。</p><p>这是第二段。</p></article>
  <footer>页脚</footer>
</body></html>
"""

CONTENT_A = "# 标题\n\n" + "甲" * 200 + "\n\n" + "乙" * 200
CONTENT_B = "# 标题\n\n" + "丙" * 200 + "\n\n" + "丁" * 200

WEB_REL_DIR = os.path.join("00-inbox-收件箱", "from-web-网页")


class FakeResponse:
    text = HTML

    def raise_for_status(self):
        pass

    @property
    def headers(self):
        return {"Content-Type": "text/html"}


class TestFetchPage(unittest.TestCase):
    def test_extracts_title_and_body(self):
        with mock.patch.object(kb_web.requests, "get", return_value=FakeResponse()):
            title, content = kb_web.fetch_page("https://example.com/docs")
        self.assertEqual(title, "测试页面")
        self.assertIn("正文第一段", content)
        self.assertNotIn("页脚", content)
        self.assertNotIn("var x = 1;", content)
        self.assertIn("https://example.com/docs", content)


class TestCanonicalizeUrl(unittest.TestCase):
    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            kb_web.canonicalize_url("HTTPS://Example.COM/Docs/Page"),
            "https://example.com/Docs/Page",
        )

    def test_strips_fragment_and_tracking_params(self):
        url = "https://example.com/a?utm_source=x&id=2&fbclid=y&gclid=z&q=3#section"
        self.assertEqual(kb_web.canonicalize_url(url), "https://example.com/a?id=2&q=3")

    def test_tracking_params_case_insensitive(self):
        url = "https://example.com/a?UTM_Source=x&FBCLID=y&keep=1"
        self.assertEqual(kb_web.canonicalize_url(url), "https://example.com/a?keep=1")

    def test_default_ports_removed(self):
        self.assertEqual(
            kb_web.canonicalize_url("http://Example.com:80/x/"),
            "http://example.com/x/",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com:443/x"),
            "https://example.com/x",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com:8443/x"),
            "https://example.com:8443/x",
        )

    def test_path_kept_as_is(self):
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/docs/"),
            "https://example.com/docs/",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/docs///"),
            "https://example.com/docs///",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/"),
            "https://example.com/",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com"),
            "https://example.com/",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/docs"),
            "https://example.com/docs",
        )

    def test_query_order_preserved(self):
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/?b=2&a=1&utm_medium=x"),
            "https://example.com/?b=2&a=1",
        )

    def test_userinfo_rejected(self):
        for bad in (
            "https://user:pass@example.com/x",
            "https://user@example.com/x",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                kb_web.canonicalize_url(bad)

    def test_invalid_urls_raise_value_error(self):
        for bad in ("ftp://example.com", "https://", "not a url",
                    "https://example.com:bad/x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                kb_web.canonicalize_url(bad)


class WebIngestTestBase(unittest.TestCase):
    """临时库 + 临时 vault + 假 embedding，验证真实入库链路。"""

    def setUp(self):
        activate_fake_embedding()
        cfg.FTS_BASE_TOKENIZER = "simple"
        self.db_path = temp_db_env(self)
        self.vault = os.path.join(self.db_path, "vault")

    def web_dir(self):
        return os.path.join(self.vault, WEB_REL_DIR)

    def snapshot_from_output(self, output: str) -> str:
        line = next(
            line for line in output.splitlines()
            if line.startswith("网页快照: ")
        )
        return line.split(": ", 1)[1].strip()

    def table_rows(self):
        table = kb_schema.get_or_create_table()
        arrow = table.to_lance().to_table(columns=["source", "text", "chunk_index"])
        return list(zip(
            arrow.column("source").to_pylist(),
            arrow.column("text").to_pylist(),
            arrow.column("chunk_index").to_pylist(),
        ))

    def ingest(self, url, title="测试页面", content=CONTENT_A):
        with mock.patch.object(kb_web, "fetch_page", return_value=(title, content)):
            return kb_web.ingest_url(url)


class TestIngestUrlRejects(WebIngestTestBase):
    def test_invalid_url_rejected(self):
        self.assertEqual(
            kb_web.ingest_url("ftp://example.com"),
            "❌ 无效的 URL: ftp://example.com",
        )

    def test_fetch_failure_does_not_write_vault_or_db(self):
        with mock.patch.object(kb_web, "fetch_page",
                               side_effect=ConnectionError("refused")):
            out = kb_web.ingest_url("https://example.com")
        self.assertIn("抓取失败", out)
        self.assertFalse(os.path.exists(self.web_dir()))
        self.assertEqual(kb_schema.row_count(), 0)

    def test_snapshot_failure_does_not_write_db(self):
        with mock.patch.object(kb_web, "fetch_page",
                               return_value=("测试页面", CONTENT_A)), \
                mock.patch.object(kb_web.vault_source, "save_web_markdown",
                                  side_effect=OSError("disk full")):
            out = kb_web.ingest_url("https://example.com/page")
        self.assertIn("网页快照写入失败", out)
        self.assertEqual(kb_schema.row_count(), 0)


class TestIngestUrlVaultSource(WebIngestTestBase):
    def test_snapshot_lands_in_web_inbox_and_source_is_vault_relative(self):
        original = "https://example.com/a?utm_source=feed&id=1"
        output = self.ingest(original)

        self.assertIn("网页已索引", output)
        self.assertIn("网页快照: ", output)
        snapshot_rel = self.snapshot_from_output(output)
        self.assertTrue(
            snapshot_rel.startswith(WEB_REL_DIR + os.sep),
            snapshot_rel,
        )
        snapshot_path = os.path.join(self.vault, snapshot_rel)
        self.assertTrue(os.path.isfile(snapshot_path))

        canonical = kb_web.canonicalize_url(original)
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]
        self.assertTrue(os.path.basename(snapshot_path).endswith(f"-{digest}.md"))
        self.assertTrue(os.path.basename(snapshot_path).startswith("测试页面-"))

        expected_source = vault_source.rel_to_vault(snapshot_path)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {expected_source},
        )
        self.assertIn(f"文件: {expected_source}", output)
        self.assertIn("新增记录:", output)

        with open(snapshot_path, "r", encoding="utf-8") as handle:
            markdown = handle.read()
        self.assertTrue(markdown.startswith("---"))
        self.assertIn("tags: [web-clip]", markdown)
        self.assertIn(f"url: '{original}'", markdown)

    def test_chunk_text_has_no_yaml_frontmatter(self):
        self.ingest("https://example.com/page")
        rows = self.table_rows()
        self.assertTrue(rows)
        for _source, text, _index in rows:
            self.assertNotIn("generator: lancedb-search", text)
            self.assertNotIn("tags: [web-clip]", text)
            self.assertNotIn("clipped:", text)
            self.assertFalse(text.lstrip().startswith("---"), text[:80])

    def test_different_canonical_urls_get_different_sources(self):
        self.ingest("https://example.com/a")
        self.ingest("https://example.com/b")
        sources = kb_schema.existing_sources(kb_schema.get_or_create_table())
        self.assertEqual(len(sources), 2)
        self.assertFalse(any(src.startswith("网页-") for src in sources))
        snapshots = [
            name for name in os.listdir(self.web_dir())
            if name.endswith(".md")
        ]
        self.assertEqual(len(snapshots), 2)

    def test_header_keeps_original_url(self):
        url = "https://example.com/a?utm_source=feed&id=1"
        with mock.patch.object(kb_web.requests, "get", return_value=FakeResponse()):
            output = kb_web.ingest_url(url)
        self.assertIn("网页已索引", output)
        rows = self.table_rows()
        self.assertTrue(any(url in text for _src, text, _idx in rows))

    def test_whitespace_and_uppercase_scheme_accepted(self):
        with mock.patch.object(kb_web, "fetch_page",
                               return_value=("测试页面", CONTENT_A)) as fetch:
            output = kb_web.ingest_url("  HTTPS://Example.com/page  ")
        self.assertIn("网页已索引", output)
        fetch.assert_called_once_with("HTTPS://Example.com/page")


class TestIngestUrlIdempotency(WebIngestTestBase):
    def test_repeat_ingest_same_canonical_url_does_not_duplicate(self):
        first = self.ingest("https://example.com/page?utm_source=feed")
        row_count = kb_schema.row_count()
        snapshot_rel = self.snapshot_from_output(first)
        self.assertGreater(row_count, 0)

        second = self.ingest("https://Example.com/page#section")

        self.assertIn("新增记录: 0", second)
        self.assertIn("替换旧记录: 0", second)
        self.assertEqual(kb_schema.row_count(), row_count)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {vault_source.rel_to_vault(os.path.join(self.vault, snapshot_rel))},
        )
        snapshots = [
            name for name in os.listdir(self.web_dir())
            if name.endswith(".md")
        ]
        self.assertEqual(len(snapshots), 1)

    def test_changed_content_replaces_old_chunks_in_same_source(self):
        first = self.ingest("https://example.com/page")
        snapshot_rel = self.snapshot_from_output(first)
        row_count = kb_schema.row_count()
        old_texts = [text for _src, text, _idx in self.table_rows()]

        second = self.ingest("https://example.com/page", content=CONTENT_B)

        self.assertIn("替换旧记录: 1", second)
        self.assertEqual(kb_schema.row_count(), row_count)
        self.assertEqual(
            kb_schema.existing_sources(kb_schema.get_or_create_table()),
            {vault_source.rel_to_vault(os.path.join(self.vault, snapshot_rel))},
        )
        texts = [text for _src, text, _idx in self.table_rows()]
        self.assertFalse(any("甲" in text or "乙" in text for text in texts))
        self.assertTrue(any("丙" in text or "丁" in text for text in texts))
        self.assertNotEqual(old_texts, texts)


if __name__ == "__main__":
    unittest.main()
