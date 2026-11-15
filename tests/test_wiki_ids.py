# -*- coding: utf-8 -*-
"""稳定 ID、ULID 与文本指纹。"""

import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import ids

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _decode_time_prefix(value: str) -> int:
    decoded = 0
    for char in value[:10]:
        decoded = decoded * 32 + _CROCKFORD.index(char)
    return decoded


class TestDocumentId(unittest.TestCase):
    def test_stable_and_hex(self):
        first = ids.document_id("file", r"D:\docs\a.md")
        second = ids.document_id("file", r"D:\docs\a.md")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        expected = hashlib.sha256(
            "file\x00D:\\docs\\a.md".encode("utf-8")
        ).hexdigest()
        self.assertEqual(first, expected)

    def test_source_type_and_uri_are_sensitive(self):
        base = ids.document_id("file", "a.md")
        self.assertNotEqual(base, ids.document_id("web", "a.md"))
        self.assertNotEqual(base, ids.document_id("file", "b.md"))
        self.assertNotEqual(
            ids.document_id("file", "A.md"), ids.document_id("file", "a.md")
        )

    def test_unicode_uri(self):
        first = ids.document_id("file", r"D:\vault\wiki-维基\页面.md")
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_empty_inputs_rejected(self):
        with self.assertRaises(ValueError):
            ids.document_id("", "a.md")
        with self.assertRaises(ValueError):
            ids.document_id("file", "")


class TestChunkId(unittest.TestCase):
    def test_stable_and_sensitive(self):
        text_hash = ids.content_sha256("chunk text")
        base = ids.chunk_id("rev-1", 0, text_hash)
        self.assertEqual(base, ids.chunk_id("rev-1", 0, text_hash))
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        self.assertNotEqual(base, ids.chunk_id("rev-2", 0, text_hash))
        self.assertNotEqual(base, ids.chunk_id("rev-1", 1, text_hash))
        self.assertNotEqual(
            base, ids.chunk_id("rev-1", 0, ids.content_sha256("other"))
        )

    def test_invalid_index_rejected(self):
        with self.assertRaises(ValueError):
            ids.chunk_id("rev-1", -1, "hash")
        with self.assertRaises(ValueError):
            ids.chunk_id("rev-1", "0", "hash")  # type: ignore[arg-type]


class TestUlid(unittest.TestCase):
    def test_format_and_uniqueness(self):
        values = [ids.new_ulid() for _ in range(300)]
        self.assertEqual(len(values), len(set(values)))
        for value in values:
            self.assertEqual(len(value), 26)
            self.assertTrue(set(value) <= set(_CROCKFORD), value)

    def test_monotonic_order(self):
        values = [ids.new_ulid() for _ in range(300)]
        self.assertEqual(values, sorted(values))

    def test_same_millisecond_random_part_increments(self):
        fixed_ns = 1_700_000_000_000 * 1_000_000
        with mock.patch.object(ids.time, "time_ns", return_value=fixed_ns):
            ids._last_timestamp_ms = 0
            ids._last_random = 0
            values = [ids.new_ulid() for _ in range(5)]
        try:
            self.assertEqual(len(values), len(set(values)))
            self.assertEqual(values, sorted(values))
            self.assertTrue(all(value[:10] == values[0][:10] for value in values))
            self.assertEqual(_decode_time_prefix(values[0]), fixed_ns // 1_000_000)
        finally:
            ids._last_timestamp_ms = 0
            ids._last_random = 0

    def test_clock_rollback_keeps_monotonic(self):
        with mock.patch.object(ids.time, "time_ns", return_value=1_700_000_000_000 * 1_000_000):
            ids._last_timestamp_ms = 0
            ids._last_random = 0
            first = ids.new_ulid()
        with mock.patch.object(ids.time, "time_ns", return_value=1_600_000_000_000 * 1_000_000):
            second = ids.new_ulid()
        try:
            self.assertLess(first, second)
        finally:
            ids._last_timestamp_ms = 0
            ids._last_random = 0


class TestTextHashes(unittest.TestCase):
    def test_content_hash_sensitive_to_newlines(self):
        self.assertNotEqual(
            ids.content_sha256("a\r\nb"), ids.content_sha256("a\nb")
        )
        self.assertRegex(ids.content_sha256("a"), r"^[0-9a-f]{64}$")

    def test_normalize_newlines_and_trailing_whitespace(self):
        self.assertEqual(
            ids.normalize_text_sha256("a  \r\nb\t\r\n"),
            ids.normalize_text_sha256("a\nb\n"),
        )
        self.assertEqual(
            ids.normalize_text_sha256("a"), ids.normalize_text_sha256("a   ")
        )

    def test_normalize_keeps_leading_and_internal_whitespace(self):
        self.assertNotEqual(
            ids.normalize_text_sha256(" a"), ids.normalize_text_sha256("a")
        )
        self.assertNotEqual(
            ids.normalize_text_sha256("a b"), ids.normalize_text_sha256("a  b")
        )
        self.assertNotEqual(
            ids.normalize_text_sha256("a\n\nb"), ids.normalize_text_sha256("a\nb")
        )

    def test_unicode(self):
        self.assertEqual(
            ids.normalize_text_sha256("页面  \r\n"),
            ids.normalize_text_sha256("页面\n"),
        )


if __name__ == "__main__":
    unittest.main()
