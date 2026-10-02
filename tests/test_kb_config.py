# -*- coding: utf-8 -*-
"""kb_config：库路径解析优先级（LANCEDB_DB_PATH 环境变量 > 默认值）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import config as cfg


class TestResolveDbPath(unittest.TestCase):
    def test_env_overrides_default(self):
        old = os.environ.get("LANCEDB_DB_PATH")
        try:
            os.environ["LANCEDB_DB_PATH"] = r"D:\tmp\env_db"
            self.assertEqual(cfg.resolve_db_path(), r"D:\tmp\env_db")
        finally:
            if old is None:
                os.environ.pop("LANCEDB_DB_PATH", None)
            else:
                os.environ["LANCEDB_DB_PATH"] = old

    def test_default_when_env_unset(self):
        old = os.environ.pop("LANCEDB_DB_PATH", None)
        try:
            self.assertEqual(cfg.resolve_db_path(), cfg.DEFAULT_DB_PATH)
        finally:
            if old is not None:
                os.environ["LANCEDB_DB_PATH"] = old

    def test_flat_config_surface(self):
        # 分区机制已移除：注册表相关函数不应再存在
        for name in ("load_registry", "normalize_project", "list_projects",
                     "source_roots", "guess_project_from_cwd", "resolve_project"):
            self.assertFalse(hasattr(cfg, name), f"cfg.{name} 应已删除")


if __name__ == "__main__":
    unittest.main()
