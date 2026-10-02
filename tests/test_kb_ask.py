# -*- coding: utf-8 -*-
"""kb_ask：证据检索打包（全 mock，不联网、无模型生成调用）。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import ask as kb_ask


def _structured(results):
    return {"results": results, "trace": {"mode": "hybrid"}}


class TestAskKnowledge(unittest.TestCase):
    def test_empty_results_message(self):
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               return_value=_structured([])):
            out = kb_ask.ask_knowledge("问题")["evidence_block"]
            self.assertIn("未找到", out)

    def test_evidence_block_with_citations(self):
        structured = _structured([
            {"text": "BIMbase 支持国产 BIM。", "source": r"D:\x\bim.md",
             "chunk_index": 2, "category": ""},
            {"text": "洪涝模型很复杂。", "source": "flood.md",
             "chunk_index": 0, "category": ""},
        ])
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               return_value=structured):
            out = kb_ask.ask_knowledge("BIMbase 是什么")["evidence_block"]
        self.assertIn("[1]", out)
        self.assertIn("[2]", out)
        self.assertIn("bim.md（chunk #2）", out)
        self.assertIn("BIMbase 支持国产 BIM。", out)
        self.assertIn("洪涝模型很复杂。", out)

    def test_evidence_struct_fields(self):
        structured = _structured([
            {"text": "t", "source": "s.md", "chunk_index": 3, "category": "paper"},
        ])
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               return_value=structured):
            result = kb_ask.ask_knowledge("问题")
        ev = result["evidence"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["cite"], "[1]")
        self.assertEqual(ev[0]["source"], "s.md")
        self.assertEqual(ev[0]["chunk_index"], 3)
        self.assertEqual(ev[0]["category"], "paper")

    def test_retrieval_error_propagates(self):
        with mock.patch.object(kb_ask.kb_search, "search_structured",
                               side_effect=ValueError("知识库为空")):
            with self.assertRaises(ValueError):
                kb_ask.ask_knowledge("问题")


if __name__ == "__main__":
    unittest.main()
