"""Codex 后端：`codex exec --json` 的一次性结构化调用。无工具、只读沙箱、不持久化会话、
提示词走标准输入。

与 Claude 后端的两点不同：

- 独立的 CODEX_HOME（agentops 状态目录下的 codex-home）。agentops 自己的 config.toml 关掉
  账号级连接器与用户插件；judge.config.toml 这个 profile 再关掉 shell 工具并 deny 敏感目录；
  Codex 写回的状态也都落在这个目录，不碰审核人的 ~/.codex/config.toml。
  登录态 auth.json 软链到审核人的 ~/.codex/auth.json。
- 订阅额度没有美元数：从 `turn.completed.usage` 记 token 五项，美元按本地价格表估算，
  分发层另做当日 token 硬上限。

AGENTOPS_CODEX_CMD 可替换 codex 可执行文件（测试用假的）。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template
from types import MappingProxyType

from agentops.paths import home
from agentops.repo import TEMPLATES
from agentops.runtime.base import LlmError, StructuredResult, Usage

RUNTIME = "codex"
ENV_CODEX_CMD = "AGENTOPS_CODEX_CMD"
ENV_USER_CODEX_HOME = "CODEX_HOME"
DEFAULT_USER_CODEX_HOME = "~/.codex"
HOME_CONFIG_TEMPLATE = TEMPLATES / "codex" / "home-config.toml"
JUDGE_PROFILE = "judge"
JUDGE_PROFILE_TEMPLATE = TEMPLATES / "codex" / "judge.config.toml"
HOME_MODE = 0o700  # 目录里有 auth.json 软链
# 带 --dangerously-bypass-hook-trust 时 turn 前固定出现的告警项，不是失败
HOOK_TRUST_WARNING = "--dangerously-bypass-hook-trust"
STDIN_NOTICE = "Reading additional input from stdin"  # stderr 固定一行，不算错误
STDERR_TAIL_LINES = 3
MESSAGE_LIMIT = 300
TOKENS_PER_MILLION = 1_000_000
_DROPPED = ("OPENAI_API_KEY",)  # 设了会改走 API 计费，去掉以确保用订阅登录态


@dataclass(frozen=True)
class ParsedEvents:
    thread_id: str | None
    usage: Usage | None
    last_message: str | None
    errors: tuple[str, ...]  # turn.failed：这一轮确实失败了
    warnings: tuple[str, ...]  # error 项：实测见过传输回退、hook 信任告警，不一定失败
    turns: int


def codex_command() -> list[str]:
    return shlex.split(os.environ.get(ENV_CODEX_CMD) or "codex")


def is_fake() -> bool:
    return bool(os.environ.get(ENV_CODEX_CMD))


def user_codex_home() -> Path:
    """审核人自己的 Codex 目录：只从这里拿登录态，别的都不读。"""
    return Path(os.environ.get(ENV_USER_CODEX_HOME) or DEFAULT_USER_CODEX_HOME).expanduser()


def codex_home() -> Path:
    return home() / "codex-home"


def workdir() -> Path:
    path = home() / "llm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_atomic(path: Path, text: str) -> None:
    """内容没变就不写；要写就先写临时文件再改名，并发启动的 codex 不会读到半截文件。"""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def render_profile(template: Path, target: Path) -> str:
    return Template(template.read_text(encoding="utf-8")).substitute(
        workdir=str(workdir()),
        agentops_home=str(home()),
        codex_home=str(target),
        user_codex_home=str(user_codex_home()),
        user_home=str(Path.home()),
    )


def _link_auth(target: Path) -> None:
    auth = target / "auth.json"
    if auth.is_symlink() and not auth.exists():
        auth.unlink()  # 审核人重新登录或登出过，软链悬空，重建
    if auth.is_symlink() or auth.exists():
        return
    source = user_codex_home() / "auth.json"
    if not source.exists():
        if is_fake():
            return
        raise LlmError(
            f"找不到 Codex 登录态 {source}：先在终端运行 codex login；"
            f"或在 CODEX_HOME={target} 下登录一次"
        )
    try:
        auth.symlink_to(source)
    except FileExistsError:
        pass  # 两个调用同时首次渲染


def ensure_home() -> Path:
    """渲染 agentops 专用的 CODEX_HOME：主配置与 judge profile 按模板写，缺登录态就软链。幂等。"""
    target = codex_home()
    try:
        target.mkdir(parents=True, exist_ok=True, mode=HOME_MODE)
        _write_atomic(target / "config.toml", HOME_CONFIG_TEMPLATE.read_text(encoding="utf-8"))
        _write_atomic(
            target / f"{JUDGE_PROFILE}.config.toml", render_profile(JUDGE_PROFILE_TEMPLATE, target)
        )
        _link_auth(target)
    except OSError as exc:
        raise LlmError(f"无法准备 CODEX_HOME {target}: {exc}") from exc
    return target


def call_env() -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in _DROPPED}
    return {**base, ENV_USER_CODEX_HOME: str(codex_home())}


def build_command(
    *,
    model: str,
    effort: str | None,
    schema_path: Path,
    output_path: Path,
    workdir: Path,
) -> list[str]:
    """judge profile（无工具、只读、敏感目录 deny）、不留会话、不要 git 仓库。

    提示词从标准输入读（`-`），不受参数长度限制。
    """
    command = [
        *codex_command(),
        "exec",
        "--json",
        "--ephemeral",
        "--profile",
        JUDGE_PROFILE,
        "--skip-git-repo-check",
        "-C",
        str(workdir),
        "-m",
        model,
    ]
    if effort:
        command += ["-c", f"model_reasoning_effort={effort}"]
    command += ["--output-schema", str(schema_path), "-o", str(output_path), "-"]
    return command


def _error_message(payload: object) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("message") or payload)
    return str(payload or "")


def parse_events(text: str) -> ParsedEvents:
    """读 `--json` 事件流：thread_id、按轮累计的 usage、最后一条 agent_message、失败与告警。"""
    thread_id: str | None = None
    usage: Usage | None = None
    last_message: str | None = None
    errors: list[str] = []
    warnings: list[str] = []
    turns = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = str(event.get("thread_id") or "") or None
        elif kind == "turn.completed":
            turns += 1
            reported = event.get("usage")
            if isinstance(reported, dict):
                turn_usage = Usage.from_mapping(reported)
                usage = turn_usage if usage is None else usage.plus(turn_usage)
        elif kind == "turn.failed":
            errors.append(_error_message(event.get("error")) or "turn.failed")
        elif kind == "error":
            warnings.append(_error_message(event.get("message") or event.get("error")) or "error")
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                last_message = str(item.get("text") or "")
            elif item.get("type") == "error":
                message = str(item.get("message") or "")
                if HOOK_TRUST_WARNING not in message:
                    warnings.append(message)
    return ParsedEvents(thread_id, usage, last_message, tuple(errors), tuple(warnings), turns)


def estimate_cost(
    model: str, usage: Usage, prices: Mapping[str, Mapping[str, float]]
) -> float | None:
    """按每百万 token 的价格表估算；表里没有这个模型就返回 None。

    cache_write_input_tokens 不计价（OpenAI 不收）。
    """
    table = prices.get(model)
    if not table:
        return None

    def rate(key: str) -> float:
        return float(table.get(key, 0.0)) / TOKENS_PER_MILLION

    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return round(
        uncached * rate("input")
        + usage.cached_input_tokens * rate("cached_input")
        + usage.output_tokens * rate("output"),
        6,
    )


def _stderr_tail(stderr: str) -> str:
    lines = [ln for ln in (stderr or "").strip().splitlines() if STDIN_NOTICE not in ln]
    return " / ".join(lines[-STDERR_TAIL_LINES:])[:MESSAGE_LIMIT]


def _read_output(output_path: Path, parsed: ParsedEvents) -> str:
    """结构化结果优先读 -o 文件；文件缺失或为空就退回最后一条 agent_message（两处内容相同）。"""
    text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    return text.strip() or (parsed.last_message or "")


def structured_call(
    prompt: str,
    schema: Mapping[str, object],
    *,
    model: str,
    effort: str | None,
    timeout_minutes: float,
    label: str,
    prices: Mapping[str, Mapping[str, float]],
) -> StructuredResult:
    ensure_home()
    work = workdir()
    stamp = uuid.uuid4().hex
    schema_path = work / f"codex-{stamp}.schema.json"
    output_path = work / f"codex-{stamp}.out.json"
    command = build_command(
        model=model, effort=effort, schema_path=schema_path, output_path=output_path, workdir=work
    )
    try:
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            cwd=work,
            env=call_env(),
            timeout=timeout_minutes * 60,
        )
        return _interpret(result, output_path, model=model, label=label, prices=prices)
    except OSError as exc:
        raise LlmError(f"无法运行 codex: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # 超时拿不到 usage，这次的 token 不进当日上限；靠超时分钟数兜底
        raise LlmError(f"{label} 调用超过 {timeout_minutes} 分钟") from exc
    finally:
        schema_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def _interpret(
    result: subprocess.CompletedProcess[str],
    output_path: Path,
    *,
    model: str,
    label: str,
    prices: Mapping[str, Mapping[str, float]],
) -> StructuredResult:
    parsed = parse_events(result.stdout)
    estimate = estimate_cost(model, parsed.usage, prices) if parsed.usage else None
    cost = estimate or 0.0
    # 失败时把告警项也带上：额度用尽、传输失败这类原因就写在那里
    noise = "；".join((*parsed.errors, *parsed.warnings))[:MESSAGE_LIMIT]
    if parsed.errors:
        raise LlmError(f"{label} 失败: {noise}", cost_usd=cost, usage=parsed.usage)
    if result.returncode != 0:
        detail = " / ".join(part for part in (_stderr_tail(result.stderr), noise) if part)
        raise LlmError(
            f"{label} 退出码 {result.returncode}: {detail}", cost_usd=cost, usage=parsed.usage
        )
    try:
        data = json.loads(_read_output(output_path, parsed))
    except json.JSONDecodeError as exc:
        why = f"（{noise}）" if noise else ""
        raise LlmError(f"{label} 没有结构化输出{why}", cost_usd=cost, usage=parsed.usage) from exc
    if not isinstance(data, dict):
        raise LlmError(f"{label} 的结构化输出不是 JSON 对象", cost_usd=cost, usage=parsed.usage)
    raw = {
        "thread_id": parsed.thread_id,
        "usage": None if parsed.usage is None else parsed.usage.to_dict(),
        "price_known": estimate is not None,
        "warnings": list(parsed.warnings),
        "rc": result.returncode,
        "stderr": _stderr_tail(result.stderr),
    }
    return StructuredResult(
        data=MappingProxyType(data),
        runtime=RUNTIME,
        cost_usd=cost,
        cost_estimated=True,
        price_known=estimate is not None,
        num_turns=parsed.turns,
        usage=parsed.usage,
        session_id=parsed.thread_id,
        raw=MappingProxyType(raw),
    )
