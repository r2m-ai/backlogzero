"""ao start | resume"""

from __future__ import annotations

import argparse

from agentops import start
from agentops import workitems as wi
from agentops.config import load_config
from agentops.errors import AgentopsError


def register(subparsers: argparse._SubParsersAction) -> None:
    st = subparsers.add_parser(
        "start", help="为工作项建克隆并启动会话（交互开 Terminal 窗口；--headless 无人值守）"
    )
    st.add_argument("ref")
    st.add_argument("--headless", action="store_true")
    st.add_argument("--budget", type=float, help="无人值守预算（美元，客户端估算）")
    st.add_argument("--model")
    st.add_argument("--dry-run", action="store_true", help="只准备（克隆、提示词、脚本），不启动")
    st.set_defaults(func=cmd_start)

    rs = subparsers.add_parser("resume", help="让被退回的无人值守工作项的原会话重做一轮")
    rs.add_argument("ref")
    rs.add_argument("--round", type=int, default=1)
    rs.add_argument("--hint", help="默认取工作项里 steward 写的 hint")
    rs.set_defaults(func=cmd_resume)


def cmd_start(args: argparse.Namespace) -> int:
    config = load_config()
    item = wi.load_ref(args.ref)
    mode = "headless" if args.headless else "interactive"
    launch = start.prepare_launch(config, item, mode=mode, model=args.model, budget_usd=args.budget)
    start.print_launch(launch)
    if args.dry_run:
        print("dry-run：已准备，未启动。")
        return 0
    if mode == "interactive":
        try:
            start.start_interactive(launch)
        except AgentopsError as exc:
            start.rollback_launch(config, item, launch.session_id, reason=str(exc))
            raise AgentopsError(f"{exc}（已回滚：工作项回到 ready，克隆已删除）") from exc
        print("已在 Terminal 新窗口里启动会话。")
        return 0
    result = start.run_headless(config, launch)
    print(
        f"会话结束：rc {result.rc}，{result.num_turns} 轮，估算 ${result.cost_usd:.2f}，"
        f"{'已' if result.land_requested else '未'}投递 ao land。"
    )
    return 0 if not result.is_error else 1


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_config()
    item = wi.load_ref(args.ref)
    hint = args.hint or str(item.extra.get("hint") or "合并队列退回了你的分支，请查看并重做。")
    result = start.resume_headless(config, item, hint=hint, round_no=args.round)
    print(
        f"resume 结束：rc {result.rc}，本轮估算 ${result.cost_delta_usd:.2f}"
        f"（累计 ${result.cost_usd:.2f}），"
        f"{'已' if result.land_requested else '未'}投递 ao land。"
    )
    return 0 if not result.is_error else 1
