"""hooks 共用：读 stdin JSON、找项目根、读 .pipeline.toml、认运行时与会话、调用 ao。

只依赖标准库，python3.12。Claude Code 与 Codex 的 hook 都跑这些脚本：stdin 字段大体相同，
差别在工具名（Codex 是 Bash / apply_patch）、会话 id（Codex 给的是 thread id）和环境变量
（Codex 不保证把 ao start 导出的 AGENTOPS_* 传给 hook 进程，所以另有一份按克隆路径查的标记文件）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

AO = os.environ.get("AGENTOPS_AO") or os.path.expanduser("~/.local/bin/ao")
DEFAULT_PROTECTED = ("AGENTS.md", "CLAUDE.md", ".pipeline.toml", "justfile", ".claude/", ".codex/")
DEFAULT_EXISTING_ONLY = ("tests/", "fixtures/")
RUNTIMES = ("claude", "codex")
EMIT_TIMEOUT = 15
MARKER_DIGEST_CHARS = 24  # 与 agentops.runtime.codex_worker.marker_path 保持一致
PROJECT_WALK_LIMIT = 20
GRANT_KEY = "allow_test_edits"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")  # 传给 ao 的仓库名、会话 id 只认这种
UNKNOWN_REPO = "unknown"
# 克隆的 .git 是会话可写的：在沙箱外对它跑 git 时，不让它的配置带起外部程序
GIT_SAFE_OPTIONS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


@dataclass(frozen=True)
class Context:
    """一次 hook 调用的会话上下文。session 是 ao 这边认的会话 id。"""

    project: Path
    base: Path  # 相对路径按它解析：Codex 是这次调用的 cwd，Claude 是项目根
    runtime: str
    session: str | None
    item: str | None
    mode: str
    managed: bool  # 由 ao start 起的会话：ao 自己记 session_start / session_end
    marker: dict  # ao start 写的克隆标记（没有就是空）；里面带拦截策略的快照


def read_input() -> dict:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _repo_root(start: Path) -> Path:
    """从 start 往上找带 .git 的目录；找不到就用 start 本身。"""
    current = start
    for _ in range(PROJECT_WALK_LIMIT):
        if (current / ".git").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return start


def project_dir(data: dict) -> Path:
    """项目根（克隆根）。CLI 给的 cwd 可能是子目录，所以往上找 .git，而不是直接用 cwd。"""
    explicit = os.environ.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _repo_root(Path(data.get("cwd") or os.getcwd()).expanduser().resolve())


def safe_name(value: object) -> str | None:
    text = str(value) if value is not None else ""
    return text if SAFE_NAME.match(text) else None


def agentops_home() -> Path:
    return Path(os.environ.get("AGENTOPS_HOME") or "~/.agentops").expanduser()


def marker_path(project: Path) -> Path:
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:MARKER_DIGEST_CHARS]
    return agentops_home() / "sessions" / "by-clone" / f"{digest}.json"


def load_marker(project: Path) -> dict:
    """ao start 为 Codex 会话写的标记（会话写不了这个目录）。没有或读不了就当没有。"""
    try:
        data = json.loads(marker_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def detect_runtime(data: dict, marker: dict) -> str:
    """先信 ao 导出的变量，再信 CLI 自己给的线索（会话记录文件、工具名），最后才看克隆标记。

    标记排最后：同一克隆路径上留下的旧标记不能把一个 Claude 会话认成 Codex。
    """
    env = os.environ.get("AGENTOPS_RUNTIME")
    if env in RUNTIMES:
        return env
    transcript = str(data.get("transcript_path") or "")
    if Path(transcript).name.startswith("rollout-"):
        return "codex"
    if "/.claude/" in transcript:
        return "claude"
    if data.get("tool_name") == "apply_patch":
        return "codex"
    if marker.get("runtime") in RUNTIMES:
        return str(marker["runtime"])
    return "claude"


def _call_dir(data: dict, project: Path) -> Path:
    """这次工具调用的 cwd；不在项目里（或读不出来）就退回项目根。"""
    try:
        cwd = Path(str(data.get("cwd") or project)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return project
    return cwd if cwd == project or project in cwd.parents else project


def context(data: dict) -> Context:
    project = project_dir(data)
    marker = load_marker(project)
    runtime = detect_runtime(data, marker)
    hook_session = data.get("session_id") or None
    env_session = os.environ.get("AGENTOPS_SESSION") or None
    if runtime != "codex":
        return Context(
            project=project,
            base=project,
            marker={},
            runtime=runtime,
            session=hook_session,
            item=os.environ.get("AGENTOPS_ITEM") or None,
            mode=os.environ.get("AGENTOPS_MODE") or "interactive",
            managed=bool(env_session),
        )
    # Codex 的 session_id 是 thread id；ao start 起的会话一律用 ao 自己的会话 id
    ao_session = env_session or (str(marker["session"]) if marker.get("session") else None)
    item = os.environ.get("AGENTOPS_ITEM") or marker.get("item")
    return Context(
        project=project,
        base=_call_dir(data, project),
        marker=marker,
        runtime=runtime,
        session=ao_session or hook_session,
        item=None if item is None else str(item),
        mode=os.environ.get("AGENTOPS_MODE") or str(marker.get("mode") or "interactive"),
        managed=bool(ao_session),
    )


def _string_list(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _marker_policy(project: Path, marker: dict) -> dict | None:
    """ao start 从 main 上读出来、写进标记的策略快照。会话改不了它（标记在沙箱可写范围之外）。"""
    snapshot = marker.get("policy")
    if not isinstance(snapshot, dict):
        return None
    protected = _string_list(snapshot.get("protected"))
    existing_only = _string_list(snapshot.get("existing_only"))
    branch = snapshot.get("default_branch")
    if protected is None or existing_only is None or not isinstance(branch, str) or not branch:
        return None
    return {
        "repo": safe_name(marker.get("repo")) or safe_name(project.name) or UNKNOWN_REPO,
        "default_branch": branch,
        "protected": protected,
        "existing_only": existing_only,
        # 起会话时 main 的提交：克隆里的 main 引用会话是能动的，提交号动不了
        "ref": safe_name(marker.get("base_sha")) or branch,
    }


def load_policy(project: Path, marker: dict | None = None) -> dict:
    """拦截策略。有标记快照就用快照；否则读克隆里的 .pipeline.toml，读不到用默认值。

    读文件的路径不让配置问题把 hook 弄崩；它的弱点是文件在会话可写的克隆里，
    所以 ao start 起的 Codex 会话都走快照。
    """
    snapshot = _marker_policy(project, marker or {})
    if snapshot is not None:
        return snapshot
    fallback_repo = safe_name(project.name) or UNKNOWN_REPO
    policy = {
        "repo": fallback_repo,
        "default_branch": "main",
        "protected": list(DEFAULT_PROTECTED),
        "existing_only": list(DEFAULT_EXISTING_ONLY),
        "ref": "main",
    }
    path = project / ".pipeline.toml"
    if not path.exists():
        return policy
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"hook 警告: {path} 解析失败，使用默认策略: {exc}", file=sys.stderr)
        return policy
    repo = data.get("repo", {}) if isinstance(data.get("repo"), dict) else {}
    protected = data.get("protected", {}) if isinstance(data.get("protected"), dict) else {}
    branch = str(repo.get("default_branch") or "main")
    return {
        "repo": safe_name(repo.get("name")) or fallback_repo,
        "default_branch": branch,
        "protected": list(protected.get("paths") or policy["protected"]),
        "existing_only": list(protected.get("existing_only") or policy["existing_only"]),
        "ref": branch,
    }


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _front_matter_lines(text: str) -> list[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return lines[1:end]
    return []


def _grant_values(lines: list[str]) -> list[str]:
    """从 front matter 里取 allow_test_edits：支持块列表和 `[a, b]` 行内写法。"""
    for index, line in enumerate(lines):
        if not line.startswith(GRANT_KEY + ":"):
            continue
        rest = line[len(GRANT_KEY) + 1 :].strip()
        if rest.startswith("[") and rest.endswith("]"):
            return [_unquote(v) for v in rest[1:-1].split(",") if v.strip()]
        if rest:
            return []
        values = []
        for follow in lines[index + 1 :]:
            if not follow.lstrip().startswith("- "):
                break
            values.append(_unquote(follow.lstrip()[2:]))
        return values
    return []


def load_test_grants(repo: str, item: str | None) -> tuple[str, ...]:
    """工作项里人填的、授权会话修改的已有测试文件清单（只认精确的相对路径）。

    工作项文件在 ~/.agentops 里，会话写不了；读不到、格式不对一律当没有授权。
    """
    if not item or not item.isdigit() or not safe_name(repo):
        return ()
    path = agentops_home() / "workitems" / repo / f"{int(item):03d}.md"
    try:
        lines = _front_matter_lines(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ()
    granted = []
    for value in _grant_values(lines):
        parts = value.split("/")
        if value and not value.startswith("/") and all(p not in ("", ".", "..") for p in parts):
            granted.append(value)
    return tuple(granted)


def session_item_args(item: str | None = None) -> list[str]:
    value = item if item is not None else os.environ.get("AGENTOPS_ITEM")
    return ["--item", value] if value and value.isdigit() else []


def emit(
    event_type: str,
    *,
    repo: str,
    session: str | None,
    kv: dict[str, str],
    item: str | None = None,
    timeout: float = EMIT_TIMEOUT,
) -> None:
    """写事件；ao 不在或失败只警告，不影响 hook 的判定。"""
    args = [AO, "events", "emit", event_type, "--repo", repo, *session_item_args(item)]
    if safe_name(session):
        args += ["--session", str(session)]
    for key, value in kv.items():
        args += ["--kv", f"{key}={value}"]
    try:
        subprocess.run(args, check=False, timeout=timeout, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"hook 警告: 写事件 {event_type} 失败: {exc}", file=sys.stderr)


def git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project), *GIT_SAFE_OPTIONS, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
