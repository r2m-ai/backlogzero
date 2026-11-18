"""ao start（C2）：一项一会话。

预检、预算预留、占位、登记在一把机器级锁里完成；然后建独立克隆、渲染会话配置、写启动脚本。
交互模式开 Terminal 窗口，无人值守模式跑 claude -p 并把 total_cost_usd 记进事件。
退回时 resume 原会话（--fork-session，每轮成本独立计），最多 max_returns 轮。

会话跑在哪个 agent CLI 上由仓库的 runtime 决定（agentops.runtime.*）：启动脚本、会话配置、
结果解析都走那一层。Codex 仓库目前只有无人值守，退回重做在后续阶段。
"""

from __future__ import annotations

import fcntl
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agentops import budget, clones, events, notify, queue, sessions
from agentops import policy as policy_mod
from agentops import repo as repo_tools
from agentops import workitems as wi
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.paths import home, logs_dir
from agentops.runtime import claude as claude_rt
from agentops.runtime import codex as codex_rt
from agentops.runtime import codex_worker
from agentops.runtime.base import DEFAULT_RUNTIME, WorkerResult
from agentops.timeutil import now_iso
from agentops.workitem_model import WorkItem

TEST_GRANT_KEY = "allow_test_edits"
TEMPLATES = repo_tools.TEMPLATES / "prompts"
MODES = ("interactive", "headless")
OSASCRIPT_TIMEOUT = 30


@dataclass(frozen=True)
class Launch:
    item: WorkItem
    clone: Path
    branch: str
    session_id: str
    name: str
    mode: str
    model: str
    budget_usd: float
    directory: Path
    prompt_path: Path
    settings_path: Path
    script_path: Path
    result_path: Path
    effort: str = ""  # 空 = 用 CLI 默认
    runtime: str = DEFAULT_RUNTIME
    last_message_path: Path | None = None  # Codex：-o 写出的最后回复


@dataclass(frozen=True)
class HeadlessResult:
    session_id: str
    rc: int
    is_error: bool
    cost_usd: float
    cost_delta_usd: float
    num_turns: int
    land_requested: bool
    summary: str


