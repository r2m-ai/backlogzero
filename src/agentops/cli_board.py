"""ao board"""

from __future__ import annotations

import argparse

from agentops import board
from agentops.config import load_config


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("board", help="看板：只监听 127.0.0.1:8749 的本地网页")
    p.add_argument("--port", type=int, help="默认取 config [board].port")
    p.set_defaults(func=cmd_board)


def cmd_board(args: argparse.Namespace) -> int:
    board.serve(load_config(), port=args.port)
    return 0
