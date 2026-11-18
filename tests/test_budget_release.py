"""预算预留的回滚与陈旧释放：start/resume 失败不留台账残留，会话已结束的陈旧预留被清掉。"""

import json
from datetime import timedelta

import pytest
from test_phase4 import _ready, _types
from test_phase4 import world as world  # noqa: F401

from agentops import budget, events, sessions, start
from agentops import workitems as wi
from agentops.errors import AgentopsError
from agentops.timeutil import now


def _age_reservation(call_id: str, hours: float) -> None:
    path = budget.ledger_path()
    data = json.loads(path.read_text())
    data["reservations"][call_id]["ts"] = (now() - timedelta(hours=hours)).isoformat()
    path.write_text(json.dumps(data))


def _reserve(call_id: str, amount: float = 1.0) -> None:
    assert budget.reserve(call_id, amount, limit=10.0, label=f"test {call_id}")


def _released_events():
    return [e for e in events.read_events() if e.type == "budget_released"]


def test_resume_failing_in_wi_apply_releases_reservation(world, monkeypatch):
    item = _ready("resume leak")
    launch = start.prepare_launch(world.config, item, mode="headless")
    start.run_headless(world.config, launch)
    # needs_human → in_progress 状态机不允许，resume 在预留之后抛错
    stuck = wi.apply(wi.load("demo", item.id), status="needs_human", force=True)

    with pytest.raises(AgentopsError):
        start.resume_headless(world.config, stuck, hint="x", round_no=1)

    assert budget.outstanding() == 0
    assert any(e.detail["call"].endswith(":resume1") for e in _released_events())


def test_prepare_launch_failing_after_require_releases_reservation(world, monkeypatch):
    item = _ready("start leak")

    def boom(*args, **kwargs):
        raise RuntimeError("register boom")

    monkeypatch.setattr(sessions, "register", boom)

    with pytest.raises(RuntimeError, match="register boom"):
        start.prepare_launch(world.config, item, mode="headless")

    assert budget.outstanding() == 0
    assert len(_released_events()) == 1


def test_prepare_launch_failing_after_clone_releases_reservation(world, monkeypatch):
    item = _ready("late leak")

    def boom(*args, **kwargs):
        raise RuntimeError("render boom")

    monkeypatch.setattr(start, "build_prompt", boom)

    with pytest.raises(RuntimeError, match="render boom"):
        start.prepare_launch(world.config, item, mode="headless")

    assert budget.outstanding() == 0
    assert "budget_released" in _types()


def test_release_restores_available_immediately_and_marks_settled(agentops_home):
    _reserve("c1", 4.0)
    assert budget.available(10.0) == 6.0

    assert budget.release("c1", "测试") is True

    assert budget.available(10.0) == 10.0
    record = json.loads(budget.ledger_path().read_text())["settled"][-1]
    assert record["released"] is True and record["reason"] == "测试"
    assert budget.release("c1", "again") is False


def test_stale_reservations_age_boundary(agentops_home):
    _reserve("old")
    _reserve("fresh")
    _age_reservation("old", 12.5)
    _age_reservation("fresh", 11.5)

    assert set(budget.stale_reservations(12)) == {"old"}
    assert set(budget.stale_reservations(11)) == {"old", "fresh"}


def test_release_stale_skips_running_session_and_frees_ended(agentops_home):
    sessions.register(
        "s-running", repo="demo", item=1, branch="b", worktree=None, mode="headless", name="n"
    )
    sessions.register(
        "s-ended", repo="demo", item=2, branch="b", worktree=None, mode="headless", name="n"
    )
    sessions.end("s-ended", rc=0)
    for call in ("s-running", "s-ended:resume1", "s-gone"):
        _reserve(call)
        _age_reservation(call, 13)

    released = budget.release_stale()

    assert set(released) == {"s-ended:resume1", "s-gone"}
    assert set(budget.reservations()) == {"s-running"}
    assert len(_released_events()) == 2


def test_stale_hours_defaults_to_12_and_reads_config(agentops_home):
    assert budget.stale_hours() == 12.0
    from agentops.paths import config_file

    config_file().write_text(config_file().read_text() + "\n[budget]\nstale_hours = 3\n")
    assert budget.stale_hours() == 3.0
