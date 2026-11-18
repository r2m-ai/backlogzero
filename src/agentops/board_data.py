"""看板（C10）的 10 个面板数据，全部由确定性数据源算出，不调用 LLM。"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agentops import budget, events, kpi, pm_propose, queue, sessions, slots
from agentops import workitems as wi
from agentops.config import Config
from agentops.paths import home, slots_dir
from agentops.timeutil import now, parse_iso

EDGAR_STALE_DAYS = 92
SLOT_BUSY = "占用"
SLOT_FREE = "空闲"
RAN = "已运行"
NOT_RAN = "未运行"


def is_approval_gate(item: wi.WorkItem) -> bool:
    """没有 needs_human_kind 的旧项（#7 之前）按审批门对待。"""
    return item.extra.get("needs_human_kind", "approval_gate") == "approval_gate"


TODAY_EVENT_TYPES = (
    "capture_start",
    "capture_end",
    "capture_noop",
    "nightly_run_start",
    "nightly_run_end",
    "nightly_start",
    "nightly_end",
    "pm_run",
    "dispatch",
    "auto_dispatch",
    "session_start",
    "session_end",
    "land_requested",
    "landed",
    "returned",
    "needs_human",
    "approved",
    "brief_written",
    "budget_refused",
)


@dataclass(frozen=True)
class Panel:
    key: str
    title: str
    rows: Sequence[Mapping[str, object]]
    note: str = ""
    error: str = ""


def _safe(key: str, title: str, build) -> Panel:
    """任何一个面板出错都只显示错误，不拖垮整页。"""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - 看板必须能显示
        return Panel(key, title, (), error=f"{type(exc).__name__}: {exc}")


def attention(config: Config) -> Panel:
    def build() -> Panel:
        rows: list[dict[str, object]] = []
        drops = _latest_drop_candidates()
        for item in wi.list_items():
            reasons = []
            if item.status == "needs_human":
                reasons.append("needs_human")
            if int(item.extra.get("returns", 0) or 0) >= 2:  # type: ignore[arg-type]
                reasons.append(f"退回 {item.extra.get('returns')} 次")
            if item.id in drops.get(item.repo, {}):
                reasons.append(f"PM 建议砍掉：{drops[item.repo][item.id]}")
            if reasons:
                rows.append(
                    {
                        "ref": item.ref,
                        "repo": item.repo,
                        "id": item.id,
                        "status": item.status,
                        "why": "；".join(reasons),
                        "hint": str(item.extra.get("hint") or "")[:300],
                        "approval_sha": str(item.extra.get("approval_sha") or ""),
                        "can_approve": item.status == "needs_human"
                        and bool(item.extra.get("approval_sha"))
                        and is_approval_gate(item),
                        "can_requeue": item.status == "needs_human"
                        and bool(item.extra.get("approval_sha"))
                        and bool(item.extra.get("clone_path"))
                        and bool(item.branch or item.extra.get("branch"))
                        and not is_approval_gate(item),
                        "can_drop": item.status not in ("landed", "dropped"),
                    }
                )
        return Panel("attention", "需要你看", rows, note="" if rows else "暂无需要处理的工作项")

    return _safe("attention", "需要你看", build)


def _latest_drop_candidates() -> dict[str, dict[int, str]]:
    result: dict[str, dict[int, str]] = {}
    for event in events.read_events():
        if event.type == "pm_run" and event.repo:
            drops = event.detail.get("drops") or []
            result[event.repo] = {int(d): "见 pm-report.md" for d in drops}  # type: ignore[union-attr]
    return result


def proposals(config: Config) -> Panel:
    """PM 提出、等人决定的 backlog 工作项：采纳进 ready，或放弃。"""

    def build() -> Panel:
        rows = [
            {
                "ref": i.ref,
                "repo": i.repo,
                "id": i.id,
                "title": i.title,
                "module": i.module,
                "priority": i.priority,
                "reason": str(i.extra.get("pm_reason") or "")[:300],
                "acceptance": "；".join(i.acceptance)[:300],
                "proposed_at": str(i.extra.get("proposed_at") or "")[:16],
                "can_adopt": True,
                "can_drop": True,
            }
            for i in pm_propose.open_proposals()
        ]
        return Panel("proposals", "PM 建议", rows, note="" if rows else "没有待决定的建议")

    return _safe("proposals", "PM 建议", build)


