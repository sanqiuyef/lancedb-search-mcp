# -*- coding: utf-8 -*-
"""检索内核：官方 hybrid 检索 + 本地 CrossEncoder reranker（全本地化）。

官方模式：table.search(query, query_type="hybrid") 同时走向量召回与原生
BM25 FTS 召回，由 Reranker 融合排序。精排用 LocalCrossEncoderReranker
（BAAI/bge-reranker-v2-m3），模型加载或推理失败自动回退 RRF。
"""

from __future__ import annotations

import sys
from typing import Dict, List

import pyarrow as pa

from . import config as cfg
from . import model_lifecycle as lifecycle
from . import schema as kb_schema
from .embeddings import embed_query
from lancedb.rerankers import Reranker, RRFReranker


# =============================================================
# 本地 reranker（CrossEncoder，默认 BAAI/bge-reranker-v2-m3）
# =============================================================

_local_cross_encoder = None


def _unload_cross_encoder() -> bool:
    """卸载持有器中的重排模型；返回是否确实卸载了已加载的模型。"""
    global _local_cross_encoder
    if _local_cross_encoder is None:
        return False
    _local_cross_encoder = None
    return True


lifecycle.register_unloader("reranker:" + cfg.LOCAL_RERANK_MODEL, _unload_cross_encoder)


def _get_cross_encoder():
    """进程级单例：CrossEncoder 权重较大，首次精排时才加载，闲置后自动卸载。"""
    global _local_cross_encoder
    if _local_cross_encoder is None:
        from sentence_transformers import CrossEncoder

        from .embeddings import _resolve_device
        _local_cross_encoder = CrossEncoder(
            cfg.LOCAL_RERANK_MODEL, device=_resolve_device()
        )
    lifecycle.touch()
    return _local_cross_encoder


def reset_local_reranker() -> None:
    """测试用：清空 CrossEncoder 单例。"""
    global _local_cross_encoder
    _local_cross_encoder = None


class LocalCrossEncoderReranker(Reranker):
    """本地 CrossEncoder 对融合候选精排；模型加载或推理失败回退 RRF。

    构造轻量（不触发模型加载），权重在首次 rerank_hybrid 时才载入显存。
    """

    def __init__(self, model: str = "", top_n: int = 32, truncate_chars: int = 1024):
        super().__init__()
        self.model = model or cfg.LOCAL_RERANK_MODEL
        self.top_n = top_n
        self.truncate_chars = truncate_chars
        self.fallback = RRFReranker()

    def rerank_hybrid(
        self,
        query: str,
        vector_results: pa.Table,
        fts_results: pa.Table,
    ) -> pa.Table:
        try:
            return self._rerank(query, vector_results, fts_results)
        except Exception as e:
            print(f"[reranker] 本地精排失败，回退 RRF: {e}", file=sys.stderr)
            return self.fallback.rerank_hybrid(query, vector_results, fts_results)

    def _rerank(
        self,
        query: str,
        vector_results: pa.Table,
        fts_results: pa.Table,
    ) -> pa.Table:
        combined = self.merge_results(vector_results, fts_results)  # 去重合并
        if combined.num_rows == 0:
            return combined

        docs = combined.slice(0, min(self.top_n, combined.num_rows))
        texts = [t[: self.truncate_chars] for t in docs.column("text").to_pylist()]
        scores = _get_cross_encoder().predict(
            [(query, t) for t in texts], show_progress_bar=False
        )

        # 未参与精排的尾部候选统一给 0 分，保持出现在结果尾部
        all_scores = [0.0] * combined.num_rows
        for i, s in enumerate(scores):
            all_scores[i] = float(s)
        combined = combined.append_column(
            "_relevance_score", pa.array(all_scores, type=pa.float32())
        ).sort_by([("_relevance_score", "descending")])
        return combined


def build_reranker():
    """返回本地 CrossEncoder 精排器（全本地化，唯一后端）。"""
    return LocalCrossEncoderReranker()


