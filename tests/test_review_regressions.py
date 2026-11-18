"""审查发现的问题的回归测试。"""

import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import uuid

import pytest

from agentops import cli, config, events, sessions, slots
from agentops import workitems as wi
from agentops.errors import AgentopsError
from agentops.paths import home, sessions_file
from agentops.workitems import ValidationError


def _ao(*args: str) -> list[str]:
    return [sys.executable, "-m", "agentops.cli", *args]


def test_concurrent_wi_new_never_loses_items(agentops_home):
    procs = [
        subprocess.Popen(
            _ao("wi", "new", "--repo", "r", "--title", f"t{n}"), stdout=subprocess.DEVNULL
        )
        for n in range(8)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0] * 8

    items = wi.list_items(repo="r")
    assert [i.id for i in items] == list(range(1, 9))
    assert sorted(i.title for i in items) == sorted(f"t{n}" for n in range(8))


def test_save_leaves_no_temp_files(agentops_home):
    wi.create(repo="r", title="t")

    names = sorted(p.name for p in wi.item_path("r", 1).parent.iterdir())
    assert names == [".lock", "001.md"]


def test_apply_reloads_from_disk_before_changing(agentops_home):
    item = wi.create(repo="r", title="t", acceptance=("a",))
    wi.apply(item, status="ready")

    with pytest.raises(wi.InvalidTransition):
        wi.apply(item, status="queued")

    assert wi.apply(item, status="in_progress").status == "in_progress"


def test_hand_edited_scalars_are_rejected(agentops_home):
    bad = textwrap.dedent(
        """\
        ---
        id: 1
        repo: r
        title: yes
        acceptance: tests pass
        ---
        """
    )
    with pytest.raises(ValidationError):
        wi.from_text(bad)
    with pytest.raises(ValidationError):
        wi.from_text("---\nid: 1\nrepo: r\ntitle:\n---\n")
    with pytest.raises(ValidationError):
        wi.from_text("---\nid: 1\nrepo: r\ntitle: ok\ndepends_on: '12'\n---\n")


def test_load_rejects_front_matter_that_disagrees_with_file_name(agentops_home):
    wi.create(repo="r", title="t")
    path = wi.item_path("r", 1)
    path.write_text(path.read_text().replace("id: 1", "id: 7"))

    with pytest.raises(ValidationError):
        wi.load("r", 1)


def test_kv_cannot_touch_known_fields(agentops_home, capsys):
    cli.main(["wi", "new", "--repo", "r", "--title", "t"])

    assert cli.main(["wi", "set", "r#1", "--kv", "title=123"]) == 2
    assert "--kv" in capsys.readouterr().err


def test_list_with_status_filter_uses_full_dependency_pool(agentops_home, capsys):
    dep = wi.create(repo="r", title="dep", acceptance=("a",))
    wi.apply(dep, status="landed", force=True)
    child = wi.create(repo="r", title="child", acceptance=("a",), depends_on=(1,))
    wi.apply(child, status="ready")
    capsys.readouterr()

    assert cli.main(["wi", "list", "--status", "ready", "--json"]) == 0
    import json

    rows = json.loads(capsys.readouterr().out)
    assert [r["ref"] for r in rows] == ["r#2"]
    assert rows[0]["blocked_by"] == []


def test_double_dash_rejected_outside_slot(agentops_home, capsys):
    cli.main(["wi", "new", "--repo", "r", "--title", "t"])

    assert cli.main(["wi", "set", "r#1", "--priority", "P1", "--", "junk"]) == 2
    assert "ao slot" in capsys.readouterr().err
    assert wi.load("r", 1).priority == "P2"


def test_events_tail_rejects_zero(agentops_home):
    with pytest.raises(SystemExit):
        cli.main(["events", "tail", "-n", "0"])


def test_naive_timestamp_line_is_skipped(agentops_home, capsys):
    events.emit("good")
    with open(events.events_file(), "a") as handle:
        handle.write('{"ts": "2026-01-01T00:00:00", "type": "naive"}\n123\n')

    assert [e.type for e in events.read_events()] == ["good"]
    assert "events.jsonl" in capsys.readouterr().err


