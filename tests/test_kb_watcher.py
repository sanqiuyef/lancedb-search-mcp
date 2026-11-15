# -*- coding: utf-8 -*-
"""kb_watcher：watchdog 事件处理、尾沿防抖、批量删除与多目录监听。

测试直接调用 handler 方法并伪造 SimpleNamespace 事件，不启动真实文件系统
事件循环，也不访问 LanceDB / vault；ingest 的更新与删除均用 monkeypatch 替身。
防抖计时器通过注入模块常量、手动 Timer 或 flush_pending() 控制，不真等 5 秒。
"""

import contextlib
import io
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _relative_resolver(root):
    """模拟库中 source 为监听根下相对路径的解析器（不访问真实库）。"""
    def resolve(path):
        try:
            return os.path.relpath(path, root), ""
        except ValueError:  # Windows 跨盘
            return "", ""
    return resolve


def _patch_resolver(testcase, resolver):
    """替换 ingest.resolve_writable_source，watcher 单元测试不触碰 LanceDB。"""
    patcher = mock.patch.object(
        kb_watcher.kb_ingest, "resolve_writable_source", side_effect=resolver)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return patcher


class _ManualTimer:
    """可手动触发的 Timer 替身，用于确定性地验证尾沿防抖。"""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.started = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args, **self.kwargs)


