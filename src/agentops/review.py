"""ao review：按 execute-review-improve 流程让 Codex 只读评审一个阶段（计划第 6A 节）。

步骤：跑测试存输出，拼 prompt，codex exec 只读评审，结果存到 AGENTOPS_HOME/reviews/phase-N.md，
执行者在同一文件里逐条处置。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentops import events, review_plan
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.paths import home as agentops_home
from agentops.timeutil import now_iso

SUITE_ROOT = Path(__file__).resolve().parent.parent.parent
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TAG_TEMPLATE = "phase-{phase}-start"
ENV_CODEX_CMD = "AGENTOPS_CODEX_CMD"
ENV_REVIEWS_DIR = "AGENTOPS_REVIEWS_DIR"  # 测试用：把评审记录目录指到别处，避免碰真实记录
NO_DECISIONS = "（本仓库没有决策记录）"
DEFAULT_CODEX_CMD = "codex exec -s read-only -C {repo} --skip-git-repo-check -o {output} -"


@dataclass(frozen=True)
class PhaseSpec:
    title: str
    mode: str  # none | light | full | report
    focus: str
    d_ids: tuple[str, ...]


PHASES: Mapping[int, PhaseSpec] = {
    0: PhaseSpec("环境", "none", "", ()),
    1: PhaseSpec("套件骨架", "light", "重任务槽位的锁，在并发和进程崩溃时是否正确", ("D5", "D11")),
    2: PhaseSpec(
        "业务仓库和护栏",
        "full",
        "hook 和沙箱能否被绕过、main 保护、留出数据隔离",
        ("D4", "D10", "D12"),
    ),
    3: PhaseSpec(
        "合并队列和 steward",
        "full",
        "竞态、重新排队、审批和 SHA 绑定、失败后能否恢复、Bash 改测试能否被第二道防线挡住",
        ("D1", "D2", "D3", "D5"),
    ),
    4: PhaseSpec(
        "会话、判断层和 PM",
        "full",
        "预算上限是否真生效、resume 会不会死循环、提示词注入的途径",
        ("D6", "D9"),
    ),
    5: PhaseSpec("看板和定时任务", "light", "网页伪造请求、只监听本机、令牌", ("D7", "D8")),
    6: PhaseSpec(
        "演示验收",
        "report",
        "执行报告里 D1–D13 的每条结论，是否都有事件日志或截图作证据",
        tuple(f"D{n}" for n in range(1, 14)),
    ),
}


@dataclass(frozen=True)
class RepoRef:
    name: str
    path: Path
    kind: str  # suite | business
    test_cmd: str


@dataclass(frozen=True)
class DiffResult:
    repo: str
    range_label: str
    stat: str
    diff: str
    untracked: str
    truncated: bool
    tag_missing: bool


@dataclass(frozen=True)
class TestResult:
    repo: str
    command: str
    rc: int
    output: str


@dataclass(frozen=True)
class ReviewOutcome:
    phase: int
    review_file: Path
    prompt_file: Path
    codex_rc: int | None
    dry_run: bool


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=120,
        )
    except OSError as exc:
        raise AgentopsError(f"无法运行 git: {exc}") from exc


def phase_spec(phase: int) -> PhaseSpec:
    if phase not in PHASES:
        raise AgentopsError(f"阶段号必须在 0–6 之间，得到 {phase}")
    spec = PHASES[phase]
    if spec.mode == "none":
        raise AgentopsError(f"阶段 {phase}（{spec.title}）按计划不评审")
    return spec


def tag_name(phase: int) -> str:
    return TAG_TEMPLATE.format(phase=phase)


def suite_repo() -> RepoRef:
    return RepoRef("agentops", SUITE_ROOT, "suite", "just check")


def business_repos(config: Config) -> tuple[RepoRef, ...]:
    found = []
    for name in config.repos:
        path = config.repo_path(name)
        if (path / ".git").exists():
            found.append(RepoRef(name, path, "business", "just test-all"))
    return tuple(found)


def all_repos(config: Config) -> tuple[RepoRef, ...]:
    return (suite_repo(), *business_repos(config))


def tag_phase(repos: Iterable[RepoRef], phase: int, *, force: bool = False) -> tuple[str, ...]:
    """在每个仓库的当前 HEAD 打 phase-N-start 标签。已有且指向同一提交就跳过。"""
    tag = tag_name(phase)
    messages = []
    for repo in repos:
        head = _git(repo.path, "rev-parse", "--verify", "-q", "HEAD")
        if head.returncode != 0:
            messages.append(f"{repo.name}: 还没有提交，跳过（评审时视为本阶段新建）")
            continue
        existing = _git(repo.path, "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}")
        if existing.returncode == 0 and not force:
            if existing.stdout.strip() == head.stdout.strip():
                messages.append(f"{repo.name}: {tag} 已在 {head.stdout.strip()[:7]}，无需改动")
                continue
            raise AgentopsError(
                f"{repo.name}: {tag} 已指向 {existing.stdout.strip()[:7]}，与 HEAD 不同；"
                "确认要移动就加 --force"
            )
        result = _git(
            repo.path, "tag", "-f" if force else "-a", tag, "-m", f"agentops phase {phase} start"
        )
        if force:
            result = _git(repo.path, "tag", "-f", "-a", tag, "-m", f"agentops phase {phase} start")
        if result.returncode != 0:
            raise AgentopsError(f"{repo.name}: 打标签失败: {result.stderr.strip()}")
        messages.append(f"{repo.name}: {tag} → {head.stdout.strip()[:7]}")
    return tuple(messages)


def _tail(text: str, max_lines: int) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    kept = lines[-max_lines:]
    return "\n".join([f"…（前 {len(lines) - max_lines} 行已省略）", *kept]), True


def collect_diff(
    repo: RepoRef, phase: int, *, exclude: Sequence[str], max_lines: int
) -> DiffResult:
    """从 phase-N-start 标签到工作区的 diff；没有标签就视为本阶段新建，和空树比。"""
    tag = tag_name(phase)
    base_lookup = _git(repo.path, "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}")
    tag_missing = base_lookup.returncode != 0
    base = EMPTY_TREE if tag_missing else base_lookup.stdout.strip()
    head = _git(repo.path, "rev-parse", "--short", "HEAD").stdout.strip() or "（无提交）"
    label = (
        f"无 {tag} 标签，视为本阶段新建，与空树比较（工作区 {head}）"
        if tag_missing
        else f"{tag}（{base[:7]}）→ 工作区（HEAD {head}）"
    )
    pathspec = [".", *(f":(exclude){pattern}" for pattern in exclude)]
    stat = _git(repo.path, "diff", "--stat", base, "--", *pathspec).stdout.strip()
    diff_text, truncated = _tail(_git(repo.path, "diff", base, "--", *pathspec).stdout, max_lines)
    untracked = _git(repo.path, "status", "--short", "--untracked-files=all").stdout.strip()
    return DiffResult(repo.name, label, stat, diff_text, untracked, truncated, tag_missing)


def run_tests(repo: RepoRef, *, max_lines: int, timeout_minutes: float) -> TestResult:
    """运行仓库的测试命令，返回尾部输出。Codex 在只读沙箱里跑不了测试，只能看结果。"""
    try:
        result = subprocess.run(
            shlex.split(repo.test_cmd),
            cwd=str(repo.path),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout_minutes * 60,
        )
        combined = result.stdout + ("\n" + result.stderr if result.stderr.strip() else "")
        rc = result.returncode
    except subprocess.TimeoutExpired:
        combined, rc = f"超过 {timeout_minutes} 分钟未结束", 124
    except OSError as exc:
        combined, rc = f"无法运行 {repo.test_cmd!r}: {exc}", 127
    output, _ = _tail(combined.strip(), max_lines)
    return TestResult(repo.name, repo.test_cmd, rc, output)


def _read(path: Path, label: str) -> str:
    if not path.exists():
        raise AgentopsError(f"缺少{label}: {path}")
    return path.read_text(encoding="utf-8", errors="replace").strip()


def demote_headings(text: str) -> str:
    """嵌入文档的标题统一降两级，让它们都落在 prompt 自己的二级章节之下。"""
    return "\n".join(("##" + line) if line.startswith("#") else line for line in text.splitlines())


def build_prompt(
    *,
    phase: int,
    plan: str,
    process: str,
    decisions: str,
    summary: str,
    deviations: str,
    diffs: Sequence[DiffResult],
    tests: Sequence[TestResult],
    feedback: str = "无",
) -> str:
    spec = phase_spec(phase)
    light_note = ""
    if spec.mode == "light":
        light_note = (
            f"\n\n**本次是轻审，只审这一个问题，其他方面不展开：{spec.focus}。**"
            "仍按四项输出，核对结果和最大失误都只围绕这个问题。"
        )
    elif spec.mode == "report":
        light_note = (
            f"\n\n**本次用报告评审模式：{spec.focus}。**"
            "核对结果按 D1–D13 逐条写：有证据 / 证据不足 / 与证据矛盾。"
        )
    diff_parts = []
    for item in diffs:
        diff_parts.append(
            f"#### {item.repo}：{item.range_label}\n\n{item.stat or '（无改动）'}\n\n"
            + (f"未跟踪或未提交的文件：\n```\n{item.untracked}\n```\n\n" if item.untracked else "")
            + f"```diff\n{item.diff or '（无改动）'}\n```"
            + ("\n\n（diff 过长，只保留了尾部）" if item.truncated else "")
        )
    test_parts = [
        f"#### {item.repo}：`{item.command}`（退出码 {item.rc}）\n\n```\n{item.output}\n```"
        for item in tests
    ] or ["（本次未运行测试）"]
    return "\n\n".join(
        [
            f"# 评审材料：阶段 {phase}（{spec.title}）",
            "## 1. 通用流程（execute-review-improve）\n\n" + demote_headings(process),
            "## 2. 审稿人启动语\n\n" + review_plan.reviewer_kickoff(process),
            f"## 3. 项目说明（计划全文，版本 {review_plan.plan_version(plan)}）\n\n"
            + demote_headings(plan),
            "## 4. 审核人已确认的决定\n\n"
            "### 4.1 计划的版本改动说明\n\n" + review_plan.preamble(plan) + "\n\n"
            "### 4.2 隐私规则（开场指令第 6 条）\n\n" + review_plan.privacy_rule(plan) + "\n\n"
            "### 4.3 决策记录（docs/decisions.md）\n\n" + demote_headings(decisions),
            "## 5. 本阶段的验收条件与完成标准\n\n"
            "### 5.1 第 6 节对应行\n\n" + review_plan.phase_row(plan, phase) + "\n\n"
            "### 5.2 评审方式（第 6A 节）\n\n" + review_plan.review_row(plan, phase) + "\n\n"
            "### 5.3 相关完成标准（第 8 节）\n\n"
            + review_plan.d_rows(plan, spec.d_ids)
            + light_note,
            "## 6. 材料包\n\n"
            f"本轮：{now_iso()}；流程版本 {review_plan.process_version(process)}；"
            f"计划版本 {review_plan.plan_version(plan)}\n\n"
            "### 6.1 执行者小结\n\n" + demote_headings(summary) + "\n\n"
            "### 6.2 与计划不一致之处（docs/claude-cli.md）\n\n"
            + demote_headings(deviations)
            + "\n\n"
            "### 6.3 代码改动\n\n" + "\n\n".join(diff_parts) + "\n\n"
            "### 6.4 测试输出\n\n" + "\n\n".join(test_parts) + "\n\n"
            "### 6.5 反馈\n\n" + feedback,
            "## 7. 输出要求\n\n"
            "只输出流程规定的四项：核对结果、最大失误、反馈解释、候选改动。每条意见给编号，"
            "带文件路径或命令作为证据。评判依据只有第 3、4、5 节；不要自行提出新的验收标准。"
            "候选改动若涉及修改计划本身，标明“需审核人确认”。不要改写交付物。",
        ]
    )


def codex_command(config: Config, *, repo_path: Path, output: Path) -> list[str]:
    template = os.environ.get(ENV_CODEX_CMD) or str(
        config.review.get("codex_cmd") or DEFAULT_CODEX_CMD
    )
    rendered = template.replace("{repo}", shlex.quote(str(repo_path))).replace(
        "{output}", shlex.quote(str(output))
    )
    return shlex.split(rendered)


def run_codex(
    prompt: str,
    *,
    command: Sequence[str],
    cwd: Path,
    output: Path,
    log: Path,
    timeout_minutes: float,
) -> tuple[int, str]:
    """把 prompt 从标准输入喂给 codex，返回退出码和审稿正文（优先 -o 文件，否则 stdout）。"""
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command),
            input=prompt,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            cwd=str(cwd),
            timeout=timeout_minutes * 60,
        )
    except FileNotFoundError as exc:
        raise AgentopsError(
            f"找不到审稿命令 {command[0]!r}。请安装 Codex 命令行版并登录，"
            "或在 config.toml 的 [review] 里改 codex_cmd"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentopsError(f"codex 超过 {timeout_minutes} 分钟未结束") from exc
    elapsed = time.monotonic() - started
    log.write_text(
        f"$ {' '.join(command)}\n退出码 {result.returncode}，耗时 {elapsed:.0f} 秒\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    text = output.read_text(encoding="utf-8", errors="replace").strip() if output.exists() else ""
    return result.returncode, text or result.stdout.strip()


def reviews_root() -> Path:
    override = os.environ.get(ENV_REVIEWS_DIR)
    return Path(override) if override else agentops_home() / "reviews"


def review_paths(phase: int) -> Mapping[str, Path]:
    reviews = reviews_root()
    inputs = reviews / "inputs"
    return {
        "review": reviews / f"phase-{phase}.md",
        "summary": reviews / f"phase-{phase}.summary.md",
        "prompt": inputs / f"phase-{phase}.prompt.md",
        "tests": inputs / f"phase-{phase}.tests.txt",
        "codex_out": inputs / f"phase-{phase}.codex.out.md",
        "codex_log": inputs / f"phase-{phase}.codex.log",
    }


def _review_document(
    *,
    phase: int,
    spec: PhaseSpec,
    command: Sequence[str],
    diffs: Sequence[DiffResult],
    tests: Sequence[TestResult],
    opinion: str,
    plan_version: str,
    process_version: str,
) -> str:
    ranges = "；".join(f"{d.repo}：{d.range_label}" for d in diffs)
    test_summary = "；".join(f"{t.repo} 退出码 {t.rc}" for t in tests) or "未运行"
    mode = {"light": f"轻审（问题：{spec.focus}）", "full": "必审", "report": "报告评审"}[spec.mode]
    return f"""# 阶段 {phase} 评审记录（{spec.title}）

