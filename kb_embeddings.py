# -*- coding: utf-8 -*-
"""Embedding 后端：自定义 SiliconFlow 函数注册进官方 embedding 注册表。

基于 lancedb.embeddings 官方框架：`@register("siliconflow")` 后经
`get_registry().get("siliconflow").create()` 使用，配合 LanceModel 的
SourceField/VectorField 在 table.add() 与查询时自动向量化。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import List, Union

import numpy as np

import requests

from lancedb.embeddings import TextEmbeddingFunction, get_registry, register

import kb_config as cfg


# =============================================================
# 线程安全 LRU 缓存（watcher 线程与请求线程并发访问）
# =============================================================

class LRUCache:
    def __init__(self, capacity: int = 2000):
        self._cache: OrderedDict[str, list] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def put(self, key: str, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                if len(self._cache) >= self._capacity:
                    self._cache.popitem(last=False)
            self._cache[key] = value

    def __len__(self):
        with self._lock:
            return len(self._cache)


def _hash_text(text: str) -> str:
    """SHA-256 做缓存 key，避免截断碰撞。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_embedding_cache = LRUCache(capacity=2000)


def _auth_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if cfg.SILICONFLOW_API_KEY:
        headers["Authorization"] = f"Bearer {cfg.SILICONFLOW_API_KEY}"
    return headers


# =============================================================
# 自定义 SiliconFlow EmbeddingFunction（官方注册表条目）
# =============================================================

@register("siliconflow")
class SiliconFlowEmbeddings(TextEmbeddingFunction):
    """SiliconFlow OpenAI 兼容 embedding 接口，默认 Qwen3-Embedding-8B / 1024 维。"""

    name: str = cfg.EMBED_MODEL
    dimensions: int = cfg.EMBED_DIM
    batch_size: int = 32
    api_base: str = cfg.EMBEDDING_URL

    def ndims(self) -> int:
        return self.dimensions

    def generate_embeddings(
        self, texts: Union[List[str], np.ndarray]
    ) -> List[np.array]:
        """批量生成向量：先查 LRU 缓存，未命中的按 batch_size 分批调 API。"""
        texts = [str(t) for t in list(texts)]
        results: list = [None] * len(texts)
        pending_idx: list[int] = []
        pending_txt: list[str] = []

        for i, text in enumerate(texts):
            cached = _embedding_cache.get(_hash_text(text))
            if cached is not None:
                results[i] = cached
            else:
                pending_idx.append(i)
                pending_txt.append(text)

        for start in range(0, len(pending_txt), self.batch_size):
            batch = pending_txt[start:start + self.batch_size]
            vecs = self._request(batch)
            for offset, vec in enumerate(vecs):
                original = pending_idx[start + offset]
                results[original] = vec
                _embedding_cache.put(_hash_text(batch[offset]), vec)

        # 全部命中缓存时 results 可能已就绪；不足处兜底（理论不可达）
        return [np.array(r, dtype=np.float32) for r in results]

    def _request(self, batch: List[str]) -> List[List[float]]:
        resp = requests.post(
            self.api_base,
            headers=_auth_headers(),
            json={
                "model": self.name,
                "input": batch,
                "encoding_format": "float",
                "dimensions": self.dimensions,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]


# =============================================================
# 激活后端选择
# =============================================================

_embedding_func = None
_embedding_lock = threading.Lock()


def _resolve_device() -> str:
    """LOCAL_MODEL_DEVICE=auto 时探测 CUDA；torch 不接受 "auto" 设备串。"""
    if cfg.LOCAL_MODEL_DEVICE != "auto":
        return cfg.LOCAL_MODEL_DEVICE
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def get_embedding_function():
    """返回当前激活的官方 EmbeddingFunction（api → siliconflow，local → sentence-transformers）。"""
    global _embedding_func
    with _embedding_lock:
        if _embedding_func is not None:
            return _embedding_func
        if cfg.EMBEDDING_BACKEND == "local":
            func = (
                get_registry()
                .get("sentence-transformers")
                .create(name=cfg.LOCAL_EMBED_MODEL, device=_resolve_device())
            )
        else:
            func = get_registry().get("siliconflow").create()
        _embedding_func = func
        return func


def embedding_identity() -> tuple[str, str, int]:
    """(后端, 模型, 维度)，用于状态展示与索引一致性检查。"""
    func = get_embedding_function()
    if cfg.EMBEDDING_BACKEND == "local":
        return ("local", cfg.LOCAL_EMBED_MODEL, cfg.LOCAL_EMBED_DIM)
    return ("api", getattr(func, "name", type(func).__name__), func.ndims())


def reset_embedding_function() -> None:
    """测试用：清空进程级缓存的单例。"""
    global _embedding_func
    with _embedding_lock:
        _embedding_func = None


def embed_query(text: str) -> List[float]:
    """单条查询向量化（走同一缓存）。"""
    return list(get_embedding_function().compute_query_embeddings(text)[0])


def embed_texts(texts: List[str]) -> List[List[float]]:
    """批量文本向量化（走同一缓存与分批逻辑）。"""
    return [list(v) for v in get_embedding_function().compute_source_embeddings(texts)]