class TestEventHandlers(unittest.TestCase):
    def setUp(self):
        self.dir = _temp_dir(self)
        # 文件放在监听根的子目录里：source 含分隔符，属可自动管理的相对路径
        self.path = os.path.join(self.dir, "notes", "note.md")
        _write(self.path)
        _patch_resolver(self, _relative_resolver(self.dir))
        self.handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(self.handler.cancel_pending)

    def _rel(self, path):
        return os.path.relpath(path, self.dir)

    def test_created_event_reindexes_without_is_dir_attribute(self):
        event = _file_event(src_path=self.path)  # 无 is_dir 属性
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="✅ 文档已更新!"
        ) as update:
            self.handler.on_created(event)
            self.handler.flush_pending()
        update.assert_called_once_with(self.path, source=self._rel(self.path))

    def test_modified_event_reindexes_without_is_dir_attribute(self):
        event = _file_event(src_path=self.path)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="✅ 文档已更新!"
        ) as update:
            self.handler.on_modified(event)
            self.handler.flush_pending()
        update.assert_called_once_with(self.path, source=self._rel(self.path))

    def test_directory_events_are_ignored(self):
        event = _file_event(src_path=self.dir, dest_path=self.path, is_directory=True)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=0
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_created(event)
            self.handler.on_modified(event)
            self.handler.on_moved(event)
            self.handler.on_deleted(event)
            self.handler.flush_pending()
        update.assert_not_called()
        delete.assert_not_called()
        add.assert_not_called()
        self.assertEqual(self.handler._TIMERS, {})
        self.assertEqual(self.handler._pending_deletes, set())

    def test_created_and_modified_share_debounce_per_path(self):
        other = os.path.join(self.dir, "notes", "other.md")
        _write(other)
        key = kb_watcher._normalize_path(self.path)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update:
            self.handler.on_created(_file_event(src_path=self.path))
            self.handler.on_modified(_file_event(src_path=self.path))
            self.handler.on_created(_file_event(src_path=self.path))
            self.assertIn(key, self.handler._TIMERS)
            update.assert_not_called()

            self.handler.flush_pending()
            self.assertEqual(update.call_count, 1)
            self.assertNotIn(key, self.handler._TIMERS)

            # 另一个路径不受同一路径防抖影响
            self.handler.on_created(_file_event(src_path=other))
            self.assertEqual(update.call_count, 1)
            self.handler.flush_pending()
            self.assertEqual(update.call_count, 2)

    def test_debounce_state_directly_suppresses_event(self):
        key = kb_watcher._normalize_path(self.path)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update:
            self.handler.on_created(_file_event(src_path=self.path))
            # 事件只登记待执行计时器，不在事件线程里立即执行
            self.assertIn(key, self.handler._TIMERS)
            update.assert_not_called()
            self.handler.flush_pending()
        update.assert_called_once_with(self.path, source=self._rel(self.path))

    def test_moved_deletes_old_source_then_indexes_dest(self):
        src = os.path.join(self.dir, "notes", "old.md")
        dest = os.path.join(self.dir, "notes", "new.md")
        _write(dest)
        calls = []

        def delete_side_effect(source, **kwargs):
            calls.append(("delete", source))
            return 1

        def add_side_effect(path, **kwargs):
            calls.append(("add", path))
            return "ok"

        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact",
            side_effect=delete_side_effect,
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document",
            side_effect=add_side_effect,
        ) as add:
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))
            self.handler.flush_pending()

        self.assertEqual(calls, [("delete", self._rel(src)), ("add", dest)])
        delete.assert_called_once_with(self._rel(src))
        add.assert_called_once_with(dest, scan_dir=self.handler._watch_root_path)

    def test_moved_is_debounced_by_dest_path(self):
        src = os.path.join(self.dir, "notes", "old.md")
        dest = os.path.join(self.dir, "notes", "new.md")
        _write(dest)
        event = _file_event(src_path=src, dest_path=dest)
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_moved(event)
            self.handler.on_moved(event)
            self.handler.flush_pending()
        # 整个 move 操作按 src+dest 组合键防抖：删旧 + 重建都只执行一次
        delete.assert_called_once_with(self._rel(src))
        add.assert_called_once_with(dest, scan_dir=self.handler._watch_root_path)

    def test_deleted_immediately_removes_source_even_if_debounced(self):
        src = os.path.join(self.dir, "notes", "gone.md")
        key = kb_watcher._normalize_path(src)
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete:
            # 该路径有待执行的重索引时，删除事件先取消它，再排队删除
            self.handler.on_created(_file_event(src_path=src))
            self.assertIn(key, self.handler._TIMERS)
            self.handler.on_deleted(_file_event(src_path=src))
            self.assertNotIn(key, self.handler._TIMERS)
            self.handler.flush_pending()
        delete.assert_called_once_with(self._rel(src))
        update.assert_not_called()

    def test_handler_swallows_ingest_exceptions(self):
        src = os.path.join(self.dir, "notes", "old.md")
        src2 = os.path.join(self.dir, "notes", "old2.md")
        dest = os.path.join(self.dir, "notes", "new.md")
        other = os.path.join(self.dir, "notes", "other.md")
        _write(dest)
        _write(other)
        with mock.patch.object(
            kb_watcher.kb_ingest,
            "update_document",
            side_effect=RuntimeError("boom"),
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest,
            "delete_source_exact",
            side_effect=RuntimeError("boom"),
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest,
            "add_single_document",
            side_effect=RuntimeError("boom"),
        ) as add:
            # 任何 ingest 异常都不能从 handler 冒出（否则 watchdog 线程退出）
            self.handler.on_created(_file_event(src_path=self.path))
            self.handler.on_modified(_file_event(src_path=other))
            self.handler.on_deleted(_file_event(src_path=src))
            self.handler.on_moved(_file_event(src_path=src2, dest_path=dest))
            self.handler.flush_pending()
        self.assertGreaterEqual(update.call_count, 2)
        self.assertGreaterEqual(delete.call_count, 1)
        self.assertGreaterEqual(add.call_count, 1)


