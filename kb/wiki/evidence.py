# -*- coding: utf-8 -*-
"""证据校验与 Claim 证据链。

- ``normalize_ws`` / ``verify_quote`` 是纯函数：不读库、不联网；
- ``add_claim_evidence`` 只接受能在切片原文中定位的引文，落库前完成校验；
- 切片原文不在 SQLite：默认用 chunk_refs 的 source_value / chunk_index /
  legacy_doc_id 回查 LanceDB ``my_docs``；测试或调用方已有原文时可传
  ``chunk_text=`` / ``provider=`` 跳过回查；
- ``revalidate_evidence`` 对既有证据重新核对，返回统计但不会删除证据行。
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Callable, Mapping

from .ids import new_ulid, normalize_text_sha256
from .repositories import claims as claim_repo
from .repositories import documents as doc_repo

VERIFIED = "verified"
NOT_FOUND = "not_found"


class EvidenceError(RuntimeError):
    """证据链错误基类。"""


class EvidenceVerificationError(ValueError, EvidenceError):
    """引文无法在切片原文中定位，拒绝写入。"""

    def __init__(self, quote: str, status: str = NOT_FOUND) -> None:
        self.quote = quote
        self.status = status
        super().__init__(f"引文未通过校验（{status}）：{quote[:80]!r}")


class ChunkTextUnavailable(EvidenceError):
    """无法取得 chunk 原文（LanceDB 不可用或定位失败）。"""


def normalize_ws(text: str) -> str:
    """所有连续空白归一为单个空格，并去掉首尾空白。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    return " ".join(text.split())


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """归一化并记录每个归一字符在原文中的下标。"""
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if chars:
                pending_space = True
            continue
        if pending_space:
            chars.append(" ")
            # 空格本身不参与原文定位；映射到后续首个非空白字符。
            offsets.append(index)
            pending_space = False
        chars.append(char)
        offsets.append(index)
    return "".join(chars), offsets


def verify_quote(chunk_text: str, quote: str) -> tuple[str, int, int]:
    """归一化后做子串匹配，返回 ``(status, char_start, char_end)``。

    命中返回 ``("verified", start, end)``，end 为原文切片右开区间；未命中
    返回 ``("not_found", 0, 0)``。空引文一律 not_found。
    """
    if not isinstance(chunk_text, str) or not isinstance(quote, str):
        raise TypeError("chunk_text / quote 必须是字符串")
    normalized_quote = normalize_ws(quote)
    if not normalized_quote:
        return NOT_FOUND, 0, 0

    normalized_chunk, offsets = _normalize_with_offsets(chunk_text)
    position = normalized_chunk.find(normalized_quote)
    if position < 0:
        return NOT_FOUND, 0, 0
    char_start = offsets[position]
    char_end = offsets[position + len(normalized_quote) - 1] + 1
    return VERIFIED, char_start, char_end


def quote_sha256(quote: str) -> str:
    """引文指纹：sha256(normalize_ws(quote))。"""
    return hashlib.sha256(normalize_ws(quote).encode("utf-8")).hexdigest()


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _lookup_text_in_lancedb(
    ref: Mapping[str, Any], table: Any = None
) -> str:
    """按 chunk_ref 的 migration 字段回查 LanceDB 原文。"""
    from .. import schema as kb_schema

    try:
        if table is None:
            if not kb_schema.table_exists():
                raise ChunkTextUnavailable("LanceDB 主表不存在")
            table = kb_schema.get_or_create_table()
        source = str(ref.get("source_value") or "")
        chunk_index = int(ref.get("chunk_index") or 0)
        where = (
            f"source = '{_sql_quote(source)}' AND chunk_index = {chunk_index}"
        )
        arrow = table.to_lance().scanner(
            columns=["text", "source", "chunk_index", "doc_id"], filter=where
        ).to_table()
    except ChunkTextUnavailable:
        raise
    except Exception as exc:
        raise ChunkTextUnavailable(f"LanceDB 原文回查失败: {exc}") from exc

    texts = arrow.column("text").to_pylist()
    doc_ids = arrow.column("doc_id").to_pylist()
    candidates = [
        (str(text or ""), str(doc_id or ""))
        for text, doc_id in zip(texts, doc_ids)
    ]
    if not candidates:
        raise ChunkTextUnavailable(
            f"LanceDB 中找不到 chunk: source={source!r}, index={chunk_index}"
        )

    legacy_doc_id = str(ref.get("legacy_doc_id") or "")
    if legacy_doc_id:
        matched = [text for text, doc_id in candidates if doc_id == legacy_doc_id]
        if matched:
            return matched[0]
        raise ChunkTextUnavailable(
            f"LanceDB 中找不到 legacy_doc_id={legacy_doc_id!r} 的切片"
        )

    if len(candidates) == 1:
        return candidates[0][0]

    expected_sha = str(ref.get("text_sha256") or "")
    matched = [
        text for text, _ in candidates
        if normalize_text_sha256(text) == expected_sha
    ]
    if matched:
        return matched[0]
    raise ChunkTextUnavailable(
        f"同一 (source, chunk_index) 有多条原文且无法按 text_sha256 消歧: "
        f"source={source!r}, index={chunk_index}"
    )


