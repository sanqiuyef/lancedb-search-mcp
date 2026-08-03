# -*- coding: utf-8 -*-
"""
MCP LanceDB 知识搜索服务器 - 嵌入式 LanceDB + 硅基流动 Embedding/Reranker API
无需 Docker，无需本地模型，极低内存占用（~50MB）

v3.0 - 功能增强版：
  - 删除文档（delete_documents）
  - 更新文档（update_document）
  - 来源过滤 + 分页 + 去重
  - 全文搜索（FTS）：vector / text / hybrid 三种模式
  - RRF 融合排序（hybrid 模式）
"""
import os
import sys
import re
import hashlib
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Optional, List, Dict
from urllib.parse import unquote, urlparse

import requests
import lancedb
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import ToolAnnotations
from mcp.server.models import InitializationOptions
from mcp.types import (
    RootsCapability,
    RootsListChangedNotification,
    ServerCapabilities,
    ToolsCapability,
)
import pyarrow as pa
import json

from knowledge_browser_core import load_kb_config, search_with_trace
from content_graph import ContentGraphStore, extractor_from_environment

# =============================================================
# 可选本地 ML 依赖（sentence-transformers）
# =============================================================
_LOCAL_EMBED_MODEL = None   # 懒加载的 embedding 模型实例
_LOCAL_RERANK_MODEL = None  # 懒加载的 reranker 模型实例


def _resolve_device() -> str:
    """解析 LOCAL_MODEL_DEVICE 为实际的 torch 设备字符串"""
    d = LOCAL_MODEL_DEVICE
    if d == "auto":
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            return "cpu"
        except ImportError:
            return "cpu"
    return d


def _load_local_embed_model():
    """懒加载本地 embedding 模型（sentence-transformers）"""
    global _LOCAL_EMBED_MODEL, EMBED_DIM
    if _LOCAL_EMBED_MODEL is not None:
        return _LOCAL_EMBED_MODEL

    try:
        import os as _os
        _os.environ["TOKENIZERS_PARALLELISM"] = "false"
        _os.environ.pop("HF_HUB_OFFLINE", None)  # 兼容 Anaconda 离线设置
        from sentence_transformers import SentenceTransformer

        device = _resolve_device()
        print(f"[INFO] 正在加载本地 Embedding 模型: {LOCAL_EMBED_MODEL}  (device={device})", file=sys.stderr)
        model = SentenceTransformer(LOCAL_EMBED_MODEL, device=device)
        _LOCAL_EMBED_MODEL = model

        # 自动检测输出维度并验证
        test_vec = model.encode("test", normalize_embeddings=True)
        detected_dim = len(test_vec)
        if detected_dim != EMBED_DIM:
            print(f"[WARN] 本地模型维度 ({detected_dim}) 与配置维度 ({EMBED_DIM}) 不一致", file=sys.stderr)
            print(f"[WARN] 将使用检测到的维度 {detected_dim}，如需强制指定请设置 LOCAL_EMBED_DIM", file=sys.stderr)
            EMBED_DIM = detected_dim

        print(f"[INFO] 本地 Embedding 模型加载完成 (dim={EMBED_DIM})", file=sys.stderr)
        return model
    except Exception as e:
        err_msg = str(e)
        if "No module named" in err_msg or "cannot import name" in err_msg:
            hint = f"缺少依赖: {e}\n请执行: pip install sentence-transformers torch"
        elif "out of memory" in err_msg.lower() or "CUDA" in err_msg:
            hint = f"GPU 资源不足: {e}\n请设置 LOCAL_MODEL_DEVICE=cpu 使用 CPU 模式"
        elif "connect" in err_msg.lower() or "timeout" in err_msg.lower():
            hint = f"模型下载失败（网络问题）: {e}\n请检查网络连接"
        else:
            hint = f"模型加载失败: {e}"
        raise RuntimeError(hint)


def _load_local_reranker_model():
    """懒加载本地 reranker 模型"""
    global _LOCAL_RERANK_MODEL
    if _LOCAL_RERANK_MODEL is not None:
        return _LOCAL_RERANK_MODEL

    try:
        import os as _os
        _os.environ["TOKENIZERS_PARALLELISM"] = "false"
        _os.environ.pop("HF_HUB_OFFLINE", None)  # 兼容 Anaconda 离线设置
        from sentence_transformers import CrossEncoder

        device = _resolve_device()
        print(f"[INFO] 正在加载本地 Reranker 模型: {LOCAL_RERANK_MODEL}  (device={device})", file=sys.stderr)
        model = CrossEncoder(LOCAL_RERANK_MODEL, device=device)
        _LOCAL_RERANK_MODEL = model
        print("[INFO] 本地 Reranker 模型加载完成", file=sys.stderr)
        return model
    except Exception as e:
        err_msg = str(e)
        if "No module named" in err_msg or "cannot import name" in err_msg:
            hint = f"缺少依赖: {e}\n请执行: pip install sentence-transformers torch"
        elif "out of memory" in err_msg.lower() or "CUDA" in err_msg:
            hint = f"GPU 资源不足: {e}\n请设置 LOCAL_MODEL_DEVICE=cpu 使用 CPU 模式"
        elif "connect" in err_msg.lower() or "timeout" in err_msg.lower():
            hint = f"模型下载失败（网络问题）: {e}\n请检查网络连接"
        else:
            hint = f"模型加载失败: {e}"
        raise RuntimeError(hint)


# MCP 工具调用超时由主机端控制。对于大文件（如PDF），
# 使用新增的 add_single_document 工具一次处理一个文件以避免超时。
mcp = FastMCP("LanceDB 知识搜索", port=8002)

# ── MCP Roots 支持 ──────────────────────────────────────────────
# 当客户端切换项目时，自动匹配并切换知识库

_current_roots = []


def _root_to_path(root) -> str | None:
    """将 Root URI 转换为本地路径"""
    try:
        uri = root.uri
        uri_str = str(uri)
        parsed = urlparse(uri_str)
        if parsed.scheme == "file":
            path = unquote(parsed.path)
            if path.startswith("/") and ":" in path:
                path = path.lstrip("/")
            return path
    except Exception:
        pass
    return None


async def _on_roots_changed(notification: RootsListChangedNotification) -> None:
    """客户端通知 Roots 变化时，自动切换知识库"""
    global _current_roots
    try:
        ctx = mcp._mcp_server.request_context
        result = await ctx.session.list_roots()
        _current_roots = result.roots

        # 尝试自动匹配知识库
        for root in _current_roots:
            root_path = _root_to_path(root)
            if root_path and os.path.isdir(root_path):
                # 尝试匹配注册表
                registry = _load_registry()
                for name, info in registry.items():
                    kb_dir = info["path"]
                    project_root = kb_dir
                    if kb_dir.endswith(".reasonix\\knowledge") or kb_dir.endswith(".reasonix/knowledge"):
                        project_root = os.path.dirname(os.path.dirname(kb_dir))
                    elif kb_dir.endswith("lancedb_data"):
                        project_root = os.path.dirname(kb_dir)
                    if os.path.normpath(root_path) == os.path.normpath(project_root):
                        switch_knowledge_base(name)
                        return

                # 尝试按项目名匹配：项目根目录存在于 cherry-workplace 即可
                # （知识库目录未创建时由 get_db() 惰性创建，避免新项目误落 global）
                base = r"D:\cherry-workplace"
                project_name = os.path.basename(root_path)
                project_root = os.path.join(base, project_name)
                if os.path.isdir(project_root):
                    legacy = os.path.join(project_root, "lancedb_data")
                    if os.path.isdir(os.path.join(legacy, "my_docs.lance")):
                        switch_knowledge_base(legacy)
                    else:
                        switch_knowledge_base(
                            os.path.join(project_root, ".reasonix", "knowledge")
                        )
                    return
    except LookupError:
        pass
    except Exception:
        pass


# ── 以下补丁依赖 mcp._mcp_server 私有 API，需锁定 mcp 版本（升级前请先验证兼容性）──

# 注册 Roots 通知处理器
mcp._mcp_server.notification_handlers[RootsListChangedNotification] = _on_roots_changed


def _patch_roots_capability():
    """在 MCP 初始化选项中注入 roots 能力声明"""
    original = mcp._mcp_server.create_initialization_options

    def patched(notification_options=None, experimental_capabilities=None):
        base = original(notification_options, experimental_capabilities)
        # 复制基础 capabilities 并注入 roots
        base_caps = base.capabilities
        patched_caps = ServerCapabilities(
            prompts=base_caps.prompts,
            resources=base_caps.resources,
            tools=base_caps.tools,
            logging=base_caps.logging,
            completions=base_caps.completions,
            experimental=base_caps.experimental,
            roots=RootsCapability(listChanged=True),
        )
        return InitializationOptions(
            server_name=base.server_name,
            server_version=base.server_version,
            capabilities=patched_caps,
            instructions=base.instructions,
            website_url=base.website_url,
            icons=base.icons,
        )

    mcp._mcp_server.create_initialization_options = patched


_patch_roots_capability()

# ── 原代码继续 ──────────────────────────────────────────────────

# =============================================================
# 配置
# =============================================================

# ── 后端选择 ──
# "api"  → 硅基流动 API（需 SILICONFLOW_API_KEY）
# "local" → 本地 sentence-transformers（默认，已配置 BAAI/bge-m3）
# "ollama" → Ollama 服务（需运行 ollama serve）
EMBEDDING_BACKEND = os.environ.get("EMBEDDING_BACKEND", "api").lower()
RERANKER_BACKEND = os.environ.get("RERANKER_BACKEND", "api").lower()  # api / local / disabled

# ── API 模式配置（硅基流动） ──
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
EMBEDDING_URL = "https://api.siliconflow.cn/v1/embeddings"
RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
EMBED_DIM = 1024  # Qwen3-Embedding-8B 支持 32~4096，1024 是质量/成本最佳平衡点

# ── 本地模型配置（sentence-transformers） ──
LOCAL_EMBED_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-m3")
LOCAL_RERANK_MODEL = os.environ.get("LOCAL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
LOCAL_EMBED_DIM = int(os.environ.get("LOCAL_EMBED_DIM", "1024"))
LOCAL_MODEL_DEVICE = os.environ.get("LOCAL_MODEL_DEVICE", "auto").lower()

# ── Ollama 配置 ──
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
KB_CONFIG_PATH = os.path.join(SERVER_DIR, "kb-config.json")
CONTENT_GRAPH_PATH = os.environ.get(
    "CONTENT_GRAPH_PATH", os.path.join(SERVER_DIR, "content_graph.sqlite3")
)
_CONTENT_GRAPH_STORE = None

# DB_PATH 现在由 get_db() 自动检测并设置，不再是硬编码常量
DB_PATH = None  # 首次 get_db() 时由 _detect_db_path() 填充
TABLE_NAME = "my_docs"

# 多知识库管理 —— 全局状态
_connected_path = None   # 当前已连接的数据库路径
_current_kb_name = "自动检测"  # 当前知识库显示名称
_db = None               # LanceDB 连接单例
_kb_registry = None      # kb-config.json 的注册表缓存
_kb_aliases = {}
_kb_config_stamp = None  # (mtime, size)，用于判断 kb-config.json 是否变化（热重载）

# 文档后缀
EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".html", ".htm"}
EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".venv", ".reasonix", "truncated-results"}
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB

# ── 扫描 PDF OCR 回退（默认关闭，避免无 OCR 环境时影响现有流程）──
OCR_ENABLED = os.environ.get("LANCEDB_OCR", "0") == "1"
OCR_TESSERACT_CMD = os.environ.get(
    "TESSERACT_CMD", r"D:\cherry-workplace\tools\tesseract\tesseract.exe"
)
OCR_LANG = os.environ.get("LANCEDB_OCR_LANG", "chi_sim+eng")
OCR_DPI = int(os.environ.get("LANCEDB_OCR_DPI", "300"))
OCR_MAX_PAGES = int(os.environ.get("LANCEDB_OCR_MAX_PAGES", "0"))  # 0 = 全部页面
OCR_MIN_TEXT_CHARS = int(os.environ.get("LANCEDB_OCR_MIN_TEXT_CHARS", "500"))

