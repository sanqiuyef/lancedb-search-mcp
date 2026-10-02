# -*- coding: utf-8 -*-
"""本地模型生命周期：用时挂载、闲置自动卸载（对齐 Ollama keep-alive 行为）。

嵌入与重排模型把权重放在各自的模块级持有器里；本模块统一记录"最近一次使用
时间"，由守护线程在闲置超过 LOCAL_MODEL_IDLE_UNLOAD 秒后执行卸载回调并清空
CUDA 缓存。LOCAL_MODEL_IDLE_UNLOAD=0 表示常驻不卸载。

竞态说明：卸载只发生在闲置超时后（分钟级），单次推理是秒级，正常不会撞上；
极端并发下最坏结果是下一次调用触发一次重新加载。
"""

from __future__ import annotations

import gc
import sys
import threading
import time
from typing import Callable

from . import config as cfg

CHECK_INTERVAL = 15  # 守护线程巡检间隔（秒）

_lock = threading.Lock()
_last_used: float | None = None
_unloaders: dict[str, Callable[[], bool]] = {}
_watchdog_started = False


def register_unloader(name: str, fn: Callable[[], bool]) -> None:
    """注册卸载回调；fn 返回 True 表示确实卸载了已加载的模型。"""
    with _lock:
        _unloaders[name] = fn


def touch() -> None:
    """每次实际使用模型（加载或推理）时调用，刷新闲置计时。"""
    global _last_used, _watchdog_started
    with _lock:
        _last_used = time.monotonic()
        started = _watchdog_started
        _watchdog_started = True
    if not started:
        thread = threading.Thread(target=_watchdog_loop, name="kb-model-unload", daemon=True)
        thread.start()


def maybe_unload(now: float | None = None) -> bool:
    """闲置超时则执行卸载；返回是否发生了卸载。now 参数供测试注入时钟。"""
    timeout = cfg.LOCAL_MODEL_IDLE_UNLOAD
    if timeout <= 0:
        return False
    with _lock:
        last = _last_used
        unloaders = list(_unloaders.items())
    if last is None or (now if now is not None else time.monotonic()) - last <= timeout:
        return False
    unloaded = False
    for name, fn in unloaders:
        try:
            if fn():
                unloaded = True
                print(f"[model] 闲置超过 {timeout}s，已卸载 {name}", file=sys.stderr)
        except Exception as e:  # 卸载失败不阻断其他回调
            print(f"[model] 卸载 {name} 失败: {e}", file=sys.stderr)
    if unloaded:
        _release_cuda_memory()
    return unloaded


def unload_now() -> bool:
    """立即卸载（无论是否闲置）；返回是否卸载了模型。"""
    global _last_used
    with _lock:
        unloaders = list(_unloaders.items())
    unloaded = False
    for name, fn in unloaders:
        try:
            if fn():
                unloaded = True
                print(f"[model] 手动卸载 {name}", file=sys.stderr)
        except Exception as e:
            print(f"[model] 卸载 {name} 失败: {e}", file=sys.stderr)
    if unloaded:
        _release_cuda_memory()
        with _lock:
            _last_used = None
    return unloaded


def _release_cuda_memory() -> None:
    """模型对象存在引用环，需先 gc 再清 CUDA 缓存，显存才确定释放。"""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def _watchdog_loop() -> None:
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            maybe_unload()
        except Exception as e:  # 守护线程永不退出
            print(f"[model] 巡检异常: {e}", file=sys.stderr)
