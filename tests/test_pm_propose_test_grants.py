"""PM 建议里可选的 allow_test_edits：建项时校验，原样写进工作项（接 #16）。"""

from __future__ import annotations

import pytest
from test_pm_propose import _calls, _proposal, _structured, world  # noqa: F401

from agentops import pm_propose, start
from agentops import workitems as wi

GOOD = "tests/test_engine.py"


def _grant(path=GOOD, reason="规格推翻了旧断言"):
    return {"path": path, "reason": reason}


@pytest.fixture
def tests_dir(world):  # noqa: F811
    (world.main / "tests").mkdir(exist_ok=True)
    (world.main / "tests" / "test_engine.py").write_text("def test_a():\n    pass\n")
    (world.main / "src_file.py").write_text("x = 1\n")
    return world


def _run(world, monkeypatch, *proposals):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_MODE", _structured(list(proposals)))
    return pm_propose.propose(world.config, "demo")


def test_schema_field_is_optional():
    item_schema = pm_propose.PROPOSAL_SCHEMA["properties"]["proposals"]["items"]
    assert "allow_test_edits" in item_schema["properties"]
    assert "allow_test_edits" not in item_schema["required"]


def test_proposal_without_field_creates_item_without_grants(tests_dir, monkeypatch):
    report = _run(tests_dir, monkeypatch, _proposal())

    item = wi.load("demo", report.created[0])
    assert "allow_test_edits" not in item.extra
    assert start.test_edit_grants(item) == ()


def test_proposal_with_field_passes_grants_through_to_work_item(tests_dir, monkeypatch):
    (tests_dir.main / "tests" / "test_b.py").write_text("")
    grants = [_grant(), _grant("tests/test_b.py", "另一条原因")]

    report = _run(tests_dir, monkeypatch, _proposal(allow_test_edits=grants))

    item = wi.load("demo", report.created[0])
    assert report.rejected == ()
    assert start.test_edit_grants(item) == (GOOD, "tests/test_b.py")
    assert item.extra["allow_test_edits"] == [GOOD, "tests/test_b.py"]
    assert "规格推翻了旧断言" in item.body and "另一条原因" in item.body


def test_prompt_explains_when_to_fill_the_field(tests_dir, monkeypatch):
    _run(tests_dir, monkeypatch, _proposal())

    prompt = _calls(tests_dir)[-1]["prompt"]
    assert "allow_test_edits" in prompt and "拿不准就不填" in prompt


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("tests/test_missing.py", "不存在"),
        ("/etc/passwd", "绝对路径"),
        ("tests/../src_file.py", ".."),
        ("src_file.py", "tests/"),
        ("", "为空"),
    ],
)
def test_invalid_grant_path_rejects_proposal_with_clear_error(tests_dir, monkeypatch, path, needle):
    report = _run(tests_dir, monkeypatch, _proposal(allow_test_edits=[_grant(path)]))

    assert report.created == ()
    assert report.rejected == ("分位组合",)
    assert wi.list_items("demo") == ()
    err = pm_propose._validate(_proposal(allow_test_edits=[_grant(path)]), [], tests_dir.main)[1]
    assert needle in err and path in err


def test_directory_is_not_accepted_as_grant(tests_dir):
    err = pm_propose._validate(_proposal(allow_test_edits=[_grant("tests")]), [], tests_dir.main)[1]
    assert "tests" in err