# ── 查询扩展：缩写→完整词（提升 FTS/BM25 准确率） ──
# 格式：{"缩写": ["展开词1", "展开词2"]}
QUERY_EXPANSIONS = {
    "sqli": ["sql injection", "sqli"],
    "xss": ["cross site scripting", "xss"],
    "csrf": ["cross site request forgery", "csrf"],
    "rce": ["remote code execution", "rce"],
    "privesc": ["privilege escalation", "privesc"],
    "k8s": ["kubernetes", "k8s"],
    "ml": ["machine learning", "ml"],
    "dl": ["deep learning", "dl"],
    "nlp": ["natural language processing", "nlp"],
    "ner": ["named entity recognition", "ner"],
    "cnn": ["convolutional neural network", "cnn"],
    "rnn": ["recurrent neural network", "rnn"],
    "lstm": ["long short term memory", "lstm"],
    "gcn": ["graph convolutional network", "gcn"],
    "gnn": ["graph neural network", "gnn"],
    "gan": ["generative adversarial network", "gan"],
    "vae": ["variational autoencoder", "vae"],
    "nerf": ["neural radiance field", "nerf"],
    "nerf": ["neural radiance fields", "nerf"],
    "bim": ["building information modeling", "bim"],
    "lod": ["level of detail", "lod"],
    "ifc": ["industry foundation classes", "ifc"],
    "api": ["application programming interface", "api"],
    "sdk": ["software development kit", "sdk"],
    "cli": ["command line interface", "cli"],
    "gui": ["graphical user interface", "gui"],
    "db": ["database", "db"],
    "ui": ["user interface", "ui"],
    "ux": ["user experience", "ux"],
    "ai": ["artificial intelligence", "ai"],
    "iot": ["internet of things", "iot"],
    "sla": ["service level agreement", "sla"],
    "mvp": ["minimum viable product", "mvp"],
}

# ── 查询扩展组：双向同义词 ──
# 组内每个词自动扩展到组内其他所有词
QUERY_EXPANSION_GROUPS = [
    ["triple barrier", "tb", "trip_barr"],
    ["profit factor", "pf"],
]

# ── 类别映射：目录路径→类别名 ──
# 按文件夹路径自动打标签，搜索时可筛选
CATEGORY_MAPPINGS = {
    "论文": "paper",
    "papers": "paper",
    "文献": "paper",
    "notes": "note",
    "笔记": "note",
    "code": "code",
    "src": "code",
    "docs": "documentation",
    "文档": "documentation",
    "manual": "manual",
    "手册": "manual",
    "tutorial": "tutorial",
    "教程": "tutorial",
    "api": "api",
}

# ── 关键词路由：查询包含这些词时优先对应类别 ──
# 不过滤（其他类别的结果仍会出现，只是排名较低）
KEYWORD_ROUTES = {
    "paper": ["论文", "文献", "study", "research", "experiment", "method", "algorithm"],
    "code": ["实现", "代码", "function", "class", "def", "import", "代码示例"],
    "manual": ["教程", "manual", "guide", "如何", "怎么", "安装", "配置"],
    "api": ["api", "接口", "endpoint", "sdk"],
}

HEADERS = {
    "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
    "Content-Type": "application/json"
}


# =============================================================
# LRU Embedding 缓存（hash key，智能淘汰）
# =============================================================

class LRUCache:
    """线程安全的 LRU 缓存"""
    def __init__(self, capacity: int = 2000):
        self._cache = OrderedDict()
        self._capacity = capacity

    def get(self, key: str):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._capacity:
                self._cache.popitem(last=False)  # 淘汰最久未用
        self._cache[key] = value

    def __len__(self):
        return len(self._cache)


