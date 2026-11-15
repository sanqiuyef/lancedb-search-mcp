# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

网页抓取入库（ingest_url）：requests 抓取 → BeautifulSoup 剥离标签 →
kb.vault_source.save_web_markdown 写 vault 剪藏 → add_single_document 分块入库。
网页正文与所有其他来源一样只从 vault Markdown 进入向量库；同一 canonical URL
重复抓取复用同一快照路径，由 add_single_document 幂等替换切片。
单线程串行、无 JS 渲染。优化方向：反爬重试、正文智能抽取、与
web-search-server 抓取链复用。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

import requests

from . import ingest as kb_ingest
from . import vault_source


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
    """抓取网页 → 写 vault 剪藏 Markdown → 按 vault 相对路径入库。

    同一 canonical URL 重复抓取使用同一快照文件，切片由
    :func:`kb.ingest.add_single_document` 幂等替换，不会产生第二个 source。
    抓取失败时不会写 vault，也不会改库；快照写入或入库失败会明确返回错误。
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

    try:
        md_path = vault_source.save_web_markdown(raw, title, content)
    except Exception as e:
        return f"❌ 网页快照写入失败: {e}"
    try:
        snapshot = vault_source.rel_to_vault(md_path)
    except (TypeError, ValueError) as e:
        return f"❌ 网页快照不在 vault 内: {e}"

    try:
        result = kb_ingest.add_single_document(str(md_path))
    except Exception as e:
        return (
            f"❌ 网页入库失败（快照已保存: {snapshot}）: {e}"
        )
    if result.startswith("[ERROR]"):
        return f"❌ 网页入库失败（快照已保存: {snapshot}）\n{result}"

    return (
        f"✅ **网页已索引**\n{'─' * 40}\n"
        f"标题: {title}\nURL: {url}\ncanonical: {canonical}\n"
        f"网页快照: {snapshot}\n"
        f"{result}\n"
        f"💡 用 search_knowledge 搜索此内容。"
    )
