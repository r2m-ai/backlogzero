"""ao init | doctor"""

from __future__ import annotations

import argparse
import json

from agentops import doctor, init
from agentops.output import format_table
from agentops.paths import home


def register(subparsers: argparse._SubParsersAction) -> None:
    init_parser = subparsers.add_parser("init", help="初始化 ~/.agentops（可重复运行）")
    init_parser.set_defaults(func=cmd_init)

    doc = subparsers.add_parser("doctor", help="检查工具、认证、状态目录")
    doc.add_argument("--no-auth", action="store_true", help="跳过 claude auth status 检查")
    doc.add_argument("--live", action="store_true", help="发一次最小请求验证 token 真伪（会联网）")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=cmd_doctor)


def cmd_init(args: argparse.Namespace) -> int:
    created = init.initialize()
    if created:
        print("已创建:")
        for path in created:
            print(f"  {path}")
    else:
        print(f"{home()} 已就绪，无需改动")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor.run_checks(include_auth=not args.no_auth, live=args.live)
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=2))
    else:
        print(
            format_table(
                [(c.name, c.status, c.detail) for c in checks], ("check", "status", "detail")
            )
        )
    return doctor.exit_code(checks)
