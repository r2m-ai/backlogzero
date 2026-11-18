"""agentops#15：Codex 的 token 日上限不计缓存命中（未缓存输入 + 输出）。"""

import pytest

from agentops import budget, events
from agentops.runtime.base import Usage

LIMIT = 100_000


def _session_end(usage: dict[str, int]) -> None:
    events.emit("session_end", detail={"runtime": "codex", "role": "worker", "usage": usage})


def _refused() -> list[events.Event]:
    return [e for e in events.read_events() if e.type == "budget_refused"]


def test_budget_tokens_excludes_cached_input_while_total_is_unchanged():
    # Arrange
    usage = Usage(input_tokens=1728835, cached_input_tokens=1651072, output_tokens=4227)

    # Act / Assert
    assert usage.budget_tokens == 81990
    assert usage.total == 1733062


def test_tokens_today_counts_only_uncached_input_and_output(agentops_home):
    # Arrange
    _session_end({"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 50})
    _session_end({"input_tokens": 200, "cached_input_tokens": 0, "output_tokens": 30})

    # Act
    used = budget.tokens_today(runtime="codex", role="worker")

    # Assert
    assert used == (100 + 50) + (200 + 30)


def test_require_tokens_allows_large_cache_hits_below_uncached_limit(agentops_home):
    # Arrange
    _session_end(
        {"input_tokens": 5_000_000, "cached_input_tokens": 4_950_000, "output_tokens": 10_000}
    )

    # Act
    budget.require_tokens("worker", runtime="codex", limit=LIMIT, label="start")

    # Assert
    assert _refused() == []


def test_require_tokens_refuses_when_uncached_plus_output_reaches_limit(agentops_home):
    # Arrange
    _session_end({"input_tokens": 500_000, "cached_input_tokens": 420_000, "output_tokens": 20_000})

    # Act
    with pytest.raises(budget.BudgetExceeded, match="不计缓存命中"):
        budget.require_tokens("worker", runtime="codex", limit=LIMIT, label="start")

    # Assert
    refused = _refused()
    assert len(refused) == 1
    assert refused[0].detail["tokens_used"] == LIMIT
    assert refused[0].detail["token_limit"] == LIMIT


def test_cached_larger_than_input_never_goes_negative(agentops_home):
    # Arrange
    usage = Usage(input_tokens=100, cached_input_tokens=500, output_tokens=40)
    _session_end(usage.to_dict())

    # Act
    used = budget.tokens_today(runtime="codex", role="worker")

    # Assert
    assert usage.budget_tokens == 40
    assert used == 40


def test_events_without_cached_field_keep_the_old_total(agentops_home):
    # Arrange
    _session_end({"input_tokens": 700, "output_tokens": 60})

    # Act
    used = budget.tokens_today(runtime="codex", role="worker")

    # Assert
    assert used == 760