def reranker_identity() -> str:
    """当前 reranker 描述（不触发模型加载），用于状态展示。"""
    return f"local / {cfg.LOCAL_RERANK_MODEL}（首次检索时加载）"


# =============================================================
# 过滤条件拼装
# =============================================================

def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def build_where(source_filter: str = "", category_filter: str = "") -> str:
    clauses = []
    if source_filter:
        clauses.append(f"source LIKE '%{_sql_escape(source_filter)}%'")
    if category_filter:
        clauses.append(f"category = '{_sql_escape(category_filter)}'")
    return " AND ".join(clauses)


# =============================================================
# 结构化检索（MCP 与桌面端共用）
# =============================================================

def search_structured(
    query: str,
    limit: int = 20,
    use_reranker: bool = True,
    source_filter: str = "",
    category_filter: str = "",
    search_mode: str = "vector",
) -> Dict:
    """返回 {"results": [...], "trace": {...}}，结果含 text/source/chunk_index/得分。"""
    if not kb_schema.table_exists():
        raise ValueError("知识库为空，请先添加文档。")
    table = kb_schema.get_or_create_table()
    where = build_where(source_filter, category_filter)
    mode = (search_mode or "vector").lower()
    if mode == "fts":  # 兼容别名：官方 query_type 与旧工具命名
        mode = "text"
    if mode not in ("vector", "text", "hybrid"):
        mode = "vector"

    trace: Dict = {"mode": mode, "limit": limit,
                   "reranker": "none", "warning": ""}
    try:
        if mode == "hybrid":
            builder = table.search(query, query_type="hybrid")
            reranker = build_reranker() if use_reranker else RRFReranker()
            trace["reranker"] = type(reranker).__name__
            builder = builder.rerank(reranker)
        elif mode == "text":
            builder = table.search(query, query_type="fts")
        else:
            vec = embed_query(query)
            builder = table.search(vec)
        if where:
            builder = builder.where(where, prefilter=(mode != "vector"))
        rows = builder.limit(max(1, limit)).to_list()
    except Exception as e:
        trace["warning"] = str(e)
        raise

    results: List[Dict] = []
    for row in rows:
        item = {
            "text": row.get("text", ""),
            "source": row.get("source", ""),
            "chunk_index": int(row.get("chunk_index", 0) or 0),
            "category": row.get("category", ""),
        }
        if "_relevance_score" in row and row["_relevance_score"] is not None:
            item["relevance_score"] = round(float(row["_relevance_score"]), 4)
        elif "_distance" in row and row["_distance"] is not None:
            item["_distance"] = float(row["_distance"])
            item["relevance_score"] = round(1.0 - float(row["_distance"]), 4)
        results.append(item)
    trace["result_count"] = len(results)
    return {"results": results, "trace": trace}


# =============================================================
# 以文搜文
# =============================================================

def search_similar(filepath: str, max_results: int = 5) -> List[Dict]:
    """以文搜文：参考文档前 512 字符向量化后做纯向量检索。"""
    import os

    from . import ingest as kb_ingest

    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")
    text = kb_ingest.extract_text(filepath)
    if not text or len(text) < 20:
        raise ValueError(f"无法从文件中提取有效文本: {os.path.basename(filepath)}")

    if not kb_schema.table_exists():
        raise ValueError("知识库为空。")
    table = kb_schema.get_or_create_table()
    query_vec = embed_query(text[:512])
    builder = table.search(query_vec)
    rows = builder.limit(min(max(max_results, 1), 20) + 1).to_list()

    out = []
    for row in rows[:max_results]:
        out.append({
            "text": row.get("text", ""),
            "source": row.get("source", ""),
            "chunk_index": int(row.get("chunk_index", 0) or 0),
            "category": row.get("category", ""),
            "_distance": float(row.get("_distance", 0) or 0),
        })
    return out[:max_results]
