# -*- coding: utf-8 -*-
"""测试公共设施：确定性假 embedding、临时库初始化。"""

import os
import tempfile

import numpy as np

from kb import config as cfg
from kb import embeddings as kb_embeddings
from kb import schema as kb_schema
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
    kb_embeddings.reset_embedding_function()
    kb_embeddings._embedding_func = FakeEmbeddings.create()
    kb_schema._chunk_model = None


_TEMP_ENV_KEYS = (
    "LANCEDB_DB_PATH",
    "LANCEDB_VAULT_ROOT",
    "WIKI_ENABLED",
    "WIKI_DB_PATH",
    "WIKI_VAULT_PATH",
)


def temp_db_env(testcase=None) -> str:
    """创建临时库与临时 vault，并写入隔离环境变量，返回临时库路径。

    vault 固定为 ``<临时库>/vault``，确保入库测试绝不会读写真实 Obsidian
    vault；``WIKI_ENABLED=0`` 且 WIKI_* 路径也指向临时目录，Wiki 钩子即
    使被误调用也不会碰真实 DB。传入 unittest.TestCase 时用 addCleanup
    保存/恢复全部相关环境变量，避免测试之间互相污染。
    """
    db_path = tempfile.mkdtemp(prefix="kb_test_")
    vault_path = os.path.join(db_path, "vault")
    os.makedirs(vault_path, exist_ok=True)
    saved = {key: os.environ.get(key) for key in _TEMP_ENV_KEYS}
    os.environ["LANCEDB_DB_PATH"] = db_path
    os.environ["LANCEDB_VAULT_ROOT"] = vault_path
    os.environ["WIKI_ENABLED"] = "0"
    os.environ["WIKI_DB_PATH"] = os.path.join(db_path, "wiki.sqlite3")
    os.environ["WIKI_VAULT_PATH"] = vault_path
    kb_schema.reset_db()

    if testcase is not None and hasattr(testcase, "addCleanup"):
        def _restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            kb_schema.reset_db()

        testcase.addCleanup(_restore)
    return db_path