class TestLegacyBasenameGuard(unittest.TestCase):
    """库中只有 basename 的旧来源无法反推绝对路径，watcher 必须整体跳过事件。"""

    def setUp(self):
        self.dir = _temp_dir(self)
        self.path = os.path.join(self.dir, "notes", "note.md")
        _write(self.path)
        _patch_resolver(self, lambda path: ("note.md", ""))
        self.handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(self.handler.cancel_pending)

    def _write_patches(self):
        return (
            mock.patch.object(kb_watcher.kb_ingest, "update_document"),
            mock.patch.object(kb_watcher.kb_ingest, "add_single_document"),
            mock.patch.object(kb_watcher.kb_ingest, "delete_source_exact"),
            mock.patch.object(kb_watcher.kb_ingest, "delete_documents"),
        )

    def test_create_and_modify_skip_without_any_write(self):
        event = _file_event(src_path=self.path)
        patches = self._write_patches()
        with patches[0] as update, patches[1] as add, patches[2] as delete, \
                patches[3] as delete_glob:
            self.handler.on_created(event)
            self.handler.on_modified(event)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.handler.flush_pending()
        update.assert_not_called()
        add.assert_not_called()
        delete.assert_not_called()
        delete_glob.assert_not_called()
        self.assertIn("legacy basename source", stderr.getvalue())
        self.assertIn("需人工确认", stderr.getvalue())

    def test_delete_skips_without_any_write(self):
        event = _file_event(src_path=self.path)
        patches = self._write_patches()
        with patches[0] as update, patches[1] as add, patches[2] as delete, \
                patches[3] as delete_glob:
            self.handler.on_deleted(event)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.handler.flush_pending()
        update.assert_not_called()
        add.assert_not_called()
        delete.assert_not_called()
        delete_glob.assert_not_called()
        self.assertIn("legacy basename source", stderr.getvalue())

    def test_move_skips_whole_event_without_any_write(self):
        dest = os.path.join(self.dir, "notes", "new.md")
        _write(dest)
        patches = self._write_patches()
        with patches[0] as update, patches[1] as add, patches[2] as delete, \
                patches[3] as delete_glob:
            self.handler.on_moved(
                _file_event(src_path=self.path, dest_path=dest))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.handler.flush_pending()
        update.assert_not_called()
        add.assert_not_called()
        delete.assert_not_called()
        delete_glob.assert_not_called()
        self.assertIn("跳过移动事件", stderr.getvalue())
        self.assertIn("legacy basename source", stderr.getvalue())


class TestSourceResolutionBehavior(unittest.TestCase):
    """相对路径来源自动管理；新文件按监听根相对路径入库。"""

    def setUp(self):
        self.dir = _temp_dir(self)
        self.handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(self.handler.cancel_pending)

    def test_relative_existing_source_updates_via_update_document(self):
        path = os.path.join(self.dir, "notes", "a.md")
        _write(path)
        _patch_resolver(self, lambda p: ("notes/a.md", ""))
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_modified(_file_event(src_path=path))
            self.handler.flush_pending()
        update.assert_called_once_with(path, source="notes/a.md")
        add.assert_not_called()

    def test_new_nested_file_is_added_with_scan_dir(self):
        path = os.path.join(self.dir, "notes", "new.md")
        _write(path)
        _patch_resolver(self, lambda p: ("", ""))
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_created(_file_event(src_path=path))
            self.handler.flush_pending()
        add.assert_called_once_with(path, scan_dir=self.handler._watch_root_path)
        update.assert_not_called()

    def test_same_basename_with_unrelated_suffix_is_treated_as_new(self):
        path = os.path.join(self.dir, "notes", "report.md")
        _write(path)
        _patch_resolver(
            self, lambda p: (os.path.join("other", "report.md"), ""))
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_modified(_file_event(src_path=path))
            self.handler.flush_pending()
        update.assert_not_called()
        add.assert_called_once_with(path, scan_dir=self.handler._watch_root_path)

    def test_new_root_level_file_is_skipped_as_legacy_basename(self):
        path = os.path.join(self.dir, "new.md")
        _write(path)
        _patch_resolver(self, lambda p: ("", ""))
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_created(_file_event(src_path=path))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.handler.flush_pending()
        add.assert_not_called()
        update.assert_not_called()
        self.assertIn("legacy basename source", stderr.getvalue())

    def test_relative_delete_uses_exact_source_not_glob(self):
        path = os.path.join(self.dir, "notes", "gone.md")
        _patch_resolver(self, lambda p: ("notes/gone.md", ""))
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "delete_documents", return_value="ok"
        ) as delete_glob:
            self.handler.on_deleted(_file_event(src_path=path))
            self.handler.flush_pending()
        delete.assert_called_once_with("notes/gone.md")
        delete_glob.assert_not_called()

    def test_ambiguous_source_skips_event(self):
        path = os.path.join(self.dir, "notes", "a.md")
        _write(path)
        _patch_resolver(
            self, lambda p: ("", "[ERROR] 匹配到多个知识库来源，无法唯一确定"))
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update:
            self.handler.on_modified(_file_event(src_path=path))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.handler.flush_pending()
        update.assert_not_called()
        self.assertIn("匹配到多个知识库来源", stderr.getvalue())


