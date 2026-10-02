# -*- coding: utf-8 -*-
"""kb_web：网页抓取与正文抽取（mock HTTP，不联网）。"""

import os
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
