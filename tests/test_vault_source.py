# -*- coding: utf-8 -*-
"""kb.vault_source：外部文件 → vault Markdown、frontmatter 与网页剪藏。"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg  # noqa: E402
from kb import pdf_convert  # noqa: E402
from kb import vault_source  # noqa: E402
from tests.kb_test_support import temp_db_env  # noqa: E402


def fake_convert_pdf(pdf_path, use_cache=True, export_dir=None):
    """模拟 MinerU：在 export_dir 写 <stem>.md + <stem>_images/。"""
    pdf = Path(pdf_path)
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = out_dir / f"{pdf.stem}_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "img-001.png").write_bytes(b"fake-png")
    md_path = out_dir / f"{pdf.stem}.md"
    md_path.write_text(
        f"# {pdf.stem}\n\n正文内容 " + ("可检索" * 30)
        + f"\n\n![fig]({pdf.stem}_images/img-001.png)\n",
        encoding="utf-8",
    )
    return {
        "md_path": str(md_path),
        "exported_md": str(md_path),
        "chars": len(md_path.read_text(encoding="utf-8")),
        "images": 1,
        "formulas": 0,
        "cached": False,
        "method": "auto",
        "lang": "ch",
    }


def fake_convert_pdf_with_markdown(markdown: str):
    """模拟 MinerU：转换产物正文固定为指定 Markdown。"""
    def convert(pdf_path, use_cache=True, export_dir=None):
        pdf = Path(pdf_path)
        out_dir = Path(export_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{pdf.stem}.md"
        md_path.write_text(markdown, encoding="utf-8")
        return {
            "md_path": str(md_path),
            "exported_md": str(md_path),
            "chars": len(markdown),
            "images": 0,
            "formulas": 0,
            "cached": False,
            "method": "auto",
            "lang": "ch",
        }
    return convert


class VaultSourceTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = temp_db_env(self)
        self.vault = Path(self.db_path) / "vault"
        self.external = Path(tempfile.mkdtemp(prefix="kb_vault_src_ext_"))
        self.addCleanup(shutil.rmtree, str(self.external), True)

    def write_external(self, name: str, content: "str | bytes") -> Path:
        path = self.external / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def patch_pdf(self):
        return mock.patch.object(pdf_convert, "convert_pdf", side_effect=fake_convert_pdf)


class TestVaultPaths(VaultSourceTestBase):
    def test_vault_root_env_priority(self):
        old_ldb = os.environ.get("LANCEDB_VAULT_ROOT")
        old_wiki = os.environ.get("WIKI_VAULT_PATH")
        try:
            os.environ["LANCEDB_VAULT_ROOT"] = str(self.vault)
            os.environ["WIKI_VAULT_PATH"] = str(self.external)
            self.assertEqual(vault_source.vault_root(), str(self.vault.resolve()))
            os.environ.pop("LANCEDB_VAULT_ROOT", None)
            self.assertEqual(vault_source.vault_root(), str(self.external.resolve()))
        finally:
            if old_ldb is None:
                os.environ.pop("LANCEDB_VAULT_ROOT", None)
            else:
                os.environ["LANCEDB_VAULT_ROOT"] = old_ldb
            if old_wiki is None:
                os.environ.pop("WIKI_VAULT_PATH", None)
            else:
                os.environ["WIKI_VAULT_PATH"] = old_wiki

    def test_is_under_vault_and_rel_path(self):
        inside = self.vault / "notes" / "a.md"
        outside = self.external / "a.md"
        self.assertTrue(vault_source.is_under_vault(inside))
        self.assertTrue(vault_source.is_under_vault(self.vault))
        self.assertFalse(vault_source.is_under_vault(outside))
        self.assertEqual(
            vault_source.rel_to_vault(inside), os.path.normpath("notes/a.md"))
        with self.assertRaises(ValueError):
            vault_source.rel_to_vault(outside)

    def test_symlink_target_outside_vault_is_not_under_vault(self):
        outside = self.write_external("outside.md", "库外内容。")
        link = self.vault / "link.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            link_dir = self.vault / "linked-dir"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_dir), str(self.external)],
                capture_output=True,
            )
            if result.returncode != 0:
                self.skipTest("当前环境不允许创建符号链接或 junction")
            link = link_dir / "outside.md"
        self.assertFalse(vault_source.is_under_vault(link))
        with self.assertRaises(ValueError):
            vault_source.rel_to_vault(link)

    def test_target_dir_for(self):
        self.assertEqual(
            vault_source.target_dir_for("paper", "x.pdf"),
            self.vault / "08-literature-文献" / "papers-论文全文",
        )
        self.assertEqual(
            vault_source.target_dir_for("", "https://example.com/a"),
            self.vault / "00-inbox-收件箱" / "from-web-网页",
        )
        self.assertEqual(
            vault_source.target_dir_for("web", "https://example.com/a"),
            self.vault / "00-inbox-收件箱" / "from-web-网页",
        )
        self.assertEqual(
            vault_source.target_dir_for("", "x.docx"),
            self.vault / "raw-素材" / "converted-转换",
        )


class TestPreparePdf(VaultSourceTestBase):
    def test_paper_pdf_lands_in_literature_dir(self):
        pdf = self.write_external("论文A.pdf", b"%PDF-1.4 fake")
        with self.patch_pdf():
            prepared = vault_source.prepare_markdown(str(pdf), category_hint="paper")

        md_path = Path(prepared.md_path)
        self.assertTrue(prepared.converted)
        self.assertEqual(prepared.converter, "MinerU")
        self.assertEqual(prepared.category, "paper")
        self.assertEqual(md_path.parent, self.vault / "08-literature-文献" / "papers-论文全文")
        self.assertEqual(
            prepared.source,
            os.path.normpath("08-literature-文献/papers-论文全文/论文A.md"),
        )
        self.assertNotIn(".pdf", prepared.source.lower())
        self.assertTrue((md_path.parent / "论文A_images" / "img-001.png").is_file())

        text = md_path.read_text(encoding="utf-8")
        self.assertIn("generator: lancedb-search", text)
        self.assertIn("tags: [literature]", text)
        self.assertIn("type: literature", text)
        self.assertIn("status: unread", text)
        self.assertIn("summary: 由 pdf 经 MinerU 转换", text)
        self.assertIn(f"source: '{pdf.resolve()}'", text)
        self.assertIn("论文A_images/img-001.png", text)
        # 原始 PDF 仍在 vault 外
        self.assertTrue(pdf.is_file())

    def test_non_paper_pdf_lands_in_convert_dir(self):
        pdf = self.write_external("manual.pdf", b"%PDF-1.4 fake")
        with self.patch_pdf():
            prepared = vault_source.prepare_markdown(str(pdf))

        md_path = Path(prepared.md_path)
        self.assertEqual(md_path.parent, self.vault / "raw-素材" / "converted-转换")
        self.assertEqual(
            prepared.source,
            os.path.normpath("raw-素材/converted-转换/manual.md"),
        )
        text = md_path.read_text(encoding="utf-8")
        self.assertIn("tags: [converted]", text)
        self.assertIn("type: fleeting", text)
        self.assertNotIn("status: unread", text)

    def test_same_source_is_reused_without_reconversion(self):
        pdf = self.write_external("reuse.pdf", b"%PDF-1.4 fake")
        with mock.patch.object(
            pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ) as converter:
            first = vault_source.prepare_markdown(str(pdf))
            second = vault_source.prepare_markdown(str(pdf))

        self.assertEqual(first.md_path, second.md_path)
        self.assertEqual(first.source, second.source)
        self.assertEqual(converter.call_count, 1)

    def test_existing_foreign_target_gets_suffix_and_is_not_overwritten(self):
        target = self.vault / "raw-素材" / "converted-转换" / "same.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        user_text = "---\nsource: 'D:\\other\\same.pdf'\n---\n\n用户自己的笔记\n"
        target.write_text(user_text, encoding="utf-8")

        pdf = self.write_external("same.pdf", b"%PDF-1.4 fake")
        with self.patch_pdf():
            prepared = vault_source.prepare_markdown(str(pdf))

        final = Path(prepared.md_path)
        self.assertNotEqual(final, target)
        self.assertTrue(final.name.startswith("same-"))
        self.assertTrue(final.name.endswith(".md"))
        self.assertEqual(target.read_text(encoding="utf-8"), user_text)
        self.assertIn(f"source: '{pdf.resolve()}'", final.read_text(encoding="utf-8"))
        image_dir = final.parent / f"{final.stem}_images"
        self.assertTrue((image_dir / "img-001.png").is_file())

    def test_collision_suffixed_target_is_reused(self):
        target = self.vault / "raw-素材" / "converted-转换" / "same.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\nsource: 'D:\\other\\same.pdf'\n---\n\n用户笔记\n",
                          encoding="utf-8")
        pdf = self.write_external("same.pdf", b"%PDF-1.4 fake")

        with mock.patch.object(
            pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ) as converter:
            first = vault_source.prepare_markdown(str(pdf))
            second = vault_source.prepare_markdown(str(pdf))

        self.assertEqual(first.md_path, second.md_path)
        self.assertEqual(converter.call_count, 1)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "---\nsource: 'D:\\other\\same.pdf'\n---\n\n用户笔记\n")

    def test_pdf_conversion_failure_is_reported(self):
        pdf = self.write_external("broken.pdf", b"%PDF-1.4 fake")
        with mock.patch.object(
            pdf_convert, "convert_pdf", side_effect=RuntimeError("mineru down")
        ):
            with self.assertRaises(vault_source.VaultSourceError) as ctx:
                vault_source.prepare_markdown(str(pdf))
        self.assertIn("mineru down", str(ctx.exception))

    def test_regenerated_pdf_backs_up_previous_generated_markdown(self):
        pdf = self.write_external("paper.pdf", b"%PDF-1.4 fake")
        with mock.patch.object(
            pdf_convert, "convert_pdf", side_effect=fake_convert_pdf
        ):
            first = vault_source.prepare_markdown(str(pdf))
        target = Path(first.md_path)
        old_body = vault_source.strip_frontmatter(
            target.read_text(encoding="utf-8"))

        future = time.time() + 30
        os.utime(pdf, (future, future))
        with mock.patch.object(
            pdf_convert, "convert_pdf",
            side_effect=fake_convert_pdf_with_markdown(
                "# paper\n\n新版正文 " + ("更新后" * 30)),
        ):
            second = vault_source.prepare_markdown(str(pdf))

        self.assertEqual(second.md_path, first.md_path)
        self.assertTrue(second.regenerated)
        self.assertIn(
            "新版正文",
            target.read_text(encoding="utf-8"),
        )
        backups = sorted(target.parent.glob(f"{target.stem}.bak-*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            vault_source.strip_frontmatter(
                backups[0].read_text(encoding="utf-8")),
            old_body,
        )

    def test_same_source_without_generator_is_treated_as_user_file(self):
        target = self.vault / "raw-素材" / "converted-转换" / "same.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        pdf = self.write_external("same.pdf", b"%PDF-1.4 fake")
        user_text = (
            f"---\nsource: '{pdf.resolve()}'\n---\n\n"
            "用户手写笔记（没有 generator 标记）\n"
        )
        target.write_text(user_text, encoding="utf-8")

        with self.patch_pdf():
            prepared = vault_source.prepare_markdown(str(pdf))

        final = Path(prepared.md_path)
        self.assertNotEqual(final, target)
        self.assertTrue(final.name.startswith("same-"))
        self.assertEqual(target.read_text(encoding="utf-8"), user_text)


class TestPrepareText(VaultSourceTestBase):
    def test_external_markdown_is_copied_into_vault(self):
        source = self.write_external("notes.md", "# 外部笔记\n\n正文。" * 10)
        prepared = vault_source.prepare_markdown(str(source))

        copied = Path(prepared.md_path)
        self.assertFalse(prepared.converted)
        self.assertEqual(prepared.converter, "")
        self.assertTrue(prepared.regenerated)
        self.assertEqual(copied.parent, self.vault / "raw-素材" / "converted-转换")
        digest = vault_source._path_digest(source)
        expected = os.path.normpath(
            f"raw-素材/converted-转换/notes-{digest}.md")
        self.assertEqual(prepared.source, expected)
        self.assertEqual(copied.name, f"notes-{digest}.md")

        text = copied.read_text(encoding="utf-8")
        self.assertIn("generator: lancedb-search", text)
        self.assertIn(f"source: '{source.resolve()}'", text)
        self.assertEqual(
            vault_source.strip_frontmatter(text),
            source.read_text(encoding="utf-8"),
        )
        self.assertTrue(source.is_file())

        again = vault_source.prepare_markdown(str(source))
        self.assertEqual(again.md_path, prepared.md_path)
        self.assertEqual(again.source, prepared.source)
        self.assertFalse(again.regenerated)

    def test_external_text_update_keeps_one_fixed_target_and_backs_up(self):
        source = self.write_external("paper.txt", "第一版内容。" * 30)
        first = vault_source.prepare_markdown(str(source))
        target = Path(first.md_path)
        first_body = vault_source.strip_frontmatter(
            target.read_text(encoding="utf-8"))
        expected = os.path.normpath(
            f"raw-素材/converted-转换/paper-{vault_source._path_digest(source)}.md")
        self.assertEqual(first.source, expected)

        source.write_text("第二版内容。" * 30, encoding="utf-8")
        second = vault_source.prepare_markdown(str(source))

        self.assertEqual(second.md_path, first.md_path)
        self.assertEqual(second.source, first.source)
        self.assertTrue(second.regenerated)
        self.assertEqual(
            vault_source.strip_frontmatter(
                target.read_text(encoding="utf-8")),
            "第二版内容。" * 30,
        )
        backups = sorted(target.parent.glob(f"{target.stem}.bak-*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            vault_source.strip_frontmatter(
                backups[0].read_text(encoding="utf-8")),
            first_body,
        )

    def test_user_modified_fixed_target_is_not_overwritten(self):
        source = self.write_external("fixed.md", "可更新内容。" * 30)
        first = vault_source.prepare_markdown(str(source))
        target = Path(first.md_path)
        target.write_text("# 用户改过的笔记\n\n不要覆盖。", encoding="utf-8")

        source.write_text("又一段新内容。" * 30, encoding="utf-8")
        with self.assertRaises(vault_source.VaultSourceError):
            vault_source.prepare_markdown(str(source))
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "# 用户改过的笔记\n\n不要覆盖。",
        )

    def test_category_hint_is_carried_on_prepared_source(self):
        source = self.write_external("topic.md", "论文内容。" * 30)
        prepared = vault_source.prepare_markdown(
            str(source), category_hint="paper")
        self.assertEqual(prepared.category, "paper")
        self.assertEqual(
            Path(prepared.md_path).parent,
            self.vault / "08-literature-文献" / "papers-论文全文",
        )
        self.assertIn(
            "type: literature",
            Path(prepared.md_path).read_text(encoding="utf-8"),
        )

    def test_vault_markdown_is_used_in_place(self):
        path = self.vault / "notes" / "inside.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 库内笔记\n\n正文。" * 10, encoding="utf-8")

        prepared = vault_source.prepare_markdown(str(path))

        self.assertEqual(prepared.md_path, str(path.resolve()))
        self.assertEqual(
            prepared.source, os.path.normpath("notes/inside.md"))
        self.assertFalse(prepared.converted)
        self.assertFalse(prepared.regenerated)
        self.assertEqual(prepared.original_path, str(path.resolve()))

    def test_doc_is_rejected_with_docx_hint(self):
        doc = self.write_external("legacy.doc", b"binary")
        with self.assertRaises(vault_source.VaultSourceError) as ctx:
            vault_source.prepare_markdown(str(doc))
        self.assertIn(".doc", str(ctx.exception))
        self.assertIn(".docx", str(ctx.exception))


class TestPrepareOffice(VaultSourceTestBase):
    def test_docx_uses_docling_first(self):
        docx = self.write_external("report.docx", b"fake-docx")
        with mock.patch.object(
            vault_source, "_docling_markdown", return_value="# Report\n\nDocling body"
        ), mock.patch.object(vault_source, "_markitdown_text") as markitdown:
            prepared = vault_source.prepare_markdown(str(docx))

        markitdown.assert_not_called()
        self.assertTrue(prepared.converted)
        self.assertEqual(prepared.converter, "docling")
        md_path = Path(prepared.md_path)
        self.assertEqual(md_path.parent, self.vault / "raw-素材" / "converted-转换")
        text = md_path.read_text(encoding="utf-8")
        self.assertIn("tags: [converted]", text)
        self.assertIn("summary: 由 docx 经 docling 转换", text)
        self.assertIn("Docling body", text)

    def test_docx_falls_back_to_markitdown(self):
        docx = self.write_external("slides.pptx", b"fake-pptx")
        with mock.patch.object(
            vault_source, "_docling_markdown", side_effect=RuntimeError("no docling")
        ), mock.patch.object(
            vault_source, "_markitdown_text", return_value="# Slides\n\nMarkitdown body"
        ):
            prepared = vault_source.prepare_markdown(str(docx))

        self.assertEqual(prepared.converter, "markitdown")
        text = Path(prepared.md_path).read_text(encoding="utf-8")
        self.assertIn("summary: 由 pptx 经 markitdown 转换", text)
        self.assertIn("Markitdown body", text)

    def test_suffixed_office_target_is_reused_before_conversion(self):
        target = self.vault / "raw-素材" / "converted-转换" / "report.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("用户自己的 report 笔记。", encoding="utf-8")
        docx = self.write_external("report.docx", b"fake-docx")

        with mock.patch.object(
            vault_source, "_docling_markdown",
            return_value="# Report\n\nDocling body",
        ):
            first = vault_source.prepare_markdown(str(docx))

        with mock.patch.object(
            vault_source, "_docling_markdown",
            side_effect=AssertionError("不应重新转换"),
        ) as docling, mock.patch.object(
            vault_source, "_markitdown_text",
            side_effect=AssertionError("不应重新转换"),
        ):
            second = vault_source.prepare_markdown(str(docx))

        docling.assert_not_called()
        self.assertEqual(second.md_path, first.md_path)
        self.assertEqual(second.source, first.source)
        self.assertFalse(second.regenerated)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "用户自己的 report 笔记。")

    def test_both_converters_failing_raises(self):
        xlsx = self.write_external("table.xlsx", b"fake-xlsx")
        with mock.patch.object(
            vault_source, "_docling_markdown", side_effect=RuntimeError("docling bad")
        ), mock.patch.object(
            vault_source, "_markitdown_text", side_effect=RuntimeError("markitdown bad")
        ):
            with self.assertRaises(vault_source.VaultSourceError) as ctx:
                vault_source.prepare_markdown(str(xlsx))
        message = str(ctx.exception)
        self.assertIn("docling bad", message)
        self.assertIn("markitdown bad", message)


class TestFrontmatter(VaultSourceTestBase):
    def test_strip_frontmatter(self):
        self.assertEqual(
            vault_source.strip_frontmatter("---\ntitle: x\n---\n\n正文"),
            "正文",
        )
        self.assertEqual(
            vault_source.strip_frontmatter("\ufeff---\na: b\n---\n正文"),
            "正文",
        )
        self.assertEqual(vault_source.strip_frontmatter("---\n---\n正文"), "正文")
        self.assertEqual(vault_source.strip_frontmatter("正文"), "正文")
        self.assertEqual(vault_source.strip_frontmatter(""), "")
        unclosed = "---\ntitle: x\n正文"
        self.assertEqual(vault_source.strip_frontmatter(unclosed), unclosed)

    def test_strip_frontmatter_keeps_non_yaml_horizontal_rule(self):
        # 章节标题不是 YAML 键值：分隔线属于正文，不能把“第一章”删掉。
        text = "---\n第一章\n---\n正文"
        self.assertEqual(vault_source.strip_frontmatter(text), text)
        text = "---\n第一章\n第二章\n---\n正文"
        self.assertEqual(vault_source.strip_frontmatter(text), text)

    def test_frontmatter_source_round_trip(self):
        original = r"D:\资料\O'Brien\论文.pdf"
        front = vault_source._render_frontmatter(
            "paper", original, ".pdf", "MinerU", created="2026-11-15")
        self.assertIn("source: 'D:\\资料\\O''Brien\\论文.pdf'", front)
        self.assertIn("generator: lancedb-search", front)
        text = front + "\n正文"
        self.assertEqual(vault_source._frontmatter_source(text), original)


class TestSaveWebMarkdown(VaultSourceTestBase):
    def test_web_clip_path_and_frontmatter(self):
        url = "https://Example.com/a?utm_source=x&id=1#frag"
        result = vault_source.save_web_markdown(
            url, "A/B: 标题?", "# 正文\n\n网页内容")
        path = Path(result)

        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.vault / "00-inbox-收件箱" / "from-web-网页")
        canonical = "https://example.com/a?id=1"
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]
        self.assertTrue(path.name.endswith(f"-{digest}.md"))
        self.assertNotIn("/", path.name)
        self.assertNotIn(":", path.name)

        text = path.read_text(encoding="utf-8")
        self.assertIn("tags: [web-clip]", text)
        self.assertIn("type: fleeting", text)
        self.assertIn(f"source: '{canonical}'", text)
        self.assertIn(f"url: '{url}'", text)
        self.assertIn("clipped: '", text)
        self.assertIn("网页内容", text)

        again = vault_source.save_web_markdown(url, "A/B: 标题?", "# 正文\n\n新内容")
        self.assertEqual(again, path)
        self.assertIn("新内容", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
