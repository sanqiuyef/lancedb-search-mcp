# -*- coding: utf-8 -*-
"""search_knowledge 命令行兜底：MCP 文本检索超时/不可用时，用本脚本以全新
子进程直接查文本知识库（kb.search，与 MCP 同一实现）。

用法:
    python search_knowledge_cli.py "<query>" [limit] [source_filter] [mode]
输出:
    stdout 打印 JSON（results 含 text/source/chunk_index + trace）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HF_HOME", r"D:\huggingface")
os.environ.setdefault("HF_HUB_CACHE", r"D:\huggingface\hub")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("LANCEDB_DB_PATH", r"D:\cherry-workplace\knowledge-hub\lancedb")

from kb import search as kb_search  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print('用法: python search_knowledge_cli.py "<query>" [limit] [source_filter] [mode]',
              file=sys.stderr)
        return 2
    query = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    source_filter = sys.argv[3] if len(sys.argv) > 3 else ""
    mode = sys.argv[4] if len(sys.argv) > 4 else "hybrid"
    out = kb_search.search_structured(
        query=query, limit=limit, use_reranker=True,
        source_filter=source_filter, search_mode=mode,
    )
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
