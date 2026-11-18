"""ao repo init | install-hooks"""

from __future__ import annotations

import argparse
from pathlib import Path

from agentops import repo
from agentops.runtime.base import DEFAULT_RUNTIME, RUNTIMES


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("repo", help="接入业务仓库")
    sub = parser.add_subparsers(dest="repo_command", required=True)

    init = sub.add_parser(
        "init", help="复制 AGENTS.md、.pipeline.toml、.claude/settings.json 等模板并装 git 钩子"
    )
    init.add_argument("path", type=Path)
    init.add_argument("--name", required=True, help="仓库名，例如 quant-lab")
    init.add_argument("--force", action="store_true", help="覆盖已有文件")
    init.add_argument(
        "--runtime",
        choices=RUNTIMES,
        default=DEFAULT_RUNTIME,
        help="codex 时另外生成 .codex/hooks.json；config.toml 里该仓库也要写 runtime",
    )
    init.set_defaults(func=cmd_init)

    hooks = sub.add_parser("install-hooks", help="只装 main 保护的 git 钩子")
    hooks.add_argument("path", type=Path)
    hooks.set_defaults(func=cmd_install_hooks)


def cmd_init(args: argparse.Namespace) -> int:
    result = repo.init_repo(args.path, args.name, force=args.force, runtime=args.runtime)
    for name in result.written:
        print(f"写入 {name}")
    for name in result.skipped:
        print(f"跳过 {name}（已存在，--force 可覆盖）")
    for hook in result.hooks:
        print(f"钩子 {hook}")
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    for hook in repo.install_git_hooks(args.path.expanduser().resolve()):
        print(f"钩子 {hook}")
    return 0
