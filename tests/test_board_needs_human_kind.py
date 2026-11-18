"""needs_human_kind：非审批门的 needs_human 看板不给批准按钮，/approve 拒绝，可重新入队。"""

from http import HTTPStatus
from types import SimpleNamespace

import pytest

from agentops import approvals, board, board_data, queue
from agentops import config as config_mod
from agentops import workitems as wi

SHA = "abc1234def"


@pytest.fixture
def state(agentops_home, tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    (agentops_home / "config.toml").write_text(f'[repos]\ndemo = "{tmp_path}"\n')
    config = config_mod.load_config()
    st = board.BoardState(config, 8749)
    return SimpleNamespace(
        state=st, headers={"Host": st.host_header, "Origin": st.origin}, token=st.token
    )


def _item(kind):
    extra = {} if kind is None else {"needs_human_kind": kind}
    item = wi.create(repo="demo", title="t", acceptance=("a",))
    return wi.apply(
        item,
        status="needs_human",
        force=True,
        approval_sha=SHA,
        clone_path="/tmp/clone",
        branch="feat/1-x",
        hint="测试失败",
        **extra,
    )


def _row(item):
    panel = board_data.attention(config_mod.load_config())
    return next(r for r in panel.rows if r["ref"] == item.ref)


def _post(s, path, item, **extra):
    form = {"token": s.token, "ref": item.ref, "sha": SHA, **extra}
    return board.handle_action(s.state, path, form, s.headers)


@pytest.mark.parametrize("kind", ["failure", "max_returns"])
def test_non_gate_item_has_no_approve_button_and_approve_is_rejected(state, kind):
    item = _item(kind)

    row = _row(item)
    status, message = _post(state, "/approve", item)

    assert not row["can_approve"] and row["can_requeue"] and row["hint"] == "测试失败"
    assert 400 <= status < 500 and message
    assert not approvals.is_approved("demo", item.id, SHA)


@pytest.mark.parametrize("kind", ["approval_gate", None])
def test_gate_or_legacy_item_keeps_approve_button(state, kind):
    item = _item(kind)

    row = _row(item)
    status, _ = _post(state, "/approve", item)

    assert row["can_approve"] and not row["can_requeue"]
    assert status == HTTPStatus.OK
    assert approvals.is_approved("demo", item.id, SHA)


def test_requeue_submits_land_request_for_failure_item(state):
    item = _item("failure")

    status, _ = _post(state, "/requeue", item)

    assert status == HTTPStatus.OK
    assert wi.load("demo", item.id).status == "queued"
    assert [r.sha for r in queue.ingest_inbox("demo")] == [SHA]


def test_requeue_rejected_for_approval_gate_item(state):
    item = _item("approval_gate")

    status, _ = _post(state, "/requeue", item)

    assert 400 <= status < 500
    assert wi.load("demo", item.id).status == "needs_human"
