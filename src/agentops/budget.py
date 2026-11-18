"""预算（C8 的每日上限）：从事件算当日已结算花费，用带文件锁的台账记录未结算的预留。

start、resume、判断层、PM 都先 reserve 再运行，结束后 settle；并发调用不能重复使用同一份余额。
"""

from __future__ import annotations

import fcntl
import json
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from agentops import events
from agentops.errors import AgentopsError
from agentops.paths import config_file, home
from agentops.runtime.base import Usage
from agentops.timeutil import now, now_iso, parse_iso

COST_EVENTS = ("session_end", "llm_call")
DEFAULT_STALE_HOURS = 12.0


class BudgetExceeded(AgentopsError):
    pass


def ledger_path() -> Path:
    return home() / "budget" / "ledger.json"


@contextmanager
def _locked() -> Iterator[dict[str, object]]:
    """机器级锁内读台账，退出时写回。"""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, object] = {"reservations": {}, "settled": []}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
                    if isinstance(loaded, dict):
                        data = {
                            "reservations": dict(loaded.get("reservations") or {}),
                            "settled": list(loaded.get("settled") or [])[-200:],
                        }
                except json.JSONDecodeError:
                    pass
            yield data
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def spend_on(day: date) -> float:
    total = 0.0
    for event in events.read_events():
        if event.type in COST_EVENTS and event.time.astimezone().date() == day:
            total += float(event.detail.get("cost_usd") or 0.0)  # type: ignore[arg-type]
    return round(total, 4)


def spend_today() -> float:
    return spend_on(now().date())


def _usage_total(detail: Mapping[str, object]) -> int:
    usage = detail.get("usage")
    return Usage.from_mapping(usage).budget_tokens if isinstance(usage, Mapping) else 0


def tokens_on(day: date, *, runtime: str, role: str) -> int:
    """某运行时某角色当日用掉的 token（未缓存输入 + 输出，不计缓存命中）。

    judge / PM 来自 llm_call 事件，worker 来自 session_end 事件，两者都带 runtime、role、usage。
    """
    return sum(
        _usage_total(event.detail)
        for event in events.read_events()
        if event.type in COST_EVENTS
        and event.detail.get("runtime") == runtime
        and event.detail.get("role") == role
        and event.time.astimezone().date() == day
    )


def tokens_today(*, runtime: str, role: str) -> int:
    return tokens_on(now().date(), runtime=runtime, role=role)


def require_tokens(role: str, *, runtime: str, limit: int, label: str) -> None:
    """Codex 侧的硬上限：当日该角色 token 用量到了上限就不再调用（订阅额度没有美元数可控）。

    用量不计缓存命中（未缓存输入 + 输出）。

    只是调用前的闸门，不做预留：并发的几次调用可能一起越过上限，最后一次也可能超出。
    """
    used = tokens_today(runtime=runtime, role=role)
    if used < limit:
        return
    events.emit(
        "budget_refused",
        detail={
            "label": label,
            "runtime": runtime,
            "role": role,
            "tokens_used": used,
            "token_limit": limit,
        },
    )
    raise BudgetExceeded(
        f"当日 {runtime} 的 {role} 已用 {used} token（不计缓存命中），"
        f"上限 {limit}：{label} 不再调用"
    )


def outstanding(data: Mapping[str, object] | None = None) -> float:
    if data is None:
        with _locked() as current:
            data = current
    reservations = data.get("reservations") or {}
    return round(sum(float(r.get("amount", 0.0)) for r in reservations.values()), 4)  # type: ignore[union-attr]


def available(limit: float) -> float:
    return round(limit - spend_today() - outstanding(), 4)


def reserve(call_id: str, amount: float, *, limit: float, label: str) -> bool:
    """锁内检查 已结算 + 未结算预留 + 本次 <= 上限；通过就持久化预留。失败返回 False，不抛。"""
    spent = spend_today()
    with _locked() as data:
        reservations: dict[str, dict[str, object]] = data["reservations"]  # type: ignore[assignment]
        held = sum(float(r.get("amount", 0.0)) for r in reservations.values())
        if spent + held + amount > limit + 1e-9:
            events.emit(
                "budget_refused",
                detail={
                    "call": call_id,
                    "label": label,
                    "amount": amount,
                    "spent": spent,
                    "held": held,
                    "limit": limit,
                },
            )
            return False
        reservations[call_id] = {"amount": amount, "label": label, "ts": now_iso()}
    return True


