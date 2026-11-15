# -*- coding: utf-8 -*-
"""LanceDB 知识库检索面板（零插件版）：本地 HTTP 服务 + 内嵌网页。

用途：给 Obsidian（核心 Web Viewer 或 Custom Frames）与任意浏览器提供
「查文献库」面板；检索内核直接复用 kb.search.search_structured
（向量 / BM25 / 混合 + 本地 bge 精排），不依赖 MCP 与任何 Obsidian 插件。

Phase 6 追加只读 Wiki 浏览与提案审核路由（``/api/wiki/*``），全部复用
``kb.wiki.service`` 门面；WIKI_ENABLED 未开启或 vault/DB 未配置时返回 503，
不影响原有检索接口。

仅绑定 127.0.0.1（默认端口 8002）。启动器见 run_ui.cmd。
"""

from __future__ import annotations

import datetime
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from kb import config as cfg  # noqa: E402

PORT = int(os.environ.get("LANCEDB_UI_PORT", "8002"))
UI_HTML = HERE / "ui" / "index.html"
_search_lock = threading.Lock()  # 串行化检索（模型推理不并发）

MAX_BODY_BYTES = 64 * 1024
_WIKI_HINT = "设置 WIKI_ENABLED=1、WIKI_VAULT_PATH 与 WIKI_DB_PATH 后重启服务"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
_LOOPBACK_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_SEC_FETCH_SITES = frozenset({"same-origin", "none"})
_CSRF_HEADER = "X-Wiki-CSRF"
# 单进程内存 token：启动时一次性生成，只在 /api/wiki/status 返回，不写日志或文件。
_CSRF_TOKEN = secrets.token_hex(32)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_WIKI_GET_ROUTES = frozenset(
    {
        "/api/wiki/status",
        "/api/wiki/pages",
        "/api/wiki/page",
        "/api/wiki/proposals",
        "/api/wiki/proposal",
        "/api/wiki/validate",
    }
)


class _WikiUnavailable(RuntimeError):
    """Wiki 未启用或 vault/DB 未配置。"""


class _BadRequest(ValueError):
    """请求参数或 JSON 体非法。"""


class _BodyTooLarge(ValueError):
    """请求体超过上限。"""


class _Forbidden(ValueError):
    """跨源或 CSRF 校验失败。"""


class _UnsupportedMediaType(ValueError):
    """写接口的 Content-Type 不是 application/json。"""


def _status() -> dict:
    from kb import schema as kb_schema

    try:
        exists = kb_schema.table_exists()
        count = kb_schema.row_count() if exists else 0
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "db": cfg.resolve_db_path(),
        "table": cfg.TABLE_NAME,
        "rows": count,
    }


def _search(payload: dict) -> dict:
    from kb import search as kb_search

    query = str(payload.get("query", "")).strip()
    if not query:
        return {"ok": False, "error": "查询为空"}
    limit = max(1, min(int(payload.get("limit", 10) or 10), 30))
    mode = str(payload.get("mode", "hybrid") or "hybrid")
    use_reranker = bool(payload.get("use_reranker", True))
    source_filter = str(payload.get("source_filter", "") or "")
    category_filter = str(payload.get("category_filter", "") or "")
    with _search_lock:
        out = kb_search.search_structured(
            query,
            limit=limit,
            use_reranker=use_reranker,
            source_filter=source_filter,
            category_filter=category_filter,
            search_mode=mode,
        )
    return {"ok": True, **out}


def _json_default(value):
    """把 YAML 解析出的日期等对象转成 JSON 可序列化值。"""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _wiki_config():
    """每个请求按环境变量解析 Wiki 配置，运行时开关/路径变更即时生效。"""
    from kb.wiki.config import WikiConfig, WikiConfigError

    try:
        config = WikiConfig.from_env()
    except WikiConfigError as exc:
        raise _WikiUnavailable(str(exc)) from exc
    if not config.enabled:
        raise _WikiUnavailable("WIKI_ENABLED 未开启")
    if not config.vault_path or not os.path.isdir(config.vault_path):
        raise _WikiUnavailable("Wiki vault 未配置或目录不存在")
    if not config.db_path or os.path.isdir(config.db_path):
        raise _WikiUnavailable("Wiki DB 未配置")
    return config


def _wiki_service():
    from kb.wiki import service

    return service


def _query_value(params: dict, name: str, default: str = "") -> str:
    values = params.get(name)
    if not values:
        return default
    return values[0]