class TestIsIndexable(unittest.TestCase):
    def test_supported_extension_is_indexable(self):
        self.assertTrue(kb_watcher._is_indexable(os.path.join("root", "note.md")))
        self.assertTrue(kb_watcher._is_indexable(os.path.join("root", "DATA.JSON")))
        self.assertTrue(kb_watcher._is_indexable(os.path.join("root", "script.PY")))
        self.assertFalse(kb_watcher._is_indexable(os.path.join("root", "note.exe")))
        self.assertFalse(kb_watcher._is_indexable(os.path.join("root", "README")))

    def test_excluded_directory_segments_are_rejected(self):
        for name in ("node_modules", ".git", "__pycache__", ".venv",
                     "truncated-results"):
            with self.subTest(name=name):
                self.assertFalse(
                    kb_watcher._is_indexable(os.path.join("root", name, "note.md"))
                )
        self.assertFalse(
            kb_watcher._is_indexable(os.path.join("root", ".git", "nested", "x.py"))
        )

    def test_similar_directory_or_filename_is_not_excluded(self):
        self.assertTrue(
            kb_watcher._is_indexable(
                os.path.join("root", "node_modules_backup", "note.md")
            )
        )
        self.assertTrue(
            kb_watcher._is_indexable(os.path.join("root", "node_modules.md"))
        )

    def test_directory_like_paths_are_not_indexable(self):
        directory = os.path.join("root", "notes")
        self.assertFalse(kb_watcher._is_indexable(directory))
        self.assertFalse(kb_watcher._is_indexable(directory + os.sep))
        self.assertFalse(kb_watcher._is_indexable(""))


class TestEventFiltering(unittest.TestCase):
    def setUp(self):
        self.dir = _temp_dir(self)
        self.handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(self.handler.cancel_pending)

    def test_deleted_event_in_excluded_dir_does_not_touch_index(self):
        path = os.path.join(self.dir, ".git", "note.md")
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_documents", return_value="ok"
        ) as delete:
            self.handler.on_deleted(_file_event(src_path=path))
            self.handler.flush_pending()
        delete.assert_not_called()

    def test_created_event_with_unsupported_extension_does_not_schedule(self):
        path = os.path.join(self.dir, "note.exe")
        with mock.patch.object(
            kb_watcher.kb_ingest, "update_document", return_value="ok"
        ) as update:
            self.handler.on_created(_file_event(src_path=path))
            self.assertEqual(self.handler._TIMERS, {})
            self.handler.flush_pending()
        update.assert_not_called()