def _hash_text(text: str) -> str:
    """用 SHA-256 做缓存 key，避免截断碰撞"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_embedding_cache = LRUCache(capacity=2000)


# =============================================================
# 多知识库注册表 + 自动检测
# =============================================================


def _load_registry() -> dict:
    """加载 kb-config.json 知识库注册表（带 mtime 热重载，改配置无需重启进程）"""
    global _kb_registry, _kb_aliases, _kb_config_stamp
    if _kb_registry is not None:
        # 已加载过：仅当文件 mtime/size 变化时才重读，否则直接返回缓存
        try:
            st = os.stat(KB_CONFIG_PATH)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return _kb_registry
        if _kb_config_stamp is not None and stamp == _kb_config_stamp:
            return _kb_registry
    _kb_registry = {}
    _kb_config_stamp = None
    if os.path.exists(KB_CONFIG_PATH):
        try:
            st = os.stat(KB_CONFIG_PATH)
            data, _kb_aliases = load_kb_config(KB_CONFIG_PATH, repair=True)
            _kb_registry = data.get("knowledge_bases", {})
            _kb_config_stamp = (st.st_mtime, st.st_size)
        except Exception as e:
            print(f"[WARN] 知识库配置加载失败: {e}", file=sys.stderr)
    return _kb_registry


def _detect_db_path() -> str:
    """
    自动检测当前应使用的知识库路径。
    优先级：
        1. LANCEDB_DB_PATH 环境变量（精确路径）
        2. REASONIX_WORKSPACE 环境变量（项目根 → .reasonix/knowledge）
        3. 当前工作目录 os.getcwd() 自动匹配
        4. kb-config.json 注册表匹配
        5. 兜底：全局共享知识库
    """
    # ── 1. 精确路径环境变量 ──
    exact = os.environ.get("LANCEDB_DB_PATH")
    if exact:
        return exact

    # ── 2. 工作区环境变量 ──
    ws = os.environ.get("REASONIX_WORKSPACE") or os.environ.get("REASONIX_CURRENT_PROJECT")
    if ws and os.path.isdir(ws):
        # 工作区是客户端显式声明的项目上下文：即使知识库目录尚未创建，
        # 也直接指向它（get_db() 会惰性创建），避免新项目误落 global。
        legacy = os.path.join(ws, "lancedb_data")
        if os.path.isdir(os.path.join(legacy, "my_docs.lance")):
            return legacy
        return os.path.join(ws, ".reasonix", "knowledge")

    # ── 3. CWD 自动匹配 ──
    cwd = os.getcwd()
    registry = _load_registry()

    # 3a. 匹配注册表（路径前缀匹配）
    for info in registry.values():
        kb_dir = info["path"]
        # 判断 project_root：如果是 .reasonix/knowledge，取两级上级
        project_root = kb_dir
        if kb_dir.endswith(".reasonix\\knowledge") or kb_dir.endswith(".reasonix/knowledge"):
            project_root = os.path.dirname(os.path.dirname(kb_dir))
        elif kb_dir.endswith("lancedb_data"):
            project_root = os.path.dirname(kb_dir)
        if os.path.normcase(cwd).startswith(os.path.normcase(project_root)):
            return kb_dir

    # 3b. 智能扫描：自动发现 cherry-workplace 下所有项目
    base = r"D:\cherry-workplace"
    if os.path.isdir(base):
        names = sorted(os.listdir(base), key=len, reverse=True)
        for name in names:
            for candidate in [
                os.path.join(base, name, ".reasonix", "knowledge"),
                os.path.join(base, name, "lancedb_data"),
            ]:
                if os.path.isdir(os.path.join(candidate, "my_docs.lance")):
                    if cwd.startswith(os.path.join(base, name)):
                        return candidate

    # ── 4. 兜底：全局共享知识库 ──
    global_path = os.environ.get("LANCEDB_GLOBAL_PATH")
    if global_path:
        return global_path
    # 从注册表找 global
    if "global" in registry:
        return registry["global"]["path"]
    # 最后兜底
    return os.path.join(os.path.expanduser("~"), ".reasonix", "knowledge")


# =============================================================
# LanceDB 连接 + 索引管理
# =============================================================


def get_db():
    """
    获取 LanceDB 连接。
    - 首次调用时用 _detect_db_path() 自动检测路径
    - 手动 switch_knowledge_base 后自动跟随
    - 每次调用检查路径变化，自动重连
    """
    global _db, _connected_path, _current_kb_name, DB_PATH, _embedding_cache

    # 首次：自动检测
    if _connected_path is None:
        detected = _detect_db_path()
        _connected_path = detected
        DB_PATH = detected
        _current_kb_name = _guess_kb_name(detected)

    # 路径变化时重连
    if _db is None and _connected_path:
        os.makedirs(_connected_path, exist_ok=True)
        _db = lancedb.connect(_connected_path)
    return _db


def _guess_kb_name(path: str) -> str:
    """从路径推断知识库显示名称"""
    registry = _load_registry()
    for name, info in registry.items():
        if os.path.normpath(info["path"]) == os.path.normpath(path):
            return name
    # 从路径结构推断
    if ".reasonix\\knowledge" in path or ".reasonix/knowledge" in path:
        # D:\cherry-workplace\通用\.reasonix\knowledge → 通用
        parts = path.replace("/", "\\").split("\\")
        for i, p in enumerate(parts):
            if p == ".reasonix" and i > 0:
                return f"项目-{parts[i-1]}"
    if "lancedb_data" in path:
        parts = path.replace("/", "\\").split("\\")
        for i, p in enumerate(parts):
            if p == "lancedb_data" and i > 0:
                return f"项目-{parts[i-1]}"
    if "knowledge" in path:
        parts = path.replace("/", "\\").split("\\")
        for i, p in enumerate(parts):
            if p == "knowledge" and i > 0:
                return parts[i-1]
    return os.path.basename(path)


def _ensure_index(table):
    """确保表有向量索引（IVF-PQ）和全文索引（FTS），没有则自动创建"""
    # 1. 向量索引
    try:
        existing = table.list_indices()
        has_vector_idx = any("vector" in list(getattr(idx, "columns", idx.get("columns", []) if hasattr(idx, "get") else []))
                             for idx in existing)
        if has_vector_idx:
            return
    except Exception:
        pass

    row_count = len(table)
    if row_count < 1000:
        return

    try:
        num_partitions = min(max(int(row_count ** 0.5), 16), 256)
        num_sub_vectors = 16
        table.create_index(
            metric="cosine",
            vector_column_name="vector",
            index_type="IVF_PQ",
            num_partitions=num_partitions,
            num_sub_vectors=num_sub_vectors,
        )
    except Exception as e:
        print(f"[WARN] 向量索引创建失败（退回暴力扫描）: {e}", file=sys.stderr)


def _ensure_fts_index(table, force_rebuild=False):
    """确保表有全文索引（FTS），没有则自动创建"""
    try:
        if not force_rebuild:
            # 检查是否已有 FTS 索引，避免每次重建
            existing = table.list_indices()
            has_fts = any("text" in list(getattr(idx, "columns", idx.get("columns", []) if hasattr(idx, "get") else []))
                          for idx in existing)
            if has_fts:
                return
        table.create_fts_index("text", replace=True)
    except Exception as e:
        # FTS 索引创建失败不阻塞
        print(f"[WARN] FTS 索引创建失败: {e}", file=sys.stderr)


def get_or_create_table(db=None):
    """获取或创建 LanceDB 表"""
    if db is None:
        db = get_db()
    if TABLE_NAME in db.table_names():
        tbl = db.open_table(TABLE_NAME)
        _ensure_index(tbl)
        return tbl

    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("chunk_index", pa.int32()),
        pa.field("category", pa.string()),
    ])
    return db.create_table(TABLE_NAME, schema=schema)


# =============================================================
# Embedding 抽象层（API / 本地 / Ollama，带 LRU 缓存）
# =============================================================

# -- 本地模式 embedding 函数 --


def _get_embedding_local(text: str) -> List[float]:
    """本地 sentence-transformers embedding"""
    model = _load_local_embed_model()
    vec = model.encode(text, normalize_embeddings=True).tolist()
    return vec


def _get_embeddings_batch_local(texts: List[str]) -> List[List[float]]:
    """本地批量 embedding"""
    model = _load_local_embed_model()
    vecs = model.encode(texts, normalize_embeddings=True).tolist()
    return vecs


# -- Ollama 模式 embedding 函数 --
_OLLAMA_EMBED_DIM_DETECTED = False


def _get_embedding_ollama(text: str) -> List[float]:
    """调用 Ollama Embedding API"""
    global _OLLAMA_EMBED_DIM_DETECTED, EMBED_DIM
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": LOCAL_EMBED_MODEL, "input": text},
        timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    vec = data["embeddings"][0]
    if not _OLLAMA_EMBED_DIM_DETECTED:
        detected_dim = len(vec)
        if detected_dim != EMBED_DIM:
            print(f"[INFO] 更新 Embedding 维度: {EMBED_DIM} -> {detected_dim}", file=sys.stderr)
            EMBED_DIM = detected_dim
        _OLLAMA_EMBED_DIM_DETECTED = True
    return vec


def _get_embeddings_batch_ollama(texts: List[str]) -> List[List[float]]:
    """Ollama 批量 embedding"""
    global _OLLAMA_EMBED_DIM_DETECTED, EMBED_DIM
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": LOCAL_EMBED_MODEL, "input": texts},
        timeout=120
    )
    resp.raise_for_status()
    data = resp.json()
    vecs = data["embeddings"]
    if not _OLLAMA_EMBED_DIM_DETECTED and vecs:
        detected_dim = len(vecs[0])
        if detected_dim != EMBED_DIM:
            print(f"[INFO] 更新 Embedding 维度: {EMBED_DIM} -> {detected_dim}", file=sys.stderr)
            EMBED_DIM = detected_dim
        _OLLAMA_EMBED_DIM_DETECTED = True
    return vecs


# -- 统一入口 --


def get_embedding(text: str) -> List[float]:
    """统一的 Embedding 入口（根据 EMBEDDING_BACKEND 自动选择后端）"""
    cache_key = _hash_text(text)
    cached = _embedding_cache.get(cache_key)
    if cached is not None:
        return cached

    if EMBEDDING_BACKEND == "local":
        vec = _get_embedding_local(text)
    elif EMBEDDING_BACKEND == "ollama":
        vec = _get_embedding_ollama(text)
    else:
        # 默认 API 模式
        resp = requests.post(
            EMBEDDING_URL,
            headers=HEADERS,
            json={
                "model": EMBED_MODEL,
                "input": text,
                "encoding_format": "float",
                "dimensions": EMBED_DIM
            },
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data["data"][0]["embedding"]

    _embedding_cache.put(cache_key, vec)
    return vec


def get_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """统一的批量 Embedding 入口"""
    if not texts:
        return []

    # 分离缓存命中和未命中
    results = [None] * len(texts)
    uncached_indices = []
    uncached_texts = []

    for i, text in enumerate(texts):
        cache_key = _hash_text(text)
        cached = _embedding_cache.get(cache_key)
        if cached is not None:
            results[i] = cached
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    if not uncached_texts:
        return results

    # 按后端调度
    if EMBEDDING_BACKEND == "local":
        api_vecs = _get_embeddings_batch_local(uncached_texts)
    elif EMBEDDING_BACKEND == "ollama":
        api_vecs = _get_embeddings_batch_ollama(uncached_texts)
    else:
        resp = requests.post(
            EMBEDDING_URL,
            headers=HEADERS,
            json={
                "model": EMBED_MODEL,
                "input": uncached_texts,
                "encoding_format": "float",
                "dimensions": EMBED_DIM
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        api_vecs = [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]

    for j, vec in enumerate(api_vecs):
        original_idx = uncached_indices[j]
        results[original_idx] = vec
        _embedding_cache.put(_hash_text(uncached_texts[j]), vec)

    return results


# =============================================================
# Reranker 抽象层（API / 本地 / disabled）
# =============================================================


def _rerank_local(query: str, documents: List[Dict], top_n: int = 5) -> List[Dict]:
    """本地 CrossEncoder reranker"""
    model = _load_local_reranker_model()
    texts = [d["text"] for d in documents]
    pairs = [[query, t] for t in texts]
    scores = model.predict(pairs)

    indexed = list(enumerate(scores))
    indexed.sort(key=lambda x: x[1], reverse=True)
    indexed = indexed[:top_n]

    reranked = []
    for idx, score in indexed:
        reranked.append({
            **documents[idx],
            "relevance_score": round(float(score), 4)
        })
    return reranked


def rerank_results(query: str, documents: List[Dict], top_n: int = 5) -> List[Dict]:
    """统一的 Reranker 入口（根据 RERANKER_BACKEND 自动选择后端）"""
    if not documents:
        return documents

    if RERANKER_BACKEND == "disabled":
        return documents[:top_n]

    if RERANKER_BACKEND == "local":
        return _rerank_local(query, documents, top_n)

    # 默认 API 模式
    texts = [d["text"] for d in documents]
    resp = requests.post(
        RERANK_URL,
        headers=HEADERS,
        json={
            "model": RERANK_MODEL,
            "query": query,
            "documents": texts,
            "top_n": min(top_n, len(texts)),
            "return_documents": True
        },
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()

    reranked = []
    for r in data["results"]:
        idx = r["index"]
        reranked.append({
            **documents[idx],
            "relevance_score": round(r["relevance_score"], 4)
        })
    return reranked


# =============================================================
# 文档提取与分块
# =============================================================


def _pdf_text_needs_ocr(text: str) -> bool:
    """判断 PDF 文本层是否缺失或几乎只包含重复水印。"""
    lines = [re.sub(r"\s+", "", line) for line in text.splitlines()]
    lines = [line for line in lines if len(line) >= 3]
    compact_text = "".join(lines)
    if len(compact_text) < OCR_MIN_TEXT_CHARS:
        # 单页短文档也可能是正常文本 PDF，避免为一行完整文本额外做 OCR。
        if len(lines) == 1 and len(lines[0]) >= 30:
            return False
        return True

    # 扫描件常见情况：每页只能抽到同一段水印，累计字符数可能很多。
    if len(lines) >= 8:
        repeated = Counter(lines).most_common(1)[0][1]
        if len(set(lines)) <= max(3, len(lines) // 8) or repeated / len(lines) >= 0.85:
            return True
    return False


def _ocr_pdf(filepath: str) -> str:
    """使用 PyMuPDF 渲染扫描件，再交给本地 Tesseract 逐页识别。"""
    if not os.path.isfile(OCR_TESSERACT_CMD):
        raise RuntimeError(f"未找到 Tesseract: {OCR_TESSERACT_CMD}")

    import fitz
    from io import BytesIO
    from PIL import Image
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = OCR_TESSERACT_CMD
    document = fitz.open(filepath)
    page_count = len(document)
    limit = min(page_count, OCR_MAX_PAGES) if OCR_MAX_PAGES > 0 else page_count
    scale = OCR_DPI / 72
    matrix = fitz.Matrix(scale, scale)
    pages = []

    try:
        for page_number in range(limit):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            with Image.open(BytesIO(pixmap.tobytes("png"))) as image:
                page_text = pytesseract.image_to_string(image, lang=OCR_LANG, config="--psm 6")
            if page_text.strip():
                pages.append(page_text)
            if (page_number + 1) % 10 == 0 or page_number + 1 == limit:
                print(f"[OCR] {os.path.basename(filepath)}: {page_number + 1}/{limit} 页", file=sys.stderr)
    finally:
        document.close()

    if limit < page_count:
        pages.append(f"\n\n[OCR 仅处理前 {limit}/{page_count} 页，受 LANCEDB_OCR_MAX_PAGES 限制]\n")
    return "\n\n".join(pages)


def extract_text(filepath: str) -> str:
    """从文件中提取纯文本"""
    ext = Path(filepath).suffix.lower()
    try:
        if ext in (".md", ".txt"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        elif ext == ".docx":
            from docx import Document
            doc = Document(filepath)
            return "\n".join(p.text for p in doc.paragraphs)
        elif ext in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        elif ext == ".pdf":
            text = ""
            try:
                from pdfminer.high_level import extract_text as pdf_extract
                text = pdf_extract(filepath) or ""
            except Exception:
                try:
                    import PyPDF2
                    with open(filepath, "rb") as f:
                        reader = PyPDF2.PdfReader(f)
                        text = "\n".join(p.extract_text() or "" for p in reader.pages)
                except Exception:
                    text = ""

            if OCR_ENABLED and _pdf_text_needs_ocr(text):
                print(f"[OCR] 检测到扫描件或重复水印，开始识别: {filepath}", file=sys.stderr)
                try:
                    ocr_text = _ocr_pdf(filepath)
                    if len(ocr_text.strip()) >= 20:
                        return ocr_text
                    print(f"[OCR] 未提取到有效文字，保留原文本层: {filepath}", file=sys.stderr)
                except Exception as e:
                    print(f"[OCR] 识别失败，保留原文本层 {filepath}: {e}", file=sys.stderr)
            return text
        else:
            return ""
    except Exception:
        return ""


def chunk_text(text: str, source: str) -> List[Dict]:
    """将文本分块（带 overlap），.md 文件按 Markdown 标题优先分块"""
    # .md 文件用 Markdown 感知分块
    if source.lower().endswith(".md"):
        return _chunk_markdown(text, source)
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) < CHUNK_SIZE:
            current += para + "\n"
        else:
            if current.strip():
                chunks.append({
                    "text": current.strip(),
                    "source": source,
                    "chunk_index": len(chunks),
                })
                # overlap：保留最后一段文本作为下一块的开头
                if CHUNK_OVERLAP > 0 and current.strip():
                    words = current.split()
                    overlap_text = " ".join(words[-CHUNK_OVERLAP:]) if len(words) > CHUNK_OVERLAP else current.strip()
                    current = overlap_text + "\n" + para + "\n"
                else:
                    current = para + "\n"
            else:
                current = para + "\n"

    if current.strip():
        cat = _guess_category(source)
        chunks.append({
            "text": current.strip(),
            "source": source,
            "chunk_index": len(chunks),
            "category": cat,
        })

    return chunks


def _chunk_markdown(text: str, source: str) -> List[Dict]:
    """Markdown 感知分块：按 ##/### 标题分割，保持语义完整性"""
    cat = _guess_category(source)
    lines = text.split("\n")

    # 找到所有标题行及其位置
    headings = []  # [(level, title_text, line_index), ...]
    for i, line in enumerate(lines):
        m = re.match(r'^(#{1,4})\s+(.+?)(?:\s+#+)?$', line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            headings.append((level, title, i))

    # 如果没有标题，回退到按段落分块（带 overlap）
    if len(headings) < 2:
        return _chunk_by_paragraph(text, source, cat)

    # 按标题分割
    sections = []
    for j, (level, title, start) in enumerate(headings):
        end = headings[j + 1][2] if j + 1 < len(headings) else len(lines)
        section_text = "\n".join(lines[start:end]).strip()
        sections.append((title, section_text))

    chunks = []
    for title, section_text in sections:
        if not section_text:
            continue
        # 如果 section 不超过 chunk_size，整段保留
        if len(section_text) <= CHUNK_SIZE * 1.5:
            chunks.append({
                "text": f"# {title}\n\n{section_text}",
                "source": source,
                "chunk_index": len(chunks),
                "category": cat,
            })
        else:
            # 长 section 再按段落分块（保留标题前缀）
            sub_chunks = _chunk_by_paragraph(section_text, source, cat,
                                              prefix=f"# {title}\n\n")
            chunks.extend(sub_chunks)

    return chunks


def _chunk_by_paragraph(text: str, source: str, category: str = "",
                         prefix: str = "") -> List[Dict]:
    """按段落分块（带 overlap）"""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    current = prefix if prefix else ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) < CHUNK_SIZE:
            current += para + "\n"
        else:
            if current.strip():
                chunks.append({
                    "text": current.strip(),
                    "source": source,
                    "chunk_index": len(chunks),
                    "category": category,
                })
                if CHUNK_OVERLAP > 0 and current.strip():
                    words = current.split()
                    overlap_text = " ".join(words[-CHUNK_OVERLAP:]) if len(words) > CHUNK_OVERLAP else current.strip()
                    current = (prefix if prefix else "") + overlap_text + "\n" + para + "\n"
                else:
                    current = (prefix if prefix else "") + para + "\n"
            else:
                current = (prefix if prefix else "") + para + "\n"

    if current.strip():
        chunks.append({
            "text": current.strip(),
            "source": source,
            "chunk_index": len(chunks),
            "category": category,
        })

    return chunks


def _get_existing_sources_fast(table) -> set:
    """只查 source 列，不全量加载（优化内存和速度）"""
    try:
        # 使用 PyArrow 表直接读取 source 列，兼容中文编码
        arrow_table = table.to_lance().to_table(columns=["source"])
        col = arrow_table.column("source")
        sources = set()
        for i in range(len(col)):
            val = col[i]
            src = val.as_py() if hasattr(val, 'as_py') else str(val)
            if src:
                sources.add(src)
        return sources
    except Exception:
        # 降级：逐批读取（兼容不同 LanceDB 版本）
        sources = set()
        try:
            for batch in table.to_batches(columns=["source"]):
                for val in batch.column("source"):
                    src = val.as_py() if hasattr(val, 'as_py') else str(val)
                    if src:
                        sources.add(src)
        except Exception:
            pass
        return sources


# =============================================================
# 查询扩展 + 类别推断
# =============================================================


