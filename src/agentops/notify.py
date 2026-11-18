"""桌面通知：免打扰时段只记事件；其余时间用 osascript 弹 macOS 通知，失败不影响流程。"""

from __future__ import annotations

import subprocess
from datetime import datetime

from agentops import events
from agentops.config import Config
from agentops.timeutil import now


def in_quiet_hours(config: Config, at: datetime | None = None) -> bool:
    start_text, end_text = config.quiet_hours
    current = (at or now()).strftime("%H:%M")
    if start_text <= end_text:
        return start_text <= current < end_text
    return current >= start_text or current < end_text  # 跨午夜


def notify(
    config: Config, title: str, message: str, *, repo: str | None = None, item: int | None = None
) -> bool:
    quiet = in_quiet_hours(config)
    sent = False
    if not quiet:
        script = f"display notification {_osa(message)} with title {_osa(title)}"
        try:
            sent = (
                subprocess.run(
                    ["osascript", "-e", script], capture_output=True, timeout=10
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            sent = False
    events.emit(
        "notification",
        repo=repo,
        item=item,
        detail={"title": title, "message": message, "quiet": quiet, "sent": sent},
    )
    return sent


def _osa(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
