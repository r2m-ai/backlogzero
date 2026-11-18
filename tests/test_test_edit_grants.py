"""工作项的 allow_test_edits 授权清单：hook 只放行清单内的已有测试，提示里带上清单。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentops import cli, start
from agentops import workitems as wi

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
AO = Path(sys.executable).parent / "ao"
pytestmark = pytest.mark.skipif(not AO.exists(), reason="需要 uv sync 装好项目自己的 ao")
GRANTED = "tests/test_steward.py"
OTHER = "tests/test_other.py"


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "AGENTOPS_STEWARD": "1"},
    )


@pytest.fixture
def project(agentops_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_AO", str(AO))
    for name in ("AGENTOPS_RUNTIME", "AGENTOPS_SESSION", "AGENTOPS_ITEM", "AGENTOPS_MODE"):
        monkeypatch.delenv(name, raising=False)
    path = (tmp_path / "demo").resolve()
    assert cli.main(["repo", "init", str(path), "--name", "demo"]) == 0
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@localhost")
    (path / "tests").mkdir()
    for name in (GRANTED, OTHER):
        (path / name).write_text("def test_a():\n    assert True\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _item(grants=None) -> wi.WorkItem:
    item = wi.create(repo="demo", title="改行为", acceptance=["x"])
    if grants is not None:
        item = wi.apply(item, allow_test_edits=grants)
    return item


def _edit(project: Path, item_id: int, rel: str) -> subprocess.CompletedProcess:
    payload = {
        "session_id": "s1",
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(project / rel)},
    }
    env = {**os.environ, "AGENTOPS_ITEM": str(item_id), "AGENTOPS_AO": str(AO)}
    return subprocess.run(
        [sys.executable, str(HOOKS / "protect_tests.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_hook_allows_existing_test_listed_in_grants(project):
    item = _item([GRANTED])

    result = _edit(project, item.id, GRANTED)

    assert result.returncode == 0, result.stderr


def test_hook_blocks_unlisted_existing_test_and_says_not_in_grants(project):
    item = _item([GRANTED])

    result = _edit(project, item.id, OTHER)

    assert result.returncode == 2
    assert OTHER in result.stderr and "不在工作项的授权清单内" in result.stderr


def test_hook_grants_do_not_cover_protected_paths(project):
    item = _item([GRANTED, "justfile"])

    assert _edit(project, item.id, "justfile").returncode == 2


def test_hook_without_grants_behaves_as_before(project):
    item = _item()

    result = _edit(project, item.id, GRANTED)

    assert result.returncode == 2
    assert "只能新增" in result.stderr and "授权清单" not in result.stderr


def test_prompt_lists_grants_and_states_only_listed_files_may_change(project):
    item = _item([GRANTED])

    prompt = start.build_prompt(wi.load("demo", item.id), project, "feat/x")

    assert GRANTED in prompt and "只有清单内的文件可改" in prompt


def test_prompt_is_unchanged_without_grants(project):
    item = _item()

    prompt = start.build_prompt(wi.load("demo", item.id), project, "feat/x")

    assert "授权" not in prompt


def test_cli_set_accepts_list_and_rejects_non_list(project):
    item = _item()
    ref = f"demo#{item.id}"

    assert cli.main(["wi", "set", ref, "--kv", f'allow_test_edits=["{GRANTED}"]']) == 0
    assert wi.load("demo", item.id).extra["allow_test_edits"] == [GRANTED]
    assert cli.main(["wi", "set", ref, "--kv", "allow_test_edits=tests/x.py"]) != 0
