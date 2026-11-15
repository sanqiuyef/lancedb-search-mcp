# -*- coding: utf-8 -*-
"""Markdown frontmatter 安全解析、校验与 wiki link 解析。"""

import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import markdown as md


def _valid_data():
    return {
        "title": "测试页面",
        "tags": ["wiki", "测试"],
        "created": date(2026, 11, 15),
        "type": "permanent",
        "summary": "一句话摘要",
        "page_id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "slug": "test-page",
        "page_type": "concept",
        "status": "published",
        "aliases": ["测试页", "Test Page"],
        "revision_id": "01JABCDEFGHJKMNPQRSTVWXYZ1",
        "updated": "2026-11-15T09:00:00Z",
    }


class TestFrontmatterRoundTrip(unittest.TestCase):
    def test_parse_render_round_trip(self):
        data = _valid_data()
        body = "# 测试\n\n见 [[Other Page|别名]]。\n"
        rendered = md.render_frontmatter(data, body)
        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn("\n---\n", rendered)

        parsed, parsed_body = md.parse_frontmatter(rendered)
        self.assertEqual(parsed, data)
        self.assertEqual(parsed_body, body)

    def test_empty_frontmatter(self):
        rendered = md.render_frontmatter({}, "正文\n")
        self.assertEqual(rendered, "---\n---\n正文\n")
        self.assertEqual(md.parse_frontmatter(rendered), ({}, "正文\n"))

    def test_no_frontmatter_returns_empty_mapping(self):
        self.assertEqual(md.parse_frontmatter("# 普通文档\n"), ({}, "# 普通文档\n"))
        self.assertEqual(md.parse_frontmatter(""), ({}, ""))

    def test_bom_is_ignored(self):
        data, body = md.parse_frontmatter("\ufeff---\ntitle: x\n---\nbody")
        self.assertEqual(data, {"title": "x"})
        self.assertEqual(body, "body")

    def test_body_newlines_preserved(self):
        data = {"title": "x"}
        body = "\n\n第一行\n第二行\n"
        parsed, parsed_body = md.parse_frontmatter(
            md.render_frontmatter(data, body)
        )
        self.assertEqual(parsed, data)
        self.assertEqual(parsed_body, body)

    def test_render_requires_mapping_and_string_body(self):
        with self.assertRaises(TypeError):
            md.render_frontmatter(["not", "mapping"], "")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            md.render_frontmatter({}, b"bytes")  # type: ignore[arg-type]


class TestFrontmatterErrors(unittest.TestCase):
    def test_missing_closing_delimiter(self):
        with self.assertRaises(md.FrontmatterError):
            md.parse_frontmatter("---\ntitle: x\n")

    def test_non_mapping_yaml(self):
        with self.assertRaises(md.FrontmatterError):
            md.parse_frontmatter("---\n- a\n- b\n---\n")

    def test_broken_yaml(self):
        with self.assertRaises(md.FrontmatterError):
            md.parse_frontmatter("---\ntitle: [unclosed\n---\n")

    @unittest.skipIf(md._yaml is None, "需要 PyYAML 验证 unsafe tag 被拒绝")
    def test_python_tag_rejected_by_safe_load(self):
        with self.assertRaises(md.FrontmatterError):
            md.parse_frontmatter(
                "---\ntitle: !!python/object/apply:os.system ['echo hi']\n---\n"
            )


