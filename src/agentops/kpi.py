"""ao kpi（C7）：人类介入率、冲突率、返工率、回滚数、每项成本、平均排队时长。反向指标一起看。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime

from agentops import events
from agentops.timeutil import parse_iso


def compute(since: datetime | None = None, repo: str | None = None) -> Mapping[str, object]:
    selected = events.filter_events(events.read_events(), since=since, repo=repo)
    counts: dict[str, int] = defaultdict(int)
    cost_by_item: dict[str, float] = defaultdict(float)
    land_requested_at: dict[tuple[str, int, str], datetime] = {}
    waits: list[float] = []
    for event in selected:
        counts[event.type] += 1
        if event.type in ("session_end", "llm_call") and event.item is not None:
            cost_by_item[f"{event.repo}#{event.item}"] += float(event.detail.get("cost_usd") or 0.0)  # type: ignore[arg-type]
        if event.type == "land_requested" and event.item is not None:
            land_requested_at[(event.repo or "", event.item, str(event.detail.get("sha")))] = (
                parse_iso(event.ts)
            )
        if event.type == "integration_start" and event.item is not None:
            key = (event.repo or "", event.item, str(event.detail.get("sha")))
            started = land_requested_at.pop(key, None)
            if started is not None:
                waits.append((parse_iso(event.ts) - started).total_seconds())
    landed = counts["landed"]
    processed = counts["integration_start"]
    requested = counts["land_requested"]
    return {
        "since": since.isoformat() if since else None,
        "landed": landed,
        "needs_human": counts["needs_human"],
        "rebase_conflict": counts["rebase_conflict"],
        "returned": counts["returned"],
        "land_requested": requested,
        "processed": processed,
        "requeued": counts["requeued"],
        "rollbacks": counts["rollback"],
        "hook_blocks": counts["hook_block"],
        "human_intervention_rate": _ratio(counts["needs_human"], landed),
        "conflict_rate": _ratio(counts["rebase_conflict"], processed),
        "rework_rate": _ratio(counts["returned"], requested),
        "cost_by_item": dict(sorted(cost_by_item.items())),
        "cost_total": round(sum(cost_by_item.values()), 4),
        "avg_queue_wait_s": round(sum(waits) / len(waits), 1) if waits else None,
        "sessions": counts["session_start"],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def format_report(data: Mapping[str, object]) -> str:
    def pct(value: object) -> str:
        return "n/a" if value is None else f"{float(value) * 100:.0f}%"

    lines = [
        f"区间起点：{data['since'] or '全部'}",
        f"合入 {data['landed']}，needs_human {data['needs_human']}，退回 {data['returned']}，"
        f"冲突 {data['rebase_conflict']}，重排队 {data['requeued']}，回滚 {data['rollbacks']}，"
        f"hook 拦截 {data['hook_blocks']}",
        f"人类介入率 {pct(data['human_intervention_rate'])}（needs_human ÷ landed）",
        f"冲突率 {pct(data['conflict_rate'])}"
        f"（rebase_conflict ÷ 入队处理次数 {data['processed']}）",
        f"返工率 {pct(data['rework_rate'])}（returned ÷ land_requested {data['land_requested']}）",
        f"平均排队 {data['avg_queue_wait_s'] if data['avg_queue_wait_s'] is not None else 'n/a'}"
        " 秒",
        f"成本合计 ${data['cost_total']}（客户端估算），会话 {data['sessions']} 个",
    ]
    costs = data["cost_by_item"]
    if isinstance(costs, dict) and costs:
        lines.append("每项成本：" + "，".join(f"{k} ${v:.2f}" for k, v in costs.items()))
    lines.append("反向指标：回滚数和返工率要和介入率一起看，不能靠少问人压低介入率。")
    return "\n".join(lines)
