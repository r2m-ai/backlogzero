"""C4 判断层：只在 rebase 冲突、规则触发、测试失败时做一次无状态的结构化调用。它从不改代码，
也不能批准；最多只能在三个动作里选一个。LLM 不可用或输出不合法时退回到确定性的默认动作。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agentops import budget, events, llm
from agentops import repo as repo_tools
from agentops.config import Config
from agentops.workitem_model import WorkItem

ACTIONS = ("return_to_session", "needs_human", "retry_flaky")
TRIGGERS = ("rebase_conflict", "tests_fail", "rule_triggered")
SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "reason": {"type": "string"},
        "hint": {"type": "string"},
    },
    "required": ["action", "reason", "hint"],
    "additionalProperties": False,
}
GUIDANCE = {
    "rebase_conflict": (
        "冲突通常退回原会话让它 rebase；只有冲突涉及受保护文件或已经退回多次时才转人工。"
        "retry_flaky 不适用。"
    ),
    "tests_fail": (
        "看失败是代码问题（退回）还是偶发故障（retry_flaky）；"
        "反复失败或失败原因需要人决定时转人工。"
    ),
    "rule_triggered": (
        "规则触发默认转人工。只有当改动明显是无意的（例如误删函数、误改测试）、"
        "退回会话就能自行改正时才退回。retry_flaky 不适用。"
    ),
}
MAX_CONTEXT_CHARS = 6000


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    hint: str
    cost_usd: float
    source: str  # llm | default


def render_prompt(
    trigger: str, *, ref: str, title: str, module: str, returns: object, context: str
) -> str:
    """judge 提示词；tools/judge_consistency.py 也用它，离线重放同一批输入。"""
    template = (repo_tools.TEMPLATES / "prompts" / "judge.md").read_text(encoding="utf-8")
    values = {
        "trigger": trigger,
        "ref": ref,
        "title": title,
        "module": module,
        "returns": str(returns),
        "guidance": GUIDANCE[trigger],
        "context": context[-MAX_CONTEXT_CHARS:].replace("</untrusted>", "<\\/untrusted>"),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def _prompt(trigger: str, item: WorkItem, context: str) -> str:
    return render_prompt(
        trigger,
        ref=item.ref,
        title=item.title,
        module=item.module,
        returns=item.extra.get("returns", 0),
        context=context,
    )


def decide(config: Config, *, trigger: str, item: WorkItem, context: str, default: str) -> Decision:
    if trigger not in TRIGGERS:
        raise ValueError(f"未知触发原因 {trigger!r}")
    if default not in ACTIONS:
        raise ValueError(f"默认动作不合法 {default!r}")
    if not bool(config.steward.get("judge", True)):
        return Decision(default, "判断层已关闭", "", 0.0, "default")
    try:
        result = llm.call_structured(
            _prompt(trigger, item, context),
            SCHEMA,
            config=config,
            role="steward",
            label="judge",
            repo=item.repo,
            item=item.id,
        )
    except (llm.LlmError, budget.BudgetExceeded) as exc:
        events.emit(
            "judge_error",
            repo=item.repo,
            item=item.id,
            detail={"trigger": trigger, "error": str(exc), "default": default},
        )
        return Decision(default, f"判断层出错，按默认处理：{exc}", "", 0.0, "default")
    action = str(result.data.get("action", ""))
    if action not in ACTIONS or (action == "retry_flaky" and trigger != "tests_fail"):
        events.emit(
            "judge_error",
            repo=item.repo,
            item=item.id,
            detail={"trigger": trigger, "error": f"动作不合法 {action!r}", "default": default},
        )
        return Decision(default, "判断层给出不合法动作，按默认处理", "", result.cost_usd, "default")
    decision = Decision(
        action,
        str(result.data.get("reason", "")),
        str(result.data.get("hint", "")),
        result.cost_usd,
        "llm",
    )
    events.emit(
        "judge_call",
        repo=item.repo,
        item=item.id,
        detail={
            "trigger": trigger,
            "action": action,
            "reason": decision.reason[:300],
            "cost_usd": result.cost_usd,
        },
    )
    return decision
