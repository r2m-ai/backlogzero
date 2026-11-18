"""重任务槽位（C5）：fcntl.flock 做的 N 槽信号量，机器级共享。"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from agentops import events
from agentops.errors import AgentopsError
from agentops.paths import slots_dir

POLL_SECONDS = 0.5
NOT_FOUND_RC = 127
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class SlotTimeout(AgentopsError):
    pass


@dataclass(frozen=True)
class SlotLease:
    name: str
    index: int
    waited_seconds: float
    fd: int


def lock_path(name: str, index: int) -> Path:
    if not _NAME_RE.match(name):
        raise AgentopsError(f"槽位名只能含字母、数字、下划线和连字符，得到 {name!r}")
    return slots_dir() / f"{name}.{index}.lock"


def try_lock(path: Path) -> IO[str] | None:
    """非阻塞拿锁。拿到返回文件句柄（关闭即释放），拿不到返回 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


@contextmanager
def acquire(
    name: str,
    count: int,
    *,
    poll_seconds: float = POLL_SECONDS,
    timeout_seconds: float | None = None,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[SlotLease]:
    """占用 name 的 N 个槽之一；都忙则轮询等待，第一次等待时回调 on_wait。"""
    if count < 1:
        raise AgentopsError(f"槽位数必须 ≥ 1，得到 {count}")
    started = time.monotonic()
    notified = False
    while True:
        for index in range(1, count + 1):
            handle = try_lock(lock_path(name, index))
            if handle is None:
                continue
            try:
                yield SlotLease(
                    name=name,
                    index=index,
                    waited_seconds=time.monotonic() - started,
                    fd=handle.fileno(),
                )
            finally:
                # 只关闭不显式解锁：若有后代仍持有继承来的 fd，槽位继续被占用，直到它退出
                handle.close()
            return
        if not notified:
            notified = True
            if on_wait is not None:
                on_wait()
        if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
            raise SlotTimeout(f"等待槽位 {name} 超过 {timeout_seconds} 秒")
        time.sleep(poll_seconds)


def shell_exit_code(code: int) -> int:
    """Popen 对被信号杀死的子进程返回负数，换算成 shell 的 128+信号号。"""
    return code if code >= 0 else 128 - code


GROUP_GRACE_SECONDS = 30.0
GROUP_POLL_SECONDS = 0.2


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group(pgid: int, *, grace_seconds: float) -> bool:
    """等整个进程组退出；超过宽限期就先 SIGTERM 再 SIGKILL。返回是否强杀过掉队进程。"""
    deadline = time.monotonic() + grace_seconds
    while _group_alive(pgid):
        if time.monotonic() > deadline:
            break
        time.sleep(GROUP_POLL_SECONDS)
    if not _group_alive(pgid):
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 10.0
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(GROUP_POLL_SECONDS)
    if _group_alive(pgid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        while _group_alive(pgid):
            time.sleep(GROUP_POLL_SECONDS)
    return True


TIMEOUT_RC = 124


def run_forwarding_signals(
    command: Sequence[str],
    *,
    pass_fds: Sequence[int] = (),
    grace_seconds: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> tuple[int, bool]:
    """在独立进程组里运行命令，信号转发给整个组，等整组退出后才返回。

    返回 (退出码, 是否强杀过掉队进程)。包装进程（just、sh）退出后孙进程还在跑时，槽位不会提前释放。
    log_path 给出时 stdout/stderr 追加写到该文件。
    超过 timeout_seconds 整组先 SIGTERM 再 SIGKILL，退出码 124。
    """
    log_handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            list(command),
            pass_fds=tuple(pass_fds),
            process_group=0,
            cwd=None if cwd is None else str(cwd),
            env=None if env is None else dict(env),
            stdout=log_handle,
            stderr=subprocess.STDOUT if log_handle is not None else None,
        )
    finally:
        if log_handle is not None:
            log_handle.close()
    pgid = process.pid

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signum)

    previous = {sig: signal.signal(sig, forward) for sig in _FORWARDED_SIGNALS}
    try:
        try:
            code = shell_exit_code(process.wait(timeout=timeout_seconds))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
                process.wait()
            code = TIMEOUT_RC
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    grace = GROUP_GRACE_SECONDS if grace_seconds is None else grace_seconds
    killed_stragglers = _wait_for_group(pgid, grace_seconds=grace)
    return code, killed_stragglers


def _emit_best_effort(type: str, **kwargs: object) -> None:
    """事件写不进去不该阻塞任务本身；报到 stderr 即可。"""
    try:
        events.emit(type, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        print(f"警告: 事件 {type} 写入失败: {exc}", file=sys.stderr)


def run_with_slot(
    name: str,
    command: Sequence[str],
    *,
    count: int,
    repo: str | None = None,
    item: int | None = None,
    session: str | None = None,
    timeout_seconds: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log_path: Path | None = None,
    command_timeout_seconds: float | None = None,
) -> int:
    """在槽位里运行命令，记 slot_wait / slot_acquired / slot_released 事件，返回命令退出码。

    timeout_seconds 是等槽位的上限；command_timeout_seconds 是命令本身的上限（超时退出码 124）。

    命令在独立进程组里运行，信号转发给整组，整组退出后才释放槽位。
    锁的文件描述符传给子进程：ao 自己被 SIGKILL 时，仍持有该 fd 的后代会继续占住槽位。
    """
    if not command:
        raise AgentopsError("ao slot 需要在 -- 之后给出要运行的命令")
    lock_path(name, 1)
    tag: Mapping[str, object] = {"slot": name, "count": count, "pid": os.getpid()}
    common = {"repo": repo, "item": item, "session": session}

    def on_wait() -> None:
        _emit_best_effort("slot_wait", detail=tag, **common)

    with acquire(name, count, timeout_seconds=timeout_seconds, on_wait=on_wait) as lease:
        _emit_best_effort(
            "slot_acquired",
            detail={**tag, "index": lease.index, "waited_s": round(lease.waited_seconds, 2)},
            **common,
        )
        started = time.monotonic()
        code = NOT_FOUND_RC
        killed_stragglers = False
        failure: AgentopsError | None = None
        try:
            code, killed_stragglers = run_forwarding_signals(
                command,
                pass_fds=(lease.fd,),
                cwd=cwd,
                env=env,
                log_path=log_path,
                timeout_seconds=command_timeout_seconds,
            )
        except OSError as exc:
            failure = AgentopsError(f"无法运行命令 {command[0]!r}: {exc}")
        except KeyboardInterrupt:
            code = shell_exit_code(-signal.SIGINT)
        finally:
            _emit_best_effort(
                "slot_released",
                detail={
                    **tag,
                    "index": lease.index,
                    "rc": code,
                    "duration_s": round(time.monotonic() - started, 2),
                    "killed_stragglers": killed_stragglers,
                },
                **common,
            )
        if failure is not None:
            raise failure
    return code
