"""ao repo init、git 的 main 保护钩子、Claude Code hooks（喂模拟 JSON）。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentops import cli, events, repo

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
AO_BIN = Path.home() / ".local" / "bin" / "ao"
pytestmark = pytest.mark.skipif(not AO_BIN.exists(), reason="需要先 just install 装好 ao")


def _git(path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def project(agentops_home, tmp_path):
    path = tmp_path / "demo"
    assert cli.main(["repo", "init", str(path), "--name", "demo"]) == 0
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@localhost")
    (path / "tests").mkdir()
    (path / "tests" / "test_existing.py").write_text("def test_a():\n    assert True\n")
    (path / "src").mkdir()
    (path / "src" / "mod.py").write_text("X = 1\n")
    _git(path, "add", "-A")
    assert _git(path, "commit", "-q", "-m", "init", env={"AGENTOPS_STEWARD": "1"}).returncode == 0
    return path


def test_repo_init_writes_templates_and_hooks(project):
    assert (project / "AGENTS.md").read_text().startswith("# AGENTS.md — demo")
    assert 'name = "demo"' in (project / ".pipeline.toml").read_text()
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]
    assert "~/data/demo/holdout" in settings["sandbox"]["filesystem"]["denyRead"][0]
    for name in repo.GIT_HOOKS:
        hook = project / ".git" / "hooks" / name
        assert hook.exists() and os.access(hook, os.X_OK)
    assert _git(project, "config", "gc.auto").stdout.strip() == "0"


def test_repo_init_is_idempotent(project, capsys):
    (project / "AGENTS.md").write_text("custom\n")

    assert cli.main(["repo", "init", str(project), "--name", "demo"]) == 0

    assert (project / "AGENTS.md").read_text() == "custom\n"
    assert "跳过 AGENTS.md" in capsys.readouterr().out


def test_commit_on_main_is_rejected_unless_steward(project):
    (project / "src" / "mod.py").write_text("X = 2\n")
    _git(project, "add", "-A")

    denied = _git(project, "commit", "-q", "-m", "direct")
    assert denied.returncode != 0
    assert "steward" in denied.stderr

    allowed = _git(project, "commit", "-q", "-m", "by steward", env={"AGENTOPS_STEWARD": "1"})
    assert allowed.returncode == 0
    blocks = [e for e in events.read_events() if e.type == "main_protect_block"]
    assert blocks and blocks[-1].detail["hook"] == "pre-commit" and blocks[-1].repo == "demo"


def test_update_ref_on_main_is_rejected(project):
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    _git(project, "checkout", "-q", "-b", "feat/1-x")
    (project / "src" / "mod.py").write_text("X = 3\n")
    _git(project, "add", "-A")
    assert _git(project, "commit", "-q", "-m", "feature work").returncode == 0
    new = _git(project, "rev-parse", "HEAD").stdout.strip()

    denied = _git(project, "update-ref", "refs/heads/main", new)
    assert denied.returncode != 0
    assert _git(project, "rev-parse", "main").stdout.strip() == head

    allowed = _git(project, "update-ref", "refs/heads/main", new, env={"AGENTOPS_STEWARD": "1"})
    assert allowed.returncode == 0
    assert _git(project, "rev-parse", "main").stdout.strip() == new


def _run_hook(name: str, payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _edit(project: Path, path: str, tool: str = "Edit") -> dict:
    return {
        "session_id": "sess-1",
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": path},
    }


def test_protect_tests_blocks_existing_test_and_allows_new(project):
    blocked = _run_hook("protect_tests.py", _edit(project, "tests/test_existing.py"))
    assert blocked.returncode == 2
    assert "只能新增" in blocked.stderr

    allowed = _run_hook("protect_tests.py", _edit(project, "tests/test_new.py", tool="Write"))
    assert allowed.returncode == 0

    source = _run_hook("protect_tests.py", _edit(project, "src/mod.py"))
    assert source.returncode == 0


def test_protect_tests_blocks_protected_paths_and_logs_event(project):
    for path in ("AGENTS.md", ".pipeline.toml", ".claude/settings.json", "justfile"):
        result = _run_hook("protect_tests.py", _edit(project, path), env={"AGENTOPS_ITEM": "4"})
        assert result.returncode == 2, path

    blocks = [e for e in events.read_events() if e.type == "hook_block"]
    assert len(blocks) >= 4
    assert blocks[-1].detail["path"] == "justfile"
    assert blocks[-1].item == 4 and blocks[-1].session == "sess-1"


def test_protect_tests_ignores_other_tools_and_outside_paths(project, tmp_path):
    other = _run_hook(
        "protect_tests.py", {**_edit(project, "tests/test_existing.py"), "tool_name": "Read"}
    )
    assert other.returncode == 0

    outside = _run_hook("protect_tests.py", _edit(project, str(tmp_path / "elsewhere.py")))
    assert outside.returncode == 0

    garbage = subprocess.run(
        [sys.executable, str(HOOKS / "protect_tests.py")],
        input="not json",
        capture_output=True,
        text=True,
    )
    assert garbage.returncode == 0


def test_register_and_stop_hooks_write_records(project):
    payload = {
        "session_id": "sess-9",
        "cwd": str(project),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    started = _run_hook(
        "register_session.py", payload, env={"AGENTOPS_ITEM": "7", "AGENTOPS_MODE": "headless"}
    )
    assert started.returncode == 0
    assert "demo#7" in started.stdout

    stopped = _run_hook(
        "stop_report.py", {"session_id": "sess-9", "cwd": str(project)}, env={"AGENTOPS_ITEM": "7"}
    )
    assert stopped.returncode == 0
    still_running = subprocess.run(
        [str(AO_BIN), "session", "list", "--json"], capture_output=True, text=True, env=os.environ
    )
    assert [s["session_id"] for s in json.loads(still_running.stdout)] == ["sess-9"]  # Stop 不释放
    ended = _run_hook(
        "session_end.py",
        {"session_id": "sess-9", "cwd": str(project), "reason": "exit"},
        env={"AGENTOPS_ITEM": "7"},
    )
    assert ended.returncode == 0

    out = subprocess.run(
        [str(AO_BIN), "session", "list", "--all", "--json"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    sessions = json.loads(out.stdout)
    assert sessions[0]["session_id"] == "sess-9" and sessions[0]["item"] == 7
    assert sessions[0]["mode"] == "headless"
    assert sessions[0]["status"] == "ended"  # SessionEnd hook 标记了结束
    types = [e.type for e in events.read_events()]
    assert "session_start" in types and "session_stop" in types


def test_hooks_do_not_need_shutil_which_ao_when_missing(project, tmp_path):
    """ao 不可用时 hook 仍要给出判定，不能崩。"""
    env = {"AGENTOPS_AO": str(tmp_path / "missing-ao")}
    blocked = _run_hook("protect_tests.py", _edit(project, "AGENTS.md"), env=env)
    assert blocked.returncode == 2
    assert "警告" in blocked.stderr
    assert shutil.which("git")


def test_protect_tests_denies_when_default_branch_is_unreadable(agentops_home, tmp_path):
    """审稿意见 S2：查不到 main 时不能当成"新文件"放行。"""
    path = tmp_path / "nomain"
    assert cli.main(["repo", "init", str(path), "--name", "nomain"]) == 0
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@localhost")
    _git(path, "checkout", "-q", "-b", "feature")  # 只有 feature 分支，没有 main
    (path / "tests").mkdir()
    (path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    _git(path, "add", "-A")
    assert _git(path, "commit", "-q", "-m", "init on feature").returncode == 0

    result = _run_hook("protect_tests.py", _edit(path, "tests/test_x.py"))

    assert result.returncode == 2
    assert "无法确认" in result.stderr


def test_session_can_only_update_its_own_branch(project):
    """带 AGENTOPS_BRANCH 的会话不能更新别的分支；自己的分支正常提交。"""
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    _git(project, "branch", "feat/2-other", head)
    _git(project, "checkout", "-q", "-b", "feat/1-mine")
    (project / "src" / "mod.py").write_text("X = 9\n")
    _git(project, "add", "-A")
    env = {"AGENTOPS_BRANCH": "feat/1-mine"}

    assert _git(project, "commit", "-q", "-m", "mine", env=env).returncode == 0
    mine = _git(project, "rev-parse", "HEAD").stdout.strip()

    denied = _git(project, "update-ref", "refs/heads/feat/2-other", mine, env=env)
    assert denied.returncode != 0 and "自己的分支" in denied.stderr
    assert _git(project, "rev-parse", "feat/2-other").stdout.strip() == head
    blocks = [e for e in events.read_events() if e.type == "main_protect_block"]
    assert blocks[-1].detail["rule"] == "own-branch"

    # 没有 AGENTOPS_BRANCH 的人（或 ao start 建分支时）不受规则 2 限制
    assert _git(project, "update-ref", "refs/heads/feat/2-other", mine).returncode == 0


def test_hooks_do_not_duplicate_events_for_ao_managed_sessions(project):
    env = {"AGENTOPS_ITEM": "7", "AGENTOPS_MODE": "headless", "AGENTOPS_SESSION": "sess-managed"}
    payload = {"session_id": "sess-managed", "cwd": str(project), "source": "startup"}
    assert _run_hook("register_session.py", payload, env=env).returncode == 0
    assert (
        _run_hook(
            "stop_report.py", {"session_id": "sess-managed", "cwd": str(project)}, env=env
        ).returncode
        == 0
    )

    assert (
        _run_hook(
            "session_end.py", {"session_id": "sess-managed", "cwd": str(project)}, env=env
        ).returncode
        == 0
    )
    types = [e.type for e in events.read_events() if e.session == "sess-managed"]
    assert "session_start" not in types and "session_end" not in types
    assert "session_stop" in types and "session_registered" in types and "session_exit" in types
