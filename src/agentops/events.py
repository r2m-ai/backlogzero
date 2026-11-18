"""事件日志 events.jsonl：每行 {ts, repo, item, session, type, detail}（C7）。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from agentops.jsonl import append_line, read_records
from agentops.paths import events_file
from agentops.timeutil import now_iso, parse_iso


@dataclass(frozen=True)
class Event:
    ts: str
    type: str
    repo: str | None = None
    item: int | None = None
    session: str | None = None
    detail: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def time(self) -> datetime:
        return parse_iso(self.ts)

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "repo": self.repo,
            "item": self.item,
            "session": self.session,
            "type": self.type,
            "detail": dict(self.detail),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Event:
        ts = str(data["ts"])
        if parse_iso(ts).tzinfo is None:
            raise ValueError(f"ts 缺少时区: {ts}")
        detail = data.get("detail") or {}
        if not isinstance(detail, dict):
            raise ValueError("detail 必须是对象")
        item = data.get("item")
        return cls(
            ts=ts,
            type=str(data["type"]),
            repo=None if data.get("repo") is None else str(data["repo"]),
            item=None if item is None else int(item),  # type: ignore[arg-type]
            session=None if data.get("session") is None else str(data["session"]),
            detail=MappingProxyType(dict(detail)),
        )


def emit(
    type: str,
    *,
    repo: str | None = None,
    item: int | None = None,
    session: str | None = None,
    detail: Mapping[str, object] | None = None,
    path: Path | None = None,
) -> Event:
    event = Event(
        ts=now_iso(),
        type=type,
        repo=repo,
        item=item,
        session=session,
        detail=MappingProxyType(dict(detail or {})),
    )
    append_line(path or events_file(), event.to_json())
    return event


def read_events(path: Path | None = None) -> tuple[Event, ...]:
    return read_records(path or events_file(), Event.from_dict)


def filter_events(
    events: Iterable[Event],
    *,
    since: datetime | None = None,
    types: Iterable[str] | None = None,
    repo: str | None = None,
    item: int | None = None,
    session: str | None = None,
) -> tuple[Event, ...]:
    wanted = None if types is None else frozenset(types)
    return tuple(
        event
        for event in events
        if (since is None or event.time >= since)
        and (wanted is None or event.type in wanted)
        and (repo is None or event.repo == repo)
        and (item is None or event.item == item)
        and (session is None or event.session == session)
    )
