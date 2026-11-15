# -*- coding: utf-8 -*-
"""LanceDB 知识库检索面板（零插件版）：本地 HTTP 服务 + 内嵌网页。

用途：给 Obsidian（核心 Web Viewer 或 Custom Frames）与任意浏览器提供
「查文献库」面板；检索内核直接复用 kb.search.search_structured
（向量 / BM25 / 混合 + 本地 bge 精排），不依赖 MCP 与任何 Obsidian 插件。

仅绑定 127.0.0.1（默认端口 8002）。启动器见 run_ui.cmd。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from kb import config as cfg  # noqa: E402

PORT = int(os.environ.get("LANCEDB_UI_PORT", "8002"))
UI_HTML = HERE / "ui" / "index.html"
_search_lock = threading.Lock()  # 串行化检索（模型推理不并发）


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


class Handler(BaseHTTPRequestHandler):
    server_version = "LanceDBUI/0.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(
            code,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
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
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/search":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"bad request: {e}"})
            return
        try:
            self._json(200, _search(payload))
        except Exception as e:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(e)})

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
