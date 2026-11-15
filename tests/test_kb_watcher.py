# -*- coding: utf-8 -*-
"""kb_watcher：watchdog 事件处理与多目录监听。

测试直接调用 handler 方法并伪造 SimpleNamespace 事件，不启动真实文件系统
事件循环，也不访问 LanceDB / vault；ingest 的更新与删除均用 monkeypatch 替身。
"""

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import watcher as kb_watcher


def _file_event(src_path="", dest_path="", is_directory=False):
    """伪造 watchdog 6.x 事件：只有 is_directory，没有已移除的 is_dir。"""
    return SimpleNamespace(
        path=src_path or dest_path,
        src_path=src_path,
        dest_path=dest_path,
        is_directory=is_directory,
    )


def _temp_dir(testcase):
    td = tempfile.TemporaryDirectory(prefix="kb_watcher_test_")
    testcase.addCleanup(td.cleanup)
    return td.name


def _write(path, text="这是足够长的测试内容，用于通过 watcher 的文件检查。\n"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestEventHandlers(unittest.TestCase):
    def setUp(self):
        self.dir = _temp_dir(self)
        self.path = os.path.join(self.dir, "note.md")
        _write(self.path)
        self.handler = kb_watcher._AutoIndexHandler()

    def test_created_event_reindexes_without_is_dir_attribute(self):
        event = _file_event(src_path=self.path)  # 无 is_dir 属性
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="✅ 文档已更新!"
        ) as update:
            self.handler.on_created(event)
        update.assert_called_once_with(self.path)

    def test_modified_event_reindexes_without_is_dir_attribute(self):
        event = _file_event(src_path=self.path)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="✅ 文档已更新!"
        ) as update:
            self.handler.on_modified(event)
        update.assert_called_once_with(self.path)

    def test_directory_events_are_ignored(self):
        event = _file_event(src_path=self.dir, dest_path=self.path, is_directory=True)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "delete_documents", return_value="ok"
        ) as delete:
            self.handler.on_created(event)
            self.handler.on_modified(event)
            self.handler.on_moved(event)
            self.handler.on_deleted(event)
        update.assert_not_called()
        delete.assert_not_called()

    def test_created_and_modified_share_debounce_per_path(self):
        other = os.path.join(self.dir, "other.md")
        _write(other)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update:
            self.handler.on_created(_file_event(src_path=self.path))
            self.handler.on_modified(_file_event(src_path=self.path))
            self.handler.on_created(_file_event(src_path=self.path))
            self.assertEqual(update.call_count, 1)
            self.assertIn(self.path, self.handler._debounce)

            # 另一个路径不受同一路径防抖影响
            self.handler.on_created(_file_event(src_path=other))
            self.assertEqual(update.call_count, 2)

    def test_debounce_state_directly_suppresses_event(self):
        self.handler._debounce[self.path] = time.time()
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update:
            self.handler.on_created(_file_event(src_path=self.path))
        update.assert_not_called()

    def test_moved_deletes_old_source_then_reindexes_dest(self):
        src = os.path.join(self.dir, "old.md")
        dest = os.path.join(self.dir, "new.md")
        _write(dest)
        calls = []

        def delete_side_effect(**kwargs):
            calls.append(("delete", kwargs.get("source_pattern")))
            return "ok"

        def update_side_effect(path):
            calls.append(("update", path))
            return "ok"

        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", side_effect=update_side_effect
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "delete_documents", side_effect=delete_side_effect
        ) as delete:
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))

        self.assertEqual(calls, [("delete", "old.md"), ("update", dest)])
        delete.assert_called_once_with(source_pattern="old.md")
        update.assert_called_once_with(dest)

    def test_moved_is_debounced_by_dest_path(self):
        src = os.path.join(self.dir, "old.md")
        dest = os.path.join(self.dir, "new.md")
        _write(dest)
        event = _file_event(src_path=src, dest_path=dest)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "delete_documents", return_value="ok"
        ) as delete:
            self.handler.on_moved(event)
            self.handler.on_moved(event)
        # 整个 move 操作按 dest_path 防抖：删旧 + 重建都只执行一次
        delete.assert_called_once_with(source_pattern="old.md")
        update.assert_called_once_with(dest)

    def test_deleted_immediately_removes_source_even_if_debounced(self):
        src = os.path.join(self.dir, "gone.md")
        # 即使该路径刚被防抖记录，删除事件也必须立即处理
        self.handler._debounce[src] = time.time()
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_documents", return_value="ok"
        ) as delete:
            self.handler.on_deleted(_file_event(src_path=src))
        delete.assert_called_once_with(source_pattern="gone.md")

    def test_handler_swallows_ingest_exceptions(self):
        src = os.path.join(self.dir, "old.md")
        dest = os.path.join(self.dir, "new.md")
        _write(dest)
        with mock.patch.object(
            kb_watcher.kb_ingest,
            "update_document",
            side_effect=RuntimeError("boom"),
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest,
            "delete_documents",
            side_effect=RuntimeError("boom"),
        ) as delete:
            # 任何 ingest 异常都不能从 handler 冒出（否则 watchdog 线程退出）
            self.handler.on_created(_file_event(src_path=self.path))
            self.handler.on_modified(_file_event(src_path=self.path))
            self.handler.on_deleted(_file_event(src_path=src))
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))
        self.assertGreaterEqual(update.call_count, 2)
        self.assertGreaterEqual(delete.call_count, 2)


