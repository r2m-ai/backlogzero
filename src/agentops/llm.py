"""一次性的结构化 LLM 调用的分发层：按仓库配置选运行时（claude / codex），统一做预算预留、
结算与 llm_call 事件。判断层和 PM 都从这里调；后端实现在 agentops.runtime.*。

Claude 后端的 AGENTOPS_CLAUDE_CMD、build_command 等仍从这里导出，老调用方不用改。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from agentops import budget, events
from agentops.config import Config
from agentops.runtime import claude as claude_rt
from agentops.runtime import codex as codex_rt
from agentops.runtime.base import ROLES, LlmError, StructuredResult, Usage
from agentops.runtime.claude import (
    ENV_CLAUDE_CMD,
    build_command,
    call_env,
    claude_command,
    is_fake,
    workdir,
)

LlmResult = StructuredResult

__all__ = [
    "ENV_CLAUDE_CMD",
    "LlmError",
    "LlmResult",
    "StructuredResult",
    "Usage",
    "build_command",
    "call_env",
    "call_structured",
    "claude_command",
    "is_fake",
    "workdir",
]


def _emit(
    *,
    label: str,
    runtime: str,
    role: str,
    model: str,
    repo: str,
    item: int | None,
    cost_usd: float,
    cost_estimated: bool,
    price_known: bool,
    num_turns: int,
    usage: Usage | None,
    error: str | None = None,
) -> None:
    detail: dict[str, object] = {
        "label": label,
        "runtime": runtime,
        "role": role,
        "model": model,
        "cost_usd": cost_usd,
        "cost_estimated": cost_estimated,
        "price_known": price_known,
        "num_turns": num_turns,
        "usage": None if usage is None else usage.to_dict(),
    }
    if error is not None:
        detail["error"] = error[:300]
    events.emit("llm_call", repo=repo, item=item, detail=detail)


def call_structured(
    prompt: str,
    schema: Mapping[str, object],
    *,
    config: Config,
    role: str,
    label: str,
    repo: str,
    item: int | None = None,
    max_turns: int = 4,
    timeout_minutes: float = 10,
) -> StructuredResult:
    """按 repo 的 runtime 选后端；模型、思考强度、预算都从 config 按角色取。

    预算：先预留该角色的美元额度（Codex 侧是估算口径），Codex 还要过当日 token 硬上限；
    成功或失败都结算并记一条 llm_call 事件（失败的带 error）。后端抛出 LlmError 之外的异常
    （OSError、KeyboardInterrupt 等）也会释放预留，预留没有过期机制，漏一次就占一天。
    """
    if role not in ROLES:
        raise ValueError(f"未知角色 {role!r}")
    runtime = config.runtime_for(repo)
    model = config.model_for(role, runtime)
    effort = config.effort_for(role, runtime)
    budget_usd = float(config.budget_usd[role])
    call_id = f"llm-{uuid.uuid4()}"
    if runtime == codex_rt.RUNTIME:
        budget.require_tokens(role, runtime=runtime, limit=config.token_limit(role), label=label)
    budget.require(call_id, budget_usd, limit=config.daily_budget_usd, label=label)
    settled = False
    try:
        if runtime == codex_rt.RUNTIME:
            result = codex_rt.structured_call(
                prompt,
                schema,
                model=model,
                effort=effort,
                timeout_minutes=timeout_minutes,
                label=label,
                prices=config.codex_prices,
            )
        else:
            result = claude_rt.structured_call(
                prompt,
                schema,
                model=model,
                effort=effort,
                budget_usd=budget_usd,
                max_turns=max_turns,
                timeout_minutes=timeout_minutes,
                label=label,
            )
    except LlmError as exc:
        budget.settle(call_id, exc.cost_usd)
        settled = True
        _emit(
            label=label,
            runtime=runtime,
            role=role,
            model=model,
            repo=repo,
            item=item,
            cost_usd=exc.cost_usd,
            cost_estimated=runtime == codex_rt.RUNTIME,
            price_known=exc.cost_usd > 0 or runtime != codex_rt.RUNTIME,
            num_turns=exc.num_turns,
            usage=exc.usage,
            error=str(exc),
        )
        raise
    finally:
        if not settled:
            budget.settle(call_id, 0.0)
    budget.settle(call_id, result.cost_usd)
    _emit(
        label=label,
        runtime=runtime,
        role=role,
        model=model,
        repo=repo,
        item=item,
        cost_usd=result.cost_usd,
        cost_estimated=result.cost_estimated,
        price_known=result.price_known,
        num_turns=result.num_turns,
        usage=result.usage,
    )
    return result
