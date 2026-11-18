#!/usr/bin/env python3.12
"""PreToolUse hook，Claude Code 与 Codex 共用：
- 允许新增测试文件；
- 拒绝修改 tests/、fixtures/ 下 main 上已存在的文件（工作项 allow_test_edits 清单内的除外）；
- 拒绝修改 .pipeline.toml [protected].paths 里的路径；
- 拒绝时退出码 2，原因写 stderr（两边都会原样给模型看），并写 hook_block 事件。

看哪些工具：Claude 的 Edit / Write / NotebookEdit / MultiEdit（file_path）；Codex 的 apply_patch
（补丁里每个 `*** … File:` 与 `*** Move to:`，路径是绝对路径）和 Bash（只在 Codex 下，保守匹配）。

退出码不是 2 就等于放行，所以 Codex 下任何检查不了的情况都按拒绝处理：路径解析不了、命令太长、
脚本自己出错。这只是第一道；主防线是 steward 合入时按 main 上的策略对整个分支 diff 做的规则检查。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _common import (
    Context,
    context,
    detect_runtime,
    emit,
    git,
    load_policy,
    load_test_grants,
    read_input,
)
from _shell_targets import Unanalyzable, write_targets

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
PATCH_TOOL = "apply_patch"
SHELL_TOOL = "Bash"
PATCH_PATH = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.M)
SHOWN_PATH_CHARS = 200
GUIDANCE = (
    "测试和受保护文件只能由人改：tests/ 下可以新增文件，不能动 main 上已有的。"
    "如果不改它就做不完，说明规格有冲突：把冲突写进最后的回复然后停下，"
    "不要 ao land，也不要换别的办法改。"
)


class Unresolvable(Exception):
    """路径没法解析成确定的位置（符号链接成环、非法字符）。"""


def _covered(rel: str, prefix: str) -> bool:
    prefix = prefix.strip()
    if prefix.endswith("/"):
        return rel == prefix.rstrip("/") or rel.startswith(prefix)
    return rel == prefix or rel.startswith(prefix + "/")


def exists_on_branch(project: Path, rel: str, branch: str) -> bool | None:
    """文件在 branch 上是否存在。查不到 branch 或 git 出错返回 None，由调用方拒绝。"""
    if git(project, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}").returncode != 0:
        return None
    listed = git(project, "ls-tree", "--name-only", branch, "--", rel)
    if listed.returncode != 0:
        return None
    return bool(listed.stdout.strip())


def decide(rel: str, policy: dict, project: Path, granted: tuple[str, ...] = ()) -> str | None:
    for prefix in policy["protected"]:
        if _covered(rel, prefix):
            return f"受保护路径 {prefix}，改动需要人批准"
    for prefix in policy["existing_only"]:
        if not _covered(rel, prefix):
            continue
        existed = exists_on_branch(project, rel, policy.get("ref") or policy["default_branch"])
        if existed is None:
            return (
                f"无法确认 {rel} 在 {policy['default_branch']} 上是否已存在（分支不可读），先拒绝"
            )
        if existed and rel in granted:
            continue
        if existed:
            reason = f"{prefix} 下 {policy['default_branch']} 上已有的文件只能新增不能修改"
            if granted:
                reason += f"；{rel} 不在工作项的授权清单内（只有 {', '.join(granted)} 可改）"
            return reason
    return None


def _command_text(tool_input: dict) -> str:
    command = tool_input.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def raw_targets(tool: str, tool_input: dict, ctx: Context) -> list[str]:
    """这次工具调用要写的路径（原样，未解析）。不归这个 hook 管的工具返回空。"""
    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        return [str(raw)] if raw else []
    if tool == PATCH_TOOL:
        return PATCH_PATH.findall(_command_text(tool_input))
    if tool == SHELL_TOOL and ctx.runtime == "codex":
        return write_targets(_command_text(tool_input))
    return []


def relative_to_project(raw: str, project: Path, base: Path) -> str | None:
    """解析成项目内的相对路径；项目外的路径由权限规则处理，返回 None。解析不了抛 Unresolvable。"""
    try:
        target = Path(raw).expanduser()
    except RuntimeError:  # `~不存在的用户/…`：按字面路径算
        target = Path(raw)
    try:
        target = (target if target.is_absolute() else base / target).resolve()
    except (RuntimeError, OSError, ValueError) as exc:
        raise Unresolvable(str(exc)) from exc
    try:
        return target.relative_to(project).as_posix()
    except ValueError:
        return None


def _deny(ctx: Context, policy: dict, tool: str, path: str, reason: str) -> int:
    shown = path[:SHOWN_PATH_CHARS]
    emit(
        "hook_block",
        repo=policy["repo"],
        session=ctx.session,
        item=ctx.item,
        kv={"path": shown, "tool": tool, "reason": reason, "runtime": ctx.runtime},
    )
    print(f"protect_tests: 拒绝 {tool} {shown}: {reason}。{GUIDANCE}", file=sys.stderr)
    return 2


def check(data: dict) -> int:
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return 0
    ctx = context(data)
    policy = load_policy(ctx.project, ctx.marker)
    try:
        targets = raw_targets(tool, tool_input, ctx)
    except Unanalyzable as exc:
        return _deny(ctx, policy, tool, "（整条命令）", f"命令没法检查（{exc}），请拆成几条短命令")
    granted = load_test_grants(policy["repo"], ctx.item) if targets else ()
    for raw in targets:
        try:
            rel = relative_to_project(raw, ctx.project, ctx.base)
        except Unresolvable:
            return _deny(ctx, policy, tool, raw, "路径解析不了（符号链接成环或含非法字符），先拒绝")
        if rel is None:
            continue
        reason = decide(rel, policy, ctx.project, granted)
        if reason is not None:
            return _deny(ctx, policy, tool, rel, reason)
    return 0


def _must_fail_closed(data: dict) -> bool:
    """Codex 的调用（补丁、shell）出了意外也要拒绝；Claude 的行为保持原样。"""
    return data.get("tool_name") in (PATCH_TOOL, SHELL_TOOL) or detect_runtime(data, {}) == "codex"


def main() -> int:
    data = read_input()
    try:
        return check(data)
    except Exception as exc:  # noqa: BLE001 —— 这里的"崩溃"会被 CLI 当成放行
        if not _must_fail_closed(data):
            raise
        print(
            f"protect_tests: 检查时出错，先拒绝（{type(exc).__name__}: {exc}）。"
            "这是流水线护栏自己的问题：把这条报错写进最后的回复然后停下，不要换别的办法绕过。",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
