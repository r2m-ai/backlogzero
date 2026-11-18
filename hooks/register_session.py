#!/usr/bin/env python3.12
"""SessionStart hook：登记会话（session_id、工作项、分支、worktree），并把工作项提示放进上下文。

Codex 下 stdin 的 session_id 是 thread id；ao start 起的会话用 ao 自己的会话 id 登记（见 _common）。
纯文本 stdout 在 Claude Code 和 Codex 的 SessionStart 里都会成为模型上下文。
"""

from __future__ import annotations

import subprocess
import sys

from _common import AO, context, emit, git, load_policy, read_input, session_item_args


def main() -> int:
    data = read_input()
    if data.get("source") not in (None, "startup", "resume"):
        return 0
    ctx = context(data)
    if not ctx.session:
        return 0
    policy = load_policy(ctx.project)
    branch = git(ctx.project, "branch", "--show-current").stdout.strip() or None
    args = [
        AO,
        "session",
        "register",
        "--session-id",
        ctx.session,
        "--repo",
        policy["repo"],
        "--worktree",
        str(ctx.project),
        "--mode",
        ctx.mode,
        *session_item_args(ctx.item),
    ]
    if branch:
        args += ["--branch", branch]
    try:
        subprocess.run(args, check=False, timeout=15, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"hook 警告: 会话登记失败: {exc}", file=sys.stderr)
    if not ctx.managed:  # ao start 自己会写 session_start，不重复
        emit(
            "session_start",
            repo=policy["repo"],
            session=ctx.session,
            item=ctx.item,
            kv={"mode": ctx.mode, "branch": branch or ""},
        )

    if ctx.item:
        ref = f"{policy['repo']}#{ctx.item}"
        print(f"agentops: 本会话负责工作项 {ref}，分支 {branch}。做完后执行 `ao land`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
