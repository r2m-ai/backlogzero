"""阶段 5：看板（防伪造、11 个面板）、launchd 渲染、定时入口、简报。"""

import html
import http.client
import json
import os
import plistlib
import re
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from sandboxing import SKIP_REASON, inside_sandbox

from agentops import approvals, board, cli, events, launchd, llm, scheduled
from agentops import config as config_mod
from agentops import workitems as wi

FAKE = f"{__import__('sys').executable} {Path(__file__).parent / 'fake_claude.py'}"
STEWARD = {"AGENTOPS_STEWARD": "1"}


@pytest.fixture
def world(agentops_home, tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(llm.ENV_CLAUDE_CMD, FAKE)
    monkeypatch.setenv(
        "FAKE_CLAUDE_MODE",
        'structured={"priorities": [], "dispatch_order": [], "drop_candidates": [], "notes": "ok"}',
    )
    main = tmp_path / "demo"
    assert cli.main(["repo", "init", str(main), "--name", "demo"]) == 0
    justfile = "\n".join(
        [
            'set shell := ["bash", "-euo", "pipefail", "-c"]',
            "",
            "capture:",
            "    echo captured",
            "",
            "nightly:",
            "    echo nightly-ran",
            "",
            "test-all:",
            "    echo ok",
            "",
        ]
    )
    (main / "justfile").write_text(justfile)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@localhost"], check=True)
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "-m", "base"],
        check=True,
        env={**os.environ, **STEWARD},
    )
    config_lines = [
        "[repos]",
        f'demo = "{main}"',
        "",
        "[workspace]",
        f'clones = "{tmp_path / "clones"}"',
        f'integration = "{tmp_path / "wt"}"',
        "",
        "[steward]",
        "auto_dispatch = false",
        "",
        "[schedule]",
        'capture = "14:00"',
        'nightly = "01:30"',
        "",
    ]
    (agentops_home / "config.toml").write_text("\n".join(config_lines))
    return SimpleNamespace(main=main, config=config_mod.load_config(), tmp=tmp_path)


@pytest.fixture
def server(world):
    if inside_sandbox():
        pytest.skip(SKIP_REASON)
    srv = board.make_server(world.config, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        server=srv,
        state=srv.RequestHandlerClass.state,
        base=f"http://127.0.0.1:{srv.server_address[1]}",
    )
    srv.shutdown()
    srv.server_close()


def _request(base, path, *, data=None, headers=None):
    body = None if data is None else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        base + path, data=body, method="POST" if data is not None else "GET"
    )
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _h(text):
    return html.escape(str(text), quote=True)


def _needs_human_item(sha="abc1234def"):
    item = wi.create(repo="demo", title="needs approval", acceptance=("a",))
    return wi.apply(
        item,
        status="needs_human",
        force=True,
        approval_sha=sha,
        hint="受保护路径",
        branch="feat/1-x",
    )


def test_board_renders_eleven_panels_and_only_binds_localhost(server):
    status, html = _request(server.base, "/")

    assert status == 200
    for title in (
        "需要你看",
        "PM 建议",
        "工作项",
        "正在跑的会话",
        "合并队列",
        "重任务槽位",
        "今天的计划和实际运行",
        "dbt 结果",
        "数据新鲜度",
        "磁盘",
        "KPI（7 天）",
    ):
        assert title in html, title
    assert server.server.server_address[0] == "127.0.0.1"
    assert "暂无" in html  # 空数据显示暂无，不报错
    assert "面板出错" not in html


def test_board_api_state_has_eleven_panels_without_errors(server):
    status, body = _request(server.base, "/api/state")
    panels = json.loads(body)
    assert status == 200 and len(panels) == 11
    assert all(not p["error"] for p in panels), [p["error"] for p in panels if p["error"]]


def test_forged_requests_are_rejected(server, world):
    item = _needs_human_item()
    state = server.state
    good = {"Host": state.host_header, "Origin": state.origin}
    form = {"token": state.token, "ref": item.ref, "sha": "abc1234def"}

    assert (
        _request(server.base, "/approve", data=form, headers={"Host": state.host_header})[0] == 403
    )  # 没有 Origin
    assert (
        _request(
            server.base, "/approve", data=form, headers={**good, "Origin": "http://evil.example"}
        )[0]
        == 403
    )
    assert (
        _request(server.base, "/approve", data=form, headers={**good, "Host": "localhost:8749"})[0]
        == 403
    )
    assert (
        _request(server.base, "/approve", data={**form, "token": "wrong"}, headers=good)[0] == 403
    )
    assert (
        _request(server.base, "/approve", data={**form, "sha": "stale00"}, headers=good)[0] == 409
    )

    assert not approvals.is_approved("demo", item.id, "abc1234def")
    rejected = [e for e in events.read_events() if e.type == "board_rejected"]
    assert len(rejected) == 4