def require(call_id: str, amount: float, *, limit: float, label: str) -> None:
    if not reserve(call_id, amount, limit=limit, label=label):
        raise BudgetExceeded(
            f"当日预算不足：{label} 需要 ${amount:.2f}，上限 ${limit:.2f}，"
            f"已用 ${spend_today():.2f}，预留 ${outstanding():.2f}"
        )


def settle(call_id: str, actual: float) -> None:
    """释放预留并记录实际花费（实际花费的事件由调用方写）。没有预留也不报错。"""
    with _locked() as data:
        reservations: dict[str, dict[str, object]] = data["reservations"]  # type: ignore[assignment]
        held = reservations.pop(call_id, None)
        record = {"call": call_id, "actual": actual, "ts": now_iso()}
        record["reserved"] = (held or {}).get("amount")
        data["settled"].append(record)  # type: ignore[union-attr]


def release(call_id: str, reason: str) -> bool:
    """回滚没有真正花钱的预留：弹出预留、settled 里标 released 与原因、发 budget_released。

    没有这条预留就什么也不做，返回 False。
    """
    with _locked() as data:
        reservations: dict[str, dict[str, object]] = data["reservations"]  # type: ignore[assignment]
        held = reservations.pop(call_id, None)
        if held is None:
            return False
        data["settled"].append(  # type: ignore[union-attr]
            {
                "call": call_id,
                "actual": 0.0,
                "ts": now_iso(),
                "reserved": held.get("amount"),
                "released": True,
                "reason": reason,
            }
        )
    events.emit(
        "budget_released",
        detail={
            "call": call_id,
            "amount": held.get("amount"),
            "label": held.get("label"),
            "reason": reason,
        },
    )
    return True


def stale_hours() -> float:
    """config.toml 的 [budget] stale_hours，缺省 DEFAULT_STALE_HOURS（config 里不必写）。"""
    path = config_file()
    if not path.exists():
        return DEFAULT_STALE_HOURS
    try:
        section = tomllib.loads(path.read_text(encoding="utf-8")).get("budget") or {}
        return float(section.get("stale_hours", DEFAULT_STALE_HOURS))
    except (tomllib.TOMLDecodeError, TypeError, ValueError, AttributeError):
        return DEFAULT_STALE_HOURS


def reservations() -> dict[str, dict[str, object]]:
    with _locked() as data:
        return dict(data["reservations"])  # type: ignore[arg-type]


def stale_reservations(max_age_hours: float) -> dict[str, dict[str, object]]:
    """预留时间超过 max_age_hours 的预留（ts 无法解析的也算陈旧）。"""
    cutoff = now() - timedelta(hours=max_age_hours)
    stale = {}
    for call_id, held in reservations().items():
        try:
            is_stale = parse_iso(str(held.get("ts"))) < cutoff
        except ValueError:
            is_stale = True
        if is_stale:
            stale[call_id] = held
    return stale


def _session_of(call_id: str) -> str:
    return call_id.split(":", 1)[0]


def release_stale(max_age_hours: float | None = None) -> tuple[str, ...]:
    """释放陈旧且对应会话已结束（不在 sessions 里或不是 running）的预留，返回释放的 call_id。"""
    from agentops import sessions  # 延迟导入，避免循环依赖

    hours = stale_hours() if max_age_hours is None else max_age_hours
    latest = sessions.latest_by_id()
    released = []
    for call_id in stale_reservations(hours):
        record = latest.get(_session_of(call_id))
        if record is not None and record.status == sessions.RUNNING:
            continue
        if release(call_id, f"陈旧预留（超过 {hours:g} 小时，会话已结束）"):
            released.append(call_id)
    return tuple(released)
