"""agentops#7：审批后重测失败进 needs_human，不得无限重排队。"""

from test_steward import STEWARD, _commit, _git, _run, _start, _types, world  # noqa: F401

from agentops import approvals, steward
from agentops import workitems as wi


def test_approved_then_test_failure_is_not_requeued_again(world):  # noqa: F811
    item, clone = _start(world, "touch agents then fail")
    sha = _commit(clone, "edit AGENTS", {"AGENTS.md": "# changed\n"})
    assert _run(world, clone).result == "needs_human"
    # 人批准之前 main 上多了一条隔离不好的测试：重测必败
    (world.main / ".fail").write_text("1\n")
    _git(world.main, "add", "-A")
    assert _git(world.main, "commit", "-q", "-m", "flaky", env=STEWARD).returncode == 0

    approvals.record("demo", item.id, sha[:7], by="terminal", note="ok")
    steward.run_once(world.config, "demo")
    for _ in range(3):
        steward.run_once(world.config, "demo")

    assert _types().count("requeued_after_approval") == 1
    loaded = wi.load("demo", item.id)
    assert loaded.status in {"needs_human", "returned"}
    assert loaded.extra.get("hint")


def test_max_returns_needs_human_is_not_requeued_by_old_approval(world):  # noqa: F811
    item, clone = _start(world, "keeps failing")
    sha = _commit(clone, "break", {".fail": "1\n"})
    for _ in range(3):
        _run(world, clone)
    assert wi.load("demo", item.id).status == "needs_human"

    approvals.record("demo", item.id, sha[:7], by="terminal", note="ok")
    steward.run_once(world.config, "demo")

    assert "requeued_after_approval" not in _types()
    assert wi.load("demo", item.id).status == "needs_human"
