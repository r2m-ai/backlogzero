from dataclasses import replace

import pytest

from agentops import events
from agentops import workitems as wi
from agentops.workitems import InvalidTransition, SpecNotReady, ValidationError


def test_create_starts_in_backlog_with_next_id(agentops_home):
    item = wi.create(repo="quant-lab", title="采集", type="feat", module="data", priority="P1")

    assert item.status == "backlog"
    assert item.id == 1
    assert item.ref == "quant-lab#1"
    assert wi.item_path("quant-lab", 1).exists()


def test_ids_increment_per_repo(agentops_home):
    first = wi.create(repo="quant-lab", title="a")
    second = wi.create(repo="quant-lab", title="b")
    other = wi.create(repo="other", title="c")

    assert (first.id, second.id, other.id) == (1, 2, 1)


def test_ready_gate_rejects_empty_acceptance(agentops_home):
    item = wi.create(repo="quant-lab", title="x")

    with pytest.raises(SpecNotReady):
        wi.transition(item, "ready")


def test_ready_gate_passes_with_acceptance(agentops_home):
    item = wi.create(repo="quant-lab", title="x", acceptance=("tests pass",))

    assert wi.transition(item, "ready").status == "ready"


def test_transition_table_rejects_skipping_states(agentops_home):
    item = wi.create(repo="quant-lab", title="x", acceptance=("a",))

    with pytest.raises(InvalidTransition):
        wi.transition(item, "landed")


def test_full_happy_path_then_landed_is_terminal(agentops_home):
    item = wi.create(repo="quant-lab", title="x", acceptance=("a",))

    for status in ("ready", "in_progress", "queued", "testing", "landed"):
        item = wi.transition(item, status)

    assert item.status == "landed"
    with pytest.raises(InvalidTransition):
        wi.transition(item, "ready")


def test_returned_can_resume_or_requeue(agentops_home):
    item = wi.create(repo="quant-lab", title="x", acceptance=("a",))
    for status in ("ready", "in_progress", "queued", "returned"):
        item = wi.transition(item, status)

    assert wi.transition(item, "in_progress").status == "in_progress"
    assert wi.transition(item, "queued").status == "queued"


def test_transition_does_not_mutate_original(agentops_home):
    item = wi.create(repo="quant-lab", title="x", acceptance=("a",))

    wi.transition(item, "ready")

    assert item.status == "backlog"


def test_round_trip_preserves_body_and_unknown_keys(agentops_home):
    item = wi.create(repo="quant-lab", title="x", body="背景\n规格\n")

    wi.apply(item, hint="try again", returns=2)
    loaded = wi.load("quant-lab", 1)

    assert loaded.body == "背景\n规格\n"
    assert loaded.extra["hint"] == "try again"
    assert loaded.extra["returns"] == 2
    assert loaded.title == "x"


def test_blocked_by_reports_unlanded_dependencies(agentops_home):
    dep = wi.create(repo="quant-lab", title="dep", acceptance=("a",))
    item = wi.create(repo="quant-lab", title="x", depends_on=(dep.id,))

    assert wi.blocked_by(item) == (1,)

    wi.save(replace(dep, status="landed"))
    assert wi.blocked_by(item) == ()


def test_missing_dependency_counts_as_blocked(agentops_home):
    item = wi.create(repo="quant-lab", title="x", depends_on=(99,))

    assert wi.blocked_by(item) == (99,)


def test_dispatchable_requires_ready_unblocked_and_auto_trigger(agentops_home):
    auto = wi.create(repo="quant-lab", title="a", acceptance=("a",))
    nightly = wi.create(repo="quant-lab", title="n", acceptance=("a",), trigger="nightly")
    auto = wi.save(wi.transition(auto, "ready"))
    nightly = wi.save(wi.transition(nightly, "ready"))

    assert wi.is_dispatchable(auto)
    assert not wi.is_dispatchable(nightly)
    assert wi.is_dispatchable(nightly, nightly=True)


def test_module_busy_lists_active_items_in_same_module(agentops_home):
    active = wi.create(repo="quant-lab", title="a", module="research", acceptance=("a",))
    wi.save(wi.transition(wi.transition(active, "ready"), "in_progress"))
    wi.create(repo="quant-lab", title="b", module="research")

    assert [item.id for item in wi.module_busy("quant-lab", "research")] == [1]
    assert wi.module_busy("quant-lab", "data") == ()


def test_parse_ref():
    assert wi.parse_ref("quant-lab#4") == ("quant-lab", 4)
    with pytest.raises(ValidationError):
        wi.parse_ref("nope")


def test_validate_rejects_bad_priority(agentops_home):
    with pytest.raises(ValidationError):
        wi.create(repo="quant-lab", title="x", priority="P9")


def test_update_rejects_changing_id_or_repo(agentops_home):
    item = wi.create(repo="quant-lab", title="x")

    with pytest.raises(ValidationError):
        wi.update(item, id=7)


def test_apply_emits_status_event(agentops_home):
    item = wi.create(repo="quant-lab", title="x", acceptance=("a",))

    wi.apply(item, status="ready")

    status_events = [e for e in events.read_events() if e.type == "wi_status"]
    assert status_events[-1].detail["to"] == "ready"
    assert status_events[-1].item == 1
    assert status_events[-1].repo == "quant-lab"


def test_apply_force_bypasses_transition_table_and_marks_event(agentops_home):
    item = wi.create(repo="quant-lab", title="x")

    forced = wi.apply(item, status="landed", force=True)

    assert forced.status == "landed"
    last = events.read_events()[-1]
    assert last.type == "wi_status" and last.detail["forced"] is True


def test_list_items_filters_by_repo_and_status(agentops_home):
    wi.create(repo="quant-lab", title="a", acceptance=("a",))
    ready = wi.create(repo="quant-lab", title="b", acceptance=("a",))
    wi.save(wi.transition(ready, "ready"))
    wi.create(repo="other", title="c")

    assert len(wi.list_items()) == 3
    assert [i.id for i in wi.list_items(repo="quant-lab", status="ready")] == [2]


def test_from_text_rejects_missing_front_matter():
    with pytest.raises(ValidationError):
        wi.from_text("no front matter here")
