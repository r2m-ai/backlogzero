"""ao export"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from agentops import export
from agentops.config import load_config
from agentops.errors import AgentopsError

NEXT_STEPS = """\
后续由人在目标副本里执行（本命令不提交、不推送）:
  git -C {target} diff            # 先看差异
  git -C {target} config user.name  "<公开用的名字>"
  git -C {target} config user.email "<公开用的邮箱>"
  git -C {target} add -A
  git -C {target} commit -m "<type>: <说明>"
  git -C {target} push"""


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "export", help="把当前版本同步到公开仓库的本机副本（不提交不推送）"
    )
    parser.add_argument("--to", required=True, type=Path, help="公开仓库的本机副本")
    parser.add_argument(
        "--from", dest="source", type=Path, default=Path.cwd(), help="源仓库（默认当前目录）"
    )
    parser.set_defaults(func=cmd_export)


def _username() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError) as exc:
        raise AgentopsError(f"无法取得本机用户名，拒绝导出: {exc}") from exc


def _print_group(label: str, paths: tuple[str, ...]) -> None:
    print(f"{label}: {len(paths)}")
    for path in paths[: export.MAX_LISTED]:
        print(f"  {path}")
    if len(paths) > export.MAX_LISTED:
        print(f"  …… 另有 {len(paths) - export.MAX_LISTED} 个未列出")


def cmd_export(args: argparse.Namespace) -> int:
    redact = [str(w) for w in load_config().review.get("redact", ())]
    result = export.run_export(
        args.source, args.to, redact=redact, home=Path.home(), username=_username()
    )
    print(f"源提交: {result.source_sha}")
    if result.is_unchanged:
        print("公开版已是最新")
        return 0
    _print_group("新增", result.added)
    _print_group("修改", result.modified)
    _print_group("删除", result.deleted)
    print(NEXT_STEPS.format(target=args.to.resolve()))
    return 0
