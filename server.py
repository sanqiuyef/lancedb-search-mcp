# -*- coding: utf-8 -*-
"""LanceDB 知识搜索 MCP Server —— 整改后的薄入口（13 个工具）。

单库扁平结构（2026-10-02 用户决定移除分区机制）：一个 LanceDB 库、一个池子，
无 project 列与注册表；类别（category）按源目录自动标注，仍可作过滤条件。

内核全部基于 lancedb 0.39 官方 API：
  - LanceModel schema + embedding 注册表（kb_schema / kb_embeddings）
  - 原生 BM25 FTS（jieba 分词）+ 官方 hybrid 检索（kb_search）
  - 本地模型用时挂载、闲置自动卸载（kb_model_lifecycle）

自建模块（待后期单独优化，文件头有标记）：kb_web（网页入库）/ kb_watcher（目录监听）。
"""

import json
import os
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import ToolAnnotations

from kb import ask as kb_ask
from kb import config as cfg
from kb import ingest as kb_ingest
from kb import schema as kb_schema
from kb import search as kb_search
from kb import watcher as kb_watcher
from kb import web as kb_web

# 启动即预导入 sentence_transformers（连带 torch/sklearn/scipy 全链）：
# 事件循环启动后再懒加载会卡死在 scipy BLAS DLL 导入上，导致首轮搜索无限挂起（2026-11-05 实测）。
import sentence_transformers  # noqa: E402

mcp = FastMCP("LanceDB 知识搜索", port=8002)


def _residency_policy() -> str:
    timeout = cfg.LOCAL_MODEL_IDLE_UNLOAD
    if timeout <= 0:
        return "常驻（不自动卸载）"
    return f"用时挂载，闲置 {timeout}s 后自动卸载显存"


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
        lines.append(
            f"{i}. **{fname}** #{d.get('chunk_index', 0)}{score_str}{cat}\n"
            f"   来源: {source}\n   {text}"
        )
    return "\n\n".join(lines)


# =============================================================
# 检索与问答
# =============================================================

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_knowledge",
    description="搜索本地文档知识库。支持向量搜索、关键词搜索、混合搜索（官方 hybrid）。可指定来源过滤、类别过滤和片段模式。",
)
def search_knowledge(
    query: Annotated[str, "搜索关键词/问题"],
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
            limit=limit,
            use_reranker=use_reranker,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
        )
    except Exception as error:
        return f"❌ 搜索失败: {error}"
    return _fmt_results(query, structured, limit, snippet_mode)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="ask_knowledge",
    description="基于知识库检索回答问题所需的编号证据块（[1] 来源 + 全文 chunk）。请只依据证据作答并标注 [n] 引用；证据不足时明确说明，不编造。",
)
def ask_knowledge(
    query: Annotated[str, "要回答的问题"],
    limit: Annotated[int, "检索条数（1-10）"] = 5,
    use_reranker: Annotated[bool, "是否使用 Reranker 精排"] = True,
    source_filter: Annotated[str, "来源过滤（如 'paper.pdf'）"] = "",
    category_filter: Annotated[str, "类别过滤（如 'paper'）"] = "",
    search_mode: Annotated[str, "搜索模式 - vector / text / hybrid"] = "hybrid",
) -> str:
    result = kb_ask.ask_knowledge(
        query=query,
        limit=limit,
        use_reranker=use_reranker,
        source_filter=source_filter,
        category_filter=category_filter,
        search_mode=search_mode,
    )
    return result["evidence_block"]


# =============================================================
# 知识库管理
# =============================================================

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_knowledge_status",
    description="查看知识库状态：文档数量、类别分布、索引状态、模型配置与驻留策略等。",
)
def get_knowledge_status() -> str:
    from kb.embeddings import embedding_identity

    if not kb_schema.table_exists():
        backend, model, dims = embedding_identity()
        return (
            f"❌ 知识库为空（库路径: {cfg.resolve_db_path()}）\n"
            f"Embedding: {backend} / {model} / {dims} 维\n"
            f"Reranker: {kb_search.reranker_identity()}\n"
            f"模型驻留: {_residency_policy()}"
        )
    table = kb_schema.get_or_create_table()
    arrow = table.to_lance().to_table(columns=["source", "category"])
    sources = arrow.column("source").to_pylist()
    categories = arrow.column("category").to_pylist()

    cat_dist: dict[str, int] = {}
    for cat in categories:
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
        f"Reranker: {kb_search.reranker_identity()}",
        f"模型驻留: {_residency_policy()}",
        f"索引: {', '.join(sorted(index_names)) or '（无）'}",
    ]
    if cat_dist:
        lines.append("类别分布: " + " | ".join(
            f"{k}: {v}" for k, v in sorted(cat_dist.items(), key=lambda x: -x[1])
        ))
    return "\n".join(lines)


