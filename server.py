# -*- coding: utf-8 -*-
"""LanceDB 知识搜索 MCP Server —— 整改后的薄入口（16 个工具）。

内核全部基于 lancedb 0.39 官方 API：
  - LanceModel schema + embedding 注册表（kb_schema / kb_embeddings）
  - 原生 BM25 FTS（jieba 分词）+ 官方 hybrid 检索（kb_search）
  - 官方 Reranker 接口实现 SiliconFlow 精排（kb_search.SiliconFlowReranker）

自建模块（待后期单独优化，文件头有标记）：kb_web / kb_watcher /
content_graph / knowledge_browser_core / knowledge_graph*（桌面端）。
"""

import json
import os
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import ToolAnnotations

import kb_ask
import kb_config as cfg
import kb_ingest
import kb_schema
import kb_search
import kb_watcher
import kb_web
from content_graph import ContentGraphStore, extractor_from_environment

mcp = FastMCP("LanceDB 知识搜索", port=8002)


def _resolve_project(project: str) -> str:
    """入参分区名 → 规范化（未注册的名称视为跨全部分区）。"""
    return cfg.normalize_project(project)


def _fmt_trace(trace: dict) -> str:
    parts = [f"模式: {trace.get('mode', '?')}"]
    if trace.get("reranker") and trace["reranker"] != "none":
        parts.append(f"Reranker: {trace['reranker']}")
    if trace.get("warning"):
        parts.append(f"⚠️ {trace['warning']}")
    return " | ".join(parts)


def _fmt_results(query: str, structured: dict, limit: int, snippet: bool = True) -> str:
    docs = structured["results"]
    if not docs:
        return f"未搜索到与「{query}」相关的结果。"
    shown = docs[: max(1, limit)]
    lines = [
        f"🔎 **知识库搜索结果** 「{query}」",
        f"   {_fmt_trace(structured['trace'])} | 共 {len(docs)} 条（显示前 {len(shown)} 条）",
        "─" * 60,
    ]
    for i, d in enumerate(shown, 1):
        source = d.get("source", "?")
        fname = source.split("\\")[-1] if "\\" in source else source.split("/")[-1]
        text = d.get("text", "")
        if snippet and len(text) > 500:
            cut = text[:500]
            last = max(cut.rfind(". "), cut.rfind("。"), cut.rfind("\n"), cut.rfind(" "))
            text = (cut[: last + 1] if last > 200 else cut) + "..."
        score = d.get("relevance_score")
        score_str = f" ★{score:.4f}" if score is not None else ""
        cat = f" [{d['category']}]" if d.get("category") else ""
        proj = f" 📁{d['project']}" if d.get("project") else ""
        lines.append(
            f"{i}. **{fname}** #{d.get('chunk_index', 0)}{score_str}{cat}{proj}\n"
            f"   来源: {source}\n   {text}"
        )
    return "\n\n".join(lines)


# =============================================================
# 检索与问答
# =============================================================

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_knowledge",
    description="搜索本地文档知识库。支持向量搜索、关键词搜索、混合搜索（官方 hybrid）。可指定分区、来源过滤、类别过滤和片段模式。",
)
def search_knowledge(
    query: Annotated[str, "搜索关键词/问题"],
    project: Annotated[str, "分区过滤（如 'BIMbase'、'小论文（北松区）'），空则跨全部分区"] = "",
    limit: Annotated[int, "返回结果数（1-20）"] = 5,
    use_reranker: Annotated[bool, "是否使用 Reranker 精排（默认开启）"] = True,
    source_filter: Annotated[str, "来源过滤（如 'paper.pdf' 或 'notes/'）"] = "",
    search_mode: Annotated[str, "搜索模式 - vector / text / hybrid"] = "vector",
    snippet_mode: Annotated[bool, "是否截断结果为片段（默认开启，减少 token 消耗）"] = True,
    category_filter: Annotated[str, "类别过滤（如 'paper'、'code'）"] = "",
) -> str:
    limit = min(max(limit, 1), 20)
    try:
        structured = kb_search.search_structured(
            query=query,
            project=_resolve_project(project),
            limit=limit,
            use_reranker=use_reranker,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
        )
    except Exception as error:
        return f"❌ 搜索失败: {error}"
    return _fmt_results(query, structured, limit, snippet_mode)


