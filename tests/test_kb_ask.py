# -*- coding: utf-8 -*-
"""kb_ask：检索上下文拼装与 chat 调用（全 mock，不联网）。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import ask as kb_ask
from kb import config as cfg


class TestAskKnowledge(unittest.TestCase):
    def test_no_api_key(self):
        old = cfg.SILICONFLOW_API_KEY
        cfg.SILICONFLOW_API_KEY = ""
        try:
            out = kb_ask.ask_knowledge("问题")
            self.assertIn("SILICONFLOW_API_KEY", out)
        finally:
            cfg.SILICONFLOW_API_KEY = old

    def test_empty_results_message(self):
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               return_value={"results": [], "trace": {}}):
            out = kb_ask.ask_knowledge("问题")
            self.assertIn("未找到", out)

    def test_answer_with_citations(self):
        structured = {
            "results": [
                {"text": "BIMbase 支持国产 BIM。", "source": r"D:\x\bim.md",
                 "chunk_index": 2, "category": "", "project": "BIMbase"},
                {"text": "洪涝模型很复杂。", "source": "flood.md",
                 "chunk_index": 0, "category": "", "project": ""},
            ],
            "trace": {"mode": "hybrid"},
        }
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "BIMbase 是国产 BIM 平台 [1]。"}}]
        }
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               return_value=structured), \
             mock.patch.object(kb_ask.requests, "post", return_value=response):
            out = kb_ask.ask_knowledge("BIMbase 是什么")
        self.assertIn("[1]", out)
        self.assertIn("bim.md #2", out)
        self.assertIn("── 引用 ──", out)

    def test_chat_failure_reported(self):
        structured = {"results": [{"text": "t", "source": "s", "chunk_index": 0,
                                   "category": "", "project": ""}], "trace": {}}
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               return_value=structured), \
             mock.patch.object(kb_ask.requests, "post",
                               side_effect=ConnectionError("boom")):
            out = kb_ask.ask_knowledge("问题")
        self.assertIn("问答模型调用失败", out)


if __name__ == "__main__":
    unittest.main()
