"""派发被跳过：同一 (项, 原因) 连续多轮只写一条 dispatch_skipped。"""

from test_phase4 import _ready, world  # noqa: F401, I001

from agentops import events, pm
from agentops import workitems as wi


def _skips(item_id):
    return [
        e.detail["reason"]
        for e in events.read_events()
        if e.type == "dispatch_skipped" and e.item == item_id
    ]


def _busy(title, module):
    return wi.apply(_ready(title, module=module), status="in_progress")


def test_repeated_skip_with_same_reason_writes_one_event(world):  # noqa: F811
    _busy("holder", "m1")
    waiting = _ready("waiting", module="m1")

    for _ in range(5):
        assert pm.dispatch(world.config, "demo") == ()

    assert _skips(waiting.id) == ["模块已有会话"]
    stored = wi.load("demo", waiting.id)
    assert stored.extra["skip_reason"] == "模块已有会话"
    assert stored.extra["skip_since"]


def test_reason_change_writes_new_event(world, monkeypatch):  # noqa: F811
    _busy("holder", "m1")
    waiting = _ready("waiting", module="m1")
    pm.dispatch(world.config, "demo")
    pm.dispatch(world.config, "demo")
    monkeypatch.setattr(pm.sessions, "active", lambda **_: (object(), object()))  # 并发上限 2
    pm.dispatch(world.config, "demo")
    pm.dispatch(world.config, "demo")

    assert _skips(waiting.id) == ["模块已有会话", "并发已满"]


def test_skip_counts_afresh_after_dispatch(world, monkeypatch):  # noqa: F811
    holder = _busy("holder", "m1")
    waiting = _ready("waiting", module="m1")
    pm.dispatch(world.config, "demo")
    pm.dispatch(world.config, "demo")
    wi.apply(holder, status="dropped")  # 释放模块锁
    monkeypatch.setattr(pm.start, "spawn_detached", lambda *a, **k: 1234)

    assert pm.dispatch(world.config, "demo") == (waiting.id,)
    assert "skip_reason" not in wi.load("demo", waiting.id).extra

    # 会话启动后又回到 ready，同模块再被占：重新计一条
    wi.apply(wi.load("demo", waiting.id), status="in_progress", dispatch_pending=None)
    wi.apply(wi.load("demo", waiting.id), status="ready", force=True)
    _busy("holder2", "m1")
    pm.dispatch(world.config, "demo")
    pm.dispatch(world.config, "demo")

    assert _skips(waiting.id) == ["模块已有会话", "模块已有会话"]