class TestValidatePageFrontmatter(unittest.TestCase):
    def test_valid_wiki_page_passes(self):
        data = _valid_data()
        self.assertEqual(md.validate_page_frontmatter(data), data)

    def test_missing_required_fields_reported(self):
        required = [
            "title",
            "tags",
            "created",
            "type",
            "summary",
            "page_id",
            "slug",
            "page_type",
            "status",
            "aliases",
            "revision_id",
            "updated",
        ]
        for field in required:
            with self.subTest(field=field):
                data = _valid_data()
                data.pop(field)
                with self.assertRaises(md.FrontmatterValidationError) as ctx:
                    md.validate_page_frontmatter(data)
                self.assertIn(field, str(ctx.exception))

    def test_tags_and_aliases_must_be_string_lists(self):
        data = _valid_data()
        data["tags"] = "not-a-list"
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

        data = _valid_data()
        data["aliases"] = ["ok", 3]
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

    def test_created_must_be_valid_date(self):
        data = _valid_data()
        data["created"] = "2026-13-01"
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

        data = _valid_data()
        data["created"] = "2026/11/15"
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

    def test_wiki_type_must_be_permanent(self):
        data = _valid_data()
        data["type"] = "note"
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

        # 普通 vault 页面可以复用 vault 部分契约
        self.assertEqual(
            md.validate_page_frontmatter(data, require_wiki=False), data
        )

    def test_page_type_and_status_enum(self):
        data = _valid_data()
        data["page_type"] = "bogus"
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

        data = _valid_data()
        data["status"] = "bogus"
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(data)

    def test_unquoted_updated_timestamp_is_accepted(self):
        data = _valid_data()
        data["updated"] = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(md.validate_page_frontmatter(data), data)

        parsed, _ = md.parse_frontmatter(
            "---\nupdated: 2026-11-15T09:00:00Z\n---\n"
        )
        self.assertIsInstance(parsed["updated"], datetime)

    def test_non_mapping_rejected(self):
        with self.assertRaises(md.FrontmatterValidationError):
            md.validate_page_frontmatter(["not", "mapping"])  # type: ignore[arg-type]


class TestSplitWikiLinks(unittest.TestCase):
    def test_targets_and_aliases(self):
        body = "A [[Target]] B [[Other|Alias]] C [[ Third | 别名 ]]"
        self.assertEqual(
            md.split_wiki_links(body), ["Target", "Other", "Third"]
        )

    def test_anchor_is_kept_in_target(self):
        self.assertEqual(
            md.split_wiki_links("[[Page#Section|小节]]"), ["Page#Section"]
        )

    def test_duplicates_are_preserved_in_order(self):
        self.assertEqual(
            md.split_wiki_links("[[A]] [[B]] [[A]]"), ["A", "B", "A"]
        )

    def test_invalid_links_ignored(self):
        self.assertEqual(md.split_wiki_links("[single] [[unclosed"), [])
        self.assertEqual(md.split_wiki_links("[[ | alias ]]"), [])
        self.assertEqual(md.split_wiki_links(""), [])

    def test_non_string_rejected(self):
        with self.assertRaises(TypeError):
            md.split_wiki_links(None)  # type: ignore[arg-type]


class TestFallbackYaml(unittest.TestCase):
    def test_simple_parser_scalars_and_inline_lists(self):
        text = (
            "---\n"
            "title: 测试\n"
            'tags: [a, "b, c"]\n'
            "created: 2026-11-15\n"
            "wiki: true\n"
            "empty: null\n"
            "count: 3\n"
            "ratio: 0.5\n"
            "---\n"
            "body\n"
        )
        with mock.patch.object(md, "_yaml", None):
            data, body = md.parse_frontmatter(text)
        self.assertEqual(
            data,
            {
                "title": "测试",
                "tags": ["a", "b, c"],
                "created": "2026-11-15",
                "wiki": True,
                "empty": None,
                "count": 3,
                "ratio": 0.5,
            },
        )
        self.assertEqual(body, "body\n")

    def test_fallback_render_round_trip(self):
        data = _valid_data()
        data["created"] = "2026-11-15"
        body = "# 标题\n\n正文 [[Other|别名]]\n"
        with mock.patch.object(md, "_yaml", None):
            rendered = md.render_frontmatter(data, body)
            parsed, parsed_body = md.parse_frontmatter(rendered)
        self.assertEqual(parsed, data)
        self.assertEqual(parsed_body, body)

    def test_fallback_rejects_nested_yaml(self):
        with mock.patch.object(md, "_yaml", None):
            with self.assertRaises(md.FrontmatterError):
                md.parse_frontmatter("---\nmeta:\n  nested: 1\n---\n")
            with self.assertRaises(md.FrontmatterError):
                md.render_frontmatter({"meta": {"nested": 1}}, "")

    def test_fallback_rejects_unclosed_inline_list(self):
        with mock.patch.object(md, "_yaml", None):
            with self.assertRaises(md.FrontmatterError):
                md.parse_frontmatter("---\ntags: [a, b\n---\n")


if __name__ == "__main__":
    unittest.main()