def workitems(config: Config) -> Panel:
    def build() -> Panel:
        items = wi.list_items()
        rows = [
            {
                "ref": i.ref,
                "status": i.status,
                "priority": i.priority,
                "module": i.module,
                "trigger": i.trigger,
                "deps": ",".join(map(str, i.depends_on)) or "-",
                "blocked_by": ",".join(map(str, wi.blocked_by(i, items=items))) or "-",
                "title": i.title,
            }
            for i in items
        ]
        return Panel("workitems", "工作项", rows, note="" if rows else "暂无工作项")

    return _safe("workitems", "工作项", build)


def running_sessions(config: Config) -> Panel:
    def build() -> Panel:
        stale = float(config.sessions.get("stale_after_hours", 6))  # type: ignore[arg-type]
        rows = [
            {
                "session": s.session_id[:8],
                "item": f"{s.repo}#{s.item}" if s.repo and s.item is not None else "-",
                "mode": s.mode or "-",
                "branch": s.branch or "-",
                "since": s.ts,
            }
            for s in sessions.active(stale_after_hours=stale)
        ]
        return Panel("sessions", "正在跑的会话", rows, note="" if rows else "暂无会话")

    return _safe("sessions", "正在跑的会话", build)


def merge_queue(config: Config) -> Panel:
    def build() -> Panel:
        rows: list[dict[str, object]] = []
        for repo in config.repos:
            latest = sorted(queue.latest(repo).values(), key=lambda line: line.ts)[-8:]
            for line in latest:
                rows.append(
                    {
                        "repo": repo,
                        "item": f"{repo}#{line.request.item}",
                        "status": line.status,
                        "sha": line.request.sha[:7],
                        "ts": line.ts,
                        "detail": json.dumps(dict(line.detail), ensure_ascii=False)[:120],
                    }
                )
        return Panel("queue", "合并队列", rows, note="" if rows else "队列为空")

    return _safe("queue", "合并队列", build)


def slot_usage(config: Config) -> Panel:
    def build() -> Panel:
        rows = []
        root = slots_dir()
        names = (
            sorted({p.name.rsplit(".", 2)[0] for p in root.glob("*.lock")}) if root.exists() else []
        )
        for name in names or ["test"]:
            count = config.slot_count(name)
            for index in range(1, count + 1):
                handle = slots.try_lock(slots.lock_path(name, index))
                held = handle is None
                if handle is not None:
                    handle.close()
                rows.append({"slot": f"{name}.{index}", "state": SLOT_BUSY if held else SLOT_FREE})
        return Panel("slots", "重任务槽位", rows)

    return _safe("slots", "重任务槽位", build)


def today(config: Config) -> Panel:
    def build() -> Panel:
        start = now().replace(hour=0, minute=0, second=0, microsecond=0)
        todays = [
            e for e in events.read_events() if e.time >= start and e.type in TODAY_EVENT_TYPES
        ]
        counts: dict[str, int] = {}
        for e in todays:
            counts[e.type] = counts.get(e.type, 0) + 1
        rows = [
            {
                "when": f"{config.schedule['capture']} 计划",
                "what": "capture（采集）",
                "status": _ran(counts, "capture_end", "capture_noop"),
            }
        ]
        rows.append(
            {
                "when": f"{config.schedule['nightly']} 计划",
                "what": "nightly（dbt → PM → 派发 → 简报）",
                "status": _ran(counts, "nightly_run_end", "nightly_end"),
            }
        )
        for key, label in (
            ("landed", "合入"),
            ("returned", "退回"),
            ("needs_human", "转人工"),
            ("dispatch", "派发"),
            ("session_end", "会话结束"),
            ("budget_refused", "预算拒绝"),
        ):
            if counts.get(key):
                rows.append({"when": "今天", "what": label, "status": str(counts[key])})
        rows.append(
            {
                "when": "今天",
                "what": "花费（估算）",
                "status": f"${budget.spend_today():.2f} / ${config.daily_budget_usd}",
            }
        )
        return Panel("today", "今天的计划和实际运行", rows)

    return _safe("today", "今天的计划和实际运行", build)


def _ran(counts: Mapping[str, int], *keys: str) -> str:
    return RAN if any(counts.get(k) for k in keys) else NOT_RAN