@mcp.tool(
    name="add_documents",
    description="扫描 vault 内目录并增量入库。source 一律为 vault 相对路径；"
                "PDF/Word/PPT/Excel 会先转成 Markdown 落到 vault（PDF 走 MinerU，"
                "Office 走 docling → markitdown），再按转换后的 md 切片。"
                "vault 外目录会被拒绝，外部文件请用 add_single_document。"
                "reindex_all 只在扫描目录等于 vault 根且扫描到可入库文件时清库重建。"
                "export_markdown_dir 仅作额外导出，不改变 source。",
)
def add_documents(
    scan_dir: Annotated[str, "要扫描的目录路径"],
    reindex_all: Annotated[bool, "是否清空知识库后重新添加（默认只增量）"] = False,
    export_markdown_dir: Annotated[
        str, "可选：PDF 转换出的 Markdown 导出目录（留空不导出）"] = "",
) -> str:
    return kb_ingest.add_documents(scan_dir, reindex_all=reindex_all,
                                   export_markdown_dir=export_markdown_dir)


@mcp.tool(
    name="add_single_document",
    description="向量化单个文档到知识库（一次只处理一个文件，避免大批量超时）。"
                "外部文件会先转成 Markdown 落到 vault（PDF 走 MinerU，"
                "docx/pptx/xlsx 走 docling → markitdown，.doc 需另存为 .docx），"
                "再以 vault 相对路径作为 source 入库。"
                "scan_dir 仅为旧调用兼容保留，不参与 source 计算；"
                "export_markdown_dir 仅作额外导出，不改变 source。",
)
def add_single_document(
    filepath: Annotated[str, "文件路径"],
    scan_dir: Annotated[str, "可选，来源基准目录（source 记录为相对路径）"] = "",
    export_markdown_dir: Annotated[
        str, "可选：PDF 转换出的 Markdown 导出目录（留空不导出）"] = "",
) -> str:
    return kb_ingest.add_single_document(filepath, scan_dir=scan_dir,
                                         export_markdown_dir=export_markdown_dir)


@mcp.tool(
    name="update_document",
    description="更新知识库中的文档：二进制或 vault 外文件先转换为 vault Markdown 再"
                "幂等替换（PDF 走 MinerU，Office 走 docling → markitdown）。vault 内"
                "文本按完整路径匹配唯一 source（新来源取 vault 相对路径），先成功写入"
                "新行再清理旧行；同名 source 无法唯一判定时返回错误，不写入也不删除。",
)
def update_document(
    filepath: Annotated[str, "文件路径"],
) -> str:
    return kb_ingest.update_document(filepath)


@mcp.tool(
    name="delete_documents",
    description="删除知识库中的文档。source_pattern 先按完整 source 精确匹配"
                "（normcase，Windows 下不区分大小写）；未命中且含 * ? [ 通配符时按 "
                "fnmatch 通配匹配（如 '*.pdf'、'docs/*'）。不再使用子串匹配；"
                "匹配集合覆盖全部来源时会被拒绝并需 confirm_all=True；留空也需 confirm_all。",
)
def delete_documents(
    source_pattern: Annotated[str, "来源（精确值或含 * ? [ 的通配模式，如 'paper.pdf'、'docs/*'、'*.pdf'），留空则需 confirm_all"] = "",
    confirm_all: Annotated[bool, "确认清空整个知识库"] = False,
) -> str:
    return kb_ingest.delete_documents(source_pattern, confirm_all=confirm_all)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="list_documents",
    description="列出知识库中的所有文档及其统计信息（chunk 数、类别等）。",
)
def list_documents(
    category_filter: Annotated[str, "按类别过滤（如 'paper'）"] = "",
    limit: Annotated[int, "最大返回文档数（1-200）"] = 50,
) -> str:
    return kb_ingest.list_documents(category_filter, limit)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_document",
    description="获取知识库中文档的完整内容（按 chunk 顺序拼接）。用于查看被片段模式截断的原文。",
)
def get_document(
    filepath: Annotated[str, "文档路径或文件名（支持模糊匹配）"],
) -> str:
    if not kb_schema.table_exists():
        return "❌ 知识库为空。"
    table = kb_schema.get_or_create_table()
    all_sources = kb_schema.existing_sources(table)
    matched = {s for s in all_sources if filepath.casefold() in (s or "").casefold()}
    if not matched:
        return f"未找到匹配「{filepath}」的文档。"
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
    max_results: Annotated[int, "返回相似文档数（1-20）"] = 5,
) -> str:
    try:
        results = kb_search.search_similar(filepath, max_results=max_results)
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
) -> str:
    return kb_web.ingest_url(url)


