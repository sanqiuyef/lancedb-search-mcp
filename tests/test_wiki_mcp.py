# -*- coding: utf-8 -*-
"""MCP 注册与参数解析：WIKI_ENABLED 开关、14 个工具名、逗号列表与 payload_json。

测试只重载 server 模块并检查 FastMCP 注册表，不启动 mcp.run()；
service 调用用 monkeypatch 隔离，不触碰真实 vault / 知识库。
"""

import importlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ORIGINAL_WIKI_ENABLED = os.environ.get("WIKI_ENABLED")
os.environ["WIKI_ENABLED"] = "0"

import server  # noqa: E402

EXISTING_TOOLS = {
    "search_knowledge",
    "ask_knowledge",
    "get_knowledge_status",
    "add_documents",
    "add_single_document",
    "update_document",
    "delete_documents",
    "list_documents",
    "get_document",
    "search_similar",
    "ingest_url",
    "start_watcher",
    "stop_watcher",
    "search_images",
    "ingest_images",
}

WIKI_TOOLS = {
    "wiki_status",
    "wiki_search",
    "wiki_get_page",
    "wiki_list_pages",
    "wiki_get_related",
    "wiki_get_evidence",
    "wiki_search_evidence",
    "wiki_create_page",
    "wiki_propose_update",
    "wiki_submit_extraction",
    "wiki_get_proposal",
    "wiki_review_queue",
    "wiki_apply_proposal",
    "wiki_validate",
}


def _restore_original_env():
    if _ORIGINAL_WIKI_ENABLED is None:
        os.environ.pop("WIKI_ENABLED", None)
    else:
        os.environ["WIKI_ENABLED"] = _ORIGINAL_WIKI_ENABLED


class WikiMcpRegistrationTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("WIKI_ENABLED")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("WIKI_ENABLED", None)
        else:
            os.environ["WIKI_ENABLED"] = self._saved
        importlib.reload(server)

    def reload_with(self, value):
        os.environ["WIKI_ENABLED"] = value
        return importlib.reload(server)

    def tool_names(self, module=None):
        target = module or server
        return set(target.mcp._tool_manager._tools)

    def test_disabled_keeps_existing_15_tools(self):
        module = self.reload_with("0")
        names = self.tool_names(module)
        self.assertEqual(len(names), 15)
        self.assertEqual(names, EXISTING_TOOLS)
        self.assertFalse(any(name.startswith("wiki_") for name in names))

    def test_enabled_registers_exactly_14_wiki_tools(self):
        module = self.reload_with("1")
        names = self.tool_names(module)
        self.assertEqual(len(names), 29)
        self.assertEqual(
            {name for name in names if name.startswith("wiki_")}, WIKI_TOOLS
        )
        self.assertEqual(len(WIKI_TOOLS), 14)
        self.assertTrue(EXISTING_TOOLS.issubset(names))

    def test_enabled_then_disabled_restores_15(self):
        self.reload_with("1")
        module = self.reload_with("0")
        names = self.tool_names(module)
        self.assertEqual(len(names), 15)
        self.assertEqual(names, EXISTING_TOOLS)

    def test_truthy_values_register_14_wiki_tools(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value):
                module = self.reload_with(value)
                names = self.tool_names(module)
                self.assertEqual(len(names), 29)
                self.assertEqual(
                    {name for name in names if name.startswith("wiki_")},
                    WIKI_TOOLS,
                )

    def test_falsy_or_empty_values_do_not_register_wiki_tools(self):
        for value in ("0", "false", "FALSE", "off", "", "  "):
            with self.subTest(value=value):
                module = self.reload_with(value)
                names = self.tool_names(module)
                self.assertEqual(len(names), 15)
                self.assertFalse(any(name.startswith("wiki_") for name in names))

    def test_unset_env_does_not_register_wiki_tools(self):
        os.environ.pop("WIKI_ENABLED", None)
        module = importlib.reload(server)
        names = self.tool_names(module)
        self.assertEqual(len(names), 15)
        self.assertFalse(any(name.startswith("wiki_") for name in names))


class WikiMcpHandlerTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("WIKI_ENABLED")
        os.environ["WIKI_ENABLED"] = "1"
        self.module = importlib.reload(server)
        self.addCleanup(self._restore)
        self.wiki_service = sys.modules["kb.wiki.service"]

    def _restore(self):
        if self._saved is None:
            os.environ.pop("WIKI_ENABLED", None)
        else:
            os.environ["WIKI_ENABLED"] = self._saved
        importlib.reload(server)

    def parse(self, text):
        payload = json.loads(text)
        self.assertIsInstance(payload, dict)
        return payload

    def test_wiki_create_page_splits_list_arguments(self):
        with mock.patch.object(
            self.wiki_service, "create_page", return_value={"ok": True}
        ) as create_mock:
            payload = self.parse(
                self.module.wiki_create_page(
                    "标题",
                    "concept",
                    "正文",
                    slug="slug",
                    aliases="别名一, 别名二，别名三",
                    tags="tag1、tag2; tag3",
                    summary="",
                    sources="paper.pdf, notes/",
                )
            )
        self.assertTrue(payload["ok"])
        kwargs = create_mock.call_args.kwargs
        self.assertEqual(kwargs["aliases"], ["别名一", "别名二", "别名三"])
        self.assertEqual(kwargs["tags"], ["tag1", "tag2", "tag3"])
        self.assertEqual(kwargs["sources"], ["paper.pdf", "notes/"])
        self.assertEqual(create_mock.call_args.args[:3], ("标题", "concept", "正文"))

    def test_wiki_search_splits_tags(self):
        with mock.patch.object(
            self.wiki_service, "search", return_value={"ok": True}
        ) as search_mock:
            payload = self.parse(
                self.module.wiki_search(
                    "query", mode="hybrid", limit=5, tags="flood, urban"
                )
            )
        self.assertTrue(payload["ok"])
        kwargs = search_mock.call_args.kwargs
        self.assertEqual(kwargs["tags"], ["flood", "urban"])
        self.assertEqual(kwargs["mode"], "hybrid")
        self.assertEqual(kwargs["limit"], 5)

    def test_wiki_submit_extraction_parses_json(self):
        submitted = {
            "analysis": "分析",
            "entities": [{"name": "A", "confidence": 0.9}],
        }
        with mock.patch.object(
            self.wiki_service, "submit_extraction", return_value={"ok": True}
        ) as submit_mock:
            payload = self.parse(
                self.module.wiki_submit_extraction(json.dumps(submitted))
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(submit_mock.call_args.args[0], submitted)

    def test_wiki_submit_extraction_rejects_invalid_json(self):
        with mock.patch.object(
            self.wiki_service, "submit_extraction"
        ) as submit_mock:
            payload = self.parse(self.module.wiki_submit_extraction("{not json"))
        self.assertFalse(payload["ok"])
        self.assertIn("JSON", payload["error"])
        submit_mock.assert_not_called()

    def test_wiki_apply_proposal_passes_decision_and_note(self):
        with mock.patch.object(
            self.wiki_service, "apply_proposal", return_value={"ok": True}
        ) as apply_mock:
            payload = self.parse(
                self.module.wiki_apply_proposal(
                    "proposal-1", decision="reject", review_note="理由"
                )
            )
        self.assertTrue(payload["ok"])
        kwargs = apply_mock.call_args.kwargs
        self.assertEqual(kwargs["decision"], "reject")
        self.assertEqual(kwargs["review_note"], "理由")
        self.assertEqual(apply_mock.call_args.args[0], "proposal-1")

    def test_exceptions_become_ok_false_json(self):
        with mock.patch.object(
            self.wiki_service, "status", side_effect=RuntimeError("boom")
        ):
            payload = self.parse(self.module.wiki_status())
        self.assertFalse(payload["ok"])
        self.assertIn("boom", payload["error"])

    def test_get_proposal_and_review_queue_delegate(self):
        with mock.patch.object(
            self.wiki_service, "get_proposal", return_value={"ok": True}
        ) as get_mock:
            self.assertTrue(self.parse(self.module.wiki_get_proposal("p-1"))["ok"])
        self.assertEqual(get_mock.call_args.args[0], "p-1")

        with mock.patch.object(
            self.wiki_service, "review_queue", return_value={"ok": True}
        ) as queue_mock:
            payload = self.parse(
                self.module.wiki_review_queue(status="pending", limit=7)
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(queue_mock.call_args.kwargs, {"status": "pending", "limit": 7})


if __name__ == "__main__":
    unittest.main()
