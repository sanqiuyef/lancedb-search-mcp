# -*- coding: utf-8 -*-
"""RAG 证据检索：本地混合检索 + 精排 → 编号证据块（答问由调用方模型完成）。

2026-10-02 全本地化：本包不做任何模型生成调用。bge-m3 检索、bge-reranker
精排（均在 D:\\huggingface 本地缓存），检索到的证据以 [1]、[2] 编号块返回，
由调用方（MCP 宿主模型）基于证据作答并标注引用。
"""

from __future__ import annotations

from . import search as kb_search


def ask_knowledge(
    query: str,
    limit: int = 5,
    use_reranker: bool = True,
    source_filter: str = "",
    category_filter: str = "",
    search_mode: str = "hybrid",
) -> dict:
    """检索回答「问题」所需的证据块，返回 {"evidence": [...], "evidence_block": str}。

    evidence_block 是可直接嵌进提示词的编号证据文本（[1] 来源 + 全文 chunk），
    调用方模型应只依据证据作答，并在回答中标注 [n] 引用；证据不足时明确说明。
    """
    structured = kb_search.search_structured(
        query=query,
        limit=min(max(limit, 1), 10),
        use_reranker=use_reranker,
        source_filter=source_filter,
        category_filter=category_filter,
        search_mode=search_mode,
    )
    results = structured["results"]
    evidence = []
    for i, d in enumerate(results, 1):
        evidence.append({
            "cite": f"[{i}]",
            "source": d.get("source", "?"),
            "chunk_index": d.get("chunk_index", 0),
            "category": d.get("category", ""),
            "text": d.get("text", ""),
        })

    lines = [f"知识库证据（问题：「{query}」，共 {len(evidence)} 条）：", ""]
    for ev in evidence:
        lines.append(f"{ev['cite']} 来源: {ev['source']}（chunk #{ev['chunk_index']}）")
        lines.append(ev["text"])
        lines.append("")
    if not evidence:
        lines = [f"知识库中未找到与「{query}」相关的证据。"]
    return {"evidence": evidence, "evidence_block": "\n".join(lines).strip()}
