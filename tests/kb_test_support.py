# -*- coding: utf-8 -*-
"""测试公共设施：确定性假 embedding、临时库初始化。"""

import os
import tempfile

import numpy as np

import kb_config as cfg
import kb_embeddings
import kb_schema
from lancedb.embeddings import TextEmbeddingFunction, register


@register("test-fake-embedding")
class FakeEmbeddings(TextEmbeddingFunction):
    """确定性 embedding：按词符哈希映射到固定 32 维，语义无关但可复现。"""

    dimensions: int = 32

    def ndims(self) -> int:
        return self.dimensions

    def generate_embeddings(self, texts):
        out = []
        for text in texts:
            vec = np.zeros(self.dimensions, dtype=np.float32)
            for token in str(text).split() or ["<empty>"]:
                idx = int(abs(hash(token)) % self.dimensions)
                vec[idx] += 1.0
            norm = np.linalg.norm(vec)
            out.append(vec / norm if norm else vec)
        return out


def activate_fake_embedding() -> None:
    """把测试 embedding 设为激活后端，并清空 schema 缓存。"""
    cfg.EMBEDDING_BACKEND = "test"
    kb_embeddings.reset_embedding_function()
    kb_embeddings._embedding_func = FakeEmbeddings.create()
    kb_schema._chunk_model = None


def temp_db_env(monkey_target=None) -> str:
    """创建临时库路径并写入环境变量，返回路径。"""
    db_path = tempfile.mkdtemp(prefix="kb_test_")
    os.environ["LANCEDB_DB_PATH"] = db_path
    kb_schema.reset_db()
    return db_path


def write_registry(tmpdir: str, projects: dict, extra: dict | None = None) -> str:
    """写一份临时 kb-config.json 并让 kb_config 立即热重载到它。"""
    import json

    path = os.path.join(tmpdir, "kb-config.json")
    payload = {"projects": projects}
    if extra:
        payload.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    cfg.KB_CONFIG_PATH = path
    cfg._kb_registry = None
    cfg._kb_config_stamp = None
    cfg.load_registry(force=True)
    return path