class _FakeObserver:
    """替代 watchdog Observer，避免测试启动真实监听线程。"""

    instances = []

    def __init__(self):
        self.scheduled = []
        self.started = False
        self.stopped = False
        self.joined = None
        _FakeObserver.instances.append(self)

    def schedule(self, handler, path, recursive=False):
        self.scheduled.append((handler, path, recursive))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = timeout


class TestWatcherLifecycle(unittest.TestCase):
    def setUp(self):
        _FakeObserver.instances.clear()
        self._saved_observers = dict(kb_watcher._OBSERVERS)
        kb_watcher._OBSERVERS.clear()
        self.addCleanup(self._restore_state)
        patcher = mock.patch("watchdog.observers.Observer", _FakeObserver)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir1 = _temp_dir(self)
        self.dir2 = _temp_dir(self)

    def _restore_state(self):
        kb_watcher._OBSERVERS.clear()
        kb_watcher._OBSERVERS.update(self._saved_observers)

    def test_start_same_dir_is_idempotent(self):
        first = kb_watcher.start_watcher(self.dir1)
        second = kb_watcher.start_watcher(self.dir1)

        self.assertIn("文件监听已启动", first)
        self.assertIn("已在运行", second)
        self.assertEqual(len(kb_watcher._OBSERVERS), 1)
        self.assertEqual(len(_FakeObserver.instances), 1)

        observer = _FakeObserver.instances[0]
        self.assertTrue(observer.started)
        self.assertEqual(len(observer.scheduled), 1)
        handler, path, recursive = observer.scheduled[0]
        self.assertIsInstance(handler, kb_watcher._AutoIndexHandler)
        self.assertEqual(path, os.path.abspath(self.dir1))
        self.assertTrue(recursive)

    def test_two_dirs_start_and_stop_individually(self):
        kb_watcher.start_watcher(self.dir1)
        kb_watcher.start_watcher(self.dir2)

        self.assertEqual(len(kb_watcher._OBSERVERS), 2)
        self.assertEqual(len(_FakeObserver.instances), 2)
        running, dirs = kb_watcher.watcher_running()
        self.assertTrue(running)
        self.assertIn(os.path.abspath(self.dir1), dirs)
        self.assertIn(os.path.abspath(self.dir2), dirs)

        message = kb_watcher.stop_watcher(self.dir1)
        self.assertIn("已停止", message)
        self.assertEqual(len(kb_watcher._OBSERVERS), 1)
        self.assertTrue(_FakeObserver.instances[0].stopped)
        self.assertFalse(_FakeObserver.instances[1].stopped)

        kb_watcher.stop_watcher()
        self.assertEqual(len(kb_watcher._OBSERVERS), 0)
        self.assertTrue(_FakeObserver.instances[1].stopped)
        self.assertIn("未在运行", kb_watcher.stop_watcher())

    def test_default_watch_dir_is_cwd_and_reported(self):
        message = kb_watcher.start_watcher()
        self.assertIn("文件监听已启动", message)
        self.assertIn(os.path.abspath(os.getcwd()), message)
        self.assertIn("默认监听当前工作目录", message)
        self.assertEqual(len(_FakeObserver.instances), 1)

    def test_missing_dir_is_rejected_without_starting_observer(self):
        missing = os.path.join(self.dir1, "does-not-exist")
        message = kb_watcher.start_watcher(missing)
        self.assertIn("目录不存在", message)
        self.assertEqual(len(_FakeObserver.instances), 0)
        self.assertEqual(len(kb_watcher._OBSERVERS), 0)


if __name__ == "__main__":
    unittest.main()
