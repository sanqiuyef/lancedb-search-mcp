# -*- coding: utf-8 -*-
"""LanceDB 表结构（官方 LanceModel）与索引维护。

Schema 采用官方 SourceField/VectorField 自动向量化模式；新增 doc_id（内容指纹）
与 ingested_at（入库时间）两列用于去重与诊断。FTS 为原生 BM25 倒排索引，
jieba 分词词典位于 LANCE_LANGUAGE_MODEL_HOME。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import lancedb
from lancedb.pydantic import LanceModel, Vector

from . import config as cfg
from . import embeddings as kb_embeddings


_db = None
_chunk_model = None


def get_db():
    """单库连接单例（路径解析：LANCEDB_DB_PATH > kb-config.json > 默认）。"""
    global _db
    if _db is None:
        db_path = cfg.resolve_db_path()
        os.makedirs(db_path, exist_ok=True)
        _db = lancedb.connect(db_path)
    return _db


def reset_db() -> None:
    """测试/切库用：清空连接与 schema 缓存。"""
    global _db, _chunk_model
    _db = None
    _chunk_model = None


def get_chunk_model():
    """按当前激活 embedding 函数构建 Chunk LanceModel（进程内缓存）。"""
    global _chunk_model
    if _chunk_model is not None:
        return _chunk_model
    func = kb_embeddings.get_embedding_function()
    dims = func.ndims()

    class Chunk(LanceModel):
        text: str = func.SourceField()
        vector: Vector(dims) = func.VectorField()
        source: str
        chunk_index: int
        category: str = ""
        doc_id: str = ""
        ingested_at: str = ""

    _chunk_model = Chunk
    return Chunk


def chunk_doc_id(text: str, source: str, chunk_index: int) -> str:
    """内容指纹：同库同源重复入库可据此识别。"""
    import hashlib

    return hashlib.sha256(
        f"{source}\x00{chunk_index}\x00{text}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def get_or_create_table():
    """打开或创建主表；空表时用 LanceModel schema 建（自带元数据列）。"""
    db = get_db()
    if cfg.TABLE_NAME in db.table_names():
        return db.open_table(cfg.TABLE_NAME)
    model = get_chunk_model()
    return db.create_table(cfg.TABLE_NAME, schema=model, mode="create")


def table_exists() -> bool:
    return cfg.TABLE_NAME in get_db().table_names()


def row_count() -> int:
    """重开表取行数（删除/更新后旧表对象的 len 可能有缓存）。"""
    if not table_exists():
        return 0
    return len(get_db().open_table(cfg.TABLE_NAME))


def existing_sources(table) -> set[str]:
    """只读 source 列取全部来源，用于增量判断。"""
    arrow = table.to_lance().to_table(columns=["source"])
    return {
        v if isinstance(v, str) else str(v)
        for v in arrow.column("source").to_pylist()
    }


# =============================================================
# 索引维护
# =============================================================

VECTOR_INDEX_MIN_ROWS = 1000


def ensure_vector_index(table) -> str:
    """行数达标时创建 IVF_PQ 余弦向量索引；返回动作说明。"""
    rows = len(table)
    if rows < VECTOR_INDEX_MIN_ROWS:
        return f"向量索引未创建（{rows} 行 < {VECTOR_INDEX_MIN_ROWS}）"
    names = {i.name for i in table.list_indices()}
    if "vector_idx" in names:
        return "向量索引已存在"
    import math

    partitions = min(max(int(math.sqrt(rows)), 16), 256)
    table.create_index(
        index_type="IVF_PQ",
        metric="cosine",
        num_partitions=partitions,
        num_sub_vectors=16,
        replace=True,
    )
    return f"IVF_PQ 向量索引已创建（partitions={partitions}）"


def ensure_fts_index(table, force_rebuild: bool = False) -> str:
    """确保原生 BM25 FTS 索引存在（jieba 分词）；返回动作说明。"""
    names = {i.name for i in table.list_indices()}
    if "text_idx" in names:
        if not force_rebuild:
            return "FTS 索引已存在"
        table.drop_index("text_idx")
    table.create_fts_index("text", base_tokenizer=cfg.FTS_BASE_TOKENIZER, replace=True)
    return f"FTS 索引已创建（tokenizer={cfg.FTS_BASE_TOKENIZER}）"


def fold_new_rows(table) -> None:
    """把增量写入的行折叠进既有索引（FTS/向量索引均依赖此步骤）。"""
    try:
        table.optimize()
    except Exception as e:  # 空增量或索引未建时 optimize 可能无操作/报错
        print(f"[schema] optimize 跳过: {e}", file=sys.stderr)


# =============================================================
# 库目录说明（自动维护的 README.md）
# =============================================================

def update_db_readme() -> None:
    """重写库目录下 README.md（目录说明书），供人工浏览。"""
    db_path = cfg.resolve_db_path()
    readme = os.path.join(db_path, "README.md")
    try:
        if not table_exists():
            return
        table = get_or_create_table()
        total = len(table)
        arrow = table.to_lance().to_table(columns=["source", "category"])
        sources = arrow.column("source").to_pylist()
        categories = arrow.column("category").to_pylist()

        cat_dist: dict[str, int] = {}
        doc_count: dict[str, int] = {}
        for src, cat in zip(sources, categories):
            if cat:
                cat_dist[cat] = cat_dist.get(cat, 0) + 1
            doc_count[src] = doc_count.get(src, 0) + 1

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "# LanceDB 知识库（knowledge_v2）",
            "",
            f"> 本文件由 lancedb-search MCP 自动维护，更新时间：{now}",
            "",
            f"- 总 chunk 数：{total}",
            f"- 文档数：{len(doc_count)}",
            f"- Embedding：{'/'.join(str(x) for x in kb_embeddings.embedding_identity())}",
        ]
        if cat_dist:
            lines += ["", "## 类别分布", ""]
            for cat, cnt in sorted(cat_dist.items(), key=lambda x: -x[1]):
                lines.append(f"- {cat}：{cnt} chunks")
        lines += ["", "## 文档清单", ""]
        for src, cnt in sorted(doc_count.items()):
            lines.append(f"- {src}（{cnt} chunks）")
        with open(readme, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[schema] README 更新失败: {e}", file=sys.stderr)
