"""runtime 适配层的公共类型：运行时名、角色、token 用量、一次结构化调用的结果与错误。
后端模块（claude、codex）各自实现 structured_call，llm.py 按仓库配置分发。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agentops.errors import AgentopsError

RUNTIMES = ("claude", "codex")
DEFAULT_RUNTIME = "claude"
ROLES = ("worker", "steward", "pm")


class LlmError(AgentopsError):
    """后端调用失败。失败也可能已经花了钱、用了 token，带回来让分发层结算与记账。"""

    def __init__(
        self,
        message: str,
        *,
        cost_usd: float = 0.0,
        usage: Usage | None = None,
        num_turns: int = 0,
    ):
        super().__init__(message)
        self.cost_usd = cost_usd
        self.usage = usage
        self.num_turns = num_turns


@dataclass(frozen=True)
class Usage:
    """Codex `turn.completed.usage` 的五项；Claude 后端不报 token，用 None 表示。"""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total(self) -> int:
        """预算口径：输入 + 输出（cached 是 input 的子集，reasoning 是 output 的子集）。"""
        return self.input_tokens + self.output_tokens

    @property
    def budget_tokens(self) -> int:
        """日上限口径：未缓存输入 + 输出（不计缓存命中；cached 比 input 大的异常数据按 0 处理）。"""
        return max(self.input_tokens - self.cached_input_tokens, 0) + self.output_tokens

    def plus(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_input_tokens + other.cache_write_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Usage:
        def count(key: str) -> int:
            value = data.get(key, 0)
            return (
                int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0
            )

        return cls(
            count("input_tokens"),
            count("cached_input_tokens"),
            count("cache_write_input_tokens"),
            count("output_tokens"),
            count("reasoning_output_tokens"),
        )


@dataclass(frozen=True)
class StructuredResult:
    """一次结构化调用的统一结果。

    cost_estimated 为真表示美元数是本地价格表的估算（Codex 订阅）；price_known 为假表示价格表里
    没有这个模型，cost_usd 记 0，只剩 token 上限兜底。
    """

    data: Mapping[str, object]
    runtime: str
    cost_usd: float
    cost_estimated: bool
    price_known: bool
    num_turns: int
    usage: Usage | None
    session_id: str | None
    raw: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class WorkerResult:
    """一次 worker 会话的统一结果（ao start 收尾时用）。

    session_id 是运行时自己的会话 id：Claude 的 session_id（--fork-session 后会变），
    Codex 的 thread id。ao 这边的会话 id 由 ao start 自己管。
    """

    session_id: str | None
    is_error: bool
    cost_usd: float
    cost_estimated: bool
    price_known: bool
    num_turns: int
    usage: Usage | None
    summary: str
