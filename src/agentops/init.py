"""ao init：建立 ~/.agentops 目录结构和默认 config.toml，可重复运行。"""

from __future__ import annotations

from agentops.config import DEFAULT_CONFIG_TOML, load_config
from agentops.paths import (
    STATE_SUBDIRS,
    config_file,
    events_file,
    home,
    sessions_file,
    trials_file,
    workitems_dir,
)


def initialize() -> tuple[str, ...]:
    """创建缺失的目录和文件，返回本次新建的路径列表。"""
    created: list[str] = []
    root = home()
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
        created.append(str(root))
    for sub in STATE_SUBDIRS:
        path = root / sub
        if not path.exists():
            path.mkdir(parents=True, mode=0o700)
            created.append(str(path))
    config = config_file()
    if not config.exists():
        config.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        config.chmod(0o600)
        created.append(str(config))
    for repo in load_config().repos:
        path = workitems_dir(repo)
        if not path.exists():
            path.mkdir(parents=True, mode=0o700)
            created.append(str(path))
    for path in (events_file(), sessions_file(), trials_file()):
        if not path.exists():
            path.touch(mode=0o600)
            created.append(str(path))
    return tuple(created)
