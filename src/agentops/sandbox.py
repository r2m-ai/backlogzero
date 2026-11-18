"""steward 跑候选代码（prepare_cmd、test_cmd）时的沙箱：macOS 自带的 sandbox-exec（Seatbelt）。

候选分支里的测试可以是任意代码。它只能写集成副本、临时目录、uv 缓存；
写不了 ~/.agentops、主仓库、克隆、套件、Claude 配置和钥匙串；不能联网。
规则顺序：先 allow default，再 deny，最后用 allow 放行子目录（Seatbelt 以最后匹配的规则为准）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def available() -> bool:
    return os.access(SANDBOX_EXEC, os.X_OK) and shutil.which("sandbox-exec") is not None


def _rule(verb: str, paths: Sequence[Path]) -> str:
    entries = " ".join(f'(subpath "{_escape(p)}")' for p in paths if str(p))
    return f"({verb} file-write* {entries})" if entries else ""


def _escape(path: Path) -> str:
    return str(path.expanduser().resolve()).replace("\\", "\\\\").replace('"', '\\"')


def profile(
    *, allow_write: Sequence[Path], deny_write: Sequence[Path], allow_network: bool = False
) -> str:
    rules = ["(version 1)", "(allow default)"]
    if not allow_network:
        rules.append("(deny network*)")
    rules.append(_rule("deny", deny_write))
    rules.append(_rule("allow", allow_write))
    return "\n".join(rule for rule in rules if rule)


def default_deny(
    *, agentops_home: Path, main_repo: Path, clones_root: Path, suite_root: Path
) -> tuple[Path, ...]:
    home = Path.home()
    return (
        agentops_home,
        main_repo,
        clones_root,
        suite_root,
        home / ".claude",
        home / ".claude.json",
        home / "Library" / "Keychains",
        home / ".ssh",
        home / ".local" / "bin",
    )


def default_allow(*, workdir: Path, log_dir: Path, run_tmp: Path) -> tuple[Path, ...]:
    """只放行本次运行专用的临时目录（作为候选代码的 TMPDIR），不放行整个系统临时目录：
    系统临时目录可能装着别的东西（测试里的临时仓库就在那），整目录放行会把 deny 重新打开。"""
    return (
        workdir,
        log_dir,
        run_tmp,
        Path("/private/tmp"),
        Path("/tmp"),
        Path.home() / ".cache" / "uv",
    )


def make_run_tmp(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"agentops-{label}-"))


def wrap(command: Sequence[str], profile_text: str) -> list[str]:
    return [SANDBOX_EXEC, "-p", profile_text, *command]
