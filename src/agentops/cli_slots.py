"""ao slot <name> [--count N] -- <command...>"""

from __future__ import annotations

import argparse

from agentops import slots
from agentops.config import load_config


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "slot",
        help="在重任务槽位里运行命令",
        usage="ao slot NAME [--count N] [--repo R] [--item I] [--session S] -- COMMAND...",
    )
    parser.add_argument("name", help="槽位名，例如 test")
    parser.add_argument("--count", type=int, help="槽位数，默认取 config.toml 的 [slots]")
    parser.add_argument("--repo")
    parser.add_argument("--item", type=int)
    parser.add_argument("--session")
    parser.add_argument("--timeout", type=float, help="等待槽位的秒数上限")
    parser.set_defaults(func=cmd_slot)


def cmd_slot(args: argparse.Namespace) -> int:
    command = list(getattr(args, "command_tail", []))
    count = args.count if args.count is not None else load_config().slot_count(args.name)
    return slots.run_with_slot(
        args.name,
        command,
        count=count,
        repo=args.repo,
        item=args.item,
        session=args.session,
        timeout_seconds=args.timeout,
    )
