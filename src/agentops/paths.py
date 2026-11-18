"""运行状态目录（默认 ~/.agentops）的路径约定。"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "AGENTOPS_HOME"
DEFAULT_HOME = "~/.agentops"
STATE_SUBDIRS = ("workitems", "queue", "approvals", "slots", "inbox")


def home() -> Path:
    return Path(os.environ.get(ENV_HOME) or DEFAULT_HOME).expanduser()


def workitems_root() -> Path:
    return home() / "workitems"


def workitems_dir(repo: str) -> Path:
    return workitems_root() / repo


def queue_file(repo: str) -> Path:
    return home() / "queue" / f"{repo}.jsonl"


def approvals_dir() -> Path:
    return home() / "approvals"


def slots_dir() -> Path:
    return home() / "slots"


def inbox_dir() -> Path:
    return home() / "inbox"


def events_file() -> Path:
    return home() / "events.jsonl"


def sessions_file() -> Path:
    return home() / "sessions.jsonl"


def trials_file() -> Path:
    return home() / "trials.jsonl"


def config_file() -> Path:
    return home() / "config.toml"


def logs_dir(repo: str | None = None) -> Path:
    base = home() / "logs"
    return base if repo is None else base / repo


def steward_dir() -> Path:
    return home() / "steward"
