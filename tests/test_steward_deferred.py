"""主仓库不干净时合入延后重试，而不是 error + returned（agentops#6）。"""

from test_steward import RUN_TESTS, _commit, _git, _start, _types
from test_steward import world as world  # noqa: F401

from agentops import events, land, queue, steward
from agentops import workitems as wi


def _dirty(world):
    (world.main / "run-tests.sh").write_text(RUN_TESTS + "# local edit\n")


def _clean(world):
    _git(world.main, "checkout", "--", "run-tests.sh")


def test_dirty_main_defers_once_without_running_tests(world):
    item, clone = _start(world, "dirty main")
    _commit(clone, "feat: add thing", {"src/new.py": "x = 1\n"})
    _dirty(world)
    land.land(world.config, cwd=clone, session="s1")

    for _ in range(3):
        outcomes = steward.run_once(world.config, "demo")
        assert [o.result for o in outcomes] == ["deferred"]

    types = _types()
    assert types.count("land_deferred") == 1
    assert "tests_start" not in types
    assert "land_failed" not in types
    notes = [e for e in events.read_events() if e.type == "notification"]
    assert len(notes) == 1
    assert len(queue.pending("demo")) == 1
    assert wi.load("demo", item.id).status == "queued"


def test_lands_automatically_after_main_turns_clean(world):
    item, clone = _start(world, "recover")
    _commit(clone, "feat: add thing", {"src/new.py": "x = 1\n"})
    _dirty(world)
    land.land(world.config, cwd=clone, session="s1")
    assert steward.run_once(world.config, "demo")[0].result == "deferred"

    _clean(world)
    outcomes = steward.run_once(world.config, "demo")

    assert [o.result for o in outcomes] == ["landed"]
    assert wi.load("demo", item.id).status == "landed"


def test_main_dirtied_during_tests_requeues(world, monkeypatch):
    item, clone = _start(world, "dirty late")
    _commit(clone, "feat: add thing", {"src/new.py": "x = 1\n"})
    land.land(world.config, cwd=clone, session="s1")
    answers = iter([None, "主仓库不在 main 或工作区不干净，无法快进"])
    real = steward._main_dirty_reason
    monkeypatch.setattr(steward, "_main_dirty_reason", lambda *a: next(answers, None))

    outcome = steward.run_once(world.config, "demo")[0]

    assert outcome.result == "deferred"
    assert "tests_pass" in _types()
    assert "land_failed" not in _types()
    assert len(queue.pending("demo")) == 1
    assert wi.load("demo", item.id).status == "queued"

    monkeypatch.setattr(steward, "_main_dirty_reason", real)
    assert steward.run_once(world.config, "demo")[0].result == "landed"
