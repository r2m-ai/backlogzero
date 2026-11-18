"""多轮 resume：每轮 --fork-session 出新会话 id，会话材料仍按首轮目录定位。用假的 claude。"""

import json
from pathlib import Path

import pytest
from test_phase4 import _fake_calls, _ready
from test_phase4 import world as world  # noqa: F401

from agentops import events, sessions, start
from agentops import workitems as wi


def _flag_value(call: dict, flag: str) -> str:
    args = call["args"]
    return args[args.index(flag) + 1]


def _returned(item_id: int):
    """steward 退回：工作项进入 returned，再由 ao resume 接手。"""
    return wi.apply(wi.load("demo", item_id), status="returned", force=True)


@pytest.fixture
def two_rounds(world, monkeypatch):
    """无人值守工作项：start → resume 第 1 轮 → resume 第 2 轮，每轮成本不同。"""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "idle")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.5")
    item = _ready("multi round")
    launch = start.prepare_launch(world.config, item, mode="headless")
    start.run_headless(world.config, launch)
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.3")
    first = start.resume_headless(world.config, _returned(item.id), hint="一", round_no=1)
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.2")
    second = start.resume_headless(world.config, _returned(item.id), hint="二", round_no=2)
    return world, item, launch, first, second


def test_second_resume_round_uses_previous_fork_id(two_rounds):
    env, _, launch, first, second = two_rounds

    calls = _fake_calls(env)

    assert len(calls) == 3
    assert launch.session_id in calls[1]["args"]
    assert calls[2]["args"][calls[2]["args"].index("--resume") + 1] == first.session_id
    assert "--fork-session" in calls[2]["args"]
    assert len({launch.session_id, first.session_id, second.session_id}) == 3


def test_each_round_cost_is_independent(two_rounds):
    _, item, _, first, second = two_rounds

    assert (first.cost_delta_usd, second.cost_delta_usd) == (0.3, 0.2)
    assert (first.cost_usd, second.cost_usd) == (0.8, 1.0)
    assert wi.load("demo", item.id).extra["cost_usd_total"] == 1.0


def test_session_materials_stay_in_origin_directory(two_rounds):
    env, item, launch, first, second = two_rounds
    root = start.sessions_root()

    origin = root / launch.session_id
    assert (origin / "resume-1.md").exists() and (origin / "resume-2.md").exists()
    for fork in (first.session_id, second.session_id):
        assert not (root / fork).exists()
    # 第 2 轮 --settings 指向的文件在运行时必须存在
    settings = Path(_flag_value(_fake_calls(env)[2], "--settings"))
    assert settings == origin / "settings.json" and settings.exists()
    loaded = wi.load("demo", item.id)
    assert loaded.session_id == second.session_id
    assert loaded.extra["origin_session_id"] == launch.session_id


def test_session_chain_and_registry_order_are_kept(two_rounds):
    _, _, launch, first, second = two_rounds

    ends = [e for e in events.read_events() if e.type == "session_end"]
    assert [e.detail["forked_from"] for e in ends] == [None, launch.session_id, first.session_id]
    assert [e.session for e in ends] == [launch.session_id, first.session_id, second.session_id]
    latest = sessions.latest_by_id()
    assert all(latest[s].status == "ended" for s in (launch.session_id, first.session_id))
    assert latest[second.session_id].status == "ended"
    records = [
        json.loads(line)
        for line in (start.home() / "sessions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    order = [(r["session_id"], r.get("status")) for r in records]
    assert order.index((first.session_id, "running")) < order.index((second.session_id, "running"))
    assert order.index((first.session_id, "running")) < order.index((first.session_id, "ended"))


def test_legacy_item_without_origin_uses_session_id(world, monkeypatch):
    """旧工作项没有 origin_session_id：目录按 session_id 定位。"""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "idle")
    item = _ready("legacy")
    launch = start.prepare_launch(world.config, item, mode="headless")
    start.run_headless(world.config, launch)
    legacy = wi.apply(wi.load("demo", item.id), origin_session_id=None)
    assert "origin_session_id" not in legacy.extra

    restored = start.launch_from_item(world.config, legacy)

    assert restored.directory == start.sessions_root() / launch.session_id
