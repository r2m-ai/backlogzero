"""ao token setup | check"""

from __future__ import annotations

import argparse

from agentops import doctor, token_setup


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("token", help="一年期 token 的存取")
    sub = parser.add_subparsers(dest="token_command", required=True)

    setup = sub.add_parser(
        "setup", help="运行 claude setup-token 并把 token 直接存进钥匙串（不复制粘贴、不落盘）"
    )
    setup.add_argument("--columns", type=int, default=token_setup.DEFAULT_COLUMNS)
    setup.add_argument("--no-verify", action="store_true", help="存入后不发请求验证")
    setup.set_defaults(func=cmd_setup)

    check = sub.add_parser("check", help="检查钥匙串里 token 的格式并发一次最小请求验真伪")
    check.set_defaults(func=cmd_check)


def _print_checks(checks) -> int:
    for item in checks:
        print(f"{item.name:<10} {item.status:<5} {item.detail}")
    return doctor.exit_code(checks)


def cmd_setup(args: argparse.Namespace) -> int:
    print(token_setup.setup(columns=args.columns))
    if args.no_verify:
        return 0
    return _print_checks((doctor.check_keychain(), doctor.check_live_auth()))


def cmd_check(args: argparse.Namespace) -> int:
    return _print_checks((doctor.check_keychain(), doctor.check_live_auth()))
