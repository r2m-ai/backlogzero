"""ao wi new | list | show | set | ready"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentops import workitems as wi
from agentops.cli_common import none_if_literal_none, parse_int_list, parse_kv
from agentops.errors import AgentopsError
from agentops.output import format_table


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("wi", help="工作项")
    sub = parser.add_subparsers(dest="wi_command", required=True)

    new = sub.add_parser("new", help="新建工作项（初始状态 backlog）")
    new.add_argument("--repo", required=True)
    new.add_argument("--title", required=True)
    new.add_argument("--type", default="feat", choices=wi.TYPES)
    new.add_argument("--module", default="core")
    new.add_argument("--priority", default="P2", choices=wi.PRIORITIES)
    new.add_argument("--depends", help="依赖的工作项 id，逗号分隔")
    new.add_argument("--acceptance", action="append", default=[], help="验收条目，可重复")
    new.add_argument("--trigger", default="auto", choices=wi.TRIGGERS)
    new.add_argument("--model", help="这一项用的模型（sonnet/opus），默认 config 的 worker")
    new.add_argument("--effort", help="思考强度 low/medium/high/xhigh/max，默认 Claude Code 默认")
    new.add_argument("--body", default="", help="正文")
    new.add_argument("--body-file", type=Path, help="从文件读正文")
    new.add_argument("--ready", action="store_true", help="创建后直接进入 ready（需有 acceptance）")
    new.add_argument("--id", type=int, help="指定编号（演示和测试用），默认自动递增")
    new.set_defaults(func=cmd_new)

    lst = sub.add_parser("list", help="列出工作项")
    lst.add_argument("--repo")
    lst.add_argument("--status", choices=wi.STATUSES)
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="显示一个工作项")
    show.add_argument("ref", help="例如 quant-lab#4")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    setp = sub.add_parser("set", help="修改字段或状态")
    setp.add_argument("ref")
    setp.add_argument("--status", choices=wi.STATUSES)
    setp.add_argument("--priority", choices=wi.PRIORITIES)
    setp.add_argument("--type", choices=wi.TYPES)
    setp.add_argument("--module")
    setp.add_argument("--title")
    setp.add_argument("--trigger", choices=wi.TRIGGERS)
    setp.add_argument("--model")
    setp.add_argument("--effort")
    setp.add_argument("--depends", help="覆盖依赖列表，逗号分隔")
    setp.add_argument("--add-acceptance", action="append", default=[])
    setp.add_argument("--session-id", help="写 none 清空")
    setp.add_argument("--branch", help="写 none 清空")
    setp.add_argument("--kv", action="append", default=[], help="额外字段 key=value，值 null 删除")
    setp.add_argument("--force", action="store_true", help="跳过状态机（只给人用）")
    setp.set_defaults(func=cmd_set)

    ready = sub.add_parser("ready", help="规格就绪闸门：acceptance 非空才能进入 ready")
    ready.add_argument("ref")
    ready.set_defaults(func=cmd_ready)


def item_to_dict(item: wi.WorkItem) -> dict[str, object]:
    return {
        "ref": item.ref,
        "id": item.id,
        "repo": item.repo,
        "title": item.title,
        "type": item.type,
        "module": item.module,
        "priority": item.priority,
        "status": item.status,
        "depends_on": list(item.depends_on),
        "blocked_by": list(wi.blocked_by(item)),
        "acceptance": list(item.acceptance),
        "trigger": item.trigger,
        "session_id": item.session_id,
        "branch": item.branch,
        "extra": dict(item.extra),
        "path": str(wi.item_path(item.repo, item.id)),
    }


def cmd_new(args: argparse.Namespace) -> int:
    body = args.body_file.read_text(encoding="utf-8") if args.body_file else args.body
    if args.ready and not any(a.strip() for a in args.acceptance):
        raise AgentopsError("--ready 需要至少一条 --acceptance，规格未就绪不能进入 ready")
    item = wi.create(
        repo=args.repo,
        item_id=args.id,
        title=args.title,
        type=args.type,
        module=args.module,
        priority=args.priority,
        depends_on=parse_int_list(args.depends),
        acceptance=args.acceptance,
        trigger=args.trigger,
        body=body,
    )
    choices = {k: v for k, v in (("model", args.model), ("effort", args.effort)) if v}
    if choices:
        item = wi.apply(item, **choices)
    if args.ready:
        item = wi.apply(item, status="ready")
    print(f"{item.ref} {item.status} {item.title}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    pool = wi.list_items(repo=args.repo)
    items = tuple(i for i in pool if args.status is None or i.status == args.status)
    if args.json:
        print(json.dumps([item_to_dict(i) for i in items], ensure_ascii=False, indent=2))
        return 0
    rows = [
        (
            i.ref,
            i.status,
            i.priority,
            i.module,
            i.trigger,
            ",".join(map(str, i.depends_on)) or "-",
            ",".join(map(str, wi.blocked_by(i, items=pool))) or "-",
            i.title,
        )
        for i in items
    ]
    headers = ("ref", "status", "pri", "module", "trigger", "deps", "blocked", "title")
    print(format_table(rows, headers) if rows else "（没有工作项）")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    item = wi.load_ref(args.ref)
    if args.json:
        print(json.dumps(item_to_dict(item), ensure_ascii=False, indent=2))
        return 0
    print(wi.item_path(item.repo, item.id).read_text(encoding="utf-8"), end="")
    blocked = wi.blocked_by(item)
    print(f"\n# blocked_by: {', '.join(map(str, blocked)) if blocked else '无'}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    item = wi.load_ref(args.ref)
    changes: dict[str, object] = {}
    for name in ("status", "priority", "type", "module", "title", "trigger"):
        value = getattr(args, name)
        if value is not None:
            changes[name] = value
    if args.depends is not None:
        changes["depends_on"] = parse_int_list(args.depends)
    if args.add_acceptance:
        changes["acceptance"] = (*item.acceptance, *args.add_acceptance)
    if args.session_id is not None:
        changes["session_id"] = none_if_literal_none(args.session_id)
    if args.branch is not None:
        changes["branch"] = none_if_literal_none(args.branch)
    extra = parse_kv(args.kv)
    extra.update({k: v for k, v in (("model", args.model), ("effort", args.effort)) if v})
    reserved = set(wi.FRONT_MATTER_KEYS) | {"body"}
    for key in extra:
        if key in reserved:
            raise AgentopsError(f"{key} 请用对应参数修改，--kv 只用于额外字段")
    grants = extra.get("allow_test_edits")
    if grants is not None and not (
        isinstance(grants, list) and all(isinstance(g, str) and g.strip() for g in grants)
    ):
        raise AgentopsError("allow_test_edits 必须是路径列表，例如 '[\"tests/test_x.py\"]'")
    changes.update(extra)
    if not changes:
        raise AgentopsError("没有给出任何修改")
    updated = wi.apply(item, force=args.force, **changes)
    print(f"{updated.ref} {updated.status}")
    return 0


def cmd_ready(args: argparse.Namespace) -> int:
    item = wi.load_ref(args.ref)
    updated = wi.apply(item, status="ready")
    print(f"{updated.ref} ready")
    return 0