def search_knowledge_structured(
    query: str,
    project: str = "",
    search_mode: str = "vector",
    use_reranker: bool = True,
    source_filter: str = "",
    category_filter: str = "",
    limit: int = 20,
) -> dict:
    """桌面浏览器兼容入口：把官方内核结果映射为旧版 payload 形状。"""
    structured = kb_search.search_structured(
        query=query,
        project=_resolve_project(project),
        limit=max(1, limit),
        use_reranker=use_reranker,
        source_filter=source_filter,
        category_filter=category_filter,
        search_mode=search_mode,
    )
    trace = structured.get("trace", {})
    return {
        "results": structured["results"],
        "mode": trace.get("mode", search_mode),
        "candidate_count": len(structured["results"]),
        "reranker_status": trace.get("reranker") or "none",
        "expanded_query": query,
        "warning": trace.get("warning", ""),
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="ask_knowledge",
    description="基于知识库检索结果回答提问，答案附来源引用（[1] 文件 chunk#N）。检索不到相关内容时明确说明，不编造。",
)
def ask_knowledge(
    query: Annotated[str, "要回答的问题"],
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
    limit: Annotated[int, "检索条数（1-10）"] = 5,
    use_reranker: Annotated[bool, "是否使用 Reranker 精排"] = True,
    source_filter: Annotated[str, "来源过滤（如 'paper.pdf'）"] = "",
    category_filter: Annotated[str, "类别过滤（如 'paper'）"] = "",
    search_mode: Annotated[str, "搜索模式 - vector / text / hybrid"] = "hybrid",
    chat_model: Annotated[str, "作答模型名，空则用默认 CHAT_MODEL"] = "",
) -> str:
    return kb_ask.ask_knowledge(
        query=query,
        project=project,
        limit=limit,
        use_reranker=use_reranker,
        source_filter=source_filter,
        category_filter=category_filter,
        search_mode=search_mode,
        chat_model=chat_model,
    )


# =============================================================
# 知识库管理
# =============================================================

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_knowledge_status",
    description="查看知识库状态：文档数量、分区分布、索引状态、embedding 配置等信息。",
)
def get_knowledge_status() -> str:
    from kb_embeddings import embedding_identity

    if not kb_schema.table_exists():
        backend, model, dims = embedding_identity()
        return (
            f"❌ 知识库为空（库路径: {cfg.resolve_db_path()}）\n"
            f"Embedding: {backend} / {model} / {dims} 维"
        )
    table = kb_schema.get_or_create_table()
    arrow = table.to_lance().to_table(columns=["source", "project", "category"])
    sources = arrow.column("source").to_pylist()
    projects = arrow.column("project").to_pylist()
    categories = arrow.column("category").to_pylist()

    dist: dict[str, int] = {}
    cat_dist: dict[str, int] = {}
    for proj, cat in zip(projects, categories):
        dist[proj or "（未分区）"] = dist.get(proj or "（未分区）", 0) + 1
        if cat:
            cat_dist[cat] = cat_dist.get(cat, 0) + 1

    index_names = {i.name for i in table.list_indices()}
    backend, model, dims = embedding_identity()
    lines = [
        "📊 **知识库状态**",
        "─" * 40,
        f"库路径: {cfg.resolve_db_path()}",
        f"总 chunk 数: {len(table)} | 文档数: {len(set(sources))}",
        f"Embedding: {backend} / {model} / {dims} 维",
        f"索引: {', '.join(sorted(index_names)) or '（无）'}",
        "",
        "分区分布:",
    ]
    for proj, cnt in sorted(dist.items(), key=lambda x: -x[1]):
        lines.append(f"  - {proj}: {cnt} chunks")
    if cat_dist:
        lines.append("类别分布: " + " | ".join(
            f"{k}: {v}" for k, v in sorted(cat_dist.items(), key=lambda x: -x[1])
        ))
    lines.append(f"注册表分区: {', '.join(cfg.list_projects()) or '（无）'}")
    return "\n".join(lines)


