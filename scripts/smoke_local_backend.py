# -*- coding: utf-8 -*-
"""本地后端冒烟：按 MCP 注册的环境变量真实加载 bge-m3 + bge-reranker-v2-m3 并推理一次。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from kb import config as cfg  # noqa: E402
from kb import search as ks  # noqa: E402
from kb.embeddings import embed_query, embedding_identity  # noqa: E402

print(f"EMBEDDING_BACKEND = {cfg.EMBEDDING_BACKEND}")
print(f"RERANKER_BACKEND  = {cfg.RERANKER_BACKEND}")
backend, model, dims = embedding_identity()
print(f"embedding_identity: {backend} / {model} / {dims} 维")
print(f"reranker_identity : {ks.reranker_identity()}")

t0 = time.time()
vec = embed_query("BIM 平台如何解析 IFC 文件")
print(f"\n[bge-m3] 加载+推理耗时 {time.time()-t0:.1f}s，向量维度 = {len(vec)}，前3维 = {[round(v, 4) for v in vec[:3]]}")
assert len(vec) == cfg.LOCAL_EMBED_DIM, "维度不符"

t1 = time.time()
reranker = ks.build_reranker()
print(f"\nbuild_reranker -> {type(reranker).__name__} (model={reranker.model})")
ce = ks._get_cross_encoder()
print(f"[bge-reranker] 模型加载耗时 {time.time()-t1:.1f}s, device = {ce.device}")
pairs = [("BIM 平台如何解析 IFC 文件", "IFC 文件解析需要处理几何实体与属性映射。"),
         ("BIM 平台如何解析 IFC 文件", "城市洪涝模拟采用 SWMM 模型。")]
scores = ce.predict(pairs, show_progress_bar=False)
print(f"rerank 得分: 相关={float(scores[0]):.4f}  无关={float(scores[1]):.4f}")
assert float(scores[0]) > float(scores[1]), "相关文档得分应更高"

print("\n✅ 本地后端冒烟通过")
