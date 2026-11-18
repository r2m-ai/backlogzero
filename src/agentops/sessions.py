"""会话登记 sessions.jsonl：追加写，最后一条记录代表当前状态（C2）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from types import MappingProxyType

from agentops.errors import AgentopsError
from agentops.jsonl import append_line, read_records
from agentops.paths import sessions_file
from agentops.timeutil import now, now_iso, parse_iso

RUNNING = "running"
ENDED = "ended"


@dataclass(frozen=True)
class SessionRecord:
    ts: str
    session_id: str
    status: str
    repo: str | None = None
    item: int | None = None
    branch: str | None = None
    worktree: str | None = None
    mode: str | None = None
    name: str | None = None
    rc: int | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SessionRecord:
        return cls(**data)  # type: ignore[arg-type]


def _append(record: SessionRecord) -> SessionRecord:
    append_line(sessions_file(), json.dumps(record.to_dict(), ensure_ascii=False))
    return record


def register(
    session_id: str,
    *,
    repo: str | None,
    item: int | None,
    branch: str | None = None,
    worktree: str | None = None,
    mode: str = "interactive",
    name: str | None = None,
) -> SessionRecord:
    return _append(
        SessionRecord(
            ts=now_iso(),
            session_id=session_id,
            status=RUNNING,
            repo=repo,
            item=item,
            branch=branch,
            worktree=worktree,
            mode=mode,
            name=name,
        )
    )


def all_records() -> tuple[SessionRecord, ...]:
    return read_records(sessions_file(), SessionRecord.from_dict)


def latest_by_id() -> Mapping[str, SessionRecord]:
    latest: dict[str, SessionRecord] = {}
    for record in all_records():
        latest[record.session_id] = record
    return MappingProxyType(latest)


def active(*, stale_after_hours: float | None = None) -> tuple[SessionRecord, ...]:
    """运行中的会话。stale_after_hours 给出时，超过这么久没有新记录的会话视为陈旧，不计在内。"""
    running = [r for r in latest_by_id().values() if r.status == RUNNING]
    if stale_after_hours is None:
        return tuple(running)
    cutoff = now() - timedelta(hours=stale_after_hours)
    return tuple(r for r in running if parse_iso(r.ts) >= cutoff)


def prune(*, stale_after_hours: float) -> tuple[SessionRecord, ...]:
    """把陈旧的运行中会话标记为结束（rc 为空），返回被清理的记录。"""
    cutoff = now() - timedelta(hours=stale_after_hours)
    pruned = []
    for record in latest_by_id().values():
        if record.status == RUNNING and parse_iso(record.ts) < cutoff:
            pruned.append(_append(replace(record, ts=now_iso(), status=ENDED, rc=None)))
    return tuple(pruned)


def end(session_id: str, *, rc: int | None = None, cost_usd: float | None = None) -> SessionRecord:
    latest = latest_by_id().get(session_id)
    if latest is None:
        raise AgentopsError(f"没有登记过会话 {session_id!r}")
    return _append(replace(latest, ts=now_iso(), status=ENDED, rc=rc, cost_usd=cost_usd))
