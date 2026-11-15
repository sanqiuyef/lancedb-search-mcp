# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

网页抓取入库（ingest_url）：requests 抓取 → BeautifulSoup 剥离标签 → 分块入库。
同一 canonical URL 重复抓取时先补写新块，成功后再删除不在新 doc_id 集合中的旧块
（幂等且避免写入失败丢旧数据）；单线程串行、无 JS 渲染。优化方向：反爬重试、
正文智能抽取、与 web-search-server 抓取链复用。
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
    - 路径原样保留（不折叠尾斜杠），空路径按根路径补 "/"；
    - 含用户信息（user:pass@）的 URL 直接拒绝：同一 host 下不同身份不应合并；
    - 已知边界（暂不处理）：query 参数不排序、IDN 域名不做 punycode 归一，
      因此同一资源的不同参数顺序/不同 Unicode 写法仍视为不同身份。
    """
    raw = (url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise ValueError(f"非法 URL: {url}") from error

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"非法 URL: {url}")
    if "@" in parsed.netloc:  # user[:pass]@host —— 拒绝身份合并
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

    path = parsed.path or "/"  # 空路径补根路径；非空路径不做尾斜杠折叠

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
    """canonical URL → source：网页-<清洗后的 host>-<sha1 前 12 位>。"""
    host = urlsplit(canonical_url).hostname or ""
    host_part = re.sub(r'[\\/:*?"<>|]', "_", host)[:60]
    digest = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:12]
    return f"网页-{host_part}-{digest}"


def _existing_doc_ids(table, source: str) -> set[str]:
    """读取该 source 现有行的 doc_id（旧行可能为空串）。"""
    where = f"source = '{_sql_quote(source)}'"
    arrow = table.to_lance().to_table(columns=["doc_id"], filter=where)
    return {value or "" for value in arrow.column("doc_id").to_pylist()}


def _stale_rows_where(source: str, new_doc_ids: set[str]) -> str:
    """删除该 source 下不属于新 doc_id 集合的旧行；空 doc_id（旧格式）一并清理。"""
    quoted = ",".join(f"'{_sql_quote(doc_id)}'" for doc_id in sorted(new_doc_ids))
    return (
        f"source = '{_sql_quote(source)}' AND "
        f"(doc_id = '' OR doc_id NOT IN ({quoted}))"
    )


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
    """抓取网页内容并索引到知识库；同一 canonical URL 重复抓取幂等。

    写入顺序：先补写新 doc_id 的块，add_chunks 成功后才删除旧块——写入失败时
    旧数据保持原样（宁可重试后多出部分新块，也不丢已有内容）。
    """
    raw = (url or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return f"❌ 无效的 URL: {url}"
    try:
        canonical = canonicalize_url(raw)
    except ValueError:
        return f"❌ 无效的 URL: {url}"
    try:
        title, content = fetch_page(raw)
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
    new_doc_ids = {ch["doc_id"] for ch in chunks}

    table = kb_schema.get_or_create_table()
    # 只补写库里还没有的块：内容未变的重复抓取不产生重复行。
    existing_ids = _existing_doc_ids(table, source)
    pending = [ch for ch in chunks if ch["doc_id"] not in existing_ids]
    added = kb_ingest.add_chunks(table, pending) if pending else 0
    # add 成功后才收敛旧行；失败会直接抛出，旧行保持不变。
    replaced = 0
    if existing_ids - new_doc_ids:
        stale = table.delete(_stale_rows_where(source, new_doc_ids))
        replaced = int(getattr(stale, "num_deleted_rows", 0) or 0)
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