def test_board_approve_and_drop_with_valid_token(server, world):
    item = _needs_human_item()
    state = server.state
    good = {"Host": state.host_header, "Origin": state.origin}
    status, html = _request(server.base, "/")
    token = re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)
    assert token == state.token

    status, _ = _request(
        server.base,
        "/approve",
        data={"token": token, "ref": item.ref, "sha": "abc1234def"},
        headers=good,
    )

    assert status == 200
    approval = approvals.find("demo", item.id, "abc1234def")
    assert approval is not None and approval.by == "board"

    other = wi.apply(wi.create(repo="demo", title="to drop", acceptance=("a",)), status="ready")
    status, _ = _request(
        server.base, "/drop", data={"token": token, "ref": other.ref}, headers=good
    )
    assert status == 200 and wi.load("demo", other.id).status == "dropped"
    assert "dropped" in [e.type for e in events.read_events()]


def test_board_drop_removes_the_items_clone(server, world):
    # Arrange：一个带克隆目录的 ready 工作项
    from agentops import clones

    item = wi.apply(wi.create(repo="demo", title="drop me", acceptance=("a",)), status="ready")
    clone = clones.clone_dir(world.config, item)
    clone.mkdir(parents=True)
    (clone / "marker").write_text("x")
    token = server.state.token  # ready 的工作项不在「需要你看」里，页面上没有它的表单
    good = {"Host": server.state.host_header, "Origin": server.state.origin}

    # Act
    status, _ = _request(server.base, "/drop", data={"token": token, "ref": item.ref}, headers=good)

    # Assert：状态 dropped，克隆目录已删
    assert status == 200 and wi.load("demo", item.id).status == "dropped"
    assert not clone.exists()
    removed = [e for e in events.read_events() if e.type == "clone_removed"]
    assert removed and removed[-1].detail["reason"] == "dropped"


def test_launchd_render_produces_valid_plists(world, tmp_path):
    rendered = launchd.render_all(world.config, target_dir=tmp_path / "agents")

    assert [r.name for r in rendered] == ["steward", "board", "capture", "nightly"]
    for item in rendered:
        data = plistlib.loads(item.path.read_bytes())
        assert data["Label"] == item.label
        assert data["ProgramArguments"][0] == str(Path.home() / ".local" / "bin" / "ao")
        assert "PATH" in data["EnvironmentVariables"]
    capture = plistlib.loads((tmp_path / "agents" / "com.agentops.capture.plist").read_bytes())
    assert capture["StartCalendarInterval"] == {"Hour": 14, "Minute": 0}
    nightly = plistlib.loads((tmp_path / "agents" / "com.agentops.nightly.plist").read_bytes())
    assert nightly["StartCalendarInterval"] == {"Hour": 1, "Minute": 30}
    steward = plistlib.loads((tmp_path / "agents" / "com.agentops.steward.plist").read_bytes())
    assert steward["KeepAlive"] is True
    commands = launchd.install_commands(rendered)
    assert sum(c.startswith("launchctl bootstrap gui/") for c in commands) == 4
    assert sum(c.startswith("cp ") for c in commands) == 4
    assert launchd.kick_command("capture").endswith("/com.agentops.capture")


def test_capture_and_nightly_run_recipes_and_write_events(world):
    results = scheduled.capture(world.config)
    assert results == {"demo": 0}

    outcome = scheduled.nightly(world.config, dispatch=False)

    types = [e.type for e in events.read_events()]
    for expected in (
        "capture_start",
        "capture_end",
        "nightly_run_start",
        "nightly_start",
        "nightly_end",
        "pm_run",
        "brief_written",
        "nightly_run_end",
    ):
        assert expected in types, expected
    assert outcome["demo"]["just_nightly_rc"] == 0
    brief = Path(outcome["brief"])
    assert brief.exists()
    text = brief.read_text()
    assert "每日简报" in text and "7 天 KPI" in text and "需要你看" in text
    log = next((Path(world.tmp) / "agentops-state" / "logs" / "demo").glob("capture-*.log"))
    assert "captured" in log.read_text()


def test_recipe_failure_is_recorded_not_fatal(world):
    (world.main / "justfile").write_text("nightly:\n    exit 3\n")

    outcome = scheduled.nightly(world.config, dispatch=False)

    assert outcome["demo"]["just_nightly_rc"] == 3
    assert [e for e in events.read_events() if e.type == "nightly_end"][-1].detail["rc"] == 3
    assert "brief_written" in [e.type for e in events.read_events()]