- 评审时间：{now_iso()}
- 流程版本：execute-review-improve {process_version}（docs/process/execute-review-improve.md）
- 计划版本：{plan_version}
- 评审方式：{mode}
- 审稿人：Codex，命令 `{" ".join(command)}`
- 输入存档：AGENTOPS_HOME/reviews/inputs/ 下的 phase-{phase}.prompt.md、.tests.txt、.codex.log
- diff 范围：{ranges}
- 测试：{test_summary}
- 效果记录：意见数 _；采纳 _；真 bug _（执行者处置后填写）

## 审稿意见（Codex 原文）

{opinion or "（codex 没有返回内容，见 inputs/phase-" + str(phase) + ".codex.log）"}

## 处置（执行者填写）

| 编号 | 意见摘要 | 处置（采纳/拒绝/待查证） | 依据或改动位置 |
|---|---|---|---|

## 修正后复测

（处置完成后重新运行测试，贴结论）
"""


def run_review(
    config: Config,
    phase: int,
    *,
    repos: Sequence[RepoRef],
    summary_path: Path | None = None,
    with_tests: bool = True,
    dry_run: bool = False,
    feedback: str = "无",
) -> ReviewOutcome:
    spec = phase_spec(phase)
    paths = review_paths(phase)
    paths["prompt"].parent.mkdir(parents=True, exist_ok=True)
    plan = _read(SUITE_ROOT / "docs" / "plan.md", "计划文档")
    process = _read(SUITE_ROOT / "docs" / "process" / "execute-review-improve.md", "流程文件")
    decisions_path = SUITE_ROOT / "docs" / "decisions.md"
    decisions = _read(decisions_path, "决策记录") if decisions_path.exists() else NO_DECISIONS
    deviations = _read(SUITE_ROOT / "docs" / "claude-cli.md", "核对记录")
    summary = _read(summary_path or paths["summary"], f"执行者小结（阶段 {phase}）")
    review_cfg = config.review
    exclude = tuple(str(p) for p in review_cfg.get("diff_exclude", ()))  # type: ignore[union-attr]
    max_diff = int(review_cfg.get("max_diff_lines", 4000))  # type: ignore[arg-type]
    max_test = int(review_cfg.get("max_test_lines", 300))  # type: ignore[arg-type]
    timeout = float(review_cfg.get("timeout_minutes", 30))  # type: ignore[arg-type]
    diffs = [collect_diff(r, phase, exclude=exclude, max_lines=max_diff) for r in repos]
    for item in diffs:
        if item.tag_missing:
            print(
                f"警告: {item.repo} 没有 {tag_name(phase)} 标签，diff 视为本阶段新建",
                file=sys.stderr,
            )
    tests: list[TestResult] = []
    if with_tests:
        tests = [run_tests(r, max_lines=max_test, timeout_minutes=timeout) for r in repos]
        paths["tests"].write_text(
            "\n\n".join(f"== {t.repo}: {t.command} (rc {t.rc}) ==\n{t.output}" for t in tests),
            encoding="utf-8",
        )
    prompt = build_prompt(
        phase=phase,
        plan=plan,
        process=process,
        decisions=decisions,
        summary=summary,
        deviations=deviations,
        diffs=diffs,
        tests=tests,
        feedback=feedback,
    )
    prompt = review_plan.redact(
        prompt,
        [str(n) for n in review_cfg.get("redact", ())],
        home=Path.home(),  # type: ignore[union-attr]
    )
    paths["prompt"].write_text(prompt, encoding="utf-8")
    command = codex_command(config, repo_path=SUITE_ROOT, output=paths["codex_out"])
    if dry_run:
        return ReviewOutcome(phase, paths["review"], paths["prompt"], None, True)
    rc, opinion = run_codex(
        prompt,
        command=command,
        cwd=SUITE_ROOT,
        output=paths["codex_out"],
        log=paths["codex_log"],
        timeout_minutes=float(review_cfg.get("timeout_minutes", 30)),  # type: ignore[arg-type]
    )
    paths["review"].write_text(
        _review_document(
            phase=phase,
            spec=spec,
            command=command,
            diffs=diffs,
            tests=tests,
            opinion=opinion,
            plan_version=review_plan.plan_version(plan),
            process_version=review_plan.process_version(process),
        ),
        encoding="utf-8",
    )
    events.emit(
        "review_run",
        repo="agentops",
        detail={
            "phase": phase,
            "rc": rc,
            "repos": [r.name for r in repos],
            "tests": [t.rc for t in tests],
        },
    )
    return ReviewOutcome(phase, paths["review"], paths["prompt"], rc, False)