@mcp.tool(
    name="start_watcher",
    description="启动文件变更监听，当文档目录中的文件被修改/新增/移动时自动重新索引。需要安装 watchdog 依赖。",
)
def start_watcher(
    watch_dir: Annotated[str, "要监听的目录（默认当前工作目录）"] = "",
) -> str:
    return kb_watcher.start_watcher(watch_dir)


@mcp.tool(
    name="stop_watcher",
    description="停止文件变更监听。",
)
def stop_watcher() -> str:
    return kb_watcher.stop_watcher()


# =============================================================
# 图像检索（2026-10-06 新增：jina-clip-v2 图文对齐）
# =============================================================

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_images",
    description="以文搜图：在文献图片向量库中按内容检索图像（jina-clip-v2 图文对齐空间，中文查询可用）。返回图片路径、来源文献与相似度。",
)
def search_images(
    query: Annotated[str, "图片内容的自然语言描述（中英文均可）"],
    limit: Annotated[int, "返回图片数（1-20）"] = 8,
    source_filter: Annotated[str, "来源过滤（citekey 或目录名片段，如 'asif2026'）"] = "",
) -> str:
    from kb import image_search as kb_img

    limit = min(max(limit, 1), 20)
    try:
        if not kb_img.image_table_exists():
            return (
                "❌ 图像库为空。请先用 ingest_images 工具入库图片（默认扫描 vault 文献图片目录）。\n"
                f"   嵌入模型: {kb_img.image_identity()}"
            )
        structured = kb_img.search_images_structured(query=query, limit=limit, source_filter=source_filter)
    except Exception as error:
        import sys
        import traceback

        print(f"[search_images] {traceback.format_exc()}", file=sys.stderr)
        return f"❌ 图像搜索失败: {error}"
    docs = structured["results"]
    if not docs:
        return f"未搜索到与「{query}」相关的图片。"
    trace = structured["trace"]
    lines = [
        f"🖼️ **图像搜索结果** 「{query}」",
        f"   模式: {trace['mode']} | {trace['model']} | 表内 {trace['table_rows']} 张",
        "─" * 60,
    ]
    for i, d in enumerate(docs, 1):
        lines.append(
            f"{i}. **{d['source_doc']}/{d['file_name']}** 相似度: {d['similarity']:.4f}\n"
            f"   路径: {d['image_path']}"
        )
    lines.append("提示: 用 Read 工具打开路径即可查看图片。")
    return "\n\n".join(lines)


@mcp.tool(
    name="ingest_images",
    description="把目录下的图片向量化入库（jpg/jpeg/png；默认扫描 vault 文献图片目录，按 mtime 增量跳过未变更）。",
)
def ingest_images(
    scan_dir: Annotated[str, "图片根目录（默认 vault 文献图片目录）"] = "",
    rebuild: Annotated[bool, "清空重建（默认 false，增量入库）"] = False,
) -> str:
    from kb import image_search as kb_img

    try:
        return kb_img.ingest_images(scan_dir=scan_dir, rebuild=rebuild)
    except Exception as error:
        return f"❌ 图像入库失败: {error}"


# =============================================================
# Wiki V1（默认关闭）：WIKI_ENABLED 为 1/true/yes/on 时追加 14 个工具
# =============================================================

_WIKI_TRUE_VALUES = {"1", "true", "yes", "on"}


def _wiki_enabled() -> bool:
    """与 kb.ingest / WikiConfig 一致的布尔解析（大小写不敏感、去空白）。"""
    return os.environ.get("WIKI_ENABLED", "").strip().lower() in _WIKI_TRUE_VALUES


