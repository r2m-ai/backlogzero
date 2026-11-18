"""JSONL 文件的共用读写：带锁追加，坏行跳过并警告。"""

from __future__ import annotations

import fcntl
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

_PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def append_line(path: Path, line: str) -> None:
    """带文件锁的追加写，多个进程同时写也不会串行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_records[T](path: Path, parse: Callable[[Mapping[str, object]], T]) -> tuple[T, ...]:
    """逐行解析。坏行跳过并在 stderr 报一次，不让一条坏记录拖垮读取方。"""
    if not path.exists():
        return ()
    records: list[T] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("不是 JSON 对象")
            records.append(parse(data))
        except _PARSE_ERRORS as exc:
            print(f"警告: {path.name} 第 {number} 行无法解析，已跳过: {exc}", file=sys.stderr)
    return tuple(records)
