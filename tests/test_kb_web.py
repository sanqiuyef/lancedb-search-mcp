# -*- coding: utf-8 -*-
"""kb_web：网页抓取与正文抽取（mock HTTP，不联网）。"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import web as kb_web

HTML = """
<html><head><title>测试页面</title></head>
<body>
  <nav>导航</nav>
  <script>var x = 1;</script>
  <article><p>这是正文第一段，内容足够长。</p><p>这是第二段。</p></article>
  <footer>页脚</footer>
</body></html>
"""


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


class TestIngestUrl(unittest.TestCase):
    def test_invalid_url_rejected(self):
        self.assertIn("无效", kb_web.ingest_url("ftp://example.com"))

    def test_fetch_failure_reported(self):
        with mock.patch.object(kb_web.requests, "get",
                               side_effect=ConnectionError("refused")):
            out = kb_web.ingest_url("https://example.com")
        self.assertIn("抓取失败", out)


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
            "http://example.com/x",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com:443/x"),
            "https://example.com/x",
        )
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com:8443/x"),
            "https://example.com:8443/x",
        )

    def test_trailing_slash_normalized(self):
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/docs/"),
            "https://example.com/docs",
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
            kb_web.canonicalize_url("https://example.com/docs///"),
            "https://example.com/docs",
        )

    def test_query_order_preserved(self):
        self.assertEqual(
            kb_web.canonicalize_url("https://example.com/?b=2&a=1&utm_medium=x"),
            "https://example.com/?b=2&a=1",
        )

    def test_invalid_urls_raise_value_error(self):
        for bad in ("ftp://example.com", "https://", "not a url",
                    "https://example.com:bad/x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                kb_web.canonicalize_url(bad)


class FakeChunkTable:
    """只实现 ingest_url 用到的 delete/len：按 source 删除模拟旧行。"""

    def __init__(self):
        self.rows = []
        self.delete_calls = []

    def delete(self, where):
        self.delete_calls.append(where)
        prefix = "source = '"
        source = where[len(prefix):-1].replace("''", "'")
        removed = sum(1 for row in self.rows if row["source"] == source)
        self.rows = [row for row in self.rows if row["source"] != source]
        return SimpleNamespace(num_deleted_rows=removed)

    def __len__(self):
        return len(self.rows)


def _fake_add_chunks(table, chunks):
    table.rows.extend(chunks)
    return len(chunks)


class TestIngestUrlIdentity(unittest.TestCase):
    def setUp(self):
        self.table = FakeChunkTable()

    def _ingest(self, url):
        with mock.patch.object(kb_web.requests, "get", return_value=FakeResponse()), \
             mock.patch.object(kb_web.kb_schema, "get_or_create_table",
                               return_value=self.table), \
             mock.patch.object(kb_web.kb_schema, "ensure_vector_index"), \
             mock.patch.object(kb_web.kb_schema, "ensure_fts_index"), \
             mock.patch.object(kb_web.kb_schema, "fold_new_rows"), \
             mock.patch.object(kb_web.kb_schema, "update_db_readme"), \
             mock.patch.object(kb_web.kb_ingest, "add_chunks",
                               side_effect=_fake_add_chunks):
            return kb_web.ingest_url(url)

    def test_different_pages_get_different_sources(self):
        self._ingest("https://example.com/a")
        source_a = self.table.rows[0]["source"]
        self._ingest("https://example.com/b")
        sources = {row["source"] for row in self.table.rows}
        self.assertEqual(len(sources), 2)
        self.assertIn(source_a, sources)
        self.assertRegex(source_a, r"^网页-example\.com-[0-9a-f]{8}$")

    def test_utm_variants_share_source(self):
        self._ingest("https://example.com/a?utm_source=feed")
        source_first = self.table.rows[0]["source"]
        self._ingest("https://example.com/a?utm_medium=social")
        self.assertEqual({row["source"] for row in self.table.rows}, {source_first})

    def test_repeat_ingest_replaces_old_rows(self):
        self._ingest("https://example.com/page?utm_source=feed")
        row_count = len(self.table.rows)
        self.assertGreater(row_count, 0)
        source = self.table.rows[0]["source"]

        self._ingest("https://Example.com/page#section")

        self.assertEqual(len(self.table.rows), row_count)
        self.assertEqual({row["source"] for row in self.table.rows}, {source})
        self.assertEqual(
            self.table.delete_calls,
            [f"source = '{source}'", f"source = '{source}'"],
        )

    def test_header_keeps_original_url(self):
        url = "https://example.com/a?utm_source=feed&id=1"
        self._ingest(url)
        self.assertTrue(any(url in row["text"] for row in self.table.rows))


if __name__ == "__main__":
    unittest.main()
