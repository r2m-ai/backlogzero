"""阶段 3：合并队列和 steward 的 7 个场景，加克隆、队列、策略、审批。"""

import os
import subprocess
from types import SimpleNamespace

import pytest
from sandboxing import SKIP_REASON, inside_sandbox, steward_sandbox_text

from agentops import approvals, cli, clones, events, land, policy, queue, repo, steward
from agentops import config as config_mod
from agentops import workitems as wi
from agentops.errors import AgentopsError

PIPELINE = """[repo]
name = "demo"
default_branch = "main"
autonomy = "auto"
test_cmd = "sh run-tests.sh"
max_sessions = 2

[protected]
paths = ["AGENTS.md", "CLAUDE.md", ".pipeline.toml", "justfile", ".claude/", "config/"]
existing_only = ["tests/", "fixtures/"]

[rules]
deny_delete_under = ["src/"]
deny_public_function_removal = true
"""
RUN_TESTS = "#!/bin/sh\nif [ -f .fail ]; then echo 'tests failed'; exit 1; fi\necho 'tests ok'\n"
MOD = "def public():\n    return 1\n\n\ndef _private():\n    return 2\n"
STEWARD = {"AGENTOPS_STEWARD": "1"}


def _git(path, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def world(agentops_home, tmp_path, monkeypatch):
    """主仓库 demo（装了钩子）、临时克隆和集成目录、指向它们的 config。"""
    for name in approvals.SESSION_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    main = tmp_path / "demo"
    assert cli.main(["repo", "init", str(main), "--name", "demo"]) == 0
    (main / ".pipeline.toml").write_text(PIPELINE)
    (main / "run-tests.sh").write_text(RUN_TESTS)
    (main / "src").mkdir()
    (main / "src" / "mod.py").write_text(MOD)
    (main / "tests").mkdir()
    (main / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    _git(main, "config", "user.name", "t")
    _git(main, "config", "user.email", "t@localhost")
    _git(main, "add", "-A")
    assert _git(main, "commit", "-q", "-m", "base", env=STEWARD).returncode == 0
    (agentops_home / "config.toml").write_text(
        f'[repos]\ndemo = "{main}"\n\n[workspace]\nclones = "{tmp_path / "clones"}"\n'
        f'integration = "{tmp_path / "wt"}"\n\n[steward]\npoll_seconds = 1\n'
        "test_timeout_minutes = 1\nmax_returns = 2\njudge = false\nauto_resume = false\n"
        + steward_sandbox_text()
    )
    config = config_mod.load_config()
    return SimpleNamespace(main=main, config=config, tmp=tmp_path)


def _start(world, title, module="core"):
    item = wi.create(repo="demo", title=title, module=module, acceptance=("a",))
    item = wi.apply(wi.apply(item, status="ready"), status="in_progress")
    info = clones.create_clone(world.config, item)
    return wi.load("demo", item.id), info.path


def _commit(path, message, files):
    for name, content in files.items():
        target = path / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    _git(path, "add", "-A")
    result = _git(path, "commit", "-q", "-m", message)
    assert result.returncode == 0, result.stderr
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _run(world, clone, session="s1"):
    land.land(world.config, cwd=clone, session=session)
    outcomes = steward.run_once(world.config, "demo")
    assert len(outcomes) == 1
    return outcomes[0]


def _types():
    return [e.type for e in events.read_events()]


def _main_log(world):
    return _git(world.main, "log", "--oneline", "main").stdout


def test_scenario_1_clean_land(world):
    item, clone = _start(world, "clean feature")
    sha = _commit(clone, "feat: add thing", {"src/new.py": "x = 1\n"})

    outcome = _run(world, clone)

    assert outcome.result == "landed", outcome
    assert "feat: add thing" in _main_log(world)
    assert wi.load("demo", item.id).status == "landed"
    assert not clone.exists()
    types = _types()
    for expected in (
        "land_requested",
        "integration_start",
        "tests_start",
        "tests_pass",
        "landed",
        "clone_removed",
    ):
        assert expected in types, expected
    assert queue.latest("demo")[outcome.request.id].status == "landed"
    assert outcome.request.sha == sha


def test_scenario_2_rebase_conflict_is_returned(world):
    a, clone_a = _start(world, "a")
    b, clone_b = _start(world, "b", module="other")
    _commit(clone_a, "a changes mod", {"src/mod.py": MOD.replace("return 1", "return 10")})
    _commit(clone_b, "b changes mod", {"src/mod.py": MOD.replace("return 1", "return 20")})

    assert _run(world, clone_a).result == "landed"
    outcome = _run(world, clone_b, session="s2")

    assert outcome.result == "returned"
    assert "rebase_conflict" in _types() and "returned" in _types()
    loaded = wi.load("demo", b.id)
    assert loaded.status == "returned" and "冲突" in str(loaded.extra["hint"])
    assert clone_b.exists()
    assert "b changes mod" not in _main_log(world)


def test_scenario_3_test_failure_is_returned(world):
    item, clone = _start(world, "broken")
    _commit(clone, "break tests", {".fail": "1\n"})

    outcome = _run(world, clone)

    assert outcome.result == "returned"
    assert "tests_fail" in _types()
    loaded = wi.load("demo", item.id)
    assert loaded.status == "returned" and "tests failed" in str(loaded.extra["hint"])
    assert "break tests" not in _main_log(world)


def test_scenario_4_protected_path_needs_human(world):
    item, clone = _start(world, "touch agents")
    sha = _commit(clone, "edit AGENTS", {"AGENTS.md": "# changed\n"})

    outcome = _run(world, clone)

    assert outcome.result == "needs_human"
    triggered = [e for e in events.read_events() if e.type == "rule_triggered"][-1]
    assert "protected_path" in triggered.detail["rules"]
    loaded = wi.load("demo", item.id)
    assert loaded.status == "needs_human" and loaded.extra["approval_sha"] == sha
    assert "edit AGENTS" not in _main_log(world)
    assert "notification" in _types()


def test_scenario_5_approval_then_land(world):
    item, clone = _start(world, "touch agents")
    sha = _commit(clone, "edit AGENTS", {"AGENTS.md": "# changed\n"})
    assert _run(world, clone).result == "needs_human"

    approvals.record("demo", item.id, sha[:7], by="terminal", note="ok")
    outcomes = steward.run_once(world.config, "demo")

    assert outcomes and outcomes[0].result == "landed"
    types = _types()
    assert (
        types.index("approved")
        < types.index("requeued_after_approval")
        < types.index("approval_used")
        < types.index("landed")
    )
    assert wi.load("demo", item.id).status == "landed"
    assert "edit AGENTS" in _main_log(world)


def test_scenario_6_new_commit_after_land_is_requeued(world):
    item, clone = _start(world, "late commit")
    _commit(clone, "first", {"src/new.py": "x = 1\n"})
    land.land(world.config, cwd=clone, session="s1")
    second = _commit(clone, "second", {"src/new.py": "x = 2\n"})

    first_pass = steward.run_once(world.config, "demo")
    assert first_pass[0].result == "requeued"
    assert "requeued" in _types()
    assert [r.sha for r in queue.pending("demo")] == [second]

    second_pass = steward.run_once(world.config, "demo")
    assert second_pass[0].result == "landed"
    log = _main_log(world)
    assert "first" in log and "second" in log
    assert wi.load("demo", item.id).status == "landed"


def test_scenario_7_bash_edited_existing_test_is_caught_by_steward(world):
    """第二道防线：hook 拦不住 Bash 改测试，steward 合并时按 diff 兜底。"""
    item, clone = _start(world, "sneaky")
    _commit(
        clone, "weaken test", {"tests/test_a.py": "def test_a():\n    assert 1 == 1  # edited\n"}
    )

    outcome = _run(world, clone)

    assert outcome.result == "needs_human"
    triggered = [e for e in events.read_events() if e.type == "rule_triggered"][-1]
    assert "existing_test_modified" in triggered.detail["rules"]
    assert "weaken test" not in _main_log(world)


def test_scenario_7b_new_test_file_is_fine(world):
    item, clone = _start(world, "adds test")
    _commit(clone, "add test", {"tests/test_new.py": "def test_new():\n    assert True\n"})

    assert _run(world, clone).result == "landed"


def test_deleted_source_and_removed_public_function_trigger_rules(world):
    a, clone_a = _start(world, "deletes")
    _commit(clone_a, "delete module", {"src/mod.py": None})
    assert _run(world, clone_a).result == "needs_human"
    assert (
        "deleted_source"
        in [e for e in events.read_events() if e.type == "rule_triggered"][-1].detail["rules"]
    )

    b, clone_b = _start(world, "removes function", module="other")
    _commit(clone_b, "drop public", {"src/mod.py": "def _private():\n    return 2\n"})
    assert _run(world, clone_b, session="s2").result == "needs_human"
    last = [e for e in events.read_events() if e.type == "rule_triggered"][-1]
    assert "public_function_removed" in last.detail["rules"]


def test_max_returns_escalates_to_needs_human(world):
    item, clone = _start(world, "keeps failing")
    _commit(clone, "break", {".fail": "1\n"})

    assert _run(world, clone).result == "returned"
    assert _run(world, clone).result == "returned"
    outcome = _run(world, clone)

    assert outcome.result == "needs_human"
    loaded = wi.load("demo", item.id)
    assert loaded.status == "needs_human" and loaded.extra["returns"] == 3


def test_approve_refuses_inside_claude_session(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    with pytest.raises(AgentopsError):
        approvals.record("demo", 1, "abcdef1", by="terminal")


def test_land_rejects_dirty_clone_and_non_clone_dir(world):
    item, clone = _start(world, "dirty")
    (clone / "src" / "x.py").write_text("x\n")

    with pytest.raises(AgentopsError):
        land.land(world.config, cwd=clone, session="s")
    with pytest.raises(AgentopsError):
        land.land(world.config, cwd=world.main, session="s")


def test_untracked_file_in_main_does_not_block_fast_forward(world):
    """人在主仓库里放着未跟踪的草稿（git 快进不会碰它）不应挡住合入。"""
    item, clone = _start(world, "scratch in main")
    _commit(clone, "feat: add thing", {"src/new.py": "x = 1\n"})
    scratch = world.main / "draft-notes.md"
    scratch.write_text("人的草稿，还没提交\n")

    outcome = _run(world, clone)

    assert outcome.result == "landed", outcome
    assert scratch.read_text() == "人的草稿，还没提交\n"
    assert wi.load("demo", item.id).status == "landed"


def test_session_settings_deny_main_repo_and_other_clones(world):
    a, clone_a = _start(world, "a")
    b, clone_b = _start(world, "b", module="other")

    rendered = repo.render_session_settings(
        "demo", clone_a, main_repo=world.main, other_clones=clones.list_clones(world.config, "demo")
    )

    import json

    deny = json.loads(rendered)["sandbox"]["filesystem"]["denyWrite"]
    assert str(world.main.resolve()) in deny
    assert str(clone_b.resolve()) in deny
    assert str(clone_a.resolve()) not in deny


def test_policy_parse_defaults_and_prepare_cmd():
    parsed = policy.parse_policy(
        '[repo]\nname = "x"\nprepare_cmd = "uv sync --offline"\n', "fallback"
    )

    assert parsed.repo == "x" and parsed.prepare_cmd == "uv sync --offline"
    assert parsed.test_cmd == "just test-all" and "tests/" in parsed.existing_only
    assert policy.parse_policy("", "fb").repo == "fb"


def test_queue_pending_is_fifo_and_requeue_goes_last(agentops_home):
    first = queue.new_request(
        repo="r", item=1, branch="b1", sha="a" * 40, clone_path="/x", session=None
    )
    second = queue.new_request(
        repo="r", item=2, branch="b2", sha="b" * 40, clone_path="/y", session=None
    )
    queue.append("r", first, "queued")
    queue.append("r", second, "queued")
    assert [r.item for r in queue.pending("r")] == [1, 2]

    queue.append("r", first, "requeued")
    assert [r.item for r in queue.pending("r")] == [2, 1]

    queue.append("r", second, "landed")
    assert [r.item for r in queue.pending("r")] == [1]


# ---- 阶段 3 评审（docs/reviews/phase-3.md）的回归测试 ----


@pytest.mark.skipif(inside_sandbox(), reason=SKIP_REASON)
def test_r1_candidate_test_cannot_write_approval_or_main(world):
    """R1 + 候选改动 C1：候选分支里的测试想写审批目录、主仓库、克隆根，全部失败；
    也不能借 AGENTOPS_STEWARD 更新主仓库的 main。"""
    from agentops.paths import approvals_dir

    item, clone = _start(world, "evil tests")
    approval = approvals_dir() / f"demo-{item.id}-forged.json"
    script = (
        "#!/bin/sh\n"
        f"touch '{approval}' 2>/dev/null && echo wrote-approval\n"
        f"touch '{world.main}/evil.txt' 2>/dev/null && echo wrote-main\n"
        f"touch '{world.config.clones_root()}/evil.txt' 2>/dev/null && echo wrote-clones\n"
        f'[ -n "$AGENTOPS_STEWARD" ] && echo has-steward-env\n'
        f"git -C '{world.main}' update-ref refs/heads/main HEAD~0 2>/dev/null && echo moved-main\n"
        "echo tests ok\nexit 0\n"
    )
    _commit(clone, "evil tests", {"run-tests.sh": script, "src/new.py": "x = 1\n"})

    outcome = _run(world, clone)

    assert outcome.result == "landed", outcome.reason  # 测试本身退出 0，但越权写入都没发生
    assert not approval.exists()
    assert not (world.main / "evil.txt").exists()
    assert not (world.config.clones_root() / "evil.txt").exists()
    from agentops.paths import logs_dir

    log = "\n".join(p.read_text() for p in logs_dir("demo").glob("*.log"))
    assert "wrote-approval" not in log and "wrote-main" not in log and "wrote-clones" not in log
    assert "has-steward-env" not in log and "moved-main" not in log


def test_r2_processing_request_is_recovered_after_crash(world):
    item, clone = _start(world, "crash")
    _commit(clone, "feat", {"src/new.py": "x = 1\n"})
    land.land(world.config, cwd=clone, session="s1")
    steward.process_repo  # noqa: B018 - 仅说明下面模拟的是它中途崩溃
    queue.ingest_inbox("demo")
    request = queue.pending("demo")[0]
    queue.append("demo", request, "processing")  # 模拟：写了 processing 就崩了
    wi.apply(wi.load("demo", item.id), status="queued")
    wi.apply(wi.load("demo", item.id), status="testing")

    outcomes = steward.run_once(world.config, "demo")

    assert outcomes and outcomes[0].result == "landed"
    assert "recovered" in _types()
    assert wi.load("demo", item.id).status == "landed"


def test_r2_crash_after_fast_forward_still_records_landed(world):
    """main 已快进但没来得及记 landed：重跑得到空 diff，仍正常记 landed。"""
    item, clone = _start(world, "ff then crash")
    sha = _commit(clone, "feat", {"src/new.py": "x = 1\n"})
    land.land(world.config, cwd=clone, session="s1")
    queue.ingest_inbox("demo")
    request = queue.pending("demo")[0]
    queue.append("demo", request, "processing")
    wi.apply(wi.load("demo", item.id), status="queued")
    assert _git(world.main, "fetch", "-q", str(clone), "HEAD").returncode == 0
    assert _git(world.main, "merge", "--ff-only", sha, env=STEWARD).returncode == 0  # 模拟已快进

    outcomes = steward.run_once(world.config, "demo")

    assert outcomes[0].result == "landed"
    assert _git(world.main, "rev-parse", "main").stdout.strip() == sha
    assert "landed" in _types()


def test_r3_requeue_after_fetch_window_returns_item_to_queued(world, monkeypatch):
    """SHA 检查通过之后、fetch 之前分支又变了：工作项要从 testing 回到 queued，下一轮能继续。"""
    item, clone = _start(world, "late late commit")
    first = _commit(clone, "first", {"src/new.py": "x = 1\n"})
    land.land(world.config, cwd=clone, session="s1")
    real_tip = steward._branch_tip
    state = {"armed": True}

    def tip_then_commit(clone_path, branch):
        tip = real_tip(clone_path, branch)
        if state["armed"]:
            state["armed"] = False
            _commit(clone, "second", {"src/new.py": "x = 2\n"})  # 在 SHA 检查和 fetch 之间追加提交
        return tip

    monkeypatch.setattr(steward, "_branch_tip", tip_then_commit)
    first_pass = steward.run_once(world.config, "demo")
    assert first_pass[0].result == "requeued"
    assert wi.load("demo", item.id).status == "queued"

    second_pass = steward.run_once(world.config, "demo")
    assert second_pass[0].result == "landed"
    assert first in _git(world.main, "log", "--format=%H", "main").stdout
    assert wi.load("demo", item.id).status == "landed"


def test_r4_replacing_existing_test_with_symlink_is_caught(world):
    item, clone = _start(world, "symlink trick")
    target = clone / "tests" / "test_a.py"
    target.unlink()
    (clone / "tests" / "real.py").write_text("def test_a():\n    assert 1 == 1\n")
    target.symlink_to("real.py")
    _git(clone, "add", "-A")
    assert _git(clone, "commit", "-q", "-m", "symlink test").returncode == 0

    outcome = _run(world, clone)

    assert outcome.result == "needs_human"
    last = [e for e in events.read_events() if e.type == "rule_triggered"][-1]
    assert "existing_test_modified" in last.detail["rules"]


def test_sandbox_profile_denies_before_allowing_subpaths(tmp_path):
    from agentops import sandbox

    text = sandbox.profile(allow_write=[tmp_path / "work"], deny_write=[tmp_path])
    assert text.index("(deny file-write*") < text.index("(allow file-write*")
    assert "(deny network*)" in text
    assert sandbox.wrap(["sh", "-c", "true"], text)[:2] == [sandbox.SANDBOX_EXEC, "-p"]
