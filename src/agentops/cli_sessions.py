"""ao session register | list | end"""

from __future__ import annotations

import argparse
import json

from agentops import events, sessions
from agentops.output import format_table


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("session", help="会话登记")
    sub = parser.add_subparsers(dest="session_command", required=True)

    reg = sub.add_parser("register", help="登记一个会话（SessionStart hook 调用）")
    reg.add_argument("--session-id", required=True)
    reg.add_argument("--repo")
    reg.add_argument("--item", type=int)
    reg.add_argument("--branch")
    reg.add_argument("--worktree")
    reg.add_argument("--mode", default="interactive", choices=("interactive", "headless"))
    reg.add_argument("--name")
    reg.set_defaults(func=cmd_register)

    lst = sub.add_parser("list", help="列出会话，默认只看运行中的")
    lst.add_argument("--all", action="store_true")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    prune = sub.add_parser("prune", help="把超过时限没有更新的运行中会话标记为结束")
    prune.add_argument("--hours", type=float, help="默认取 config [sessions].stale_after_hours")
    prune.set_defaults(func=cmd_prune)

    end = sub.add_parser("end", help="标记会话结束")
    end.add_argument("--session-id", required=True)
    end.add_argument("--rc", type=int)
    end.add_argument("--cost-usd", type=float)
    end.set_defaults(func=cmd_end)


def cmd_register(args: argparse.Namespace) -> int:
    record = sessions.register(
        args.session_id,
        repo=args.repo,
        item=args.item,
        branch=args.branch,
        worktree=args.worktree,
        mode=args.mode,
        name=args.name,
    )
    events.emit(
        "session_registered",
        repo=args.repo,
        item=args.item,
        session=args.session_id,
        detail={"mode": args.mode, "branch": args.branch, "worktree": args.worktree},
    )
    print(json.dumps(record.to_dict(), ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = tuple(sessions.latest_by_id().values()) if args.all else sessions.active()
    if args.json:
        print(json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2))
        return 0
    rows = [
        (
            r.session_id[:8],
            r.status,
            f"{r.repo}#{r.item}" if r.repo and r.item is not None else "-",
            r.mode or "-",
            r.branch or "-",
            r.ts,
        )
        for r in records
    ]
    headers = ("session", "status", "item", "mode", "branch", "ts")
    print(format_table(rows, headers) if rows else "（没有会话）")
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    from agentops import budget

    record = sessions.end(args.session_id, rc=args.rc, cost_usd=args.cost_usd)
    budget.settle(args.session_id, float(args.cost_usd or 0.0))
    events.emit(
        "session_end",
        repo=record.repo,
        item=record.item,
        session=record.session_id,
        detail={"rc": args.rc, "cost_usd": args.cost_usd},
    )
    print(json.dumps(record.to_dict(), ensure_ascii=False))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from agentops.config import load_config

    default_hours = float(load_config().sessions["stale_after_hours"])  # type: ignore[arg-type]
    hours = args.hours if args.hours is not None else default_hours
    pruned = sessions.prune(stale_after_hours=hours)
    for record in pruned:
        events.emit("session_pruned", repo=record.repo, item=record.item, session=record.session_id)
    print(f"清理了 {len(pruned)} 个陈旧会话（超过 {hours} 小时没有更新）")
    return 0
