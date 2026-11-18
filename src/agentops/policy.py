"""steward 的策略检查（C3 第 4 步、C6 第二道防线）。

规则：受保护路径有改动；existing_only 目录下 main 上已有的文件被修改或删除（Bash 改测试的兜底）；
删除 deny_delete_under 下的文件；删掉公开函数（diff 里有 -def 而无对应 +def）。策略以 main 上的
.pipeline.toml 为准，分支改不了它。
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agentops.errors import AgentopsError

_DEF_RE = re.compile(r"^[-+]def (\w+)\(")


@dataclass(frozen=True)
class Policy:
    repo: str
    default_branch: str = "main"
    autonomy: str = "auto"
    test_cmd: str = "just test-all"
    prepare_cmd: str | None = None
    max_sessions: int = 2
    protected: tuple[str, ...] = (
        "AGENTS.md",
        "CLAUDE.md",
        ".pipeline.toml",
        "justfile",
        ".claude/",
        ".codex/",
    )
    existing_only: tuple[str, ...] = ("tests/", "fixtures/")
    deny_delete_under: tuple[str, ...] = ("src/",)
    deny_public_function_removal: bool = True


@dataclass(frozen=True)
class Trigger:
    rule: str
    path: str
    detail: str


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except OSError as exc:
        raise AgentopsError(f"无法运行 git: {exc}") from exc


def parse_policy(text: str, repo_fallback: str) -> Policy:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AgentopsError(f".pipeline.toml 解析失败: {exc}") from exc
    repo = data.get("repo", {}) if isinstance(data.get("repo"), dict) else {}
    protected = data.get("protected", {}) if isinstance(data.get("protected"), dict) else {}
    rules = data.get("rules", {}) if isinstance(data.get("rules"), dict) else {}
    defaults = Policy(repo=repo_fallback)
    return Policy(
        repo=str(repo.get("name") or repo_fallback),
        default_branch=str(repo.get("default_branch") or defaults.default_branch),
        autonomy=str(repo.get("autonomy") or defaults.autonomy),
        test_cmd=str(repo.get("test_cmd") or defaults.test_cmd),
        prepare_cmd=(str(repo["prepare_cmd"]) if repo.get("prepare_cmd") else None),
        max_sessions=int(repo.get("max_sessions") or defaults.max_sessions),
        protected=tuple(protected.get("paths") or defaults.protected),
        existing_only=tuple(protected.get("existing_only") or defaults.existing_only),
        deny_delete_under=tuple(rules.get("deny_delete_under") or defaults.deny_delete_under),
        deny_public_function_removal=bool(
            rules.get("deny_public_function_removal", defaults.deny_public_function_removal)
        ),
    )


def load_policy(repo_path: Path, repo_name: str, ref: str = "main") -> Policy:
    """从 main 的树里读 .pipeline.toml，分支上的改动不算数。缺文件用默认值。"""
    shown = _git(repo_path, "show", f"{ref}:.pipeline.toml")
    if shown.returncode != 0:
        return Policy(repo=repo_name)
    return parse_policy(shown.stdout, repo_name)


def _covered(path: str, prefix: str) -> bool:
    prefix = prefix.strip()
    if prefix.endswith("/"):
        return path == prefix.rstrip("/") or path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def name_status(repo_path: Path, base: str, head: str) -> tuple[tuple[str, str, str | None], ...]:
    """(status, path, old_path)。重命名的 old_path 是改名前的路径。"""
    result = _git(repo_path, "diff", "--name-status", "-M", base, head)
    if result.returncode != 0:
        raise AgentopsError(f"git diff 失败: {result.stderr.strip()}")
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        status = parts[0][0]
        if status == "R" and len(parts) >= 3:
            entries.append((status, parts[2], parts[1]))
        elif len(parts) >= 2:
            entries.append((status, parts[1], None))
    return tuple(entries)


def removed_public_functions(repo_path: Path, base: str, head: str, path: str) -> tuple[str, ...]:
    diff = _git(repo_path, "diff", base, head, "--", path).stdout
    removed, added = set(), set()
    for line in diff.splitlines():
        match = _DEF_RE.match(line)
        if not match:
            continue
        (removed if line.startswith("-") else added).add(match.group(1))
    return tuple(sorted(name for name in removed - added if not name.startswith("_")))


_STATUS_WORDS = {"M": "修改", "D": "删除", "R": "改名", "T": "替换类型", "C": "复制"}


def _describe(status: str) -> str:
    return _STATUS_WORDS.get(status, f"改动（{status}）")


def check(repo_path: Path, base: str, head: str, policy: Policy) -> tuple[Trigger, ...]:
    triggers: list[Trigger] = []
    for status, path, old_path in name_status(repo_path, base, head):
        touched = [path] + ([old_path] if old_path else [])
        for candidate in touched:
            for prefix in policy.protected:
                if _covered(candidate, prefix):
                    triggers.append(
                        Trigger(
                            "protected_path", candidate, f"受保护路径 {prefix} 被改动（{status}）"
                        )
                    )
                    break
        if status != "A":  # 修改、删除、改名、类型替换（T，例如换成符号链接）、复制都算
            for candidate in touched:
                for prefix in policy.existing_only:
                    if _covered(candidate, prefix):
                        triggers.append(
                            Trigger(
                                "existing_test_modified",
                                candidate,
                                f"{prefix} 下 main 上已有的文件被{_describe(status)}（第二道防线）",
                            )
                        )
                        break
        if status == "D":
            for prefix in policy.deny_delete_under:
                if _covered(path, prefix):
                    triggers.append(Trigger("deleted_source", path, f"删除了 {prefix} 下的文件"))
                    break
        if (
            policy.deny_public_function_removal
            and status in {"M", "R"}
            and path.endswith(".py")
            and not any(_covered(path, p) for p in policy.existing_only)
        ):
            for name in removed_public_functions(repo_path, base, head, path):
                triggers.append(Trigger("public_function_removed", path, f"公开函数 {name} 被删除"))
    return tuple(_dedupe(triggers))


def _dedupe(triggers: list[Trigger]) -> list[Trigger]:
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for trigger in triggers:
        key = (trigger.rule, trigger.path, trigger.detail)
        if key not in seen:
            seen.add(key)
            unique.append(trigger)
    return unique


def needs_approval(policy: Policy, triggers: tuple[Trigger, ...]) -> bool:
    return policy.autonomy == "approve" or bool(triggers)


def summarize(triggers: tuple[Trigger, ...]) -> Mapping[str, object]:
    return {
        "rules": sorted({t.rule for t in triggers}),
        "paths": sorted({t.path for t in triggers}),
        "count": len(triggers),
    }
