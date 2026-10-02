# -*- coding: utf-8 -*-
"""RAG 问答：检索结构化结果 → SiliconFlow chat 带编号引用作答。"""

from __future__ import annotations

import requests

from . import config as cfg
from . import search as kb_search


SYSTEM_PROMPT = (
    "你是知识库问答助手。只能依据提供的检索上下文回答，回答中必须用引用编号"
    "（如 [1]、[2]）标注依据。上下文不足以回答时明确说不知道，不要编造。"
)


def ask_knowledge(
    query: str,
    limit: int = 5,
    use_reranker: bool = True,
    source_filter: str = "",
    category_filter: str = "",
    search_mode: str = "hybrid",
    chat_model: str = "",
) -> str:
    """基于知识库回答提问，答案附来源引用。"""
    if not cfg.SILICONFLOW_API_KEY:
        return "❌ 未配置 SILICONFLOW_API_KEY，无法调用问答模型。"

    try:
        structured = kb_search.search_structured(
            query=query,
            limit=min(max(limit, 1), 10),
            use_reranker=use_reranker,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
        )
    except Exception as e:
        return f"❌ 检索失败: {e}"

    results = structured["results"]
    if not results:
        return f"知识库中未找到与「{query}」相关的内容，无法回答。"

    context_parts = []
    for i, d in enumerate(results, 1):
        context_parts.append(
            f"[{i}] 来源: {d.get('source', '?')}（chunk #{d.get('chunk_index', 0)}）\n{d.get('text', '')}"
        )
    context = "\n\n".join(context_parts)
    user_prompt = f"问题: {query}\n\n检索到的上下文:\n{context}"

    try:
        resp = requests.post(
            cfg.CHAT_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.SILICONFLOW_API_KEY}",
            },
            timeout=120,
            json={
                "model": chat_model or cfg.CHAT_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 1024,
            },
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"❌ 问答模型调用失败: {e}"

    ref_lines = []
    for i, d in enumerate(results, 1):
        source = d.get("source", "?")
        fname = source.split("\\")[-1] if "\\" in source else source.split("/")[-1]
        ref_lines.append(f"[{i}] {fname} #{d.get('chunk_index', 0)} — {source}")

    return f"📖 **知识库问答** 「{query}」\n\n{answer}\n\n── 引用 ──\n" + "\n".join(ref_lines)
