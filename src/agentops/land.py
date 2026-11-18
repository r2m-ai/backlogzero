"""ao land（会话侧）：把当前克隆的分支和 SHA 投递到 inbox。沙箱里写不了队列和事件，只写 inbox。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agentops import clones, events, queue
from agentops.config import Config
from agentops.errors import AgentopsError


def land(config: Config, *, cwd: Path, session: str | None = None) -> queue.LandRequest:
    repo_env, item_env = os.environ.get("AGENTOPS_REPO"), os.environ.get("AGENTOPS_ITEM")
    parsed = clones.parse_clone_path(cwd, config)
    if repo_env and item_env and item_env.isdigit():
        repo_name, item_id = repo_env, int(item_env)
    elif parsed is not None:
        repo_name, item_id = parsed
    else:
        raise AgentopsError(
            f"{cwd} 不是工作项的克隆目录，也没有 AGENTOPS_REPO/AGENTOPS_ITEM 环境变量"
        )
    if not (cwd / ".git").exists():
        raise AgentopsError(f"{cwd} 不是 git 仓库")
    branch = clones.current_branch(cwd)
    if not branch or branch == "main":
        raise AgentopsError("当前不在工作分支上（在 main 或游离 HEAD），不能送入队列")
    if clones.is_dirty(cwd):
        raise AgentopsError("工作区有未提交的改动，先提交再 ao land")
    request = queue.new_request(
        repo=repo_name,
        item=item_id,
        branch=branch,
        sha=clones.head_sha(cwd),
        clone_path=str(cwd.resolve()),
        session=session
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("AGENTOPS_SESSION"),
    )
    queue.submit_inbox(request)
    try:  # 沙箱里 events.jsonl 通常不可写；steward 入队时会正式写 land_requested
        events.emit(
            "land_submitted",
            repo=repo_name,
            item=item_id,
            session=request.session,
            detail={"sha": request.sha},
        )
    except OSError:
        pass
    print(
        f"已投递合入请求 {request.id}（分支 {branch}，{request.sha[:7]}）。"
        "steward 每轮读取 inbox 入队。",
        file=sys.stderr,
    )
    return request
