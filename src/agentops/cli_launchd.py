"""ao launchd render|status|commands，以及 ao capture | nightly | brief"""

from __future__ import annotations

import argparse
import json

from agentops import launchd, scheduled
from agentops.config import load_config


def register(subparsers: argparse._SubParsersAction) -> None:
    top = subparsers.add_parser("launchd", help="4 个 launchd 任务的 plist 渲染、状态和安装命令")
    sub = top.add_subparsers(dest="launchd_command", required=True)
    r = sub.add_parser("render", help="把 plist 渲染到 ~/.agentops/launchd/（不加载）")
    r.set_defaults(func=cmd_render)
    c = sub.add_parser("commands", help="打印安装、卸载、手动触发的 launchctl 命令（由人执行）")
    c.set_defaults(func=cmd_commands)
    s = sub.add_parser("status", help="看 4 个任务是否已加载")
    s.set_defaults(func=cmd_status)

    cap = subparsers.add_parser("capture", help="定时采集入口：每个仓库 caffeinate -i just capture")
    cap.add_argument("--repo")
    cap.set_defaults(func=cmd_capture)

    night = subparsers.add_parser(
        "nightly", help="夜间任务：just nightly → ao pm → ao pm --dispatch --nightly → 简报"
    )
    night.add_argument("--repo")
    night.add_argument("--no-dispatch", action="store_true")
    night.set_defaults(func=cmd_nightly)

    brief = subparsers.add_parser("brief", help="写今天的每日简报到 ~/.agentops/briefs/")
    brief.add_argument("--date")
    brief.set_defaults(func=cmd_brief)


def cmd_render(args: argparse.Namespace) -> int:
    for item in launchd.render_all(load_config()):
        print(f"{item.label}: {item.path}")
    print("已渲染。安装命令见 `ao launchd commands`。")
    return 0


def cmd_commands(args: argparse.Namespace) -> int:
    rendered = launchd.render_all(load_config())
    print("# 安装（会添加 4 个后台登录项，macOS 会提示“已添加后台项目”）")
    for line in launchd.install_commands(rendered):
        print(line)
    print("\n# 手动触发一次（验收用）")
    for job in ("capture", "nightly"):
        print(launchd.kick_command(job))
    print("\n# 卸载")
    for line in launchd.uninstall_commands(rendered):
        print(line)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    for job in launchd.JOBS:
        loaded, summary = launchd.status(job)
        print(f"{job:<8} {'已加载' if loaded else '未加载'}  {summary}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    results = scheduled.capture(load_config(), [args.repo] if args.repo else None)
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(rc == 0 for rc in results.values()) else 1


def cmd_nightly(args: argparse.Namespace) -> int:
    results = scheduled.nightly(
        load_config(), [args.repo] if args.repo else None, dispatch=not args.no_dispatch
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    print(scheduled.write_brief(load_config(), args.date))
    return 0