class TestTrailingEdgeDebounce(unittest.TestCase):
    def setUp(self):
        self.dir = _temp_dir(self)
        self.path = os.path.join(self.dir, "notes", "note.md")
        _write(self.path)
        _patch_resolver(self, _relative_resolver(self.dir))

    def test_only_latest_timer_survives_and_stale_timer_is_ignored(self):
        with mock.patch.object(kb_watcher.threading, "Timer", _ManualTimer):
            handler = kb_watcher._AutoIndexHandler(self.dir)
            self.addCleanup(handler.cancel_pending)
            event = _file_event(src_path=self.path)
            key = kb_watcher._normalize_path(self.path)
            with mock.patch.object(
                kb_watcher.kb_ingest, "update_document", return_value="ok"
            ) as update:
                handler.on_created(event)
                first = handler._TIMERS[key]
                handler.on_modified(event)
                second = handler._TIMERS[key]

                self.assertIsNot(first, second)
                self.assertTrue(first.cancelled)
                # 已过期的 Timer 即使触发也不能执行
                first.fire()
                self.assertEqual(update.call_count, 0)
                second.fire()
                self.assertEqual(update.call_count, 1)
                self.assertEqual(update.call_args_list, [mock.call(self.path, source=os.path.relpath(self.path, self.dir))])

    def test_rapid_events_execute_once_after_quiet_period(self):
        with mock.patch.object(kb_watcher, "_DEBOUNCE_SECONDS", 0.1):
            handler = kb_watcher._AutoIndexHandler(self.dir)
            self.addCleanup(handler.cancel_pending)
            event = _file_event(src_path=self.path)
            with mock.patch.object(
                kb_watcher.kb_ingest, "update_document", return_value="ok"
            ) as update:
                handler.on_created(event)
                handler.on_modified(event)
                handler.on_created(event)
                deadline = time.time() + 2.0
                while update.call_count == 0 and time.time() < deadline:
                    time.sleep(0.01)
                time.sleep(0.15)  # 再等一段，确认没有第二次执行
                self.assertEqual(update.call_count, 1)
                self.assertNotIn(kb_watcher._normalize_path(self.path),
                                 handler._TIMERS)

    def test_failed_update_is_retried_on_next_event(self):
        handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(handler.cancel_pending)
        event = _file_event(src_path=self.path)
        key = kb_watcher._normalize_path(self.path)
        with mock.patch.object(
            kb_watcher.kb_ingest,
            "update_document",
            side_effect=[RuntimeError("boom"), "ok"],
        ) as update:
            handler.on_created(event)
            handler.flush_pending()
            self.assertEqual(update.call_count, 1)
            # 失败后不保留成功状态，下一次事件仍会重新安排
            self.assertNotIn(key, handler._TIMERS)

            handler.on_modified(event)
            handler.flush_pending()
        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list,
                         [mock.call(self.path, source=os.path.relpath(self.path, self.dir)), mock.call(self.path, source=os.path.relpath(self.path, self.dir))])


class TestMovedRootBoundary(unittest.TestCase):
    def setUp(self):
        self.dir = _temp_dir(self)
        _patch_resolver(self, _relative_resolver(self.dir))
        self.handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(self.handler.cancel_pending)

    def _rel(self, path):
        return os.path.relpath(path, self.dir)

    def test_moved_within_root_deletes_source_then_indexes_dest(self):
        src = os.path.join(self.dir, "notes", "old.md")
        dest = os.path.join(self.dir, "notes", "new.md")
        _write(dest)
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))
            self.handler.flush_pending()
        delete.assert_called_once_with(self._rel(src))
        add.assert_called_once_with(dest, scan_dir=self.handler._watch_root_path)

    def test_moved_outside_root_only_deletes_source(self):
        outside = _temp_dir(self)
        src = os.path.join(self.dir, "notes", "old.md")
        dest = os.path.join(outside, "new.md")
        _write(dest)
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))
            self.handler.flush_pending()
        delete.assert_called_once_with(self._rel(src))
        add.assert_not_called()

    def test_moved_into_excluded_subdir_is_not_reindexed(self):
        src = os.path.join(self.dir, "notes", "old.md")
        dest = os.path.join(self.dir, ".git", "new.md")
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))
            self.handler.flush_pending()
        delete.assert_called_once_with(self._rel(src))
        add.assert_not_called()

    def test_moved_from_outside_root_only_indexes_dest(self):
        outside = _temp_dir(self)
        src = os.path.join(outside, "old.md")
        dest = os.path.join(self.dir, "notes", "new.md")
        _write(dest)
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete, mock.patch.object(
            kb_watcher.kb_ingest, "add_single_document", return_value="ok"
        ) as add:
            self.handler.on_moved(_file_event(src_path=src, dest_path=dest))
            self.handler.flush_pending()
        # 根外旧路径不归本监听器管理，不得按 basename 误删库中来源
        delete.assert_not_called()
        add.assert_called_once_with(dest, scan_dir=self.handler._watch_root_path)