def test_corrupt_sessions_line_is_skipped(agentops_home, capsys):
    sessions.register("s1", repo="r", item=1)
    with open(sessions_file(), "a") as handle:
        handle.write('{"unexpected": 1}\n')

    assert [s.session_id for s in sessions.active()] == ["s1"]
    assert "sessions.jsonl" in capsys.readouterr().err


def test_empty_agentops_home_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AGENTOPS_HOME", "")

    assert home() == home().expanduser()
    assert home().name == ".agentops"


def test_config_rejects_wrong_types(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text('[general]\nmax_sessions = "two"\n')
    with pytest.raises(AgentopsError):
        config.load_config(bad)

    bad.write_text("[slots]\ntest = 2.7\n")
    with pytest.raises(AgentopsError):
        config.load_config(bad)

    bad.write_text("[repos]\n")
    assert config.load_config(bad).repos == {}


def test_slot_command_not_found_still_releases(agentops_home):
    assert cli.main(["slot", "test", "--count", "1", "--", "/no/such/binary"]) == 2

    released = [e for e in events.read_events() if e.type == "slot_released"]
    assert released and released[-1].detail["rc"] == slots.NOT_FOUND_RC
    handle = slots.try_lock(slots.lock_path("test", 1))
    assert handle is not None
    handle.close()


CHILD_SLEEP_SECONDS = 30
CHILD_START_TIMEOUT_SECONDS = 10


def test_sigterm_is_forwarded_and_slot_released_after_child_exits(agentops_home):
    marker = f"agentops-sigterm-test-{uuid.uuid4().hex}"
    proc = subprocess.Popen(_ao("slot", "test", "--count", "1", "--", *_marked_sleep(marker)))
    children = _wait_for_marked_children(marker, parent_pid=proc.pid)
    assert children, "槽位里的子进程没有在限时内启动"

    proc.send_signal(signal.SIGTERM)
    code = proc.wait(timeout=10)

    assert code == 128 + signal.SIGTERM
    released = [e for e in events.read_events() if e.type == "slot_released"]
    assert released and released[-1].detail["rc"] == 128 + signal.SIGTERM
    handle = slots.try_lock(slots.lock_path("test", 1))
    assert handle is not None
    handle.close()
    assert not _process_alive(children)


def _marked_sleep(marker: str) -> list[str]:
    """带唯一标记的长睡子进程：pgrep 只会找到它，不会撞上机器上别人的 sleep。"""
    return [sys.executable, "-c", f"import time; time.sleep({CHILD_SLEEP_SECONDS})", marker]


def _marked_pids(marker: str, *, exclude: int) -> list[int]:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split() if int(p) != exclude]


def _wait_for_marked_children(marker: str, *, parent_pid: int) -> list[int]:
    deadline = time.monotonic() + CHILD_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pids = _marked_pids(marker, exclude=parent_pid)
        if pids:
            return pids
        time.sleep(0.1)
    return []


def _process_alive(pids: list[int]) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            continue
    return False


def test_slot_name_is_validated(agentops_home):
    with pytest.raises(AgentopsError):
        slots.lock_path("../evil", 1)


def test_repo_name_is_validated(agentops_home):
    with pytest.raises(ValidationError):
        wi.create(repo="../x", title="t")


def test_auth_error_never_contains_child_output(monkeypatch, tmp_path):
    from agentops import auth

    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "leak $CLAUDE_CODE_OAUTH_TOKEN" >&2\necho notjson\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    with pytest.raises(AgentopsError) as info:
        auth.auth_status({"PATH": os.environ["PATH"], "CLAUDE_CODE_OAUTH_TOKEN": "SECRET-X"})

    assert "SECRET-X" not in str(info.value)
    assert "leak" not in str(info.value)


def _slot_free() -> bool:
    handle = slots.try_lock(slots.lock_path("test", 1))
    if handle is None:
        return False
    handle.close()
    return True


