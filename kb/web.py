# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

网页抓取入库（ingest_url）：requests 抓取 → BeautifulSoup 剥离标签 → 分块入库。
同一 canonical URL 重复抓取时先删除旧 source 的块再写入（幂等）；单线程串行、
无 JS 渲染。优化方向：反爬重试、正文智能抽取、与 web-search-server 抓取链复用。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

from . import config as cfg
from . import ingest as kb_ingest
from . import schema as kb_schema


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_TRACKING_PARAMS = {"fbclid", "gclid"}


def canonicalize_url(url: str) -> str:
    """URL → 稳定身份（用于 source 与重复抓取去重）；非法 URL 抛 ValueError。

    - scheme/host 小写，去掉 http:80 / https:443 默认端口与 fragment；
    - 删除 utm_* / fbclid / gclid 追踪参数，其余 query 保持原顺序；
    - 非根路径去掉末尾斜杠，根路径保留 "/"。
    """
    raw = (url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise ValueError(f"非法 URL: {url}") from error

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"非法 URL: {url}")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"非法 URL: {url}") from error

    host = parsed.hostname.lower()
    if ":" in host:  # IPv6 字面量需补回方括号
        host = f"[{host}]"
    netloc = host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = "&".join(
        segment
        for segment in parsed.query.split("&")
        if segment and not _is_tracking_param(segment)
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def _is_tracking_param(segment: str) -> bool:
    key = unquote(segment.split("=", 1)[0]).strip().lower()
    return key.startswith("utm_") or key in _TRACKING_PARAMS


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _source_for(canonical_url: str) -> str:
    """canonical URL → source：网页-<清洗后的 host>-<sha1 前 8 位>。"""
    host = urlsplit(canonical_url).hostname or ""
    host_part = re.sub(r'[\\/:*?"<>|]', "_", host)[:60]
    digest = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:8]
    return f"网页-{host_part}-{digest}"


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


def ingest_url(url: str) -> str:
    """抓取网页内容并索引到知识库；同一 canonical URL 重复抓取幂等。"""
    if not url.startswith(("http://", "https://")):
        return f"❌ 无效的 URL: {url}"
    try:
        canonical = canonicalize_url(url)
    except ValueError:
        return f"❌ 无效的 URL: {url}"
    try:
        title, content = fetch_page(url)
    except Exception as e:
        return f"❌ 抓取失败: {e}"
    if len(content) < 50:
        return f"❌ 页面内容太少，无法索引: {url}"

    source = _source_for(canonical)
    chunks = kb_ingest._chunk_by_paragraph(content, source)
    if not chunks:
        return f"❌ 无法从页面提取有效内容: {url}"

    for ch in chunks:
        ch["doc_id"] = kb_schema.chunk_doc_id(ch["text"], ch["source"], ch["chunk_index"])
        ch["ingested_at"] = kb_ingest.datetime_now_iso()

    table = kb_schema.get_or_create_table()
    deleted = table.delete(f"source = '{_sql_quote(source)}'")
    replaced = int(getattr(deleted, "num_deleted_rows", 0) or 0)
    added = kb_ingest.add_chunks(table, chunks)
    kb_schema.ensure_vector_index(table)
    kb_schema.ensure_fts_index(table)
    kb_schema.fold_new_rows(table)
    kb_schema.update_db_readme()
    return (
        f"✅ **网页已索引**\n{'─' * 40}\n"
        f"标题: {title}\nURL: {url}\n"
        f"文本块: {len(chunks)}\n已入库: {added}\n替换旧块: {replaced}\n"
        f"知识库总块数: {len(table)}\n"
        f"💡 用 search_knowledge 搜索此内容。"
    )
