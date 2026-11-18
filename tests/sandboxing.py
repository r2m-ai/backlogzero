"""测试是否已经在 Seatbelt 沙箱里（steward 跑候选测试、Claude Code 会话都在沙箱里）。

沙箱里：嵌套 sandbox-exec 被拒（rc 71）、不能绑 localhost 端口、写不了钥匙串。
依赖这些能力的测试在沙箱里跳过；被测的 steward 在沙箱里关掉自己的 sandbox，避免嵌套。
"""

from __future__ import annotations

import functools
import subprocess

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROBE_PROFILE = "(version 1)(allow default)"
SKIP_REASON = "已在沙箱里（steward 候选测试或会话）：嵌套沙箱、绑端口、写钥匙串都不可用"


@functools.cache
def inside_sandbox() -> bool:
    """能再套一层 sandbox-exec 就不在沙箱里；被拒（rc 71）说明已在。没有 sandbox-exec 当作不在。"""
    try:
        result = subprocess.run(
            [SANDBOX_EXEC, "-p", PROBE_PROFILE, "/usr/bin/true"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode != 0


def steward_sandbox_lines() -> tuple[str, ...]:
    """写进测试 world 的 config.toml [steward] 段：沙箱里关掉被测 steward 的 sandbox。"""
    return ("sandbox = false",) if inside_sandbox() else ()


def steward_sandbox_text() -> str:
    return "".join(f"{line}\n" for line in steward_sandbox_lines())
