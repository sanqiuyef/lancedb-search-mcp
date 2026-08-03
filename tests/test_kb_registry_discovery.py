# -*- coding: utf-8 -*-
"""针对复盘报告中定位的 server.py 知识库发现/注册机制问题的回归测试。

覆盖：
- kb-config.json 注册表热重载（改配置无需重启进程）
- _resolve_db_path 对"尚未创建知识库目录"的项目/路径的解析
- _detect_db_path 在 REASONIX_WORKSPACE 存在时的项目库优先（不再兜底 global）
- get_db() 对不存在的知识库目录的惰性创建
"""

import json
import os
import shutil
import tempfile
import unittest

import server


def _write_config(path, bases, default="global"):
    payload = {"knowledge_bases": bases, "default": default}
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class KbDiscoveryTest(unittest.TestCase):
    """注册表热重载 + 路径解析 + 自动检测的回归测试"""

    def setUp(self):
        # 保存环境变量与模块全局状态，测试结束后恢复
        self._saved_environ = {k: v for k, v in os.environ.items()}
        self._saved_attrs = {
            "_kb_registry": server._kb_registry,
            "_kb_aliases": server._kb_aliases,
            "_kb_config_stamp": server._kb_config_stamp,
            "_db": server._db,
            "_connected_path": server._connected_path,
            "DB_PATH": server.DB_PATH,
            "_current_kb_name": server._current_kb_name,
        }
        for key in ("LANCEDB_DB_PATH", "REASONIX_WORKSPACE", "REASONIX_CURRENT_PROJECT"):
            os.environ.pop(key, None)
        # 用临时 kb-config.json 隔离，避免依赖真实注册表
        self._tmp = tempfile.mkdtemp(prefix="kb-registry-test-")
        self._config_path = os.path.join(self._tmp, "kb-config.json")
        self._old_config_path = server.KB_CONFIG_PATH
        server.KB_CONFIG_PATH = self._config_path
        _write_config(
            self._config_path,
            {"global": {"path": os.path.join(self._tmp, "global-kb")}},
        )

    def tearDown(self):
        server.KB_CONFIG_PATH = self._old_config_path
        for key, value in self._saved_attrs.items():
            setattr(server, key, value)
        os.environ.clear()
        os.environ.update(self._saved_environ)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── 注册表热重载 ──

    def test_load_registry_hot_reload_after_config_change(self):
        registry1 = server._load_registry()
        self.assertIn("global", registry1)
        self.assertNotIn("project-demo", registry1)

        # 不重启进程，直接修改配置文件（内容变化 → mtime/size 变化）
        _write_config(
            self._config_path,
            {
                "global": {"path": os.path.join(self._tmp, "global-kb")},
                "project-demo": {"path": os.path.join(self._tmp, "demo-kb")},
            },
        )
        registry2 = server._load_registry()
        self.assertIn("project-demo", registry2)
        self.assertEqual(
            os.path.join(self._tmp, "demo-kb"),
            registry2["project-demo"]["path"],
        )

    def test_load_registry_caches_when_unchanged(self):
        registry1 = server._load_registry()
        registry2 = server._load_registry()
        self.assertIs(registry1, registry2)

    # ── _resolve_db_path 解析 ──

    def test_resolve_db_path_registry_name(self):
        self.assertEqual(
            os.path.join(self._tmp, "global-kb"),
            server._resolve_db_path("global"),
        )

    def test_resolve_db_path_existing_dir(self):
        self.assertEqual(self._tmp, server._resolve_db_path(self._tmp))

    def test_resolve_db_path_not_created_knowledge_dir(self):
        # 父目录存在、子目录形似知识库目录（尚未创建）→ 应返回该路径
        pending = os.path.join(self._tmp, "knowledge")
        self.assertEqual(pending, server._resolve_db_path(pending))

    def test_resolve_db_path_missing_legacy_dir_not_resolved(self):
        # 未创建的 lancedb_data 路径不应被当作有效知识库路径（需已含 my_docs.lance；
        # 已存在的空目录走原有 isdir 显式路径语义，不属于本放宽分支）
        legacy = os.path.join(self._tmp, "lancedb_data")
        self.assertIsNone(server._resolve_db_path(legacy))
        # 一旦其中建了 my_docs.lance，就应解析成功
        os.makedirs(os.path.join(legacy, "my_docs.lance"))
        self.assertEqual(legacy, server._resolve_db_path(legacy))

    def test_resolve_db_path_unknown_project_none(self):
        self.assertIsNone(server._resolve_db_path("no-such-project-xyz"))

    @unittest.skipUnless(
        os.path.isdir(r"D:\cherry-workplace\comfy-playground"),
        "需要本机 D:\\cherry-workplace\\comfy-playground 项目目录",
    )
    def test_resolve_db_path_project_root_without_kb_dir(self):
        # 项目根存在但 .reasonix\\knowledge 尚未创建 → 仍解析到标准知识库路径
        expected = os.path.join(
            r"D:\cherry-workplace\comfy-playground", ".reasonix", "knowledge"
        )
        self.assertEqual(expected, server._resolve_db_path("comfy-playground"))

    # ── _detect_db_path 自动检测 ──

    def test_detect_db_path_workspace_without_kb_dir(self):
        # REASONIX_WORKSPACE 指向存在但从未建库的项目 → 返回项目知识库路径（而非 global）
        os.environ["REASONIX_WORKSPACE"] = self._tmp
        self.assertEqual(
            os.path.join(self._tmp, ".reasonix", "knowledge"),
            server._detect_db_path(),
        )

    def test_detect_db_path_workspace_prefers_existing_legacy(self):
        legacy = os.path.join(self._tmp, "lancedb_data", "my_docs.lance")
        os.makedirs(legacy)
        os.environ["REASONIX_WORKSPACE"] = self._tmp
        self.assertEqual(
            os.path.join(self._tmp, "lancedb_data"),
            server._detect_db_path(),
        )

    # ── get_db 惰性创建 ──

    def test_get_db_creates_missing_kb_dir(self):
        pending = os.path.join(self._tmp, "knowledge")
        os.environ["LANCEDB_DB_PATH"] = pending
        db = server.get_db()
        self.assertIsNotNone(db)
        self.assertTrue(os.path.isdir(pending))
        self.assertEqual(pending, server._connected_path)


if __name__ == "__main__":
    unittest.main()