def _build_expansion_table() -> Dict[str, set]:
    """构建统一的查询扩展表：合并 QUERY_EXPANSIONS + QUERY_EXPANSION_GROUPS"""
    table = {}
    for abbr, expansions in QUERY_EXPANSIONS.items():
        table[abbr] = set(expansions)

    for group in QUERY_EXPANSION_GROUPS:
        for term in group:
            if term not in table:
                table[term] = set()
            table[term].update(g for g in group if g != term)

    return table


_EXPANSION_TABLE = _build_expansion_table()


def _expand_query(query: str) -> str:
    """
    展开查询中的缩写/别名，提升 FTS/BM25 召回率。
    例如 "k8s deployment" → "kubernetes k8s deployment"
    """
    words = re.findall(r'\b\w+\b', query.lower())
    extra_terms = []
    seen = set()

    for w in words:
        if w in _EXPANSION_TABLE:
            for term in _EXPANSION_TABLE[w]:
                t = term.lower()
                if t not in seen and t not in words and t != w:
                    extra_terms.append(term)
                    seen.add(t)

    if not extra_terms:
        return query

    return query + " " + " ".join(extra_terms)


def _guess_category(source_path: str) -> str:
    """
    从文件路径推测类别（基于 CATEGORY_MAPPINGS 的文件夹名匹配）。
    例如 "D:/papers/paper1.pdf" → "paper"
    """
    if not source_path:
        return ""
    parts = source_path.replace("\\", "/").split("/")
    for part in parts:
        part_lower = part.lower()
        if part_lower in CATEGORY_MAPPINGS:
            return CATEGORY_MAPPINGS[part_lower]
        for key, cat in CATEGORY_MAPPINGS.items():
            if key in part_lower:
                return cat
    return ""


def _keyword_routing(query: str) -> Dict[str, float]:
    """
    检测查询中的关键词，返回类别→提升权重。
    返回空 dict = 不路由。
    """
    q_lower = query.lower()
    boosts = {}
    for category, keywords in KEYWORD_ROUTES.items():
        for kw in keywords:
            if kw.lower() in q_lower:
                boosts[category] = max(boosts.get(category, 0), 1.2)
                break
    return boosts


def _apply_category_boost(docs: List[Dict], boosts: Dict[str, float]) -> List[Dict]:
    """
    对匹配关键路由类别的文档施加排名提升。
    在 RRF 之后/reranker 之前调用。
    """
    if not boosts:
        return docs
    for d in docs:
        cat = d.get("category", "")
        if cat in boosts:
            d["_category_boost"] = True
    boosted = [d for d in docs if d.get("_category_boost")]
    normal = [d for d in docs if not d.get("_category_boost")]
    return boosted + normal


# =============================================================
# MCP 工具
# =============================================================


def _resolve_db_path(project: str) -> str | None:
    """
    根据 project 参数解析临时知识库路径（不改变全局状态）。
    支持：
      - 注册表名称: "global", "project-通用"
      - 项目简称: "通用", "小论文（北松区）"
      - 完整路径
      - 相对 cherry-workplace 的项目名
    """
    if not project:
        return None

    registry = _load_registry()

    # 1. 精确注册表名称
    if project in registry:
        return registry[project]["path"]
    canonical_name = _kb_aliases.get(project)
    if canonical_name in registry:
        return registry[canonical_name]["path"]

    # 2. 去掉 project- 前缀重试
    for name, info in registry.items():
        clean_name = name.replace("project-", "")
        if clean_name == project or name.lower().endswith(project.lower()):
            return info["path"]

    # 3. 作为文件路径（知识库目录尚未创建时也可解析——父级存在且形似知识库目录）
    if os.path.isdir(project):
        return project
    parent = os.path.dirname(project)
    base_name = os.path.basename(project).lower()
    if parent and os.path.isdir(parent):
        if base_name == "knowledge":
            return project
        # 旧式 lancedb_data 库必须已含 my_docs.lance 才算有效（与自动检测的 legacy 策略一致）
        if base_name == "lancedb_data" and os.path.isdir(os.path.join(project, "my_docs.lance")):
            return project

    # 4. 作为 cherry-workplace 下的项目名（项目根存在即可，知识库目录由 get_db() 惰性创建）
    base = r"D:\cherry-workplace"
    project_root = os.path.join(base, project)
    if os.path.isdir(project_root):
        legacy = os.path.join(project_root, "lancedb_data")
        if os.path.isdir(os.path.join(legacy, "my_docs.lance")):
            return legacy
        return os.path.join(project_root, ".reasonix", "knowledge")

    return None


def _rrf_fusion(vector_results, fts_results, k: int = 60) -> List[Dict]:
    """Reciprocal Rank Fusion: 融合向量和 FTS 排名"""
    scores = {}
    doc_map = {}

    for rank, doc in enumerate(vector_results):
        key = (doc["source"], doc["chunk_index"])
        scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
        doc_map[key] = doc

    for rank, doc in enumerate(fts_results):
        key = (doc["source"], doc["chunk_index"])
        scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
        if key not in doc_map:
            doc_map[key] = doc

    # 按融合分数排序
    sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [doc_map[key] for key in sorted_keys]


def search_knowledge_structured(
    query: str,
    project: str = "",
    limit: int = 20,
    use_reranker: bool = True,
    source_filter: str = "",
    search_mode: str = "vector",
    category_filter: str = "",
) -> dict:
    """Shared structured retrieval entry point for MCP formatting and the desktop browser."""
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            raise ValueError(f"未找到知识库「{project}」。")
        db = lancedb.connect(db_path)
    else:
        db = get_db()
    if TABLE_NAME not in db.table_names():
        raise ValueError("知识库为空，请先添加文档。")
    _ensure_index(db.open_table(TABLE_NAME))
    return search_with_trace(
        db,
        query,
        mode=search_mode,
        use_reranker=use_reranker,
        source_filter=source_filter,
        category_filter=category_filter,
        limit=max(1, limit),
        embedder=get_embedding,
        reranker=rerank_results,
        query_expander=_expand_query,
        category_router=_keyword_routing,
    )


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_knowledge",
    description="搜索本地文档知识库。支持向量搜索、关键词搜索、混合搜索。可指定来源过滤、类别过滤、分页和片段模式。"
)
def search_knowledge(
    query: str,
    project: str = "",
    limit: int = 5,
    use_reranker: bool = True,
    source_filter: str = "",
    page: int = 1,
    page_size: int = 5,
    search_mode: str = "vector",
    snippet_mode: bool = True,
    category_filter: str = "",
) -> str:
    """
    搜索本地知识库。

    Args:
        query: 搜索关键词/问题
        project: 指定知识库名称或路径（如 "project-通用"），空则使用当前知识库
        limit: 返回结果数（1-20）
        use_reranker: 是否使用 Reranker 精排（默认开启，需要联网调用硅基流动 API）
        source_filter: 来源过滤（如 "paper.pdf" 或 "notes/"），只返回匹配的结果
        page: 页码（从 1 开始）
        page_size: 每页条数（默认 5）
        search_mode: 搜索模式 - "vector"(向量搜索，默认), "text"(关键词搜索), "hybrid"(混合搜索，向量+关键词融合)
        snippet_mode: 是否截断结果为片段（默认开启，截断 ~500 字，减少 ~70% token 消耗）
        category_filter: 类别过滤（如 "paper"、"code"），只返回匹配类别的结果
    """
    limit = min(max(limit, 1), 20)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 20)
    requested = min(max(limit * page, page * page_size), 80)
    try:
        structured = search_knowledge_structured(
            query=query,
            project=project,
            limit=requested,
            use_reranker=use_reranker,
            source_filter=source_filter,
            search_mode=search_mode,
            category_filter=category_filter,
        )
    except Exception as error:
        return f"❌ 搜索失败: {error}"

    docs = [{**item} for item in structured["results"]]
    if not docs:
        return f"未搜索到与「{query}」相关的结果（模式: {search_mode}）。"
    if snippet_mode:
        for document in docs:
            if len(document["text"]) <= 500:
                continue
            document["content_length"] = len(document["text"])
            text = document["text"][:500]
            last_period = max(text.rfind(". "), text.rfind("。"), text.rfind("\n"), text.rfind(" "))
            document["text"] = (text[:last_period + 1] if last_period > 200 else text) + "..."

    total_results = len(docs)
    total_pages = max(1, (total_results + page_size - 1) // page_size)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_docs = docs[start_idx:end_idx]

    if not page_docs:
        return f"未找到第 {page} 页的结果（共 {total_results} 条，{total_pages} 页）。"

    # 9. 格式化输出
    filter_info = f"（来源: {source_filter}）" if source_filter else ""
    cat_info = f"（类别: {category_filter}）" if category_filter else ""
    mode_label = {"vector": "向量", "text": "关键词", "hybrid": "混合"}[search_mode]
    expand_note = " (已展开查询)" if structured["expanded_query"] != query else ""
    snippet_note = " [片段模式]" if snippet_mode else ""
    fallback_note = f"\n   ⚠️ {structured['warning']}" if structured["warning"] else ""
    header = (
        f"🔎 **知识库搜索结果** 「{query}」{filter_info}{cat_info}{expand_note}\n"
        f"   模式: {mode_label}{snippet_note} | 共 {total_results} 条结果 | 第 {page}/{total_pages} 页\n"
        f"   候选: {structured['candidate_count']} | Reranker: {structured['reranker_status']}"
        f"{fallback_note}\n{'─' * 60}"
    )

    lines = []
    for i, d in enumerate(page_docs, start_idx + 1):
        source = d.get("source", "?")
        fname = source.split("\\")[-1] if "\\" in source else source.split("/")[-1]
        chunk_info = f"#{d.get('chunk_index', 0)}"
        score = d.get("relevance_score", None)
        score_str = f" ★{score:.2f}" if score else ""
        cat = d.get("category", "")
        cat_str = f" [{cat}]" if cat else ""
        size_str = f" (原 {d['content_length']} 字)" if d.get("content_length") else ""

        lines.append(
            f"{i}. **{fname}** {chunk_info}{score_str}{cat_str}{size_str}\n"
            f"   来源: {source}\n"
            f"   {d.get('text', '')[:300]}"
        )

    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_knowledge_status",
    description="查看知识库状态：文档数量、数据库位置、索引状态等信息。"
)
def get_knowledge_status() -> str:
    """查看知识库状态"""
    db = get_db()

    # 可用 KB 列表
    registry = _load_registry()
    kb_lines = []
    for name, info in sorted(registry.items()):
        marker = " ⬅️ 当前" if name == _current_kb_name else ""
        kb_lines.append(f"  - {name}{marker} ({info['path']})")

    kb_section = (
        f"\n\n📚 **可用知识库** （{len(registry)} 个）\n"
        + "\n".join(kb_lines)
        + f"\n💡 使用 switch_knowledge_base(name) 切换，或指定 project= 参数"
    ) if registry else ""

    if TABLE_NAME not in db.table_names():
        return (
            f"📊 **知识库状态**\n"
            f"{'─' * 40}\n"
            f"当前知识库: {_current_kb_name}\n"
            f"数据库位置: {DB_PATH}\n"
            f"总文档块: 0（知识库为空）\n"
            f"Embedding 后端: {EMBEDDING_BACKEND}\n"
            f"Embedding 模型: {EMBED_MODEL if EMBEDDING_BACKEND == 'api' else LOCAL_EMBED_MODEL} (dim={EMBED_DIM})\n"
            f"Reranker 后端: {RERANKER_BACKEND}\n"
            f"Reranker 模型: {RERANK_MODEL if RERANKER_BACKEND == 'api' else LOCAL_RERANK_MODEL}\n"
            f"Embedding 缓存: {len(_embedding_cache)} 条\n"
            f"文件格式: {'、'.join(sorted(EXTENSIONS))}"
            + kb_section
            + "\n\n💡 请使用 add_documents 工具添加文档。"
        )

    tbl = db.open_table(TABLE_NAME)
    count = len(tbl)

    # 索引状态
    index_info = "无"
    try:
        stats = tbl.index_stats()
        if stats and stats.get("num_indexed_rows", 0) > 0:
            indexed = stats.get("num_indexed_rows", 0)
            index_info = f"IVF-PQ 已启用（已索引 {indexed} 条）"
    except Exception:
        pass

    # FTS 索引状态
    fts_info = "未启用"
    try:
        tbl.search("test", query_type="fts").limit(1).to_list()
        fts_info = "已启用"
    except Exception:
        pass

    return (
        f"📊 **知识库状态**\n"
        f"{'─' * 40}\n"
        f"当前知识库: {_current_kb_name}\n"
        f"数据库位置: {DB_PATH}\n"
        f"总文档块: {count}\n"
        f"向量索引: {index_info}\n"
        f"全文索引: {fts_info}\n"
        f"Embedding 后端: {EMBEDDING_BACKEND}\n"
        f"Embedding 模型: {EMBED_MODEL if EMBEDDING_BACKEND == 'api' else LOCAL_EMBED_MODEL} (dim={EMBED_DIM})\n"
        f"Reranker 后端: {RERANKER_BACKEND}\n"
        f"Reranker 模型: {RERANK_MODEL if RERANKER_BACKEND == 'api' else LOCAL_RERANK_MODEL}\n"
        f"Embedding 缓存: {len(_embedding_cache)} 条\n"
        f"文件格式: {'、'.join(sorted(EXTENSIONS))}"
        + kb_section
    )


