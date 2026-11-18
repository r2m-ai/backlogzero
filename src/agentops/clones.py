"""每个工作项一份独立 git 克隆（代替 worktree）。

目录 <clones>/<repo>/<id>-<slug>，分支 <type>/<id>-<slug>。

会话的沙箱只能写自己的克隆；主仓库和别的克隆一律不可写。合入或放弃后删除克隆。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentops import events
from agentops import repo as repo_tools
from agentops.config import Config
from agentops.errors import AgentopsError
from agentops.workitem_model import WorkItem

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CLONE_DIR_RE = re.compile(r"^(\d{3,})-[a-z0-9-]+$")
SESSION_GIT_NAME = "agentops-session"
# 克隆的 .git 是会话可写的。ao 在沙箱外对克隆跑 git 时不让克隆自己的配置带起外部程序；
# 另外约定：沙箱外只对克隆跑不刷新索引的查询（rev-parse、branch、ls-tree），不跑 status / diff。
UNTRUSTED_GIT_OPTIONS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
SESSION_GIT_EMAIL = "session@localhost"


@dataclass(frozen=True)
class CloneInfo:
    repo: str
    item: int
    path: Path
    branch: str


def slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:40] or _SLUG_RE.sub("-", fallback.lower()).strip("-")[:40] or "item"


def branch_name(item: WorkItem) -> str:
    return f"{item.type}/{item.id}-{slugify(item.title, item.module)}"


def clone_dir(config: Config, item: WorkItem) -> Path:
    return config.clones_root() / item.repo / f"{item.id:03d}-{slugify(item.title, item.module)}"


def parse_clone_path(path: Path, config: Config) -> tuple[str, int] | None:
    """从 <clones>/<repo>/<id>-<slug> 反推 (repo, id)。"""
    try:
        rel = path.resolve().relative_to(config.clones_root().resolve())
    except ValueError:
        return None
    if len(rel.parts) < 2 or not _CLONE_DIR_RE.match(rel.parts[1]):
        return None
    return rel.parts[0], int(rel.parts[1].split("-", 1)[0])


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *UNTRUSTED_GIT_OPTIONS, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except OSError as exc:
        raise AgentopsError(f"无法运行 git: {exc}") from exc


def create_clone(config: Config, item: WorkItem, *, default_branch: str = "main") -> CloneInfo:
    """从主仓库做本地克隆（硬链接对象），建工作分支，装钩子，设本地身份。已存在则报错。"""
    main_repo = config.repo_path(item.repo)
    if not (main_repo / ".git").exists():
        raise AgentopsError(f"主仓库 {main_repo} 不是 git 仓库")
    target = clone_dir(config, item)
    if target.exists():
        raise AgentopsError(f"克隆已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(
        ["git", "clone", "-q", "--branch", default_branch, str(main_repo), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if cloned.returncode != 0:
        raise AgentopsError(f"git clone 失败: {cloned.stderr.strip()}")
    branch = branch_name(item)
    for args in (
        ("checkout", "-q", "-b", branch),
        ("config", "user.name", SESSION_GIT_NAME),
        ("config", "user.email", SESSION_GIT_EMAIL),
        ("config", "gc.auto", "0"),
    ):
        result = _git(target, *args)
        if result.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            raise AgentopsError(f"克隆初始化失败（git {' '.join(args)}）: {result.stderr.strip()}")
    repo_tools.install_git_hooks(target)
    info = CloneInfo(item.repo, item.id, target, branch)
    events.emit(
        "clone_created",
        repo=item.repo,
        item=item.id,
        detail={"path": str(target), "branch": branch},
    )
    return info


def remove_clone(config: Config, item: WorkItem, *, reason: str) -> bool:
    target = clone_dir(config, item)
    if not target.exists():
        return False
    shutil.rmtree(target)
    events.emit(
        "clone_removed",
        repo=item.repo,
        item=item.id,
        detail={"path": str(target), "reason": reason},
    )
    return True


def list_clones(config: Config, repo: str) -> tuple[Path, ...]:
    root = config.clones_root() / repo
    if not root.exists():
        return ()
    return tuple(sorted(p for p in root.iterdir() if p.is_dir() and _CLONE_DIR_RE.match(p.name)))


def head_sha(path: Path) -> str:
    result = _git(path, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise AgentopsError(f"{path} 没有提交: {result.stderr.strip()}")
    return result.stdout.strip()


def ref_sha(path: Path, ref: str) -> str:
    result = _git(path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    if result.returncode != 0:
        raise AgentopsError(f"{path} 里没有 {ref}: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(path: Path) -> str:
    return _git(path, "branch", "--show-current").stdout.strip()


def is_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain").stdout.strip())