class TestDeleteBatching(unittest.TestCase):
    def setUp(self):
        self.dir = _temp_dir(self)
        _patch_resolver(self, _relative_resolver(self.dir))
        self.handler = kb_watcher._AutoIndexHandler(self.dir)
        self.addCleanup(self.handler.cancel_pending)

    def test_repeated_deletes_are_deduplicated_and_batched(self):
        first = os.path.join(self.dir, "notes", "a.md")
        second = os.path.join(self.dir, "notes", "b.md")
        with mock.patch.object(
            kb_watcher.kb_ingest, "delete_source_exact", return_value=1
        ) as delete:
            self.handler.on_deleted(_file_event(src_path=first))
            self.handler.on_deleted(_file_event(src_path=first))
            self.handler.on_deleted(_file_event(src_path=second))
            self.assertEqual(len(self.handler._pending_deletes), 2)
            delete.assert_not_called()
            self.handler.flush_pending()
        self.assertEqual(delete.call_count, 2)
        sources = {c.args[0] for c in delete.call_args_list}
        self.assertEqual(
            sources,
            {os.path.relpath(first, self.dir), os.path.relpath(second, self.dir)},
        )

    def test_delete_flush_timer_batches_within_window(self):
        with mock.patch.object(kb_watcher, "_DELETE_FLUSH_SECONDS", 0.1):
            handler = kb_watcher._AutoIndexHandler(self.dir)
            self.addCleanup(handler.cancel_pending)
            with mock.patch.object(
                kb_watcher.kb_ingest, "delete_source_exact", return_value=1
            ) as delete:
                handler.on_deleted(
                    _file_event(src_path=os.path.join(self.dir, "notes", "a.md")))
                handler.on_deleted(
                    _file_event(src_path=os.path.join(self.dir, "notes", "b.md")))
                delete.assert_not_called()
                deadline = time.time() + 2.0
                while delete.call_count < 2 and time.time() < deadline:
                    time.sleep(0.01)
                time.sleep(0.1)
                self.assertEqual(delete.call_count, 2)