@mcp.tool(
    name="switch_knowledge_base",
    description="切换知识库。name 可以是注册表名称(如 global/project-通用)、项目名称(如 通用/智能测绘)、或完整路径。切换后所有后续操作使用新知识库。"
)
def switch_knowledge_base(name: str = "global") -> str:
    """
    切换当前知识库。

    Args:
        name: 知识库标识。支持注册表名称、项目简称、完整路径。
    """
    global _current_kb_name, _db, _connected_path, DB_PATH

    # 尝试解析（_resolve_db_path 已覆盖注册表名、项目简称、完整路径、cherry-workplace 项目名，
    # 且允许知识库目录尚未创建——切换后由 get_db() 惰性创建）
    target = _resolve_db_path(name)
    if target is None:
        registry = _load_registry()
        available = "\n".join(f"  - {n} ({i['description']})" for n, i in sorted(registry.items()))
        return (
            f"❌ 未找到知识库「{name}」\n"
            f"可用知识库:\n{available}\n"
            f"也支持项目名称（如「通用」「智能测绘」）或完整路径。"
        )

    # 断开旧连接
    if target != _connected_path or _db is None:
        _db = None
        _connected_path = target
        DB_PATH = target
        _current_kb_name = _guess_kb_name(target)
        _embedding_cache = LRUCache(capacity=2000)
        # 触发重连
        result = get_db()
        if result is None:
            return f"❌ 无法连接到知识库: {target}"

    return f"✅ 已切换到知识库: {_current_kb_name}\n路径: {DB_PATH}"


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="list_knowledge_bases",
    description="列出所有可用的知识库（含注册表和自动发现），并标记当前激活的。"
)
def list_knowledge_bases() -> str:
    """列出所有可用的知识库"""
    registry = _load_registry()
    if not registry:
        return "暂无注册的知识库。\n在使用 add_documents 时将自动创建项目知识库。"

    lines = [f"📚 知识库列表（共 {len(registry)} 个）", "─" * 40]
    for name, info in sorted(registry.items()):
        marker = " ⬅️ 当前" if name == _current_kb_name else ""
        desc = info.get("description", "")
        lines.append(f"  {name}{marker}")
        lines.append(f"    路径: {info['path']}")
        if desc:
            lines.append(f"    说明: {desc}")
        lines.append("")

    lines.append(f"当前激活: {_current_kb_name}")
    lines.append(f"当前路径: {DB_PATH}")
    lines.append("💡 使用 switch_knowledge_base(name) 切换")

    return "\n".join(lines)


