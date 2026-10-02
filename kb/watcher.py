# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

目录监听自动重索引（start/stop_watcher）：watchdog Observer + 5 秒防抖，
文件修改/新增/移动时调用 kb_ingest.update_document 增量更新。
现状：单目录、无删除事件处理；优化方向：多目录注册、删除同步、事件队列。
"""

from __future__ import annotations

import os
import sys
import time

from . import config as cfg
from . import ingest as kb_ingest

_FILE_OBSERVER = None
_FILE_OBSERVER_DIR = None

_DEBOUNCE_SECONDS = 5


def watcher_running() -> tuple[bool, str]:
    return _FILE_OBSERVER is not None, _FILE_OBSERVER_DIR or ""


def start_watcher(watch_dir: str = "") -> str:
    """启动文件变更监听，自动增量重索引发生变化的文档。"""
    global _FILE_OBSERVER, _FILE_OBSERVER_DIR

    if _FILE_OBSERVER is not None:
        return f"⚠️ 文件监听已在运行中（{_FILE_OBSERVER_DIR}）。先使用 stop_watcher 停止。"

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return "❌ 需要 watchdog 库。请安装: pip install watchdog"

    if not watch_dir:
        watch_dir = os.getcwd()
    if not os.path.isdir(watch_dir):
        return f"❌ 目录不存在: {watch_dir}"

    class _AutoIndexHandler(FileSystemEventHandler):
        def __init__(self):
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
                print(f"[watcher] 已更新: {filepath}\n{result.splitlines()[0]}", file=sys.stderr)
            except Exception as e:
                print(f"[watcher] 更新失败 {filepath}: {e}", file=sys.stderr)

        def _maybe(self, path: str) -> None:
            now = time.time()
            if now - self._debounce.get(path, 0) > _DEBOUNCE_SECONDS:
                self._debounce[path] = now
                self._reindex(path)

        def on_modified(self, event):
            if not event.is_dir:
                self._maybe(event.src_path)

        def on_created(self, event):
            if not event.is_dir:
                self._maybe(event.src_path)

        def on_moved(self, event):
            if not event.is_dir:
                self._reindex(event.dest_path)

    handler = _AutoIndexHandler()
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=True)
    observer.daemon = True
    observer.start()

    _FILE_OBSERVER = observer
    _FILE_OBSERVER_DIR = watch_dir
    return (
        f"✅ **文件监听已启动**\n{'─' * 40}\n"
        f"监听目录: {watch_dir}\n文件变更时将自动增量重索引。\n"
        f"💡 使用 stop_watcher 停止监听。"
    )


def stop_watcher() -> str:
    """停止文件变更监听。"""
    global _FILE_OBSERVER, _FILE_OBSERVER_DIR
    if _FILE_OBSERVER is None:
        return "文件监听未在运行。"
    _FILE_OBSERVER.stop()
    _FILE_OBSERVER.join(timeout=5)
    stopped_dir = _FILE_OBSERVER_DIR
    _FILE_OBSERVER = None
    _FILE_OBSERVER_DIR = None
    return f"🛑 文件监听已停止（{stopped_dir}）。"
