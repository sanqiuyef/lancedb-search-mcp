# -*- coding: utf-8 -*-
"""[自建模块 · 待后期单独优化]

目录监听自动重索引（start/stop_watcher）：watchdog Observer + 尾沿防抖。

- 文件新增/修改：每个路径独立重置 5 秒计时器，静默 5 秒后只索引一次。
- 删除事件：进入待删集合，1.5 秒后批量执行一次（同一路径去重），避免
  删除风暴时逐事件同步删除。
- 移动事件：按 src+dest 组合键防抖；只有目标仍在监听根内才重索引，
  移出根只清理旧来源。
- 所有事件先经 _is_indexable 过滤扩展名与排除目录；目录事件直接忽略
  （限制：整目录移入/移出不会递归迁移其中文件的索引，依赖 watchdog
  对目录内文件产生的独立事件兜底）。
- 只自动改写可反推绝对路径的相对 source。vault 根下的顶层文件（source
  无分隔符但 ``vault_root/source`` 正是事件文件）同样可自动更新；库中
  只有无法反推绝对路径的旧 basename 来源一律跳过并写 stderr，要求人工
  确认，避免按 basename 误替换/误删同名文档。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import TYPE_CHECKING, Callable

from . import config as cfg
from . import ingest as kb_ingest
from . import vault_source as kb_vault_source

if TYPE_CHECKING:
    from watchdog.observers import Observer

_OBSERVERS: dict[str, Observer] = {}
_REGISTRY_LOCK = threading.Lock()
_DEBOUNCE_SECONDS = 5
_DELETE_FLUSH_SECONDS = 1.5

try:
    from watchdog.events import FileSystemEventHandler as _FileSystemEventHandler
except ImportError:  # watchdog 未安装时仍允许导入本模块
    _FileSystemEventHandler = object  # type: ignore[assignment,misc]


def _log(message: str) -> None:
    print(f"[watcher] {message}", file=sys.stderr)


def _normalize_path(path: str) -> str:
    """registry / 计时器统一 key：normcase(realpath(path))。"""
    if not path:
        return ""
    return os.path.normcase(os.path.realpath(path))


def _is_within(path: str, root: str) -> bool:
    """判断已规范化的 path 是否位于已规范化的 root 内（含 root 本身）。"""
    if not path or not root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # Windows 不同盘符
        return False


def _is_indexable(path: str) -> bool:
    """扩展名受支持且路径目录段不含 EXCLUDE_DIRS 中的任一段。"""
    if not path:
        return False
    if os.path.splitext(path)[1].lower() not in cfg.EXTENSIONS:
        return False
    excluded = {
        d.strip("\\/").lower() for d in cfg.EXCLUDE_DIRS if d and d.strip("\\/")
    }
    if not excluded:
        return True
    segments = [s for s in os.path.normcase(path).replace("\\", "/").split("/") if s]
    # 只看目录段，避免文件名恰好叫 node_modules.md 时被误判
    return not any(segment.lower() in excluded for segment in segments[:-1])


def _source_matches_path(filepath: str, source: str) -> bool:
    """source 是否是该文件路径的完整后缀（按目录边界，不区分大小写/分隔符）。"""
    file_norm = os.path.normcase(os.path.abspath(filepath)).replace("\\", "/").rstrip("/")
    source_norm = os.path.normcase(source or "").replace("\\", "/").rstrip("/")
    if not source_norm or source_norm in (".", ".."):
        return False
    return file_norm == source_norm or file_norm.endswith("/" + source_norm)


def _legacy_source_matches_file(source: str, filepath: str) -> bool:
    """纯 basename source 是否正好是 vault 根下的该文件（顶层文件合法）。"""
    if not kb_ingest.source_is_legacy_basename(source):
        return True
    try:
        expected = os.path.join(kb_vault_source.vault_root(), source)
    except (TypeError, ValueError):
        return False
    return _normalize_path(expected) == _normalize_path(filepath)


def _observer_alive(observer) -> bool:
    """observer 是否仍存活；没有 is_alive 的测试替身按存活处理。"""
    is_alive = getattr(observer, "is_alive", None)
    if is_alive is None:
        return True
    try:
        return bool(is_alive())
    except Exception:
        return True


class _AutoIndexHandler(_FileSystemEventHandler):
    """watchdog 事件处理器：尾沿防抖重索引 + 批量删除 + 移动同步。

    所有事件回调都兜住异常，避免异常冒泡终止 watchdog 监听线程。
    """

    def __init__(self, watch_root: str) -> None:
        self._watch_root = _normalize_path(watch_root)
        # 原始大小写/分隔符的根路径，用于把事件路径还原成库里的相对 source
        self._watch_root_path = os.path.realpath(os.path.abspath(watch_root))
        self._lock = threading.RLock()
        # key -> 待执行 Timer；同一 key 新事件会取消旧 Timer 并重置计时
        self._TIMERS: dict[str, threading.Timer] = {}
        self._VERSIONS: dict[str, int] = {}
        self._pending_actions: dict[str, Callable[[], None]] = {}
        self._pending_deletes: set[str] = set()
        self._delete_timer: threading.Timer | None = None

    # ── 计时器 ──────────────────────────────────────────────

    def _schedule(self, key: str, action: Callable[[], None]) -> None:
        """为 key 安排尾沿执行；重复事件取消旧计时器，静默期满后执行一次。"""
        with self._lock:
            version = self._VERSIONS.get(key, 0) + 1
            self._VERSIONS[key] = version
            old_timer = self._TIMERS.pop(key, None)
            self._pending_actions[key] = action
            timer = threading.Timer(
                _DEBOUNCE_SECONDS, self._fire, args=(key, version)
            )
            timer.daemon = True
            self._TIMERS[key] = timer
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    def _fire(self, key: str, version: int) -> None:
        with self._lock:
            if self._VERSIONS.get(key) != version:
                return  # 已被更新的事件取代
            self._VERSIONS.pop(key, None)
            self._TIMERS.pop(key, None)
            action = self._pending_actions.pop(key, None)
        if action is None:
            return
        try:
            action()
        except Exception as e:
            # 执行失败不记录成功状态；后续事件会重新安排计时器并重试
            _log(f"执行待处理动作失败 {key}: {e}")

    def _cancel_timer(self, key: str) -> None:
        with self._lock:
            self._VERSIONS.pop(key, None)
            timer = self._TIMERS.pop(key, None)
            self._pending_actions.pop(key, None)
        if timer is not None:
            timer.cancel()

    def flush_pending(self) -> None:
        """同步执行所有待处理动作（测试/停机用），不等待防抖计时。"""
        with self._lock:
            actions = list(self._pending_actions.values())
            timers = list(self._TIMERS.values())
            self._pending_actions.clear()
            self._TIMERS.clear()
            self._VERSIONS.clear()
            delete_timer = self._delete_timer
            self._delete_timer = None
        for timer in timers:
            timer.cancel()
        if delete_timer is not None:
            delete_timer.cancel()
        for action in actions:
            try:
                action()
            except Exception as e:
                _log(f"执行待处理动作失败: {e}")
        self._flush_deletes()

    def cancel_pending(self) -> None:
        """取消全部计时器与待删集合，不执行任何 ingest 动作。"""
        with self._lock:
            timers = list(self._TIMERS.values())
            self._TIMERS.clear()
            self._VERSIONS.clear()
            self._pending_actions.clear()
            delete_timer = self._delete_timer
            self._delete_timer = None
            self._pending_deletes.clear()
        for timer in timers:
            timer.cancel()
        if delete_timer is not None:
            delete_timer.cancel()

    # ── ingest 动作 ────────────────────────────────────────

    def _existing_source_for_write(self, filepath: str) -> "tuple[str, str]":
        """解析库中已存在的目标 source，返回 (source, skip_reason)。

        无匹配时返回 ("", "")，由调用方按新文件处理。匹配到 legacy
        basename（库中只有 `a.md` 这类无法反推绝对路径的来源）或存在
        同名歧义时返回非空 skip_reason，调用方不得写库。
        """
        try:
            source, error = kb_ingest.resolve_writable_source(filepath)
        except Exception as exc:
            return "", f"解析来源失败: {exc}"
        if error:
            return "", error
        if source and not _legacy_source_matches_file(source, filepath):
            return "", (
                f"legacy basename source，需人工确认: {source}（{filepath}）"
            )
        if source and not _source_matches_path(filepath, source):
            # 同 basename 但不是该路径的后缀：不能确认是同一文件，按新文件处理，
            # 避免把同名的其他目录来源当作本文件的旧版本替换掉。
            return "", ""
        return source, ""

    def _legacy_new_source_reason(self, filepath: str) -> str:
        """新文件按 vault 相对路径算出的 source 若是悬空 basename，返回跳过原因。

        改用 ``rel_to_vault``：只有 ``vault_root/source`` 与事件路径不是同一
        文件时才是真正的 legacy basename。vault 根下的顶层文件（source 无
        分隔符）是合法的，必须能正常入库；vault 外文件会复制进 vault，也不
        存在 legacy 歧义。
        """
        try:
            planned = kb_vault_source.rel_to_vault(filepath)
        except (TypeError, ValueError):
            return ""
        if not kb_ingest.source_is_legacy_basename(planned):
            return ""
        if _legacy_source_matches_file(planned, filepath):
            return ""
        return (
            f"legacy basename source，需人工确认: {planned}（{filepath}）"
        )

    def _reindex(self, filepath: str, existing_source: "str | None" = None) -> None:
        """更新已有相对路径来源，或把监听根下的新文件按相对路径入库。

        ``existing_source`` 为 None 时现场解析；显式传入 "" 表示按新文件处理。
        legacy basename 来源一律跳过并写 stderr，不调用任何写函数。
        """
        if not _is_indexable(filepath):
            return
        if existing_source is None:
            existing_source, skip_reason = self._existing_source_for_write(filepath)
            if skip_reason:
                _log(f"跳过更新 {filepath}: {skip_reason}")
                return
        if not existing_source:
            skip_reason = self._legacy_new_source_reason(filepath)
            if skip_reason:
                _log(f"跳过更新 {filepath}: {skip_reason}")
                return
        try:
            if os.path.getsize(filepath) > cfg.MAX_FILE_SIZE:
                return
        except OSError:
            return
        try:
            if existing_source:
                result = kb_ingest.update_document(filepath, source=existing_source)
                label = "已更新"
            else:
                result = kb_ingest.add_single_document(
                    filepath, scan_dir=self._watch_root_path)
                label = "已新增"
            first_line = result.splitlines()[0] if result else ""
            _log(f"{label}: {filepath}\n{first_line}")
        except Exception as e:
            _log(f"更新失败 {filepath}: {e}")

    def _delete_source(self, filepath: str,
                       existing_source: "str | None" = None) -> None:
        """按库中解析出的 source 精确删除；legacy basename 来源跳过并提示。"""
        if not _is_indexable(filepath):
            return
        if existing_source is None:
            existing_source, skip_reason = self._existing_source_for_write(filepath)
            if skip_reason:
                _log(f"跳过删除 {filepath}: {skip_reason}")
                return
        if not existing_source:
            return
        try:
            removed = kb_ingest.delete_source_exact(existing_source)
            _log(f"已删除旧索引: {existing_source}（{removed} 行）")
        except Exception as e:
            _log(f"删除失败 {existing_source}: {e}")

    def _queue_delete(self, filepath: str) -> None:
        """删除事件入队，首个事件启动 1.5 秒批量 flush；同路径去重。"""
        key = _normalize_path(filepath)
        if not key:
            return
        created = False
        with self._lock:
            self._pending_deletes.add(key)
            if self._delete_timer is None:
                timer = threading.Timer(_DELETE_FLUSH_SECONDS, self._flush_deletes)
                timer.daemon = True
                self._delete_timer = timer
                created = True
        if created:
            timer.start()

    def _flush_deletes(self) -> None:
        with self._lock:
            pending = self._pending_deletes
            self._pending_deletes = set()
            delete_timer = self._delete_timer
            self._delete_timer = None
        if delete_timer is not None:
            delete_timer.cancel()
        for path in sorted(pending):
            self._delete_source(path)

    def _handle_moved(self, src_path: str, dest_path: str) -> None:
        # 只清理监听根内的旧路径；从根外移入时不得按 basename 误删库中来源
        src_in_root = _is_indexable(src_path) and _is_within(
            _normalize_path(src_path), self._watch_root
        )
        dest_in_root = _is_indexable(dest_path) and _is_within(
            _normalize_path(dest_path), self._watch_root
        )

        src_source = ""
        if src_in_root:
            src_source, src_reason = self._existing_source_for_write(src_path)
        else:
            src_reason = ""
        dest_reason = (
            self._legacy_new_source_reason(dest_path) if dest_in_root else ""
        )
        skip_reason = src_reason or dest_reason
        if skip_reason:
            # 移动是「删旧 + 建新」两步：任一侧是 legacy basename 就整体跳过，
            # 避免只删不建或只建不删的半完成状态。
            _log(f"跳过移动事件 {src_path} -> {dest_path}: {skip_reason}")
            return

        if src_in_root:
            self._delete_source(src_path, existing_source=src_source)
        if dest_in_root:
            # 目标按新文件处理，用监听根相对路径作 source；若同一相对 source
            # 已存在，add_single_document 会幂等替换。
            self._reindex(dest_path, existing_source="")

    # ── 事件入口 ───────────────────────────────────────────

    def on_modified(self, event) -> None:
        try:
            if getattr(event, "is_directory", False):
                return
            path = getattr(event, "src_path", "") or getattr(event, "path", "")
            if not _is_indexable(path):
                return
            self._schedule(_normalize_path(path), lambda: self._reindex(path))
        except Exception as e:
            _log(f"处理修改事件失败: {e}")

    def on_created(self, event) -> None:
        try:
            if getattr(event, "is_directory", False):
                return
            path = getattr(event, "src_path", "") or getattr(event, "path", "")
            if not _is_indexable(path):
                return
            self._schedule(_normalize_path(path), lambda: self._reindex(path))
        except Exception as e:
            _log(f"处理新增事件失败: {e}")

    def on_moved(self, event) -> None:
        try:
            if getattr(event, "is_directory", False):
                # 已知限制：整目录移入/移出不会递归迁移其中文件的索引；
                # 依赖 watchdog 对目录内文件产生的独立事件兜底。
                return
            src_path = getattr(event, "src_path", "") or getattr(event, "path", "")
            dest_path = getattr(event, "dest_path", "") or src_path
            if not _is_indexable(src_path) and not _is_indexable(dest_path):
                return
            # 该路径若有 pending 重索引，移动后以组合动作的结果为准
            src_key = _normalize_path(src_path)
            dest_key = _normalize_path(dest_path)
            self._cancel_timer(src_key)
            self._cancel_timer(dest_key)
            # 用 src+dest 组合键区分不同移动，同时避免同一移动重复执行
            key = src_key + "\x00" + dest_key
            self._schedule(key, lambda: self._handle_moved(src_path, dest_path))
        except Exception as e:
            _log(f"处理移动事件失败: {e}")

    def on_deleted(self, event) -> None:
        try:
            if getattr(event, "is_directory", False):
                return
            path = getattr(event, "src_path", "") or getattr(event, "path", "")
            if not _is_indexable(path):
                return
            # 先取消同路径待执行的重索引，再排队批量删除
            self._cancel_timer(_normalize_path(path))
            self._queue_delete(path)
        except Exception as e:
            _log(f"处理删除事件失败: {e}")


def watcher_running() -> tuple[bool, str]:
    """返回是否仍有存活监听；顺带清理已死亡的 registry 项。"""
    with _REGISTRY_LOCK:
        dead = [
            path for path, observer in _OBSERVERS.items()
            if not _observer_alive(observer)
        ]
        for path in dead:
            _OBSERVERS.pop(path, None)
        dirs = sorted(_OBSERVERS)
    if dead:
        _log(f"清理已停止的监听项: {', '.join(dead)}")
    return bool(dirs), ", ".join(dirs)


def _check_start_conflict_locked(key: str) -> str:
    """在持有 _REGISTRY_LOCK 时检查重复与父子目录重叠。"""
    for other in _OBSERVERS:
        if key == other:
            return f"⚠️ 文件监听已在运行中（{other}），无需重复启动。"
        try:
            common = os.path.commonpath([key, other])
        except ValueError:  # 不同盘符，互不重叠
            continue
        if common == other:
            return (
                f"❌ 无法启动监听（{key}）：该目录位于已监听目录 {other} 之内，"
                f"父子目录重叠；请先停止 {other}。"
            )
        if common == key:
            return (
                f"❌ 无法启动监听（{key}）：该目录包含已监听目录 {other}，"
                f"父子目录重叠；请先停止 {other}。"
            )
    return ""


def _purge_dead_locked() -> None:
    for path in [
        p for p, observer in _OBSERVERS.items() if not _observer_alive(observer)
    ]:
        _OBSERVERS.pop(path, None)


def start_watcher(watch_dir: str = "") -> str:
    """启动文件变更监听，自动增量重索引发生变化的文档。

    同一目录重复启动是幂等的；不同目录可追加监听，但拒绝父子目录重叠
    （否则同一事件会被内外两层监听重复处理）。未传 watch_dir 时监听 cwd。
    """
    try:
        from watchdog.observers import Observer
    except ImportError:
        return "❌ 需要 watchdog 库。请安装: pip install watchdog"

    use_cwd = not watch_dir
    if use_cwd:
        watch_dir = os.getcwd()
    watch_dir = os.path.realpath(os.path.abspath(watch_dir))
    key = _normalize_path(watch_dir)

    if not os.path.isdir(watch_dir):
        return f"❌ 目录不存在: {watch_dir}"

    with _REGISTRY_LOCK:
        _purge_dead_locked()
        conflict = _check_start_conflict_locked(key)
    if conflict:
        return conflict

    handler = _AutoIndexHandler(watch_dir)
    observer = Observer()
    try:
        observer.schedule(handler, watch_dir, recursive=True)
        observer.daemon = True
        observer.start()
    except Exception as e:
        _stop_observer(observer, watch_dir)
        return f"❌ 文件监听启动失败（{watch_dir}）: {e}"

    with _REGISTRY_LOCK:
        # observer 启动期间可能有并发 start，插入前再检查一次
        conflict = _check_start_conflict_locked(key)
        if conflict:
            should_stop = True
        else:
            _OBSERVERS[key] = observer
            should_stop = False
            count = len(_OBSERVERS)
    if should_stop:
        _stop_observer(observer, watch_dir)
        return conflict

    lines = [
        "✅ **文件监听已启动**",
        "─" * 40,
        f"监听目录: {watch_dir}"
        + ("（未指定 watch_dir，默认监听当前工作目录）" if use_cwd else ""),
        f"当前监听目录数: {count}",
        "文件修改/新增将自动增量重索引，删除/移动会清理旧索引。",
        "💡 使用 stop_watcher 停止监听（不传目录则停止全部）。",
    ]
    return "\n".join(lines)


def _stop_observer(observer, watch_dir: str) -> bool:
    """停止并等待 observer；join 后仍存活则返回 False。"""
    try:
        observer.stop()
        observer.join(timeout=5)
    except Exception as e:
        _log(f"停止监听失败 {watch_dir}: {e}")
        return False
    if _observer_alive(observer):
        _log(f"停止监听超时，observer 仍在运行: {watch_dir}")
        return False
    return True


def stop_watcher(watch_dir: str = "") -> str:
    """停止文件变更监听：空参停止全部，指定目录只停该目录。

    停止失败的 observer 会保留在 registry 中，便于用户重试 stop_watcher；
    不会因为一次 join 超时就丢失停止它的入口。
    """
    if not watch_dir:
        with _REGISTRY_LOCK:
            _purge_dead_locked()
            targets = sorted(_OBSERVERS.items())
        if not targets:
            return "文件监听未在运行。"

        stopped: list[str] = []
        failed: list[str] = []
        for path, observer in targets:
            if _stop_observer(observer, path):
                with _REGISTRY_LOCK:
                    if _OBSERVERS.get(path) is observer:
                        _OBSERVERS.pop(path, None)
                stopped.append(path)
            else:
                failed.append(path)

        if not failed:
            return f"🛑 文件监听已停止（{', '.join(stopped)}）。"
        lines = []
        if stopped:
            lines.append(f"🛑 已停止: {', '.join(stopped)}")
        lines.append(
            f"❌ 停止失败，仍保留监听项: {', '.join(failed)}"
            "（join 超时，observer 仍在运行；可重试 stop_watcher）"
        )
        return "\n".join(lines)

    target = _normalize_path(watch_dir)
    with _REGISTRY_LOCK:
        _purge_dead_locked()
        observer = _OBSERVERS.get(target)
    if observer is None:
        return f"文件监听未在运行（{target}）。"
    if not _stop_observer(observer, target):
        return (
            f"❌ 文件监听停止失败（{target}）：join 超时，observer 仍在运行；"
            "已保留注册项，可稍后重试。"
        )

    with _REGISTRY_LOCK:
        if _OBSERVERS.get(target) is observer:
            _OBSERVERS.pop(target, None)
        remaining = sorted(_OBSERVERS)

    message = f"🛑 文件监听已停止（{target}）。"
    if remaining:
        message += f"\n仍在监听: {', '.join(remaining)}"
    return message