@mcp.tool(
    name="add_documents",
    description="扫描指定目录，将文档（.md/.pdf/.docx/.txt/.py 等）向量化后存入知识库。"
)
def add_documents(
    scan_dir: str,
    project: str = "",
    reindex_all: bool = False,
) -> str:
    """
    扫描目录并向量化文档。

    Args:
        scan_dir: 要扫描的目录路径，如 "D:\\cherry-workplace\\通用"
        project: 指定知识库名称或路径（如 "project-通用"），空则使用当前知识库
        reindex_all: 是否重建全部索引（清空后重新添加）。默认只增量添加新文件。
    """
    if not os.path.isdir(scan_dir):
        return f"❌ 目录不存在: {scan_dir}"

    # 支持 project 参数 —— 连接到指定知识库
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。使用 list_knowledge_bases 查看可用知识库。"
        db = lancedb.connect(db_path)
        if TABLE_NAME in _db_table_names(db):
            table = db.open_table(TABLE_NAME)
        else:
            table = get_or_create_table(db)
        kb_label = f"{_guess_kb_name(db_path)}（{db_path}）"
    else:
        db = get_db()
        table = get_or_create_table()
        kb_label = f"{_current_kb_name}（{DB_PATH}）"

    # 如果需要重建
    if reindex_all and TABLE_NAME in _db_table_names(db):
        db.drop_table(TABLE_NAME)
        # 用同一个 project 库连接重建，避免 drop 后写入全局库
        table = get_or_create_table(db)
        existing_sources = set()
    else:
        # 优化：只查 source 列，不全量加载
        existing_sources = set()
        if TABLE_NAME in _db_table_names(db):
            existing_sources = _get_existing_sources_fast(table)

    # 1. 扫描文件
    filepaths = []
    for root, dirs, files in os.walk(scan_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in EXTENSIONS:
                continue
            fp = os.path.join(root, f)
            if os.path.getsize(fp) > MAX_FILE_SIZE:
                continue
            filepaths.append(fp)

    filepaths = sorted(set(filepaths))
    # 转为相对路径再比对（existing_sources 存的是 relpath）
    new_files = [fp for fp in filepaths if os.path.relpath(fp, scan_dir) not in existing_sources]

    if not new_files:
        msg = f"扫描「{scan_dir}」，共 {len(filepaths)} 个文件，没有新文件需要添加。"
        if existing_sources:
            msg += f"（已有 {len(existing_sources)} 个文件在知识库中）"
        msg += f"\n📚 当前知识库: {kb_label}"
        return msg

    if not reindex_all and existing_sources:
        msg_extra = f"（跳过 {len(existing_sources)} 个已有文件）"
    else:
        msg_extra = ""

    # 2. 提取 + 分块
    all_chunks = []
    for fp in new_files:
        text = extract_text(fp)
        if not text or len(text) < 20:
            continue
        rel_path = os.path.relpath(fp, scan_dir) if scan_dir else fp
        chunks = chunk_text(text, rel_path)
        all_chunks.extend(chunks)

    if not all_chunks:
        return f"扫描完成，但没有提取到有效文本内容。"

    # 3. 批量向量化 + 写入 LanceDB（batch_size=32，吃满 API 限制）
    batch_size = 32
    total = len(all_chunks)
    added = 0

    for i in range(0, total, batch_size):
        batch = all_chunks[i:i + batch_size]
        texts = [b["text"][:512] for b in batch]

        try:
            vectors = get_embeddings_batch(texts)
        except Exception as e:
            return f"❌ Embedding API 调用失败（第 {i} 批）: {e}"

        data = []
        for j, b in enumerate(batch):
            data.append({
                "vector": vectors[j],
                "text": b["text"],
                "source": b["source"],
                "chunk_index": b["chunk_index"],
                "category": b.get("category", ""),
            })

        table.add(data)
        added += len(data)

    # 4. 全部添加完成后，尝试创建/更新索引
    _ensure_index(table)
    _ensure_fts_index(table)

    # 5. 最终状态
    final_count = len(table)
    return (
        f"✅ **文档添加完成！**\n"
        f"{'─' * 40}\n"
        f"📚 知识库: {kb_label}\n"
        f"扫描目录: {scan_dir}\n"
        f"新扫描文件: {len(new_files)}\n"
        f"新提取文本块: {total}\n"
        f"已保存文本资产: {assets_added}\n"
        f"已向量化入库: {added}\n"
        f"知识库总块数: {final_count}\n"
        f"{msg_extra}"
    )


@mcp.tool(
    name="add_single_document",
    description="向量化单个文档到知识库（一次只处理一个文件，避免大批量超时）。支持 .md/.pdf/.docx/.txt 等格式。"
)
def add_single_document(
    filepath: str,
    project: str = "",
) -> str:
    """
    向量化单个文档并存入知识库。

    Args:
        filepath: 要添加的文件路径
        project: 指定知识库名称或路径，空则使用当前知识库

    v2.0 优化：改用批量 Embedding API，N 次请求 → ceil(N/32) 次。
    """
    if not os.path.isfile(filepath):
        return f"[ERROR] 文件不存在: {filepath}"

    ext = Path(filepath).suffix.lower()
    if ext not in EXTENSIONS:
        return f"[ERROR] 不支持的文件格式: {ext}"

    if os.path.getsize(filepath) > MAX_FILE_SIZE:
        return f"[ERROR] 文件过大: {os.path.getsize(filepath) / 1024 / 1024:.1f}MB"

    text = extract_text(filepath)
    if not text or len(text) < 20:
        return f"[ERROR] 无法从文件中提取有效文本: {os.path.basename(filepath)}"

    fname = os.path.basename(filepath)
    chunks = chunk_text(text, fname)
    if not chunks:
        return f"[ERROR] 分块后无有效内容: {fname}"

    # 支持 project 参数
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
        table = get_or_create_table(db)
    else:
        table = get_or_create_table()

    # 批量向量化（batch_size=32），替代逐条调用
    batch_size = 32
    added = 0
    failed = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts_to_embed = [ch["text"][:512] for ch in batch]

        try:
            vectors = get_embeddings_batch(texts_to_embed)
        except Exception as e:
            # 批量失败时降级为逐条
            for ch in batch:
                try:
                    vec = get_embedding(ch["text"][:512])
                    table.add([{
                        "vector": vec,
                        "text": ch["text"],
                        "source": ch["source"],
                        "chunk_index": ch["chunk_index"],
                        "category": ch.get("category", ""),
                    }])
                    added += 1
                except Exception:
                    failed += 1
            continue

        data = []
        for j, ch in enumerate(batch):
            data.append({
                "vector": vectors[j],
                "text": ch["text"],
                "source": ch["source"],
                "chunk_index": ch["chunk_index"],
                "category": ch.get("category", ""),
            })

        table.add(data)
        added += len(data)

    # 尝试更新索引
    _ensure_index(table)
    _ensure_fts_index(table, force_rebuild=True)

    final_count = len(table)
    result = (
        "[OK] 文档已入库!\n"
        + f"文件名: {fname}\n"
        + f"总文本块: {len(chunks)}\n"
        + f"已入库: {added}\n"
    )
    if failed > 0:
        result += f"失败: {failed}\n"
    result += f"知识库总块数: {final_count}"
    return result



@mcp.tool(
    name="update_document",
    description="更新知识库中的文档（删除旧版本后重新添加）。用于文档内容变更后的重新索引。"
)
def update_document(
    filepath: str,
    project: str = "",
) -> str:
    """
    更新知识库中的文档：先删除旧版本，再重新向量化入库。

    Args:
        filepath: 要更新的文件路径
        project: 指定知识库名称或路径，空则使用当前知识库
    """
    if not os.path.isfile(filepath):
        return f"[ERROR] 文件不存在: {filepath}"

    ext = Path(filepath).suffix.lower()
    if ext not in EXTENSIONS:
        return f"[ERROR] 不支持的文件格式: {ext}"

    fname = os.path.basename(filepath)

    # 支持 project 参数
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
        table = get_or_create_table(db) if TABLE_NAME in db.table_names() else None
    else:
        db = get_db()
        table = db.open_table(TABLE_NAME) if TABLE_NAME in db.table_names() else None

    # 1. 删除旧版本（LanceDB 原生 delete，无需全量加载向量）
    deleted_count = 0
    if table:
        # 找到匹配的来源（按文件名匹配）
        all_sources = _get_existing_sources_fast(table)
        matched = {s for s in all_sources if fname.lower() in s.lower()}

        if matched:
            for src in matched:
                safe_src = src.replace("'", "''")
                table.delete(f"source = '{safe_src}'")
            deleted_count = len(matched)

    # 2. 重新添加
    text = extract_text(filepath)
    if not text or len(text) < 20:
        return f"[ERROR] 无法从文件中提取有效文本: {fname}"

    chunks = chunk_text(text, fname)
    if not chunks:
        return f"[ERROR] 分块后无有效内容: {fname}"

    if project:
        table = get_or_create_table(db)
    else:
        table = get_or_create_table()

    # 批量向量化
    batch_size = 32
    added = 0
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts_to_embed = [ch["text"][:512] for ch in batch]
        try:
            vectors = get_embeddings_batch(texts_to_embed)
        except Exception as e:
            return f"[ERROR] Embedding API 调用失败: {e}"

        data = []
        for j, ch in enumerate(batch):
            data.append({
                "vector": vectors[j],
                "text": ch["text"],
                "source": ch["source"],
                "chunk_index": ch["chunk_index"],
                "category": ch.get("category", ""),
            })
        table.add(data)
        added += len(data)

    _ensure_index(table)
    _ensure_fts_index(table, force_rebuild=True)

    final_count = len(table)
    return (
        f"✅ 文档已更新!\n"
        f"文件名: {fname}\n"
        f"删除旧记录: {deleted_count}\n"
        f"新增记录: {added}\n"
        f"知识库总块数: {final_count}"
    )


@mcp.tool(
    name="delete_documents",
    description="删除知识库中的文档。按来源文件名/路径模式删除，或清空整个知识库。"
)
def delete_documents(
    source_pattern: str = "",
    project: str = "",
    confirm_all: bool = False,
) -> str:
    """
    删除知识库中的文档。

    Args:
        source_pattern: 来源文件名或路径的模糊匹配模式（如 "paper.pdf" 或 "notes/"）。
                       留空则需要 confirm_all=True 才能清空全部。
        project: 指定知识库名称或路径，空则使用当前知识库
        confirm_all: 确认清空整个知识库（仅当 source_pattern 为空时生效）。
    """
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
    else:
        db = get_db()
    if TABLE_NAME not in db.table_names():
        return "知识库为空，无需删除。"

    table = db.open_table(TABLE_NAME)
    total_before = len(table)

    if source_pattern:
        # 按来源模式删除（LanceDB 原生 delete，无需全量加载向量）
        all_sources = _get_existing_sources_fast(table)
        matched_sources = {s for s in all_sources if source_pattern.lower() in s.lower()}

        if not matched_sources:
            return f"未找到匹配「{source_pattern}」的文档。知识库中共有 {total_before} 条记录。"

        # 逐个删除匹配的 source
        for src in matched_sources:
            safe_src = src.replace("'", "''")
            table.delete(f"source = '{safe_src}'")

        remaining = len(table)
        deleted = total_before - remaining

        return (
            f"🗑️ **文档删除完成**\n"
            f"{'─' * 40}\n"
            f"匹配模式: {source_pattern}\n"
            f"匹配到的来源: {len(matched_sources)} 个\n"
            f"删除记录数: {deleted}\n"
            f"剩余记录数: {remaining}\n"
            f"匹配文件: {', '.join(sorted(matched_sources)[:10])}"
            + (f"\n... 等共 {len(matched_sources)} 个" if len(matched_sources) > 10 else "")
        )

    elif confirm_all:
        # 清空全部
        db.drop_table(TABLE_NAME)
        return (
            f"🗑️ **知识库已清空**\n"
            f"{'─' * 40}\n"
            f"删除记录数: {total_before}\n"
        )

    else:
        return (
            f"⚠️ 请指定 source_pattern 进行选择性删除，或设置 confirm_all=True 清空全部。\n"
            f"当前知识库共 {total_before} 条记录。"
        )


@mcp.tool(
    name="rebuild_knowledge",
    description="重建知识库向量索引：读取已有文本，用当前 Embedding 后端重新编码后写回。用于切换模型后修复旧数据。"
)
def rebuild_knowledge(
    project: str = "",
    batch_size: int = 32,
) -> str:
    """
    重建知识库：用当前 EMBEDDING_BACKEND 重新编码所有已有文本块。
    切换 Embedding 模型后（如 API → local），旧向量与新模型不兼容，需要用此工具重建。

    Args:
        project: 指定知识库名称或路径（如 "global"、"project-通用"），空则使用当前知识库
        batch_size: 每批编码数量（默认 32）
    """
    # 1. 获取数据库和表
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。使用 list_knowledge_bases 查看可用知识库。"
        db = lancedb.connect(db_path)
    else:
        db = get_db()
        db_path = DB_PATH

    if TABLE_NAME not in db.table_names():
        return "❌ 知识库为空，没有需要重建的数据。"

    old_table = db.open_table(TABLE_NAME)
    total = len(old_table)
    if total == 0:
        return "❌ 知识库为空，没有需要重建的数据。"

    # 2. 读取所有记录（只读 text、source、chunk_index，不读旧向量）
    try:
        import pandas as pd
        df = old_table.to_pandas(columns=["text", "source", "chunk_index"])
        texts = df["text"].tolist()
        sources = df["source"].tolist()
        chunk_indices = [int(x) for x in df["chunk_index"].tolist()]
    except ImportError:
        return "❌ 需要 pandas 库。请执行: pip install pandas"

    print(f"[rebuild] 读取 {total} 条记录，开始重新编码...", file=sys.stderr)

    # 3. 批量重新编码
    all_vectors = []
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i + batch_size]
        batch_truncated = [t[:512] for t in batch_texts]
        try:
            vecs = get_embeddings_batch(batch_truncated)
            all_vectors.extend(vecs)
        except Exception as e:
            return f"❌ 编码失败（第 {i} 批）: {e}"
        print(f"[rebuild] 进度: {min(i+batch_size, total)}/{total}", file=sys.stderr)

    # 4. 删除旧表，创建新表
    db.drop_table(TABLE_NAME)
    new_table = get_or_create_table(db)

    # 5. 写回新数据
    records = []
    for i in range(total):
        records.append({
            "vector": all_vectors[i],
            "text": texts[i],
            "source": sources[i],
            "chunk_index": chunk_indices[i],
            "category": "",
        })

    new_table.add(records)

    # 6. 重建索引
    _ensure_index(new_table)
    _ensure_fts_index(new_table, force_rebuild=True)

    global _embedding_cache
    # 7. 清空 embedding 缓存（旧向量已无效）
    _embedding_cache = LRUCache(capacity=2000)

    final_count = len(new_table)
    display_name = _guess_kb_name(db_path) if project else _current_kb_name

    return (
        f"✅ **知识库重建完成！**\n"
        f"{'─' * 40}\n"
        f"知识库: {display_name}\n"
        f"总记录数: {final_count}\n"
        f"Embedding 后端: {EMBEDDING_BACKEND}\n"
        f"模型: {EMBED_MODEL if EMBEDDING_BACKEND == 'api' else LOCAL_EMBED_MODEL} (dim={EMBED_DIM})\n"
        f"缓存已清空（旧向量已无效）\n"
        f"💡 现在可用新模型搜索此知识库。"
    )


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="list_documents",
    description="列出知识库中的所有文档及其统计信息（chunk 数、类别等）。"
)
def list_documents(
    project: str = "",
    category_filter: str = "",
    limit: int = 50,
) -> str:
    """
    列出知识库中的所有文档及其统计信息。

    Args:
        project: 指定知识库名称或路径，空则使用当前知识库
        category_filter: 按类别过滤（如 "paper" 只列出论文类文档）
        limit: 最大返回文档数（默认 50，最大 200）
    """
    limit = min(max(limit, 1), 200)

    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
    else:
        db = get_db()

    if TABLE_NAME not in db.table_names():
        return "❌ 知识库为空，没有文档。"

    table = db.open_table(TABLE_NAME)
    total = len(table)

    # 聚合查询：按 source 分组统计
    try:
        import pandas as pd
        df = table.to_pandas(columns=["source", "category"])
    except ImportError:
        # 无 pandas 时回退
        sources = _get_existing_sources_fast(table)
        lines = [
            f"📄 **文档列表** （共 {len(sources)} 个）",
            f"{'─' * 40}",
        ]
        for s in sorted(sources)[:limit]:
            lines.append(f"  - {s}")
        if len(sources) > limit:
            lines.append(f"  ... 等共 {len(sources)} 个")
        lines.append(f"\n知识库总块数: {total}")
        return "\n".join(lines)

    # 按文档名分组统计
    doc_groups = df.groupby("source").agg(
        chunk_count=("source", "count"),
        category=("category", "first"),
    ).reset_index()

    # 按 chunk 数降序排列
    doc_groups = doc_groups.sort_values("chunk_count", ascending=False)

    # 类别过滤
    if category_filter:
        doc_groups = doc_groups[
            doc_groups["category"].str.lower() == category_filter.lower()
        ]

    total_docs = len(doc_groups)
    display = doc_groups.head(limit)

    # 类别统计
    cat_counts = df["category"].value_counts().to_dict() if "category" in df.columns else {}
    cat_summary = " | ".join(f"{k}: {v}" for k, v in sorted(cat_counts.items(), key=lambda x: -x[1]) if k)

    lines = [
        f"📄 **文档列表** （共 {total_docs} 个，显示前 {min(limit, total_docs)} 个）",
        f"{'─' * 40}",
        f"知识库总块数: {total}",
    ]
    if cat_summary:
        lines.append(f"类别分布: {cat_summary}")
    lines.append("")

    for _, row in display.iterrows():
        fname = row["source"]
        cnt = int(row["chunk_count"])
        cat = row.get("category", "")
        cat_str = f" [{cat}]" if cat else ""
        lines.append(f"  {fname}{cat_str} — {cnt} 块")

    return "\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_similar",
    description="基于嵌入相似度查找与给定文档最相似的文档。"
)
def search_similar(
    filepath: str,
    project: str = "",
    max_results: int = 5,
) -> str:
    """
    找到与指定文档最相似的已有文档（以文搜文）。

    Args:
        filepath: 参考文档的路径
        project: 指定知识库名称或路径，空则使用当前知识库
        max_results: 返回相似文档数（1-20）
    """
    max_results = min(max(max_results, 1), 20)

    if not os.path.isfile(filepath):
        return f"❌ 文件不存在: {filepath}"

    # 提取文本
    text = extract_text(filepath)
    if not text or len(text) < 20:
        return f"❌ 无法从文件中提取有效文本: {os.path.basename(filepath)}"

    # 向量化查詢
    try:
        query_vec = get_embedding(text[:512])
    except Exception as e:
        return f"❌ Embedding 调用失败: {e}"

    # 相似搜索
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
    else:
        db = get_db()

    if TABLE_NAME not in db.table_names():
        return "❌ 知识库为空。"

    tbl = db.open_table(TABLE_NAME)
    try:
        results = tbl.search(query_vec).limit(max_results + 1).to_list()
    except Exception as e:
        return f"❌ 搜索失败: {e}"

    if not results:
        return "未找到相似文档。"

    fname_ref = os.path.basename(filepath)
    lines = [
        f"🔍 **相似文档搜索** 参考: {fname_ref}",
        f"{'─' * 60}",
    ]

    for i, r in enumerate(results[:max_results]):
        source = r.get("source", "?")
        dist = r.get("_distance", 0)
        similarity = 1 - dist  # 余弦距离转相似度
        snippet = r.get("text", "")[:200].replace("\n", " ")
        lines.append(
            f"{i + 1}. **{source}** 相似度: {similarity:.4f}\n"
            f"   {snippet}..."
        )

    return "\n\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_document",
    description="获取知识库中文档的完整内容。配合 snippet_mode 使用，查看被截断的原文。"
)
def get_document(
    filepath: str,
    project: str = "",
) -> str:
    """
    获取知识库中指定文档的完整文本内容。
    用于查看被 snippet_mode 截断的长文本。

    Args:
        filepath: 文档路径或文件名（支持模糊匹配）
        project: 指定知识库名称或路径，空则使用当前知识库
    """
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
    else:
        db = get_db()

    if TABLE_NAME not in db.table_names():
        return "❌ 知识库为空。"

    table = db.open_table(TABLE_NAME)

    # 精确匹配或模糊匹配
    try:
        arrow_table = table.to_lance().to_table(columns=["source", "text", "chunk_index"])
        col = arrow_table.column("source")
        texts_col = arrow_table.column("text")
        idx_col = arrow_table.column("chunk_index")

        matched = []
        for i in range(len(col)):
            src = col[i].as_py() if hasattr(col[i], 'as_py') else str(col[i])
            if filepath.lower() in src.lower():
                matched.append({
                    "source": src,
                    "text": texts_col[i].as_py() if hasattr(texts_col[i], 'as_py') else str(texts_col[i]),
                    "chunk_index": int(idx_col[i]),
                })
    except Exception:
        return "❌ 读取知识库失败。"

    if not matched:
        return f"未找到匹配「{filepath}」的文档。"

    # 按 source 分组
    from collections import OrderedDict
    groups = OrderedDict()
    for m in matched:
        src = m["source"]
        if src not in groups:
            groups[src] = []
        groups[src].append(m)

    lines = []
    for src, chunks in groups.items():
        chunks.sort(key=lambda x: x["chunk_index"])
        lines.append(f"# 文档: {src}")
        lines.append(f"总块数: {len(chunks)}")
        lines.append(f"{'─' * 40}")
        # 按 chunk_index 顺序拼接全文
        full_text = "\n\n".join(c["text"] for c in chunks)
        lines.append(full_text)
        lines.append("")

    return "\n".join(lines)