@mcp.tool(
    name="add_documents",
    description="扫描指定目录，将文档（.md/.pdf/.docx/.txt 等）向量化后存入知识库。",
)
def add_documents(
    scan_dir: Annotated[str, "要扫描的目录路径"],
    project: Annotated[str, "目标分区名，空则按源目录推断或归（全部）"] = "",
    reindex_all: Annotated[bool, "是否清空该分区后重新添加（默认只增量）"] = False,
) -> str:
    return kb_ingest.add_documents(scan_dir, project=project, reindex_all=reindex_all)


@mcp.tool(
    name="add_single_document",
    description="向量化单个文档到知识库（一次只处理一个文件，避免大批量超时）。支持 .md/.pdf/.docx/.txt 等格式。",
)
def add_single_document(
    filepath: Annotated[str, "文件路径"],
    project: Annotated[str, "目标分区名，空则按源目录推断"] = "",
    scan_dir: Annotated[str, "可选，来源基准目录（source 记录为相对路径）"] = "",
) -> str:
    return kb_ingest.add_single_document(filepath, project=project, scan_dir=scan_dir)


@mcp.tool(
    name="update_document",
    description="更新知识库中的文档（删除旧版本后重新索引）。用于文档内容变更后的重新索引。",
)
def update_document(
    filepath: Annotated[str, "文件路径"],
    project: Annotated[str, "分区过滤，空则按源目录推断"] = "",
) -> str:
    return kb_ingest.update_document(filepath, project=project)


