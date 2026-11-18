"""ao review tag | prompt | run（计划第 6A 节）"""

from __future__ import annotations

import argparse
from pathlib import Path

from agentops import review
from agentops.config import load_config
from agentops.errors import AgentopsError


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("review", help="Codex 只读评审一个阶段")
    sub = parser.add_subparsers(dest="review_command", required=True)

    tag = sub.add_parser("tag", help="在套件和各业务仓库打 phase-N-start 标签（阶段开始时执行）")
    tag.add_argument("--phase", type=int, required=True)
    tag.add_argument("--force", action="store_true", help="标签已存在且指向别处时移动它")
    tag.set_defaults(func=cmd_tag)

    for name, help_text in (
        ("prompt", "只生成 prompt 存档，不调用 codex"),
        ("run", "生成 prompt 并调用 codex，写 AGENTOPS_HOME/reviews/phase-N.md"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--phase", type=int, required=True)
        p.add_argument(
            "--summary",
            type=Path,
            help="执行者小结文件，默认 AGENTOPS_HOME/reviews/phase-N.summary.md",
        )
        p.add_argument("--no-tests", action="store_true", help="不跑测试（只在调试 prompt 时用）")
        p.add_argument("--feedback", default="无", help="相关历史反馈，默认“无”")
        p.add_argument(
            "--repo",
            action="append",
            default=[],
            help="只评审这些仓库（可重复），默认套件加所有业务仓库",
        )
        p.set_defaults(func=cmd_prompt if name == "prompt" else cmd_run)


def cmd_tag(args: argparse.Namespace) -> int:
    for line in review.tag_phase(review.all_repos(load_config()), args.phase, force=args.force):
        print(line)
    return 0


def _run(args: argparse.Namespace, *, dry_run: bool) -> int:
    config = load_config()
    repos = review.all_repos(config)
    if args.repo:
        unknown = set(args.repo) - {r.name for r in repos}
        if unknown:
            raise AgentopsError(f"未知仓库: {', '.join(sorted(unknown))}")
        repos = tuple(r for r in repos if r.name in args.repo)
    outcome = review.run_review(
        config,
        args.phase,
        repos=repos,
        summary_path=args.summary,
        with_tests=not args.no_tests,
        dry_run=dry_run,
        feedback=args.feedback,
    )
    print(f"prompt 存档: {outcome.prompt_file}")
    if dry_run:
        return 0
    print(f"评审记录: {outcome.review_file}（codex 退出码 {outcome.codex_rc}）")
    print("下一步：在评审记录的“处置”表里逐条写采纳/拒绝/待查证，修正后重跑测试并填“修正后复测”。")
    return 0 if outcome.codex_rc == 0 else 1


def cmd_prompt(args: argparse.Namespace) -> int:
    return _run(args, dry_run=True)


def cmd_run(args: argparse.Namespace) -> int:
    return _run(args, dry_run=False)