@mcp.tool(
    name="ingest_url",
    description="抓取网页内容并索引到知识库。支持 HTML 页面、Markdown、纯文本等。"
)
def ingest_url(
    url: str,
    project: str = "",
) -> str:
    """
    抓取网页内容、提取文本并索引到知识库。

    Args:
        url: 要抓取的网页 URL（如 https://example.com/docs）
        project: 指定知识库名称或路径，空则使用当前知识库
    """
    if not url.startswith(("http://", "https://")):
        return f"❌ 无效的 URL: {url}"

    # 1. 抓取网页
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        return f"❌ 抓取失败: {e}"

    # 2. 提取文本
    content_type = resp.headers.get("Content-Type", "")
    html_text = resp.text

    # 简单提取纯文本（从 HTML 剥离标签）
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")

        # 移除脚本、样式等
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        title = soup.title.string.strip() if soup.title else "Untitled"
        body = soup.find("article") or soup.find("main") or soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # 清理空白行
        text = "\n".join(line.strip() for line in text.split("\n") if line.strip())

        if len(text) < 50:
            return f"❌ 页面内容太少（{len(text)} 字），无法索引: {url}"

        # 加上标题作为前缀
        content = f"# {title}\n\n来源: {url}\n\n{text}"

    except ImportError:
        # 无 BeautifulSoup 时降级为简单提取
        content = html_text
        title = url

    # 3. 分块
    fname = f"网页-{url.split('//')[1].split('/')[0] if '//' in url else url}"
    fname = re.sub(r'[\\/:*?"<>|]', '_', fname)[:80]
    chunks = _chunk_by_paragraph(content, fname)

    if not chunks:
        return f"❌ 无法从页面提取有效内容: {url}"

    # 4. 写入知识库
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
        table = get_or_create_table(db)
    else:
        table = get_or_create_table()

    batch_size = 32
    added = 0
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts_to_embed = [ch["text"][:512] for ch in batch]
        try:
            vectors = get_embeddings_batch(texts_to_embed)
        except Exception:
            # 降级逐条
            for ch in batch:
                try:
                    vec = get_embedding(ch["text"][:512])
                    table.add([{
                        "vector": vec,
                        "text": ch["text"],
                        "source": ch["source"],
                        "chunk_index": ch["chunk_index"],
                        "category": ch.get("category", ""),
                    }])
                    added += 1
                except Exception:
                    pass
            continue

        data = []
        for j, ch in enumerate(batch):
            data.append({
                "vector": vectors[j],
                "text": ch["text"],
                "source": ch["source"],
                "chunk_index": ch["chunk_index"],
                "category": ch.get("category", ""),
            })
        table.add(data)
        added += len(data)

    _ensure_index(table)
    _ensure_fts_index(table, force_rebuild=True)

    final_count = len(table)
    return (
        f"✅ **网页已索引**\n"
        f"{'─' * 40}\n"
        f"标题: {title}\n"
        f"URL: {url}\n"
        f"文本块: {len(chunks)}\n"
        f"已入库: {added}\n"
        f"知识库总块数: {final_count}\n"
        f"💡 用 search_knowledge 搜索此内容。"
    )



# ── 文件监听器（可选依赖 watchdog） ──
_FILE_OBSERVER = None
_FILE_OBSERVER_DIR = None


@mcp.tool(
    name="start_watcher",
    description="启动文件变更监听，当文档目录中的文件被修改/新增/删除时自动重新索引。需要安装 watchdog 依赖。"
)
def start_watcher(
    watch_dir: str = "",
    project: str = "",
) -> str:
    """
    启动文件变更监听，自动增量重索引发生变化的文档。

    Args:
        watch_dir: 要监听的目录（默认使用当前知识库的文档目录）
        project: 指定知识库名称或路径，空则使用当前知识库
    """
    global _FILE_OBSERVER, _FILE_OBSERVER_DIR

    if _FILE_OBSERVER is not None:
        return f"⚠️ 文件监听已在运行中（{_FILE_OBSERVER_DIR}）。先使用 stop_watcher 停止。"

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        return (
            "❌ 需要 watchdog 库。请安装:\n"
            "   pip install watchdog"
        )

    # 确定监听目录
    if not watch_dir:
        watch_dir = os.getcwd()
    if not os.path.isdir(watch_dir):
        return f"❌ 目录不存在: {watch_dir}"

    class _AutoIndexHandler(FileSystemEventHandler):
        def __init__(self, project):
            self.project = project
            self._debounce = {}  # path -> time

        def _reindex(self, filepath):
            """增量重索引单个文件"""
            ext = os.path.splitext(filepath)[1].lower()
            if ext not in EXTENSIONS:
                return
            if any(excl in filepath for excl in EXCLUDE_DIRS):
                return
            if os.path.getsize(filepath) > MAX_FILE_SIZE:
                return

            try:
                if self.project:
                    update_document(filepath, project=self.project)
                else:
                    update_document(filepath)
                print(f"[watcher] 已更新: {filepath}", file=sys.stderr)
            except Exception as e:
                print(f"[watcher] 更新失败 {filepath}: {e}", file=sys.stderr)

        def on_modified(self, event):
            if not event.is_dir and event.src_path.endswith(tuple(EXTENSIONS)):
                # 防抖：同一文件 5 秒内不重复处理
                now = __import__("time").time()
                last = self._debounce.get(event.src_path, 0)
                if now - last > 5:
                    self._debounce[event.src_path] = now
                    self._reindex(event.src_path)

        def on_created(self, event):
            if not event.is_dir and event.src_path.endswith(tuple(EXTENSIONS)):
                self._reindex(event.src_path)

        def on_moved(self, event):
            if not event.is_dir and event.dest_path.endswith(tuple(EXTENSIONS)):
                self._reindex(event.dest_path)

    handler = _AutoIndexHandler(project)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=True)
    observer.start()

    _FILE_OBSERVER = observer
    _FILE_OBSERVER_DIR = watch_dir

    return (
        f"✅ **文件监听已启动**\n"
        f"{'─' * 40}\n"
        f"监听目录: {watch_dir}\n"
        f"文件变更时将自动增量重索引。\n"
        f"💡 使用 stop_watcher 停止监听。"
    )


@mcp.tool(
    name="stop_watcher",
    description="停止文件变更监听。"
)
def stop_watcher() -> str:
    """停止文件变更监听"""
    global _FILE_OBSERVER, _FILE_OBSERVER_DIR

    if _FILE_OBSERVER is None:
        return "⚠️ 没有正在运行的文件监听。"

    try:
        _FILE_OBSERVER.stop()
        _FILE_OBSERVER.join(timeout=5)
    except Exception:
        pass

    _FILE_OBSERVER = None
    dir_name = _FILE_OBSERVER_DIR
    _FILE_OBSERVER_DIR = None

