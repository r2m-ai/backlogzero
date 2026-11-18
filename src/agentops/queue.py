"""合并队列（C3）：inbox 收会话的合入请求，journal 是只追加的队列日志，steward 是唯一写者。

会话在沙箱里只能写 ~/.agentops/inbox/，所以 `ao land` 只投递请求文件；steward 读 inbox 入队。
journal 每行 {ts, id, status, request, detail}，一个请求的最新一行就是它的当前状态。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType

from agentops import events
from agentops.jsonl import append_line, read_records
from agentops.paths import inbox_dir, queue_file
from agentops.timeutil import now_iso

PENDING = frozenset({"queued", "requeued"})
STATUSES = (
    "queued",
    "processing",
    "requeued",
    "landed",
    "returned",
    "needs_human",
    "dropped",
    "error",
)


@dataclass(frozen=True)
class LandRequest:
    id: str
    repo: str
    item: int
    branch: str
    sha: str
    clone_path: str
    session: str | None
    requested_at: str

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> LandRequest:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass(frozen=True)
class JournalLine:
    ts: str
    id: str
    status: str
    request: LandRequest
    detail: Mapping[str, object]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> JournalLine:
        return cls(
            ts=str(data["ts"]),
            id=str(data["id"]),
            status=str(data["status"]),
            request=LandRequest.from_dict(data["request"]),  # type: ignore[arg-type]
            detail=MappingProxyType(dict(data.get("detail") or {})),  # type: ignore[arg-type]
        )


def new_request(
    *, repo: str, item: int, branch: str, sha: str, clone_path: str, session: str | None
) -> LandRequest:
    ts = now_iso()
    return LandRequest(
        id=f"{repo}#{item}@{sha[:7]}-{ts.replace(':', '').replace('.', '')}",
        repo=repo,
        item=item,
        branch=branch,
        sha=sha,
        clone_path=clone_path,
        session=session,
        requested_at=ts,
    )


def land_inbox_dir() -> Path:
    return inbox_dir() / "land"


def submit_inbox(request: LandRequest) -> Path:
    """会话侧：把合入请求放进 inbox。文件名带 repo 和 item，steward 按时间顺序处理。"""
    target = land_inbox_dir() / f"{request.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(asdict(request), ensure_ascii=False), encoding="utf-8")
    temp.replace(target)
    return target


def append(
    repo: str, request: LandRequest, status: str, detail: Mapping[str, object] | None = None
) -> None:
    if status not in STATUSES:
        raise ValueError(f"未知队列状态 {status!r}")
    line = {
        "ts": now_iso(),
        "id": request.id,
        "status": status,
        "request": asdict(request),
        "detail": dict(detail or {}),
    }
    append_line(queue_file(repo), json.dumps(line, ensure_ascii=False))


def journal(repo: str) -> tuple[JournalLine, ...]:
    return read_records(queue_file(repo), JournalLine.from_dict)


def latest(repo: str) -> Mapping[str, JournalLine]:
    return MappingProxyType({line.id: line for _, line in _latest_with_index(repo)})


def _latest_with_index(repo: str) -> tuple[tuple[int, JournalLine], ...]:
    result: dict[str, tuple[int, JournalLine]] = {}
    for index, line in enumerate(journal(repo)):
        result[line.id] = (index, line)
    return tuple(result.values())


def pending(repo: str) -> tuple[LandRequest, ...]:
    """先进先出：按最新一行的（时间，日志行号）排序，requeued 的自然排到队尾。"""
    lines = [(index, line) for index, line in _latest_with_index(repo) if line.status in PENDING]
    return tuple(line.request for _, line in sorted(lines, key=lambda pair: (pair[1].ts, pair[0])))


def ingest_inbox(repo: str) -> tuple[LandRequest, ...]:
    """steward 侧：把 inbox 里属于 repo 的请求入队并删除文件，写 land_requested 事件。"""
    root = land_inbox_dir()
    if not root.exists():
        return ()
    taken: list[LandRequest] = []
    for path in sorted(root.glob("*.json")):
        try:
            request = LandRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError) as exc:
            bad = path.with_suffix(".bad")
            path.replace(bad)
            events.emit("inbox_rejected", repo=repo, detail={"file": str(bad), "error": str(exc)})
            continue
        if request.repo != repo:
            continue
        append(repo, request, "queued")
        events.emit(
            "land_requested",
            repo=repo,
            item=request.item,
            session=request.session,
            detail={"branch": request.branch, "sha": request.sha, "clone": request.clone_path},
        )
        path.unlink()
        taken.append(request)
    return tuple(taken)


def recover_processing(repo: str) -> tuple[LandRequest, ...]:
    """steward 启动时把上次没处理完（processing）的请求重新排队。流程是幂等的：
    已快进过的分支再处理一遍会得到空 diff 并正常记 landed。"""
    recovered = []
    for line in latest(repo).values():
        if line.status == "processing":
            append(repo, line.request, "requeued", {"recovered": True, "reason": "上次处理未完成"})
            events.emit("recovered", repo=repo, item=line.request.item, detail={"request": line.id})
            recovered.append(line.request)
    return tuple(recovered)
