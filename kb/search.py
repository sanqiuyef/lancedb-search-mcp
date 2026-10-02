# -*- coding: utf-8 -*-
"""检索内核：官方 hybrid 检索 + 自定义 SiliconFlow reranker。

官方模式：table.search(query, query_type="hybrid") 同时走向量召回与原生
BM25 FTS 召回，由 Reranker 融合排序。默认 RRF；配置 API 精排时使用本模块的
SiliconFlowReranker（官方 Reranker 子类），API 失败自动回退 RRF。
"""

from __future__ import annotations

import sys
from typing import Dict, List

import pyarrow as pa
import requests

from . import config as cfg
from . import schema as kb_schema
from .embeddings import embed_query
from lancedb.rerankers import Reranker, RRFReranker


# =============================================================
# SiliconFlow reranker（官方 Reranker 接口实现）
# =============================================================

class SiliconFlowReranker(Reranker):
    """调用 SiliconFlow /rerank 对融合候选精排；失败时回退 RRF。

    候选可达 40~80 条且每条是完整 chunk（800+ 字），远超 rerank 接口的文档
    数与上下文限制，发送前截断文本并限制条数；候选本身已按召回排序，仅重排
    前 32 条不会改变返回的 top-N 集合。
    """

    def __init__(self, model: str = cfg.RERANK_MODEL, top_n: int = 32,
                 truncate_chars: int = 1024, timeout: int = 30):
        super().__init__()
        self.model = model
        self.top_n = top_n
        self.truncate_chars = truncate_chars
        self.timeout = timeout
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
            print(f"[reranker] API 失败，回退 RRF: {e}", file=sys.stderr)
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
        resp = requests.post(
            cfg.RERANK_URL,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {cfg.SILICONFLOW_API_KEY}"}
                   if cfg.SILICONFLOW_API_KEY else {}),
            },
            json={
                "model": self.model,
                "query": query,
                "documents": texts,
                "top_n": len(texts),
                "return_documents": False,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results = resp.json()["results"]

        # 未参与精排的尾部候选统一给 0 分，保持出现在结果尾部
        scores = [0.0] * combined.num_rows
        for r in results:
            scores[r["index"]] = float(r["relevance_score"])
        combined = combined.append_column(
            "_relevance_score", pa.array(scores, type=pa.float32())
        ).sort_by([("_relevance_score", "descending")])
        return combined


def build_reranker():
    """按配置返回 reranker：api → SiliconFlow 精排；否则官方 RRF。"""
    if cfg.RERANKER_BACKEND == "api" and cfg.SILICONFLOW_API_KEY:
        return SiliconFlowReranker()
    return RRFReranker()


# =============================================================
# 过滤条件拼装
# =============================================================

def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def build_where(project: str = "", source_filter: str = "",
                category_filter: str = "") -> str:
    clauses = []
    if project:
        clauses.append(f"project = '{_sql_escape(project)}'")
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
    project: str = "",
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
    where = build_where(project, source_filter, category_filter)
    mode = (search_mode or "vector").lower()
    if mode == "fts":  # 兼容别名：官方 query_type 与旧工具命名
        mode = "text"
    if mode not in ("vector", "text", "hybrid"):
        mode = "vector"

    trace: Dict = {"mode": mode, "project": project, "limit": limit,
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
            "project": row.get("project", ""),
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

def search_similar(filepath: str, project: str = "", max_results: int = 5) -> List[Dict]:
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
    where = build_where(project)
    if where:
        builder = builder.where(where)
    rows = builder.limit(min(max(max_results, 1), 20) + 1).to_list()

    out = []
    for row in rows[:max_results]:
        out.append({
            "text": row.get("text", ""),
            "source": row.get("source", ""),
            "chunk_index": int(row.get("chunk_index", 0) or 0),
            "category": row.get("category", ""),
            "project": row.get("project", ""),
            "_distance": float(row.get("_distance", 0) or 0),
        })
    return out[:max_results]
