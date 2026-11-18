"""Claude 后端：claude -p 加 --json-schema 的一次性结构化调用，无工具、不持久化会话。
从 llm.py 原样搬来，行为不变。AGENTOPS_CLAUDE_CMD 可替换 claude 可执行文件（测试用假的）；
设了它就不读钥匙串。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from agentops import auth
from agentops.paths import home
from agentops.runtime.base import LlmError, StructuredResult, WorkerResult

if TYPE_CHECKING:
    from agentops.start import Launch

RUNTIME = "claude"
ENV_CLAUDE_CMD = "AGENTOPS_CLAUDE_CMD"
_DROPPED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def claude_command() -> list[str]:
    return shlex.split(os.environ.get(ENV_CLAUDE_CMD) or "claude")


def is_fake() -> bool:
    return bool(os.environ.get(ENV_CLAUDE_CMD))


def call_env() -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in _DROPPED}
    if is_fake():
        return base
    return auth.claude_env(base)


def workdir() -> os.PathLike[str]:
    path = home() / "llm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_command(
    prompt: str,
    schema: Mapping[str, object],
    *,
    model: str,
    budget_usd: float,
    max_turns: int,
    effort: str | None = None,
) -> list[str]:
    """单次结构化调用的 claude 命令行；effort 为空就不传，用 Claude Code 默认。"""
    command = [*claude_command(), "-p", "--model", model]
    if effort:
        command += ["--effort", effort]
    command += [
        "--max-budget-usd",
        f"{budget_usd}",
        "--max-turns",
        str(max_turns),
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "project",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
        prompt,
    ]
    return command


def structured_call(
    prompt: str,
    schema: Mapping[str, object],
    *,
    model: str,
    effort: str | None,
    budget_usd: float,
    max_turns: int,
    timeout_minutes: float,
    label: str,
) -> StructuredResult:
    command = build_command(
        prompt, schema, model=model, budget_usd=budget_usd, max_turns=max_turns, effort=effort
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=workdir(),
            env=call_env(),
            timeout=timeout_minutes * 60,
        )
    except OSError as exc:
        raise LlmError(f"无法运行 claude: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LlmError(f"{label} 调用超过 {timeout_minutes} 分钟") from exc
    try:
        raw = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise LlmError(f"{label} 没有返回 JSON（退出码 {result.returncode}）") from exc
    if not isinstance(raw, dict):
        raise LlmError(f"{label} 返回的不是 JSON 对象")
    cost = float(raw.get("total_cost_usd") or 0.0)
    turns = int(raw.get("num_turns") or 0)
    if raw.get("is_error"):
        raise LlmError(
            f"{label} 失败: {str(raw.get('result', ''))[:200]}", cost_usd=cost, num_turns=turns
        )
    data = raw.get("structured_output")
    if not isinstance(data, dict):
        raise LlmError(f"{label} 没有结构化输出", cost_usd=cost, num_turns=turns)
    session = raw.get("session_id")
    return StructuredResult(
        data=MappingProxyType(data),
        runtime=RUNTIME,
        cost_usd=cost,
        cost_estimated=False,
        price_known=True,
        num_turns=turns,
        usage=None,
        session_id=None if session is None else str(session),
        raw=MappingProxyType({**raw, "rc": result.returncode}),
    )


def worker_script(launch: Launch, *, max_turns: int, resume_prompt: Path | None = None) -> str:
    """worker 会话的启动脚本（原 start._script，逐字搬来）。

    resume_prompt 给出时是原会话重做一轮。
    """
    claude_flags = [
        "--permission-mode",
        "auto",
        "--model",
        launch.model,
        "--settings",
        str(launch.settings_path),
        "--setting-sources",
        "project,local",
    ]
    if launch.effort:
        claude_flags += ["--effort", launch.effort]
    if resume_prompt is None:
        claude_flags += ["--session-id", launch.session_id, "--name", launch.name]
        prompt_file = launch.prompt_path
    else:
        # --fork-session：本轮是新会话 id，result.json 里的 session_id 是它；成本只含本轮
        claude_flags += ["--resume", launch.session_id, "--fork-session"]
        prompt_file = resume_prompt
    if launch.mode == "headless":
        claude_flags += [
            "-p",
            "--permission-prompts",
            "none",
            "--max-budget-usd",
            f"{launch.budget_usd}",
            "--max-turns",
            str(max_turns),
            "--output-format",
            "json",
        ]
    flags = " ".join(shlex.quote(f) for f in claude_flags)
    redirect = f" > {shlex.quote(str(launch.result_path))}" if launch.mode == "headless" else ""
    return (
        "#!/bin/bash\n"
        "# agentops 会话启动脚本：由 ao start 生成，会话自己写不了这个目录。\n"
        f"cd {shlex.quote(str(launch.clone))} || exit 1\n"
        f"export AGENTOPS_REPO={shlex.quote(launch.item.repo)} AGENTOPS_ITEM={launch.item.id} "
        f"AGENTOPS_MODE={launch.mode} AGENTOPS_BRANCH={shlex.quote(launch.branch)} "
        f"AGENTOPS_SESSION={launch.session_id}\n"
        "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN\n"
        'if [ -z "${AGENTOPS_CLAUDE_CMD:-}" ]; then\n'
        "  export CLAUDE_CODE_OAUTH_TOKEN="
        '"$(/usr/bin/security find-generic-password -s agentops-claude -w)"\n'
        "fi\n"
        "CLAUDE_CMD=${AGENTOPS_CLAUDE_CMD:-claude}\n"
        f'exec $CLAUDE_CMD {flags} "$(cat {shlex.quote(str(prompt_file))})"{redirect}\n'
    )


def parse_worker_result(result_path: Path, rc: int) -> WorkerResult:
    """无人值守会话的 result.json（--output-format json）。读不到或不是 JSON 就按空结果算。"""
    raw: Mapping[str, object] = {}
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8") or "{}")
            raw = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            raw = {}
    session = raw.get("session_id")
    return WorkerResult(
        session_id=None if not session else str(session),
        is_error=bool(raw.get("is_error")) or rc != 0,
        cost_usd=float(raw.get("total_cost_usd") or 0.0),  # type: ignore[arg-type]
        cost_estimated=False,
        price_known=True,
        num_turns=int(raw.get("num_turns") or 0),  # type: ignore[call-overload]
        usage=None,
        summary=str(raw.get("result") or ""),
    )
