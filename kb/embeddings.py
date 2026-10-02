# -*- coding: utf-8 -*-
"""Embedding 后端：本地 sentence-transformers（全本地化，2026-10-02 起）。

基于 lancedb.embeddings 官方框架：`@register("local-sentence-transformers")`
注册条目配合 LanceModel 的 SourceField/VectorField 在 table.add() 与查询时
自动向量化；权重放模块持有器，由 kb_model_lifecycle 闲置卸载。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import List, Union

import numpy as np

from lancedb.embeddings import TextEmbeddingFunction, get_registry, register

from . import config as cfg
from . import model_lifecycle as lifecycle


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


# =============================================================
# 本地 sentence-transformers（懒加载 + 闲置自动卸载）
# =============================================================

# 权重持有器独立于函数实例：schema（LanceModel）会长期持有函数对象，
# 若权重挂在实例上，闲置卸载时显存放不掉。
_st_holder: dict = {"model": None}


def _load_st_model(name: str, device: str, trust_remote_code: bool = True):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(name, device=device, trust_remote_code=trust_remote_code)
    lifecycle.touch()
    return model


def _unload_st_model() -> bool:
    """卸载持有器中的嵌入模型；返回是否确实卸载了已加载的模型。"""
    if _st_holder["model"] is None:
        return False
    _st_holder["model"] = None
    return True


lifecycle.register_unloader("embedding:" + cfg.LOCAL_EMBED_MODEL, _unload_st_model)


@register("local-sentence-transformers")
class LocalSentenceTransformerEmbeddings(TextEmbeddingFunction):
    """本地 sentence-transformers 嵌入（默认 BAAI/bge-m3）。

    与官方同名条目的区别：① ndims() 直接返回配置维度，不再加载模型探测；
    ② 权重放模块持有器，支持闲置自动卸载与测试注入假模型。
    """

    name: str = cfg.LOCAL_EMBED_MODEL
    dimensions: int = cfg.LOCAL_EMBED_DIM
    device: str = "cpu"
    normalize: bool = True
    trust_remote_code: bool = True

    def ndims(self) -> int:
        return self.dimensions

    def generate_embeddings(
        self, texts: Union[List[str], np.ndarray]
    ) -> List[np.array]:
        if _st_holder["model"] is None:
            _st_holder["model"] = _load_st_model(self.name, self.device, self.trust_remote_code)
        lifecycle.touch()
        vectors = _st_holder["model"].encode(
            [str(t) for t in list(texts)],
            convert_to_numpy=True,
            normalize_embeddings=self.normalize,
        )
        return [np.asarray(v, dtype=np.float32) for v in vectors]


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
    """返回本地 sentence-transformers EmbeddingFunction（全本地化，唯一后端）。"""
    global _embedding_func
    with _embedding_lock:
        if _embedding_func is not None:
            return _embedding_func
        func = (
            get_registry()
            .get("local-sentence-transformers")
            .create(name=cfg.LOCAL_EMBED_MODEL, dimensions=cfg.LOCAL_EMBED_DIM,
                    device=_resolve_device())
        )
        _embedding_func = func
        return func


def embedding_identity() -> tuple[str, str, int]:
    """(后端, 模型, 维度)，用于状态展示与索引一致性检查。"""
    return ("local", cfg.LOCAL_EMBED_MODEL, cfg.LOCAL_EMBED_DIM)


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
