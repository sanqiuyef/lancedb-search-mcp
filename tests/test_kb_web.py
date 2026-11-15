# -*- coding: utf-8 -*-
"""kb_web：网页抓取与正文抽取（mock HTTP，不联网）。"""

import os
import re
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

CONTENT_A = "# 标题\n\n" + "甲" * 200 + "\n\n" + "乙" * 200
CONTENT_B = "# 标题\n\n" + "丙" * 200 + "\n\n" + "丁" * 200


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
        self.assertEqual(
            kb_web.ingest_url("ftp://example.com"),
            "❌ 无效的 URL: ftp://example.com",
        )

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


class _FakeColumn:
    def __init__(self, values):
        self._values = values

    def to_pylist(self):
        return list(self._values)


class _FakeArrow:
    def __init__(self, data):
        self._data = data

    def column(self, name):
        return _FakeColumn(self._data[name])


class _FakeChunkLance:
    def __init__(self, rows):
        self._rows = rows

    def to_table(self, columns=None, filter=None):
        columns = columns or ["source", "doc_id"]
        rows = self._rows
        if filter:
            match = re.match(r"^source = '((?:[^']|'')*)'$", filter)
            if not match:
                raise AssertionError(f"unexpected filter: {filter}")
            source = match.group(1).replace("''", "'")
            rows = [row for row in rows if row["source"] == source]
        return _FakeArrow({name: [row.get(name, "") for row in rows]
                           for name in columns})


class FakeChunkTable:
    """记录 add/delete 顺序；to_lance 支持按 source 过滤读取 doc_id。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.delete_calls = []
        self.operations = []

    def to_lance(self):
        return _FakeChunkLance(self.rows)

    def delete(self, where):
        self.delete_calls.append(where)
        self.operations.append(("delete", where))
        match = re.match(
            r"^source = '((?:[^']|'')*)' AND "
            r"\(doc_id = '' OR doc_id NOT IN \((.*)\)\)$",
            where,
        )
        if not match:
            raise AssertionError(f"unexpected delete where: {where}")
        source = match.group(1).replace("''", "'")
        new_ids = {
            value.replace("''", "'")
            for value in re.findall(r"'((?:[^']|'')*)'", match.group(2))
        }
        stale = [
            row for row in self.rows
            if row["source"] == source
            and (not row.get("doc_id") or row["doc_id"] not in new_ids)
        ]
        self.rows = [row for row in self.rows if row not in stale]
        return SimpleNamespace(num_deleted_rows=len(stale))

    def __len__(self):
        return len(self.rows)


def _fake_add_chunks(table, chunks):
    table.operations.append(("add", [ch["doc_id"] for ch in chunks]))
    table.rows.extend(chunks)
    return len(chunks)


class TestIngestUrlIdentity(unittest.TestCase):
    def setUp(self):
        self.table = FakeChunkTable()
        self.last_fetch = None

    def _ingest(self, url, content=CONTENT_A, add_failure=False,
                patch_fetch=True):
        fetch = mock.Mock(return_value=("测试页面", content))
        self.last_fetch = fetch
        add = mock.Mock(side_effect=_fake_add_chunks)
        if add_failure:
            add.side_effect = RuntimeError("embedding down")
        patches = [
            mock.patch.object(kb_web.kb_schema, "get_or_create_table",
                              return_value=self.table),
            mock.patch.object(kb_web.kb_schema, "ensure_vector_index"),
            mock.patch.object(kb_web.kb_schema, "ensure_fts_index"),
            mock.patch.object(kb_web.kb_schema, "fold_new_rows"),
            mock.patch.object(kb_web.kb_schema, "update_db_readme"),
            mock.patch.object(kb_web.kb_ingest, "add_chunks", add),
        ]
        if patch_fetch:
            patches.append(mock.patch.object(kb_web, "fetch_page", fetch))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return kb_web.ingest_url(url)

    def test_different_pages_get_different_sources(self):
        self._ingest("https://example.com/a")
        source_a = self.table.rows[0]["source"]
        self._ingest("https://example.com/b")
        sources = {row["source"] for row in self.table.rows}
        self.assertEqual(len(sources), 2)
        self.assertIn(source_a, sources)
        self.assertRegex(source_a, r"^网页-example\.com-[0-9a-f]{12}$")
        self.assertEqual(len(source_a.rsplit("-", 1)[1]), 12)

    def test_utm_variants_share_source(self):
        self._ingest("https://example.com/a?utm_source=feed")
        source_first = self.table.rows[0]["source"]
        self._ingest("https://example.com/a?utm_medium=social")
        self.assertEqual({row["source"] for row in self.table.rows}, {source_first})

    def test_repeat_ingest_does_not_duplicate_rows(self):
        self._ingest("https://example.com/page?utm_source=feed")
        row_count = len(self.table.rows)
        self.assertGreater(row_count, 0)
        source = self.table.rows[0]["source"]
        self.table.operations.clear()

        output = self._ingest("https://Example.com/page#section")

        self.assertIn("已入库: 0", output)
        self.assertEqual(len(self.table.rows), row_count)
        self.assertEqual({row["source"] for row in self.table.rows}, {source})
        self.assertEqual(self.table.operations, [])

    def test_changed_content_adds_before_deleting_stale_rows(self):
        self._ingest("https://example.com/page")
        old_ids = {row["doc_id"] for row in self.table.rows}
        old_count = len(self.table.rows)
        self.table.operations.clear()

        output = self._ingest("https://example.com/page", content=CONTENT_B)

        self.assertIn("替换旧块", output)
        add_index = next(i for i, op in enumerate(self.table.operations)
                         if op[0] == "add")
        delete_index = next(i for i, op in enumerate(self.table.operations)
                            if op[0] == "delete")
        self.assertLess(add_index, delete_index)
        self.assertEqual(len(self.table.rows), old_count)
        self.assertFalse(old_ids & {row["doc_id"] for row in self.table.rows})
        self.assertTrue(any("丙" in row["text"] for row in self.table.rows))
        self.assertFalse(any("甲" in row["text"] for row in self.table.rows))

    def test_add_failure_keeps_old_rows(self):
        self._ingest("https://example.com/page")
        old_rows = list(self.table.rows)
        self.table.operations.clear()
        self.table.delete_calls.clear()

        with self.assertRaises(RuntimeError):
            self._ingest("https://example.com/page", content=CONTENT_B,
                         add_failure=True)

        self.assertEqual(self.table.delete_calls, [])
        self.assertEqual(self.table.rows, old_rows)

    def test_whitespace_and_uppercase_scheme_accepted(self):
        output = self._ingest("  HTTPS://Example.com/page  ")
        self.assertIn("网页已索引", output)
        self.last_fetch.assert_called_once_with("HTTPS://Example.com/page")

    def test_header_keeps_original_url(self):
        url = "https://example.com/a?utm_source=feed&id=1"
        with mock.patch.object(kb_web.requests, "get", return_value=FakeResponse()):
            self._ingest(url, patch_fetch=False)
        self.assertTrue(any(url in row["text"] for row in self.table.rows))


if __name__ == "__main__":
    unittest.main()
