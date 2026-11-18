"""时间工具：带时区的 ISO 时间戳和 7d/24h 这类时长解析。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def now() -> datetime:
    return datetime.now(UTC).astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="milliseconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def parse_duration(text: str) -> timedelta:
    """把 '7d'、'24h'、'30m'、'10s' 解析成 timedelta。"""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"时长格式应为 <数字><s|m|h|d>，例如 7d，得到 {text!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])
