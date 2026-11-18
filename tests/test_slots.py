import os
import subprocess
import sys

from agentops import cli, events, slots
from agentops.timeutil import parse_iso


def test_three_processes_two_slots_one_waits(agentops_home):
    cmd = [
        sys.executable,
        "-m",
        "agentops.cli",
        "slot",
        "test",
        "--count",
        "2",
        "--",
        "sleep",
        "1.5",
    ]
    env = {**os.environ, "AGENTOPS_HOME": str(agentops_home)}

    procs = [subprocess.Popen(cmd, env=env) for _ in range(3)]
    codes = [proc.wait(timeout=60) for proc in procs]

    assert codes == [0, 0, 0]
    all_events = events.read_events()
    assert sum(e.type == "slot_wait" for e in all_events) >= 1
    acquired = [e for e in all_events if e.type == "slot_acquired"]
    released = [e for e in all_events if e.type == "slot_released"]
    assert len(acquired) == 3 and len(released) == 3
    assert _max_concurrency(acquired, released) <= 2


def _max_concurrency(acquired, released):
    starts = {e.detail["pid"]: parse_iso(e.ts) for e in acquired}
    ends = {e.detail["pid"]: parse_iso(e.ts) for e in released}
    points = [(starts[p], 1) for p in starts] + [(ends[p], -1) for p in ends]
    running, peak = 0, 0
    for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
        running += delta
        peak = max(peak, running)
    return peak


def test_slot_returns_child_exit_code(agentops_home):
    code = cli.main(["slot", "test", "--count", "1", "--", "sh", "-c", "exit 3"])

    assert code == 3
    assert events.read_events()[-1].detail["rc"] == 3


def test_acquire_context_manager_holds_and_releases(agentops_home):
    with slots.acquire("probe", 1) as lease:
        assert lease.index == 1
        assert slots.try_lock(slots.lock_path("probe", 1)) is None

    handle = slots.try_lock(slots.lock_path("probe", 1))
    assert handle is not None
    handle.close()
