"""ao events emit | tail"""

from __future__ import annotations

import argparse
import json

from agentops import events
from agentops.cli_common import parse_json_object, parse_kv
from agentops.errors import AgentopsError
from agentops.output import format_table
from agentops.timeutil import now, parse_duration


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("必须 ≥ 1")
    return value


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("events", help="事件日志")
    sub = parser.add_subparsers(dest="events_command", required=True)

    emit = sub.add_parser("emit", help="追加一条事件")
    emit.add_argument("type")
    emit.add_argument("--repo")
    emit.add_argument("--item", type=int)
    emit.add_argument("--session")
    emit.add_argument("--detail", help="JSON 对象")
    emit.add_argument("--kv", action="append", default=[], help="detail 的 key=value，可重复")
    emit.set_defaults(func=cmd_emit)

    tail = sub.add_parser("tail", help="显示最近的事件")
    tail.add_argument("-n", type=positive_int, default=20)
    tail.add_argument("--type", action="append", dest="types")
    tail.add_argument("--since", help="例如 7d、24h、30m")
    tail.add_argument("--repo")
    tail.add_argument("--item", type=int)
    tail.add_argument("--session")
    tail.add_argument("--json", action="store_true")
    tail.set_defaults(func=cmd_tail)


def cmd_emit(args: argparse.Namespace) -> int:
    detail = {**parse_json_object(args.detail), **parse_kv(args.kv)}
    event = events.emit(
        args.type, repo=args.repo, item=args.item, session=args.session, detail=detail
    )
    print(event.to_json())
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    since = None
    if args.since:
        try:
            since = now() - parse_duration(args.since)
        except ValueError as exc:
            raise AgentopsError(str(exc)) from exc
    selected = events.filter_events(
        events.read_events(),
        since=since,
        types=args.types,
        repo=args.repo,
        item=args.item,
        session=args.session,
    )[-args.n :]
    if args.json:
        print(json.dumps([e.to_dict() for e in selected], ensure_ascii=False, indent=2))
        return 0
    rows = [
        (
            e.ts,
            e.type,
            f"{e.repo}#{e.item}" if e.repo and e.item is not None else (e.repo or "-"),
            (e.session or "-")[:8],
            json.dumps(dict(e.detail), ensure_ascii=False),
        )
        for e in selected
    ]
    print(
        format_table(rows, ("ts", "type", "item", "session", "detail")) if rows else "（没有事件）"
    )
    return 0