class _FakeObserver:
    """替代 watchdog Observer，避免测试启动真实监听线程。"""

    instances = []

    def __init__(self):
        self.scheduled = []
        self.started = False
        self.stopped = False
        self.joined = None
        self.alive = False
        self.hang_on_join = False
        _FakeObserver.instances.append(self)

    def schedule(self, handler, path, recursive=False):
        self.scheduled.append((handler, path, recursive))

    def start(self):
        self.started = True
        self.alive = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = timeout
        if not self.hang_on_join:
            self.alive = False

    def is_alive(self):
        return self.alive


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
        self.assertEqual(path, os.path.realpath(self.dir1))
        self.assertTrue(recursive)

    def test_two_dirs_start_and_stop_individually(self):
        kb_watcher.start_watcher(self.dir1)
        kb_watcher.start_watcher(self.dir2)

        key1 = kb_watcher._normalize_path(self.dir1)
        key2 = kb_watcher._normalize_path(self.dir2)
        self.assertEqual(len(kb_watcher._OBSERVERS), 2)
        self.assertEqual(len(_FakeObserver.instances), 2)
        running, dirs = kb_watcher.watcher_running()
        self.assertTrue(running)
        self.assertIn(key1, dirs)
        self.assertIn(key2, dirs)

        message = kb_watcher.stop_watcher(self.dir1)
        self.assertIn("已停止", message)
        self.assertEqual(len(kb_watcher._OBSERVERS), 1)
        self.assertTrue(_FakeObserver.instances[0].stopped)
        self.assertFalse(_FakeObserver.instances[1].stopped)
        self.assertFalse(_FakeObserver.instances[0].is_alive())
        self.assertTrue(_FakeObserver.instances[1].is_alive())

        kb_watcher.stop_watcher()
        self.assertEqual(len(kb_watcher._OBSERVERS), 0)
        self.assertTrue(_FakeObserver.instances[1].stopped)
        self.assertIn("未在运行", kb_watcher.stop_watcher())

    def test_default_watch_dir_is_cwd_and_reported(self):
        message = kb_watcher.start_watcher()
        self.assertIn("文件监听已启动", message)
        self.assertIn(os.path.realpath(os.getcwd()), message)
        self.assertIn("默认监听当前工作目录", message)
        self.assertEqual(len(_FakeObserver.instances), 1)

    def test_missing_dir_is_rejected_without_starting_observer(self):
        missing = os.path.join(self.dir1, "does-not-exist")
        message = kb_watcher.start_watcher(missing)
        self.assertIn("目录不存在", message)
        self.assertEqual(len(_FakeObserver.instances), 0)
        self.assertEqual(len(kb_watcher._OBSERVERS), 0)

    def test_registry_normalizes_case_dot_and_realpath(self):
        kb_watcher.start_watcher(self.dir1)
        key = kb_watcher._normalize_path(self.dir1)
        self.assertIn(key, kb_watcher._OBSERVERS)

        variants = [os.path.join(self.dir1, ".", "sub", "..")]
        if os.path.normcase(self.dir1.upper()) == os.path.normcase(self.dir1):
            variants.append(self.dir1.upper())
        for variant in variants:
            with self.subTest(variant=variant):
                message = kb_watcher.start_watcher(variant)
                self.assertIn("已在运行", message)
                self.assertEqual(len(kb_watcher._OBSERVERS), 1)
                self.assertEqual(len(_FakeObserver.instances), 1)

    def test_parent_and_child_dirs_overlap_is_rejected(self):
        child = os.path.join(self.dir1, "sub")
        os.makedirs(child)

        kb_watcher.start_watcher(self.dir1)
        message = kb_watcher.start_watcher(child)
        self.assertIn("父子目录重叠", message)
        self.assertEqual(len(kb_watcher._OBSERVERS), 1)

        kb_watcher.stop_watcher()
        self.assertIn("文件监听已启动", kb_watcher.start_watcher(child))
        message = kb_watcher.start_watcher(self.dir1)
        self.assertIn("父子目录重叠", message)
        self.assertEqual(len(kb_watcher._OBSERVERS), 1)

    def test_stop_timeout_keeps_registry_and_allows_retry(self):
        kb_watcher.start_watcher(self.dir1)
        observer = _FakeObserver.instances[0]
        observer.hang_on_join = True

        message = kb_watcher.stop_watcher(self.dir1)
        self.assertIn("停止失败", message)
        self.assertIn(kb_watcher._normalize_path(self.dir1),
                      kb_watcher._OBSERVERS)
        self.assertTrue(observer.stopped)
        self.assertTrue(observer.is_alive())

        running, _ = kb_watcher.watcher_running()
        self.assertTrue(running)

        observer.hang_on_join = False
        message = kb_watcher.stop_watcher(self.dir1)
        self.assertIn("已停止", message)
        self.assertEqual(kb_watcher._OBSERVERS, {})
        self.assertFalse(observer.is_alive())

    def test_stop_all_reports_failure_and_keeps_failed_entry(self):
        kb_watcher.start_watcher(self.dir1)
        kb_watcher.start_watcher(self.dir2)
        _FakeObserver.instances[0].hang_on_join = True

        message = kb_watcher.stop_watcher()
        key1 = kb_watcher._normalize_path(self.dir1)
        key2 = kb_watcher._normalize_path(self.dir2)
        self.assertIn("停止失败", message)
        self.assertIn(key1, message)
        self.assertIn(key2, message)
        self.assertIn(key1, kb_watcher._OBSERVERS)
        self.assertNotIn(key2, kb_watcher._OBSERVERS)

    def test_watcher_running_prunes_dead_observers(self):
        kb_watcher.start_watcher(self.dir1)
        _FakeObserver.instances[0].alive = False

        running, dirs = kb_watcher.watcher_running()
        self.assertFalse(running)
        self.assertEqual(dirs, "")
        self.assertEqual(kb_watcher._OBSERVERS, {})

    def test_stop_then_restart_same_dir(self):
        kb_watcher.start_watcher(self.dir1)
        self.assertIn("已停止", kb_watcher.stop_watcher(self.dir1))

        message = kb_watcher.start_watcher(self.dir1)
        self.assertIn("文件监听已启动", message)
        self.assertEqual(len(kb_watcher._OBSERVERS), 1)
        self.assertEqual(len(_FakeObserver.instances), 2)
        self.assertTrue(_FakeObserver.instances[1].started)
        self.assertTrue(_FakeObserver.instances[1].is_alive())


if __name__ == "__main__":
    unittest.main()
