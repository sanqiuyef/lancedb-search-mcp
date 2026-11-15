# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

目录监听自动重索引（start/stop_watcher）：watchdog Observer + 5 秒防抖。
支持多目录并行监听；文件修改/新增/移动时调用 kb_ingest.update_document
增量更新，删除/移动时调用 kb_ingest.delete_documents 清理旧来源。
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

from . import config as cfg
from . import ingest as kb_ingest

if TYPE_CHECKING:
    from watchdog.observers import Observer

_OBSERVERS: dict[str, Observer] = {}
_DEBOUNCE_SECONDS = 5

try:
    from watchdog.events import FileSystemEventHandler as _FileSystemEventHandler
except ImportError:  # watchdog 未安装时仍允许导入本模块
    _FileSystemEventHandler = object  # type: ignore[assignment,misc]


def _log(message: str) -> None:
    print(f"[watcher] {message}", file=sys.stderr)


class _AutoIndexHandler(_FileSystemEventHandler):
    """watchdog 事件处理器：防抖重索引 + 删除/移动同步清理。

    所有事件回调都兜住异常，避免异常冒泡终止 watchdog 监听线程。
    """

    def __init__(self) -> None:
        self._debounce: dict[str, float] = {}

    def _reindex(self, filepath: str) -> None:
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in cfg.EXTENSIONS:
            return
        if any(excl in filepath for excl in cfg.EXCLUDE_DIRS):
            return
        try:
            if os.path.getsize(filepath) > cfg.MAX_FILE_SIZE:
                return
        except OSError:
            return
        try:
            result = kb_ingest.update_document(filepath)
            first_line = result.splitlines()[0] if result else ""
            _log(f"已更新: {filepath}\n{first_line}")
        except Exception as e:
            _log(f"更新失败 {filepath}: {e}")

    def _delete_source(self, filepath: str) -> None:
        """按旧路径的文件名删除库中对应来源。"""
        source = os.path.basename(filepath)
        if not source:
            return
        try:
            result = kb_ingest.delete_documents(source_pattern=source)
            first_line = result.splitlines()[0] if result else ""
            _log(f"已删除旧索引: {source}\n{first_line}")
        except Exception as e:
            _log(f"删除失败 {source}: {e}")

    def _debounced(self, key: str, action) -> None:
        now = time.time()
        if now - self._debounce.get(key, 0) > _DEBOUNCE_SECONDS:
            self._debounce[key] = now
            action()

    def _maybe(self, path: str) -> None:
        self._debounced(path, lambda: self._reindex(path))

    def _handle_moved(self, src_path: str, dest_path: str) -> None:
        self._delete_source(src_path)
        self._reindex(dest_path)

    def on_modified(self, event) -> None:
        try:
            if not getattr(event, "is_directory", False):
                self._maybe(event.src_path)
        except Exception as e:
            _log(f"处理修改事件失败: {e}")

    def on_created(self, event) -> None:
        try:
            if not getattr(event, "is_directory", False):
                self._maybe(event.src_path)
        except Exception as e:
            _log(f"处理新增事件失败: {e}")

    def on_moved(self, event) -> None:
        try:
            if getattr(event, "is_directory", False):
                return
            dest_path = getattr(event, "dest_path", "") or event.src_path
            self._debounced(
                dest_path,
                lambda: self._handle_moved(event.src_path, dest_path),
            )
        except Exception as e:
            _log(f"处理移动事件失败: {e}")

    def on_deleted(self, event) -> None:
        try:
            if getattr(event, "is_directory", False):
                return
            path = getattr(event, "src_path", "") or getattr(event, "path", "")
            # 删除不防抖：立即清理，重复事件由 delete_documents 自身保证幂等
            self._delete_source(path)
        except Exception as e:
            _log(f"处理删除事件失败: {e}")


def watcher_running() -> tuple[bool, str]:
    dirs = sorted(_OBSERVERS)
    return bool(dirs), ", ".join(dirs)


def start_watcher(watch_dir: str = "") -> str:
    """启动文件变更监听，自动增量重索引发生变化的文档。

    同一目录重复启动是幂等的；不同目录可追加监听。
    未传 watch_dir 时监听当前工作目录（cwd）。
    """
    try:
        from watchdog.observers import Observer
    except ImportError:
        return "❌ 需要 watchdog 库。请安装: pip install watchdog"

    use_cwd = not watch_dir
    if use_cwd:
        watch_dir = os.getcwd()
    watch_dir = os.path.abspath(watch_dir)

    if not os.path.isdir(watch_dir):
        return f"❌ 目录不存在: {watch_dir}"
    if watch_dir in _OBSERVERS:
        return f"⚠️ 文件监听已在运行中（{watch_dir}），无需重复启动。"

    handler = _AutoIndexHandler()
    observer = Observer()
    try:
        observer.schedule(handler, watch_dir, recursive=True)
        observer.daemon = True
        observer.start()
    except Exception as e:
        try:
            observer.stop()
            observer.join(timeout=5)
        except Exception:
            pass
        return f"❌ 文件监听启动失败（{watch_dir}）: {e}"

    _OBSERVERS[watch_dir] = observer
    lines = [
        "✅ **文件监听已启动**",
        "─" * 40,
        f"监听目录: {watch_dir}"
        + ("（未指定 watch_dir，默认监听当前工作目录）" if use_cwd else ""),
        f"当前监听目录数: {len(_OBSERVERS)}",
        "文件修改/新增将自动增量重索引，删除/移动会清理旧索引。",
        "💡 使用 stop_watcher 停止监听（不传目录则停止全部）。",
    ]
    return "\n".join(lines)


def _stop_observer(observer, watch_dir: str) -> None:
    try:
        observer.stop()
        observer.join(timeout=5)
    except Exception as e:
        _log(f"停止监听失败 {watch_dir}: {e}")


def stop_watcher(watch_dir: str = "") -> str:
    """停止文件变更监听：空参停止全部，指定目录只停该目录。"""
    if not _OBSERVERS:
        return "文件监听未在运行。"

    if not watch_dir:
        stopped = sorted(_OBSERVERS)
        for path in stopped:
            _stop_observer(_OBSERVERS[path], path)
        _OBSERVERS.clear()
        return f"🛑 文件监听已停止（{', '.join(stopped)}）。"

    target = os.path.abspath(watch_dir)
    observer = _OBSERVERS.pop(target, None)
    if observer is None:
        return f"文件监听未在运行（{target}）。"
    _stop_observer(observer, target)

    message = f"🛑 文件监听已停止（{target}）。"
    if _OBSERVERS:
        message += f"\n仍在监听: {', '.join(sorted(_OBSERVERS))}"
    return message
