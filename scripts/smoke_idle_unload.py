# -*- coding: utf-8 -*-
"""闲置自动卸载冒烟：加载 → 推理 → 闲置超时释放显存 → 再调用自动重载。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import torch

from kb import config as cfg  # noqa: E402
from kb import search as ks  # noqa: E402
from kb import embeddings as kb_embeddings  # noqa: E402

assert cfg.LOCAL_MODEL_IDLE_UNLOAD > 0, f"需设置 LOCAL_MODEL_IDLE_UNLOAD>0，当前={cfg.LOCAL_MODEL_IDLE_UNLOAD}"


def mem(tag: str) -> None:
    alloc = torch.cuda.memory_allocated() / 1024**3
    reserved = torch.cuda.memory_reserved() / 1024**3
    print(f"{tag}: 本进程显存 allocated={alloc:.2f}GB reserved={reserved:.2f}GB")


def embed_loaded() -> bool:
    return kb_embeddings._st_holder["model"] is not None


def rerank_loaded() -> bool:
    return ks._local_cross_encoder is not None


print(f"闲置卸载阈值: {cfg.LOCAL_MODEL_IDLE_UNLOAD}s")
t0 = time.time()
vec = kb_embeddings.embed_query("BIM 平台如何解析 IFC 文件")
print(f"[1] 首次嵌入（冷加载）{time.time()-t0:.1f}s，维度={len(vec)}，嵌入模型在载={embed_loaded()}")
mem("    ")

t1 = time.time()
score = float(ks._get_cross_encoder().predict([("q", "d")], show_progress_bar=False)[0])
print(f"[2] 首次重排（冷加载）{time.time()-t1:.1f}s，得分={score:.4f}，重排模型在载={rerank_loaded()}")
mem("    ")

print(f"[3] 闲置等待（阈值 {cfg.LOCAL_MODEL_IDLE_UNLOAD}s + 巡检 {ks.lifecycle.CHECK_INTERVAL}s）...")
deadline = time.time() + cfg.LOCAL_MODEL_IDLE_UNLOAD + ks.lifecycle.CHECK_INTERVAL + 10
while time.time() < deadline:
    # 持有器清空后 watchdog 还要跑 gc + empty_cache，等显存真正落下来
    if (not embed_loaded() and not rerank_loaded()
            and torch.cuda.memory_allocated() / 1024**3 < 0.5):
        break
    time.sleep(2)
print(f"    闲置后：嵌入模型在载={embed_loaded()}，重排模型在载={rerank_loaded()}")
mem("    ")
assert not embed_loaded() and not rerank_loaded(), "闲置超时后模型应被卸载"

t2 = time.time()
vec = kb_embeddings.embed_query("再次调用应自动重载")
print(f"[4] 再次嵌入（自动重载）{time.time()-t2:.1f}s，维度={len(vec)}，嵌入模型在载={embed_loaded()}")
mem("    ")

print("\n✅ 用时挂载 / 闲置卸载 / 再用重载 全部符合预期")
