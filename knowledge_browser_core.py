# -*- coding: utf-8 -*-
"""Structured, read-mostly services for the LanceDB knowledge browser."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import lancedb


TABLE_NAME = "my_docs"
RELATION_TYPES = ("related", "supports", "contradicts", "depends_on", "supersedes")
SKIP_SOURCE_DIRS = {
    ".git", ".reasonix", ".codex", ".obsidian", "__pycache__", "build", "dist",
    "node_modules", ".venv", "venv",
}


def _path_identity(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.normpath(path))))


def _workspace_root(info: dict) -> str:
    roots = [value for value in info.get("source_roots", []) if value]
    if roots:
        return os.path.realpath(os.path.abspath(os.path.normpath(roots[0])))
    path = os.path.normpath(info.get("path", ""))
    lowered = path.casefold().replace("/", "\\")
    if lowered.endswith("\\.reasonix\\knowledge"):
        return os.path.realpath(os.path.abspath(os.path.dirname(os.path.dirname(path))))
    if os.path.basename(path).casefold() in {"knowledge", "lancedb_data"}:
        parent = os.path.dirname(path)
        cherry_root = _path_identity(r"D:\cherry-workplace")
        if _path_identity(parent).startswith(cherry_root + os.sep):
            return os.path.realpath(os.path.abspath(parent))
    return ""


def canonicalize_kb_config(payload: dict) -> tuple[dict, dict[str, str]]:
    """Enforce one workspace, one registry name and one physical database entry."""
    configured = payload.get("knowledge_bases", {})
    groups: list[list[tuple[str, dict]]] = []
    group_keys: list[set[str]] = []
    for name, raw_info in configured.items():
        info = dict(raw_info)
        path = info.get("path", "")
        if not path:
            continue
        workspace = _workspace_root(info)
        physical = _path_identity(path)
        keys = {f"database:{physical}"}
        if workspace:
            keys.add(f"workspace:{_path_identity(workspace)}")
        matching = [index for index, existing in enumerate(group_keys) if keys & existing]
        if not matching:
            groups.append([(name, info)])
            group_keys.append(keys)
            continue
        target = matching[0]
        groups[target].append((name, info))
        group_keys[target].update(keys)
        for index in reversed(matching[1:]):
            groups[target].extend(groups.pop(index))
            group_keys[target].update(group_keys.pop(index))

    canonical: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for entries in groups:
        workspaces = [_workspace_root(info) for _, info in entries]
        workspaces = [value for value in workspaces if value]

        def preference(entry):
            name, info = entry
            path = os.path.normpath(info.get("path", "")).casefold().replace("/", "\\")
            workspace = _workspace_root(info)
            expected = f"project-{os.path.basename(workspace)}" if workspace else ""
            return (
                0 if expected and name == expected else 1,
                0 if path.endswith("\\.reasonix\\knowledge") else 1,
                0 if name.startswith("project-") else 1,
            )

        chosen_name, chosen_info = min(entries, key=preference)
        chosen_workspace = _workspace_root(chosen_info)
        workspace = chosen_workspace or (workspaces[0] if workspaces else "")
        expected_name = f"project-{os.path.basename(workspace)}" if workspace else ""
        canonical_name = expected_name or chosen_name
        if canonical_name in canonical:
            canonical_name = chosen_name
        merged_roots = []
        for _, info in entries:
            entry_workspace = _workspace_root(info)
            if workspace and entry_workspace and _path_identity(entry_workspace) != _path_identity(workspace):
                continue
            for root in info.get("source_roots", []):
                if root and _path_identity(root) not in {_path_identity(item) for item in merged_roots}:
                    merged_roots.append(os.path.normpath(root))
        chosen_info = dict(chosen_info)
        if merged_roots:
            chosen_info["source_roots"] = merged_roots
        canonical[canonical_name] = chosen_info
        for old_name, _ in entries:
            if old_name != canonical_name:
                aliases[old_name] = canonical_name

    default = payload.get("default", "")
    default = aliases.get(default, default)
    if default not in canonical:
        default = next(iter(canonical), "")
    return {"knowledge_bases": canonical, "default": default}, aliases


def load_kb_config(config_path: str, *, repair: bool = False) -> tuple[dict, dict[str, str]]:
    with open(config_path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    canonical, aliases = canonicalize_kb_config(payload)
    if repair and canonical != payload:
        directory = os.path.dirname(os.path.abspath(config_path))
        descriptor, temporary = tempfile.mkstemp(prefix="kb-config-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(canonical, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, config_path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    return canonical, aliases


def load_knowledge_bases(config_path: str) -> list[dict]:
    """Load configured bases, preserving optional source roots."""
    payload, _ = load_kb_config(config_path, repair=True)
    bases = []
    seen_paths = set()
    for name, info in payload.get("knowledge_bases", {}).items():
        path = os.path.normpath(info.get("path", ""))
        if not path:
            continue
        path_key = _path_identity(path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        roots = [os.path.normpath(value) for value in info.get("source_roots", []) if value]
        bases.append({
            "name": name,
            "path": path,
            "description": info.get("description", ""),
            "source_roots": roots,
        })
    return bases


def available_knowledge_bases(config_path: str) -> list[dict]:
    available = []
    for base in load_knowledge_bases(config_path):
        try:
            db = lancedb.connect(base["path"])
            table_names = db.list_tables().tables if hasattr(db, "list_tables") else db.table_names()
            if TABLE_NAME in table_names:
                available.append(base)
        except Exception:
            continue
    return available


def _safe_filter_value(value: str) -> str:
    return value.replace("'", "''")


def _read_source_rows(table, columns: list[str], source: str | None = None):
    dataset = table.to_lance()
    if source is None:
        return dataset.to_table(columns=columns)
    safe_source = _safe_filter_value(source)
    return dataset.scanner(columns=columns, filter=f"source = '{safe_source}'").to_table()


class SourceResolver:
    """Resolve stored relative sources without changing the database."""

    def __init__(self):
        self._filename_indices: dict[tuple[str, ...], dict[str, list[str]]] = {}

    def _index_roots(self, roots: Iterable[str]) -> dict[str, list[str]]:
        key = tuple(os.path.normcase(os.path.abspath(root)) for root in roots if os.path.isdir(root))
        if key in self._filename_indices:
            return self._filename_indices[key]
        result: dict[str, list[str]] = defaultdict(list)
        for root in key:
            for current, directories, files in os.walk(root):
                directories[:] = [item for item in directories if item not in SKIP_SOURCE_DIRS]
                for filename in files:
                    result[filename.casefold()].append(os.path.join(current, filename))
        self._filename_indices[key] = dict(result)
        return self._filename_indices[key]

    def resolve(self, source: str, roots: Iterable[str]) -> dict:
        if os.path.isabs(source) and os.path.isfile(source):
            return {"status": "resolved", "path": os.path.normpath(source), "candidates": [source]}
        valid_roots = [os.path.normpath(root) for root in roots if os.path.isdir(root)]
        direct = []
        for root in valid_roots:
            candidate = os.path.normpath(os.path.join(root, source))
            if os.path.isfile(candidate):
                direct.append(candidate)
        direct = list(dict.fromkeys(direct))
        if len(direct) == 1:
            return {"status": "resolved", "path": direct[0], "candidates": direct}
        if len(direct) > 1:
            return {"status": "ambiguous", "path": "", "candidates": direct}
        matches = self._index_roots(valid_roots).get(os.path.basename(source).casefold(), [])
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            return {"status": "resolved", "path": matches[0], "candidates": matches}
        if len(matches) > 1:
            return {"status": "ambiguous", "path": "", "candidates": matches}
        return {"status": "missing", "path": "", "candidates": []}


class KnowledgeBrowserData:
    def __init__(self, config_path: str):
        self.config_path = os.path.abspath(config_path)
        self.resolver = SourceResolver()
        self._bases: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.refresh_bases()

    def refresh_bases(self) -> list[dict]:
        _, self.aliases = load_kb_config(self.config_path, repair=True)
        self._bases = {base["name"]: base for base in available_knowledge_bases(self.config_path)}
        return list(self._bases.values())

    @property
    def bases(self) -> list[dict]:
        return list(self._bases.values())

    def base(self, name: str) -> dict:
        if name not in self._bases:
            raise KeyError(f"知识库不可用: {name}")
        return self._bases[name]

    def _table(self, name: str):
        base = self.base(name)
        return lancedb.connect(base["path"]).open_table(TABLE_NAME)

    def list_documents(
        self,
        name: str,
        filter_text: str = "",
        category: str = "",
        extension: str = "",
        sort: str = "source",
        offset: int = 0,
        limit: int | None = None,
        resolve_sources: bool = False,
    ) -> dict:
        table = self._table(name)
        arrow = _read_source_rows(table, ["source", "category"])
        grouped: dict[str, dict] = {}
        for source, row_category in zip(arrow.column("source").to_pylist(), arrow.column("category").to_pylist()):
            entry = grouped.setdefault(source, {"source": source, "chunk_count": 0, "categories": Counter()})
            entry["chunk_count"] += 1
            if row_category:
                entry["categories"][row_category] += 1
        documents = []
        base = self.base(name)
        for source, entry in grouped.items():
            doc_category = entry["categories"].most_common(1)[0][0] if entry["categories"] else ""
            doc_extension = Path(source).suffix.lower().lstrip(".") or "other"
            if filter_text and filter_text.casefold() not in source.casefold():
                continue
            if category and doc_category.casefold() != category.casefold():
                continue
            if extension and doc_extension.casefold() != extension.casefold():
                continue
            resolution = self.resolver.resolve(source, base["source_roots"]) if resolve_sources else {
                "status": "unchecked", "path": "", "candidates": []
            }
            documents.append({
                "id": f"{name}::{source}",
                "knowledge_base": name,
                "source": source,
                "label": os.path.basename(source),
                "extension": doc_extension,
                "category": doc_category,
                "chunk_count": entry["chunk_count"],
                "source_status": resolution["status"],
                "source_path": resolution["path"],
                "source_candidates": resolution["candidates"],
            })
        reverse = sort in {"chunks_desc", "source_desc"}
        key = (lambda item: item["chunk_count"]) if sort.startswith("chunks") else (lambda item: item["source"].casefold())
        documents.sort(key=key, reverse=reverse)
        total = len(documents)
        end = None if limit is None else max(offset, 0) + max(limit, 0)
        return {
            "knowledge_base": name,
            "documents": documents[max(offset, 0):end],
            "total_documents": total,
            "total_chunks": sum(item["chunk_count"] for item in documents),
            "categories": dict(Counter(item["category"] or "未分类" for item in documents)),
            "extensions": dict(Counter(item["extension"] for item in documents)),
        }

    def get_document_chunks(self, name: str, source: str) -> list[dict]:
        table = self._table(name)
        arrow = _read_source_rows(table, ["source", "text", "chunk_index", "category"], source)
        chunks = []
        for row_source, text, index, category in zip(
            arrow.column("source").to_pylist(),
            arrow.column("text").to_pylist(),
            arrow.column("chunk_index").to_pylist(),
            arrow.column("category").to_pylist(),
        ):
            if row_source != source:
                continue
            chunks.append({
                "source": row_source,
                "text": text or "",
                "chunk_index": int(index),
                "category": category or "",
            })
        chunks.sort(key=lambda item: item["chunk_index"])
        return chunks

    def resolve_source_file(self, name: str, source: str) -> dict:
        return self.resolver.resolve(source, self.base(name)["source_roots"])

    def _fingerprint(self, name: str) -> str:
        table = self._table(name)
        arrow = _read_source_rows(table, ["source"])
        sources = sorted(arrow.column("source").to_pylist())
        digest = hashlib.sha256()
        digest.update(str(len(table)).encode("ascii"))
        for source in sources:
            digest.update(b"\0")
            digest.update(source.encode("utf-8", errors="replace"))
        return digest.hexdigest()

    def duplicate_groups(self) -> list[list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for name in self._bases:
            groups[self._fingerprint(name)].append(name)
        return [names for names in groups.values() if len(names) > 1]

    def get_index_health(self, name: str) -> dict:
        listing = self.list_documents(name, resolve_sources=True)
        status_counts = Counter(item["source_status"] for item in listing["documents"])
        table = self._table(name)
        try:
            indices = [getattr(item, "name", str(item)) for item in table.list_indices()]
        except Exception:
            indices = []
        duplicate_groups = [group for group in self.duplicate_groups() if name in group]
        return {
            "knowledge_base": name,
            "database_path": self.base(name)["path"],
            "source_roots": self.base(name)["source_roots"],
            "document_count": listing["total_documents"],
            "chunk_count": len(table),
            "categories": listing["categories"],
            "extensions": listing["extensions"],
            "source_status": dict(status_counts),
            "indices": indices,
            "duplicate_groups": duplicate_groups,
        }


def search_with_trace(
    db_or_path,
    query: str,
    *,
    mode: str = "vector",
    use_reranker: bool = True,
    source_filter: str = "",
    category_filter: str = "",
    limit: int = 20,
    embedder: Callable[[str], list[float]] | None = None,
    reranker: Callable[[str, list[dict], int], list[dict]] | None = None,
    query_expander: Callable[[str], str] | None = None,
    category_router: Callable[[str], dict[str, float]] | None = None,
) -> dict:
    """Run the existing retrieval stages while retaining explainability metadata."""
    if mode not in {"vector", "text", "hybrid"}:
        raise ValueError(f"不支持的搜索模式: {mode}")
    db = lancedb.connect(db_or_path) if isinstance(db_or_path, (str, os.PathLike)) else db_or_path
    table = db.open_table(TABLE_NAME)
    expanded_query = query_expander(query) if query_expander else query
    recall_limit = max(limit * 4, 40)
    vector_raw = []
    fts_raw = []
    if mode in {"vector", "hybrid"}:
        if embedder is None:
            raise ValueError("向量搜索需要 embedder。")
        vector_raw = table.search(embedder(query)).limit(recall_limit).to_list()
    if mode in {"text", "hybrid"}:
        try:
            fts_raw = table.search(expanded_query, query_type="fts").limit(recall_limit).to_list()
        except Exception:
            if mode == "text":
                raise
            fts_raw = []

    candidates: dict[tuple[str, int], dict] = {}
    for rank, raw in enumerate(vector_raw, 1):
        key = (raw.get("source", ""), int(raw.get("chunk_index", 0)))
        item = candidates.setdefault(key, {
            "source": key[0], "chunk_index": key[1], "text": raw.get("text", ""),
            "category": raw.get("category", ""), "vector_rank": None, "vector_distance": None,
            "fts_rank": None, "rrf_score": None, "category_boost": False,
            "rerank_rank": None, "rerank_score": None, "final_rank": None,
        })
        item["vector_rank"] = rank
        item["vector_distance"] = raw.get("_distance")
    for rank, raw in enumerate(fts_raw, 1):
        key = (raw.get("source", ""), int(raw.get("chunk_index", 0)))
        item = candidates.setdefault(key, {
            "source": key[0], "chunk_index": key[1], "text": raw.get("text", ""),
            "category": raw.get("category", ""), "vector_rank": None, "vector_distance": None,
            "fts_rank": None, "rrf_score": None, "category_boost": False,
            "rerank_rank": None, "rerank_score": None, "final_rank": None,
        })
        item["fts_rank"] = rank

    items = list(candidates.values())
    if mode == "hybrid":
        for item in items:
            score = 0.0
            if item["vector_rank"] is not None:
                score += 1.0 / (60 + item["vector_rank"])
            if item["fts_rank"] is not None:
                score += 1.0 / (60 + item["fts_rank"])
            item["rrf_score"] = score
        items.sort(key=lambda item: item["rrf_score"], reverse=True)
    elif mode == "vector":
        items.sort(key=lambda item: item["vector_rank"] or 10**9)
    else:
        items.sort(key=lambda item: item["fts_rank"] or 10**9)

    items = [item for item in items if not source_filter or source_filter.casefold() in item["source"].casefold()]
    items = [item for item in items if not category_filter or item["category"].casefold() == category_filter.casefold()]
    boosts = category_router(query) if category_router else {}
    for item in items:
        item["category_boost"] = item["category"] in boosts
    if boosts:
        items.sort(key=lambda item: not item["category_boost"])

    warning = ""
    reranker_status = "disabled" if not use_reranker else "ok"
    if use_reranker and len(items) > 1:
        try:
            if reranker is None:
                raise ValueError("未配置 Reranker。")
            ranked = reranker(query, items, min(limit, len(items)))
            items = ranked
            for rank, item in enumerate(items, 1):
                item["rerank_rank"] = rank
                item["rerank_score"] = item.get("relevance_score")
        except Exception as error:
            reranker_status = "fallback"
            warning = f"Reranker 失败，已回退到召回顺序: {error}"
            items = items[:limit]
    else:
        items = items[:limit]
    items = items[:limit]
    for rank, item in enumerate(items, 1):
        item["final_rank"] = rank
    return {
        "query": query,
        "expanded_query": expanded_query,
        "mode": mode,
        "reranker_status": reranker_status,
        "warning": warning,
        "results": items,
        "candidate_count": len(candidates),
    }


class RelationStore:
    """Versioned, typed manual relations kept outside LanceDB."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._migrate()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create(connection):
        connection.execute(
            """CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'related',
                note TEXT NOT NULL DEFAULT '',
                evidence_source TEXT NOT NULL DEFAULT '',
                evidence_chunk_index INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, target_id, relation_type)
            )"""
        )

    def _migrate(self):
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='relations'"
            ).fetchone()
            if not exists:
                self._create(connection)
                return
            columns = {row[1] for row in connection.execute("PRAGMA table_info(relations)")}
            if "relation_type" in columns:
                return
            backup = "relations_legacy_v1"
            suffix = 1
            while connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (backup,)
            ).fetchone():
                suffix += 1
                backup = f"relations_legacy_v1_{suffix}"
            connection.execute(f'ALTER TABLE relations RENAME TO "{backup}"')
            self._create(connection)
            now = datetime.now(timezone.utc).isoformat()
            rows = connection.execute(f'SELECT source_id, target_id FROM "{backup}"').fetchall()
            connection.executemany(
                """INSERT OR IGNORE INTO relations
                (source_id, target_id, relation_type, note, evidence_source, evidence_chunk_index, created_at, updated_at)
                VALUES (?, ?, 'related', '', '', NULL, ?, ?)""",
                [(row[0], row[1], now, now) for row in rows],
            )

    def add(self, source_id: str, target_id: str, relation_type: str, note: str = "",
            evidence_source: str = "", evidence_chunk_index: int | None = None) -> int:
        if relation_type not in RELATION_TYPES:
            raise ValueError("不支持的关系类型。")
        if not source_id or not target_id or source_id == target_id:
            raise ValueError("请选择两个不同的文档。")
        if relation_type == "related":
            source_id, target_id = sorted((source_id, target_id))
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT INTO relations
                (source_id, target_id, relation_type, note, evidence_source, evidence_chunk_index, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_id, target_id, relation_type, note, evidence_source, evidence_chunk_index, now, now),
            )
            return int(cursor.lastrowid)

    def update(self, relation_id: int, relation_type: str, note: str,
               evidence_source: str = "", evidence_chunk_index: int | None = None):
        if relation_type not in RELATION_TYPES:
            raise ValueError("不支持的关系类型。")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE relations SET relation_type=?, note=?, evidence_source=?,
                evidence_chunk_index=?, updated_at=? WHERE id=?""",
                (relation_type, note, evidence_source, evidence_chunk_index,
                 datetime.now(timezone.utc).isoformat(), relation_id),
            )

    def delete(self, relation_id: int):
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM relations WHERE id=?", (relation_id,))

    def remap_knowledge_bases(self, aliases: dict[str, str]) -> int:
        """Replace legacy ``kb::source`` IDs with canonical registry names."""
        aliases = {old: new for old, new in aliases.items() if old and new and old != new}
        if not aliases:
            return 0
        moved = 0
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT * FROM relations ORDER BY id").fetchall()
            for row in rows:
                source_id = row["source_id"]
                target_id = row["target_id"]
                for old_name, new_name in aliases.items():
                    old_prefix = old_name + "::"
                    if source_id.startswith(old_prefix):
                        source_id = new_name + "::" + source_id[len(old_prefix):]
                    if target_id.startswith(old_prefix):
                        target_id = new_name + "::" + target_id[len(old_prefix):]
                if source_id == row["source_id"] and target_id == row["target_id"]:
                    continue
                if row["relation_type"] == "related":
                    source_id, target_id = sorted((source_id, target_id))
                connection.execute(
                    """INSERT OR IGNORE INTO relations
                    (source_id, target_id, relation_type, note, evidence_source,
                     evidence_chunk_index, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, target_id, row["relation_type"], row["note"],
                     row["evidence_source"], row["evidence_chunk_index"],
                     row["created_at"], row["updated_at"]),
                )
                connection.execute("DELETE FROM relations WHERE id=?", (row["id"],))
                moved += 1
        return moved

    def list_for_document(self, document_id: str) -> list[dict]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM relations WHERE source_id=? OR target_id=? ORDER BY updated_at DESC",
                (document_id, document_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_for_nodes(self, node_ids: Iterable[str]) -> list[dict]:
        node_ids = list(node_ids)
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        query = (
            f"SELECT * FROM relations WHERE source_id IN ({placeholders}) "
            f"AND target_id IN ({placeholders})"
        )
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, [*node_ids, *node_ids]).fetchall()
        return [{
            "from": row["source_id"], "to": row["target_id"], "kind": "manual",
            "relation_type": row["relation_type"], "note": row["note"], "value": 3,
        } for row in rows]
