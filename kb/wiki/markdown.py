# -*- coding: utf-8 -*-
"""Markdown frontmatter 与 wiki 链接的纯解析工具（不做文件 IO）。

安全 YAML：优先 ``yaml.safe_load``；无 PyYAML 时使用仅支持标量/行内列表的
极简解析器，绝不回退到 unsafe load。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from . import constants as C

try:  # PyYAML 为可选依赖
    import yaml as _yaml
except ImportError:  # pragma: no cover - 当前环境有 PyYAML，逻辑仍需保留
    _yaml = None

_LINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$"
)
_KEY_RE = re.compile(r"^[^\s:#\[\]{},]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRUE_STRINGS = {"true", "yes", "on"}
_FALSE_STRINGS = {"false", "no", "off"}
_NULL_STRINGS = {"null", "~"}


class FrontmatterError(ValueError):
    """frontmatter 缺失结束分隔符、YAML 非法或顶层不是映射。"""


class FrontmatterValidationError(ValueError):
    """frontmatter 字段未满足 vault/wiki 契约。"""

    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        self.errors = tuple(errors)
        super().__init__("frontmatter 校验失败: " + "; ".join(self.errors))


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text


def _load_yaml_mapping(frontmatter_text: str) -> dict[str, Any]:
    if _yaml is not None:
        try:
            data = _yaml.safe_load(frontmatter_text)
        except Exception as exc:  # yaml.YAMLError 及潜在的解析异常
            raise FrontmatterError(f"YAML 解析失败: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise FrontmatterError("frontmatter 顶层必须是 YAML 映射（key: value）")
        return data

    return _parse_simple_yaml(frontmatter_text)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 ``---`` 包裹的 frontmatter，返回 (data, body)。

    文件不以 ``---`` 开头时返回 (``{}``, 原文)，便于读取普通 Markdown；
    一旦以 ``---`` 开头却缺少结束分隔符，则抛 ``FrontmatterError``。
    body 保留结束分隔符之后的原始内容（含换行）。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    text = _strip_bom(text)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != C.FRONTMATTER_DELIMITER:
        return {}, text

    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == C.FRONTMATTER_DELIMITER:
            closing_index = index
            break
    if closing_index is None:
        raise FrontmatterError("frontmatter 缺少结束分隔符 '---'")

    frontmatter_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    return _load_yaml_mapping(frontmatter_text), body


def _normalize_for_dump(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            _normalize_for_dump(key): _normalize_for_dump(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_for_dump(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_normalize_for_dump(item) for item in value]
    return value


def render_frontmatter(data: Mapping[str, Any], body: str = "") -> str:
    """把映射渲染为 ``---`` frontmatter + body（固定使用 ``---`` 分隔）。"""
    if not isinstance(data, Mapping):
        raise TypeError("data 必须是映射")
    if not isinstance(body, str):
        raise TypeError("body 必须是字符串")

    normalized = _normalize_for_dump(data)
    if _yaml is not None:
        try:
            yaml_text = _yaml.safe_dump(
                dict(normalized),
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).rstrip("\n")
        except Exception as exc:
            raise FrontmatterError(f"frontmatter 无法序列化为 YAML: {exc}") from exc
        if yaml_text == "{}":
            yaml_text = ""
    else:
        yaml_text = _dump_simple_yaml(normalized)

    if yaml_text:
        return (
            f"{C.FRONTMATTER_DELIMITER}\n"
            f"{yaml_text}\n"
            f"{C.FRONTMATTER_DELIMITER}\n"
            f"{body}"
        )
    return f"{C.FRONTMATTER_DELIMITER}\n{C.FRONTMATTER_DELIMITER}\n{body}"


def _is_valid_date(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return True
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _require_nonempty_string(
    data: Mapping[str, Any], key: str, errors: list[str]
) -> None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} 缺失或不是非空字符串")


def _require_string_list(
    data: Mapping[str, Any], key: str, errors: list[str]
) -> None:
    value = data.get(key)
    if not isinstance(value, list):
        errors.append(f"{key} 缺失或不是列表")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{key}[{index}] 不是非空字符串")


def _require_timestamp(
    data: Mapping[str, Any], key: str, errors: list[str]
) -> None:
    value = data.get(key)
    if isinstance(value, str):
        if not value.strip():
            errors.append(f"{key} 缺失或不是非空时间字符串")
        return
    if isinstance(value, (date, datetime)):
        return
    errors.append(f"{key} 缺失或不是日期/时间字符串")


def validate_page_frontmatter(
    data: Mapping[str, Any], *, require_wiki: bool = True
) -> dict[str, Any]:
    """校验 vault 必需字段；默认同时校验 wiki 追加字段并强制 ``type=permanent``。

    可选字段 ``sources``（``list[str]``，可为空）出现时校验为字符串列表，
    不出现时不强制。返回浅拷贝后的 data；不修改入参。``require_wiki=False``
    时只校验 vault 契约，供普通 Markdown 页面复用。
    """
    if not isinstance(data, Mapping):
        raise FrontmatterValidationError(["frontmatter 顶层必须是映射"])

    errors: list[str] = []

    _require_nonempty_string(data, "title", errors)
    _require_string_list(data, "tags", errors)
    _require_nonempty_string(data, "type", errors)
    _require_nonempty_string(data, "summary", errors)

    created = data.get("created")
    if created is None:
        errors.append("created 缺失")
    elif not _is_valid_date(created):
        errors.append("created 必须是 YYYY-MM-DD 日期")

    if "sources" in data:
        _require_string_list(data, "sources", errors)

    if require_wiki:
        _require_nonempty_string(data, "page_id", errors)
        _require_nonempty_string(data, "slug", errors)
        _require_string_list(data, "aliases", errors)
        _require_nonempty_string(data, "revision_id", errors)
        _require_timestamp(data, "updated", errors)

        page_type = data.get("page_type")
        if page_type not in C.PAGE_TYPES:
            allowed = ", ".join(C.PAGE_TYPES)
            errors.append(f"page_type 必须是 {{{allowed}}} 之一")

        status = data.get("status")
        if status not in C.PAGE_STATUSES:
            allowed = ", ".join(C.PAGE_STATUSES)
            errors.append(f"status 必须是 {{{allowed}}} 之一")

        page_type_value = data.get("type")
        if page_type_value != C.WIKI_PAGE_TYPE:
            errors.append(
                f"wiki 页面 type 必须是 {C.WIKI_PAGE_TYPE!r}"
                f"（当前值: {page_type_value!r}）"
            )

    if errors:
        raise FrontmatterValidationError(errors)
    return dict(data)


def split_wiki_links(body: str) -> list[str]:
    """解析 ``[[target]]`` / ``[[target|alias]]``，按出现顺序返回 target 列表。"""
    if not isinstance(body, str):
        raise TypeError("body 必须是字符串")

    targets: list[str] = []
    for match in _LINK_RE.finditer(body):
        target = match.group(1).split("|", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


# =============================================================
# 无 PyYAML 时的极简 YAML（仅标量 + 行内列表）
# =============================================================


def _split_inline_items(inner: str) -> list[str]:
    items: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    for char in inner:
        if quote == '"':
            buffer.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            continue
        if quote == "'":
            buffer.append(char)
            if char == "'":
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            buffer.append(char)
        elif char == ",":
            items.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    if quote is not None:
        raise FrontmatterError("简易 YAML 行内列表引号未闭合")
    items.append("".join(buffer).strip())
    if any(item == "" for item in items):
        raise FrontmatterError("简易 YAML 行内列表包含空元素")
    return items


def _parse_simple_value(value: str, line_number: int) -> Any:
    if value == "":
        return None

    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if inner == "":
            return []
        return [
            _parse_simple_value(item, line_number)
            for item in _split_inline_items(inner)
        ]
    if value.startswith("[") or value.endswith("]"):
        raise FrontmatterError(f"简易 YAML 行内列表未闭合（第 {line_number} 行）")
    if value.startswith("{") or value.startswith("- "):
        raise FrontmatterError(f"简易 YAML 不支持嵌套映射/块列表（第 {line_number} 行）")

    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise FrontmatterError(
                f"简易 YAML 双引号字符串非法（第 {line_number} 行）"
            ) from exc
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")

    lowered = value.lower()
    if lowered in _NULL_STRINGS:
        return None
    if lowered in _TRUE_STRINGS:
        return True
    if lowered in _FALSE_STRINGS:
        return False
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            raise FrontmatterError(
                f"简易 YAML 不支持缩进/嵌套（第 {line_number} 行）"
            )
        if ":" not in stripped:
            raise FrontmatterError(
                f"简易 YAML 行缺少 ':'（第 {line_number} 行）"
            )
        key_part, value_part = stripped.split(":", 1)
        key = key_part.strip()
        if not key or not _KEY_RE.match(key):
            raise FrontmatterError(
                f"简易 YAML key 非法（第 {line_number} 行）: {key!r}"
            )
        data[key] = _parse_simple_value(value_part.strip(), line_number)
    return data


def _needs_quote(value: str) -> bool:
    if value == "" or value != value.strip():
        return True
    lowered = value.lower()
    if lowered in _NULL_STRINGS | _TRUE_STRINGS | _FALSE_STRINGS:
        return True
    if _INT_RE.match(value) or _FLOAT_RE.match(value):
        return True
    if value == C.FRONTMATTER_DELIMITER:
        return True
    if value.startswith(("- ", "? ", ": ")):
        return True
    return any(char in value for char in ':#[]{},&*!|>\'"%@`\n\r\t')


def _dump_simple_value(value: Any) -> str:
    if isinstance(value, Enum):
        return _dump_simple_value(value.value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False) if _needs_quote(value) else value
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_dump_simple_value(item) for item in value) + "]"
    raise FrontmatterError(
        f"简易 YAML 不支持的值类型: {type(value).__name__}"
    )


def _dump_simple_yaml(data: Mapping[Any, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise FrontmatterError("简易 YAML 的 key 必须是非空字符串")
        rendered_key = key if _KEY_RE.match(key) else json.dumps(key, ensure_ascii=False)
        lines.append(f"{rendered_key}: {_dump_simple_value(value)}")
    return "\n".join(lines)


__all__ = [
    "FrontmatterError",
    "FrontmatterValidationError",
    "parse_frontmatter",
    "render_frontmatter",
    "split_wiki_links",
    "validate_page_frontmatter",
]