@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="generate_index",
    description="生成知识库的 Markdown 目录索引，方便人类浏览。可保存到指定路径。"
)
def generate_index(
    project: str = "",
    output_path: str = "",
) -> str:
    """
    生成知识库的 Markdown 目录索引，按类别分组显示所有文档及其统计信息。

    Args:
        project: 指定知识库名称或路径，空则使用当前知识库
        output_path: 可选，保存索引到指定文件路径（如 "笔记/知识库索引.md"）
    """
    # 1. 获取连接
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
        kb_name = project
    else:
        db = get_db()
        kb_name = _current_kb_name

    if TABLE_NAME not in db.table_names():
        return "❌ 知识库为空。"

    table = db.open_table(TABLE_NAME)
    total_chunks = len(table)

    # 2. 读取数据
    try:
        import pandas as pd
        df = table.to_pandas(columns=["source", "category"])
    except ImportError:
        # 无 pandas 回退
        sources = _get_existing_sources_fast(table)
        lines = [
            f"# 📚 知识库索引: {kb_name}",
            f"",
            f"> 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')} | 总文档块: {total_chunks}",
            f"",
            f"| 文档 | 块数 |",
            f"|------|------|",
        ]
        # 简单统计
        doc_counts = {}
        for s in sources:
            doc_counts[s] = doc_counts.get(s, 0) + 1
        # 但 sources 是 set，没有块数... 回退到简单列表
        for s in sorted(sources):
            lines.append(f"| {s} | - |")
        content = "\n".join(lines)
        if output_path:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"✅ 索引已保存到 {output_path}\n\n{content}"
        return content

    # 3. 按文档分组统计
    doc_groups = df.groupby("source").agg(
        chunk_count=("source", "count"),
        category=("category", "first"),
    ).reset_index().sort_values("chunk_count", ascending=False)

    # 4. 按类别分组
    category_order = ["paper", "code", "manual", "documentation", "note", "tutorial", "api", ""]
    grouped = {}
    for cat in category_order:
        subset = doc_groups[doc_groups["category"] == cat] if cat else doc_groups[doc_groups["category"] == ""]
        if not subset.empty:
            grouped[cat if cat else "其他"] = subset
    # 剩余未匹配类别
    remaining = doc_groups[~doc_groups["category"].isin(category_order) & (doc_groups["category"] != "")]
    if not remaining.empty:
        for cat in remaining["category"].unique():
            grouped[cat] = remaining[remaining["category"] == cat]

    # 类别统计
    cat_stats = df["category"].value_counts().to_dict()

    # 5. 生成 Markdown
    now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [
        f"# 📚 知识库索引: {kb_name}",
        f"",
        f"> 生成时间: {now} | 总文档块: {total_chunks} | 总文档数: {len(doc_groups)}",
        f"",
    ]

    # 类别概览
    if cat_stats:
        cat_summary = " | ".join(
            f"**{k}**: {v} 块" for k, v in sorted(cat_stats.items(), key=lambda x: -x[1]) if k
        )
        if cat_summary:
            lines.append(f"类别分布: {cat_summary}")
            lines.append("")

    lines.append("---")
    lines.append("")

    for cat_name, group_df in grouped.items():
        cat_label = {
            "paper": "📄 论文", "code": "💻 代码", "manual": "📘 手册",
            "documentation": "📖 文档", "note": "📝 笔记", "tutorial": "🎓 教程",
            "api": "🔌 API", "其他": "📁 未分类",
        }.get(cat_name, f"📁 {cat_name}")

        total = int(group_df["chunk_count"].sum())
        lines.append(f"## {cat_label}（{len(group_df)} 个文档, {total} 块）")
        lines.append("")
        lines.append("| # | 文档 | 块数 | 占比 |")
        lines.append("|---|------|------|------|")

        for idx, (_, row) in enumerate(group_df.iterrows(), 1):
            fname = row["source"]
            cnt = int(row["chunk_count"])
            pct = f"{cnt / total_chunks * 100:.1f}%" if total_chunks > 0 else "-"
            lines.append(f"| {idx} | {fname} | {cnt} | {pct} |")

        lines.append("")

    lines.append("---")
    lines.append(f"> 共 {len(doc_groups)} 个文档，{total_chunks} 个文本块 | 生成时间: {now}")

    content = "\n".join(lines)

    # 6. 可选保存到文件
    if output_path:
        abs_path = output_path
        if not os.path.isabs(abs_path):
            abs_path = os.path.join(os.getcwd(), abs_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ **索引已保存** → `{abs_path}`\n\n{content}"

    return content


@mcp.tool(
    name="extract_to_note",
    description="从知识库提取文档全文，生成结构化摘录笔记并保存到指定目录。新的笔记也会自动索引回知识库。"
)
def extract_to_note(
    filepath: str,
    project: str = "",
    output_dir: str = "",
) -> str:
    """
    从知识库提取文档，生成结构化摘录笔记（Markdown 格式）并保存。
    笔记包含：标题、来源、标签、核心内容分段、待办事项。
    保存后自动将新笔记索引回 LanceDB。

    Args:
        filepath: 知识库中文档的文件名或路径（支持模糊匹配）
        project: 指定知识库名称或路径，空则使用当前知识库
        output_dir: 笔记保存目录（如 "笔记/论文摘录"），默认 "笔记"
    """
    # 1. 获取知识库连接
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return f"❌ 未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
        kb_name = project
    else:
        db = get_db()
        kb_name = _current_kb_name

    if TABLE_NAME not in db.table_names():
        return "❌ 知识库为空。"

    table = db.open_table(TABLE_NAME)

    # 2. 查找匹配的文档
    try:
        arrow_table = table.to_lance().to_table(columns=["source", "text", "chunk_index", "category"])
        col = arrow_table.column("source")
        texts_col = arrow_table.column("text")
        idx_col = arrow_table.column("chunk_index")
        cat_col = arrow_table.column("category")

        matched_sources = set()
        matched_chunks = []
        for i in range(len(col)):
            src = col[i].as_py() if hasattr(col[i], 'as_py') else str(col[i])
            if filepath.lower() in src.lower():
                matched_sources.add(src)
                matched_chunks.append({
                    "source": src,
                    "text": texts_col[i].as_py() if hasattr(texts_col[i], 'as_py') else str(texts_col[i]),
                    "chunk_index": int(idx_col[i]),
                    "category": cat_col[i].as_py() if hasattr(cat_col[i], 'as_py') else str(cat_col[i]),
                })
    except Exception as e:
        return f"❌ 读取知识库失败: {e}"

    if not matched_chunks:
        return f"未找到匹配「{filepath}」的文档。"

    # 3. 按 source 分组，取 chunk 数最多的那个
    from collections import Counter
    source_counts = Counter(c["source"] for c in matched_chunks)
    primary_source = source_counts.most_common(1)[0][0]
    primary_chunks = [c for c in matched_chunks if c["source"] == primary_source]
    primary_chunks.sort(key=lambda x: x["chunk_index"])

    fname = os.path.basename(primary_source)
    name_no_ext = os.path.splitext(fname)[0]
    category = primary_chunks[0].get("category", "") if primary_chunks else ""

    # 4. 拼接全文
    full_text = "\n\n".join(c["text"] for c in primary_chunks)
    # 截断太长的情况（防止笔记文件过大）
    if len(full_text) > 50000:
        full_text = full_text[:50000] + "\n\n...（原文过长，已截断前 50000 字）"

    # 5. 生成摘录笔记
    now = __import__('datetime').datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    time_str = now.strftime('%H:%M')

    # 自动标签
    auto_tags = set()
    if category:
        auto_tags.add(category)
    # 从文本中提取关键词作为标签
    keyword_tags = {
        "深度学习", "CNN", "LSTM", "Transformer", "GNN", "GAN", "物理信息",
        "洪涝", "洪水", "城市内涝", "降雨", "水文", "水动力",
        "机器学习", "联邦学习", "迁移学习", "强化学习",
        "不确定性", "数据驱动", "代理模型", "超分辨率",
    }
    text_lower = full_text.lower()
    for tag in keyword_tags:
        if tag.lower() in text_lower[:3000]:  # 只扫前 3000 字提取标签
            auto_tags.add(tag)

    # 5 个核心段落摘要
    paragraphs = [p.strip() for p in full_text.split("\n\n") if len(p.strip()) > 100]
    highlights = paragraphs[:3] if len(paragraphs) >= 3 else paragraphs

    # 构建笔记内容
    note_lines = [
        "---",
        f'title: "{name_no_ext}"',
        f'source: "{primary_source}"',
        f'knowledge_base: "{kb_name}"',
        f'created: "{date_str}"',
        f'time: "{time_str}"',
        f'tags: [{", ".join(sorted(auto_tags))}]',
        "---",
        "",
        f"# {name_no_ext}",
        "",
        "> 本文档从知识库提取，自动生成结构化摘录笔记",
        "",
        "## 📋 基本信息",
        "",
        f"- **来源文件**: `{primary_source}`",
        f"- **知识库**: `{kb_name}`",
        f"- **总块数**: {len(primary_chunks)}",
        f"- **类别**: {category or '未分类'}",
        f"- **提取时间**: {date_str} {time_str}",
        "",
        "## 📑 内容摘要",
        "",
    ]

    for i, h in enumerate(highlights, 1):
        # 截断过长的段落
        h_text = h[:800] + "..." if len(h) > 800 else h
        note_lines.append(f"> **片段 {i}**")
        note_lines.append(">")
        for line in h_text.split("\n")[:10]:
            note_lines.append(f"> {line}")
        note_lines.append("")

    note_lines.extend([
        "## 📝 我的笔记",
        "",
        "（在此记录你的理解和想法）",
        "",
        "### 核心观点",
        "",
        "-",
        "",
        "### 方法与创新",
        "",
        "-",
        "",
        "### 与我工作的关系",
        "",
        "-",
        "",
        "## ✅ 待办",
        "",
        "- [ ] 阅读全文验证关键结论",
        "- [ ] ",
        "",
        "---",
        "",
        "## 📎 原文片段引用",
        "",
    ])

    for i, c in enumerate(primary_chunks[:5], 1):
        snippet = c["text"][:300].replace("\n", " ")
        note_lines.append(f"> **块 #{c['chunk_index']}**")
        note_lines.append(f"> {snippet}...")
        note_lines.append("")

    note_lines.append("---")
    note_lines.append(f"> 笔记由 AI 辅助生成，基于 `{primary_source}`（{date_str}）")

    content = "\n".join(note_lines)

    # 6. 保存到文件
    if not output_dir:
        output_dir = "笔记/论文摘录"
    abs_dir = output_dir if os.path.isabs(output_dir) else os.path.join(os.getcwd(), output_dir)
    os.makedirs(abs_dir, exist_ok=True)

    safe_name = name_no_ext.replace(" ", "_").replace(":", "-")
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', safe_name)[:120]
    note_path = os.path.join(abs_dir, f"{date_str}_{safe_name}.md")

    # 如果已存在，加后缀
    if os.path.exists(note_path):
        i = 1
        while os.path.exists(f"{note_path.rsplit('.', 1)[0]}_{i}.md"):
            i += 1
        note_path = f"{note_path.rsplit('.', 1)[0]}_{i}.md"

    with open(note_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 7. 新笔记也索引回 LanceDB
    try:
        note_chunks = chunk_text(content, note_path)
        if note_chunks:
            batch_size = 32
            for i in range(0, len(note_chunks), batch_size):
                batch = note_chunks[i:i + batch_size]
                texts = [b["text"][:512] for b in batch]
                try:
                    vectors = get_embeddings_batch(texts)
                except Exception:
                    continue
                data = []
                for j, b in enumerate(batch):
                    data.append({
                        "vector": vectors[j],
                        "text": b["text"],
                        "source": b["source"],
                        "chunk_index": b["chunk_index"],
                        "category": b.get("category", "note"),
                    })
                table.add(data)

            _ensure_index(table)
            _ensure_fts_index(table, force_rebuild=False)
            index_note = f"\n✅ 笔记已自动索引回知识库（{len(note_chunks)} 块）"
        else:
            index_note = ""
    except Exception as e:
        index_note = f"\n⚠️ 笔记文件已保存但自动索引失败: {e}"

    return (
        f"✅ **摘录笔记已生成**\n"
        f"{'─' * 40}\n"
        f"笔记文件: `{note_path}`\n"
        f"来源: `{primary_source}`\n"
        f"总块数: {len(primary_chunks)}\n"
        f"标签: {', '.join(sorted(auto_tags)) if auto_tags else '无'}\n"
        f"💡 在 Obsidian 中打开此文件，填写「我的笔记」和「待办」部分。"
        f"{index_note}"
    )


def _get_content_graph_store() -> ContentGraphStore:
    global _CONTENT_GRAPH_STORE
    if _CONTENT_GRAPH_STORE is None:
        _CONTENT_GRAPH_STORE = ContentGraphStore(CONTENT_GRAPH_PATH)
        _load_registry()
        _CONTENT_GRAPH_STORE.remap_knowledge_bases(_kb_aliases)
    return _CONTENT_GRAPH_STORE


def _content_graph_document(filepath: str, project: str = ""):
    """Resolve exactly one LanceDB source and read only its graph input columns."""
    if project:
        db_path = _resolve_db_path(project)
        if not db_path:
            return None, f"未找到知识库「{project}」。"
        db = lancedb.connect(db_path)
        fallback_name = project
    else:
        db = get_db()
        db_path = DB_PATH
        fallback_name = _current_kb_name
    knowledge_base = fallback_name
    if db_path:
        target_path = os.path.normcase(os.path.abspath(db_path))
        for name, info in _load_registry().items():
            if os.path.normcase(os.path.abspath(info.get("path", ""))) == target_path:
                knowledge_base = name
                break
    if TABLE_NAME not in db.table_names():
        return None, "知识库为空。"
    table = db.open_table(TABLE_NAME)
    sources = _get_existing_sources_fast(table)
    exact = [source for source in sources if source == filepath]
    if not exact:
        exact = [source for source in sources if source.casefold() == filepath.casefold()]
    if not exact:
        exact = [source for source in sources if os.path.basename(source).casefold() == os.path.basename(filepath).casefold()]
    if not exact:
        exact = [source for source in sources if filepath.casefold() in source.casefold()]
    exact = sorted(set(exact))
    if not exact:
        return None, f"未找到匹配「{filepath}」的文档。"
    if len(exact) > 1:
        preview = "\n".join(f"- {source}" for source in exact[:10])
        return None, f"来源匹配不唯一，请传入完整来源路径：\n{preview}"
    source = exact[0]
    safe_source = source.replace("'", "''")
    arrow = table.to_lance().scanner(
        columns=["source", "text", "chunk_index", "category"],
        filter=f"source = '{safe_source}'",
    ).to_table()
    chunks = []
    for row_source, text, index, category in zip(
        arrow.column("source").to_pylist(), arrow.column("text").to_pylist(),
        arrow.column("chunk_index").to_pylist(), arrow.column("category").to_pylist(),
    ):
        if row_source == source:
            chunks.append({
                "source": source, "text": text or "", "chunk_index": int(index),
                "category": category or "",
            })
    chunks.sort(key=lambda item: item["chunk_index"])
    return {"knowledge_base": knowledge_base, "source": source, "chunks": chunks}, None


@mcp.tool(
    name="build_content_graph",
    description=(
        "为一个已入库文档构建或增量更新内容级知识图谱。抽取实体、方法、观点和证据关系，"
        "写入独立SQLite，不修改LanceDB、向量或源文件。默认离线抽取；可通过CONTENT_GRAPH_*环境变量启用任意OpenAI兼容模型。"
    ),
)
def build_content_graph(filepath: str, project: str = "", force: bool = False) -> str:
    resolved, error = _content_graph_document(filepath, project)
    if error:
        return f"❌ {error}"
    try:
        extractor = extractor_from_environment()
        result = _get_content_graph_store().index_document(
            resolved["knowledge_base"], resolved["source"], resolved["chunks"], extractor, force=force
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
    description="读取某个文档已构建的内容图谱，返回实体、观点、关系、置信度和证据Chunk。不会触发模型调用。",
)
def get_content_graph(filepath: str, project: str = "", max_nodes: int = 120) -> str:
    resolved, error = _content_graph_document(filepath, project)
    if error:
        return f"❌ {error}"
    graph = _get_content_graph_store().get_document_graph(
        resolved["knowledge_base"], resolved["source"], limit=max(10, min(max_nodes, 500))
    )
    if not graph["document_id"]:
        return "当前文档尚未构建内容图谱。请先调用 build_content_graph。"
    return json.dumps(graph, ensure_ascii=False, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="search_content_graph",
    description="按名称搜索内容图谱中的实体、方法、观点、主题或数据集，并返回其来源文件。",
)
def search_content_graph(query: str, kind: str = "", project: str = "", limit: int = 20) -> str:
    try:
        results = _get_content_graph_store().search_nodes(query, kind, limit=max(1, min(limit, 100)))
        if project:
            for item in results:
                item["locations"] = [
                    location for location in item["locations"] if location["knowledge_base"] == project
                ]
            results = [item for item in results if item["locations"]]
        return json.dumps({"query": query, "count": len(results), "results": results}, ensure_ascii=False, indent=2)
    except Exception as error:
        return f"❌ 内容图谱搜索失败: {error}"


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
    name="get_content_graph_stats",
    description="查看独立内容图谱数据库的文档、位置、Chunk、节点和关系统计。",
)
def get_content_graph_stats() -> str:
    return json.dumps(_get_content_graph_store().stats(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
