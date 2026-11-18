"""定时入口（C9）：ao capture、ao nightly、ao brief。launchd 调用它们；人也可以手动跑。

capture：每个仓库 `caffeinate -i just capture`。
nightly：`just nightly`，然后 ao pm，再 ao pm --dispatch --nightly，最后写简报。
失败只记事件不中断后续步骤。全部在沙箱外以用户身份运行，这是受信任的定时任务。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from agentops import budget, events, kpi, pm, pm_propose, queue, secret_store, sessions
from agentops import workitems as wi
from agentops.auth import AuthError
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.paths import home, logs_dir
from agentops.timeutil import now, now_iso


def _caffeinated(command: Sequence[str]) -> list[str]:
    binary = shutil.which("caffeinate")
    return [binary, "-i", *command] if binary else list(command)


def recipe_env(config: Config, repo: str, recipe: str) -> dict[str, str]:
    """定时任务的环境：在当前环境上加钥匙串里的 token（config [secrets]：名字 -> 环境变量）。

    会话沙箱里跑不到这里，token 只进子进程环境，不进日志；缺哪个只记事件，任务照跑。
    """
    env = dict(os.environ)
    for name, var in config.secrets.items():
        try:
            env[str(var)] = secret_store.read_secret(str(name))
        except AuthError as exc:
            events.emit(
                f"{recipe}_secret_missing", repo=repo, detail={"secret": name, "error": str(exc)}
            )
    return env


def run_recipe(
    config: Config, repo: str, recipe: str, *, timeout_minutes: int | None = None
) -> int:
    """在主仓库里跑 `just <recipe>`，输出到 ~/.agentops/logs/<repo>/，返回退出码。

    超时取 config `[schedule].<recipe>_timeout_minutes`（默认 60）；首拉全市场要几小时。
    """
    if timeout_minutes is None:
        timeout_minutes = int(config.schedule.get(f"{recipe}_timeout_minutes", 60))  # type: ignore[arg-type]
    main_repo = config.repo_path(repo)
    if not (main_repo / "justfile").exists():
        events.emit(f"{recipe}_skipped", repo=repo, detail={"reason": "没有 justfile"})
        return 0
    log = logs_dir(repo) / f"{recipe}-{now_iso()[:19].replace(':', '')}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    events.emit(f"{recipe}_start", repo=repo, detail={"log": str(log)})
    env = recipe_env(config, repo, recipe)
    with open(log, "ab") as handle:
        try:
            rc = subprocess.run(
                _caffeinated(["just", recipe]),
                cwd=str(main_repo),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout_minutes * 60,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except OSError as exc:
            handle.write(f"无法运行: {exc}\n".encode())
            rc = 127
    events.emit(
        f"{recipe}_end",
        repo=repo,
        detail={"rc": rc, "duration_s": round(time.monotonic() - started, 1), "log": str(log)},
    )
    return rc


def active_repos(config: Config) -> tuple[str, ...]:
    return tuple(r for r in config.repos if (config.repo_path(r) / ".git").exists())


def capture(config: Config, repos: Sequence[str] | None = None) -> dict[str, int]:
    return {repo: run_recipe(config, repo, "capture") for repo in (repos or active_repos(config))}


def nightly(
    config: Config, repos: Sequence[str] | None = None, *, dispatch: bool = True
) -> dict[str, object]:
    """夜间任务。每一步失败只记事件，后面的步骤照跑。"""
    results: dict[str, object] = {}
    events.emit("nightly_run_start", detail={"repos": list(repos or active_repos(config))})
    for repo in repos or active_repos(config):
        rc = run_recipe(config, repo, "nightly")
        step: dict[str, object] = {"just_nightly_rc": rc}
        try:
            report = pm.plan(config, repo)
            step["pm"] = {
                "source": report.source,
                "cost_usd": report.cost_usd,
                "order": list(report.dispatch_order),
            }
        except AgentopsError as exc:
            events.emit("nightly_step_error", repo=repo, detail={"step": "pm", "error": str(exc)})
            step["pm"] = {"error": str(exc)}
        if bool(config.pm.get("propose", False)):
            try:
                proposal = pm_propose.propose(config, repo)
                step["proposals"] = list(proposal.created)
                if proposal.skipped_reason:
                    step["proposals_skipped"] = proposal.skipped_reason
            except AgentopsError as exc:
                events.emit(
                    "nightly_step_error", repo=repo, detail={"step": "propose", "error": str(exc)}
                )
                step["proposals"] = {"error": str(exc)}
        if dispatch:
            try:
                step["dispatched"] = list(pm.dispatch(config, repo, nightly=True))
            except AgentopsError as exc:
                events.emit(
                    "nightly_step_error", repo=repo, detail={"step": "dispatch", "error": str(exc)}
                )
                step["dispatched"] = {"error": str(exc)}
        if dispatch and isinstance(step.get("dispatched"), list):
            step["settled"] = wait_for_items(config, repo, step["dispatched"])
        results[repo] = step
    results["brief"] = str(write_brief(config))
    events.emit("nightly_run_end", detail={"results": results})
    return results


SETTLED_STATUSES = frozenset({"landed", "needs_human", "dropped"})
NOT_STARTED_STATUSES = frozenset({"ready", "backlog"})
WAIT_POLL_SECONDS = 20
NOT_STARTED_GRACE_POLLS = (
    3  # 派发后 ao start 要一两秒才把状态改成 in_progress；连续 3 次还是 ready 才算没启动
)


def wait_for_items(config: Config, repo: str, item_ids: Sequence[int]) -> dict[str, str]:
    """等夜间派发的工作项合入（或转人工）后再写简报，简报才会包含它们的结果。

    上限 `[schedule].nightly_wait_minutes`（默认 30）；到时未完成的照常写简报，只记事件。
    """
    minutes = float(config.schedule.get("nightly_wait_minutes", 30))  # type: ignore[arg-type]
    deadline = time.monotonic() + minutes * 60
    pending = {int(i) for i in item_ids}
    statuses: dict[str, str] = {}
    not_started: dict[int, int] = {}
    while pending:
        for item_id in sorted(pending):
            status = wi.load(repo, item_id).status
            statuses[str(item_id)] = status
            if status in NOT_STARTED_STATUSES:
                not_started[item_id] = not_started.get(item_id, 0) + 1
            if status in SETTLED_STATUSES or not_started.get(item_id, 0) >= NOT_STARTED_GRACE_POLLS:
                pending.discard(item_id)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(WAIT_POLL_SECONDS)
    events.emit(
        "nightly_wait_end",
        repo=repo,
        detail={"statuses": statuses, "timed_out": bool(pending), "minutes": minutes},
    )
    return statuses


def briefs_dir() -> Path:
    path = home() / "briefs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_brief(config: Config, day: str | None = None) -> Path:
    """每日一页简报：过去 24 小时发生了什么、花了多少、队列和需要人看的项、7 天 KPI。"""
    stamp = day or now().date().isoformat()
    since = now() - timedelta(hours=24)
    recent = [e for e in events.read_events() if e.time >= since]
    counts: dict[str, int] = {}
    for e in recent:
        counts[e.type] = counts.get(e.type, 0) + 1
    lines = [f"# agentops 每日简报 {stamp}", "", f"生成：{now_iso()}；区间：过去 24 小时", ""]
    lines += ["## 发生了什么", ""]
    for key, label in (
        ("landed", "合入"),
        ("returned", "退回"),
        ("needs_human", "转人工"),
        ("approved", "审批"),
        ("rebase_conflict", "冲突"),
        ("requeued", "重排队"),
        ("session_end", "会话结束"),
        ("dispatch", "派发"),
        ("pm_proposed", "PM 建议"),
        ("capture_end", "采集"),
        ("nightly_end", "夜间 just nightly"),
        ("budget_refused", "预算拒绝"),
        ("hook_block", "hook 拦截"),
        ("main_protect_block", "main 保护拦截"),
    ):
        if counts.get(key):
            lines.append(f"- {label}：{counts[key]}")
    if len(lines) == 6:
        lines.append("- 没有事件")
    landed = [e for e in recent if e.type == "landed"]
    if landed:
        lines += ["", "## 合入的工作项", ""] + [
            f"- {e.repo}#{e.item} @ {str(e.detail.get('landed_sha', ''))[:7]}（{e.ts[:16]}）"
            for e in landed
        ]
    lines += ["", "## 需要你看", ""]
    attention = [
        i
        for i in wi.list_items()
        if i.status == "needs_human" or int(i.extra.get("returns", 0) or 0) >= 2
    ]  # type: ignore[arg-type]
    lines += [
        f"- {i.ref} {i.status}：{str(i.extra.get('hint') or '')[:120]}" for i in attention
    ] or ["- 无"]
    proposals = pm_propose.open_proposals()
    if proposals:
        lines += ["", "## PM 建议（待你在看板采纳或放弃）", ""] + [
            f"- {i.ref} {i.title}：{str(i.extra.get('pm_reason') or '')[:120]}" for i in proposals
        ]
    lines += ["", "## 队列与会话", ""]
    for repo in active_repos(config):
        lines.append(f"- {repo} 待处理 {len(queue.pending(repo))} 项")
    stale = float(config.sessions.get("stale_after_hours", 6))  # type: ignore[arg-type]
    running = len(sessions.active(stale_after_hours=stale))
    lines.append(
        f"- 运行中会话 {running}；今日花费 ${budget.spend_today():.2f} / ${config.daily_budget_usd}"
    )
    lines += [
        "",
        "## 7 天 KPI",
        "",
        "```",
        kpi.format_report(kpi.compute(now() - timedelta(days=7))),
        "```",
        "",
    ]
    path = briefs_dir() / f"{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    events.emit("brief_written", detail={"path": str(path), "events": len(recent)})
    return path


def shell_quote(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)
