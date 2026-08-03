"""Durable chunk assets and replaceable embedding generations for LanceDB KBs.

The active ``my_docs`` table remains compatible with the existing browser and
search pipeline.  This module stores the recoverable chunk text separately and
keeps inactive embedding generations as ordinary LanceDB tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pyarrow as pa


ACTIVE_TABLE = "my_docs"
ASSET_TABLE = "my_docs_assets"
GENERATION_TABLE = "my_docs_generations"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunk_id(text: str, source: str, chunk_index: int) -> str:
    value = f"{source}\0{chunk_index}\0{text}".encode("utf-8", errors="replace")
    return hashlib.sha256(value).hexdigest()


def _generation_id(model: str, dimension: int) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-")[:36] or "embedding"
    return f"{stem}-{dimension}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def _table_names(db) -> set[str]:
    listed = db.list_tables() if hasattr(db, "list_tables") else db.table_names()
    return set(listed.tables if hasattr(listed, "tables") else listed)


def _asset_schema() -> pa.Schema:
    return pa.schema([
        pa.field("chunk_id", pa.string()), pa.field("text", pa.string()),
        pa.field("source", pa.string()), pa.field("chunk_index", pa.int32()),
        pa.field("category", pa.string()), pa.field("content_hash", pa.string()),
        pa.field("stored_at", pa.string()),
    ])


def _generation_schema() -> pa.Schema:
    return pa.schema([
        pa.field("generation_id", pa.string()), pa.field("table_name", pa.string()),
        pa.field("backend", pa.string()), pa.field("model", pa.string()),
        pa.field("dimension", pa.int32()), pa.field("chunk_count", pa.int64()),
        pa.field("created_at", pa.string()), pa.field("is_active", pa.bool_()),
    ])


def ensure_asset_tables(db) -> None:
    names = _table_names(db)
    if ASSET_TABLE not in names:
        db.create_table(ASSET_TABLE, schema=_asset_schema())
    if GENERATION_TABLE not in names:
        db.create_table(GENERATION_TABLE, schema=_generation_schema())


def _rows_from_arrow(arrow) -> list[dict]:
    values = arrow.to_pylist()
    return [{**row, "chunk_index": int(row["chunk_index"]), "category": row.get("category") or ""}
            for row in values]


def asset_rows(db) -> list[dict]:
    ensure_asset_tables(db)
    return _rows_from_arrow(db.open_table(ASSET_TABLE).to_lance().to_table())


def add_assets(db, chunks: Iterable[dict]) -> int:
    """Persist unique chunks before their vectors are written."""
    ensure_asset_tables(db)
    table = db.open_table(ASSET_TABLE)
    existing = set(table.to_lance().to_table(columns=["chunk_id"]).column("chunk_id").to_pylist()) if len(table) else set()
    now = _now()
    records = []
    for chunk in chunks:
        text = chunk.get("text") or ""
        source = chunk.get("source") or ""
        index = int(chunk.get("chunk_index", 0))
        identifier = chunk_id(text, source, index)
        if identifier in existing:
            continue
        records.append({
            "chunk_id": identifier, "text": text, "source": source, "chunk_index": index,
            "category": chunk.get("category") or "",
            "content_hash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
            "stored_at": now,
        })
        existing.add(identifier)
    if records:
        table.add(records)
    return len(records)


def migrate_active_chunks(db) -> int:
    """One-time, non-destructive migration of legacy active rows into assets."""
    if ACTIVE_TABLE not in _table_names(db):
        ensure_asset_tables(db)
        return 0
    active = db.open_table(ACTIVE_TABLE)
    if not len(active):
        ensure_asset_tables(db)
        return 0
    arrow = active.to_lance().to_table(columns=["text", "source", "chunk_index", "category"])
    return add_assets(db, _rows_from_arrow(arrow))


def list_generations(db) -> list[dict]:
    ensure_asset_tables(db)
    rows = db.open_table(GENERATION_TABLE).to_lance().to_table().to_pylist()
    return sorted(rows, key=lambda item: item["created_at"], reverse=True)


def _replace_generation(db, generation: dict) -> None:
    table = db.open_table(GENERATION_TABLE)
    safe = generation["generation_id"].replace("'", "''")
    table.delete(f"generation_id = '{safe}'")
    table.add([generation])


def register_active_generation(db, *, backend: str, model: str, dimension: int) -> None:
    """Register a legacy/current active table exactly once when possible."""
    if ACTIVE_TABLE not in _table_names(db):
        return
    existing = list_generations(db)
    if any(item["is_active"] for item in existing):
        return
    active_table = db.open_table(ACTIVE_TABLE)
    if len(active_table):
        vector_type = active_table.to_lance().to_table(columns=["vector"]).column("vector").type
        dimension = int(getattr(vector_type, "list_size", dimension) or dimension)
    _replace_generation(db, {
        "generation_id": "legacy-active", "table_name": ACTIVE_TABLE, "backend": backend,
        "model": model, "dimension": int(dimension), "chunk_count": int(len(active_table)),
        "created_at": _now(), "is_active": True,
    })


def build_generation(
    db,
    *,
    embedder: Callable[[list[str]], list[list[float]]],
    backend: str,
    model: str,
    dimension: int,
    batch_size: int = 32,
) -> dict:
    """Build an inactive vector generation solely from persisted chunk assets."""
    migrate_active_chunks(db)
    rows = asset_rows(db)
    if not rows:
        raise ValueError("没有可重建的 Chunk 资产；请先入库或从资产包恢复。")
    generation_id = _generation_id(model, dimension)
    table_name = f"my_docs_generation_{generation_id}"
    vectors = []
    for start in range(0, len(rows), max(1, batch_size)):
        batch = rows[start:start + max(1, batch_size)]
        encoded = embedder([item["text"][:512] for item in batch])
        if len(encoded) != len(batch) or any(len(vector) != dimension for vector in encoded):
            raise ValueError("Embedding 返回数量或维度与当前 generation 配置不一致。")
        vectors.extend(encoded)
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), dimension)), pa.field("text", pa.string()),
        pa.field("source", pa.string()), pa.field("chunk_index", pa.int32()), pa.field("category", pa.string()),
    ])
    records = [{"vector": vector, "text": row["text"], "source": row["source"],
                "chunk_index": row["chunk_index"], "category": row["category"]}
               for row, vector in zip(rows, vectors)]
    db.create_table(table_name, data=pa.Table.from_pylist(records, schema=schema), mode="create")
    generation = {
        "generation_id": generation_id, "table_name": table_name, "backend": backend, "model": model,
        "dimension": int(dimension), "chunk_count": len(records), "created_at": _now(), "is_active": False,
    }
    _replace_generation(db, generation)
    return generation


def _copy_table(db, source_name: str, target_name: str) -> None:
    if target_name in _table_names(db):
        db.drop_table(target_name)
    source = db.open_table(source_name)
    db.create_table(target_name, data=source.to_lance().to_table(), mode="create")


def activate_generation(db, generation_id: str) -> dict:
    """Switch after a completed generation exists, retaining the previous generation."""
    generations = {item["generation_id"]: item for item in list_generations(db)}
    if generation_id not in generations:
        raise ValueError(f"未找到 generation「{generation_id}」。先调用 list_embedding_generations 查看。")
    target = dict(generations[generation_id])
    if target["is_active"]:
        return target
    if target["table_name"] not in _table_names(db):
        raise ValueError("目标 generation 表不存在，无法切换。")
    current_asset_count = len(asset_rows(db))
    if target["chunk_count"] != current_asset_count:
        raise ValueError(
            f"目标 generation 只有 {target['chunk_count']} 个 Chunk，但当前文本资产有 {current_asset_count} 个；"
            "请先用当前文本资产重建该模型，避免切换后遗漏新文档。"
        )
    active = next((item for item in generations.values() if item["is_active"]), None)
    staging = "my_docs_generation_switch_staging"
    _copy_table(db, target["table_name"], staging)
    if len(db.open_table(staging)) != target["chunk_count"]:
        db.drop_table(staging)
        raise ValueError("新 generation 校验失败：Chunk 数不一致，活动索引未变更。")
    if active and ACTIVE_TABLE in _table_names(db):
        archive = f"my_docs_generation_{active['generation_id']}_archive"
        _copy_table(db, ACTIVE_TABLE, archive)
        active["table_name"] = archive
        active["is_active"] = False
        _replace_generation(db, active)
        db.drop_table(ACTIVE_TABLE)
    elif ACTIVE_TABLE in _table_names(db):
        db.drop_table(ACTIVE_TABLE)
    _copy_table(db, staging, ACTIVE_TABLE)
    db.drop_table(staging)
    if target["table_name"] in _table_names(db):
        db.drop_table(target["table_name"])
    target["table_name"] = ACTIVE_TABLE
    target["is_active"] = True
    _replace_generation(db, target)
    return target


def export_assets(db, output_path: str) -> dict:
    """Create a portable, vector-independent ZIP asset package atomically."""
    migrate_active_chunks(db)
    rows = asset_rows(db)
    if not rows:
        raise ValueError("知识库没有可导出的 Chunk 资产。")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": "lancedb-search-assets/v1", "created_at": _now(), "chunk_count": len(rows),
                "sha256": hashlib.sha256("\n".join(item["chunk_id"] for item in rows).encode()).hexdigest()}
    descriptor, temporary = tempfile.mkstemp(prefix="kb-assets-", suffix=".zip", dir=str(output.parent))
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("chunks.jsonl", "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows))
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return {"path": str(output), **manifest}


def restore_assets(db, package_path: str) -> dict:
    """Import a portable asset package. Existing identical chunks are skipped."""
    with zipfile.ZipFile(package_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "lancedb-search-assets/v1":
            raise ValueError("不是受支持的 LanceDB Search 资产包。")
        rows = [json.loads(line) for line in archive.read("chunks.jsonl").decode("utf-8").splitlines() if line]
    expected = manifest.get("sha256")
    actual = hashlib.sha256("\n".join(item["chunk_id"] for item in rows).encode()).hexdigest()
    if expected != actual:
        raise ValueError("资产包校验失败：Chunk 清单哈希不匹配。")
    added = add_assets(db, rows)
    return {"imported": added, "total_in_package": len(rows), "manifest": manifest}


def verify_assets(db) -> dict:
    migrate_active_chunks(db)
    rows = asset_rows(db)
    invalid = [item["chunk_id"] for item in rows if item["chunk_id"] != chunk_id(item["text"], item["source"], item["chunk_index"])]
    return {"chunk_count": len(rows), "invalid_chunk_ids": invalid, "generations": list_generations(db)}


def delete_assets_for_sources(db, sources: Iterable[str]) -> int:
    """Delete durable chunks only when their document is deliberately removed."""
    ensure_asset_tables(db)
    table = db.open_table(ASSET_TABLE)
    removed = 0
    for source in set(sources):
        before = len(table)
        # 用普通字符串拼接转义引号（兼容 Python 3.11，不能使用同引号嵌套 f-string）
        escaped = source.replace("'", "''")
        table.delete(f"source = '{escaped}'")
        removed += before - len(table)
    return removed