def _pgrep(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def _unique_sleep(seconds: int) -> tuple[str, str]:
    """返回（sleep 命令，pgrep 正则）。秒数后面带上本进程号和纳秒尾数，只会匹配本用例自己起的进程。

    机器上别的进程（其他会话的轮询、并行的另一轮测试）也可能在跑同样秒数的 sleep，
    按固定命令行去 pgrep 会把它们误认成本用例留下的进程。
    """
    duration = f"{seconds}.{os.getpid()}{time.monotonic_ns() % 1000:03d}"
    return f"sleep {duration}", f"^sleep {re.escape(duration)}$"


def test_sigterm_reaches_grandchildren_before_slot_is_released(agentops_home):
    """审稿意见 S1：包装进程退出后孙进程还在跑，槽位不能提前释放。"""
    sleep_cmd, sleep_pattern = _unique_sleep(31)
    proc = subprocess.Popen(
        _ao("slot", "test", "--count", "1", "--", "sh", "-c", f"{sleep_cmd} & wait")
    )
    time.sleep(1.0)
    assert _pgrep(sleep_pattern)

    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 128 + signal.SIGTERM

    assert not _process_alive(_pgrep(sleep_pattern))
    assert _slot_free()


def test_slot_waits_for_stragglers_after_direct_child_exits(agentops_home):
    """直接子进程立刻退出、后台孙进程还在跑：要等孙进程结束才释放。"""
    started = time.monotonic()
    code = cli.main(["slot", "test", "--count", "1", "--", "sh", "-c", "sleep 2 & exit 0"])

    assert code == 0
    assert time.monotonic() - started >= 1.8
    released = [e for e in events.read_events() if e.type == "slot_released"][-1]
    assert released.detail["duration_s"] >= 1.8 and released.detail["killed_stragglers"] is False


def _wait_pid_gone(pid: int, *, timeout: float = 10.0) -> bool:
    """只看这个 pid：被杀的孤儿由 init/launchd 回收，回收前可能短暂是僵尸，所以轮询。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _run_straggler_slot(tmp_path) -> int:
    """起一个立刻退出、留下后台 sleep 的命令；返回该 sleep 的 pid（由 shell 写入文件）。"""
    pid_file = tmp_path / "straggler.pid"
    script = f"sleep 60 & echo $! > {pid_file}; exit 0"
    code = cli.main(["slot", "test", "--count", "1", "--", "sh", "-c", script])
    assert code == 0
    return int(pid_file.read_text().strip())


def test_stragglers_past_grace_are_killed(agentops_home, monkeypatch, tmp_path):
    monkeypatch.setattr(slots, "GROUP_GRACE_SECONDS", 0.5)
    started = time.monotonic()

    pid = _run_straggler_slot(tmp_path)

    assert time.monotonic() - started < 15
    released = [e for e in events.read_events() if e.type == "slot_released"][-1]
    assert released.detail["killed_stragglers"] is True
    assert _wait_pid_gone(pid)


def test_straggler_check_ignores_unrelated_sleep(agentops_home, monkeypatch, tmp_path):
    """机器上另有无关的 sleep 60 在跑，不影响判断；该进程也不会被槽位清理误杀。"""
    monkeypatch.setattr(slots, "GROUP_GRACE_SECONDS", 0.5)
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        pid = _run_straggler_slot(tmp_path)

        assert pid != bystander.pid
        assert _wait_pid_gone(pid)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_timed_out_child_is_reaped(agentops_home, tmp_path):
    """超时被杀的直接子进程已被 wait 回收：waitpid 拿不到这个 pid。"""
    pid_file = tmp_path / "child.pid"
    script = f"echo $$ > {pid_file}; exec sleep 60"

    code, _ = slots.run_forwarding_signals(
        ["sh", "-c", script], timeout_seconds=1, grace_seconds=0.5
    )

    assert code == slots.TIMEOUT_RC
    with pytest.raises(ChildProcessError):
        os.waitpid(int(pid_file.read_text().strip()), os.WNOHANG)


def test_sigkill_on_ao_keeps_slot_held_while_shell_child_runs(agentops_home):
    """审稿意见 S2：ao 被 SIGKILL，持有继承 fd 的子进程还在跑时槽位仍被占用，结束后自动释放。"""
    proc = subprocess.Popen(_ao("slot", "test", "--count", "1", "--", "sh", "-c", "sleep 3"))
    time.sleep(1.0)

    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)

    assert not _slot_free()  # sh 还持有继承来的锁 fd
    deadline = time.monotonic() + 10
    while not _slot_free() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _slot_free()
