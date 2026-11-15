# -*- coding: utf-8 -*-
"""WikiConfig：默认开关、env 覆盖、绝对路径与 vault 边界。"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import constants as C
from kb.wiki.config import (
    WikiConfig,
    WikiConfigError,
    WikiDisabledError,
    WikiPathError,
    ensure_within,
)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


class TestWikiConfigDefaults(unittest.TestCase):
    def test_defaults_are_disabled_with_absolute_paths(self):
        cfg = WikiConfig.from_env({})
        self.assertFalse(cfg.enabled)
        self.assertTrue(os.path.isabs(cfg.vault_path))
        self.assertTrue(os.path.isabs(cfg.page_root))
        self.assertTrue(os.path.isabs(cfg.db_path))
        self.assertEqual(
            _norm(cfg.vault_path), _norm(os.path.realpath(C.DEFAULT_VAULT_PATH))
        )
        self.assertEqual(
            _norm(cfg.page_root),
            _norm(os.path.join(cfg.vault_path, C.DEFAULT_PAGE_ROOT)),
        )
        self.assertEqual(
            _norm(cfg.db_path),
            _norm(os.path.join(cfg.vault_path, ".wiki", "wiki.sqlite3")),
        )
        self.assertEqual(cfg.page_index_table, "wiki_pages")
        self.assertFalse(cfg.auto_publish)
        self.assertTrue(cfg.require_evidence)
        self.assertAlmostEqual(cfg.min_entity_confidence, 0.90)
        self.assertAlmostEqual(cfg.min_relation_confidence, 0.90)
        self.assertEqual(cfg.max_auto_page_updates, 0)
        self.assertEqual(cfg.db_busy_timeout_ms, 5000)
        self.assertTrue(cfg.git_commit)

    def test_vault_env_alone_does_not_enable_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WikiConfig.from_env({"WIKI_VAULT_PATH": tmp})
        self.assertFalse(cfg.enabled)


class TestWikiConfigEnvOverrides(unittest.TestCase):
    def test_all_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            db_path = os.path.join(tmp, "custom", "wiki.sqlite3")
            env = {
                "WIKI_ENABLED": "1",
                "WIKI_VAULT_PATH": vault,
                "WIKI_PAGE_ROOT": "wiki-test",
                "WIKI_DB_PATH": db_path,
                "WIKI_PAGE_INDEX_TABLE": "wiki_pages_test",
                "WIKI_AUTO_PUBLISH": "1",
                "WIKI_REQUIRE_EVIDENCE": "0",
                "WIKI_MIN_ENTITY_CONFIDENCE": "0.75",
                "WIKI_MIN_RELATION_CONFIDENCE": "0.80",
                "WIKI_MAX_AUTO_PAGE_UPDATES": "3",
                "WIKI_DB_BUSY_TIMEOUT_MS": "1234",
                "WIKI_GIT_COMMIT": "0",
            }
            cfg = WikiConfig.from_env(env)

        self.assertTrue(cfg.enabled)
        self.assertEqual(_norm(cfg.vault_path), _norm(os.path.realpath(vault)))
        self.assertEqual(
            _norm(cfg.page_root),
            _norm(os.path.join(cfg.vault_path, "wiki-test")),
        )
        self.assertEqual(_norm(cfg.db_path), _norm(os.path.realpath(db_path)))
        self.assertEqual(cfg.page_index_table, "wiki_pages_test")
        self.assertTrue(cfg.auto_publish)
        self.assertFalse(cfg.require_evidence)
        self.assertAlmostEqual(cfg.min_entity_confidence, 0.75)
        self.assertAlmostEqual(cfg.min_relation_confidence, 0.80)
        self.assertEqual(cfg.max_auto_page_updates, 3)
        self.assertEqual(cfg.db_busy_timeout_ms, 1234)
        self.assertFalse(cfg.git_commit)

    def test_relative_env_paths_become_absolute(self):
        cfg = WikiConfig.from_env(
            {
                "WIKI_VAULT_PATH": os.path.join("relative-vault"),
                "WIKI_DB_PATH": os.path.join("relative-db", "wiki.sqlite3"),
            }
        )
        self.assertTrue(os.path.isabs(cfg.vault_path))
        self.assertTrue(os.path.isabs(cfg.db_path))
        self.assertEqual(
            _norm(cfg.db_path),
            _norm(os.path.realpath(os.path.join("relative-db", "wiki.sqlite3"))),
        )

    def test_invalid_boolean_and_confidence_are_rejected(self):
        with self.assertRaises(WikiConfigError):
            WikiConfig.from_env({"WIKI_ENABLED": "maybe"})
        with self.assertRaises(WikiConfigError):
            WikiConfig.from_env({"WIKI_MIN_ENTITY_CONFIDENCE": "1.5"})
        with self.assertRaises(WikiConfigError):
            WikiConfig.from_env({"WIKI_DB_BUSY_TIMEOUT_MS": "not-an-int"})


class TestVaultBoundary(unittest.TestCase):
    def test_candidate_inside_root_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "vault")
            candidate = os.path.join(root, "wiki-维基", "concepts-概念", "a.md")
            resolved = ensure_within(root, candidate)
        self.assertEqual(_norm(resolved), _norm(candidate))
        self.assertTrue(os.path.isabs(resolved))

    def test_root_itself_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_norm(ensure_within(tmp, tmp)), _norm(tmp))

    def test_relative_candidate_is_resolved_against_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = ensure_within(tmp, os.path.join("wiki-维基", "a.md"))
            self.assertEqual(
                _norm(resolved), _norm(os.path.join(tmp, "wiki-维基", "a.md"))
            )

    def test_parent_segment_rejected_even_if_it_resolves_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "vault")
            candidate = os.path.join(root, "sub", "..", "inside.md")
            with self.assertRaises(WikiPathError):
                ensure_within(root, candidate)

    def test_escape_via_parent_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "vault")
            candidate = os.path.join(root, "..", "outside.md")
            with self.assertRaises(WikiPathError):
                ensure_within(root, candidate)

    def test_page_root_parent_segment_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(WikiPathError):
                WikiConfig.from_env(
                    {
                        "WIKI_VAULT_PATH": tmp,
                        "WIKI_PAGE_ROOT": os.path.join("..", "outside"),
                    }
                )

    @unittest.skipUnless(os.name == "nt", "跨盘校验仅在 Windows 上适用")
    def test_cross_drive_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_drive = os.path.splitdrive(os.path.abspath(tmp))[0].upper()
            other_drive = "Z:" if root_drive != "Z:" else "Y:"
            candidate = os.path.join(other_drive + os.sep, "outside", "a.md")
            with self.assertRaises(WikiPathError):
                ensure_within(tmp, candidate)

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "vault")
            outside = os.path.join(tmp, "outside")
            os.makedirs(root)
            os.makedirs(outside)
            link = os.path.join(root, "link")
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                if os.name != "nt":
                    self.skipTest("当前环境不允许创建目录符号链接")
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", link, outside],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    self.skipTest("当前环境不允许创建符号链接或 junction")
            with self.assertRaises(WikiPathError):
                ensure_within(root, os.path.join(link, "escaped.md"))


class TestSubdirsAndEnable(unittest.TestCase):
    def test_page_subdir_mapping(self):
        cfg = WikiConfig.from_env({})
        self.assertEqual(cfg.page_subdir("concept"), "concepts-概念")
        self.assertEqual(cfg.page_subdir("entity"), "entities-实体")
        self.assertEqual(cfg.page_subdir("topic"), "topics-主题")
        with self.assertRaises(ValueError):
            cfg.page_subdir("unknown")

    def test_page_type_dir_is_inside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WikiConfig.from_env({"WIKI_VAULT_PATH": tmp})
            for page_type, subdir in C.PAGE_TYPE_SUBDIRS.items():
                path = cfg.page_type_dir(page_type)
                self.assertTrue(os.path.isabs(path))
                self.assertEqual(os.path.basename(path), subdir)
                ensure_within(cfg.vault_path, path)

    def test_require_enabled_raises_when_disabled(self):
        cfg = WikiConfig.from_env({})
        with self.assertRaises(WikiDisabledError) as ctx:
            cfg.require_enabled()
        self.assertIn("WIKI_ENABLED", str(ctx.exception))

    def test_require_enabled_passes_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WikiConfig.from_env(
                {"WIKI_ENABLED": "1", "WIKI_VAULT_PATH": tmp}
            )
        self.assertIsNone(cfg.require_enabled())


if __name__ == "__main__":
    unittest.main()