def resolve_chunk_text(
    conn: sqlite3.Connection,
    chunk_id: str,
    *,
    provider: Callable[[Mapping[str, Any]], str] | None = None,
    table: Any = None,
) -> str:
    """取得 chunk 原文：优先调用方 provider，否则按 chunk_ref 回查 LanceDB。"""
    ref = doc_repo.get_chunk_ref(conn, chunk_id)
    if ref is None:
        raise ValueError(f"chunk_ref 不存在: {chunk_id}")
    if provider is not None:
        text = provider(ref)
        if not isinstance(text, str):
            raise TypeError("provider 必须返回字符串")
        return text
    return _lookup_text_in_lancedb(ref, table)


def add_claim_evidence(
    conn: sqlite3.Connection,
    claim_id: str,
    chunk_id: str,
    quote: str,
    role: str = "supports",
    page_number: int | None = None,
    section_path: str = "",
    locator: Any = None,
    *,
    chunk_text: str | None = None,
    confidence: float = 1.0,
    provider: Callable[[Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """校验并写入一条 claim 证据；相同 (claim, chunk, 引文指纹) 幂等。

    ``role`` 是调用方语义标签（supports/contradicts/context 等），当前 0003
    表结构无对应列，只随返回值返回、不落库；page_number / section_path /
    locator 会在 chunk_refs 对应字段为空时补齐。
    """
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role 必须是非空字符串")
    if not isinstance(quote, str) or not normalize_ws(quote):
        raise ValueError("quote 必须包含非空白字符")
    if claim_repo.get_claim(conn, claim_id) is None:
        raise ValueError(f"claim 不存在: {claim_id}")
    ref = doc_repo.get_chunk_ref(conn, chunk_id)
    if ref is None:
        raise ValueError(f"chunk_ref 不存在: {chunk_id}")

    if chunk_text is None:
        chunk_text = resolve_chunk_text(conn, chunk_id, provider=provider)
    elif not isinstance(chunk_text, str):
        raise TypeError("chunk_text 必须是字符串")

    status, char_start, char_end = verify_quote(chunk_text, quote)
    if status != VERIFIED:
        raise EvidenceVerificationError(quote, status)

    sha = quote_sha256(quote)
    existing = claim_repo.find_evidence(
        conn, claim_id=claim_id, chunk_id=chunk_id, text_sha256=sha
    )
    if existing is not None:
        if (
            int(existing["char_start"]) != char_start
            or int(existing["char_end"]) != char_end
        ):
            claim_repo.update_evidence_positions(
                conn,
                existing["evidence_id"],
                char_start=char_start,
                char_end=char_end,
                text_sha256=sha,
            )
        _enrich_chunk_location(
            conn,
            ref,
            page_number=page_number,
            section_path=section_path,
            locator=locator,
        )
        return {
            "evidence_id": existing["evidence_id"],
            "claim_id": claim_id,
            "chunk_id": chunk_id,
            "quote": existing["quote"],
            "role": role,
            "status": VERIFIED,
            "char_start": char_start,
            "char_end": char_end,
            "quote_sha256": sha,
            "created": False,
        }

    evidence_id = new_ulid()
    claim_repo.insert_evidence(
        conn,
        evidence_id=evidence_id,
        claim_id=claim_id,
        chunk_id=chunk_id,
        quote=quote,
        text_sha256=sha,
        char_start=char_start,
        char_end=char_end,
        confidence=confidence,
    )
    _enrich_chunk_location(
        conn,
        ref,
        page_number=page_number,
        section_path=section_path,
        locator=locator,
    )
    return {
        "evidence_id": evidence_id,
        "claim_id": claim_id,
        "chunk_id": chunk_id,
        "quote": quote,
        "role": role,
        "status": VERIFIED,
        "char_start": char_start,
        "char_end": char_end,
        "quote_sha256": sha,
        "created": True,
    }


def _enrich_chunk_location(
    conn: sqlite3.Connection,
    ref: Mapping[str, Any],
    *,
    page_number: int | None,
    section_path: str,
    locator: Any,
) -> None:
    """只补空字段，不覆盖已有定位信息。"""
    fill_page = (
        page_number
        if page_number is not None and ref.get("page_number") is None
        else None
    )
    fill_section = (
        section_path if section_path and not ref.get("section_path") else ""
    )
    locator_json = doc_repo.normalize_locator(locator)
    fill_locator = (
        locator_json
        if locator_json not in ("", "{}")
        and str(ref.get("locator_json") or "{}") in ("", "{}")
        else ""
    )
    if fill_page is not None or fill_section or fill_locator:
        doc_repo.update_chunk_ref_location(
            conn,
            str(ref["chunk_id"]),
            page_number=fill_page,
            section_path=fill_section,
            locator_json=fill_locator,
        )


def revalidate_evidence(
    conn: sqlite3.Connection,
    claim_id: str | None = None,
    *,
    provider: Callable[[Mapping[str, Any]], str] | None = None,
    table: Any = None,
) -> dict[str, int]:
    """重新核对证据引文，返回统计；不删除任何证据行。

    ``not_found`` 表示切片原文已变化或引文被篡改，需要上层人工/任务修复；
    ``skipped_no_text`` 表示 chunk_ref 缺失或原文无法回查。
    """
    rows = claim_repo.list_evidence(conn, claim_id=claim_id)
    stats = {
        "checked": 0,
        "verified": 0,
        "not_found": 0,
        "skipped_no_text": 0,
        "updated": 0,
    }
    for row in rows:
        stats["checked"] += 1
        try:
            text = resolve_chunk_text(
                conn,
                str(row["chunk_id"]),
                provider=provider,
                table=table,
            )
        except (ValueError, ChunkTextUnavailable):
            stats["skipped_no_text"] += 1
            continue

        status, char_start, char_end = verify_quote(text, str(row["quote"]))
        if status != VERIFIED:
            stats["not_found"] += 1
            continue

        stats["verified"] += 1
        sha = quote_sha256(str(row["quote"]))
        if (
            int(row["char_start"]) != char_start
            or int(row["char_end"]) != char_end
            or str(row["text_sha256"]) != sha
        ):
            claim_repo.update_evidence_positions(
                conn,
                str(row["evidence_id"]),
                char_start=char_start,
                char_end=char_end,
                text_sha256=sha,
            )
            stats["updated"] += 1
    return stats


def evidence_bundle(
    conn: sqlite3.Connection,
    chunk_id: str,
    quote: str | None = None,
) -> dict[str, Any] | None:
    """返回 chunk 的完整证据定位链；chunk 不存在时返回 None。

    指定 ``quote`` 时原样带回（便于上层把候选引文与来源一起传给 UI/模型）；
    未指定时取该 chunk 最近一条已存证据的引文（没有则为空字符串）。
    """
    row = conn.execute(
        "SELECT cr.chunk_id, cr.document_id, cr.document_revision_id, "
        "       cr.source_value, cr.chunk_index, cr.text_sha256, cr.legacy_doc_id, "
        "       cr.page_number, cr.section_path, cr.locator_json, "
        "       d.canonical_uri, d.source_type, d.display_name, d.status AS document_status "
        "FROM chunk_refs AS cr "
        "JOIN document_revisions AS dr ON dr.revision_id = cr.document_revision_id "
        "JOIN documents AS d ON d.document_id = cr.document_id "
        "WHERE cr.chunk_id = ?",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None

    quotes = [
        str(item["quote"]) for item in claim_repo.list_evidence(conn, chunk_id=chunk_id)
    ]
    if quote is None:
        quote = quotes[-1] if quotes else ""

    return {
        "chunk_id": row["chunk_id"],
        "document_id": row["document_id"],
        "document_revision_id": row["document_revision_id"],
        "source": row["canonical_uri"] or row["source_value"],
        "source_value": row["source_value"],
        "page_number": row["page_number"],
        "section_path": row["section_path"],
        "quote": quote,
        "quotes": quotes,
        "chunk_index": row["chunk_index"],
        "text_sha256": row["text_sha256"],
        "legacy_doc_id": row["legacy_doc_id"],
        "locator_json": row["locator_json"],
        "source_type": row["source_type"],
        "display_name": row["display_name"],
        "document_status": row["document_status"],
    }


def claims_missing_evidence(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """返回需要补证据的事实型 claim（非 rejected 且无任何证据）。"""
    return claim_repo.fact_claims_missing_evidence(conn)


__all__ = [
    "ChunkTextUnavailable",
    "EvidenceError",
    "EvidenceVerificationError",
    "NOT_FOUND",
    "VERIFIED",
    "add_claim_evidence",
    "claims_missing_evidence",
    "evidence_bundle",
    "normalize_ws",
    "quote_sha256",
    "resolve_chunk_text",
    "revalidate_evidence",
    "verify_quote",
]
