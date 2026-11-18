"""docs/decisions.md 有就嵌入，没有就写明没有决策记录；其他必需文件缺失仍然报错。"""

import shutil
from pathlib import Path

import pytest

from agentops import config, review
from agentops.errors import AgentopsError

REAL_ROOT = Path(__file__).resolve().parent.parent
MARKER = "决策记录样例-唯一标记"


def _suite(tmp_path: Path, *, decisions: str | None, skip: str | None = None) -> Path:
    root = tmp_path / "suite"
    for rel in ("docs/plan.md", "docs/process/execute-review-improve.md", "docs/claude-cli.md"):
        if rel == skip:
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_ROOT / rel, dst)
    if decisions is not None:
        (root / "docs" / "decisions.md").write_text(decisions, encoding="utf-8")
    return root


def _prompt(tmp_path: Path, root: Path, monkeypatch) -> str:
    monkeypatch.setattr(review, "SUITE_ROOT", root)
    summary = tmp_path / "summary.md"
    summary.write_text("小结\n", encoding="utf-8")
    outcome = review.run_review(
        config.Config(), 1, repos=(), summary_path=summary, with_tests=False, dry_run=True
    )
    return outcome.prompt_file.read_text(encoding="utf-8")


def test_prompt_embeds_decisions_when_present(agentops_home, tmp_path, monkeypatch):
    root = _suite(tmp_path, decisions=f"# 决策\n\n{MARKER}\n")

    prompt = _prompt(tmp_path, root, monkeypatch)

    assert MARKER in prompt
    assert review.NO_DECISIONS not in prompt


def test_prompt_says_no_decisions_when_file_missing(agentops_home, tmp_path, monkeypatch):
    root = _suite(tmp_path, decisions=None)

    prompt = _prompt(tmp_path, root, monkeypatch)

    assert "### 4.3 决策记录" in prompt
    assert "（本仓库没有决策记录）" in prompt


def test_missing_plan_still_errors(agentops_home, tmp_path, monkeypatch):
    root = _suite(tmp_path, decisions=None, skip="docs/plan.md")

    with pytest.raises(AgentopsError):
        _prompt(tmp_path, root, monkeypatch)