def dbt_results(config: Config) -> Panel:
    def build() -> Panel:
        rows = []
        for repo in config.repos:
            path = config.repo_path(repo) / "dbt" / "target" / "run_results.json"
            if not path.exists():
                rows.append(
                    {
                        "repo": repo,
                        "generated": "-",
                        "pass": "-",
                        "warn": "-",
                        "error": "-",
                        "note": "暂无数据（尚未 dbt build）",
                    }
                )
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            statuses = [str(r.get("status")) for r in data.get("results", [])]
            rows.append(
                {
                    "repo": repo,
                    "generated": str(data.get("metadata", {}).get("generated_at", "-")),
                    "pass": statuses.count("success") + statuses.count("pass"),
                    "warn": statuses.count("warn"),
                    "error": statuses.count("error") + statuses.count("fail"),
                    "note": "",
                }
            )
        return Panel("dbt", "dbt 结果", rows)

    return _safe("dbt", "dbt 结果", build)


def _latest_mtime(root: Path, pattern: str) -> datetime | None:
    if not root.exists():
        return None
    stamps = [p.stat().st_mtime for p in root.rglob(pattern) if p.is_file()]
    return datetime.fromtimestamp(max(stamps)).astimezone() if stamps else None


def freshness(config: Config) -> Panel:
    def build() -> Panel:
        rows = []
        for repo in config.repos:
            data_root = config.repo_path(repo).parent.parent / "data" / repo
            manifest = data_root / "lake" / "freshness.json"
            if manifest.exists():
                info = json.loads(manifest.read_text(encoding="utf-8"))
                for key, value in info.items():
                    rows.append({"source": f"{repo} {key}", "latest": str(value), "note": ""})
            else:
                latest = _latest_mtime(data_root / "lake", "*.parquet")
                rows.append(
                    {
                        "source": f"{repo} yfinance 日线",
                        "latest": latest.date().isoformat() if latest else "暂无数据",
                        "note": "按文件时间；采集任务写 freshness.json 后按内容",
                    }
                )
            edgar = _latest_mtime(
                config.data_path("edgar_manual")
                if repo == "quant-lab"
                else data_root / "edgar_manual",
                "*",
            )
            if edgar is None:
                rows.append(
                    {
                        "source": f"{repo} EDGAR 手动目录",
                        "latest": "暂无文件",
                        "note": "请手动下载 form.idx 放入 edgar_manual/",
                    }
                )
            else:
                stale = now() - edgar > timedelta(days=EDGAR_STALE_DAYS)
                rows.append(
                    {
                        "source": f"{repo} EDGAR 手动目录",
                        "latest": edgar.date().isoformat(),
                        "note": "超过一个季度，请手动更新" if stale else "",
                    }
                )
        return Panel("freshness", "数据新鲜度", rows)

    return _safe("freshness", "数据新鲜度", build)


def disk(config: Config) -> Panel:
    def build() -> Panel:
        rows = []
        for label, path in (
            ("home", home()),
            ("data", Path.home() / "data"),
            ("clones", config.clones_root()),
        ):
            target = path if path.exists() else Path.home()
            usage = shutil.disk_usage(target)
            rows.append(
                {
                    "what": label,
                    "path": str(path),
                    "free_gb": f"{usage.free / 1e9:.0f}",
                    "used_pct": f"{usage.used / usage.total * 100:.0f}%",
                }
            )
        return Panel("disk", "磁盘", rows)

    return _safe("disk", "磁盘", build)


def kpi_week(config: Config) -> Panel:
    def build() -> Panel:
        data = kpi.compute(now() - timedelta(days=7))
        rows = [
            {"metric": line.split("（")[0], "value": line}
            for line in kpi.format_report(data).splitlines()[1:-1]
        ]
        return Panel("kpi", "KPI（7 天）", rows)

    return _safe("kpi", "KPI（7 天）", build)


def all_panels(config: Config) -> tuple[Panel, ...]:
    return (
        attention(config),
        proposals(config),
        workitems(config),
        running_sessions(config),
        merge_queue(config),
        slot_usage(config),
        today(config),
        dbt_results(config),
        freshness(config),
        disk(config),
        kpi_week(config),
    )


def latest_brief() -> Path | None:
    root = home() / "briefs"
    if not root.exists():
        return None
    briefs = sorted(root.glob("*.md"))
    return briefs[-1] if briefs else None


def parse_ts(text: str) -> datetime:
    return parse_iso(text)
