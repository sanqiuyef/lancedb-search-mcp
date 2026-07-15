# -*- coding: utf-8 -*-
"""Incremental, evidence-backed content graph stored beside LanceDB.

LanceDB remains the source of truth for full chunk text and vectors.  This
module stores only stable identities, short previews, extracted concepts and
their evidence links so changing a vector model does not destroy the graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from typing import Iterable, Protocol


SCHEMA_VERSION = "1"
NODE_KINDS = ("document", "chunk", "entity", "claim", "method", "topic", "dataset")
EDGE_TYPES = (
    "contains", "mentions", "asserts", "about", "uses", "supports",
    "contradicts", "depends_on", "supersedes", "similar_to", "belongs_to", "evolves",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n.,;:，。；：()（）[]【】")
    return value.casefold()


def _document_fingerprint(chunks: Iterable[dict]) -> tuple[str, list[dict]]:
    ordered = sorted(chunks, key=lambda item: int(item["chunk_index"]))
    digest = hashlib.sha256()
    for chunk in ordered:
        digest.update(str(int(chunk["chunk_index"])).encode("ascii"))
        digest.update(b"\0")
        digest.update((chunk.get("text") or "").encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update((chunk.get("category") or "").encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest(), ordered


class ContentExtractor(Protocol):
    name: str
    version: str

    def extract(self, chunks: list[dict]) -> dict:
        """Return entities, claims and explicit relations with chunk evidence."""


class HeuristicExtractor:
    """Offline baseline used when no graph LLM is configured.

    It deliberately extracts a small, high-signal set.  Every result is marked
    as heuristic so it is never confused with model-verified knowledge.
    """

    name = "heuristic"
    version = "2"
    _english_entity = re.compile(
        r"\b(?:[A-Z][A-Za-z0-9+.-]{1,}(?:\s+[A-Z][A-Za-z0-9+.-]{1,}){0,4}|[A-Z]{2,}(?:-[A-Z0-9]+)*)\b"
    )
    _chinese_term = re.compile(
        r"[\u4e00-\u9fff]{2,14}(?:模型|算法|方法|框架|网络|系统|平台|数据集|指标|机制|策略)"
    )
    _claim_markers = (
        "结果表明", "研究表明", "本文提出", "本文发现", "实验表明", "结果显示", "研究发现",
        "we propose", "we present", "we find", "results show", "results indicate",
        "this study shows", "this paper proposes", "this paper aims", "this paper contributes",
        "this paper implements", "this study aims", "the results demonstrate", "the results show",
        "we demonstrate", "we conclude", "we achieve", "outperforms", "improves the",
        "main objective of this research", "proposed model",
    )
    _method_markers = (
        "model", "method", "algorithm", "framework", "network", "模型", "方法", "算法", "框架", "网络",
    )
    _method_tokens = ("cnn", "lstm", "transformer", "bert", "svm", "random forest", "xgboost", "lightgbm")
    _stop = {
        "the", "this", "that", "figure", "table", "section", "introduction", "abstract",
        "results", "discussion", "conclusion", "copyright", "creative commons", "pdf",
    }

    def _entities(self, text: str) -> list[tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}
        for label in [*self._english_entity.findall(text), *self._chinese_term.findall(text)]:
            label = re.sub(r"\s+", " ", label).strip()
            key = _normalise(label)
            if len(key) < 2 or key in self._stop or key.isdigit():
                continue
            kind = "method" if any(
                marker in key for marker in (*self._method_markers, *self._method_tokens)
            ) else "entity"
            found.setdefault(key, (label, kind))
            if len(found) >= 18:
                break
        return list(found.values())

    def _claims(self, text: str) -> list[str]:
        sentences = re.split(r"(?<=[。！？.!?])\s*|[\r\n]+", text)
        claims = []
        for sentence in sentences:
            compact = re.sub(r"\s+", " ", sentence).strip()
            lowered = compact.casefold()
            if 30 <= len(compact) <= 420 and any(marker in lowered for marker in self._claim_markers):
                claims.append(compact)
            if len(claims) >= 4:
                break
        return claims

    def extract(self, chunks: list[dict]) -> dict:
        candidates: dict[str, dict] = {}
        raw_claims = []
        for chunk in chunks:
            index = int(chunk["chunk_index"])
            text = chunk.get("text") or ""
            chunk_entities = self._entities(text)
            for label, kind in chunk_entities:
                key = f"{kind}\0{_normalise(label)}"
                candidate = candidates.setdefault(key, {
                    "label": label, "kind": kind, "chunks": set(), "count": 0,
                })
                candidate["chunks"].add(index)
                candidate["count"] += 1
            for claim in self._claims(text):
                raw_claims.append((index, claim))
        ranked = []
        for key, candidate in candidates.items():
            label = candidate["label"]
            repeated = len(candidate["chunks"])
            acronym = label.isupper() and len(label) <= 20
            chinese = bool(re.search(r"[\u4e00-\u9fff]", label))
            if repeated < 2 and candidate["kind"] != "method" and not acronym and not chinese:
                continue
            score = repeated * 10 + (8 if candidate["kind"] == "method" else 0) + (4 if acronym else 0)
            ranked.append((score, key, candidate))
        ranked.sort(key=lambda item: (-item[0], len(item[2]["label"]), item[2]["label"]))
        kept = {key: candidate for _, key, candidate in ranked[:140]}
        entities = []
        for candidate in kept.values():
            confidence = min(0.7, 0.42 + len(candidate["chunks"]) * 0.02)
            for index in sorted(candidate["chunks"]):
                entities.append({
                    "label": candidate["label"], "kind": candidate["kind"],
                    "description": "离线规则抽取，待模型或人工确认",
                    "chunk_index": index, "confidence": confidence,
                })
        claims = []
        seen_claims = set()
        labels = [candidate["label"] for candidate in kept.values()]
        for index, claim in raw_claims:
            key = _normalise(claim)
            if key in seen_claims:
                continue
            seen_claims.add(key)
            normalised_claim = _normalise(claim)
            mentioned = [label for label in labels if _normalise(label) in normalised_claim]
            claims.append({
                "text": claim, "chunk_index": index, "confidence": 0.5,
                "entities": mentioned[:8],
            })
            if len(claims) >= 80:
                break
        return {"entities": entities, "claims": claims, "relations": []}


class OpenAICompatibleExtractor:
    """Structured graph extraction through any OpenAI-compatible chat endpoint."""

    version = "1"

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        *,
        timeout: int = 120,
        batch_char_limit: int = 16000,
        post=None,
    ):
        if not endpoint or not model:
            raise ValueError("内容图谱模型需要同时配置接口地址和模型名称。")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.batch_char_limit = max(2000, batch_char_limit)
        self.name = f"openai-compatible:{model}"
        self._post = post

    @staticmethod
    def _parse_json(content: str) -> dict:
        content = (content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("图谱抽取模型没有返回JSON对象。")
        payload = json.loads(content[start:end + 1])
        if not isinstance(payload, dict):
            raise ValueError("图谱抽取结果必须是JSON对象。")
        for key in ("entities", "claims", "relations"):
            if not isinstance(payload.get(key, []), list):
                raise ValueError(f"图谱抽取字段 {key} 必须是数组。")
            payload.setdefault(key, [])
        return payload

    def _batches(self, chunks: list[dict]):
        batch, size = [], 0
        for chunk in chunks:
            text = chunk.get("text") or ""
            item_size = len(text)
            if batch and size + item_size > self.batch_char_limit:
                yield batch
                batch, size = [], 0
            batch.append(chunk)
            size += item_size
        if batch:
            yield batch

    def _extract_batch(self, chunks: list[dict]) -> dict:
        if self._post is None:
            import requests
            post = requests.post
        else:
            post = self._post
        allowed_indices = {int(item["chunk_index"]) for item in chunks}
        source_text = "\n\n".join(
            f"[Chunk #{int(item['chunk_index'])}]\n{item.get('text') or ''}" for item in chunks
        )
        system = (
            "You extract an evidence-backed content graph. Return JSON only with arrays entities, claims, relations. "
            "Entity item: {label, kind, description, chunk_index, confidence}; kind must be entity, method, dataset, or topic. "
            "Claim item: {text, chunk_index, confidence, entities:[labels]}. "
            "Relation item: {source, target, type, chunk_index, confidence, note}; type must be uses, supports, contradicts, "
            "depends_on, supersedes, similar_to, belongs_to, or evolves. Every item must cite exactly one provided chunk_index. "
            "Prefer a small number of high-confidence domain concepts and substantive claims; do not extract headings or generic words."
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = post(
            self.endpoint,
            headers=headers,
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": source_text},
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        payload = self._parse_json(body["choices"][0]["message"]["content"])
        for key in ("entities", "claims", "relations"):
            payload[key] = [
                item for item in payload[key]
                if isinstance(item, dict) and int(item.get("chunk_index", -1)) in allowed_indices
            ]
        return payload

    def extract(self, chunks: list[dict]) -> dict:
        merged = {"entities": [], "claims": [], "relations": []}
        for batch in self._batches(chunks):
            result = self._extract_batch(batch)
            for key in merged:
                merged[key].extend(result[key])
        return merged


def extractor_from_environment() -> ContentExtractor:
    """Create the configured extractor without making a network request."""
    kind = os.environ.get("CONTENT_GRAPH_EXTRACTOR", "heuristic").strip().casefold()
    if kind in {"", "heuristic", "offline"}:
        return HeuristicExtractor()
    if kind in {"openai", "openai-compatible", "api"}:
        return OpenAICompatibleExtractor(
            os.environ.get("CONTENT_GRAPH_LLM_URL", "").strip(),
            os.environ.get("CONTENT_GRAPH_LLM_MODEL", "").strip(),
            os.environ.get("CONTENT_GRAPH_LLM_API_KEY", "").strip(),
            timeout=int(os.environ.get("CONTENT_GRAPH_LLM_TIMEOUT", "120")),
            batch_char_limit=int(os.environ.get("CONTENT_GRAPH_BATCH_CHARS", "16000")),
        )
    raise ValueError(f"不支持的内容图谱抽取器: {kind}")


class ContentGraphStore:
    """SQLite sidecar for the content graph; never writes to LanceDB."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._initialise()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialise(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS graph_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_documents (
                    id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    extractor TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_locations (
                    knowledge_base TEXT NOT NULL,
                    source TEXT NOT NULL,
                    document_id TEXT NOT NULL REFERENCES graph_documents(id) ON DELETE CASCADE,
                    category TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (knowledge_base, source)
                );
                CREATE INDEX IF NOT EXISTS idx_graph_locations_document
                    ON graph_locations(document_id);
                CREATE TABLE IF NOT EXISTS graph_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES graph_documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text_hash TEXT NOT NULL,
                    text_preview TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    UNIQUE(document_id, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    normalised_label TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_graph_nodes_kind ON graph_nodes(kind);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_concept_identity
                    ON graph_nodes(kind, normalised_label)
                    WHERE kind IN ('entity', 'claim', 'method', 'topic', 'dataset');
                CREATE TABLE IF NOT EXISTS graph_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    evidence_chunk_id TEXT REFERENCES graph_chunks(id) ON DELETE CASCADE,
                    confidence REAL NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL,
                    owner_document_id TEXT NOT NULL REFERENCES graph_documents(id) ON DELETE CASCADE,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_id, target_id, relation_type, evidence_chunk_id, owner_document_id)
                );
                CREATE INDEX IF NOT EXISTS idx_graph_edges_owner ON graph_edges(owner_document_id);
                CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id);
                CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    @staticmethod
    def _node_id(kind: str, label: str) -> str:
        return f"{kind}:{_digest(f'{kind}\0{_normalise(label)}')[:24]}"

    @staticmethod
    def _chunk_id(document_id: str, chunk_index: int, text_hash: str) -> str:
        return f"chunk:{_digest(f'{document_id}\0{chunk_index}\0{text_hash}')[:24]}"

    @staticmethod
    def _extractor_signature(extractor: ContentExtractor) -> tuple[str, str]:
        return str(extractor.name), str(extractor.version)

    def _upsert_node(
        self,
        connection,
        kind: str,
        label: str,
        description: str = "",
        properties: dict | None = None,
        node_id: str | None = None,
    ) -> str:
        if kind not in NODE_KINDS:
            raise ValueError(f"不支持的节点类型: {kind}")
        normalised = _normalise(label)
        if not normalised:
            raise ValueError("图谱节点名称不能为空。")
        node_id = node_id or self._node_id(kind, label)
        now = _utc_now()
        if kind in {"entity", "claim", "method", "topic", "dataset"}:
            existing = connection.execute(
                "SELECT id FROM graph_nodes WHERE kind=? AND normalised_label=?", (kind, normalised)
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE graph_nodes SET label=?,
                       description=CASE WHEN ?<>'' THEN ? ELSE description END,
                       properties_json=?, updated_at=? WHERE id=?""",
                    (label, description, description, json.dumps(properties or {}, ensure_ascii=False),
                     now, existing["id"]),
                )
                return existing["id"]
        connection.execute(
            """INSERT INTO graph_nodes
               (id, kind, label, normalised_label, description, properties_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   label=excluded.label, normalised_label=excluded.normalised_label,
                   description=CASE WHEN excluded.description<>'' THEN excluded.description ELSE graph_nodes.description END,
                   properties_json=excluded.properties_json, updated_at=excluded.updated_at""",
            (node_id, kind, label, normalised, description, json.dumps(properties or {}, ensure_ascii=False), now, now),
        )
        return node_id

    def _add_edge(
        self,
        connection,
        source_id: str,
        target_id: str,
        relation_type: str,
        evidence_chunk_id: str | None,
        confidence: float,
        origin: str,
        owner_document_id: str,
        note: str = "",
    ):
        if relation_type not in EDGE_TYPES:
            raise ValueError(f"不支持的关系类型: {relation_type}")
        now = _utc_now()
        connection.execute(
            """INSERT INTO graph_edges
               (source_id, target_id, relation_type, evidence_chunk_id, confidence, origin,
                owner_document_id, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, target_id, relation_type, evidence_chunk_id, owner_document_id)
               DO UPDATE SET confidence=MAX(graph_edges.confidence, excluded.confidence),
                             origin=excluded.origin, note=excluded.note, updated_at=excluded.updated_at""",
            (source_id, target_id, relation_type, evidence_chunk_id, max(0.0, min(1.0, float(confidence))),
             origin, owner_document_id, note, now, now),
        )

    def _has_current_graph(self, connection, document_id: str, extractor: ContentExtractor) -> bool:
        name, version = self._extractor_signature(extractor)
        row = connection.execute(
            "SELECT extractor, extractor_version FROM graph_documents WHERE id=?", (document_id,)
        ).fetchone()
        if not row or row["extractor"] != name or row["extractor_version"] != version:
            return False
        return connection.execute(
            "SELECT 1 FROM graph_edges WHERE owner_document_id=? LIMIT 1", (document_id,)
        ).fetchone() is not None

    @staticmethod
    def _remove_orphan_document(connection, document_id: str | None):
        if not document_id:
            return
        still_used = connection.execute(
            "SELECT 1 FROM graph_locations WHERE document_id=? LIMIT 1", (document_id,)
        ).fetchone()
        if not still_used:
            chunk_ids = [row["id"] for row in connection.execute(
                "SELECT id FROM graph_chunks WHERE document_id=?", (document_id,)
            )]
            connection.execute("DELETE FROM graph_documents WHERE id=?", (document_id,))
            connection.execute("DELETE FROM graph_nodes WHERE id=?", (document_id,))
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                connection.execute(f"DELETE FROM graph_nodes WHERE id IN ({placeholders})", chunk_ids)
            connection.execute(
                """DELETE FROM graph_nodes
                   WHERE kind NOT IN ('document', 'chunk')
                     AND NOT EXISTS (SELECT 1 FROM graph_edges e
                                     WHERE e.source_id=graph_nodes.id OR e.target_id=graph_nodes.id)"""
            )

    def index_document(
        self,
        knowledge_base: str,
        source: str,
        chunks: list[dict],
        extractor: ContentExtractor | None = None,
        *,
        force: bool = False,
    ) -> dict:
        """Build or reuse one document graph and bind its current source location."""
        if not knowledge_base or not source:
            raise ValueError("知识库和来源不能为空。")
        if not chunks:
            raise ValueError("文档没有可用于构建图谱的 Chunk。")
        extractor = extractor or HeuristicExtractor()
        content_hash, ordered = _document_fingerprint(chunks)
        document_id = f"document:{content_hash[:24]}"
        category = next((item.get("category") or "" for item in ordered if item.get("category")), "")

        with closing(self._connect()) as connection:
            if not force and self._has_current_graph(connection, document_id, extractor):
                now = _utc_now()
                with connection:
                    previous = connection.execute(
                        "SELECT document_id FROM graph_locations WHERE knowledge_base=? AND source=?",
                        (knowledge_base, source),
                    ).fetchone()
                    connection.execute(
                        """INSERT INTO graph_locations(knowledge_base, source, document_id, category, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(knowledge_base, source) DO UPDATE SET
                               document_id=excluded.document_id, category=excluded.category, updated_at=excluded.updated_at""",
                        (knowledge_base, source, document_id, category, now),
                    )
                    if previous and previous["document_id"] != document_id:
                        self._remove_orphan_document(connection, previous["document_id"])
                return {"status": "unchanged", "document_id": document_id, **self.document_stats(document_id)}

        extraction = extractor.extract(ordered)
        name, version = self._extractor_signature(extractor)
        title = os.path.basename(source) or source
        now = _utc_now()
        chunk_ids: dict[int, str] = {}
        entity_ids: dict[str, str] = {}
        claim_ids: dict[str, str] = {}

        with closing(self._connect()) as connection, connection:
            previous = connection.execute(
                "SELECT document_id FROM graph_locations WHERE knowledge_base=? AND source=?",
                (knowledge_base, source),
            ).fetchone()
            connection.execute(
                """INSERT INTO graph_documents
                   (id, content_hash, title, chunk_count, extractor, extractor_version, indexed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, chunk_count=excluded.chunk_count,
                       extractor=excluded.extractor, extractor_version=excluded.extractor_version,
                       indexed_at=excluded.indexed_at""",
                (document_id, content_hash, title, len(ordered), name, version, now),
            )
            connection.execute(
                """INSERT INTO graph_locations(knowledge_base, source, document_id, category, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(knowledge_base, source) DO UPDATE SET
                       document_id=excluded.document_id, category=excluded.category, updated_at=excluded.updated_at""",
                (knowledge_base, source, document_id, category, now),
            )
            if previous and previous["document_id"] != document_id:
                self._remove_orphan_document(connection, previous["document_id"])
            connection.execute("DELETE FROM graph_edges WHERE owner_document_id=?", (document_id,))
            old_chunks = [row["id"] for row in connection.execute(
                "SELECT id FROM graph_chunks WHERE document_id=?", (document_id,)
            )]
            connection.execute("DELETE FROM graph_chunks WHERE document_id=?", (document_id,))
            if old_chunks:
                placeholders = ",".join("?" for _ in old_chunks)
                connection.execute(f"DELETE FROM graph_nodes WHERE id IN ({placeholders})", old_chunks)

            document_node_id = self._upsert_node(
                connection, "document", title,
                properties={"content_hash": content_hash, "chunk_count": len(ordered)},
                node_id=document_id,
            )
            for chunk in ordered:
                index = int(chunk["chunk_index"])
                text = chunk.get("text") or ""
                text_hash = _digest(text)
                chunk_id = self._chunk_id(document_id, index, text_hash)
                chunk_ids[index] = chunk_id
                preview = re.sub(r"\s+", " ", text).strip()[:800]
                connection.execute(
                    """INSERT INTO graph_chunks
                       (id, document_id, chunk_index, text_hash, text_preview, category)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chunk_id, document_id, index, text_hash, preview, chunk.get("category") or ""),
                )
                self._upsert_node(
                    connection, "chunk", f"Chunk #{index}", preview,
                    {"chunk_index": index, "text_hash": text_hash}, node_id=chunk_id,
                )
                self._add_edge(connection, document_node_id, chunk_id, "contains", chunk_id, 1.0,
                               "structural", document_id)

            for entity in extraction.get("entities", []):
                label = str(entity.get("label") or "").strip()
                kind = str(entity.get("kind") or "entity")
                index = int(entity.get("chunk_index", -1))
                if not label or kind not in {"entity", "method", "dataset", "topic"} or index not in chunk_ids:
                    continue
                key = f"{kind}\0{_normalise(label)}"
                node_id = entity_ids.get(key) or self._upsert_node(
                    connection, kind, label, str(entity.get("description") or ""),
                    {"extraction_origin": name},
                )
                entity_ids[key] = node_id
                self._add_edge(connection, chunk_ids[index], node_id, "mentions", chunk_ids[index],
                               float(entity.get("confidence", 0.5)), name, document_id)

            for claim in extraction.get("claims", []):
                text = str(claim.get("text") or "").strip()
                index = int(claim.get("chunk_index", -1))
                if not text or index not in chunk_ids:
                    continue
                node_id = claim_ids.get(_normalise(text)) or self._upsert_node(
                    connection, "claim", text, "", {"extraction_origin": name},
                )
                claim_ids[_normalise(text)] = node_id
                confidence = float(claim.get("confidence", 0.5))
                self._add_edge(connection, chunk_ids[index], node_id, "asserts", chunk_ids[index],
                               confidence, name, document_id)
                for label in claim.get("entities", []):
                    target = None
                    normalised = _normalise(str(label))
                    for key, entity_id in entity_ids.items():
                        if key.endswith("\0" + normalised):
                            target = entity_id
                            break
                    if target:
                        self._add_edge(connection, node_id, target, "about", chunk_ids[index], confidence,
                                       name, document_id)

            label_lookup = {
                _normalise(row["label"]): row["id"]
                for row in connection.execute(
                    """SELECT DISTINCT n.id, n.label FROM graph_nodes n
                       JOIN graph_edges e ON (e.source_id=n.id OR e.target_id=n.id)
                       WHERE e.owner_document_id=?""",
                    (document_id,),
                )
            }
            for relation in extraction.get("relations", []):
                relation_type = str(relation.get("type") or "")
                index = int(relation.get("chunk_index", -1))
                left = label_lookup.get(_normalise(str(relation.get("source") or "")))
                right = label_lookup.get(_normalise(str(relation.get("target") or "")))
                if relation_type not in EDGE_TYPES or not left or not right or index not in chunk_ids:
                    continue
                self._add_edge(
                    connection, left, right, relation_type, chunk_ids[index],
                    float(relation.get("confidence", 0.5)), name, document_id,
                    str(relation.get("note") or ""),
                )

            connection.execute(
                """DELETE FROM graph_nodes
                   WHERE kind NOT IN ('document', 'chunk')
                     AND NOT EXISTS (SELECT 1 FROM graph_edges e
                                     WHERE e.source_id=graph_nodes.id OR e.target_id=graph_nodes.id)"""
            )

        return {"status": "indexed", "document_id": document_id, **self.document_stats(document_id)}

    def document_stats(self, document_id: str) -> dict:
        with closing(self._connect()) as connection:
            node_count = connection.execute(
                """SELECT COUNT(DISTINCT node_id) AS count FROM (
                       SELECT source_id AS node_id FROM graph_edges WHERE owner_document_id=?
                       UNION SELECT target_id FROM graph_edges WHERE owner_document_id=?
                   )""",
                (document_id, document_id),
            ).fetchone()["count"]
            edge_count = connection.execute(
                "SELECT COUNT(*) AS count FROM graph_edges WHERE owner_document_id=?", (document_id,)
            ).fetchone()["count"]
        return {"node_count": int(node_count), "edge_count": int(edge_count)}

    def document_for_location(self, knowledge_base: str, source: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT document_id FROM graph_locations WHERE knowledge_base=? AND source=?",
                (knowledge_base, source),
            ).fetchone()
        return row["document_id"] if row else None

    def remap_knowledge_bases(self, aliases: dict[str, str]) -> int:
        """Move legacy registry aliases to canonical names without duplicating locations."""
        aliases = {old: new for old, new in aliases.items() if old and new and old != new}
        if not aliases:
            return 0
        moved = 0
        with closing(self._connect()) as connection, connection:
            orphan_candidates = set()
            for old_name, new_name in aliases.items():
                rows = connection.execute(
                    "SELECT * FROM graph_locations WHERE knowledge_base=?", (old_name,)
                ).fetchall()
                for row in rows:
                    orphan_candidates.add(row["document_id"])
                    connection.execute(
                        """INSERT OR IGNORE INTO graph_locations
                           (knowledge_base, source, document_id, category, updated_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (new_name, row["source"], row["document_id"], row["category"], row["updated_at"]),
                    )
                cursor = connection.execute(
                    "DELETE FROM graph_locations WHERE knowledge_base=?", (old_name,)
                )
                moved += cursor.rowcount
            for document_id in orphan_candidates:
                self._remove_orphan_document(connection, document_id)
        return moved

    def get_document_graph(self, knowledge_base: str, source: str, limit: int = 300) -> dict:
        document_id = self.document_for_location(knowledge_base, source)
        if not document_id:
            return {"nodes": [], "edges": [], "document_id": None, "truncated": False}
        with closing(self._connect()) as connection:
            edge_rows = connection.execute(
                """SELECT e.*, c.chunk_index AS evidence_chunk_index, c.text_preview AS evidence_preview
                   FROM graph_edges e
                   LEFT JOIN graph_chunks c ON c.id=e.evidence_chunk_id
                   WHERE e.owner_document_id=?
                   ORDER BY CASE e.relation_type
                       WHEN 'asserts' THEN 0 WHEN 'supports' THEN 0 WHEN 'contradicts' THEN 0
                       WHEN 'supersedes' THEN 0 WHEN 'evolves' THEN 0 WHEN 'depends_on' THEN 0
                       WHEN 'uses' THEN 0 WHEN 'about' THEN 1 WHEN 'mentions' THEN 2 ELSE 3 END,
                       e.id
                   LIMIT ?""",
                (document_id, max(1, limit * 3)),
            ).fetchall()
            node_ids = [document_id]
            seen = {document_id}
            selected_edges = []
            for row in edge_rows:
                needed = [row["source_id"], row["target_id"]]
                new_ids = [item for item in needed if item not in seen]
                if len(seen) + len(new_ids) > limit:
                    continue
                seen.update(new_ids)
                node_ids.extend(new_ids)
                selected_edges.append(row)
            selected_edge_ids = {row["id"] for row in selected_edges}
            evidence_chunks = [node_id for node_id in seen if node_id.startswith("chunk:")]
            if evidence_chunks:
                chunk_placeholders = ",".join("?" for _ in evidence_chunks)
                for row in connection.execute(
                    f"""SELECT e.*, c.chunk_index AS evidence_chunk_index,
                               c.text_preview AS evidence_preview
                        FROM graph_edges e
                        LEFT JOIN graph_chunks c ON c.id=e.evidence_chunk_id
                        WHERE e.owner_document_id=? AND e.relation_type='contains'
                          AND e.target_id IN ({chunk_placeholders})""",
                    (document_id, *evidence_chunks),
                ):
                    if row["id"] not in selected_edge_ids:
                        selected_edges.append(row)
                        selected_edge_ids.add(row["id"])
            placeholders = ",".join("?" for _ in node_ids)
            rows = connection.execute(
                f"SELECT * FROM graph_nodes WHERE id IN ({placeholders})", node_ids
            ).fetchall() if node_ids else []
            location_rows = connection.execute(
                "SELECT knowledge_base, source FROM graph_locations WHERE document_id=? ORDER BY knowledge_base, source",
                (document_id,),
            ).fetchall()
            total_edge_count = int(connection.execute(
                "SELECT COUNT(*) FROM graph_edges WHERE owner_document_id=?", (document_id,)
            ).fetchone()[0])
            total_node_count = int(connection.execute(
                """SELECT COUNT(DISTINCT node_id) FROM (
                       SELECT source_id AS node_id FROM graph_edges WHERE owner_document_id=?
                       UNION SELECT target_id FROM graph_edges WHERE owner_document_id=?
                   )""",
                (document_id, document_id),
            ).fetchone()[0])
        nodes = []
        for row in rows:
            item = dict(row)
            item["properties"] = json.loads(item.pop("properties_json") or "{}")
            nodes.append(item)
        edges = []
        for row in selected_edges:
            item = dict(row)
            item["from"] = item.pop("source_id")
            item["to"] = item.pop("target_id")
            edges.append(item)
        return {
            "nodes": nodes,
            "edges": edges,
            "document_id": document_id,
            "locations": [dict(row) for row in location_rows],
            "truncated": len(nodes) < total_node_count or len(selected_edges) < total_edge_count,
        }

    def get_evidence(self, edge_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT e.id, e.relation_type, e.note, e.confidence, e.origin,
                          c.chunk_index, c.text_preview, l.knowledge_base, l.source
                   FROM graph_edges e
                   LEFT JOIN graph_chunks c ON c.id=e.evidence_chunk_id
                   LEFT JOIN graph_locations l ON l.document_id=e.owner_document_id
                   WHERE e.id=?
                   ORDER BY l.knowledge_base, l.source LIMIT 1""",
                (edge_id,),
            ).fetchone()
        return dict(row) if row else None

    def search_nodes(self, query: str, kind: str = "", limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        if kind and kind not in NODE_KINDS:
            raise ValueError(f"不支持的节点类型: {kind}")
        conditions = ["(n.label LIKE ? OR n.description LIKE ?)"]
        parameters: list[object] = [f"%{query}%", f"%{query}%"]
        if kind:
            conditions.append("n.kind=?")
            parameters.append(kind)
        parameters.append(max(1, min(int(limit), 100)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT n.*, COUNT(DISTINCT e.id) AS evidence_edges
                     FROM graph_nodes n
                     LEFT JOIN graph_edges e ON (e.source_id=n.id OR e.target_id=n.id)
                     WHERE {' AND '.join(conditions)}
                     GROUP BY n.id
                     ORDER BY evidence_edges DESC, LENGTH(n.label), n.label
                     LIMIT ?""",
                parameters,
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["properties"] = json.loads(item.pop("properties_json") or "{}")
                locations = connection.execute(
                    """SELECT DISTINCT l.knowledge_base, l.source
                       FROM graph_edges e
                       JOIN graph_locations l ON l.document_id=e.owner_document_id
                       WHERE e.source_id=? OR e.target_id=?
                       ORDER BY l.knowledge_base, l.source LIMIT 20""",
                    (item["id"], item["id"]),
                ).fetchall()
                item["locations"] = [dict(location) for location in locations]
                results.append(item)
        return results

    def stats(self) -> dict:
        with closing(self._connect()) as connection:
            result = {}
            for key, table in {
                "documents": "graph_documents", "locations": "graph_locations",
                "chunks": "graph_chunks", "nodes": "graph_nodes", "edges": "graph_edges",
            }.items():
                result[key] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            result["node_kinds"] = {
                row["kind"]: int(row["count"])
                for row in connection.execute(
                    "SELECT kind, COUNT(*) AS count FROM graph_nodes GROUP BY kind ORDER BY kind"
                )
            }
        return result
