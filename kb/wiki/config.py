# -*- coding: utf-8 -*-
"""Wiki 配置与 vault 路径边界校验。

所有对外路径在构造时统一解析为绝对路径（realpath + abspath）。
``ensure_within`` 拒绝含 ``..`` 段的路径、跨盘路径与符号链接逃逸。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

from . import constants as C


class WikiConfigError(ValueError):
    """Wiki 配置值非法。"""


class WikiPathError(WikiConfigError):
    """路径越出 vault 边界或包含不允许的路径段。"""


class WikiDisabledError(RuntimeError):
    """WIKI_ENABLED=0 时调用需要启用 Wiki 的接口。"""


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_PATH_SEPARATOR_RE = re.compile(r"[\\/]+")


def _resolve_abs(path: str | os.PathLike[str]) -> str:
    """展开 ~、转绝对路径并解析符号链接。"""
    raw = os.fspath(path)
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    return os.path.realpath(os.path.abspath(os.path.expanduser(raw)))


def _has_parent_segment(path: str | os.PathLike[str]) -> bool:
    return ".." in _PATH_SEPARATOR_RE.split(os.fspath(path))


def ensure_within(root: str | os.PathLike[str], candidate: str | os.PathLike[str]) -> str:
    """确保 ``candidate`` 位于 ``root`` 内，返回 candidate 的绝对规范路径。

    相对 candidate 按 root 解析（便于校验 vault 相对路径）。
    拒绝 ``..`` 段（即使归一化后仍在 root 内）、跨盘路径（commonpath ValueError）
    与经符号链接/junction 逃逸。root 本身允许（返回 root 的绝对路径）。
    """
    if candidate is None or os.fspath(candidate) == "":
        raise WikiPathError("候选路径不能为空")
    if _has_parent_segment(candidate):
        raise WikiPathError(f"路径包含 '..' 段，已拒绝: {os.fspath(candidate)}")

    root_abs = _resolve_abs(root)
    candidate_raw = os.fspath(candidate)
    if not os.path.isabs(candidate_raw):
        candidate_raw = os.path.join(root_abs, candidate_raw)
    candidate_abs = _resolve_abs(candidate_raw)
    try:
        common = os.path.commonpath([root_abs, candidate_abs])
    except ValueError as exc:  # Windows 跨盘
        raise WikiPathError(
            f"跨盘路径不在 vault 内: {candidate_abs} (root={root_abs})"
        ) from exc

    if os.path.normcase(os.path.normpath(common)) != os.path.normcase(
        os.path.normpath(root_abs)
    ):
        raise WikiPathError(
            f"路径逃出 vault 边界: {candidate_abs} (root={root_abs})"
        )
    return candidate_abs


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise WikiConfigError(f"{name} 必须是 0/1（当前值: {raw!r}）")


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise WikiConfigError(f"{name} 必须是整数（当前值: {raw!r}）") from exc


def _parse_float(name: str, raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise WikiConfigError(f"{name} 必须是数字（当前值: {raw!r}）") from exc


@dataclass(frozen=True, slots=True)
class WikiConfig:
    """Wiki 运行配置；``from_env()`` 是推荐入口。"""

    enabled: bool = False
    vault_path: str = C.DEFAULT_VAULT_PATH
    page_root: str = C.DEFAULT_PAGE_ROOT
    db_path: str = ""
    page_index_table: str = C.DEFAULT_PAGE_INDEX_TABLE
    auto_publish: bool = C.DEFAULT_AUTO_PUBLISH
    require_evidence: bool = C.DEFAULT_REQUIRE_EVIDENCE
    min_entity_confidence: float = C.DEFAULT_MIN_ENTITY_CONFIDENCE
    min_relation_confidence: float = C.DEFAULT_MIN_RELATION_CONFIDENCE
    max_auto_page_updates: int = C.DEFAULT_MAX_AUTO_PAGE_UPDATES
    db_busy_timeout_ms: int = C.DEFAULT_DB_BUSY_TIMEOUT_MS
    git_commit: bool = C.DEFAULT_GIT_COMMIT

    def __post_init__(self) -> None:
        vault_abs = _resolve_abs(self.vault_path or C.DEFAULT_VAULT_PATH)
        object.__setattr__(self, "vault_path", vault_abs)

        page_root_raw = self.page_root or C.DEFAULT_PAGE_ROOT
        if _has_parent_segment(page_root_raw):
            raise WikiPathError(f"WIKI_PAGE_ROOT 不能包含 '..': {page_root_raw!r}")
        page_root_candidate = (
            page_root_raw
            if os.path.isabs(page_root_raw)
            else os.path.join(vault_abs, page_root_raw)
        )
        page_root_abs = ensure_within(vault_abs, page_root_candidate)
        object.__setattr__(self, "page_root", page_root_abs)

        db_path_raw = self.db_path or os.path.join(
            vault_abs, C.DEFAULT_DB_SUBDIR, C.DEFAULT_DB_FILENAME
        )
        object.__setattr__(self, "db_path", _resolve_abs(db_path_raw))

        if not isinstance(self.page_index_table, str) or not self.page_index_table.strip():
            raise WikiConfigError("WIKI_PAGE_INDEX_TABLE 不能为空")
        object.__setattr__(self, "page_index_table", self.page_index_table.strip())

        for flag_name in ("enabled", "auto_publish", "require_evidence", "git_commit"):
            object.__setattr__(self, flag_name, bool(getattr(self, flag_name)))

        for confidence_name in ("min_entity_confidence", "min_relation_confidence"):
            try:
                confidence = float(getattr(self, confidence_name))
            except (TypeError, ValueError) as exc:
                raise WikiConfigError(f"{confidence_name} 必须是 0..1 的数字") from exc
            if not 0.0 <= confidence <= 1.0:
                raise WikiConfigError(f"{confidence_name} 必须在 0..1 之间")
            object.__setattr__(self, confidence_name, confidence)

        for int_name, allow_zero in (
            ("max_auto_page_updates", True),
            ("db_busy_timeout_ms", True),
        ):
            try:
                value = int(getattr(self, int_name))
            except (TypeError, ValueError) as exc:
                raise WikiConfigError(f"{int_name} 必须是非负整数") from exc
            if value < 0 or (not allow_zero and value == 0):
                raise WikiConfigError(f"{int_name} 必须是非负整数")
            object.__setattr__(self, int_name, value)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WikiConfig":
        """从环境变量构造配置；未设置的项使用默认值。"""
        if env is None:
            env = os.environ

        vault_raw = _env_value(env, "WIKI_VAULT_PATH") or C.DEFAULT_VAULT_PATH
        page_root_raw = _env_value(env, "WIKI_PAGE_ROOT") or C.DEFAULT_PAGE_ROOT
        db_path_raw = _env_value(env, "WIKI_DB_PATH")
        page_index_table = (
            _env_value(env, "WIKI_PAGE_INDEX_TABLE") or C.DEFAULT_PAGE_INDEX_TABLE
        )

        return cls(
            enabled=_parse_bool(
                "WIKI_ENABLED", _env_value(env, "WIKI_ENABLED"), False
            ),
            vault_path=vault_raw,
            page_root=page_root_raw,
            db_path=db_path_raw or "",
            page_index_table=page_index_table,
            auto_publish=_parse_bool(
                "WIKI_AUTO_PUBLISH",
                _env_value(env, "WIKI_AUTO_PUBLISH"),
                C.DEFAULT_AUTO_PUBLISH,
            ),
            require_evidence=_parse_bool(
                "WIKI_REQUIRE_EVIDENCE",
                _env_value(env, "WIKI_REQUIRE_EVIDENCE"),
                C.DEFAULT_REQUIRE_EVIDENCE,
            ),
            min_entity_confidence=_parse_float(
                "WIKI_MIN_ENTITY_CONFIDENCE",
                _env_value(env, "WIKI_MIN_ENTITY_CONFIDENCE"),
                C.DEFAULT_MIN_ENTITY_CONFIDENCE,
            ),
            min_relation_confidence=_parse_float(
                "WIKI_MIN_RELATION_CONFIDENCE",
                _env_value(env, "WIKI_MIN_RELATION_CONFIDENCE"),
                C.DEFAULT_MIN_RELATION_CONFIDENCE,
            ),
            max_auto_page_updates=_parse_int(
                "WIKI_MAX_AUTO_PAGE_UPDATES",
                _env_value(env, "WIKI_MAX_AUTO_PAGE_UPDATES"),
                C.DEFAULT_MAX_AUTO_PAGE_UPDATES,
            ),
            db_busy_timeout_ms=_parse_int(
                "WIKI_DB_BUSY_TIMEOUT_MS",
                _env_value(env, "WIKI_DB_BUSY_TIMEOUT_MS"),
                C.DEFAULT_DB_BUSY_TIMEOUT_MS,
            ),
            git_commit=_parse_bool(
                "WIKI_GIT_COMMIT",
                _env_value(env, "WIKI_GIT_COMMIT"),
                C.DEFAULT_GIT_COMMIT,
            ),
        )

    @property
    def page_root_path(self) -> str:
        """页面根目录绝对路径（page_root 已绝对化，保留该别名便于阅读）。"""
        return self.page_root

    def page_subdir(self, page_type: str) -> str:
        """返回页面类型对应的 vault 子目录名。"""
        try:
            return C.PAGE_TYPE_SUBDIRS[page_type]
        except KeyError as exc:
            allowed = ", ".join(C.PAGE_TYPES)
            raise ValueError(
                f"未知 page_type: {page_type!r}（允许: {allowed}）"
            ) from exc

    def page_type_dir(self, page_type: str) -> str:
        """返回页面类型子目录的绝对路径，并再次校验 vault 边界。"""
        subdir = self.page_subdir(page_type)
        return ensure_within(self.vault_path, os.path.join(self.page_root, subdir))

    def require_enabled(self) -> None:
        """WIKI_ENABLED=0 时抛出清晰的禁用异常。"""
        if not self.enabled:
            raise WikiDisabledError(
                "Wiki 功能未启用（WIKI_ENABLED=0）；"
                "设置 WIKI_ENABLED=1 并重启服务后重试。"
            )


__all__ = [
    "WikiConfig",
    "WikiConfigError",
    "WikiDisabledError",
    "WikiPathError",
    "ensure_within",
]
