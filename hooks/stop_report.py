#!/usr/bin/env python3.12
"""Stop hook：写 session_stop 事件，提醒执行 ao land。

不在这里跑测试，也不在这里标记会话结束（那是 SessionEnd 的事，Stop 只是一次回答结束）。
Codex 的 Stop hook 怎么解读 stdout 没有实测过，所以 Codex 下不输出提醒（提示词里已经写了）。
"""

from __future__ import annotations

import sys

from _common import context, emit, load_policy, read_input


def main() -> int:
    data = read_input()
    ctx = context(data)
    if not ctx.session:
        return 0
    policy = load_policy(ctx.project)
    emit("session_stop", repo=policy["repo"], session=ctx.session, item=ctx.item, kv={})
    if ctx.item and ctx.runtime != "codex":
        print("agentops: 会话已停止。若改动已完成且已提交，请执行 `ao land` 送入合并队列。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
