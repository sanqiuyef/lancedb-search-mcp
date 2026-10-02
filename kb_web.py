# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

网页抓取入库（ingest_url）：requests 抓取 → BeautifulSoup 剥离标签 → 分块入库。
现状：单线程串行、无 JS 渲染、无去重；优化方向：反爬重试、正文智能抽取、
与 web-search-server 抓取链复用。
"""

from __future__ import annotations

import re
import requests

import kb_config as cfg
import kb_ingest
import kb_schema


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def fetch_page(url: str, timeout: int = 30) -> tuple[str, str]:
    """抓取 URL 并抽取正文，返回 (title, content)。抛异常表示抓取失败。"""
    resp = requests.get(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    resp.raise_for_status()

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    title = soup.title.string.strip() if soup.title and soup.title.string else "Untitled"
    body = soup.find("article") or soup.find("main") or soup.find("body")
    text = (body or soup).get_text(separator="\n", strip=True)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    return title, f"# {title}\n\n来源: {url}\n\n{text}"


def ingest_url(url: str, project: str = "") -> str:
    """抓取网页内容并索引到知识库。"""
    if not url.startswith(("http://", "https://")):
        return f"❌ 无效的 URL: {url}"
    try:
        title, content = fetch_page(url)
    except Exception as e:
        return f"❌ 抓取失败: {e}"
    if len(content) < 50:
        return f"❌ 页面内容太少，无法索引: {url}"

    host = url.split("//")[1].split("/")[0] if "//" in url else url
    fname = re.sub(r'[\\/:*?"<>|]', "_", f"网页-{host}")[:80]
    chunks = kb_ingest._chunk_by_paragraph(content, fname)
    if not chunks:
        return f"❌ 无法从页面提取有效内容: {url}"

    project = project or cfg.guess_project_from_cwd()
    for ch in chunks:
        ch["project"] = project
        ch["doc_id"] = kb_schema.chunk_doc_id(ch["text"], ch["source"], ch["chunk_index"])
        ch["ingested_at"] = kb_ingest.datetime_now_iso()

    table = kb_schema.get_or_create_table()
    added = kb_ingest.add_chunks(table, chunks, project)
    kb_schema.ensure_vector_index(table)
    kb_schema.ensure_fts_index(table)
    kb_schema.fold_new_rows(table)
    kb_schema.update_db_readme()
    return (
        f"✅ **网页已索引**\n{'─' * 40}\n"
        f"标题: {title}\nURL: {url}\n分区: {project}\n"
        f"文本块: {len(chunks)}\n已入库: {added}\n知识库总块数: {len(table)}\n"
        f"💡 用 search_knowledge 搜索此内容。"
    )
