"""ao review：计划片段提取、脱敏、标签、diff、假 codex 全流程。"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentops import cli, config, events, review, review_plan
from agentops.errors import AgentopsError

SUITE = Path(__file__).resolve().parent.parent
REAL_PLAN = (SUITE / "docs" / "plan.md").read_text(encoding="utf-8")
REAL_PROCESS = (SUITE / "docs" / "process" / "execute-review-improve.md").read_text(
    encoding="utf-8"
)

MINI_PLAN = """# 计划
版本 9.9，2026-01-01。执行者：Alice 的机器。
9.9 的改动：加了评审。

## 1. 开场指令
```
5. 不确定时先问我。
6. 不要提交个人信息。
```

## 6. 执行阶段
| 阶段 | 工时 | 交付物 | 验收 |
|---|---|---|---|
| 1 套件骨架 | 1 h | ao | 自测通过 |
| 2 业务仓库 | 2 h | quant-lab | hook 拦住 |

## 6A. 评审节点
| 阶段 | 评审方式 | 重点 |
|---|---|---|
| 1 | 轻审 | 锁 |
| 2 | 必审 | 沙箱 |

## 8. 完成标准
| ID | 标准 | 怎么验证 |
|---|---|---|
| D4 | 改测试被拦 | hook_block |
| D12 | 隐私 | 搜不到邮箱 |
"""


def test_section_and_rows_on_mini_plan():
    assert review_plan.section(MINI_PLAN, "6A").startswith("## 6A.")
    assert "| 2 业务仓库 |" in review_plan.phase_row(MINI_PLAN, 2)
    assert "| 1 套件骨架 |" not in review_plan.phase_row(MINI_PLAN, 2)
    assert "| 2 | 必审 |" in review_plan.review_row(MINI_PLAN, 2)
    rows = review_plan.d_rows(MINI_PLAN, ["D4", "D12"])
    assert "| D4 |" in rows and "| D12 |" in rows and "|---|" in rows
    assert review_plan.privacy_rule(MINI_PLAN) == "6. 不要提交个人信息。"
    assert review_plan.preamble(MINI_PLAN).startswith("# 计划")
    assert review_plan.plan_version(MINI_PLAN) == "9.9"
    assert review_plan.section(MINI_PLAN, "7") == review_plan.NOT_FOUND


def test_real_plan_parses_for_every_reviewed_phase():
    for phase in (1, 2, 3, 4, 5, 6):
        assert f"| {phase} " in review_plan.phase_row(REAL_PLAN, phase)
        assert f"| {phase} " in review_plan.review_row(REAL_PLAN, phase)
    assert "| D13 |" in review_plan.d_rows(REAL_PLAN, ["D13"])
    assert review_plan.privacy_rule(REAL_PLAN).startswith("6. 不要向任何外部服务")
    assert review_plan.plan_version(REAL_PLAN) == "1.3"
    assert "1.3 的改动" in review_plan.preamble(REAL_PLAN)


def test_real_process_file_has_kickoff_and_version():
    assert review_plan.reviewer_kickoff(REAL_PROCESS).startswith("按以下流程工作。你的角色是审稿人")
    assert review_plan.process_version(REAL_PROCESS) == "v0.2.1"


def test_redact_names_and_home(tmp_path):
    text = f"执行者 Alice 在 {tmp_path}/work 工作，alicew 不动"

    out = review_plan.redact(text, ["Alice"], home=tmp_path)

    assert out == "执行者 审核人 在 ~/work 工作，alicew 不动"


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@localhost"], check=True)


def _commit(path: Path, name: str, content: str, message: str) -> None:
    (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message], check=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "r"
    _init_repo(path)
    _commit(path, "a.py", "x = 1\n", "base")
    return review.RepoRef("r", path, "business", "true")


def test_tag_phase_is_idempotent_and_refuses_to_move(repo):
    assert "phase-3-start" in review.tag_phase([repo], 3)[0]
    assert "无需改动" in review.tag_phase([repo], 3)[0]

    _commit(repo.path, "b.py", "y = 2\n", "more")
    with pytest.raises(AgentopsError):
        review.tag_phase([repo], 3)
    assert "→" in review.tag_phase([repo], 3, force=True)[0]


def test_tag_phase_skips_repo_without_commits(tmp_path):
    path = tmp_path / "empty"
    _init_repo(path)

    messages = review.tag_phase([review.RepoRef("e", path, "business", "true")], 2)

    assert "还没有提交" in messages[0]


def test_collect_diff_from_tag_excludes_patterns_and_lists_untracked(repo):
    review.tag_phase([repo], 2)
    _commit(repo.path, "uv.lock", "lock\n", "lock")
    _commit(repo.path, "b.py", "y = 2\n", "feature")
    (repo.path / "new.txt").write_text("untracked\n")

    result = review.collect_diff(repo, 2, exclude=("uv.lock",), max_lines=4000)

    assert not result.tag_missing
    assert "b.py" in result.stat and "uv.lock" not in result.stat
    assert "+y = 2" in result.diff
    assert "new.txt" in result.untracked


def test_collect_diff_without_tag_uses_empty_tree(repo):
    result = review.collect_diff(repo, 4, exclude=(), max_lines=4000)

    assert result.tag_missing
    assert "空树" in result.range_label
    assert "+x = 1" in result.diff


def test_collect_diff_truncates_long_output(repo):
    review.tag_phase([repo], 2)
    _commit(repo.path, "big.py", "".join(f"line{i} = {i}\n" for i in range(200)), "big")

    result = review.collect_diff(repo, 2, exclude=(), max_lines=50)

    assert result.truncated and "已省略" in result.diff


def test_run_tests_captures_output_and_rc(repo):
    failing = review.RepoRef("r", repo.path, "business", "sh -c 'echo hello; echo bad >&2; exit 3'")

    result = review.run_tests(failing, max_lines=100, timeout_minutes=1)

    assert result.rc == 3 and "hello" in result.output and "bad" in result.output


def test_build_prompt_orders_sections_and_marks_light_review():
    diffs = [review.DiffResult("r", "tag→HEAD", "a.py | 1 +", "+x", "", False, False)]
    tests = [review.TestResult("r", "just check", 0, "ok")]

    prompt = review.build_prompt(
        phase=1,
        plan=MINI_PLAN,
        process=REAL_PROCESS,
        decisions="决定 A",
        summary="做了 X",
        deviations="差异 Y",
        diffs=diffs,
        tests=tests,
    )

    order = [
        prompt.index(h)
        for h in (
            "## 1. 通用流程",
            "## 2. 审稿人启动语",
            "## 3. 项目说明",
            "## 4. 审核人已确认的决定",
            "## 5. 本阶段",
            "## 6. 材料包",
            "## 7. 输出要求",
        )
    ]
    assert order == sorted(order)
    assert "本次是轻审" in prompt and "锁" in prompt
    assert "决定 A" in prompt and "做了 X" in prompt and "差异 Y" in prompt and "+x" in prompt
    assert "| 1 套件骨架 |" in prompt
    assert "| D5 |" not in prompt  # mini plan has no D5 row
    assert "\n#### 6. 执行阶段" in prompt  # 嵌入文档的标题降了两级
    assert "\n## 6. 执行阶段" not in prompt


def test_phase_spec_rejects_unreviewed_phase():
    with pytest.raises(AgentopsError):
        review.phase_spec(0)
    with pytest.raises(AgentopsError):
        review.phase_spec(9)


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """假 codex：记录 stdin，把固定意见写到 -o 文件。"""
    script = tmp_path / "fake-codex"
    opinion = "1. 核对结果：未发现\\n2. 最大失误：无\\n3. 反馈解释：无反馈\\n4. 候选改动：无修改\\n"
    script.write_text(
        "#!/bin/bash\nout=''\n"
        'while [ $# -gt 0 ]; do if [ "$1" = \'-o\' ]; then out="$2"; shift; fi; shift; done\n'
        f"cat > {tmp_path}/received.txt\n"
        f"printf '{opinion}' > \"$out\"\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv(review.ENV_CODEX_CMD, f"{script} exec -o {{output}} -")
    return tmp_path


def test_run_review_end_to_end_with_fake_codex(agentops_home, fake_codex, tmp_path, monkeypatch):
    phase = 1
    paths = review.review_paths(phase)
    summary = tmp_path / "summary.md"
    summary.write_text("阶段 1 做了 ao，执行者 Alice。\n")
    assert str(tmp_path) in str(paths["review"])  # 隔离生效，不会碰真实记录
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[review]\nredact = ["Alice"]\n')  # 脱敏词显式给出，不依赖默认值
    repo_path = tmp_path / "biz"
    _init_repo(repo_path)
    _commit(repo_path, "a.py", "x = 1\n", "base")
    repos = (review.RepoRef("biz", repo_path, "business", "sh -c 'echo tests-ok'"),)

    outcome = review.run_review(
        config.load_config(cfg_file), phase, repos=repos, summary_path=summary, with_tests=True
    )

    assert outcome.codex_rc == 0 and outcome.review_file == paths["review"]
    received = (fake_codex / "received.txt").read_text()
    assert "## 1. 通用流程" in received and "阶段 1 做了 ao" in received and "tests-ok" in received
    assert "Alice" not in received and "执行者 审核人" in received
    assert str(Path.home()) not in received
    doc = paths["review"].read_text()
    assert "核对结果：未发现" in doc and "## 处置（执行者填写）" in doc and "轻审" in doc
    assert paths["prompt"].exists() and paths["tests"].exists() and paths["codex_log"].exists()
    assert [e.type for e in events.read_events()][-1] == "review_run"


def test_run_review_requires_summary(agentops_home, tmp_path):
    with pytest.raises(AgentopsError):
        review.run_review(
            config.Config(),
            2,
            repos=(),
            summary_path=tmp_path / "missing.md",
            with_tests=False,
            dry_run=True,
        )


def test_missing_codex_gives_clear_error(agentops_home, tmp_path, monkeypatch):
    monkeypatch.setenv(review.ENV_CODEX_CMD, f"{tmp_path}/no-such-codex -o {{output}} -")
    summary = tmp_path / "s.md"
    summary.write_text("x")

    with pytest.raises(AgentopsError) as info:
        review.run_review(config.Config(), 2, repos=(), summary_path=summary, with_tests=False)

    assert "Codex" in str(info.value)


def test_cli_review_prompt_dry_run(agentops_home, tmp_path, capsys):
    summary = tmp_path / "s.md"
    summary.write_text("小结")

    code = cli.main(["review", "prompt", "--phase", "2", "--summary", str(summary), "--no-tests"])

    assert code == 0
    out = capsys.readouterr().out
    assert "prompt 存档" in out
    prompt_file = review.review_paths(2)["prompt"]
    assert prompt_file.exists() and "小结" in prompt_file.read_text()
    assert str(tmp_path) in str(prompt_file)


def test_config_review_section_merges_defaults(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[review]\nredact = ["Alice"]\n')

    loaded = config.load_config(cfg)

    assert loaded.review["redact"] == ["Alice"]
    assert loaded.review["max_diff_lines"] == 4000
    assert os.path.basename(str(loaded.review["codex_cmd"]).split()[0]) == "codex"
    assert json.dumps(dict(loaded.review))  # 可序列化


def test_cli_review_repo_filter_rejects_unknown(agentops_home, tmp_path, capsys):
    summary = tmp_path / "s.md"
    summary.write_text("小结")

    code = cli.main(
        [
            "review",
            "prompt",
            "--phase",
            "2",
            "--summary",
            str(summary),
            "--no-tests",
            "--repo",
            "nope",
        ]
    )

    assert code == 2 and "未知仓库" in capsys.readouterr().err


def test_collect_diff_survives_non_utf8_bytes_in_tree(tmp_path):
    # Arrange：一个带 phase 标签的仓库，工作区里有一个含非 UTF-8 字节的文本文件
    import subprocess

    from agentops import review
    from agentops.review import RepoRef

    repo = tmp_path / "r"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@localhost",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    git("tag", "-a", review.tag_name(9), "-m", "start")
    (repo / "evidence.txt").write_bytes("好".encode()[:2] + b" broken\n")
    git("add", "-A")

    # Act
    collected = review.collect_diff(
        RepoRef(name="r", path=repo, kind="business", test_cmd="true"),
        9,
        exclude=(),
        max_lines=2000,
    )

    # Assert：不抛 UnicodeDecodeError，坏字节被替换
    assert "evidence.txt" in collected.diff
    assert "�" in collected.diff or "broken" in collected.diff