def _int_query(params: dict, name: str, default: int) -> int:
    raw = _query_value(params, name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise _BadRequest(f"{name} 必须是整数") from exc


def _flag_query(params: dict, name: str) -> bool:
    raw = _query_value(params, name, "").strip().lower()
    if not raw:
        return True
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise _BadRequest(f"{name} 只接受 1/0")


def _required_query(params: dict, name: str) -> str:
    value = _query_value(params, name, "").strip()
    if not value:
        raise _BadRequest(f"缺少参数 {name}")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "LanceDBUI/0.2"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(
            code,
            json.dumps(
                obj, ensure_ascii=False, default=_json_default
            ).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _client_is_local(self) -> bool:
        address = self.client_address[0] if self.client_address else ""
        return address in _LOCAL_HOSTS

    def _bound_host_port(self) -> tuple[str, int]:
        """启动时 bind 的地址；server_address 在 socket 绑定后固定，不受请求影响。"""
        address = self.server.server_address
        return str(address[0]), int(address[1])

    def _allowed_host_names(self) -> frozenset[str]:
        host, _ = self._bound_host_port()
        if host in _LOCAL_HOSTS:
            return _LOOPBACK_HOST_NAMES
        return frozenset({host.lower()})

    def _host_header_allowed(self) -> bool:
        values = self.headers.get_all("Host") or []
        if len(values) != 1:
            return False
        raw = (values[0] or "").strip()
        if not raw:
            return False
        try:
            parsed = urlsplit("//" + raw)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme or parsed.username or parsed.password:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        hostname = parsed.hostname
        if hostname is None or port is None:
            return False
        _, bound_port = self._bound_host_port()
        return hostname.lower() in self._allowed_host_names() and port == bound_port

    def _origin_allowed(self) -> bool:
        values = self.headers.get_all("Origin") or []
        if not values:
            return True
        if len(values) != 1:
            return False
        raw = (values[0] or "").strip()
        if not raw:
            return False
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme.lower() != "http":
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        hostname = parsed.hostname
        if hostname is None or port is None:
            return False
        _, bound_port = self._bound_host_port()
        return hostname.lower() in self._allowed_host_names() and port == bound_port

    def _sec_fetch_site_allowed(self) -> bool:
        values = self.headers.get_all("Sec-Fetch-Site") or []
        if not values:
            return True
        if len(values) != 1:
            return False
        return (values[0] or "").strip().lower() in _ALLOWED_SEC_FETCH_SITES

    def _csrf_token_allowed(self) -> bool:
        values = self.headers.get_all(_CSRF_HEADER) or []
        if len(values) != 1:
            return False
        provided = (values[0] or "").strip()
        return bool(provided) and hmac.compare_digest(provided, _CSRF_TOKEN)

    def _content_type_is_json(self) -> bool:
        values = self.headers.get_all("Content-Type") or []
        if len(values) != 1:
            return False
        parts = [part.strip() for part in (values[0] or "").split(";")]
        if not parts or parts[0].lower() != "application/json":
            return False
        for part in parts[1:]:
            name, sep, value = part.partition("=")
            if sep and name.strip().lower() == "charset":
                if value.strip().strip('"').lower() != "utf-8":
                    return False
        return True

    def _guard_request(self) -> bool:
        """所有路由共用：Host 必须是启动时绑定的 loopback 地址，且客户端来自本机。"""
        if not self._host_header_allowed():
            self._json(421, {"ok": False, "error": "invalid Host header"})
            return False
        if not self._client_is_local():
            self._json(403, {"ok": False, "error": "只允许本机访问"})
            return False
        return True

    def _require_write_guards(self) -> None:
        """写路由的 CSRF 防护：Content-Type、Origin、Sec-Fetch-Site、一次性 token。"""
        if not self._content_type_is_json():
            raise _UnsupportedMediaType("Content-Type 必须是 application/json")
        if not self._origin_allowed():
            raise _Forbidden("Origin 不是同源地址")
        if not self._sec_fetch_site_allowed():
            raise _Forbidden("Sec-Fetch-Site 不允许")
        if not self._csrf_token_allowed():
            raise _Forbidden("CSRF token 缺失或无效")

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.strip():
            length = 0
        else:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise _BadRequest("bad request: Content-Length 非法") from exc
        if length < 0:
            raise _BadRequest("bad request: Content-Length 非法")
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge(f"请求体超过 {MAX_BODY_BYTES} 字节上限")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _BadRequest(f"bad request: {exc}") from exc
        if not isinstance(payload, dict):
            raise _BadRequest("bad request: JSON 必须是对象")
        return payload

    def _json_error(self, exc: Exception) -> None:
        if isinstance(exc, _BodyTooLarge):
            self._json(413, {"ok": False, "error": str(exc)})
            return
        if isinstance(exc, _UnsupportedMediaType):
            self._json(415, {"ok": False, "error": str(exc)})
            return
        if isinstance(exc, _Forbidden):
            self._json(403, {"ok": False, "error": str(exc)})
            return
        if isinstance(exc, _BadRequest):
            self._json(400, {"ok": False, "error": str(exc)})
            return
        if isinstance(exc, _WikiUnavailable):
            self._json(
                503,
                {"ok": False, "error": "wiki disabled", "hint": _WIKI_HINT},
            )
            return
        # 只回传异常文本，不把栈或内部路径细节写进响应。
        self._json(500, {"ok": False, "error": str(exc)})

    def _handle_wiki_get(self, parsed) -> None:
        if parsed.path not in _WIKI_GET_ROUTES:
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            config = _wiki_config()
            service = _wiki_service()
            params = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/api/wiki/status":
                result = service.status(config=config)
                if isinstance(result, dict):
                    result = dict(result)
                    result["csrf_token"] = _CSRF_TOKEN
            elif parsed.path == "/api/wiki/pages":
                result = service.list_pages(
                    page_type=_query_value(params, "page_type", ""),
                    status=_query_value(params, "status", "published"),
                    limit=_int_query(params, "limit", 50),
                    cursor=_query_value(params, "cursor", ""),
                    config=config,
                )
            elif parsed.path == "/api/wiki/page":
                result = service.get_page(
                    _required_query(params, "id"),
                    include_sources=_flag_query(params, "sources"),
                    include_backlinks=_flag_query(params, "backlinks"),
                    config=config,
                )
            elif parsed.path == "/api/wiki/proposals":
                result = service.review_queue(
                    status=_query_value(params, "status", "pending"),
                    limit=_int_query(params, "limit", 20),
                    config=config,
                )
            elif parsed.path == "/api/wiki/proposal":
                result = service.get_proposal(
                    _required_query(params, "id"), config=config
                )
            else:  # /api/wiki/validate
                result = service.validate(
                    scope=_query_value(params, "scope", "all") or "all",
                    config=config,
                )
        except Exception as exc:  # noqa: BLE001 - 统一转 JSON 错误
            self._json_error(exc)
            return
        try:
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001 - 序列化失败也不泄露栈
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_wiki_apply(self) -> None:
        try:
            self._require_write_guards()
            config = _wiki_config()
            service = _wiki_service()
            payload = self._read_json_body()
            proposal_id = payload.get("proposal_id")
            if not isinstance(proposal_id, str) or not proposal_id.strip():
                raise _BadRequest("缺少 proposal_id")
            decision = payload.get("decision")
            decision_key = decision.strip().lower() if isinstance(decision, str) else ""
            if decision_key not in ("approve", "reject"):
                raise _BadRequest("decision 必须是 approve 或 reject")
            review_note = payload.get("review_note", "")
            if review_note is None:
                review_note = ""
            if not isinstance(review_note, str):
                raise _BadRequest("review_note 必须是字符串")
            result = service.apply_proposal(
                proposal_id.strip(),
                decision_key,
                review_note,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - 统一转 JSON 错误
            self._json_error(exc)
            return
        try:
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001 - 序列化失败也不泄露栈
            self._json(500, {"ok": False, "error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard_request():
            return
        if self.path in ("/", "/index.html"):
            try:
                html = UI_HTML.read_bytes()
            except OSError as e:
                self._send(
                    500, f"UI file missing: {e}".encode("utf-8"), "text/plain; charset=utf-8"
                )
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        if self.path == "/api/status":
            self._json(200, _status())
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/wiki/"):
            self._handle_wiki_get(parsed)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard_request():
            return
        if self.path == "/api/search":
            try:
                payload = self._read_json_body()
            except Exception as exc:  # noqa: BLE001 - 400/413
                self._json_error(exc)
                return
            try:
                self._json(200, _search(payload))
            except Exception as e:  # noqa: BLE001 - 保留原有检索错误语义
                self._json(200, {"ok": False, "error": str(e)})
            return
        if urlparse(self.path).path == "/api/wiki/apply":
            self._handle_wiki_apply()
            return
        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # 静音常规访问日志
        pass


def main() -> None:
    print(
        f"[lancedb-ui] http://127.0.0.1:{PORT}/  db={cfg.resolve_db_path()}",
        flush=True,
    )
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
