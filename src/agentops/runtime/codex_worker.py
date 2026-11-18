"""Codex worker 会话：会话 profile 渲染、沙箱自检、启动脚本、结果解析、收尾清理。

一次性结构化调用（judge / PM）在 codex.py；这里是 ao start 起的、真正改代码的会话。
与 Claude worker 的差别：
- 会话配置不是 --settings JSON，而是 <CODEX_HOME>/agentops-<会话>.config.toml 这个 profile；
- 起会话前先用 `codex sandbox` 按同一份 profile 自检（模型看得到 deny 规则会主动避开，
  不能靠模型行为证明沙箱有效）；
- 项目级 hooks 要带 --dangerously-bypass-hook-trust 才触发（hook 源由 ao 渲染，审核人已定）；
- 结果是 --json 事件流加 -o 的最后回复，没有美元数，按价格表估算；
- hook 进程不保证拿得到 AGENTOPS_* 环境变量，另写一份按克隆路径查的标记文件给 hooks/_common.py。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import socket
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from agentops import clones
from agentops.errors import AgentopsError
from agentops.paths import approvals_dir, home, inbox_dir
from agentops.repo import TEMPLATES
from agentops.runtime import codex as codex_rt
from agentops.runtime.base import WorkerResult
from agentops.workitem_model import is_valid_repo

if TYPE_CHECKING:
    from agentops.policy import Policy
    from agentops.start import Launch

PERMISSION_PROFILE = "agentops"
SESSION_TEMPLATE = TEMPLATES / "codex" / "session.config.toml"
UV_CACHE = "~/.cache/uv"
HOLDOUT = "~/data/{repo}/holdout"  # 与 templates/session-settings.json 的约定一致
PROBE_TIMEOUT = 30
MARKER_DIGEST_CHARS = 24  # 与 hooks/_common.py 的 marker_path 保持一致
SUMMARY_SEPARATOR = " | "
PROBE_NAME = "agentops-sandbox-probe"
SENTINEL_NAME = "sandbox-sentinel"
NETWORK_PROBE = ("/usr/bin/nc", "-z", "-G", "2", "127.0.0.1")
LOOPBACK_TIMEOUT = 2
PRIVATE_MODE = 0o600
# 模型名与思考强度会成为 codex 的命令行参数：不能以 - 开头，不能带空白
SAFE_ARGUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_UNSAFE_IN_TOML_KEY = ('"', "\\", "\n")


def profile_name(session_id: str) -> str:
    return f"agentops-{session_id}"


def profile_path(session_id: str) -> Path:
    return codex_rt.codex_home() / f"{profile_name(session_id)}.config.toml"


def marker_path(clone: Path) -> Path:
    digest = hashlib.sha256(str(clone.resolve()).encode()).hexdigest()[:MARKER_DIGEST_CHARS]
    return home() / "sessions" / "by-clone" / f"{digest}.json"


def holdout_path(repo: str) -> Path:
    if not is_valid_repo(repo):
        raise AgentopsError(f"仓库名不合法: {repo!r}")
    return Path(HOLDOUT.format(repo=repo)).expanduser()


def session_env(launch: Launch) -> dict[str, str]:
    """会话里 shell 和 hooks 要看到的变量。脚本里 export 一遍，profile 里再 set 一遍。"""
    return {
        "AGENTOPS_REPO": launch.item.repo,
        "AGENTOPS_ITEM": str(launch.item.id),
        "AGENTOPS_MODE": launch.mode,
        "AGENTOPS_BRANCH": launch.branch,
        "AGENTOPS_SESSION": launch.session_id,
        "AGENTOPS_RUNTIME": codex_rt.RUNTIME,
    }


def _toml_key(path: Path | str) -> str:
    text = str(path)
    if any(char in text for char in _UNSAFE_IN_TOML_KEY):
        raise AgentopsError(f"路径里有引号、反斜杠或换行，不能写进 Codex profile: {text!r}")
    return text


def render_session_profile(launch: Launch, *, repos: Iterable[str] = ()) -> str:
    """repos 是 config 里所有仓库：每个仓库的 holdout 都 deny，不只是本仓库的。"""
    env = {**session_env(launch), "UV_OFFLINE": "1", "PYTHONHASHSEED": "0"}
    env_table = ", ".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in env.items()
    )
    holdouts = sorted({holdout_path(repo) for repo in (*repos, launch.item.repo)})
    return Template(SESSION_TEMPLATE.read_text(encoding="utf-8")).substitute(
        clone=_toml_key(launch.clone.resolve()),
        holdout_rules="\n".join(f'"{_toml_key(path)}" = "deny"' for path in holdouts),
        uv_cache=_toml_key(Path(UV_CACHE).expanduser()),
        inbox=_toml_key(inbox_dir()),
        codex_home=_toml_key(codex_rt.codex_home()),
        user_codex_home=_toml_key(codex_rt.user_codex_home()),
        user_home=_toml_key(Path.home()),
        env=env_table,
    )


def _write_private(path: Path, text: str) -> None:
    """只有本人可读，先写临时文件再改名：hook 不会读到半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.chmod(PRIVATE_MODE)
    temp.replace(path)


