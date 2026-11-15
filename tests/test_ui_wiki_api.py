# -*- coding: utf-8 -*-
"""UI server 的 Wiki 只读/审核 API 测试。

全部用例使用临时 vault、临时 Git 仓库与临时 SQLite；页面索引被 monkeypatch
为 stub，不会加载 bge-m3，也不会访问真实知识库。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ui_server  # noqa: E402
from kb.wiki import service  # noqa: E402
from kb.wiki.config import WikiConfig  # noqa: E402
from kb.wiki.db import connect, run_migrations  # noqa: E402
from kb.wiki.page_store import PageDraft, PageStore  # noqa: E402

_ENV_KEYS = (
    "WIKI_ENABLED",
    "WIKI_VAULT_PATH",
    "WIKI_DB_PATH",
    "WIKI_GIT_COMMIT",
    "WIKI_PAGE_INDEX_TABLE",
    "LANCEDB_DB_PATH",
)


class UiWikiApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)
        self.db_path = os.path.join(self.root, "db", "wiki.sqlite3")
        self.lance_db = os.path.join(self.root, "lancedb")

        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        self.addCleanup(self._restore_env)
        self._set_env("WIKI_ENABLED", "1")
        self._set_env("WIKI_VAULT_PATH", self.vault)
        self._set_env("WIKI_DB_PATH", self.db_path)
        self._set_env("WIKI_GIT_COMMIT", "1")
        self._set_env("LANCEDB_DB_PATH", self.lance_db)

        self.init_git()
        self.config = WikiConfig.from_env()
        self.conn = connect(self.config.db_path, self.config)
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)
        self.store = PageStore(self.config, self.conn)

        # 页面索引会写 LanceDB 并可能加载 embedding；测试中用 stub 隔离。
        self.index_patcher = mock.patch(
            "kb.wiki.service.page_index.index_page",
            side_effect=self._index_stub,
        )
        self.index_mock = self.index_patcher.start()
        self.addCleanup(self.index_patcher.stop)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    # ── 环境与临时设施 ──
    def _set_env(self, key, value):
        os.environ[key] = value

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def init_git(self):
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "Wiki UI Test"],
            ["git", "config", "user.email", "wiki-ui@test.local"],
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

    def _index_stub(self, page, *args, **kwargs):
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
        title,
        *,
        status="published",
        page_type="concept",
        slug=None,
        body="",
        aliases=None,
        sources=None,
        commit=False,
    ):
        return self.store.create_page(
            PageDraft(
                title=title,
                page_type=page_type,
                summary=f"{title} 的摘要。",
                body=body,
                slug=slug,
                aliases=list(aliases or []),
                sources=list(sources or []),
                tags=["test"],
                status=status,
            ),
            actor="test",
            commit=commit,
        )

    def make_proposal(self, page, content, rationale="补充内容"):
        result = service.propose_update(
            page.page_id,
            content,
            rationale,
            page.current_revision_id,
            config=self.config,
            conn=self.conn,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "pending")
        return result

    def request(
        self,
        method,
        path,
        body=None,
        headers=None,
        csrf=False,
        with_headers=False,
    ):
        data = None
        req_headers = dict(headers or {})
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = body
            req_headers.setdefault("Content-Type", "application/json")
        if csrf:
            req_headers.setdefault("X-Wiki-CSRF", self.csrf_token())
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=req_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if with_headers:
                    return response.status, payload, response.headers
                return response.status, payload
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            if with_headers:
                return exc.code, payload, exc.headers
            return exc.code, payload

    def csrf_token(self):
        if not hasattr(self, "_csrf_token"):
            status, data = self.request("GET", "/api/wiki/status")
            self.assertEqual(status, 200)
            token = data.get("csrf_token")
            self.assertIsInstance(token, str)
            self.assertEqual(len(token), 64)
            self._csrf_token = token
        return self._csrf_token

    # ── 用例 ──
    def test_wiki_disabled_returns_503(self):
        self._set_env("WIKI_ENABLED", "0")
        status, data = self.request("GET", "/api/wiki/status")
        self.assertEqual(status, 503)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "wiki disabled")
        self.assertIn("WIKI_ENABLED=1", data["hint"])

        status, data = self.request("GET", "/api/wiki/pages")
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "wiki disabled")

    def test_status_pages_and_validate(self):
        self.make_page("已发布概念", slug="published-concept")
        self.make_page("已发布实体", page_type="entity", slug="published-entity")
        self.make_page("草稿概念", status="draft", slug="draft-concept")

        status, data = self.request("GET", "/api/wiki/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
        self.assertEqual(data["pages"]["total"], 3)
        self.assertEqual(data["pages"]["by_status"].get("published"), 2)
        self.assertEqual(data["pages"]["by_status"].get("draft"), 1)
        self.assertIn("0001", data["db"]["schema_versions"])
        self.assertIn("config", data)

        status, data = self.request("GET", "/api/wiki/pages")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 2)
        self.assertTrue(all(p["status"] == "published" for p in data["pages"]))
        self.assertIn("next_cursor", data)

        status, data = self.request(
            "GET", "/api/wiki/pages?status=published&page_type=entity"
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["pages"][0]["slug"], "published-entity")

        status, data = self.request("GET", "/api/wiki/pages?status=&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertTrue(data["has_more"])
        self.assertTrue(data["next_cursor"])
        first_slug = data["pages"][0]["slug"]
        cursor = data["next_cursor"]
        status, data = self.request(
            "GET", "/api/wiki/pages?status=&limit=1&cursor=" + cursor
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertNotEqual(data["pages"][0]["slug"], first_slug)

        status, data = self.request("GET", "/api/wiki/validate")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("summary", data)
        self.assertIn("journal", data)

    def test_get_page_by_id_and_slug_with_links_and_sources(self):
        source = self.make_page(
            "来源页", slug="source-page", body="来源正文\n"
        )
        target = self.make_page(
            "目标页",
            slug="target-page",
            body="# 目标标题\n\n参见 [[来源页]]。\n",
            aliases=["目标别名"],
            sources=["paper.pdf"],
        )

        status, data = self.request(
            "GET",
            f"/api/wiki/page?id={target.page_id}&sources=1&backlinks=1",
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["page"]["title"], "目标页")
        self.assertEqual(data["page"]["status"], "published")
        self.assertEqual(data["page"]["page_type"], "concept")
        self.assertIn("目标别名", data["frontmatter"]["aliases"])
        self.assertIn("# 目标标题", data["body"])
        self.assertEqual(data["sources"], ["paper.pdf"])
        self.assertEqual(data["evidence"]["claim_count"], 0)
        self.assertEqual(len(data["links"]), 1)
        self.assertEqual(data["links"][0]["target_title"], "来源页")

        status, data = self.request(
            "GET", "/api/wiki/page?id=target-page&sources=0&backlinks=0"
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["page"]["page_id"], target.page_id)
        self.assertIsNone(data["evidence"])
        self.assertEqual(data["backlinks"], [])

        status, data = self.request(
            "GET", f"/api/wiki/page?id={source.page_id}&backlinks=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(data["backlinks"]), 1)
        self.assertEqual(data["backlinks"][0]["source_title"], "目标页")

    def test_proposals_and_proposal_detail(self):
        page = self.make_page(
            "提案页面", slug="proposal-page", body="旧内容\n", commit=True
        )
        proposal = self.make_proposal(page, "# 新内容\n", "补充新内容")

        status, data = self.request("GET", "/api/wiki/proposals")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["count"], 1)
        item = data["proposals"][0]
        self.assertEqual(item["proposal_id"], proposal["proposal_id"])
        self.assertEqual(item["page_title"], "提案页面")
        self.assertEqual(item["expected_revision_id"], page.current_revision_id)
        self.assertEqual(item["rationale"], "补充新内容")

        status, data = self.request(
            "GET", "/api/wiki/proposal?id=" + proposal["proposal_id"]
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["draft"], "# 新内容\n")
        self.assertTrue(data["diff"])
        self.assertEqual(data["target_page"]["page_id"], page.page_id)
        self.assertEqual(
            data["proposal"]["expected_revision_id"], page.current_revision_id
        )

    def test_apply_approve_updates_page_and_commits_git(self):
        page = self.make_page(
            "审核通过页", slug="approve-page", body="旧内容\n", commit=True
        )
        proposal = self.make_proposal(page, "新内容\n")

        head_before = self.git("rev-parse", "HEAD").stdout.strip()
        status, data = self.request(
            "POST",
            "/api/wiki/apply",
            {
                "proposal_id": proposal["proposal_id"],
                "decision": "approve",
                "review_note": "LGTM",
            },
            csrf=True,
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["status"], "applied")
        self.assertTrue(data["applied"])
        self.assertEqual(data["page_id"], page.page_id)
        self.assertTrue(data["git_commit"])

        head_after = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(head_after, head_before)
        subjects = self.git("log", "--pretty=%s").stdout
        self.assertIn("wiki: update", subjects)

        status, page_data = self.request(
            "GET", f"/api/wiki/page?id={page.page_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page_data["body"], "新内容\n")
        self.assertEqual(page_data["page"]["status"], "published")

        status, proposal_data = self.request(
            "GET", "/api/wiki/proposal?id=" + proposal["proposal_id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposal_data["proposal"]["status"], "applied")
        self.assertEqual(
            proposal_data["proposal"]["result_revision_id"], data["revision_id"]
        )

        status, queue = self.request("GET", "/api/wiki/proposals?status=pending")
        self.assertEqual(status, 200)
        self.assertEqual(queue["count"], 0)

    def test_apply_reject_does_not_change_page(self):
        page = self.make_page(
            "审核拒绝页", slug="reject-page", body="保持内容\n", commit=True
        )
        proposal = self.make_proposal(page, "不应写入\n", "理由不足")
        head_before = self.git("rev-parse", "HEAD").stdout.strip()

        status, data = self.request(
            "POST",
            "/api/wiki/apply",
            {
                "proposal_id": proposal["proposal_id"],
                "decision": "reject",
                "review_note": "理由不足",
            },
            csrf=True,
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "rejected")
        self.assertFalse(data["applied"])

        status, page_data = self.request(
            "GET", f"/api/wiki/page?id={page.page_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page_data["body"], "保持内容\n")

        status, proposal_data = self.request(
            "GET", "/api/wiki/proposal?id=" + proposal["proposal_id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposal_data["proposal"]["status"], "rejected")

        head_after = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head_after, head_before)
        self.assertEqual(self.index_mock.call_count, 0)

    def test_invalid_json_and_missing_proposal_id_return_400(self):
        status, data = self.request(
            "POST", "/api/wiki/apply", "{not-json", csrf=True
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("bad request", data["error"])

        status, data = self.request("POST", "/api/wiki/apply", {}, csrf=True)
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("proposal_id", data["error"])

        status, data = self.request("GET", "/api/wiki/page")
        self.assertEqual(status, 400)
        self.assertIn("id", data["error"])

        status, data = self.request("GET", "/api/wiki/proposal")
        self.assertEqual(status, 400)
        self.assertIn("id", data["error"])

    def test_request_body_size_limit(self):
        status, data = self.request(
            "POST", "/api/wiki/apply", "x" * 70000, csrf=True
        )
        self.assertEqual(status, 413)
        self.assertFalse(data["ok"])

    def test_host_header_must_match_bind_address(self):
        port = self.server.server_address[1]
        for host in (
            "evil.example",
            f"evil.example:{port}",
            f"127.0.0.1:{port + 1}",
            "127.0.0.1",
        ):
            status, data = self.request(
                "GET", "/api/status", headers={"Host": host}
            )
            self.assertIn(status, (403, 421), (host, data))
            self.assertFalse(data["ok"])

        status, data = self.request(
            "POST", "/api/search", {"query": "洪涝"}, headers={"Host": "evil.example"}
        )
        self.assertIn(status, (403, 421), data)
        self.assertFalse(data["ok"])

        for host in (
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        ):
            status, data = self.request(
                "GET", "/api/status", headers={"Host": host}
            )
            self.assertEqual(status, 200, (host, data))

    def test_wiki_status_exposes_csrf_token(self):
        status, data = self.request("GET", "/api/wiki/status")
        self.assertEqual(status, 200)
        token = data.get("csrf_token")
        self.assertIsInstance(token, str)
        self.assertEqual(len(token), 64)

        status, data = self.request("GET", "/api/wiki/pages")
        self.assertEqual(status, 200)
        self.assertNotIn("csrf_token", data)

    def test_responses_do_not_send_wildcard_cors_header(self):
        for path in ("/api/status", "/api/wiki/status"):
            status, data, headers = self.request(
                "GET", path, with_headers=True
            )
            self.assertEqual(status, 200, (path, data))
            self.assertIsNone(
                headers.get("Access-Control-Allow-Origin"), path
            )

    def test_apply_rejects_csrf_and_origin_violations(self):
        page = self.make_page(
            "防护页", slug="guard-page", body="原内容\n", commit=True
        )
        proposal = self.make_proposal(page, "被批准内容\n")
        body = {
            "proposal_id": proposal["proposal_id"],
            "decision": "approve",
            "review_note": "ok",
        }
        token = self.csrf_token()
        path = "/api/wiki/apply"

        # 无 token
        status, data = self.request("POST", path, body)
        self.assertEqual(status, 403, data)

        # 错误 token
        status, data = self.request(
            "POST", path, body, headers={"X-Wiki-CSRF": "0" * 64}
        )
        self.assertEqual(status, 403, data)

        # Content-Type 不是 JSON（含 text/plain 与错误 charset）
        for ctype in (
            "text/plain",
            "application/x-www-form-urlencoded",
            "application/json; charset=utf-16",
        ):
            status, data = self.request(
                "POST",
                path,
                body,
                headers={"Content-Type": ctype, "X-Wiki-CSRF": token},
            )
            self.assertEqual(status, 415, (ctype, data))

        # 跨源 Origin
        for origin in (
            "http://evil.example",
            f"http://127.0.0.1:{self.server.server_address[1] + 1}",
            "null",
        ):
            status, data = self.request(
                "POST",
                path,
                body,
                headers={"Origin": origin, "X-Wiki-CSRF": token},
            )
            self.assertEqual(status, 403, (origin, data))

        # Sec-Fetch-Site 只允许 same-origin / none
        for site in ("cross-site", "same-site"):
            status, data = self.request(
                "POST",
                path,
                body,
                headers={"Sec-Fetch-Site": site, "X-Wiki-CSRF": token},
            )
            self.assertEqual(status, 403, (site, data))

        # 以上请求都没有改变提案状态
        status, queue = self.request("GET", "/api/wiki/proposals?status=pending")
        self.assertEqual(status, 200)
        self.assertEqual(queue["count"], 1)

        # 正确 token + 同源 Origin + same-origin 可以成功
        status, data = self.request(
            "POST",
            path,
            body,
            headers={
                "Origin": self.base_url,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json; charset=utf-8",
            },
            csrf=True,
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["status"], "applied")

        status, page_data = self.request(
            "GET", f"/api/wiki/page?id={page.page_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page_data["body"], "被批准内容\n")

    def test_original_search_endpoint_still_works(self):
        fake = {
            "results": [
                {
                    "source": "paper.pdf",
                    "chunk_index": 0,
                    "text": "城市洪涝",
                    "relevance_score": 0.9,
                }
            ],
            "trace": {"mode": "hybrid", "reranker": "none"},
        }
        with mock.patch(
            "kb.search.search_structured", return_value=fake
        ) as mocked:
            status, data = self.request(
                "POST", "/api/search", {"query": "洪涝", "limit": 5}
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["results"][0]["source"], "paper.pdf")
        self.assertEqual(data["trace"]["mode"], "hybrid")
        mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
