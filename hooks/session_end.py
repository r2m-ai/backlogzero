#!/usr/bin/env python3.12
"""SessionEnd hook：会话真正退出时标记结束。

Stop 只是一次回答结束，交互会话还会继续，不能在那里释放并发。
Codex 给 SessionEnd 的时间上限是 3 秒，所以 Codex 下只调一次 ao，并把子进程超时压到 2 秒。
"""

from __future__ import annotations

import subprocess
import sys

from _common import AO, EMIT_TIMEOUT, context, emit, load_policy, read_input

CODEX_TIMEOUT = 2


def _end_session(session: str, timeout: float) -> None:
    try:
        subprocess.run(
            [AO, "session", "end", "--session-id", session],
            check=False,
            timeout=timeout,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"hook 警告: 标记会话结束失败: {exc}", file=sys.stderr)


def main() -> int:
    data = read_input()
    ctx = context(data)
    if not ctx.session:
        return 0
    policy = load_policy(ctx.project)
    timeout = CODEX_TIMEOUT if ctx.runtime == "codex" else EMIT_TIMEOUT
    by_ao = ctx.mode == "headless" or (ctx.runtime == "codex" and ctx.managed)
    if ctx.runtime == "codex" and not by_ao:
        _end_session(ctx.session, timeout)  # 只够调一次：ao session end 自己会写 session_end 事件
        return 0
    emit(
        "session_exit",
        repo=policy["repo"],
        session=ctx.session,
        item=ctx.item,
        kv={"reason": str(data.get("reason", ""))},
        timeout=timeout,
    )
    if by_ao:
        return 0  # 无人值守由 ao start 带成本记 end
    _end_session(ctx.session, timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