def marker_content(launch: Launch, policy: Policy) -> dict[str, object]:
    """给 hooks 的克隆标记：会话身份，加上从 main 读出来的拦截策略快照。

    hooks 不读克隆里的 .pipeline.toml（会话能改），也不认克隆里的 main 引用（会话能动），
    只认这里记的策略和起会话时 main 的提交号。
    """
    return {
        "session": launch.session_id,
        "repo": launch.item.repo,
        "item": launch.item.id,
        "mode": launch.mode,
        "branch": launch.branch,
        "runtime": codex_rt.RUNTIME,
        "clone": str(launch.clone.resolve()),
        "base_sha": clones.ref_sha(launch.clone, policy.default_branch),
        "policy": {
            "default_branch": policy.default_branch,
            "protected": list(policy.protected),
            "existing_only": list(policy.existing_only),
        },
    }


def write_session_files(launch: Launch, *, policy: Policy, repos: Iterable[str] = ()) -> None:
    """渲染 CODEX_HOME、会话 profile 和给 hooks 的克隆标记。"""
    codex_rt.ensure_home()
    _write_private(profile_path(launch.session_id), render_session_profile(launch, repos=repos))
    _write_private(
        marker_path(launch.clone), json.dumps(marker_content(launch, policy), ensure_ascii=False)
    )


def cleanup(session_id: str, clone: Path) -> None:
    """会话结束或启动回滚：删会话 profile 和克隆标记。没有也不报错。"""
    profile_path(session_id).unlink(missing_ok=True)
    marker_path(clone).unlink(missing_ok=True)


