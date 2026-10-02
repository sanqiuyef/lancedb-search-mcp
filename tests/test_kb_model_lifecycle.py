# -*- coding: utf-8 -*-
"""kb.model_lifecycle：闲置自动卸载逻辑 + 本地嵌入懒加载（全离线假模型）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg  # noqa: E402
from kb import embeddings as kb_embeddings  # noqa: E402
from kb import model_lifecycle as lifecycle  # noqa: E402


class FakeSTModel:
    """假 sentence-transformers 模型：返回 (n, dims) 的常量向量。"""

    def __init__(self, dims: int = 8):
        self.dims = dims
        self.calls = 0

    def encode(self, texts, convert_to_numpy=True, normalize_embeddings=True):
        import numpy as np

        self.calls += 1
        return np.ones((len(texts), self.dims), dtype="float32") / (self.dims ** 0.5)


class LifecycleTestBase(unittest.TestCase):
    """隔离生命周期模块状态与配置，测试后恢复。"""

    def setUp(self):
        self._orig_timeout = cfg.LOCAL_MODEL_IDLE_UNLOAD
        self._orig_last = lifecycle._last_used
        self._orig_unloaders = dict(lifecycle._unloaders)
        self._orig_st = kb_embeddings._st_holder["model"]

    def tearDown(self):
        cfg.LOCAL_MODEL_IDLE_UNLOAD = self._orig_timeout
        lifecycle._last_used = self._orig_last
        lifecycle._unloaders.clear()
        lifecycle._unloaders.update(self._orig_unloaders)
        kb_embeddings._st_holder["model"] = self._orig_st


class TestIdleUnload(LifecycleTestBase):
    def test_idle_timeout_triggers_unload(self):
        cfg.LOCAL_MODEL_IDLE_UNLOAD = 300
        calls = []

        lifecycle.register_unloader("fake", lambda: calls.append(1) or True)
        lifecycle._last_used = 1000.0
        self.assertTrue(lifecycle.maybe_unload(now=1301.0))
        self.assertEqual(calls, [1])

    def test_within_timeout_keeps_loaded(self):
        cfg.LOCAL_MODEL_IDLE_UNLOAD = 300
        lifecycle.register_unloader("fake", lambda: True)
        lifecycle._last_used = 1000.0
        self.assertFalse(lifecycle.maybe_unload(now=1200.0))

    def test_no_usage_never_unloads(self):
        cfg.LOCAL_MODEL_IDLE_UNLOAD = 300
        lifecycle.register_unloader("fake", lambda: True)
        lifecycle._last_used = None
        self.assertFalse(lifecycle.maybe_unload(now=10**9))

    def test_zero_timeout_disables(self):
        cfg.LOCAL_MODEL_IDLE_UNLOAD = 0
        lifecycle.register_unloader("fake", lambda: True)
        lifecycle._last_used = 0.0
        self.assertFalse(lifecycle.maybe_unload(now=10**9))

    def test_unloader_failure_does_not_block_others(self):
        cfg.LOCAL_MODEL_IDLE_UNLOAD = 60
        done = []

        def boom():
            raise RuntimeError("x")

        lifecycle.register_unloader("boom", boom)
        lifecycle.register_unloader("ok", lambda: done.append(1) or True)
        lifecycle._last_used = 0.0
        self.assertTrue(lifecycle.maybe_unload(now=61.0))
        self.assertEqual(done, [1])


class TestLazyLocalEmbeddings(LifecycleTestBase):
    def test_ndims_does_not_load_model(self):
        kb_embeddings._st_holder["model"] = None
        func = kb_embeddings.LocalSentenceTransformerEmbeddings(
            name="test/model", dimensions=32, device="cpu"
        )
        self.assertEqual(func.ndims(), 32)
        self.assertIsNone(kb_embeddings._st_holder["model"])

    def test_generate_uses_injected_model(self):
        fake = FakeSTModel(dims=8)
        kb_embeddings._st_holder["model"] = fake
        func = kb_embeddings.LocalSentenceTransformerEmbeddings(
            name="test/model", dimensions=8, device="cpu"
        )
        vectors = func.generate_embeddings(["你好", "世界"])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), 8)
        self.assertEqual(fake.calls, 1)

    def test_generate_loads_when_holder_empty(self):
        fake = FakeSTModel(dims=4)
        loaded = []
        original_loader = kb_embeddings._load_st_model

        def fake_loader(name, device, trust_remote_code=True):
            loaded.append((name, device))
            return fake

        kb_embeddings._st_holder["model"] = None
        kb_embeddings._load_st_model = fake_loader
        try:
            func = kb_embeddings.LocalSentenceTransformerEmbeddings(
                name="test/model", dimensions=4, device="cpu"
            )
            vectors = func.generate_embeddings(["单条"])
            self.assertEqual(loaded, [("test/model", "cpu")])
            self.assertEqual(len(vectors[0]), 4)
            self.assertIs(kb_embeddings._st_holder["model"], fake)  # 加载结果进持有器
        finally:
            kb_embeddings._load_st_model = original_loader
            kb_embeddings._st_holder["model"] = self._orig_st

    def test_unload_callback_clears_holder(self):
        kb_embeddings._st_holder["model"] = FakeSTModel()
        self.assertTrue(kb_embeddings._unload_st_model())
        self.assertIsNone(kb_embeddings._st_holder["model"])
        self.assertFalse(kb_embeddings._unload_st_model())  # 空持有器再卸载 = 无事发生


if __name__ == "__main__":
    unittest.main()
