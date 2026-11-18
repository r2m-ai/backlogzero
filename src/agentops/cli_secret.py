"""ao secret set|check <name>"""

from __future__ import annotations

import argparse

from agentops import secret_store
from agentops.auth import AuthError


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "secret", help="第三方 API token 的存取（钥匙串 agentops-<name>）"
    )
    sub = parser.add_subparsers(dest="secret_command", required=True)
    setter = sub.add_parser("set", help="从终端读入 token 存进钥匙串，不回显、不落盘")
    setter.add_argument("name", help="名字，例如 eodhd")
    setter.set_defaults(func=cmd_set)
    checker = sub.add_parser("check", help="确认钥匙串里有这个 token（只报长度）")
    checker.add_argument("name")
    checker.set_defaults(func=cmd_check)


def cmd_set(args: argparse.Namespace) -> int:
    try:
        print(secret_store.set_secret(args.name))
    except AuthError as exc:
        print(f"ao secret: {exc}")
        return 2
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        print(secret_store.check(args.name))
    except AuthError as exc:
        print(f"ao secret: {exc}")
        return 2
    return 0