if _wiki_enabled():
    from kb.wiki import service as wiki_service

    def _wiki_json(payload) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _wiki_dispatch(func, *args, **kwargs) -> str:
        try:
            return _wiki_json(func(*args, **kwargs))
        except Exception as error:  # 不把异常抛到 MCP 层
            return _wiki_json({"ok": False, "error": str(error)})

    def _wiki_list(value: str) -> list[str]:
        """逗号/顿号/分号分隔字符串 → 去空去重列表。"""
        if not isinstance(value, str):
            return []
        text = value
        for separator in ("，", "；", "、", ";"):
            text = text.replace(separator, ",")
        result: list[str] = []
        for item in text.split(","):
            cleaned = item.strip()
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_status",
        description="查看 Wiki 配置（脱敏）、DB 版本、业务表计数、页面/提案/任务统计、页面索引状态与未收敛 journal。",
    )
    def wiki_status() -> str:
        return _wiki_dispatch(wiki_service.status)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_search",
        description="Wiki 统一检索：page（页面索引）/ evidence（知识库证据）/ hybrid（两者）/ related（图谱邻域）。",
    )
    def wiki_search(
        query: Annotated[str, "检索词或问题"],
        mode: Annotated[str, "检索模式 - page / evidence / hybrid / related"] = "page",
        limit: Annotated[int, "返回条数（1-100）"] = 10,
        page_type: Annotated[str, "页面类型过滤 - concept / entity / topic"] = "",
        tags: Annotated[str, "标签过滤（逗号分隔）"] = "",
    ) -> str:
        return _wiki_dispatch(
            wiki_service.search,
            query,
            mode=mode,
            limit=limit,
            page_type=page_type,
            tags=_wiki_list(tags),
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_get_page",
        description="读取 Wiki 页面全文、当前修订、出链/反链与证据摘要（page_id 或 slug）。",
    )
    def wiki_get_page(
        page_id_or_slug: Annotated[str, "page_id 或 slug"],
        include_sources: Annotated[bool, "是否包含来源/证据摘要"] = True,
        include_backlinks: Annotated[bool, "是否包含反链"] = True,
    ) -> str:
        return _wiki_dispatch(
            wiki_service.get_page,
            page_id_or_slug,
            include_sources=include_sources,
            include_backlinks=include_backlinks,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_list_pages",
        description="分页列出 Wiki 页面（按 updated_at 倒序），返回条目与 next_cursor。",
    )
    def wiki_list_pages(
        page_type: Annotated[str, "页面类型 - concept / entity / topic（留空不过滤）"] = "",
        status: Annotated[str, "页面状态 - draft/review/published/archived（留空不过滤）"] = "published",
        limit: Annotated[int, "每页条数（1-200）"] = 50,
        cursor: Annotated[str, "上一页返回的 next_cursor"] = "",
    ) -> str:
        return _wiki_dispatch(
            wiki_service.list_pages,
            page_type=page_type,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_get_related",
        description="从页面或实体出发，BFS 返回 1-2 跳的页面链接与关系邻域实体。",
    )
    def wiki_get_related(
        page_id: Annotated[str, "起始 page_id（与 entity_id 至少一个）"] = "",
        entity_id: Annotated[str, "起始 entity_id（与 page_id 至少一个）"] = "",
        depth: Annotated[int, "跳数 - 1 或 2"] = 1,
        limit: Annotated[int, "最多返回条数（1-200）"] = 30,
    ) -> str:
        return _wiki_dispatch(
            wiki_service.get_related,
            page_id=page_id,
            entity_id=entity_id,
            depth=depth,
            limit=limit,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_get_evidence",
        description="按 claim_id 查看证据链与来源，或按 chunk_id 查看 evidence_bundle。",
    )
    def wiki_get_evidence(
        claim_id: Annotated[str, "claim_id（与 chunk_id 至少一个）"] = "",
        chunk_id: Annotated[str, "chunk_id（与 claim_id 至少一个）"] = "",
    ) -> str:
        return _wiki_dispatch(
            wiki_service.get_evidence, claim_id=claim_id, chunk_id=chunk_id
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_search_evidence",
        description="直接检索知识库原始切片证据（透传 kb.search），结果标注 score_source=rag。",
    )
    def wiki_search_evidence(
        query: Annotated[str, "检索词或问题"],
        limit: Annotated[int, "返回条数（1-20）"] = 5,
        source_filter: Annotated[str, "来源过滤（文件名或目录片段）"] = "",
        category_filter: Annotated[str, "类别过滤（如 paper/code）"] = "",
        search_mode: Annotated[str, "搜索模式 - vector / text / hybrid"] = "hybrid",
    ) -> str:
        return _wiki_dispatch(
            wiki_service.search_evidence,
            query,
            limit=limit,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=search_mode,
        )

    @mcp.tool(
        name="wiki_create_page",
        description="创建 Wiki 草稿页面（只接受 concept/entity/topic，status=draft，不发布）。",
    )
    def wiki_create_page(
        title: Annotated[str, "页面标题"],
        page_type: Annotated[str, "页面类型 - concept / entity / topic"],
        content_md: Annotated[str, "Markdown 正文"],
        slug: Annotated[str, "可选 slug（留空自动生成）"] = "",
        aliases: Annotated[str, "别名（逗号分隔）"] = "",
        tags: Annotated[str, "标签（逗号分隔）"] = "",
        summary: Annotated[str, "摘要（留空自动从正文提取）"] = "",
        sources: Annotated[str, "来源（逗号分隔）"] = "",
    ) -> str:
        return _wiki_dispatch(
            wiki_service.create_page,
            title,
            page_type,
            content_md,
            slug=slug,
            aliases=_wiki_list(aliases),
            tags=_wiki_list(tags),
            summary=summary,
            sources=_wiki_list(sources),
        )

    @mcp.tool(
        name="wiki_propose_update",
        description="为已有页面创建 update_page 提案（写 draft/diff，不改页面、不发布）。",
    )
    def wiki_propose_update(
        page_id: Annotated[str, "目标页面 page_id"],
        proposed_content_md: Annotated[str, "提议的新内容（完整文档或仅正文）"],
        rationale: Annotated[str, "修改理由"],
        expected_revision_id: Annotated[str, "基于的当前 revision_id（用于冲突检测）"],
    ) -> str:
        return _wiki_dispatch(
            wiki_service.propose_update,
            page_id,
            proposed_content_md,
            rationale,
            expected_revision_id,
        )

    @mcp.tool(
        name="wiki_submit_extraction",
        description="提交 Agent 抽取结果（实体/断言/关系/页面建议），服务端校验并落库；不直接发布页面。",
    )
    def wiki_submit_extraction(
        payload_json: Annotated[str, "ExtractionPayload JSON 字符串（见 agent-extraction-contract.md）"],
    ) -> str:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError) as error:
            return _wiki_json(
                {"ok": False, "error": f"payload_json 不是合法 JSON: {error}"}
            )
        if not isinstance(payload, dict):
            return _wiki_json({"ok": False, "error": "payload_json 必须是 JSON 对象"})
        return _wiki_dispatch(wiki_service.submit_extraction, payload)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_get_proposal",
        description="查看提案全文、diff、payload 与目标页当前 revision。",
    )
    def wiki_get_proposal(
        proposal_id: Annotated[str, "提案 ID"],
    ) -> str:
        return _wiki_dispatch(wiki_service.get_proposal, proposal_id)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_review_queue",
        description="列出指定状态的提案审核队列（默认 pending）。",
    )
    def wiki_review_queue(
        status: Annotated[str, "提案状态 - pending/rejected/stale/approved/applied/cancelled"] = "pending",
        limit: Annotated[int, "最多返回条数（1-200）"] = 20,
    ) -> str:
        return _wiki_dispatch(
            wiki_service.review_queue, status=status, limit=limit
        )

    @mcp.tool(
        name="wiki_apply_proposal",
        description="审核提案：approve 写入页面并刷新导航/索引；reject 不改页面。",
    )
    def wiki_apply_proposal(
        proposal_id: Annotated[str, "提案 ID"],
        decision: Annotated[str, "approve 或 reject"] = "approve",
        review_note: Annotated[str, "审核备注"] = "",
    ) -> str:
        return _wiki_dispatch(
            wiki_service.apply_proposal,
            proposal_id,
            decision=decision,
            review_note=review_note,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        name="wiki_validate",
        description="运行 Wiki 一致性校验并返回分组问题与未收敛 journal（scope: all/pages/links/claims/documents/entities/relations/journal）。",
    )
    def wiki_validate(
        scope: Annotated[str, "校验范围"] = "all",
    ) -> str:
        return _wiki_dispatch(wiki_service.validate, scope=scope)


if __name__ == "__main__":
    mcp.run()
