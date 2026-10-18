# -*- coding: utf-8 -*-
"""search_images 命令行兜底：MCP 服务进程未重启（旧代码）时，用本脚本以全新
子进程直接查图像向量库（kb.image_search，与 MCP 同一实现）。

用法:
    python search_images_cli.py "<query>" [limit] [source_filter]
输出:
    stdout 打印 JSON（results + trace）
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

from kb import image_search as kb_img  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print('用法: python search_images_cli.py "<query>[||query2||...]" [limit] [source_filter]',
              file=sys.stderr)
        return 2
    raw = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    source_filter = sys.argv[3] if len(sys.argv) > 3 else ""
    queries = [q.strip() for q in raw.split("||") if q.strip()]
    if not queries:
        queries = [raw]
    if len(queries) == 1:
        out = kb_img.search_images_structured(query=queries[0], limit=limit, source_filter=source_filter)
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        # 批量模式：同进程内跑多条查询，模型只加载一次
        merged = []
        for q in queries:
            out = kb_img.search_images_structured(query=q, limit=limit, source_filter=source_filter)
            merged.append({"query": q, "results": out["results"]})
        print(json.dumps({"batch": True, "queries": merged}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
