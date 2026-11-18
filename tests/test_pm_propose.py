"""PM 提案模式：夜间由 PM 提出 backlog 工作项，人在看板采纳（ready）或放弃。用假的 claude。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentops import board, cli, events, llm, pm_propose, scheduled
from agentops import config as config_mod
from agentops import workitems as wi
from agentops.errors import AgentopsError

FAKE = f"{sys.executable} {Path(__file__).parent / 'fake_claude.py'}"
STEWARD = {"AGENTOPS_STEWARD": "1"}
CONTEXT = "# 研究日志\n\n待解决：分位组合、流动性分层。\n"
JUSTFILE = (
    'set shell := ["bash", "-euo", "pipefail", "-c"]\n\n'
    "nightly:\n    echo nightly-ran\n\ntest-all:\n    echo ok\n"
)


def _proposal(title="分位组合", **overrides):
    base = {
        "title": title,
        "type": "feat",
        "module": "research",
        "priority": "P1",
        "depends_on": [],
        "acceptance": ["引擎支持 quantile", "just test-all 通过"],
        "body": "背景与要做的。",
        "reason": "研究日志第一条待解决问题",
    }
    return {**base, **overrides}


def _structured(proposals, notes="ok"):
    return "structured=" + json.dumps({"proposals": proposals, "notes": notes}, ensure_ascii=False)


def _config_text(main: Path, *, propose: bool = True) -> str:
    lines = ["[repos]", f'demo = "{main}"', "", "[steward]", "auto_dispatch = false", ""]
    if propose:
        lines += [
            "[pm]",
            "propose = true",
            "max_proposals = 2",
            "max_open = 2",
            'context_files = ["docs/research-log.md", "docs/missing.md"]',
            "",
        ]
    return "\n".join(lines)


@pytest.fixture
def world(agentops_home, tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(llm.ENV_CLAUDE_CMD, FAKE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", _structured([_proposal()]))
    main = tmp_path / "demo"
    assert cli.main(["repo", "init", str(main), "--name", "demo"]) == 0
    (main / "justfile").write_text(JUSTFILE)
    (main / "docs").mkdir()
    (main / "docs" / "research-log.md").write_text(CONTEXT)
    for args in (
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("add", "-A"),
    ):
        subprocess.run(["git", "-C", str(main), *args], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "-m", "base"],
        check=True,
        env={**os.environ, **STEWARD},
    )
    config_path = agentops_home / "config.toml"
    config_path.write_text(_config_text(main))
    return SimpleNamespace(
        main=main,
        config=config_mod.load_config(),
        config_path=config_path,
        log=tmp_path / "fake.log",
    )


def _calls(world):
    if not world.log.exists():
        return []
    return [json.loads(line) for line in world.log.read_text().splitlines() if line.strip()]


def _types():
    return [e.type for e in events.read_events()]


def _landed(title: str, last_result: str) -> wi.WorkItem:
    item = wi.create(repo="demo", title=title, acceptance=("a",))
    for status in ("ready", "in_progress", "queued", "testing", "landed"):
        item = wi.apply(item, status=status)
    events.emit("landed", repo="demo", item=item.id, detail={"landed_sha": "abc"})
    return wi.apply(item, last_result=last_result)


def test_config_pm_section_has_defaults_and_accepts_overrides(world):
    # Arrange：默认不开提案；world 的配置开了并改了上限
    defaults = config_mod.Config()

    # Assert
    assert defaults.pm["propose"] is False
    assert defaults.pm["max_proposals"] == 2 and defaults.pm["max_open"] == 3
    assert tuple(defaults.pm["context_files"]) == ("docs/research-log.md",)
    assert world.config.pm["propose"] is True and world.config.pm["max_open"] == 2
    assert list(world.config.pm["context_files"]) == ["docs/research-log.md", "docs/missing.md"]


def test_propose_creates_backlog_items_tagged_as_pm_proposals(world, monkeypatch):
    # Arrange：一个已合入且留下结果摘录的工作项；LLM 给一条合法、一条类型非法的建议
    _landed("已合入的", "真实数据上 CAGR 32%")
    monkeypatch.setenv(
        "FAKE_CLAUDE_MODE", _structured([_proposal(), _proposal("类型不对", type="epic")])
    )

    # Act
    report = pm_propose.propose(world.config, "demo")

    # Assert：只建了合法那条，是 backlog、带 PM 标记；提示词含研究日志、合入结果摘录，且标为数据
    assert report.created == (2,)
    assert report.rejected == ("类型不对",)
    assert report.context_files == ("docs/research-log.md",)
    item = wi.load("demo", 2)
    assert item.status == "backlog" and item.trigger == "auto" and item.module == "research"
    assert item.extra["proposed_by"] == "pm"
    assert item.extra["pm_reason"] == "研究日志第一条待解决问题"
    assert item.acceptance == ("引擎支持 quantile", "just test-all 通过")
    assert "PM 提案" in item.body and "研究日志第一条待解决问题" in item.body
    assert "pm_proposed" in _types()
    call = _calls(world)[-1]
    assert CONTEXT.strip() in call["prompt"] and "<untrusted>" in call["prompt"]
    assert "CAGR 32%" in call["prompt"]
    assert "--effort" in call["args"]


def test_propose_skips_duplicate_titles_and_stops_at_open_cap(world, monkeypatch):
    # Arrange：已有同名工作项；LLM 给三条，max_proposals=2、max_open=2
    wi.create(repo="demo", title="分位组合", acceptance=("a",))
    monkeypatch.setenv(
        "FAKE_CLAUDE_MODE",
        _structured([_proposal("分位组合"), _proposal("流动性分层"), _proposal("第三条")]),
    )

    # Act
    first = pm_propose.propose(world.config, "demo")
    calls_after_first = len(_calls(world))
    second = pm_propose.propose(world.config, "demo")

    # Assert：重名被拒，另两条创建；待决定满 2 条后第二次不再调用 LLM
    assert [wi.load("demo", i).title for i in first.created] == ["流动性分层", "第三条"]
    assert first.rejected == ("分位组合",)
    assert second.created == () and "上限 2" in second.skipped_reason
    assert len(_calls(world)) == calls_after_first
    assert "pm_propose_skipped" in _types()
    assert len(pm_propose.open_proposals("demo")) == 2


def test_propose_survives_llm_failure(world, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")

    report = pm_propose.propose(world.config, "demo")

    assert report.created == () and "LLM" in report.skipped_reason
    assert "pm_propose_error" in _types()


def test_adopt_moves_a_proposal_to_ready_and_rejects_other_items(world):
    # Arrange
    item_id = pm_propose.propose(world.config, "demo").created[0]
    plain = wi.create(repo="demo", title="人写的", acceptance=("a",))

    # Act
    adopted = pm_propose.adopt("demo", item_id, by="test")

    # Assert
    assert adopted.status == "ready" and adopted.extra["adopted_at"]
    assert "proposal_adopted" in _types()
    assert pm_propose.open_proposals("demo") == ()
    with pytest.raises(AgentopsError):
        pm_propose.adopt("demo", plain.id, by="test")
    with pytest.raises(AgentopsError):
        pm_propose.adopt("demo", item_id, by="test")  # 已经 ready，不能再采纳


def test_board_lists_proposals_and_adopt_button_moves_to_ready(world):
    # Arrange：一条待决定的建议，看板页面上有采纳按钮
    item_id = pm_propose.propose(world.config, "demo").created[0]
    state = board.BoardState(world.config, 8999)
    page = board.render_page(state)
    assert "PM 建议" in page and 'action="/adopt"' in page and "研究日志第一条待决定" not in page
    assert "研究日志第一条待解决问题" in page
    headers = {"Host": state.host_header, "Origin": state.origin}
    form = {"token": state.token, "ref": f"demo#{item_id}"}

    # Act
    status, message = board.handle_action(state, "/adopt", form, headers)

    # Assert：ready、消息、建议面板里不再有它
    assert status == 200 and "采纳" in message
    assert wi.load("demo", item_id).status == "ready"
    assert 'action="/adopt"' not in board.render_page(state)
    # 令牌不对要拒绝
    status, _ = board.handle_action(state, "/adopt", {**form, "token": "bad"}, headers)
    assert status == 403


def test_nightly_proposes_when_enabled_and_brief_lists_them(world):
    results = scheduled.nightly(world.config, ["demo"], dispatch=False)

    assert results["demo"]["proposals"] == [1]
    brief = Path(str(results["brief"])).read_text()
    assert "PM 建议" in brief and "分位组合" in brief
    assert wi.load("demo", 1).status == "backlog"  # 建议不会被夜间派发


def test_nightly_skips_propose_when_disabled(world):
    world.config_path.write_text(_config_text(world.main, propose=False))
    config = config_mod.load_config()

    results = scheduled.nightly(config, ["demo"], dispatch=False)

    assert "proposals" not in results["demo"]
    assert wi.list_items("demo") == ()
