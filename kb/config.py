# -*- coding: utf-8 -*-
r"""集中配置：环境变量与单库路径。

单库不分区的扁平结构：所有文档共居一个 LanceDB 库（2026-10-02 用户决定
移除分区机制，kb-config.json 注册表废止）。默认库为全局知识库内的
D:\cherry-workplace\knowledge-hub\lancedb（根目录将作为 Obsidian vault 协作搭建）。
"""

from __future__ import annotations

import os
import sys

# 项目根目录（kb 包的上级）；kb-config.json 等本机配置都在根目录
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── API 服务（SiliconFlow：embedding / rerank / chat） ──
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
EMBEDDING_URL = "https://api.siliconflow.cn/v1/embeddings"
RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
CHAT_URL = os.environ.get("CHAT_URL", "https://api.siliconflow.cn/v1/chat/completions")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "Qwen/Qwen3-8B")
EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
EMBED_DIM = 1024  # Qwen3-Embedding-8B 支持 32~4096，1024 是质量/成本最佳平衡点

# ── 后端选择：api（默认）/ local ──
EMBEDDING_BACKEND = os.environ.get("EMBEDDING_BACKEND", "api").lower()
RERANKER_BACKEND = os.environ.get("RERANKER_BACKEND", "api").lower()

# ── 本地模型配置（官方 sentence-transformers 注册表条目） ──
LOCAL_EMBED_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-m3")
LOCAL_EMBED_DIM = int(os.environ.get("LOCAL_EMBED_DIM", "1024"))
LOCAL_RERANK_MODEL = os.environ.get("LOCAL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
LOCAL_MODEL_DEVICE = os.environ.get("LOCAL_MODEL_DEVICE", "auto").lower()
# 闲置 N 秒后自动卸载本地模型释放显存（对齐 Ollama keep-alive；0 = 常驻不卸载）
LOCAL_MODEL_IDLE_UNLOAD = int(os.environ.get("LOCAL_MODEL_IDLE_UNLOAD", "300"))

# ── 单库与表 ──
DEFAULT_DB_PATH = r"D:\cherry-workplace\knowledge-hub\lancedb"  # 全局知识库内的向量库子目录
TABLE_NAME = "my_docs"

# ── 文档扫描 ──
EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".py", ".js", ".ts", ".json",
              ".yaml", ".yml", ".toml", ".html", ".htm"}
EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "truncated-results"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB

# ── 分块 ──
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# ── 扫描 PDF OCR 回退（默认关闭） ──
OCR_ENABLED = os.environ.get("LANCEDB_OCR", "0") == "1"
OCR_TESSERACT_CMD = os.environ.get(
    "TESSERACT_CMD", r"D:\cherry-workplace\tools\tesseract\tesseract.exe"
)
OCR_LANG = os.environ.get("LANCEDB_OCR_LANG", "chi_sim+eng")
OCR_DPI = int(os.environ.get("LANCEDB_OCR_DPI", "300"))
OCR_MAX_PAGES = int(os.environ.get("LANCEDB_OCR_MAX_PAGES", "50"))  # 0 = 不限制
OCR_MIN_TEXT_CHARS = int(os.environ.get("LANCEDB_OCR_MIN_TEXT_CHARS", "500"))

# ── 原生 FTS：jieba 分词依赖 LANCE_LANGUAGE_MODEL_HOME 下的词典 ──
FTS_BASE_TOKENIZER = os.environ.get("FTS_BASE_TOKENIZER", "jieba/default")
LANCE_LANGUAGE_MODEL_HOME = os.environ.get(
    "LANCE_LANGUAGE_MODEL_HOME", r"D:\lance\language_models"
)
os.environ.setdefault("LANCE_LANGUAGE_MODEL_HOME", LANCE_LANGUAGE_MODEL_HOME)

# ── MinerU 精准解析 API（优先于 Tesseract） ──
MINERU_API_KEY = os.environ.get("MINERU_API_KEY", "")
MINERU_API_MODEL = os.environ.get("MINERU_API_MODEL", "vlm")
MINERU_POLL_TIMEOUT = int(os.environ.get("MINERU_POLL_TIMEOUT", "300"))

try:  # MinerU SDK 为可选依赖
    _mineru_sdk_path = os.environ.get("MINERU_SDK_PATH", "")
    if _mineru_sdk_path:
        sys.path.insert(0, _mineru_sdk_path)
    from mineru import MinerU as _MinerU  # noqa: E402
    _MINERU_SDK_AVAILABLE = True
except ImportError:
    _MinerU = None
    _MINERU_SDK_AVAILABLE = False

# ── 目录路径 → 类别 ──
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


def resolve_db_path() -> str:
    """库路径解析优先级：LANCEDB_DB_PATH 环境变量 > 默认 knowledge-hub/lancedb。"""
    env = os.environ.get("LANCEDB_DB_PATH", "")
    if env:
        return env
    return DEFAULT_DB_PATH