@mcp.tool(
    name="delete_documents",
    description="删除知识库中的文档。按来源文件名/路径模式删除，或清空整个知识库。",
)
def delete_documents(
    source_pattern: Annotated[str, "来源匹配模式（如 'paper.pdf'、'notes/'），留空则需 confirm_all"] = "",
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
    confirm_all: Annotated[bool, "确认清空（指定 project 时只清空该分区）"] = False,
) -> str:
    return kb_ingest.delete_documents(source_pattern, project=project, confirm_all=confirm_all)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="list_documents",
    description="列出知识库中的所有文档及其统计信息（chunk 数、类别等）。",
)
def list_documents(
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
    category_filter: Annotated[str, "按类别过滤（如 'paper'）"] = "",
    limit: Annotated[int, "最大返回文档数（1-200）"] = 50,
) -> str:
    return kb_ingest.list_documents(project, category_filter, limit)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_document",
    description="获取知识库中文档的完整内容（按 chunk 顺序拼接）。用于查看被片段模式截断的原文。",
)
def get_document(
    filepath: Annotated[str, "文档路径或文件名（支持模糊匹配）"],
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
) -> str:
    project = _resolve_project(project)
    if not kb_schema.table_exists():
        return "❌ 知识库为空。"
    table = kb_schema.get_or_create_table()
    all_sources = kb_schema.existing_sources(table, project)
    matched = {s for s in all_sources if filepath.casefold() in (s or "").casefold()}
    if not matched:
        scope = f"分区「{project}」" if project else "知识库"
        return f"未找到匹配「{filepath}」的文档（{scope}）。"
    matched = set(sorted(matched)[:5])  # 防止一次拉取过多文档
    groups = kb_ingest.fetch_document_text(table, matched)
    lines = []
    for src, chunks in groups.items():
        lines.append(f"# 文档: {src}（{len(chunks)} 块）")
        lines.append("─" * 40)
        lines.extend(ch["text"] for ch in chunks)
        lines.append("")
    return "\n".join(lines).strip()


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_similar",
    description="基于嵌入相似度查找与给定文档最相似的文档（以文搜文）。",
)
def search_similar(
    filepath: Annotated[str, "参考文档路径"],
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
    max_results: Annotated[int, "返回相似文档数（1-20）"] = 5,
) -> str:
    try:
        results = kb_search.search_similar(filepath, project=_resolve_project(project),
                                           max_results=max_results)
    except (FileNotFoundError, ValueError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ 搜索失败: {e}"
    if not results:
        return "未找到相似文档。"
    lines = [f"🔍 **相似文档搜索** 参考: {os.path.basename(filepath)}", "─" * 60]
    for i, r in enumerate(results, 1):
        source = r.get("source", "?")
        fname = source.split("\\")[-1] if "\\" in source else source.split("/")[-1]
        similarity = 1.0 - r.get("_distance", 0.0)
        snippet = r.get("text", "")[:200].replace("\n", " ")
        lines.append(f"{i}. **{fname}** 相似度: {similarity:.4f}\n   {snippet}...")
    return "\n\n".join(lines)


# =============================================================
# 自建能力：网页入库 / 目录监听
# =============================================================

@mcp.tool(
    name="ingest_url",
    description="抓取网页内容并索引到知识库。支持 HTML 页面、Markdown、纯文本等。",
)
def ingest_url(
    url: Annotated[str, "要抓取的网页 URL"],
    project: Annotated[str, "目标分区名，空则按源目录推断"] = "",
) -> str:
    return kb_web.ingest_url(url, project=project)


@mcp.tool(
    name="start_watcher",
    description="启动文件变更监听，当文档目录中的文件被修改/新增/移动时自动重新索引。需要安装 watchdog 依赖。",
)
def start_watcher(
    watch_dir: Annotated[str, "要监听的目录（默认当前工作目录）"] = "",
    project: Annotated[str, "写入分区，空则按监听目录推断"] = "",
) -> str:
    return kb_watcher.start_watcher(watch_dir, project=project)


@mcp.tool(
    name="stop_watcher",
    description="停止文件变更监听。",
)
def stop_watcher() -> str:
    return kb_watcher.stop_watcher()


# =============================================================
# 自建能力：内容图谱（GraphRAG）
# =============================================================

_graph_store = None


def _graph_store_instance() -> ContentGraphStore:
    global _graph_store
    if _graph_store is None:
        _graph_store = ContentGraphStore(cfg.CONTENT_GRAPH_PATH)
    return _graph_store


def _content_graph_document(filepath: str, project: str = "") -> dict:
    """精确定位一个来源并读取图谱输入列；返回 {knowledge_base, source, chunks}。"""
    project = _resolve_project(project)
    knowledge_base = project or "global"
    if not kb_schema.table_exists():
        raise ValueError("知识库为空。")
    table = kb_schema.get_or_create_table()
    sources = kb_schema.existing_sources(table, project)
    exact = [s for s in sources if s == filepath]
    if not exact:
        exact = [s for s in sources if s.casefold() == filepath.casefold()]
    if not exact:
        exact = [s for s in sources
                 if os.path.basename(s).casefold() == os.path.basename(filepath).casefold()]
    if not exact:
        exact = [s for s in sources if filepath.casefold() in (s or "").casefold()]
    exact = sorted(set(exact))
    if not exact:
        raise ValueError(f"未找到匹配「{filepath}」的文档。")
    if len(exact) > 1:
        preview = "\n".join(f"- {s}" for s in exact[:10])
        raise ValueError(f"来源匹配不唯一，请传入完整来源路径：\n{preview}")
    source = exact[0]
    safe_source = source.replace("'", "''")
    arrow = table.to_lance().scanner(
        columns=["source", "text", "chunk_index", "category"],
        filter=f"source = '{safe_source}'",
    ).to_table()
    chunks = [
        {"source": s, "text": text or "", "chunk_index": int(idx), "category": cat or ""}
        for s, text, idx, cat in zip(
            arrow.column("source").to_pylist(),
            arrow.column("text").to_pylist(),
            arrow.column("chunk_index").to_pylist(),
            arrow.column("category").to_pylist(),
        ) if s == source
    ]
    chunks.sort(key=lambda item: item["chunk_index"])
    return {"knowledge_base": knowledge_base, "source": source, "chunks": chunks}


@mcp.tool(
    name="build_content_graph",
    description="为一个已入库文档构建或增量更新内容级知识图谱（实体/方法/观点/证据关系，写入独立 SQLite）。",
)
def build_content_graph(
    filepath: Annotated[str, "知识库中的文档路径或文件名"],
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
    force: Annotated[bool, "内容未变化时是否仍强制重建"] = False,
) -> str:
    try:
        resolved = _content_graph_document(filepath, project)
        extractor = extractor_from_environment()
        store = _graph_store_instance()
        result = store.index_document(
            resolved["knowledge_base"], resolved["source"], resolved["chunks"],
            extractor, force=force,
        )
        result.update({
            "knowledge_base": resolved["knowledge_base"], "source": resolved["source"],
            "extractor": extractor.name,
        })
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as error:
        return f"❌ 内容图谱构建失败: {error}"


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_content_graph",
    description="读取某个文档已构建的内容图谱，返回实体、观点、关系、置信度和证据 Chunk。不会触发模型调用。",
)
def get_content_graph(
    filepath: Annotated[str, "知识库中的文档路径或文件名"],
    project: Annotated[str, "分区过滤，空则跨全部分区"] = "",
    max_nodes: Annotated[int, "最大返回节点数（10-500，默认 120）"] = 120,
) -> str:
    try:
        resolved = _content_graph_document(filepath, project)
    except Exception as error:
        return f"❌ {error}"
    graph = _graph_store_instance().get_document_graph(
        resolved["knowledge_base"], resolved["source"], limit=max(10, min(max_nodes, 500))
    )
    if not graph["document_id"]:
        return "当前文档尚未构建内容图谱。请先调用 build_content_graph。"
    return json.dumps(graph, ensure_ascii=False, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_content_graph",
    description="按名称搜索内容图谱中的实体、方法、观点、主题或数据集；expand=True 时以命中节点为种子展开一跳邻居与关系边（GraphRAG 式检索）。",
)
def search_content_graph(
    query: Annotated[str, "搜索关键词"],
    kind: Annotated[str, "节点类型过滤（entity/method/claim/topic/dataset）"] = "",
    project: Annotated[str, "分区过滤（按节点来源位置过滤）"] = "",
    limit: Annotated[int, "最大返回节点数（默认 20）"] = 20,
    expand: Annotated[bool, "是否展开一跳邻居与关系边"] = False,
    max_neighbors: Annotated[int, "展开时的最大邻居数（默认 40）"] = 40,
) -> str:
    try:
        store = _graph_store_instance()
        if expand:
            payload = store.search_subgraph(
                query, kind, limit=max(1, min(limit, 50)), max_neighbors=max_neighbors
            )
            return json.dumps({"query": query, "expand": True, **payload},
                              ensure_ascii=False, indent=2)
        results = store.search_nodes(query, kind, limit=max(1, min(limit, 100)))
        if project:
            for item in results:
                item["locations"] = [
                    loc for loc in item["locations"]
                    if loc["knowledge_base"] == _resolve_project(project)
                ]
            results = [item for item in results if item["locations"]]
        return json.dumps(
            {"query": query, "count": len(results), "results": results},
            ensure_ascii=False, indent=2,
        )
    except Exception as error:
        return f"❌ 内容图谱搜索失败: {error}"


# =============================================================
# 注册表工具（桌面端共用）
# =============================================================

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="list_knowledge_bases",
    description="列出 kb-config.json 注册表中的全部分区（名称、描述、源目录）。",
)
def list_knowledge_bases() -> str:
    registry = cfg.load_registry()
    lines = ["📚 **知识库分区注册表**", "─" * 40]
    for name, info in registry.get("projects", {}).items():
        desc = info.get("description", "") if isinstance(info, dict) else ""
        roots = ", ".join(info.get("source_roots", [])) if isinstance(info, dict) else ""
        lines.append(f"  - {name}: {desc}" + (f"\n      源目录: {roots}" if roots else ""))
    lines.append(f"\n默认库: {registry.get('db_path') or cfg.DEFAULT_DB_PATH}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
