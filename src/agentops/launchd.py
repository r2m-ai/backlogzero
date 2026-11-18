"""launchd（C9）：渲染 4 个 plist 到 ~/Library/LaunchAgents，生成安装/卸载/触发命令。

bootstrap 会添加后台登录项，按开场指令第 2 条由人自己执行；这里只渲染文件和打印命令。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.review import SUITE_ROOT

JOBS = ("steward", "board", "capture", "nightly")
TEMPLATE_DIR = SUITE_ROOT / "launchd"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
STAGING_DIR = (
    Path.home() / ".agentops" / "launchd"
)  # 渲染到这里；复制进 LaunchAgents 并 bootstrap 由人执行


@dataclass(frozen=True)
class Rendered:
    name: str
    label: str
    path: Path


def _hm(text: str) -> tuple[int, int]:
    try:
        hour, minute = text.split(":")
        return int(hour), int(minute)
    except ValueError as exc:
        raise AgentopsError(f"config [schedule] 的时间格式应为 HH:MM，得到 {text!r}") from exc


def render_all(config: Config, *, target_dir: Path | None = None) -> tuple[Rendered, ...]:
    home = str(Path.home())
    capture_h, capture_m = _hm(config.schedule["capture"])
    nightly_h, nightly_m = _hm(config.schedule["nightly"])
    values = {
        "home": home,
        "capture_hour": capture_h,
        "capture_minute": capture_m,
        "nightly_hour": nightly_h,
        "nightly_minute": nightly_m,
    }
    out_dir = target_dir or STAGING_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (Path.home() / ".agentops" / "logs" / "launchd").mkdir(parents=True, exist_ok=True)
    rendered = []
    for job in JOBS:
        label = f"com.agentops.{job}"
        text = (TEMPLATE_DIR / f"{label}.plist").read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        plistlib.loads(text.encode("utf-8"))  # 校验是合法 plist
        path = out_dir / f"{label}.plist"
        path.write_text(text, encoding="utf-8")
        rendered.append(Rendered(job, label, path))
    return tuple(rendered)


def domain() -> str:
    return f"gui/{os.getuid()}"


def install_commands(rendered: tuple[Rendered, ...]) -> list[str]:
    """复制到 ~/Library/LaunchAgents 再 bootstrap。

    放进 LaunchAgents 的文件会在登录时自动加载，所以由人执行。
    """
    commands = [f"mkdir -p {AGENTS_DIR}"]
    for r in rendered:
        commands.append(f"cp {r.path} {AGENTS_DIR}/{r.label}.plist")
        commands.append(f"launchctl bootstrap {domain()} {AGENTS_DIR}/{r.label}.plist")
    return commands


def uninstall_commands(rendered: tuple[Rendered, ...]) -> list[str]:
    commands = []
    for r in rendered:
        commands.append(f"launchctl bootout {domain()}/{r.label}")
        commands.append(f"rm -f {AGENTS_DIR}/{r.label}.plist")
    return commands


def kick_command(job: str) -> str:
    if job not in JOBS:
        raise AgentopsError(f"任务名只能是 {'/'.join(JOBS)}")
    return f"launchctl kickstart -k {domain()}/com.agentops.{job}"


def status(job: str) -> tuple[bool, str]:
    """用 launchctl print 看任务是否已加载；返回 (已加载, 摘要)。"""
    result = subprocess.run(
        ["launchctl", "print", f"{domain()}/com.agentops.{job}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, "未加载"
    summary = [
        line.strip()
        for line in result.stdout.splitlines()
        if any(k in line for k in ("state =", "pid =", "last exit code", "runs ="))
    ]
    return True, "；".join(summary[:4]) or "已加载"
