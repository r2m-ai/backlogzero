"""cli_* 模块共用的小工具。"""

from __future__ import annotations

import json
from collections.abc import Iterable

from agentops.errors import AgentopsError


def parse_int_list(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    try:
        return tuple(int(part) for part in text.replace(" ", "").split(",") if part)
    except ValueError as exc:
        raise AgentopsError(f"应为逗号分隔的整数，得到 {text!r}") from exc


def parse_kv(pairs: Iterable[str] | None) -> dict[str, object]:
    """把 k=v 列表解析成字典；值能解析成 JSON 就用 JSON，否则当字符串。"""
    result: dict[str, object] = {}
    for pair in pairs or ():
        if "=" not in pair:
            raise AgentopsError(f"--kv 需要 key=value 形式，得到 {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def parse_json_object(text: str | None) -> dict[str, object]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentopsError(f"--detail 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentopsError("--detail 必须是 JSON 对象")
    return data


def none_if_literal_none(value: str | None) -> str | None:
    return None if value is None or value.lower() == "none" else value
