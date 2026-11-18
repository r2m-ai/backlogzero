"""业务仓库接入：复制模板、装 git 钩子（main 保护）。"""

from __future__ import annotations

import shutil
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agentops.errors import AgentopsError
from agentops.runtime.base import DEFAULT_RUNTIME, RUNTIMES
from agentops.workitem_model import is_valid_repo

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"
GIT_HOOKS = ("pre-commit", "pre-merge-commit", "reference-transaction")
# 模板文件 → 仓库里的位置
PROJECT_FILES = (
    ("AGENTS.md", "AGENTS.md"),
    ("CLAUDE.md", "CLAUDE.md"),
    ("pipeline.toml", ".pipeline.toml"),
    ("claude/settings.json", ".claude/settings.json"),
    ("justfile", "justfile"),
)
# 运行时专属的项目文件：Codex 的 hooks 配置（四个事件与 .claude/settings.json 里的一一对应）
RUNTIME_FILES = {"codex": (("codex/hooks.json", ".codex/hooks.json"),)}


@dataclass(frozen=True)
class RepoInitResult:
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    hooks: tuple[str, ...]


def render(text: str, **values: str) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=False, timeout=30
        )
    except OSError as exc:
        raise AgentopsError(f"无法运行 git: {exc}") from exc


def git_common_dir(path: Path) -> Path:
    result = _git(path, "rev-parse", "--git-common-dir")
    if result.returncode != 0:
        raise AgentopsError(f"{path} 不是 git 仓库: {result.stderr.strip()}")
    common = Path(result.stdout.strip())
    return common if common.is_absolute() else (path / common).resolve()


def ensure_git_repo(path: Path, default_branch: str = "main") -> bool:
    """没有 .git 就 git init -b main。返回是否新建。"""
    if _git(path, "rev-parse", "--git-dir").returncode == 0:
        return False
    result = _git(path, "init", "-q", "-b", default_branch)
    if result.returncode != 0:
        raise AgentopsError(f"git init 失败: {result.stderr.strip()}")
    return True


def install_git_hooks(path: Path) -> tuple[str, ...]:
    """把 main 保护钩子装进 .git/hooks（worktree 共用），并关掉自动 gc。"""
    hooks_dir = git_common_dir(path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    installed = []
    for name in GIT_HOOKS:
        source = TEMPLATES / "git-hooks" / name
        if not source.exists():
            raise AgentopsError(f"缺少模板 {source}")
        target = hooks_dir / name
        shutil.copyfile(source, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(str(target))
    _git(path, "config", "gc.auto", "0")
    return tuple(installed)


def init_repo(
    path: Path, name: str, *, force: bool = False, runtime: str = DEFAULT_RUNTIME
) -> RepoInitResult:
    if not is_valid_repo(name):
        raise AgentopsError(f"仓库名不合法: {name!r}")
    if runtime not in RUNTIMES:
        raise AgentopsError(f"runtime 必须是 {' / '.join(RUNTIMES)}，得到 {runtime!r}")
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    ensure_git_repo(path)
    written, skipped = [], []
    for template_name, target_name in (*PROJECT_FILES, *RUNTIME_FILES.get(runtime, ())):
        source = TEMPLATES / template_name
        target = path / target_name
        if target.exists() and not force:
            skipped.append(target_name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(source.read_text(encoding="utf-8"), repo=name), encoding="utf-8")
        written.append(target_name)
    hooks = install_git_hooks(path)
    return RepoInitResult(tuple(written), tuple(skipped), hooks)


SESSION_SETTINGS_TEMPLATE = TEMPLATES / "session-settings.json"


def render_session_settings(
    repo_name: str, clone_path: Path, *, main_repo: Path, other_clones: Sequence[Path] = ()
) -> str:
    """按仓库和克隆渲染 ao start 要传给 --settings 的 JSON。

    会话只能写自己的克隆（cwd）。主仓库和其他克隆在 cwd 之外本就不可写，
    这里再逐个 denyWrite 作双保险。
    """
    import json

    raw = SESSION_SETTINGS_TEMPLATE.read_text(encoding="utf-8")
    rendered = render(raw, repo=repo_name, main_repo=str(main_repo.resolve()))
    data = json.loads(rendered)
    data.pop("_comment", None)
    deny = list(data["sandbox"]["filesystem"]["denyWrite"])
    own = clone_path.resolve()
    for other in other_clones:
        resolved = other.resolve()
        if resolved != own and str(resolved) not in deny:
            deny.append(str(resolved))
    data["sandbox"]["filesystem"]["denyWrite"] = deny
    return json.dumps(data, ensure_ascii=False, indent=2)
