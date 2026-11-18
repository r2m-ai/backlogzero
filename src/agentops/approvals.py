"""审批记录（C6）：只有人类通道（看板按钮、人在终端）能写；绑定 (repo, item, sha)。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from agentops import events
from agentops.errors import AgentopsError
from agentops.paths import approvals_dir
from agentops.timeutil import now_iso

# 任一存在就视为在 agent 会话里：Claude Code 的两个，Codex 会话 shell 里的两个
SESSION_ENV_MARKERS = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX",
)
VALID_SOURCES = ("terminal", "board")


@dataclass(frozen=True)
class Approval:
    repo: str
    item: int
    sha: str
    by: str
    ts: str
    note: str = ""


def in_agent_session() -> bool:
    return any(os.environ.get(name) for name in SESSION_ENV_MARKERS)


def approval_path(repo: str, item: int, sha: str) -> Path:
    return approvals_dir() / f"{repo}-{item}-{sha}.json"


def record(repo: str, item: int, sha: str, *, by: str, note: str = "") -> Approval:
    """写审批记录。在 agent 会话（Claude Code / Codex）内执行一律拒绝，这是人类通道的硬规则。"""
    if in_agent_session():
        raise AgentopsError(
            "ao approve 不能在 Claude Code / Codex 会话里执行：审批只认看板按钮或人在终端"
        )
    if by not in VALID_SOURCES:
        raise AgentopsError(f"审批来源只能是 {'/'.join(VALID_SOURCES)}，得到 {by!r}")
    if len(sha) < 7:
        raise AgentopsError("sha 至少 7 位")
    approval = Approval(repo=repo, item=item, sha=sha, by=by, ts=now_iso(), note=note)
    path = approval_path(repo, item, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(approval), ensure_ascii=False, indent=2), encoding="utf-8")
    events.emit("approved", repo=repo, item=item, detail={"sha": sha, "by": by, "note": note})
    return approval


def find(repo: str, item: int, sha: str) -> Approval | None:
    """精确匹配完整 sha；也接受以记录里的 sha 为前缀的情况（人可能只给了短 sha）。"""
    root = approvals_dir()
    if not root.exists():
        return None
    for path in root.glob(f"{repo}-{item}-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            approval = Approval(**data)
        except (ValueError, TypeError):
            continue
        if sha.startswith(approval.sha) or approval.sha.startswith(sha):
            return approval
    return None


def is_approved(repo: str, item: int, sha: str) -> bool:
    return find(repo, item, sha) is not None
