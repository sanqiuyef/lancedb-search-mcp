# -*- coding: utf-8 -*-
"""入库后真实检索验证：中文查询 → top5 图片 → 打印路径供读图核对。

    cd D:/cherry-workplace/mcp_servers/lancedb-search
    HF_HUB_OFFLINE=1 HF_HOME=D:/huggingface python -X utf8 scripts/test_image_search.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb import image_search as kb_img  # noqa: E402

QUERIES = [
    "深度学习模型架构示意图",
    "城市内涝淹没范围地图",
    "神经网络结构图",
    "洪峰流量过程线",
    "研究区高程与坡度图",
]


def main() -> int:
    total = kb_img.image_count()
    print(f"[table] image_chunks 共 {total} 张")
    if total == 0:
        print("表为空，先跑 ingest_images", file=sys.stderr)
        return 2
    for q in QUERIES:
        r = kb_img.search_images_structured(query=q, limit=5)
        print(f"\n「{q}」")
        for i, d in enumerate(r["results"], 1):
            print(f"  {i}. [{d['similarity']:.4f}] {d['source_doc']}/{d['file_name']}")
    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
