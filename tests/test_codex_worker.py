"""阶段 C：ao start 走 runtime 层起 Codex worker 会话。会话 profile 渲染与沙箱自检、启动脚本、
事件流解析与记账、失败回滚，以及阶段 C 明确不支持的交互模式与退回重做。全部用假的 codex。"""

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sandboxing import inside_sandbox, steward_sandbox_lines

from agentops import approvals, budget, cli, clones, events, sessions, start, steward
from agentops import config as config_mod
from agentops import workitems as wi
from agentops.errors import AgentopsError
from agentops.runtime import codex as codex_rt
from agentops.runtime import codex_worker

FAKE_CODEX = f"{sys.executable} {Path(__file__).parent / 'fake_codex.py'}"
AO = Path(sys.executable).parent / "ao"
STEWARD = {"AGENTOPS_STEWARD": "1"}
ESTIMATE = (1000 * 2 + 200 * 0.1 + 50 * 10) / 1_000_000  # 默认 usage 按 gpt-6.1-sol 价格表
PIPELINE = """[repo]
name = "demo"
default_branch = "main"
autonomy = "auto"
test_cmd = "sh run-tests.sh"
max_sessions = 2

[protected]
paths = ["AGENTS.md", ".pipeline.toml", ".claude/", ".codex/"]
existing_only = ["tests/"]

[rules]
deny_delete_under = ["src/"]
deny_public_function_removal = true
"""
pytestmark = pytest.mark.skipif(not AO.exists(), reason="需要 uv sync 装好项目自己的 ao")


