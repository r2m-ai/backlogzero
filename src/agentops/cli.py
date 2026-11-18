"""ao 命令入口：把子命令分发到各 cli_* 模块。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agentops import (
    __version__,
    cli_board,
    cli_doctor,
    cli_events,
    cli_export,
    cli_launchd,
    cli_pm,
    cli_repo,
    cli_review,
    cli_secret,
    cli_sessions,
    cli_slots,
    cli_start,
    cli_steward,
    cli_token,
    cli_workitems,
)
from agentops.errors import AgentopsError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ao", description="agentops：本地 agent 流水线套件")
    parser.add_argument("--version", action="version", version=f"ao {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in (
        cli_workitems,
        cli_events,
        cli_export,
        cli_slots,
        cli_sessions,
        cli_pm,
        cli_board,
        cli_launchd,
        cli_repo,
        cli_review,
        cli_start,
        cli_steward,
        cli_secret,
        cli_token,
        cli_doctor,
    ):
        module.register(subparsers)
    return parser


def split_at_double_dash(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """把 `--` 之后的部分原样保留，交给需要它的子命令（ao slot）。"""
    items = list(argv)
    if "--" not in items:
        return items, []
    index = items.index("--")
    return items[:index], items[index + 1 :]


def main(argv: Sequence[str] | None = None) -> int:
    head, tail = split_at_double_dash(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(head)
    args.command_tail = tail
    try:
        if tail and args.command != "slot":
            raise AgentopsError("只有 ao slot 接受 -- 之后的参数")
        return int(args.func(args) or 0)
    except AgentopsError as exc:
        print(f"ao: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ao: 系统错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
