import pytest

from agentops import sessions
from agentops.errors import AgentopsError


def test_register_then_end(agentops_home):
    sessions.register(
        "s1", repo="quant-lab", item=4, branch="feat/4-x", worktree="/tmp/wt", mode="headless"
    )

    assert [s.session_id for s in sessions.active()] == ["s1"]

    ended = sessions.end("s1", rc=0, cost_usd=1.25)

    assert ended.status == "ended"
    assert ended.item == 4
    assert sessions.active() == ()
    assert sessions.latest_by_id()["s1"].cost_usd == 1.25


def test_end_unknown_session_fails(agentops_home):
    with pytest.raises(AgentopsError):
        sessions.end("ghost")


def test_stale_sessions_are_ignored_and_pruned(agentops_home):
    import json

    from agentops.jsonl import append_line
    from agentops.paths import sessions_file

    sessions.register("fresh", repo="r", item=1)
    append_line(
        sessions_file(),
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "session_id": "old", "status": "running"}),
    )

    assert {s.session_id for s in sessions.active()} == {"fresh", "old"}
    assert [s.session_id for s in sessions.active(stale_after_hours=1)] == ["fresh"]

    pruned = sessions.prune(stale_after_hours=1)

    assert [s.session_id for s in pruned] == ["old"]
    assert [s.session_id for s in sessions.active()] == ["fresh"]
