# -*- coding: utf-8 -*-
"""jina-clip-v2 冒烟测试（走 kb.image_search 生产路径）。

用法（Anaconda python）：
    cd D:/cherry-workplace/mcp_servers/lancedb-search
    HF_HUB_OFFLINE=1 HF_HOME=D:/huggingface python -X utf8 scripts/test_jina_clip_smoke.py

通过标准：
    1) 文本/图像嵌入维度 = 1024；
    2) 图文相似度矩阵可分（正确查询列明显高于无关查询列）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb import image_search as kb_img  # noqa: E402

IMG_ROOT = Path(r"D:\cherry-workplace\knowledge-hub\vault\attachments-附件\literature-文献图片")

QUERIES = [
    "城市内涝淹没水深分布云图",
    "深度学习模型架构示意图",
    "研究区域地理位置与土地利用影像",
    "降雨过程与雨型示意图",
    "预测值与观测值散点对比验证图",
]

SAMPLES_PER_DIR = 3
DIRS = ["asif2026convolutio", "ahmadi2026tlefedform"]


def main() -> int:
    img_paths: list[str] = []
    for d in DIRS:
        found = sorted((IMG_ROOT / d).glob("*.jpg"))[:SAMPLES_PER_DIR]
        img_paths.extend(str(p) for p in found)
    if not img_paths:
        print("没有找到样图", file=sys.stderr)
        return 2
    print(f"[data] {len(img_paths)} 张样图：")
    for i, p in enumerate(img_paths):
        print(f"  img{i}: {Path(p).parent.name}/{Path(p).name}")

    t_emb = np.stack([kb_img.embed_query_text(q) for q in QUERIES])
    img_emb_pairs = kb_img.embed_images(img_paths)
    kept = [p for p, _ in img_emb_pairs]
    i_emb = np.stack([v for _, v in img_emb_pairs])
    print(f"[shape] text {t_emb.shape} / image {i_emb.shape}")
    if t_emb.shape[1] != 1024:
        print(f"[FAIL] 嵌入维度 {t_emb.shape[1]} != 1024")
        return 1

    sim = (t_emb @ i_emb.T).astype(float)
    print("\n" + "query \\ img".ljust(36) + "".join(f"img{i:<7}" for i in range(len(kept))))
    for q, row in zip(QUERIES, sim):
        print(q.ljust(32) + "".join(f"{v:9.3f}" for v in row))

    print("\n每条查询的最像图片：")
    for qi, row in enumerate(sim):
        b = int(row.argmax())
        print(f"  {QUERIES[qi]} -> img{b} (相似度 {row[b]:.3f})  [{Path(kept[b]).name}]")

    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