def test_board_post_redirects_to_index_and_escapes_message(server, world):
    # Arrange：有效令牌的批准请求，用 http.client 发以便看到 303 而不是被自动跟随
    item = _needs_human_item()
    state = server.state
    _, page = _request(server.base, "/")
    token = re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
    body = urllib.parse.urlencode({"token": token, "ref": item.ref, "sha": "abc1234def"})
    target = urllib.parse.urlsplit(server.base)
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)

    # Act
    conn.request(
        "POST",
        "/approve",
        body=body,
        headers={
            "Host": state.host_header,
            "Origin": state.origin,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = conn.getresponse()
    location = resp.getheader("Location") or ""
    resp.read()
    conn.close()
    status, landing = _request(server.base, location)
    _, injected = _request(server.base, "/?m=%3Cscript%3Ealert(1)%3C/script%3E")

    # Assert：POST 后 303 回首页并带回提示；提示经过转义，30 秒自动刷新落在首页而不是 /approve
    assert resp.status == 303 and location.startswith("/?m=")
    message = urllib.parse.unquote(location[len("/?m=") :])
    assert status == 200 and message and _h(message) in landing
    assert "<script>" not in injected and "&lt;script&gt;" in injected
    assert approvals.find("demo", item.id, "abc1234def") is not None


def test_nightly_waits_for_dispatched_items_before_brief(world, monkeypatch):
    # Arrange：假派发返回工作项 8；第一次轮询时它还在 in_progress，第二次变成 landed
    from agentops import pm, scheduled

    item = wi.apply(
        wi.create(repo="demo", title="brief", acceptance=("a",), trigger="nightly"),
        status="ready",
    )
    monkeypatch.setattr(scheduled, "run_recipe", lambda *a, **k: 0)
    monkeypatch.setattr(
        pm,
        "plan",
        lambda *a, **k: type("R", (), {"source": "det", "cost_usd": 0.0, "dispatch_order": ()})(),
    )
    monkeypatch.setattr(pm, "dispatch", lambda *a, **k: [item.id])
    seen = []

    def fake_sleep(seconds):
        # 第一次轮询时工作项刚派发、还是 ready（ao start 尚未把它改成 in_progress）；之后才合入
        seen.append(seconds)
        if len(seen) == 1:
            wi.apply(wi.load("demo", item.id), status="in_progress", force=True)
        else:
            wi.apply(wi.load("demo", item.id), status="landed", force=True)

    monkeypatch.setattr(scheduled.time, "sleep", fake_sleep)

    # Act
    results = scheduled.nightly(world.config, ["demo"])

    # Assert：刚派发时的 ready 不算完成，轮询到 landed 才写简报，事件里记录了结果
    assert seen == [scheduled.WAIT_POLL_SECONDS, scheduled.WAIT_POLL_SECONDS]
    assert results["demo"]["settled"] == {str(item.id): "landed"}
    waits = [e for e in events.read_events() if e.type == "nightly_wait_end"]
    assert waits and waits[-1].detail["timed_out"] is False
    order = [
        e.type for e in events.read_events() if e.type in ("nightly_wait_end", "brief_written")
    ]
    assert order[-2:] == ["nightly_wait_end", "brief_written"]


def test_nightly_wait_gives_up_on_items_that_never_start(world, monkeypatch):
    # Arrange：派发"成功"但工作项一直停在 ready（启动失败回滚的情形）
    from agentops import pm, scheduled

    item = wi.apply(
        wi.create(repo="demo", title="stuck", acceptance=("a",), trigger="nightly"),
        status="ready",
    )
    monkeypatch.setattr(scheduled, "run_recipe", lambda *a, **k: 0)
    monkeypatch.setattr(
        pm,
        "plan",
        lambda *a, **k: type("R", (), {"source": "det", "cost_usd": 0.0, "dispatch_order": ()})(),
    )
    monkeypatch.setattr(pm, "dispatch", lambda *a, **k: [item.id])
    sleeps = []
    monkeypatch.setattr(scheduled.time, "sleep", lambda s: sleeps.append(s))

    # Act
    results = scheduled.nightly(world.config, ["demo"])

    # Assert：连续 3 次 ready 后放弃等待，不会耗满 30 分钟
    assert results["demo"]["settled"] == {str(item.id): "ready"}
    assert len(sleeps) == scheduled.NOT_STARTED_GRACE_POLLS - 1


def test_run_recipe_injects_keychain_secrets_as_env(world, monkeypatch):
    # Arrange：钥匙串里有 eodhd，justfile 的 recipe 把环境变量写进文件
    from agentops import scheduled, secret_store

    repo_path = world.config.repo_path("demo")
    (repo_path / "justfile").write_text(
        'set shell := ["bash", "-euo", "pipefail", "-c"]\n\n'
        "envcheck:\n    printf '%s' \"$EODHD_API_TOKEN\" > token-seen.txt\n"
    )
    monkeypatch.setattr(
        secret_store, "read_secret", lambda name: "fake-" + name + "-token-0123456789"
    )

    # Act
    rc = scheduled.run_recipe(world.config, "demo", "envcheck")

    # Assert：子进程看到了 token，日志和事件里没有它
    assert rc == 0
    assert (repo_path / "token-seen.txt").read_text() == "fake-eodhd-token-0123456789"
    assert not any("fake-eodhd" in str(e.detail) for e in events.read_events())


def test_run_recipe_records_missing_secret_and_continues(world, monkeypatch):
    from agentops import scheduled, secret_store
    from agentops.auth import AuthError

    repo_path = world.config.repo_path("demo")
    (repo_path / "justfile").write_text("noop:\n    @true\n")

    def missing(name):
        raise AuthError("钥匙串里没有 agentops-" + name)

    monkeypatch.setattr(secret_store, "read_secret", missing)

    rc = scheduled.run_recipe(world.config, "demo", "noop")

    assert rc == 0
    missing_events = [e for e in events.read_events() if e.type == "noop_secret_missing"]
    assert missing_events and missing_events[-1].detail["secret"] == "eodhd"
