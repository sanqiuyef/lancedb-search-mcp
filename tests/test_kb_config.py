# -*- coding: utf-8 -*-
"""kb_config：分区规范化、注册表热重载、库路径优先级。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg


class TestNormalizeProject(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_cfg_")
        from tests.kb_test_support import write_registry

        write_registry(self.tmpdir, {
            "BIMbase": {"description": "bim", "source_roots": [os.path.join(self.tmpdir, "bim")]},
            "小论文（北松区）": {"description": "paper", "source_roots": []},
            "global": {"description": "全局"},
        })

    def tearDown(self):
        cfg._kb_registry = None
        cfg._kb_config_stamp = None
        cfg.KB_CONFIG_PATH = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kb-config.json"
        )

    def test_exact_match(self):
        self.assertEqual(cfg.normalize_project("BIMbase"), "BIMbase")

    def test_alias_prefix_stripped(self):
        self.assertEqual(cfg.normalize_project("project-BIMbase"), "BIMbase")
        self.assertEqual(cfg.normalize_project("kb-BIMbase"), "BIMbase")

    def test_case_insensitive(self):
        self.assertEqual(cfg.normalize_project("bimbase"), "BIMbase")

    def test_unknown_returns_empty(self):
        self.assertEqual(cfg.normalize_project("不存在的库"), "")

    def test_empty_returns_empty(self):
        self.assertEqual(cfg.normalize_project(""), "")

    def test_registry_hot_reload(self):
        self.assertEqual(cfg.normalize_project("BIMbase"), "BIMbase")
        from tests.kb_test_support import write_registry

        write_registry(self.tmpdir, {"新分区": {"description": "x", "source_roots": []}})
        self.assertEqual(cfg.normalize_project("新分区"), "新分区")
        self.assertEqual(cfg.normalize_project("BIMbase"), "")

    def test_source_roots(self):
        self.assertEqual(cfg.source_roots("BIMbase"),
                         [os.path.join(self.tmpdir, "bim")])
        self.assertEqual(cfg.source_roots("global"), [])

    def test_list_projects(self):
        self.assertIn("BIMbase", cfg.list_projects())


class TestResolveDbPath(unittest.TestCase):
    def test_env_overrides_registry(self):
        old = os.environ.get("LANCEDB_DB_PATH")
        try:
            os.environ["LANCEDB_DB_PATH"] = r"D:\tmp\env_db"
            cfg._kb_registry = {"db_path": r"D:\tmp\registry_db", "projects": {}}
            cfg._kb_config_stamp = (0.0, 0)
            self.assertEqual(cfg.resolve_db_path(), r"D:\tmp\env_db")
        finally:
            if old is None:
                os.environ.pop("LANCEDB_DB_PATH", None)
            else:
                os.environ["LANCEDB_DB_PATH"] = old
            cfg._kb_registry = None
            cfg._kb_config_stamp = None

    def test_default_when_no_registry_entry(self):
        tmpdir = tempfile.mkdtemp(prefix="kb_nodb_")
        from tests.kb_test_support import write_registry

        write_registry(tmpdir, {"X": {"description": "x"}})  # 无 db_path 字段
        old = os.environ.pop("LANCEDB_DB_PATH", None)
        try:
            self.assertEqual(cfg.resolve_db_path(), cfg.DEFAULT_DB_PATH)
        finally:
            if old is not None:
                os.environ["LANCEDB_DB_PATH"] = old
            cfg._kb_registry = None
            cfg._kb_config_stamp = None


if __name__ == "__main__":
    unittest.main()
