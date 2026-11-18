"""ao pm | kpi"""

from __future__ import annotations

import argparse
import json

from agentops import kpi, pm, pm_propose
from agentops.config import load_config
from agentops.errors import AgentopsError
from agentops.timeutil import now, parse_duration


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("pm", help="PM Agent：排优先级、写报告；--dispatch 派发")
    p.add_argument("--repo")
    p.add_argument("--dispatch", action="store_true")
    p.add_argument(
        "--nightly",
        action="store_true",
        help="夜间模式：把 trigger=nightly 的工作项提为 ready 并派发，最多 nightly_max 项",
    )
    p.add_argument(
        "--propose",
        action="store_true",
        help="PM 提案：读研究背景提出 backlog 工作项，由人在看板采纳",
    )
    p.add_argument("--no-llm", action="store_true", help="只做确定性计算，不调用 LLM")
    p.add_argument("--dry-run", action="store_true", help="派发时只列出会派发的工作项")
    p.add_argument("--max", type=int, help="本次最多派发几项")
    p.set_defaults(func=cmd_pm)

    k = subparsers.add_parser("kpi", help="从事件日志算 KPI")
    k.add_argument("--since", default="7d")
    k.add_argument("--repo")
    k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_kpi)


def cmd_pm(args: argparse.Namespace) -> int:
    config = load_config()
    repos = (
        [args.repo]
        if args.repo
        else [r for r in config.repos if (config.repo_path(r) / ".git").exists()]
    )
    for repo in repos:
        report = pm.plan(config, repo, use_llm=not args.no_llm)
        print(
            f"{repo}: 报告 {report.path}（来源 {report.source}，${report.cost_usd:.2f}）；"
            f"顺序 {list(report.dispatch_order)}；改了优先级 {list(report.priorities_applied)}"
        )
        if args.propose:
            proposal = pm_propose.propose(config, repo)
            if proposal.skipped_reason:
                print(f"{repo}: 未提案——{proposal.skipped_reason}")
            else:
                print(
                    f"{repo}: 提案 {list(proposal.created)}（拒绝 {list(proposal.rejected)}，"
                    f"${proposal.cost_usd:.2f}）"
                )
        if args.dispatch:
            launched = pm.dispatch(
                config, repo, nightly=args.nightly, dry_run=args.dry_run, max_items=args.max
            )
            print(f"{repo}: {'将派发' if args.dry_run else '已派发'} {list(launched)}")
    return 0


def cmd_kpi(args: argparse.Namespace) -> int:
    try:
        since = now() - parse_duration(args.since)
    except ValueError as exc:
        raise AgentopsError(str(exc)) from exc
    data = kpi.compute(since, repo=args.repo)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else kpi.format_report(data))
    return 0