def _git(path, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _config_text(main: Path, tmp_path: Path, *, auto_resume: bool = False, extra: str = "") -> str:
    return "\n".join(
        [
            "[general]",
            "max_sessions = 2",
            "daily_budget_usd = 10",
            "",
            "[repos.demo]",
            f'path = "{main}"',
            'runtime = "codex"',
            "",
            "[workspace]",
            f'clones = "{tmp_path / "clones"}"',
            f'integration = "{tmp_path / "wt"}"',
            "",
            "[steward]",
            "poll_seconds = 1",
            "test_timeout_minutes = 1",
            "max_returns = 2",
            f"auto_resume = {'true' if auto_resume else 'false'}",
            "auto_dispatch = false",
            *steward_sandbox_lines(),
            "",
            extra,
        ]
    )


@pytest.fixture
def world(agentops_home, tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    for name in ("FAKE_CODEX_SANDBOX", "FAKE_CODEX_USAGE", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(codex_rt.ENV_CODEX_CMD, FAKE_CODEX)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    monkeypatch.setenv("FAKE_CODEX_MODE", "idle")
    monkeypatch.setenv("AGENTOPS_AO", str(AO))
    user_codex = tmp_path / "user-codex"
    user_codex.mkdir()
    (user_codex / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(user_codex))
    main = tmp_path / "demo"
    assert cli.main(["repo", "init", str(main), "--name", "demo", "--runtime", "codex"]) == 0
    (main / ".pipeline.toml").write_text(PIPELINE)
    (main / "run-tests.sh").write_text("#!/bin/sh\nif [ -f .fail ]; then exit 1; fi\necho ok\n")
    (main / "src").mkdir()
    (main / "src" / "mod.py").write_text("def public():\n    return 1\n")
    (main / "tests").mkdir()
    (main / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    _git(main, "config", "user.name", "t")
    _git(main, "config", "user.email", "t@localhost")
    _git(main, "add", "-A")
    assert _git(main, "commit", "-q", "-m", "base", env=STEWARD).returncode == 0

    def configure(**kwargs):
        (agentops_home / "config.toml").write_text(_config_text(main, tmp_path, **kwargs))
        return config_mod.load_config()

    return SimpleNamespace(
        home=agentops_home,
        main=main,
        tmp=tmp_path,
        user_codex=user_codex,
        log=tmp_path / "codex.log",
        configure=configure,
        config=configure(),
    )


def _ready(title, module="core", **extra):
    item = wi.create(repo="demo", title=title, module=module, acceptance=("a",))
    return wi.apply(item, status="ready", **extra)


def _calls(world, *, sandbox: bool) -> list[dict]:
    if not world.log.exists():
        return []
    records = [json.loads(line) for line in world.log.read_text().splitlines() if line.strip()]
    return [r for r in records if bool(r.get("sandbox")) == sandbox]


def _last(kind: str):
    return [e for e in events.read_events() if e.type == kind][-1]


def _profile(launch) -> Path:
    return codex_rt.codex_home() / f"agentops-{launch.session_id}.config.toml"


# ---- 准备：profile、标记、脚本、自检 ----


def test_prepare_renders_session_profile_with_sandbox_rules_and_env(world):
    # Arrange：config 里还有另一个仓库，它的 holdout 也不能让这个会话读到
    config = world.configure(extra=f'[repos.other]\npath = "{world.tmp / "other"}"\n')
    item = _ready("profile demo")

    launch = start.prepare_launch(config, item, mode="headless")
    profile = tomllib.loads(_profile(launch).read_text())
    rules = profile["permissions"]["agentops"]["filesystem"]
    env = profile["shell_environment_policy"]["set"]
    clone = str(launch.clone.resolve())

    assert launch.runtime == "codex" and launch.model == "gpt-6.1-sol"
    assert profile["default_permissions"] == "agentops"
    assert profile["permissions"]["agentops"]["extends"] == ":workspace"
    assert profile["permissions"]["agentops"]["network"]["enabled"] is False
    assert rules[f"{clone}/.git"] == "write"  # :workspace 默认 .git 只读，不放开就提交不了
    assert rules[f"{clone}/.git/config"] == "read"  # 但改不了 git 配置（能带起外部程序）
    assert rules[f"{clone}/.git/hooks"] == "read"  # 也改不了克隆里的 main 保护钩子
    assert rules[f"{clone}/.codex"] == "read"  # worker 改不了自己的 hooks.json
    assert rules[str(Path("~/data/other/holdout").expanduser())] == "deny"
    assert rules[str(Path.home() / ".git-credentials")] == "deny"
    assert rules[str(world.home / "inbox")] == "write"
    assert rules[str(Path("~/.cache/uv").expanduser())] == "write"
    assert rules[str(Path("~/data/demo/holdout").expanduser())] == "deny"
    assert rules[str(codex_rt.codex_home())] == "deny"
    assert rules[str(world.user_codex)] == "deny"
    assert rules[str(Path.home() / ".claude")] == "deny"
    assert profile["projects"][clone]["trust_level"] == "trusted"
    assert env == {
        "AGENTOPS_REPO": "demo",
        "AGENTOPS_ITEM": str(item.id),
        "AGENTOPS_MODE": "headless",
        "AGENTOPS_BRANCH": launch.branch,
        "AGENTOPS_SESSION": launch.session_id,
        "AGENTOPS_RUNTIME": "codex",
        "UV_OFFLINE": "1",
        "PYTHONHASHSEED": "0",
    }
    assert _last("session_start").detail["runtime"] == "codex"


def test_prepare_writes_clone_marker_that_hooks_can_find(world):
    item = _ready("marker demo")

    launch = start.prepare_launch(world.config, item, mode="headless")
    digest = hashlib.sha256(str(launch.clone.resolve()).encode()).hexdigest()[:24]
    marker = world.home / "sessions" / "by-clone" / f"{digest}.json"

    assert codex_worker.marker_path(launch.clone) == marker  # 与 hooks/_common.py 同一算法
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text()) == {
        "session": launch.session_id,
        "repo": "demo",
        "item": item.id,
        "mode": "headless",
        "branch": launch.branch,
        "runtime": "codex",
        "clone": str(launch.clone.resolve()),
        # 拦截策略取自 main 上的 .pipeline.toml，连同起会话时 main 的提交号
        "base_sha": _git(world.main, "rev-parse", "main").stdout.strip(),
        "policy": {
            "default_branch": "main",
            "protected": ["AGENTS.md", ".pipeline.toml", ".claude/", ".codex/"],
            "existing_only": ["tests/"],
        },
    }


def test_prepare_writes_codex_script_reading_prompt_from_stdin(world):
    item = wi.apply(_ready("script demo"), model="gpt-6-astra", effort="high")

    launch = start.prepare_launch(world.config, item, mode="headless")
    script = launch.script_path.read_text()

    assert f"exec --json --profile agentops-{launch.session_id}" in script
    assert "--dangerously-bypass-hook-trust" in script
    assert "-m gpt-6-astra" in script and "model_reasoning_effort=high" in script
    assert f"export CODEX_HOME={codex_rt.codex_home()}" in script
    assert "AGENTOPS_RUNTIME=codex" in script and f"AGENTOPS_ITEM={item.id}" in script
    assert "unset OPENAI_API_KEY" in script
    assert f"- < {launch.prompt_path}" in script and f"> {launch.result_path}" in script
    assert "--ephemeral" not in script  # 留下 rollout 文件，事后可查
    assert "security find-generic-password" not in script and "claude" not in script.lower()
    assert _calls(world, sandbox=False) == []  # 只准备，没启动


def test_prepare_self_checks_the_sandbox_before_launching(world):
    item = _ready("self check")

    launch = start.prepare_launch(world.config, item, mode="headless")
    probes = _calls(world, sandbox=True)
    targets = [p["args"][-1] for p in probes]

    assert len(probes) >= 3
    for probe in probes:
        args = probe["args"]
        assert args[args.index("-p") + 1] == f"agentops-{launch.session_id}"
        assert args[args.index("-P") + 1] == "agentops"
        assert args[args.index("-C") + 1] == str(launch.clone)
        assert probe["env"]["CODEX_HOME"] == str(codex_rt.codex_home())
    clone = launch.clone
    assert targets[0] == str(clone / ".git" / "agentops-sandbox-probe")  # 正对照先跑
    assert str(world.home / "approvals" / ".agentops-sandbox-probe") in targets  # 克隆外写不了
    assert str(clone / ".git" / "hooks" / "agentops-sandbox-probe") in targets
    assert str(clone / ".codex" / "agentops-sandbox-probe") in targets
    assert str(codex_rt.codex_home() / "sandbox-sentinel") in targets  # 登录态所在目录读不到
    if not inside_sandbox():  # 回环探针要能开监听；在 steward 的测试沙箱里开不了就不测
        assert any(os.path.basename(p["args"][-6]) == "nc" for p in probes if len(p["args"]) > 6)
    assert not list(clone.rglob("agentops-sandbox-probe"))
    assert not list((world.home / "approvals").glob(".agentops-sandbox-probe"))


def test_self_check_catches_a_profile_that_forgets_to_open_dot_git(world, monkeypatch, tmp_path):
    # Arrange：阶段 A 的实验仓库在 /private/tmp 下，把 ":workspace 默认 .git 只读" 盖住了；
    # 第一次真跑就是这条正对照探针拦下来的。这里用缺了那条规则的模板重现。
    template = tmp_path / "session.config.toml"
    lines = codex_worker.SESSION_TEMPLATE.read_text().splitlines()
    template.write_text("\n".join(ln for ln in lines if '"$clone/.git" = "write"' not in ln))
    monkeypatch.setattr(codex_worker, "SESSION_TEMPLATE", template)
    item = _ready("no git rule")

    with pytest.raises(AgentopsError, match="没有生效"):
        start.prepare_launch(world.config, item, mode="headless")

    assert wi.load("demo", item.id).status == "ready"


@pytest.mark.parametrize(("mode", "message"), [("leaky", "沙箱"), ("broken", "profile")])
def test_failed_self_check_rolls_back_and_never_launches(world, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_CODEX_SANDBOX", mode)
    item = _ready("unsafe")

    with pytest.raises(AgentopsError, match=message):
        start.prepare_launch(world.config, item, mode="headless")

    assert wi.load("demo", item.id).status == "ready"
    assert not clones.clone_dir(world.config, item).exists()
    assert not list(codex_rt.codex_home().glob("agentops-*.config.toml"))
    assert not list((world.home / "sessions" / "by-clone").glob("*.json"))
    assert _calls(world, sandbox=False) == []
    assert budget.outstanding() == 0


def test_model_name_that_looks_like_an_option_is_refused(world):
    item = wi.apply(_ready("bad model"), model="--dangerously-anything")

    with pytest.raises(AgentopsError, match="模型名"):
        start.prepare_launch(world.config, item, mode="headless")

    assert wi.load("demo", item.id).status == "ready"
    assert not list(codex_rt.codex_home().glob("agentops-*.config.toml"))


# ---- 运行与记账 ----


def test_headless_codex_session_lands_and_records_tokens(world, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "work")
    item = _ready("headless codex")

    launch = start.prepare_launch(world.config, item, mode="headless")
    result = start.run_headless(world.config, launch)
    (call,) = _calls(world, sandbox=False)
    end = _last("session_end")
    loaded = wi.load("demo", item.id)

    assert result.rc == 0 and not result.is_error and result.land_requested
    assert result.cost_usd == pytest.approx(ESTIMATE) and result.session_id == launch.session_id
    assert "landed rc=0" in result.summary
    assert call["cwd"] == str(launch.clone.resolve()) or call["cwd"] == str(launch.clone)
    assert call["env"]["AGENTOPS_ITEM"] == str(item.id)
    assert call["env"]["AGENTOPS_MODE"] == "headless" and call["env"]["AGENTOPS_RUNTIME"] == "codex"
    assert call["env"]["CODEX_HOME"] == str(codex_rt.codex_home())
    assert item.ref in call["prompt"] and "ao land" in call["prompt"]
    assert end.session == launch.session_id
    assert end.detail["runtime"] == "codex" and end.detail["role"] == "worker"
    assert end.detail["usage"]["input_tokens"] == 1200 and end.detail["cost_estimated"] is True
    assert end.detail["thread_id"] == "fake-thread" and end.detail["land_requested"] is True
    assert end.detail["cost_usd"] == pytest.approx(ESTIMATE)
    assert loaded.session_id == launch.session_id
    assert loaded.extra["codex_thread_id"] == "fake-thread"
    assert sessions.latest_by_id()[launch.session_id].status == "ended"
    assert budget.outstanding() == 0
    # 会话结束后清理：profile 与标记不留
    assert not _profile(launch).exists()
    assert not codex_worker.marker_path(launch.clone).exists()

    outcomes = steward.run_once(world.config, "demo")
    assert outcomes and outcomes[0].result == "landed"
    assert "fake codex session work" in _git(world.main, "log", "--oneline", "main").stdout


def test_profile_and_marker_are_removed_even_if_finishing_blows_up(world, monkeypatch):
    item = _ready("finish fails")
    launch = start.prepare_launch(world.config, item, mode="headless")

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(start, "_finish", boom)

    with pytest.raises(RuntimeError):
        start.run_headless(world.config, launch)

    assert not _profile(launch).exists()
    assert not codex_worker.marker_path(launch.clone).exists()


def test_codex_estimate_counts_toward_the_shared_daily_usd_budget(world, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "work")
    item = _ready("budget share")
    launch = start.prepare_launch(world.config, item, mode="headless")

    start.run_headless(world.config, launch)

    # 审核人定的双口径：当日美元上限 = Claude 实花 + Codex 估算
    assert budget.spend_today() == pytest.approx(ESTIMATE, abs=1e-4)
    # 日上限口径不计缓存命中（agentops#15）：1200 输入里有 200 是缓存命中，加 50 输出
    assert budget.tokens_today(runtime="codex", role="worker") == 1050
    assert budget.tokens_today(runtime="claude", role="worker") == 0


def test_idle_codex_session_without_land_goes_to_needs_human(world):
    item = _ready("idle codex")
    launch = start.prepare_launch(world.config, item, mode="headless")

    result = start.run_headless(world.config, launch)
    loaded = wi.load("demo", item.id)

    assert result.rc == 0 and not result.land_requested
    assert loaded.status == "needs_human" and "什么都没做" in str(loaded.extra["hint"])


def test_failed_turn_is_recorded_as_error_with_reason(world, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "failed")
    item = _ready("quota gone")
    launch = start.prepare_launch(world.config, item, mode="headless")

    result = start.run_headless(world.config, launch)

    assert result.rc == 1 and result.is_error
    assert "usage limit reached" in result.summary
    assert _last("session_end").detail["is_error"] is True


def test_worker_daily_token_cap_refuses_to_start(world):
    # Arrange：今天 Codex worker 已经用掉 1100 token，上限 1000
    config = world.configure(extra="[budget_tokens]\nworker = 1000\n")
    events.emit(
        "session_end",
        repo="demo",
        detail={
            "runtime": "codex",
            "role": "worker",
            "usage": {"input_tokens": 900, "output_tokens": 200},
        },
    )
    item = _ready("over cap")

    with pytest.raises(budget.BudgetExceeded, match="token"):
        start.prepare_launch(config, item, mode="headless")

    assert wi.load("demo", item.id).status == "ready"
    assert not clones.clone_dir(config, item).exists()


# ---- 阶段 C 不做的事要明说 ----


def test_interactive_mode_is_refused_for_codex_repos_without_side_effects(world):
    item = _ready("interactive")

    with pytest.raises(AgentopsError, match="headless"):
        start.prepare_launch(world.config, item, mode="interactive")

    assert wi.load("demo", item.id).status == "ready"
    assert not clones.clone_dir(world.config, item).exists()


def test_resume_is_refused_and_steward_does_not_spawn_it(world, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "work")
    item = _ready("rework")
    launch = start.prepare_launch(world.config, item, mode="headless")
    start.run_headless(world.config, launch)
    returned = wi.apply(wi.load("demo", item.id), status="returned", force=True)
    config = world.configure(auto_resume=True)

    with pytest.raises(AgentopsError, match="阶段 D"):
        start.resume_headless(config, returned, hint="重做", round_no=1)
    steward._maybe_resume(config, returned, "重做", 1)
    types = [e.type for e in events.read_events()]

    assert "resume_spawned" not in types and "resume_skipped" in types
    assert _last("resume_skipped").detail["runtime"] == "codex"


def test_approve_is_refused_inside_a_codex_session(agentops_home, monkeypatch):
    for name in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "01a1-thread")

    with pytest.raises(AgentopsError, match="会话"):
        approvals.record("demo", 1, "abcdef1234", by="terminal")
