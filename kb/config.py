# -*- coding: utf-8 -*-
r"""集中配置：环境变量与单库路径。

单库不分区的扁平结构：所有文档共居一个 LanceDB 库（2026-10-02 用户决定
移除分区机制，kb-config.json 注册表废止）。默认库为全局知识库内的
D:\cherry-workplace\knowledge-hub\lancedb（根目录将作为 Obsidian vault 协作搭建）。
"""

from __future__ import annotations

import os
import sys

# 项目根目录（kb 包的上级）；本机配置都在根目录
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 本地模型（2026-10-02 起全本地化：仅 D:\huggingface 的 bge-m3 + bge-reranker，零云端依赖） ──
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

# ── PDF → Markdown 本地 MinerU（2026-10-04 集成；替代 pdfminer 主通路以保住公式） ──
# 独立 venv：.mineru-venv（mineru 3.4.4 + transformers 4.57；Anaconda 的 transformers v5 不兼容）
MINERU_ENABLED = os.environ.get("LANCEDB_MINERU", "1") == "1"
MINERU_BIN = os.environ.get(
    "LANCEDB_MINERU_BIN",
    os.path.join(SERVER_DIR, ".mineru-venv", "Scripts", "mineru.exe"),
)
# 转换产物缓存目录；空 = <库目录>/_pdf_md_cache（按 PDF 路径+大小+mtime 失效）
MINERU_CACHE_DIR = os.environ.get("LANCEDB_MINERU_CACHE", "")
# 短路径暂存（规避 Windows 260 字符路径上限），需与库同级盘符且路径尽量短
MINERU_STAGING_DIR = os.environ.get("LANCEDB_MINERU_STAGING", r"D:\mstaging")
MINERU_TIMEOUT = int(os.environ.get("LANCEDB_MINERU_TIMEOUT", "1800"))

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