def sessions_root() -> Path:
    path = home() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render(template_name: str, **values: object) -> str:
    text = (TEMPLATES / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


@contextmanager
def start_lock() -> Iterator[None]:
    """预检、预算预留、占位、登记在同一把机器级锁里完成，两个启动进程不会同时通过检查。"""
    with open(home() / "start.lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _active_count(config: Config, *, exclude_session: str | None = None) -> int:
    stale_hours = float(config.sessions.get("stale_after_hours", 6))  # type: ignore[arg-type]
    running = sessions.active(stale_after_hours=stale_hours)
    return len([s for s in running if s.session_id != exclude_session])


def preflight(config: Config, item: WorkItem) -> None:
    if item.status != "ready":
        raise AgentopsError(f"{item.ref} 状态是 {item.status}，只有 ready 的工作项能启动")
    blocked = wi.blocked_by(item)
    if blocked:
        raise AgentopsError(f"{item.ref} 被依赖挡住：{', '.join(map(str, blocked))} 还没 landed")
    busy = wi.module_busy(item.repo, item.module, exclude_id=item.id)
    if busy:
        raise AgentopsError(f"模块 {item.module} 已有会话在跑：{', '.join(i.ref for i in busy)}")
    active = _active_count(config)
    if active >= config.max_sessions:
        raise AgentopsError(f"并发已满：{active}/{config.max_sessions}")


def test_edit_grants(item: WorkItem) -> tuple[str, ...]:
    raw = item.extra.get(TEST_GRANT_KEY)
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p.strip())


def _grant_section(grants: tuple[str, ...]) -> str:
    if not grants:
        return ""
    listing = "\n".join(f"- {path}" for path in grants)
    return (
        "\n\n## 授权修改的已有测试\n\n"
        "人明确允许本工作项修改下面这些已有测试文件（只有清单内的文件可改，"
        "其余已有测试仍然不能动）：\n\n"
        f"{listing}\n"
    )


def build_prompt(item: WorkItem, clone: Path, branch: str) -> str:
    agents = clone / "AGENTS.md"
    prompt = render(
        "session.md",
        ref=item.ref,
        title=item.title,
        branch=branch,
        workitem=wi.item_path(item.repo, item.id).read_text(encoding="utf-8"),
        agents=agents.read_text(encoding="utf-8") if agents.exists() else "（仓库没有 AGENTS.md）",
    )
    return prompt + _grant_section(test_edit_grants(item))


def _run_prepare(policy: policy_mod.Policy, clone: Path, timeout_minutes: int) -> None:
    if not policy.prepare_cmd:
        return
    result = subprocess.run(
        shlex.split(policy.prepare_cmd),
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_minutes * 60,
    )
    if result.returncode != 0:
        raise AgentopsError(
            f"克隆里执行 {policy.prepare_cmd!r} 失败: {result.stderr.strip()[-400:]}"
        )


def _max_turns() -> int:
    return int(os.environ.get("AGENTOPS_HEADLESS_MAX_TURNS", "80"))


def _script(launch: Launch, *, resume_prompt: Path | None = None) -> str:
    if launch.runtime == codex_rt.RUNTIME:
        return codex_worker.worker_script(launch)
    return claude_rt.worker_script(launch, max_turns=_max_turns(), resume_prompt=resume_prompt)


def _launch(
    item: WorkItem,
    clone: Path,
    branch: str,
    session_id: str,
    *,
    mode: str,
    model: str,
    budget_usd: float,
    effort: str | None = None,
    runtime: str = DEFAULT_RUNTIME,
    origin_session_id: str | None = None,
) -> Launch:
    # 会话材料（settings、brief、脚本、resume-N.md）一律放首轮会话目录；fork 出的新 id 不建目录
    directory = sessions_root() / (origin_session_id or session_id)
    chosen_effort = effort if effort is not None else str(item.extra.get("effort") or "")
    is_codex = runtime == codex_rt.RUNTIME
    return Launch(
        effort=chosen_effort,
        runtime=runtime,
        item=item,
        clone=clone,
        branch=branch,
        session_id=session_id,
        name=f"{item.ref} {item.module} {item.title}",
        mode=mode,
        model=model,
        budget_usd=budget_usd,
        directory=directory,
        prompt_path=directory / "prompt.md",
        settings_path=(
            codex_worker.profile_path(session_id) if is_codex else directory / "settings.json"
        ),
        script_path=directory / "start.sh",
        result_path=directory / ("events.jsonl" if is_codex else "result.json"),
        last_message_path=directory / "last-message.md" if is_codex else None,
    )


def rollback_launch(config: Config, item: WorkItem, session_id: str, *, reason: str) -> None:
    """启动没成功：工作项回到 ready，释放会话、预留和克隆。"""
    current = wi.load(item.repo, item.id)
    if current.session_id == session_id:
        wi.apply(
            current,
            status="ready",
            force=True,
            session_id=None,
            origin_session_id=None,
            clone_path=None,
            mode=None,
        )
    try:
        sessions.end(session_id, rc=None)
    except AgentopsError:
        pass
    budget.release(session_id, reason)
    if config.runtime_for(item.repo) == codex_rt.RUNTIME:
        codex_worker.cleanup(session_id, clones.clone_dir(config, item))
    clones.remove_clone(config, item, reason="rollback")
    events.emit(
        "session_rollback",
        repo=item.repo,
        item=item.id,
        session=session_id,
        detail={"reason": reason},
    )


def prepare_launch(
    config: Config,
    item: WorkItem,
    *,
    mode: str,
    model: str | None = None,
    budget_usd: float | None = None,
) -> Launch:
    """锁内预检、预留预算、占位、登记；锁外建克隆、准备依赖、写提示词和会话配置。失败回滚。"""
    if mode not in MODES:
        raise AgentopsError(f"mode 必须是 {'/'.join(MODES)}")
    main_repo = config.repo_path(item.repo)
    runtime = config.runtime_for(item.repo)
    is_codex = runtime == codex_rt.RUNTIME
    if is_codex and mode != "headless":
        raise AgentopsError(
            f"{item.ref}：Codex 仓库目前只支持无人值守，请用 ao start --headless（交互会话还没做）"
        )
    policy = policy_mod.load_policy(main_repo, item.repo)
    session_id = str(uuid.uuid4())
    # 工作项里可以指定 model / effort（复杂项用更强的模型），否则用 config 里该运行时的 worker 默认
    chosen_model = model or str(item.extra.get("model") or config.model_for("worker", runtime))
    chosen_effort = str(item.extra.get("effort") or config.effort_for("worker", runtime) or "")
    chosen_budget = float(
        budget_usd
        if budget_usd is not None
        else (item.extra.get("budget_usd") or config.budget_usd["worker"])  # type: ignore[arg-type]
    )
    branch = clones.branch_name(item)
    launch = _launch(
        item,
        clones.clone_dir(config, item),
        branch,
        session_id,
        mode=mode,
        model=chosen_model,
        budget_usd=chosen_budget,
        effort=chosen_effort,
        runtime=runtime,
    )
    with start_lock():
        preflight(config, item)
        if is_codex:  # 订阅额度没有美元上限，先过当日 token 硬上限
            budget.require_tokens(
                "worker",
                runtime=runtime,
                limit=config.token_limit("worker"),
                label=f"start {item.ref}",
            )
        budget.require(
            session_id, chosen_budget, limit=config.daily_budget_usd, label=f"start {item.ref}"
        )
        try:
            wi.apply(
                item,
                status="in_progress",
                dispatch_pending=None,
                skip_reason=None,
                skip_since=None,
                session_id=session_id,
                origin_session_id=session_id,
                branch=branch,
                mode=mode,
                model=chosen_model,
                budget_usd=chosen_budget,
            )
            sessions.register(
                session_id,
                repo=item.repo,
                item=item.id,
                branch=branch,
                worktree=None,
                mode=mode,
                name=launch.name,
            )
        except BaseException as exc:
            budget.release(session_id, f"启动失败：{exc}")
            raise
    try:
        info = clones.create_clone(config, item, default_branch=policy.default_branch)
        _run_prepare(policy, info.path, config.steward_int("test_timeout_minutes"))
        launch.directory.mkdir(mode=0o700, exist_ok=True)
        launch.prompt_path.write_text(build_prompt(item, info.path, info.branch), encoding="utf-8")
        if is_codex:
            # 会话 profile、给 hooks 的克隆标记（带 main 上的策略快照）、沙箱自检
            codex_worker.prepare(launch, policy=policy, repos=tuple(config.repos))
        else:
            launch.settings_path.write_text(
                repo_tools.render_session_settings(
                    item.repo,
                    info.path,
                    main_repo=main_repo,
                    other_clones=clones.list_clones(config, item.repo),
                ),
                encoding="utf-8",
            )
        launch.script_path.write_text(_script(launch), encoding="utf-8")
        launch.script_path.chmod(0o700)
    except (AgentopsError, subprocess.TimeoutExpired, OSError) as exc:
        rollback_launch(config, item, session_id, reason=f"启动失败：{exc}")
        raise AgentopsError(f"{item.ref} 启动失败，已回滚：{exc}") from exc
    except Exception as exc:
        rollback_launch(config, item, session_id, reason=f"启动失败：{exc}")
        raise
    wi.apply(wi.load(item.repo, item.id), clone_path=str(info.path))
    sessions.register(
        session_id,
        repo=item.repo,
        item=item.id,
        branch=info.branch,
        worktree=str(info.path),
        mode=mode,
        name=launch.name,
    )
    events.emit(
        "session_start",
        repo=item.repo,
        item=item.id,
        session=session_id,
        detail={
            "mode": mode,
            "runtime": runtime,
            "model": chosen_model,
            "budget_usd": chosen_budget,
            "clone": str(info.path),
            "branch": info.branch,
        },
    )
    return launch


def start_interactive(launch: Launch) -> None:
    """在 Terminal.app 新窗口里运行启动脚本。第一次会弹一次自动化权限。"""
    command = f"bash {shlex.quote(str(launch.script_path))}"
    script = f'tell application "Terminal"\n  activate\n  do script "{command}"\nend tell'
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentopsError(
            f"无法打开 Terminal: {exc}。第一次会弹“允许控制 Terminal”的权限提示，请点允许后重试"
        ) from exc
    if result.returncode != 0:
        raise AgentopsError(f"打开 Terminal 失败: {result.stderr.strip()}")
    events.emit(
        "session_window_opened",
        repo=launch.item.repo,
        item=launch.item.id,
        session=launch.session_id,
    )


def _land_requested(item: WorkItem, since_ts: str) -> bool:
    inbox = queue.land_inbox_dir()
    if inbox.exists() and any(inbox.glob(f"{item.repo}#{item.id}@*.json")):
        return True
    return any(
        line.request.item == item.id and line.ts >= since_ts for line in queue.journal(item.repo)
    )


def _escalate_no_land(
    config: Config,
    item: WorkItem,
    result: HeadlessResult,
    *,
    session: str,
    runtime: str = DEFAULT_RUNTIME,
) -> None:
    """无人值守会话结束却没投递 ao land：工作项不能一直卡在 in_progress，转给人看。

    会话自己写不了工作项（沙箱只开 inbox），所以它按 AGENTS.md 停下时只能把原因留在最后回复里，
    这里把摘要带到 hint，看板的「需要你看」面板会显示。
    """
    reason = (
        f"无人值守会话结束但没有投递 ao land（rc {result.rc}"
        f"{f'，{runtime} 报错' if result.is_error else ''}）"
    )
    excerpt = " ".join(result.summary.split())[:300]
    hint = f"{reason}。会话最后回复：{excerpt}" if excerpt else reason
    wi.apply(item, status="needs_human", hint=hint, last_result=result.summary)
    events.emit(
        "needs_human",
        repo=item.repo,
        item=item.id,
        session=session,
        detail={
            "reason": reason,
            "rc": result.rc,
            "is_error": result.is_error,
            "source": "session_end",
        },
    )
    notify.notify(config, f"{item.repo}#{item.id} 需要你看", hint, repo=item.repo, item=item.id)


def _finish(
    config: Config,
    launch: Launch,
    rc: int,
    *,
    started_ts: str,
    resumed: bool,
    call_id: str,
) -> HeadlessResult:
    is_codex = launch.runtime == codex_rt.RUNTIME
    outcome = _worker_result(config, launch, rc)
    cost = outcome.cost_usd
    budget.settle(call_id, cost)
    is_error = outcome.is_error
    item = wi.load(launch.item.repo, launch.item.id)
    landed = _land_requested(item, started_ts)
    previous_total = float(item.extra.get("cost_usd_total") or 0.0)  # type: ignore[arg-type]
    cumulative = round(previous_total + cost, 6)
    # Codex 的 thread id 只记在事件和工作项里；ao 的会话 id 不跟着变
    new_session = launch.session_id if is_codex else (outcome.session_id or launch.session_id)
    if resumed and new_session != launch.session_id:
        sessions.register(
            new_session,
            repo=item.repo,
            item=item.id,
            branch=launch.branch,
            worktree=str(launch.clone),
            mode="headless",
            name=launch.name,
        )
        sessions.end(launch.session_id, rc=rc)
        item = wi.apply(item, session_id=new_session)
    result = HeadlessResult(
        session_id=new_session,
        rc=rc,
        is_error=is_error,
        cost_usd=cumulative,
        cost_delta_usd=cost,
        num_turns=outcome.num_turns,
        land_requested=landed,
        summary=outcome.summary[:500],
    )
    extra: dict[str, object] = {}
    if is_codex and outcome.session_id:
        extra["codex_thread_id"] = outcome.session_id
    item = wi.apply(
        item, cost_usd_total=cumulative, last_session_rc=rc, last_land_requested=landed, **extra
    )
    sessions.end(new_session, rc=rc, cost_usd=cumulative)
    if launch.mode == "headless" and not landed:
        _escalate_no_land(config, item, result, session=new_session, runtime=launch.runtime)
    events.emit(
        "session_end",
        repo=item.repo,
        item=item.id,
        session=new_session,
        detail={
            "mode": launch.mode,
            "runtime": launch.runtime,
            "role": "worker",
            "rc": rc,
            "is_error": is_error,
            "cost_usd": cost,
            "cost_total_usd": cumulative,
            "cost_estimated": outcome.cost_estimated,
            "price_known": outcome.price_known,
            "usage": None if outcome.usage is None else outcome.usage.to_dict(),
            "thread_id": outcome.session_id if is_codex else None,
            "num_turns": result.num_turns,
            "land_requested": landed,
            "resumed": resumed,
            "forked_from": launch.session_id if new_session != launch.session_id else None,
        },
    )
    return result


def _worker_result(config: Config, launch: Launch, rc: int) -> WorkerResult:
    if launch.runtime == codex_rt.RUNTIME:
        return codex_worker.parse_worker_result(launch, rc, prices=config.codex_prices)
    return claude_rt.parse_worker_result(launch.result_path, rc)


def run_headless(
    config: Config, launch: Launch, *, resume_prompt: Path | None = None, call_id: str | None = None
) -> HeadlessResult:
    """前台运行无人值守会话直到结束。resume_prompt 给出时是 --resume --fork-session 原会话。"""
    started = now_iso()
    if resume_prompt is None:
        script = launch.script_path
    else:
        script = launch.directory / f"resume-{started[:19].replace(':', '')}.sh"
        script.write_text(_script(launch, resume_prompt=resume_prompt), encoding="utf-8")
        script.chmod(0o700)
    log = logs_dir(launch.item.repo) / f"session-{launch.session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    timeout = int(config.sessions.get("headless_timeout_minutes", 60)) * 60  # type: ignore[arg-type]
    with open(log, "ab") as handle:
        try:
            completed = subprocess.run(
                ["bash", str(script)],
                cwd=str(launch.clone),
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            rc = completed.returncode
        except subprocess.TimeoutExpired:
            rc = 124
    try:
        return _finish(
            config,
            launch,
            rc,
            started_ts=started,
            resumed=resume_prompt is not None,
            call_id=call_id or launch.session_id,
        )
    finally:
        # 收尾中途出错也要删：留下的克隆标记会让之后同一路径上的 hook 认错会话
        if launch.runtime == codex_rt.RUNTIME:
            codex_worker.cleanup(launch.session_id, launch.clone)


def launch_from_item(config: Config, item: WorkItem) -> Launch:
    """从已启动过的工作项恢复 Launch（resume 用）。"""
    if not item.session_id:
        raise AgentopsError(f"{item.ref} 没有会话记录，不能 resume")
    clone = Path(str(item.extra.get("clone_path") or clones.clone_dir(config, item)))
    if not (clone / ".git").exists():
        raise AgentopsError(f"{item.ref} 的克隆 {clone} 不存在")
    runtime = config.runtime_for(item.repo)
    return _launch(
        item,
        clone,
        item.branch or clones.branch_name(item),
        item.session_id,
        mode=str(item.extra.get("mode") or "headless"),
        model=str(item.extra.get("model") or config.model_for("worker", runtime)),
        budget_usd=float(item.extra.get("budget_usd") or config.budget_usd["worker"]),  # type: ignore[arg-type]
        runtime=runtime,
        origin_session_id=str(item.extra.get("origin_session_id") or "") or None,
    )


def resume_headless(config: Config, item: WorkItem, *, hint: str, round_no: int) -> HeadlessResult:
    """被退回后让原无人值守会话重做一轮。

    锁内查容量、预留预算、登记；然后 --resume --fork-session。
    """
    if config.runtime_for(item.repo) == codex_rt.RUNTIME:
        # codex exec resume 会丢掉权限 profile（docs/codex-cli.md §8），要改成新开会话重做
        raise AgentopsError(
            f"{item.ref}：Codex 仓库的退回重做（新开会话）在阶段 D 实现，目前请人工处理"
        )
    launch = launch_from_item(config, item)
    if launch.mode != "headless":
        raise AgentopsError(f"{item.ref} 是交互会话，退回说明已写进工作项，请在原窗口继续")
    launch.directory.mkdir(parents=True, exist_ok=True)
    prompt_path = launch.directory / f"resume-{round_no}.md"
    safe_hint = hint.replace("</steward-hint>", "<\\/steward-hint>")
    prompt_path.write_text(
        render("resume.md", ref=item.ref, round=round_no, hint=safe_hint), encoding="utf-8"
    )
    call_id = f"{launch.session_id}:resume{round_no}"
    with start_lock():
        active = _active_count(config, exclude_session=launch.session_id)
        if active >= config.max_sessions:
            raise AgentopsError(f"并发已满：{active}/{config.max_sessions}，resume 稍后再试")
        budget.require(
            call_id, launch.budget_usd, limit=config.daily_budget_usd, label=f"resume {item.ref}"
        )
        try:
            wi.apply(item, status="in_progress")
            sessions.register(
                launch.session_id,
                repo=item.repo,
                item=item.id,
                branch=launch.branch,
                worktree=str(launch.clone),
                mode="headless",
                name=launch.name,
            )
        except BaseException as exc:
            budget.release(call_id, f"resume 失败：{exc}")
            raise
    try:
        events.emit(
            "session_resume",
            repo=item.repo,
            item=item.id,
            session=launch.session_id,
            detail={"round": round_no},
        )
        return run_headless(config, launch, resume_prompt=prompt_path, call_id=call_id)
    except BaseException as exc:
        budget.release(call_id, f"resume 失败：{exc}")  # 已结算则无操作
        raise


def spawn_detached(args: Sequence[str], log: Path) -> int:
    """后台起一个 ao 子命令（派发、resume 用），输出追加到日志，返回 pid。"""
    log.parent.mkdir(parents=True, exist_ok=True)
    ao = os.environ.get("AGENTOPS_AO") or os.path.expanduser("~/.local/bin/ao")
    with open(log, "ab") as handle:
        process = subprocess.Popen(
            [ao, *args],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process.pid


def print_launch(launch: Launch) -> None:
    lines = [
        f"会话 {launch.session_id}",
        f"  工作项 {launch.item.ref}",
        f"  克隆 {launch.clone}",
        f"  分支 {launch.branch}",
        f"  运行时 {launch.runtime}，模式 {launch.mode}，模型 {launch.model}，"
        f"预算 ${launch.budget_usd}",
        f"  脚本 {launch.script_path}",
    ]
    print("\n".join(lines), file=sys.stderr)
