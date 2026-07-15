# -*- coding: utf-8 -*-
"""Build document-level semantic graphs from one or more LanceDB knowledge bases."""

import argparse
import html
import json
import os
from collections import Counter, defaultdict
from typing import Dict, Iterable, List

import lancedb
import numpy as np

from knowledge_browser_core import load_kb_config


TABLE_NAME = "my_docs"


def _label(source: str, limit: int = 34) -> str:
    name = os.path.splitext(os.path.basename(source))[0]
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _extension(source: str) -> str:
    ext = os.path.splitext(source)[1].lower().lstrip(".")
    return ext or "other"


def load_knowledge_bases(config_path: str) -> List[Dict[str, str]]:
    """Read configured knowledge bases and retain one entry per physical database."""
    configured = load_kb_config(config_path, repair=True)[0].get("knowledge_bases", {})

    bases = []
    seen_paths = set()
    for name, info in configured.items():
        path = os.path.normpath(info.get("path", ""))
        path_key = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        if not path or path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        bases.append({
            "name": name,
            "path": path,
            "description": info.get("description", ""),
        })
    return bases


def available_knowledge_bases(bases: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """Return configured bases that currently contain the document table."""
    available = []
    for base in bases:
        try:
            db = lancedb.connect(base["path"])
            if TABLE_NAME in db.table_names():
                available.append(base)
        except Exception:
            continue
    return available


def _load_documents(bases: Iterable[Dict[str, str]]):
    """Read document chunks and aggregate their vectors into one vector per source file."""
    documents = []
    total_chunks = 0
    for base in bases:
        db = lancedb.connect(base["path"])
        if TABLE_NAME not in db.table_names():
            continue
        table = db.open_table(TABLE_NAME)
        data = table.to_lance().to_table(columns=["source", "category", "vector"])
        sources = data.column("source").to_pylist()
        if not sources:
            continue

        categories = data.column("category").to_pylist()
        vector_column = data.column("vector").combine_chunks()
        dimension = vector_column.type.list_size
        vectors = np.asarray(vector_column.values).reshape(len(sources), dimension).astype(np.float32)
        total_chunks += len(sources)

        source_index = {}
        chunk_counts = Counter()
        category_counts = defaultdict(Counter)
        row_source_ids = np.empty(len(sources), dtype=np.int32)
        for row, (source, category) in enumerate(zip(sources, categories)):
            if source not in source_index:
                source_index[source] = len(source_index)
            source_id = source_index[source]
            row_source_ids[row] = source_id
            chunk_counts[source] += 1
            if category:
                category_counts[source][category] += 1

        sums = np.zeros((len(source_index), dimension), dtype=np.float32)
        np.add.at(sums, row_source_ids, vectors)
        counts = np.bincount(row_source_ids, minlength=len(source_index)).astype(np.float32)
        document_vectors = sums / counts[:, None]
        norms = np.linalg.norm(document_vectors, axis=1, keepdims=True)
        document_vectors = document_vectors / np.maximum(norms, 1e-12)

        for source, source_id in source_index.items():
            documents.append({
                "id": f"{base['name']}::{source}",
                "label": _label(source),
                "title": source,
                "source": source,
                "knowledge_base": base["name"],
                "knowledge_base_description": base.get("description", ""),
                "chunks": chunk_counts[source],
                "category": category_counts[source].most_common(1)[0][0] if category_counts[source] else "未分类",
                "group": _extension(source),
                "value": max(8, min(36, 8 + np.sqrt(chunk_counts[source]) * 1.5)),
                "vector": document_vectors[source_id],
            })
    return documents, total_chunks


def _semantic_edges(vectors: np.ndarray, max_neighbors: int, min_similarity: float):
    """Find nearest document neighbours without materialising an N x N similarity matrix."""
    edges = {}
    if len(vectors) < 2:
        return edges
    neighbor_count = min(max_neighbors, len(vectors) - 1)

    block_size = 512
    for start in range(0, len(vectors), block_size):
        scores = vectors[start:start + block_size] @ vectors.T
        for row, row_scores in enumerate(scores):
            source_id = start + row
            row_scores[source_id] = -1
            candidates = np.argpartition(row_scores, -neighbor_count)[-neighbor_count:]
            for target_id in candidates:
                score = float(row_scores[target_id])
                if score < min_similarity:
                    continue
                key = tuple(sorted((source_id, int(target_id))))
                edges[key] = max(edges.get(key, 0), score)
    return edges


def build_graphs(
    bases: Iterable[Dict[str, str]],
    max_neighbors: int = 2,
    min_similarity: float = 0.38,
) -> dict:
    """Return a cross-knowledge-base document graph with semantic edges."""
    documents, total_chunks = _load_documents(bases)
    if not documents:
        return {"nodes": [], "edges": [], "chunks": 0, "knowledge_bases": []}

    vectors = np.stack([document.pop("vector") for document in documents])
    semantic_edges = _semantic_edges(vectors, max_neighbors, min_similarity)
    return {
        "nodes": documents,
        "edges": [
            {
                "from": documents[left]["id"],
                "to": documents[right]["id"],
                "similarity": round(score, 3),
                "value": round(score * 3, 2),
                "kind": "semantic",
            }
            for (left, right), score in sorted(semantic_edges.items())
        ],
        "chunks": total_chunks,
        "knowledge_bases": sorted({document["knowledge_base"] for document in documents}),
        "extensions": dict(Counter(document["group"] for document in documents)),
    }


def build_graph(db_path: str, max_neighbors: int = 2, min_similarity: float = 0.38) -> dict:
    """Backwards-compatible single-knowledge-base graph builder."""
    return build_graphs(
        [{"name": os.path.basename(os.path.normpath(db_path)) or "知识库", "path": db_path, "description": ""}],
        max_neighbors=max_neighbors,
        min_similarity=min_similarity,
    )


def render_html(graph: dict, title: str) -> str:
    """Render the original portable, single-file graph view."""
    payload = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title><script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>body{{margin:0;font:14px system-ui,"Microsoft YaHei",sans-serif;color:#1f2937;background:#f8fafc}}header{{padding:20px 28px 14px;background:#fff;border-bottom:1px solid #e2e8f0}}h1{{margin:0 0 7px;font-size:21px}}#summary{{color:#475569}}#network{{height:calc(100vh - 102px);min-height:580px}}#detail{{position:fixed;right:18px;bottom:18px;width:min(360px,calc(100vw - 36px));padding:14px 16px;background:#fff;border:1px solid #cbd5e1;border-radius:10px;box-shadow:0 10px 25px #0f172a24;line-height:1.55}}.muted{{color:#64748b;font-size:12px;word-break:break-all}}</style>
</head><body><header><h1>{safe_title}</h1><div id="summary"></div></header><main id="network"></main><aside id="detail"><strong>点击一个文档节点</strong><div class="muted">节点大小表示文本块数量；连线代表文档平均向量的语义相近关系。</div></aside>
<script>const graph={payload};const nodes=new vis.DataSet(graph.nodes);const edges=new vis.DataSet(graph.edges.map(edge=>({{...edge,title:`语义相似度：${{edge.similarity}}`}})));document.getElementById('summary').textContent=`文档 ${{graph.nodes.length}} 个 · 文本块 ${{graph.chunks.toLocaleString()}} 个 · 语义关联 ${{graph.edges.length}} 条`;const network=new vis.Network(document.getElementById('network'),{{nodes,edges}},{{interaction:{{hover:true,navigationButtons:true,keyboard:true}},physics:{{stabilization:{{iterations:180}},barnesHut:{{gravitationalConstant:-6500,springLength:145}}}},nodes:{{shape:'dot',font:{{size:13,face:'Microsoft YaHei'}},borderWidth:1}},edges:{{color:{{color:'#94a3b8',highlight:'#2563eb'}},width:1,smooth:{{type:'continuous'}}}}}});network.on('click',params=>{{if(!params.nodes.length)return;const node=nodes.get(params.nodes[0]);document.getElementById('detail').innerHTML=`<strong>${{node.label}}</strong><div>${{node.chunks}} 个文本块 · ${{node.category}} · .${{node.group}}</div><div class="muted">${{node.source}}</div>`}});</script></body></html>"""


def export_graph(db_path: str, output_path: str, title: str = "知识库语义地图") -> dict:
    graph = build_graph(db_path)
    with open(output_path, "w", encoding="utf-8") as output:
        output.write(render_html(graph, title))
    return graph


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出单个 LanceDB 知识库的语义地图")
    parser.add_argument("--db", required=True, help="LanceDB 数据库路径")
    parser.add_argument("--output", required=True, help="输出 HTML 文件路径")
    parser.add_argument("--title", default="知识库语义地图")
    args = parser.parse_args()
    graph = export_graph(args.db, args.output, args.title)
    print(f"已生成 {args.output}: {len(graph['nodes'])} 个文档，{len(graph['edges'])} 条语义关联")
