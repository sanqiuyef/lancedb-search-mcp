# -*- coding: utf-8 -*-
"""Wiki 稳定标识符与文本指纹。

- ``document_id``：内容源身份（source_type + canonical_uri）的 sha256。
- ``chunk_id``：文档修订内切片的 sha256（含文本指纹，避免同 index 不同文本碰撞）。
- ``new_ulid``：纯 Python 单调 ULID（48bit 毫秒 + 80bit 随机），用于
  page / revision / entity / proposal / job 等实体主键。
- ``content_sha256`` / ``normalize_text_sha256``：原文与规范化文本指纹。

本模块无外部依赖，不读写文件/数据库。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_TIME_CHARS = 10
_ULID_RANDOM_CHARS = 16
_ULID_LENGTH = _ULID_TIME_CHARS + _ULID_RANDOM_CHARS
_ULID_RANDOM_BITS = 80
_ULID_MAX_RANDOM = (1 << _ULID_RANDOM_BITS) - 1

_ulid_lock = threading.Lock()
_last_timestamp_ms = 0
_last_random = 0


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{name} 必须是非空字符串")
    return value


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_id(source_type: str, canonical_uri: str) -> str:
    """文档稳定身份：sha256(f"{source_type}\\x00{canonical_uri}") 的 64 位 hex。"""
    source_type = _require_text("source_type", source_type)
    canonical_uri = _require_text("canonical_uri", canonical_uri)
    return _sha256_hex(f"{source_type}\x00{canonical_uri}".encode("utf-8"))


def chunk_id(revision_id: str, chunk_index: int, text_sha256: str) -> str:
    """切片稳定身份：sha256(revision_id, chunk_index, text_sha256) 的 64 位 hex。"""
    revision_id = _require_text("revision_id", revision_id)
    text_sha256 = _require_text("text_sha256", text_sha256)
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
        raise ValueError("chunk_index 必须是整数")
    if chunk_index < 0:
        raise ValueError("chunk_index 不能为负数")
    raw = f"{revision_id}\x00{chunk_index}\x00{text_sha256}"
    return _sha256_hex(raw.encode("utf-8"))


def _encode_base32(value: int, length: int) -> str:
    if value < 0:
        raise ValueError("value 不能为负数")
    max_value = 1 << (5 * length)
    if value >= max_value:
        raise ValueError("value 超出 Crockford base32 指定位宽")
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成 26 字符单调 ULID。

    同一毫秒内随机部分递增；时钟回拨或随机部分溢出时沿用/推进上一毫秒，
    保证进程内生成的 ULID 字符串严格递增且不重复。线程安全。
    """
    global _last_timestamp_ms, _last_random

    now_ms = time.time_ns() // 1_000_000
    with _ulid_lock:
        if now_ms > _last_timestamp_ms:
            timestamp_ms = now_ms
            random_value = secrets.randbits(_ULID_RANDOM_BITS)
        else:
            timestamp_ms = _last_timestamp_ms
            random_value = _last_random + 1
            if random_value > _ULID_MAX_RANDOM:
                timestamp_ms += 1
                random_value = 0
        _last_timestamp_ms = timestamp_ms
        _last_random = random_value

    return _encode_base32(timestamp_ms, _ULID_TIME_CHARS) + _encode_base32(
        random_value, _ULID_RANDOM_CHARS
    )


def content_sha256(text: str) -> str:
    """原文 UTF-8 sha256（不做任何归一化）。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    return _sha256_hex(text.encode("utf-8"))


def normalize_text_sha256(text: str) -> str:
    """规范化文本 sha256：换行统一为 \\n，逐行去掉行尾空白。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return _sha256_hex(normalized.encode("utf-8"))


__all__ = [
    "content_sha256",
    "chunk_id",
    "document_id",
    "new_ulid",
    "normalize_text_sha256",
]
