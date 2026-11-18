"""PM Agent（C8）。

先做确定性计算（依赖、规格就绪、并发、预算、KPI），再一次结构化 LLM 调用给出优先级和派发顺序，
写 ~/.agentops/pm-report.md。排序自动应用；砍掉要人确认。--dispatch 按顺序派发。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from agentops import budget, events, kpi, llm, sessions, start
from agentops import repo as repo_tools
from agentops import workitems as wi
from agentops.config import Config
from agentops.paths import home, logs_dir
from agentops.timeutil import now, now_iso
from agentops.workitem_model import WorkItem

TERMINAL = frozenset({"landed", "dropped"})
PM_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "priorities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "priority", "reason"],
                "additionalProperties": False,
            },
        },
        "dispatch_order": {"type": "array", "items": {"type": "integer"}},
        "drop_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["priorities", "dispatch_order", "drop_candidates", "notes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Snapshot:
    repo: str
    items: tuple[WorkItem, ...]
    dispatchable: tuple[int, ...]
    blocked: Mapping[int, tuple[int, ...]]
    active_sessions: int
    spend_today: float


@dataclass(frozen=True)
class PmReport:
    repo: str
    path: object
    priorities_applied: tuple[int, ...]
    dispatch_order: tuple[int, ...]
    drop_candidates: tuple[int, ...]
    cost_usd: float
    source: str


def order_file(repo: str):
    return home() / f"pm-order-{repo}.json"


def snapshot(config: Config, repo: str, *, nightly: bool = False) -> Snapshot:
    items = wi.list_items(repo)
    blocked = {i.id: wi.blocked_by(i, items=items) for i in items}
    dispatchable = tuple(i.id for i in items if wi.is_dispatchable(i, nightly=nightly, items=items))
    stale_hours = float(config.sessions.get("stale_after_hours", 6))  # type: ignore[arg-type]
    active = len(sessions.active(stale_after_hours=stale_hours))
    return Snapshot(repo, items, dispatchable, blocked, active, budget.spend_today())


def items_table(snap: Snapshot) -> str:
    """工作项清单的 Markdown 表，PM 排序和 PM 提案的提示词都用它。"""
    rows = [
        "| id | 状态 | 优先级 | 模块 | 触发 | 依赖 | 被挡 | 退回 | 规格就绪 | 标题 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i in snap.items:
        deps = ",".join(map(str, i.depends_on)) or "-"
        blocked = ",".join(map(str, snap.blocked[i.id])) or "-"
        ready = "是" if i.is_spec_ready else "否"
        rows.append(
            f"| {i.id} | {i.status} | {i.priority} | {i.module} | {i.trigger} | {deps} | "
            f"{blocked} | {i.extra.get('returns', 0)} | {ready} | {i.title.replace('|', '/')} |"
        )
    return "\n".join(rows)


def _ask_llm(config: Config, snap: Snapshot) -> tuple[Mapping[str, object], float]:
    template = (repo_tools.TEMPLATES / "prompts" / "pm.md").read_text(encoding="utf-8")
    kpi_text = kpi.format_report(kpi.compute(now() - timedelta(days=7), repo=snap.repo)).replace(
        "\n", "；"
    )
    values = {
        "repo": snap.repo,
        "active": snap.active_sessions,
        "max_sessions": config.max_sessions,
        "spend_today": f"{snap.spend_today:.2f}",
        "daily_budget": config.daily_budget_usd,
        "kpi": kpi_text,
        "items": items_table(snap),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    result = llm.call_structured(
        template, PM_SCHEMA, config=config, role="pm", label="pm", repo=snap.repo
    )
    return result.data, result.cost_usd


def plan(config: Config, repo: str, *, use_llm: bool = True) -> PmReport:
    snap = snapshot(config, repo)
    data: Mapping[str, object] = {
        "priorities": [],
        "dispatch_order": list(snap.dispatchable),
        "drop_candidates": [],
        "notes": "未调用 LLM，按优先级和编号排序。",
    }
    cost, source = 0.0, "deterministic"
    if use_llm and snap.items:
        try:
            data, cost = _ask_llm(config, snap)
            source = "llm"
        except (llm.LlmError, budget.BudgetExceeded) as exc:
            events.emit("pm_error", repo=repo, detail={"error": str(exc)})
            data = {**data, "notes": f"LLM 调用失败，按确定性排序：{exc}"}
    by_id = {i.id: i for i in snap.items}
    applied: list[int] = []
    for entry in data.get("priorities", []):  # type: ignore[union-attr]
        item = by_id.get(int(entry["id"]))  # type: ignore[index]
        if item is None or item.status in TERMINAL or item.priority == entry["priority"]:  # type: ignore[index]
            continue
        wi.apply(item, priority=str(entry["priority"]), pm_reason=str(entry["reason"]))  # type: ignore[index]
        applied.append(item.id)
    valid_order = tuple(
        int(i) for i in data.get("dispatch_order", []) if int(i) in snap.dispatchable
    )  # type: ignore[union-attr]
    order = valid_order + tuple(i for i in snap.dispatchable if i not in valid_order)
    drops = tuple(int(d["id"]) for d in data.get("drop_candidates", []))  # type: ignore[index,union-attr]
    order_file(repo).write_text(
        json.dumps({"ts": now_iso(), "order": list(order)}), encoding="utf-8"
    )
    path = home() / "pm-report.md"
    path.write_text(_report_text(config, snap, data, order, drops, source, cost), encoding="utf-8")
    events.emit(
        "pm_run",
        repo=repo,
        detail={
            "source": source,
            "cost_usd": cost,
            "applied": applied,
            "order": list(order),
            "drops": list(drops),
        },
    )
    return PmReport(repo, path, tuple(applied), order, drops, cost, source)


def _report_text(
    config: Config,
    snap: Snapshot,
    data: Mapping[str, object],
    order: Sequence[int],
    drops: Sequence[int],
    source: str,
    cost: float,
) -> str:
    lines = [
        f"# PM 报告：{snap.repo}",
        "",
        f"- 时间：{now_iso()}；来源：{source}；本次调用估算 ${cost:.2f}",
        f"- 并发 {snap.active_sessions}/{config.max_sessions}；"
        f"今日已用 ${snap.spend_today:.2f} / ${config.daily_budget_usd}",
        f"- 可派发：{', '.join(map(str, snap.dispatchable)) or '无'}；"
        f"建议顺序：{', '.join(map(str, order)) or '无'}",
        "",
        "## 优先级建议（已自动应用到未完成的工作项）",
        "",
    ]
    for entry in data.get("priorities", []):  # type: ignore[union-attr]
        lines.append(f"- #{entry['id']} → {entry['priority']}：{entry['reason']}")  # type: ignore[index]
    lines += ["", "## 建议砍掉（需在看板确认，未自动应用）", ""]
    for entry in data.get("drop_candidates", []):  # type: ignore[union-attr]
        lines.append(f"- #{entry['id']}：{entry['reason']}")  # type: ignore[index]
    if not drops:
        lines.append("- 无")
    lines += [
        "",
        "## 说明",
        "",
        str(data.get("notes", "")),
        "",
        "## 工作项清单",
        "",
        items_table(snap),
    ]
    return "\n".join(lines) + "\n"


def dispatch(
    config: Config,
    repo: str,
    *,
    nightly: bool = False,
    dry_run: bool = False,
    max_items: int | None = None,
) -> tuple[int, ...]:
    """按顺序派发 ready 且未被挡的工作项，遵守并发、模块锁、每日预算。

    nightly 时先把 trigger=nightly 的 backlog 提为 ready。
    """
    launched: list[int] = []
    if nightly:
        for item in wi.list_items(repo, status="backlog"):
            if item.trigger == "nightly" and item.is_spec_ready and not wi.blocked_by(item):
                wi.apply(item, status="ready")
                events.emit("nightly_promoted", repo=repo, item=item.id)
    snap = snapshot(config, repo, nightly=nightly)
    saved = _saved_order(repo)
    order = [i for i in saved if i in snap.dispatchable] + [
        i for i in snap.dispatchable if i not in saved
    ]
    by_id = {i.id: i for i in snap.items}
    active = snap.active_sessions
    per_session = float(config.budget_usd["worker"])
    remaining = budget.available(config.daily_budget_usd)
    limit = (
        max_items
        if max_items is not None
        else (int(config.steward.get("nightly_max", 2)) if nightly else None)
    )
    claimed_modules: set[str] = set()
    for item_id in order:
        if limit is not None and len(launched) >= limit:
            break
        item = by_id[item_id]
        if active >= config.max_sessions:
            _note_skip(item, "并发已满", dry_run=dry_run)
            break
        if item.module in claimed_modules or wi.module_busy(repo, item.module, exclude_id=item.id):
            _note_skip(item, "模块已有会话", dry_run=dry_run)
            continue
        if remaining < per_session:
            _note_skip(item, "当日预算不足", dry_run=dry_run, remaining=remaining)
            break
        if _dispatch_pending(item):
            continue
        if not dry_run:
            log = logs_dir(repo) / f"dispatch-{item_id}.log"
            pid = start.spawn_detached(["start", item.ref, "--headless"], log)
            events.emit(
                "dispatch",
                repo=repo,
                item=item_id,
                detail={"pid": pid, "nightly": nightly, "log": str(log)},
            )
        if not dry_run:
            wi.apply(item, dispatch_pending=now_iso(), skip_reason=None, skip_since=None)
        launched.append(item_id)
        claimed_modules.add(item.module)
        active += 1
        remaining -= per_session
    return tuple(launched)


def _note_skip(item: WorkItem, reason: str, *, dry_run: bool, **detail: object) -> None:
    """同一 (项, 原因) 只在首次与原因变化时写 dispatch_skipped；最近原因与首次时间记在工作项上。"""
    if not dry_run and item.extra.get("skip_reason") == reason:
        return
    since = now_iso()
    events.emit(
        "dispatch_skipped",
        repo=item.repo,
        item=item.id,
        detail={"reason": reason, "since": since, **detail},
    )
    if not dry_run:
        wi.apply(item, skip_reason=reason, skip_since=since)


def _saved_order(repo: str) -> list[int]:
    path = order_file(repo)
    if not path.exists():
        return []
    try:
        return [int(i) for i in json.loads(path.read_text(encoding="utf-8")).get("order", [])]
    except (ValueError, AttributeError):
        return []


DISPATCH_PENDING_MINUTES = 10


def _dispatch_pending(item: WorkItem) -> bool:
    """刚派发、子进程还没把它占为 in_progress 的工作项，几分钟内不重复派发。"""
    stamp = item.extra.get("dispatch_pending")
    if not stamp:
        return False
    from agentops.timeutil import parse_iso

    try:
        return (now() - parse_iso(str(stamp))).total_seconds() < DISPATCH_PENDING_MINUTES * 60
    except ValueError:
        return False
