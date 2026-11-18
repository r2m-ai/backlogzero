"""ao land | steward | approve | clone | queue"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentops import approvals, clones, land, queue, steward
from agentops import workitems as wi
from agentops.config import load_config
from agentops.errors import AgentopsError
from agentops.output import format_table


def register(subparsers: argparse._SubParsersAction) -> None:
    landp = subparsers.add_parser("land", help="（会话里）把当前克隆的分支送入合并队列")
    landp.add_argument("--session", help="会话 id，默认取环境变量")
    landp.set_defaults(func=cmd_land)

    st = subparsers.add_parser("steward", help="合并队列的处理者：--once 处理一轮，--loop 常驻")
    mode = st.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    st.add_argument("--repo")
    st.add_argument("--poll", type=float, help="--loop 的轮询秒数，默认取 config")
    st.set_defaults(func=cmd_steward)

    ap = subparsers.add_parser(
        "approve", help="人类审批：绑定工作项和入队时的 SHA（会话内执行会被拒）"
    )
    ap.add_argument("ref", help="例如 quant-lab#7")
    ap.add_argument("--sha", required=True, help="needs_human 时记录的 approval_sha，可用短 sha")
    ap.add_argument("--note", default="")
    ap.set_defaults(func=cmd_approve)

    cl = subparsers.add_parser("clone", help="工作项的独立克隆")
    sub = cl.add_subparsers(dest="clone_command", required=True)
    c_create = sub.add_parser("create")
    c_create.add_argument("ref")
    c_create.set_defaults(func=cmd_clone_create)
    c_remove = sub.add_parser("remove")
    c_remove.add_argument("ref")
    c_remove.add_argument("--reason", default="manual")
    c_remove.set_defaults(func=cmd_clone_remove)
    c_list = sub.add_parser("list")
    c_list.add_argument("--repo")
    c_list.set_defaults(func=cmd_clone_list)

    q = subparsers.add_parser("queue", help="查看合并队列")
    q.add_argument("--repo")
    q.add_argument("--all", action="store_true", help="显示每个请求的最新状态，不只待处理的")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_queue)


def cmd_land(args: argparse.Namespace) -> int:
    land.land(load_config(), cwd=Path.cwd(), session=args.session)
    return 0


def cmd_steward(args: argparse.Namespace) -> int:
    config = load_config()
    if args.loop:
        steward.run_loop(config, poll_seconds=args.poll, repo=args.repo)
        return 0
    outcomes = steward.run_once(config, args.repo)
    if not outcomes:
        print("队列为空")
    for outcome in outcomes:
        print(f"{outcome.request.id}: {outcome.result} — {outcome.reason}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    repo, item_id = wi.parse_ref(args.ref)
    approval = approvals.record(repo, item_id, args.sha, by="terminal", note=args.note)
    print(
        f"已记录审批 {approval.repo}#{approval.item} @ {approval.sha}（{approval.by}）。"
        "steward 下一轮会重新入队。"
    )
    return 0


def cmd_clone_create(args: argparse.Namespace) -> int:
    info = clones.create_clone(load_config(), wi.load_ref(args.ref))
    print(f"{info.path} 分支 {info.branch}")
    return 0


def cmd_clone_remove(args: argparse.Namespace) -> int:
    removed = clones.remove_clone(load_config(), wi.load_ref(args.ref), reason=args.reason)
    print("已删除" if removed else "没有这个克隆")
    return 0


def cmd_clone_list(args: argparse.Namespace) -> int:
    config = load_config()
    repos = [args.repo] if args.repo else list(config.repos)
    for repo in repos:
        for path in clones.list_clones(config, repo):
            print(f"{repo}\t{path}\t{clones.current_branch(path)}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    config = load_config()
    repos = [args.repo] if args.repo else [r for r in config.repos]
    rows = []
    for repo in repos:
        lines = (
            queue.latest(repo).values()
            if args.all
            else [line for line in queue.latest(repo).values() if line.status in queue.PENDING]
        )
        for line in sorted(lines, key=lambda line: line.ts):
            rows.append(
                (
                    line.ts,
                    f"{repo}#{line.request.item}",
                    line.status,
                    line.request.branch,
                    line.request.sha[:7],
                    json.dumps(dict(line.detail), ensure_ascii=False),
                )
            )
    if args.json:
        print(
            json.dumps(
                [
                    dict(zip(("ts", "item", "status", "branch", "sha", "detail"), row, strict=True))
                    for row in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        format_table(rows, ("ts", "item", "status", "branch", "sha", "detail"))
        if rows
        else "（队列为空）"
    )
    return 0


def _unused(_: AgentopsError) -> None:  # pragma: no cover
    pass
