"""阶段 C：hooks 两种运行时都认。喂 Codex 形态的 stdin（apply_patch 补丁、Bash 命令、thread id），
以及 ao repo init --runtime codex 生成的 .codex/hooks.json。
Claude 形态的行为由 test_repo_and_hooks 守住。"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentops import cli, events

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
AO = Path(sys.executable).parent / "ao"  # 被测代码自己的 ao，不用装在 ~/.local/bin 的那份
ROLLOUT = "/x/codex-home/sessions/2026/11/09/rollout-2026-11-09T10-00-00-thread-1.jsonl"
pytestmark = pytest.mark.skipif(not AO.exists(), reason="需要 uv sync 装好项目自己的 ao")


def _git(path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def project(agentops_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_AO", str(AO))
    for name in ("AGENTOPS_RUNTIME", "AGENTOPS_SESSION", "AGENTOPS_ITEM", "AGENTOPS_MODE"):
        monkeypatch.delenv(name, raising=False)
    path = (tmp_path / "demo").resolve()
    assert cli.main(["repo", "init", str(path), "--name", "demo", "--runtime", "codex"]) == 0
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@localhost")
    (path / "tests").mkdir()
    (path / "tests" / "test_existing.py").write_text("def test_a():\n    assert True\n")
    (path / "src").mkdir()
    (path / "src" / "mod.py").write_text("X = 1\n")
    _git(path, "add", "-A")
    assert _git(path, "commit", "-q", "-m", "init", env={"AGENTOPS_STEWARD": "1"}).returncode == 0
    return path


def _run_hook(name: str, payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _base(project: Path, event: str, **extra: object) -> dict:
    return {
        "session_id": "thread-1",
        "cwd": str(project),
        "hook_event_name": event,
        "transcript_path": ROLLOUT,
        "model": "gpt-6.1-sol",
        "permission_mode": "bypassPermissions",
        **extra,
    }


def _patch(project: Path, *lines: str) -> dict:
    body = "\n".join(("*** Begin Patch", *lines, "*** End Patch")) + "\n"
    return _base(
        project,
        "PreToolUse",
        tool_name="apply_patch",
        tool_input={"command": body},
        turn_id="turn-1",
        tool_use_id="call-1",
    )


def _bash(project: Path, command: str, **extra: object) -> dict:
    return _base(
        project,
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": command},
        turn_id="turn-1",
        tool_use_id="call-2",
        **extra,
    )


def _marker(home: Path, project: Path, **fields: object) -> Path:
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:24]
    path = home / "sessions" / "by-clone" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _sessions(*flags: str) -> list[dict]:
    out = subprocess.run(
        [str(AO), "session", "list", *flags, "--json"], capture_output=True, text=True
    )
    return json.loads(out.stdout)


# ---- ao repo init --runtime codex ----


def test_repo_init_codex_writes_hooks_json_for_four_events(project):
    hooks = json.loads((project / ".codex" / "hooks.json").read_text())["hooks"]

    assert set(hooks) == {"PreToolUse", "SessionStart", "Stop", "SessionEnd"}
    pre = hooks["PreToolUse"][0]
    assert set(pre["matcher"].split("|")) >= {"Bash", "apply_patch", "Edit", "Write"}
    assert "protect_tests.py" in pre["hooks"][0]["command"]
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] <= 3  # Codex 对 SessionEnd 的硬上限
    assert (project / ".claude" / "settings.json").exists()  # Claude 的那份照旧生成
    assert '".codex/"' in (project / ".pipeline.toml").read_text()


def test_repo_init_default_runtime_does_not_write_codex_dir(agentops_home, tmp_path):
    path = tmp_path / "plain"

    assert cli.main(["repo", "init", str(path), "--name", "plain"]) == 0

    assert not (path / ".codex").exists()


# ---- protect_tests：apply_patch ----


def test_apply_patch_update_of_existing_test_is_blocked_with_absolute_or_relative_path(project):
    absolute = _run_hook(
        "protect_tests.py",
        _patch(project, f"*** Update File: {project}/tests/test_existing.py", "@@", "+x = 1"),
    )
    relative = _run_hook(
        "protect_tests.py",
        _patch(project, "*** Update File: tests/test_existing.py", "@@", "+x = 1"),
    )
    deleted = _run_hook(
        "protect_tests.py", _patch(project, f"*** Delete File: {project}/tests/test_existing.py")
    )

    for result in (absolute, relative, deleted):
        assert result.returncode == 2
        assert "只能新增" in result.stderr and "tests/test_existing.py" in result.stderr
    # stderr 原文会进模型上下文，要告诉它该怎么办
    assert "停下" in absolute.stderr and "ao land" in absolute.stderr


def test_apply_patch_add_new_test_and_edit_source_are_allowed(project):
    added = _run_hook(
        "protect_tests.py",
        _patch(project, f"*** Add File: {project}/tests/test_new.py", "+def test_n(): pass"),
    )
    source = _run_hook(
        "protect_tests.py",
        _patch(project, f"*** Update File: {project}/src/mod.py", "@@", "+Y = 2"),
    )
    lookalike = _run_hook(
        "protect_tests.py", _patch(project, "*** Add File: tests_other/x.py", "+x = 1")
    )
    outside = _run_hook("protect_tests.py", _patch(project, "*** Update File: /tmp/elsewhere.py"))

    assert [r.returncode for r in (added, source, lookalike, outside)] == [0, 0, 0, 0]


def test_apply_patch_checks_every_file_in_a_multi_file_patch(project):
    result = _run_hook(
        "protect_tests.py",
        _patch(
            project,
            f"*** Update File: {project}/src/mod.py",
            "@@",
            "+Y = 2",
            f"*** Add File: {project}/tests/test_two.py",
            "+def test_two(): pass",
            f"*** Update File: {project}/tests/test_existing.py",
            "@@",
            "+z = 3",
        ),
    )

    assert result.returncode == 2 and "tests/test_existing.py" in result.stderr


def test_apply_patch_blocks_protected_paths_moves_and_overwrites(project):
    hooks_json = _run_hook(
        "protect_tests.py", _patch(project, "*** Update File: .codex/hooks.json", "@@", "+{}")
    )
    new_under_protected = _run_hook(
        "protect_tests.py", _patch(project, "*** Add File: .codex/extra.json", "+{}")
    )
    moved_onto_test = _run_hook(
        "protect_tests.py",
        _patch(project, "*** Update File: src/mod.py", "*** Move to: tests/test_existing.py"),
    )
    add_over_existing = _run_hook(
        "protect_tests.py", _patch(project, "*** Add File: tests/test_existing.py", "+x = 1")
    )

    for result in (hooks_json, new_under_protected, moved_onto_test, add_over_existing):
        assert result.returncode == 2


def test_blocked_patch_writes_hook_block_event_attributed_to_ao_session(project):
    env = {"AGENTOPS_RUNTIME": "codex", "AGENTOPS_SESSION": "sess-ao", "AGENTOPS_ITEM": "4"}

    result = _run_hook(
        "protect_tests.py",
        _patch(project, "*** Update File: tests/test_existing.py", "@@", "+x = 1"),
        env=env,
    )
    block = [e for e in events.read_events() if e.type == "hook_block"][-1]

    assert result.returncode == 2
    assert block.session == "sess-ao" and block.item == 4 and block.repo == "demo"
    assert block.detail["path"] == "tests/test_existing.py"
    assert block.detail["tool"] == "apply_patch"


# ---- protect_tests：Bash（只在 Codex 下看） ----

COMMIT = "git " + "commit"  # 拆开写：整串出现在命令行里会被执行者这边的钩子误判

BLOCKED_COMMANDS = [
    "printf 'x = 1\\n' >> tests/test_existing.py",
    "echo hi > tests/test_existing.py",
    "cat src/mod.py | tee -a tests/test_existing.py",
    "sed -i '' 's/True/False/' tests/test_existing.py",
    "perl -pi -e 's/True/False/' tests/test_existing.py",
    "rm tests/test_existing.py",
    "rm -rf tests",
    "rm tests/*.py",
    "git rm -q tests/test_existing.py",
    "git checkout main -- tests/test_existing.py",
    "mv tests/test_existing.py tests/test_renamed.py",
    "cp src/mod.py tests/test_existing.py",
    "just test-fast && truncate -s 0 tests/test_existing.py",
    "echo '{}' > .codex/hooks.json",
    "cat > AGENTS.md <<'EOF'\nnew rules\nEOF",
    # 评审补的：重定向写在 heredoc 开头那一行、heredoc 之后还有命令、多行命令
    "cat <<EOF > tests/test_existing.py\nx = 1\nEOF",
    "cat <<'EOF' | tee tests/test_existing.py\nx = 1\nEOF",
    "cat <<EOF\nhello\nEOF\nrm tests/test_existing.py",
    "git status\nrm tests/test_existing.py",
    "echo '<<A'\nrm tests/test_existing.py\nA",
    "cd tests && rm test_existing.py",
    "(cd tests; echo x >> test_existing.py)",
    "bash -c 'rm tests/test_existing.py'",
    'bash -lc "cd tests && echo hi > test_existing.py"',
    "eval 'rm tests/test_existing.py'",
    "find tests -name 'test_*.py' -delete",
    "if true; then rm tests/test_existing.py; fi",
    "time -p rm tests/test_existing.py",
    "env -u FOO rm tests/test_existing.py",
    "rm tests/test_existing.py && echo 'oops",
    "cp -t tests /tmp/test_existing.py",
    "chmod -x tests/test_existing.py",
    "echo " + "x" * 70000,  # 长到没法检查的命令直接拒绝
]

ALLOWED_COMMANDS = [
    "cat tests/test_existing.py",
    "uv run --offline pytest -q tests/ 2>&1 | tail -5",
    "pytest tests/test_existing.py > /tmp/out.log",
    "rg --line-number 'def test' tests/",
    "git diff --stat -- tests/",
    f"git add -A && {COMMIT} -m 'docs: x'",
    "git checkout -b docs/7-readme",
    "cp tests/test_existing.py /tmp/copy.py",
    "cp src/mod.py tests/test_new_copy.py",
    "echo 'def test_n(): pass' > tests/test_new.py",
    "sed --quiet '1,5p' tests/test_existing.py",
    "sed -i '' 's/1/2/' src/mod.py",
    "ao land",
    "cp src/mod.py tests/",  # 往 tests/ 里放新文件是允许的
    "mv src/new_test.py tests/",
    "cd src && rm mod.py",
    "find tests -name '*.py'",
    "python - <<'PY'\nprint('rm tests/test_existing.py > x')\nPY",
    f"{COMMIT} -m 'docs: 说明\n\n正文里提到 rm tests/test_existing.py 只是文字'",
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_bash_writes_to_existing_tests_or_protected_paths_are_blocked(project, command):
    result = _run_hook("protect_tests.py", _bash(project, command))

    assert result.returncode == 2, command
    assert "protect_tests" in result.stderr


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_bash_reads_and_new_files_are_allowed(project, command):
    result = _run_hook("protect_tests.py", _bash(project, command))

    assert result.returncode == 0, (command, result.stderr)


def test_bash_is_not_inspected_for_claude_sessions(project):
    payload = _bash(
        project,
        "rm tests/test_existing.py",
        transcript_path="/Users/x/.claude/projects/p/abc.jsonl",
    )

    assert _run_hook("protect_tests.py", payload).returncode == 0


# ---- 会话 hooks ----


def test_codex_managed_session_hooks_use_ao_session_id_not_thread_id(project):
    # Arrange：ao start 起的 Codex 会话，环境变量齐全；stdin 里的 session_id 是 Codex 的 thread id
    env = {
        "AGENTOPS_RUNTIME": "codex",
        "AGENTOPS_SESSION": "sess-ao",
        "AGENTOPS_ITEM": "7",
        "AGENTOPS_MODE": "headless",
    }
    subprocess.run(
        [
            str(AO),
            "session",
            "register",
            "--session-id",
            "sess-ao",
            "--repo",
            "demo",
            "--item",
            "7",
        ],
        capture_output=True,
        check=True,
    )

    started = _run_hook(
        "register_session.py", _base(project, "SessionStart", source="startup"), env
    )
    stopped = _run_hook(
        "stop_report.py", _base(project, "Stop", last_assistant_message="done"), env
    )
    ended = _run_hook("session_end.py", _base(project, "SessionEnd", reason="other"), env)

    assert [r.returncode for r in (started, stopped, ended)] == [0, 0, 0]
    assert "demo#7" in started.stdout  # SessionStart 的纯文本 stdout 会成为模型上下文
    assert stopped.stdout == ""  # Stop 的 stdout 语义未实测，Codex 下不输出
    assert [s["session_id"] for s in _sessions("--all")] == ["sess-ao"]
    assert _sessions()[0]["status"] == "running"  # 无人值守的结束由 ao start 记
    by_type = {e.type: e for e in events.read_events() if e.session == "sess-ao"}
    assert {"session_registered", "session_stop", "session_exit"} <= set(by_type)
    assert "session_start" not in by_type and "session_end" not in by_type
    assert not [e for e in events.read_events() if e.session == "thread-1"]


def test_codex_hooks_fall_back_to_clone_marker_when_env_is_not_passed(project, agentops_home):
    # Arrange：Codex 没把 AGENTOPS_* 传给 hook 进程；ao start 写的标记文件是唯一线索
    _marker(
        agentops_home,
        project,
        session="sess-ao",
        repo="demo",
        item=7,
        mode="headless",
        runtime="codex",
    )
    bare = {"session_id": "thread-1", "cwd": str(project), "hook_event_name": "SessionStart"}

    started = _run_hook("register_session.py", {**bare, "source": "startup"})
    ended = _run_hook("session_end.py", {**bare, "hook_event_name": "SessionEnd"})
    blocked = _run_hook("protect_tests.py", _bash(project, "rm tests/test_existing.py"))

    assert started.returncode == 0 and "demo#7" in started.stdout
    assert ended.returncode == 0 and blocked.returncode == 2
    records = _sessions("--all")
    assert [s["session_id"] for s in records] == ["sess-ao"]
    assert records[0]["item"] == 7 and records[0]["mode"] == "headless"
    assert records[0]["status"] == "running"
    block = [e for e in events.read_events() if e.type == "hook_block"][-1]
    assert block.session == "sess-ao" and block.item == 7


def test_unmanaged_codex_session_is_registered_and_ended_by_thread_id(project):
    started = _run_hook("register_session.py", _base(project, "SessionStart", source="startup"))
    ended = _run_hook("session_end.py", _base(project, "SessionEnd", reason="other"))

    assert started.returncode == 0 and ended.returncode == 0
    (record,) = _sessions("--all")
    assert record["session_id"] == "thread-1" and record["status"] == "ended"
    assert "session_start" in [e.type for e in events.read_events()]


# ---- 评审后补的：检查不了就拒绝、策略取标记、运行时不被旧标记带偏 ----


def test_unresolvable_first_path_does_not_let_the_rest_of_the_patch_through(project):
    # Arrange：补丁第一项的路径会让路径解析出错；以前是整个 hook 崩溃 = 放行
    (project / "loop_a").symlink_to("loop_b")
    (project / "loop_b").symlink_to("loop_a")
    update = f"*** Update File: {project}/tests/test_existing.py"

    no_such_user = _run_hook(
        "protect_tests.py", _patch(project, "*** Add File: ~no_such_user_zz/a", "+x", update)
    )
    symlink_loop = _run_hook(
        "protect_tests.py", _patch(project, "*** Add File: loop_a/x.py", "+x", update)
    )

    assert no_such_user.returncode == 2 and "只能新增" in no_such_user.stderr
    assert symlink_loop.returncode == 2 and "解析不了" in symlink_loop.stderr


def test_unexpected_error_fails_closed_for_codex_but_not_for_claude(project, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    import protect_tests

    def boom(data):
        raise RuntimeError("bug in the guard")

    monkeypatch.setattr(protect_tests, "check", boom)
    update = _patch(project, "*** Update File: src/mod.py", "@@", "+Y = 2")
    claude_edit = {
        "session_id": "s",
        "cwd": str(project),
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/mod.py"},
        "transcript_path": "/Users/x/.claude/projects/p/abc.jsonl",
    }

    monkeypatch.setattr(protect_tests, "read_input", lambda: update)
    assert protect_tests.main() == 2  # 退出码不是 2 就等于放行，Codex 下出错要拒绝

    monkeypatch.setattr(protect_tests, "read_input", lambda: claude_edit)
    with pytest.raises(RuntimeError):  # Claude 的行为保持原样
        protect_tests.main()


def test_marker_policy_wins_over_a_tampered_pipeline_toml_and_main_ref(project, agentops_home):
    # Arrange：会话用 hook 看不出的办法把克隆里的策略文件改松，
    # 还把克隆的 main 挪到没有这个测试的提交
    base_sha = _git(project, "rev-parse", "main").stdout.strip()
    _marker(
        agentops_home,
        project,
        session="sess-ao",
        repo="demo",
        item=7,
        mode="headless",
        runtime="codex",
        base_sha=base_sha,
        policy={
            "default_branch": "main",
            "protected": ["AGENTS.md", ".pipeline.toml", ".codex/"],
            "existing_only": ["tests/"],
        },
    )
    (project / ".pipeline.toml").write_text(
        '[repo]\nname = "other-repo"\n\n[protected]\npaths = ["zzz"]\nexisting_only = ["zzz"]\n'
    )
    _git(project, "checkout", "-q", "--orphan", "empty")
    _git(project, "rm", "-rq", "--cached", ".")
    _git(project, "commit", "-q", "--allow-empty", "-m", "empty", env={"AGENTOPS_STEWARD": "1"})
    _git(project, "branch", "-f", "main", "empty", env={"AGENTOPS_STEWARD": "1"})

    test_edit = _run_hook("protect_tests.py", _bash(project, "rm tests/test_existing.py"))
    rules_edit = _run_hook(
        "protect_tests.py", _patch(project, "*** Update File: AGENTS.md", "@@", "+x")
    )
    block = [e for e in events.read_events() if e.type == "hook_block"][-1]

    assert test_edit.returncode == 2 and rules_edit.returncode == 2
    assert block.repo == "demo"  # 仓库名也取标记，不取克隆里的文件


def test_stale_codex_marker_does_not_hijack_a_claude_session(project, agentops_home):
    # Arrange：同一克隆路径上留着一份旧的 Codex 标记，现在跑的是 Claude 会话
    _marker(
        agentops_home,
        project,
        session="sess-old",
        repo="demo",
        item=7,
        mode="headless",
        runtime="codex",
    )
    claude = {
        "session_id": "claude-sess",
        "cwd": str(project),
        "transcript_path": "/Users/x/.claude/projects/p/abc.jsonl",
    }

    bash = _run_hook(
        "protect_tests.py",
        {**claude, "tool_name": "Bash", "tool_input": {"command": "rm tests/test_existing.py"}},
    )
    started = _run_hook("register_session.py", {**claude, "source": "startup"})

    assert bash.returncode == 0  # Claude 下 Bash 不归这个 hook 管
    assert started.returncode == 0
    assert [s["session_id"] for s in _sessions("--all")] == ["claude-sess"]


def test_hook_finds_the_clone_root_when_cwd_is_a_subdirectory(project, agentops_home):
    _marker(
        agentops_home,
        project,
        session="sess-ao",
        repo="demo",
        item=7,
        mode="headless",
        runtime="codex",
    )
    in_src = {"cwd": str(project / "src")}

    patch = _run_hook(
        "protect_tests.py",
        {**_patch(project, "*** Update File: ../tests/test_existing.py", "@@", "+x"), **in_src},
    )
    shell = _run_hook(
        "protect_tests.py", {**_bash(project, "rm ../tests/test_existing.py"), **in_src}
    )
    local = _run_hook("protect_tests.py", {**_bash(project, "rm mod.py"), **in_src})
    block = [e for e in events.read_events() if e.type == "hook_block"][-1]

    assert patch.returncode == 2 and shell.returncode == 2 and local.returncode == 0
    assert block.session == "sess-ao" and block.detail["path"] == "tests/test_existing.py"


def test_corrupt_marker_is_ignored(project, agentops_home):
    marker = _marker(agentops_home, project, session="sess-ao")
    marker.write_text("{not json")

    started = _run_hook("register_session.py", _base(project, "SessionStart", source="startup"))

    assert started.returncode == 0
    assert [s["session_id"] for s in _sessions("--all")] == ["thread-1"]


def test_repo_init_rejects_unknown_runtime(agentops_home, tmp_path):
    from agentops import repo
    from agentops.errors import AgentopsError

    with pytest.raises(AgentopsError, match="runtime"):
        repo.init_repo(tmp_path / "x", "x", runtime="gemini")