def _probe(launch: Launch, command: list[str]) -> int:
    """在这份 profile 的沙箱里跑一条命令，返回退出码。输出一律丢弃。"""
    full = [
        *codex_rt.codex_command(),
        "sandbox",
        "-p",
        profile_name(launch.session_id),
        "-P",
        PERMISSION_PROFILE,
        "-C",
        str(launch.clone),
        "--",
        *command,
    ]
    try:
        result = subprocess.run(
            full,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=codex_rt.call_env(),
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentopsError(f"沙箱自检跑不起来（codex sandbox）: {exc}") from exc
    return result.returncode


def _forbidden_writes(launch: Launch) -> list[tuple[Path, str]]:
    """沙箱里必须写不成的位置。目录都真实存在，本人在沙箱外写得了，所以"写不成"只能是被拦。"""
    approvals = approvals_dir()
    approvals.mkdir(parents=True, exist_ok=True)
    targets = [
        (approvals / f".{PROBE_NAME}", "克隆之外的写入（审批目录）"),
        (launch.clone / ".git" / "hooks" / PROBE_NAME, "改克隆的 .git/hooks"),
    ]
    if (launch.clone / ".codex").is_dir():
        targets.append((launch.clone / ".codex" / PROBE_NAME, "改克隆的 .codex"))
    return targets


def _forbidden_reads(launch: Launch, repos: Iterable[str]) -> list[tuple[list[str], str]]:
    """沙箱里必须读不到的东西。哨兵是 ao 自己刚写的，放在和登录态同一个被 deny 的目录里。"""
    sentinel = codex_rt.codex_home() / SENTINEL_NAME
    _write_private(sentinel, "agentops sandbox sentinel\n")
    probes = [(["/bin/cat", str(sentinel)], f"ao 的 CODEX_HOME（登录态所在）{sentinel}")]
    for repo in sorted({*repos, launch.item.repo}):
        holdout = holdout_path(repo)
        if holdout.is_dir():
            probes.append((["/bin/ls", str(holdout)], f"holdout 目录 {holdout}"))
    return probes


def _network_is_blocked(launch: Launch) -> bool:
    """在回环口开一个监听，沙箱外先确认连得上，再看沙箱里连不连得上。

    开不了监听（比如 ao 自己就在别的沙箱里跑测试）就测不了，当作没有发现问题。
    """
    try:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            port = server.getsockname()[1]
            socket.create_connection(("127.0.0.1", port), timeout=LOOPBACK_TIMEOUT).close()
            return _probe(launch, [*NETWORK_PROBE, str(port)]) != 0
    except OSError:
        return True


def self_check(launch: Launch, *, repos: Iterable[str] = ()) -> None:
    """按会话 profile 自检沙箱，任何一项不符合预期就拒绝启动。

    1. 正对照：克隆的 .git/ 里能写。:workspace 默认不让写 .git，写得成说明这份 profile 真的生效了；
       写不成则后面的"被拒"都不可信。
    2. 写不成：克隆之外（审批目录）、克隆的 .git/hooks 与 .codex。
    3. 读不到：ao 的 CODEX_HOME（登录态所在）、存在的 holdout 目录。
    4. 连不上网（回环口）。
    模型看得到 deny 规则会主动避开，所以不能靠模型的行为证明沙箱有效，要在起会话前自己测。
    """
    inside = launch.clone / ".git" / PROBE_NAME
    forbidden = _forbidden_writes(launch)
    try:
        if _probe(launch, ["/usr/bin/touch", str(inside)]) != 0 or not inside.exists():
            raise AgentopsError(
                f"沙箱自检失败：会话 profile {profile_name(launch.session_id)} 没有生效"
                "（克隆里写不了）"
            )
        for target, what in forbidden:
            if _probe(launch, ["/usr/bin/touch", str(target)]) == 0 or target.exists():
                raise AgentopsError(f"沙箱自检失败：沙箱没有拦住{what}")
        for command, what in _forbidden_reads(launch, repos):
            if _probe(launch, command) == 0:
                raise AgentopsError(f"沙箱自检失败：沙箱没有拦住读 {what}")
        if not _network_is_blocked(launch):
            raise AgentopsError("沙箱自检失败：沙箱没有拦住联网（回环口连得上）")
    finally:
        inside.unlink(missing_ok=True)
        for target, _ in forbidden:
            target.unlink(missing_ok=True)


def prepare(launch: Launch, *, policy: Policy, repos: Iterable[str] = ()) -> None:
    write_session_files(launch, policy=policy, repos=repos)
    self_check(launch, repos=repos)


def _safe_argument(value: str, what: str) -> str:
    if not SAFE_ARGUMENT.match(value):
        raise AgentopsError(f"{what} 不能用作命令行参数: {value!r}")
    return value


def worker_script(launch: Launch) -> str:
    """无人值守 worker 的启动脚本。提示词走标准输入，事件流进 result_path，最后回复进 -o 文件。"""
    env = " ".join(f"{key}={shlex.quote(value)}" for key, value in session_env(launch).items())
    flags = [
        "exec",
        "--json",
        "--profile",
        profile_name(launch.session_id),
        "--dangerously-bypass-hook-trust",
        "-C",
        str(launch.clone),
        "-m",
        _safe_argument(launch.model, "模型名"),
    ]
    if launch.effort:
        flags += ["-c", f"model_reasoning_effort={_safe_argument(launch.effort, '思考强度')}"]
    flags += ["-o", str(launch.last_message_path), "-"]
    command = " ".join(shlex.quote(flag) for flag in flags)
    return (
        "#!/bin/bash\n"
        "# agentops 会话启动脚本（Codex）：由 ao start 生成，会话自己写不了这个目录。\n"
        f"cd {shlex.quote(str(launch.clone))} || exit 1\n"
        f"export {env}\n"
        "unset OPENAI_API_KEY\n"
        f"export CODEX_HOME={shlex.quote(str(codex_rt.codex_home()))}\n"
        "CODEX_CMD=${AGENTOPS_CODEX_CMD:-codex}\n"
        f"exec $CODEX_CMD {command} < {shlex.quote(str(launch.prompt_path))}"
        f" > {shlex.quote(str(launch.result_path))}\n"
    )


def _read(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_worker_result(
    launch: Launch, rc: int, *, prices: Mapping[str, Mapping[str, float]]
) -> WorkerResult:
    """事件流取 thread id 与用量，-o 文件取最后回复；失败原因（turn.failed）放在摘要最前面。"""
    parsed = codex_rt.parse_events(_read(launch.result_path))
    last = _read(launch.last_message_path).strip() or (parsed.last_message or "").strip()
    estimate = codex_rt.estimate_cost(launch.model, parsed.usage, prices) if parsed.usage else None
    failure = "；".join(parsed.errors)
    if not failure and rc != 0:
        failure = "；".join(parsed.warnings)
    summary = SUMMARY_SEPARATOR.join(part for part in (failure, last) if part)
    return WorkerResult(
        session_id=parsed.thread_id,
        is_error=bool(parsed.errors) or rc != 0,
        cost_usd=estimate or 0.0,
        cost_estimated=True,
        price_known=estimate is not None,
        num_turns=parsed.turns,
        usage=parsed.usage,
        summary=summary,
    )
