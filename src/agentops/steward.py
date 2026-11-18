"""steward 的确定性部分（C3）：读 inbox 入队，先进先出处理：SHA 检查、集成 rebase、槽位里跑测试、
策略检查、快进合入。只裁决不改代码；冲突和失败退回，规则触发转 needs_human。

集成副本是主仓库的一个游离 worktree <integration>/<repo>/_integration，
分支从工作项的独立克隆 fetch 进来。
所有 git 操作带 AGENTOPS_STEWARD=1，这是 main 保护钩子的放行信号。
"""

from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agentops import approvals, budget, clones, events, judge, notify, queue, sandbox, slots, trials
from agentops import policy as policy_mod
from agentops import workitems as wi
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.paths import home, logs_dir, steward_dir
from agentops.review import SUITE_ROOT

STEWARD_ENV = {"AGENTOPS_STEWARD": "1"}
LOG_TAIL_LINES = 40


@dataclass(frozen=True)
class Outcome:
    request: queue.LandRequest
    result: str  # landed | returned | needs_human | requeued | deferred | error
    reason: str
    detail: Mapping[str, object]


def _env() -> dict[str, str]:
    """steward 自己的 git 命令用：带 main 放行变量。"""
    return {**os.environ, **STEWARD_ENV}


def _candidate_env() -> dict[str, str]:
    """跑候选代码（prepare_cmd、test_cmd）用：绝不带放行变量，也不带会话 token。"""
    dropped = {
        "AGENTOPS_STEWARD",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
    return {k: v for k, v in os.environ.items() if k not in dropped}


def _candidate_command(
    config: Config, repo: str, command: list[str], workdir: Path, run_tmp: Path
) -> list[str]:
    """候选代码进 Seatbelt 沙箱：只能写集成副本、临时目录、uv 缓存，不能联网。"""
    if not bool(config.steward.get("sandbox", True)):
        return command
    if not sandbox.available():
        events.emit(
            "steward_warning", repo=repo, detail={"reason": "sandbox-exec 不可用，候选代码未隔离"}
        )
        return command
    profile = sandbox.profile(
        allow_write=sandbox.default_allow(workdir=workdir, log_dir=logs_dir(repo), run_tmp=run_tmp),
        deny_write=sandbox.default_deny(
            agentops_home=home(),
            main_repo=config.repo_path(repo),
            clones_root=config.clones_root(),
            suite_root=SUITE_ROOT,
        ),
    )
    return sandbox.wrap(command, profile)


def _git(path: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_env(),
        )
    except OSError as exc:
        raise AgentopsError(f"无法运行 git: {exc}") from exc


def _must(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise AgentopsError(f"{what}失败: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


@contextmanager
def steward_lock(repo: str) -> Iterator[None]:
    """同一仓库同一时刻只有一个 steward 在处理。"""
    steward_dir().mkdir(parents=True, exist_ok=True)
    with open(steward_dir() / f"{repo}.lock", "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentopsError(f"仓库 {repo} 已有 steward 在运行") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def integration_dir(config: Config, repo: str) -> Path:
    return config.integration_root() / repo / "_integration"


def ensure_integration(config: Config, repo: str, default_branch: str) -> Path:
    """主仓库的游离 worktree，steward 专用。缺就建，坏了就重建。"""
    main_repo = config.repo_path(repo)
    path = integration_dir(config, repo)
    if (path / ".git").exists() and _git(path, "rev-parse", "--git-dir").returncode == 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(main_repo, "worktree", "prune")
    if path.exists():
        import shutil

        shutil.rmtree(path)
    _must(
        _git(main_repo, "worktree", "add", "--detach", str(path), default_branch), "建集成 worktree"
    )
    return path


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _branch_tip(clone_path: Path, branch: str) -> str | None:
    if not (clone_path / ".git").exists():
        return None
    result = _git(
        clone_path,
        *clones.UNTRUSTED_GIT_OPTIONS,
        "rev-parse",
        "--verify",
        "-q",
        f"refs/heads/{branch}",
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _set_status(item: wi.WorkItem, status: str, **extra: object) -> wi.WorkItem:
    return wi.apply(item, status=status, **extra)


def _return(
    config: Config,
    request: queue.LandRequest,
    item: wi.WorkItem,
    reason: str,
    hint: str,
    detail: Mapping[str, object],
) -> Outcome:
    """退回原会话；退回次数超限就转 needs_human。"""
    returns = int(item.extra.get("returns", 0)) + 1  # type: ignore[arg-type]
    if returns > config.steward_int("max_returns"):
        _set_status(
            item,
            "needs_human",
            returns=returns,
            hint=f"退回 {returns} 次仍未通过：{reason}",
            needs_human_kind="max_returns",
            approval_sha=request.sha,
            clone_path=request.clone_path,
            branch=request.branch,
        )
        events.emit(
            "needs_human",
            repo=request.repo,
            item=request.item,
            session=request.session,
            detail={"reason": "max_returns", "returns": returns, **detail},
        )
        notify.notify(
            config,
            f"{request.repo}#{request.item} 需要你看",
            f"退回 {returns} 次仍未通过：{reason}",
            repo=request.repo,
            item=request.item,
        )
        queue.append(request.repo, request, "needs_human", {"reason": reason})
        return Outcome(request, "needs_human", reason, detail)
    _set_status(item, "returned", returns=returns, hint=hint)
    _maybe_resume(config, item, hint, returns)
    events.emit(
        "returned",
        repo=request.repo,
        item=request.item,
        session=request.session,
        detail={"reason": reason, "returns": returns, **detail},
    )
    queue.append(request.repo, request, "returned", {"reason": reason})
    return Outcome(request, "returned", reason, detail)


def _maybe_resume(config: Config, item: wi.WorkItem, hint: str, round_no: int) -> None:
    """原会话是无人值守时，在后台让它重做一轮（ao resume）。交互会话只写说明。"""
    if str(item.extra.get("mode") or "") != "headless" or not item.session_id:
        return
    if not bool(config.steward.get("auto_resume", True)):
        return
    runtime = config.runtime_for(item.repo)
    if runtime == "codex":
        # codex exec resume 会丢权限 profile，重做要新开会话，阶段 D 才有；这里明说而不是静默卡住
        reason = "Codex 仓库还没有自动重做，退回说明已写进工作项，请人工处理"
        events.emit(
            "resume_skipped",
            repo=item.repo,
            item=item.id,
            session=item.session_id,
            detail={"round": round_no, "runtime": runtime, "reason": reason},
        )
        notify.notify(config, f"{item.ref} 被退回", reason, repo=item.repo, item=item.id)
        return
    from agentops import start

    log = logs_dir(item.repo) / f"resume-{item.id}-{round_no}.log"
    pid = start.spawn_detached(["resume", item.ref, "--round", str(round_no)], log)
    events.emit(
        "resume_spawned",
        repo=item.repo,
        item=item.id,
        session=item.session_id,
        detail={"round": round_no, "pid": pid},
    )


def _needs_human(
    config: Config,
    request: queue.LandRequest,
    item: wi.WorkItem,
    reason: str,
    detail: Mapping[str, object],
    *,
    is_approval_gate: bool = False,
) -> Outcome:
    _set_status(
        item,
        "needs_human",
        hint=reason,
        needs_human_kind="approval_gate" if is_approval_gate else "failure",
        approval_sha=request.sha,
        clone_path=request.clone_path,
        branch=request.branch,
    )
    events.emit(
        "needs_human", repo=request.repo, item=request.item, session=request.session, detail=detail
    )
    notify.notify(
        config,
        f"{request.repo}#{request.item} 需要审批",
        reason,
        repo=request.repo,
        item=request.item,
    )
    queue.append(request.repo, request, "needs_human", {"reason": reason})
    return Outcome(request, "needs_human", reason, detail)


DIRTY_MAIN_MESSAGE = "主仓库有未提交改动，合入等它干净"


def _main_dirty_reason(main_repo: Path, default_branch: str) -> str | None:
    """主仓库不能快进的原因；可以快进返回 None。只看已跟踪文件的未提交改动：
    人放在主仓库里的未跟踪草稿不算脏，真要被合入覆盖时 git merge --ff-only 自己会拒绝。"""
    status = _git(main_repo, "status", "--porcelain", "--untracked-files=no")
    branch = _git(main_repo, "branch", "--show-current").stdout.strip()
    if status.stdout.strip() or branch != default_branch:
        return f"主仓库不在 {default_branch} 或工作区不干净，无法快进"
    return None


def _defer_land(
    config: Config, request: queue.LandRequest, item: wi.WorkItem, reason: str, *, requeue: bool
) -> Outcome:
    """主仓库不干净：请求留在队里等下一轮。同一请求只发一次 land_deferred 并通知。"""
    repo = request.repo
    if requeue:
        queue.append(repo, request, "requeued", {"reason": "land_deferred"})
        if item.status == "testing":
            _set_status(item, "queued")
    already = any(
        e.type == "land_deferred" and e.detail.get("request") == request.id
        for e in events.read_events()
        if e.repo == repo and e.item == request.item
    )
    if not already:
        events.emit(
            "land_deferred",
            repo=repo,
            item=request.item,
            session=request.session,
            detail={"request": request.id, "reason": reason, "sha": request.sha},
        )
        notify.notify(
            config,
            f"{repo}#{request.item} 合入延后",
            DIRTY_MAIN_MESSAGE,
            repo=repo,
            item=request.item,
        )
    return Outcome(request, "deferred", reason, {})


def _requeue(request: queue.LandRequest, tip: str, item: wi.WorkItem | None = None) -> Outcome:
    fresh = queue.new_request(
        repo=request.repo,
        item=request.item,
        branch=request.branch,
        sha=tip,
        clone_path=request.clone_path,
        session=request.session,
    )
    queue.append(
        request.repo,
        request,
        "dropped",
        {"superseded_by": fresh.id, "reason": "入队后分支有新提交"},
    )
    queue.append(request.repo, fresh, "requeued", {"supersedes": request.id})
    if item is not None and item.status == "testing":
        _set_status(item, "queued")
    events.emit(
        "requeued",
        repo=request.repo,
        item=request.item,
        session=request.session,
        detail={"old_sha": request.sha, "new_sha": tip},
    )
    return Outcome(request, "requeued", "入队后分支有新提交，移到队尾", {"new_sha": tip})


def _auto_dispatch(config: Config, repo: str, *, reason: str) -> tuple[int, ...]:
    """C8：依赖完成后自动启动下游。派发器自己遵守并发、模块锁、每日预算。"""
    if not bool(config.steward.get("auto_dispatch", True)):
        return ()
    from agentops import pm

    try:
        launched = pm.dispatch(config, repo)
    except AgentopsError as exc:
        events.emit("dispatch_error", repo=repo, detail={"reason": str(exc)})
        return ()
    if launched:
        events.emit("auto_dispatch", repo=repo, detail={"items": list(launched), "reason": reason})
    return launched


def _dispatchable_after(repo: str, landed_id: int) -> tuple[int, ...]:
    items = wi.list_items(repo)
    ready = tuple(
        item.id
        for item in items
        if landed_id in item.depends_on and wi.is_dispatchable(item, items=items)
    )
    if ready:
        events.emit("dispatchable", repo=repo, item=landed_id, detail={"items": list(ready)})
    return ready


def process_request(
    config: Config, request: queue.LandRequest, policy: policy_mod.Policy
) -> Outcome:
    """候选代码的专用临时目录只在本次处理期间存在。"""
    run_tmp = sandbox.make_run_tmp(request.repo)
    try:
        return _process_request_inner(config, request, policy, run_tmp)
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)


def _process_request_inner(
    config: Config, request: queue.LandRequest, policy: policy_mod.Policy, run_tmp: Path
) -> Outcome:
    repo = request.repo
    main_repo = config.repo_path(repo)
    item = wi.load(repo, request.item)
    clone_path = Path(request.clone_path)
    log = logs_dir(repo) / f"{request.id}.log"

    # 1. SHA 检查
    tip = _branch_tip(clone_path, request.branch)
    if tip is None:
        queue.append(repo, request, "error", {"reason": "克隆或分支不存在"})
        events.emit(
            "steward_error",
            repo=repo,
            item=request.item,
            detail={"reason": "克隆或分支不存在", "clone": request.clone_path},
        )
        return Outcome(request, "error", "克隆或分支不存在", {})
    if tip != request.sha:
        return _requeue(request, tip)

    dirty = _main_dirty_reason(main_repo, policy.default_branch)
    if dirty:
        return _defer_land(config, request, item, dirty, requeue=False)

    queue.append(repo, request, "processing")
    if item.status != "testing":
        item = _set_status(item, "testing")
    events.emit(
        "integration_start",
        repo=repo,
        item=request.item,
        session=request.session,
        detail={"sha": request.sha},
    )

    # 2. 集成：fetch + rebase 到 main
    integ = ensure_integration(config, repo, policy.default_branch)
    _must(_git(integ, "fetch", "-q", str(clone_path), request.branch), "从克隆 fetch")
    fetched = _must(_git(integ, "rev-parse", "FETCH_HEAD"), "读 FETCH_HEAD")
    if fetched != request.sha:
        return _requeue(request, fetched, item)
    _git(integ, "rebase", "--abort")
    _must(_git(integ, "checkout", "-q", "--detach", request.sha), "检出分支头")
    base = _must(_git(integ, "rev-parse", policy.default_branch), "读 main")
    rebased = _git(integ, "rebase", "-q", policy.default_branch)
    if rebased.returncode != 0:
        conflict = (rebased.stdout + rebased.stderr).strip().splitlines()[-8:]
        _git(integ, "rebase", "--abort")
        events.emit(
            "rebase_conflict",
            repo=repo,
            item=request.item,
            session=request.session,
            detail={"sha": request.sha, "base": base, "output": conflict},
        )
        decision = judge.decide(
            config,
            trigger="rebase_conflict",
            item=item,
            context="\n".join(conflict),
            default="return_to_session",
        )
        if decision.action == "needs_human":
            return _needs_human(
                config, request, item, f"冲突需要人处理：{decision.reason}", {"base": base}
            )
        return _return(
            config,
            request,
            item,
            "rebase 到 main 时冲突",
            (
                decision.hint
                or "rebase 到 main 冲突，请在克隆里 `git fetch origin main && "
                "git rebase origin/main` 解决后重新 ao land。"
            )
            + "\n"
            + "\n".join(conflict),
            {"base": base, "judge": decision.action},
        )
    new_sha = _must(_git(integ, "rev-parse", "HEAD"), "读 rebase 结果")

    # 3. 准备 + 测试（在槽位里）
    timeout = config.steward_int("test_timeout_minutes") * 60
    candidate_env = {**_candidate_env(), "TMPDIR": str(run_tmp)}
    if policy.prepare_cmd:
        rc = slots.run_forwarding_signals(
            _candidate_command(config, repo, shlex.split(policy.prepare_cmd), integ, run_tmp),
            cwd=integ,
            env=candidate_env,
            log_path=log,
            timeout_seconds=timeout,
        )[0]
        if rc != 0:
            events.emit(
                "prepare_fail", repo=repo, item=request.item, detail={"rc": rc, "log": str(log)}
            )
            return _return(
                config,
                request,
                item,
                f"准备命令失败（{policy.prepare_cmd}，rc {rc}）",
                _tail(log),
                {"rc": rc},
            )
    events.emit(
        "tests_start",
        repo=repo,
        item=request.item,
        session=request.session,
        detail={"cmd": policy.test_cmd, "sha": new_sha},
    )
    rc = slots.run_with_slot(
        "test",
        _candidate_command(config, repo, shlex.split(policy.test_cmd), integ, run_tmp),
        count=config.slot_count("test"),
        repo=repo,
        item=request.item,
        session=request.session,
        cwd=integ,
        env=candidate_env,
        log_path=log,
        command_timeout_seconds=timeout,
    )
    if rc != 0:
        events.emit(
            "tests_fail",
            repo=repo,
            item=request.item,
            session=request.session,
            detail={"rc": rc, "log": str(log), "tail": _tail(log, 15)},
        )
        decision = judge.decide(
            config, trigger="tests_fail", item=item, context=_tail(log), default="return_to_session"
        )
        if decision.action == "retry_flaky":
            events.emit(
                "tests_retry", repo=repo, item=request.item, detail={"reason": decision.reason}
            )
            rc = slots.run_with_slot(
                "test",
                _candidate_command(config, repo, shlex.split(policy.test_cmd), integ, run_tmp),
                count=config.slot_count("test"),
                repo=repo,
                item=request.item,
                session=request.session,
                cwd=integ,
                env=candidate_env,
                log_path=log,
                command_timeout_seconds=timeout,
            )
            if rc != 0:
                events.emit(
                    "tests_fail",
                    repo=repo,
                    item=request.item,
                    session=request.session,
                    detail={"rc": rc, "retry": True},
                )
        if rc != 0 and decision.action == "needs_human":
            return _needs_human(
                config, request, item, f"测试失败需要人看：{decision.reason}", {"rc": rc}
            )
        if rc != 0:
            return _return(
                config,
                request,
                item,
                f"测试失败（rc {rc}）",
                (decision.hint or f"`{policy.test_cmd}` 失败，退出码 {rc}。")
                + "\n日志尾部：\n"
                + _tail(log),
                {"rc": rc, "judge": decision.action},
            )
    events.emit(
        "tests_pass",
        repo=repo,
        item=request.item,
        session=request.session,
        detail={"log": str(log)},
    )

    # 4. 策略检查
    triggers = policy_mod.check(integ, base, new_sha, policy)
    if policy_mod.needs_approval(policy, triggers):
        summary = policy_mod.summarize(triggers)
        approval = approvals.find(repo, request.item, request.sha)
        if approval is None:
            events.emit(
                "rule_triggered",
                repo=repo,
                item=request.item,
                session=request.session,
                detail={**summary, "autonomy": policy.autonomy, "sha": request.sha},
            )
            reason = (
                "；".join(f"{t.detail}（{t.path}）" for t in triggers)
                or f"autonomy={policy.autonomy}，每次合入都要人批"
            )
            if triggers:
                decision = judge.decide(
                    config,
                    trigger="rule_triggered",
                    item=item,
                    context=reason,
                    default="needs_human",
                )
                if decision.action == "return_to_session":
                    return _return(
                        config,
                        request,
                        item,
                        f"规则触发，退回修正：{reason}",
                        decision.hint or reason,
                        {**summary, "judge": decision.action},
                    )
            return _needs_human(
                config,
                request,
                item,
                reason,
                {**summary, "sha": request.sha},
                is_approval_gate=True,
            )
        events.emit(
            "approval_used",
            repo=repo,
            item=request.item,
            detail={"sha": request.sha, "by": approval.by, **summary},
        )

    # 5. 快进合入。测试期间主仓库才变脏的，重新入队等下一轮。
    dirty = _main_dirty_reason(main_repo, policy.default_branch)
    if dirty:
        return _defer_land(config, request, item, dirty, requeue=True)
    merged = _git(main_repo, "merge", "--ff-only", new_sha)
    if merged.returncode != 0:
        reason = f"快进失败: {merged.stderr.strip()}"
        events.emit("land_failed", repo=repo, item=request.item, detail={"reason": reason})
        queue.append(repo, request, "error", {"reason": reason})
        _set_status(item, "returned", hint=reason)
        return Outcome(request, "error", reason, {})
    _git(integ, "checkout", "-q", "--detach", policy.default_branch)
    events.emit(
        "landed",
        repo=repo,
        item=request.item,
        session=request.session,
        detail={"sha": request.sha, "landed_sha": new_sha, "base": base},
    )
    item = _set_status(item, "landed", branch=request.branch, landed_sha=new_sha)
    queue.append(repo, request, "landed", {"landed_sha": new_sha})
    clones.remove_clone(config, item, reason="landed")
    if _dispatchable_after(repo, request.item):
        _auto_dispatch(config, repo, reason=f"landed {request.item}")
    return Outcome(request, "landed", "已快进合入 main", {"landed_sha": new_sha})


def _requeue_approved(config: Config, repo: str) -> None:
    """审批门造成的 needs_human 一旦有了对应 (item, sha) 的审批记录，就重新入队。"""
    pending_items = {r.item for r in queue.pending(repo)}
    for item in wi.list_items(repo, status="needs_human"):
        sha = item.extra.get("approval_sha")
        clone_path = item.extra.get("clone_path")
        branch = item.branch or item.extra.get("branch")
        if not (sha and clone_path and branch) or item.id in pending_items:
            continue
        if item.extra.get("needs_human_kind") != "approval_gate":
            continue  # 测试失败/冲突/max_returns 造成的 needs_human，批准不触发重排
        if approvals.is_approved(repo, item.id, str(sha)):
            request = queue.new_request(
                repo=repo,
                item=item.id,
                branch=str(branch),
                sha=str(sha),
                clone_path=str(clone_path),
                session=item.session_id,
            )
            queue.append(repo, request, "queued", {"reason": "approved"})
            wi.apply(item, status="queued")
            events.emit("requeued_after_approval", repo=repo, item=item.id, detail={"sha": sha})


def _mark_queued(repo: str, requests: tuple[queue.LandRequest, ...]) -> None:
    for request in requests:
        item = wi.load(repo, request.item)
        if item.status != "queued":
            wi.apply(item, status="queued", branch=request.branch, clone_path=request.clone_path)


def process_repo(config: Config, repo: str) -> Outcome | None:
    """处理一个仓库队列里的第一项。没有待处理就返回 None。"""
    with steward_lock(repo):
        queue.recover_processing(repo)
        _mark_queued(repo, queue.ingest_inbox(repo))
        try:
            trials.ingest(config, repo)  # 会话投递的试验登记行入账，与 land 请求一样每轮收
        except OSError as exc:
            events.emit("trials_ingest_error", repo=repo, detail={"error": str(exc)})
        _requeue_approved(config, repo)
        pending = queue.pending(repo)
        if not pending:
            return None
        request = pending[0]
        policy = policy_mod.load_policy(config.repo_path(repo), repo)
        try:
            return process_request(config, request, policy)
        except AgentopsError as exc:
            queue.append(repo, request, "error", {"reason": str(exc)})
            events.emit("steward_error", repo=repo, item=request.item, detail={"reason": str(exc)})
            try:
                wi.apply(
                    wi.load(repo, request.item), status="returned", hint=f"steward 出错：{exc}"
                )
            except AgentopsError:
                pass
            return Outcome(request, "error", str(exc), {})


def active_repos(config: Config) -> tuple[str, ...]:
    return tuple(name for name in config.repos if (config.repo_path(name) / ".git").exists())


def run_once(config: Config, repo: str | None = None) -> tuple[Outcome, ...]:
    budget.release_stale()  # 会话已结束的陈旧预留，每轮顺手释放
    outcomes = []
    for name in (repo,) if repo else active_repos(config):
        outcome = process_repo(config, name)
        if outcome is not None:
            outcomes.append(outcome)
        elif wi.list_items(name, status="ready"):
            _auto_dispatch(config, name, reason="loop")
    return tuple(outcomes)


def run_loop(config: Config, *, poll_seconds: float | None = None, repo: str | None = None) -> None:
    interval = poll_seconds if poll_seconds is not None else config.steward_int("poll_seconds")
    events.emit("steward_start", detail={"poll_seconds": interval, "pid": os.getpid()})
    while True:
        try:
            for outcome in run_once(config, repo):
                print(f"{outcome.request.id}: {outcome.result} — {outcome.reason}", file=sys.stderr)
        except AgentopsError as exc:
            print(f"steward: {exc}", file=sys.stderr)
        time.sleep(interval)
