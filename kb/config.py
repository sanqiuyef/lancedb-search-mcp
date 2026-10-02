# -*- coding: utf-8 -*-
"""集中配置：环境变量、单库路径、kb-config.json 分区注册表。

单库模式：所有项目分区共存于同一个 LanceDB 库（project 列过滤），连接一次、
永不物理切换。整改后默认库为 D:\cherry-workplace\knowledge_v2（全新 schema）。
旧库已于 2026-10-02 按用户要求删除（纯文本备份见 D:\cherry-workplace\旧库文本备份-20261002.zip）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
DEFAULT_DB_PATH = r"D:\cherry-workplace\knowledge_v2"  # 整改后的活动库（旧库已删除）
TABLE_NAME = "my_docs"
PROJECT_FIELD = "project"

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


# =============================================================
# kb-config.json 分区注册表（mtime 热重载，改配置无需重启进程）
# =============================================================

KB_CONFIG_PATH = os.path.join(SERVER_DIR, "kb-config.json")
_kb_registry: dict | None = None
_kb_config_stamp: tuple[float, int] | None = None


def load_registry(force: bool = False) -> dict:
    """加载 kb-config.json（分区名 → 项目信息），配置文件变化时自动重载。"""
    global _kb_registry, _kb_config_stamp
    stamp = None
    try:
        st = os.stat(KB_CONFIG_PATH)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None
    if not force and _kb_registry is not None and stamp == _kb_config_stamp:
        return _kb_registry
    registry: dict = {"db_path": DEFAULT_DB_PATH, "default_project": "", "projects": {}}
    if stamp is not None:
        try:
            with open(KB_CONFIG_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
            projects = payload.get("projects", {})
            if isinstance(projects, dict):  # 兼容旧多库结构（knowledge_bases 列表）
                registry.update({k: v for k, v in payload.items() if k != "projects"})
                registry["projects"] = projects
        except (OSError, json.JSONDecodeError) as e:
            print(f"[kb-config] 读取失败，使用默认注册表: {e}", file=sys.stderr)
    _kb_registry = registry
    _kb_config_stamp = stamp
    return registry


def _strip_alias(project: str) -> str:
    """兼容历史别名（project-xxx / kb-xxx 前缀）。"""
    name = (project or "").strip()
    for prefix in ("project-", "kb-", "kb_"):
        if name.lower().startswith(prefix):
            candidate = name[len(prefix):]
            if candidate:
                name = candidate
            break
    return name


def normalize_project(project: str = "") -> str:
    """规范化分区名：去别名前缀并校验注册表；未注册返回空串（表示跨全部分区）。"""
    name = _strip_alias(project)
    if not name:
        return ""
    projects = load_registry().get("projects", {})
    if name in projects:
        return name
    # 大小写不敏感的兜底匹配
    folded = {str(k).casefold(): k for k in projects}
    return folded.get(name.casefold(), "")


def list_projects() -> list[str]:
    """注册表中的全部分区名。"""
    return list(load_registry().get("projects", {}).keys())


def source_roots(project: str) -> list[str]:
    """指定分区的源文件根目录列表（可能为空）。"""
    info = load_registry().get("projects", {}).get(project, {})
    roots = info.get("source_roots", []) if isinstance(info, dict) else []
    return [r for r in roots if r]


def resolve_db_path() -> str:
    """库路径解析优先级：LANCEDB_DB_PATH 环境变量 > kb-config.json > 默认 knowledge_v2。"""
    env = os.environ.get("LANCEDB_DB_PATH", "")
    if env:
        return env
    return load_registry().get("db_path") or DEFAULT_DB_PATH


def guess_project_from_cwd() -> str:
    """按当前工作目录推断分区：命中某个分区 source_roots 前缀则返回它。"""
    cwd = os.path.normcase(os.path.normpath(os.getcwd()))
    for name in list_projects():
        for root in source_roots(name):
            norm = os.path.normcase(os.path.normpath(root))
            if cwd == norm or cwd.startswith(norm + os.sep):
                return name
    return ""


def resolve_project(project: str = "", fallback_all: bool = True) -> str:
    """工具入参 → 分区名：显式指定优先，其次注册表 default_project，再按 CWD 推断。"""
    name = normalize_project(project)
    if name:
        return name
    default = normalize_project(load_registry().get("default_project", ""))
    if default:
        return default
    return guess_project_from_cwd() if fallback_all else ""
