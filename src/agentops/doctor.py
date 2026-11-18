"""ao doctor：检查工具、版本、认证、状态目录和数据目录。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

from agentops import auth, budget
from agentops.config import Config, load_config
from agentops.errors import AgentopsError
from agentops.paths import STATE_SUBDIRS, config_file, events_file, home

OK, WARN, FAIL = "OK", "WARN", "FAIL"
MIN_CLAUDE_VERSION = "2.1.259"
MIN_FREE_GB = 20
EDGAR_EXPECTED = (
    "full-index/2026/QTR2 的 form.idx",
    "full-index/2026/QTR3 的 form.idx",
    "daily-index/2026/QTR3 的 9 月 29 日 form 文件",
    "daily-index/2026/QTR3 的 9 月 30 日 form 文件",
    "daily-index/2026/QTR4 的 10 月 1 日 form 文件",
)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def version_at_least(actual: str, minimum: str) -> bool:
    found, wanted = _VERSION_RE.search(actual), _VERSION_RE.search(minimum)
    if not found or not wanted:
        return False
    return tuple(int(x) for x in found.groups()) >= tuple(int(x) for x in wanted.groups())


def _run(command: list[str]) -> tuple[int, str]:
    """运行命令，返回 (退出码, 输出的第一行)。找不到命令返回 127。"""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    except FileNotFoundError:
        return 127, ""
    except OSError as exc:
        return 126, str(exc)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    text = (result.stdout or result.stderr).strip()
    return result.returncode, (text.splitlines()[0] if text else "")


def check_tool(name: str, args: tuple[str, ...] = ("--version",)) -> Check:
    code, first_line = _run([name, *args])
    if code == 127:
        return Check(name, FAIL, "未安装或不在 PATH")
    if code != 0:
        return Check(name, FAIL, f"退出码 {code}: {first_line}")
    return Check(name, OK, first_line)


def check_claude_version() -> Check:
    base = check_tool("claude")
    if base.status != OK:
        return base
    if not version_at_least(base.detail, MIN_CLAUDE_VERSION):
        return Check("claude", FAIL, f"{base.detail}，需要 ≥ {MIN_CLAUDE_VERSION}")
    return base


def _guard(name: str, fn):
    """路径检查在沙箱里可能抛 PermissionError，统一降级成 WARN。"""
    try:
        return fn()
    except OSError as exc:
        return Check(name, WARN, f"无法访问: {exc}")


def check_state_dirs() -> tuple[Check, ...]:
    return (_guard("state dir", _state_dirs),)


def _state_dirs() -> Check:
    root = home()
    if not root.is_dir():
        return Check("state dir", FAIL, f"{root} 不存在，先运行 ao init")
    missing = [sub for sub in STATE_SUBDIRS if not (root / sub).is_dir()]
    if missing:
        return Check("state dir", FAIL, f"缺少子目录 {', '.join(missing)}，运行 ao init")
    writable = events_file().parent.exists() and os.access(root, os.W_OK)
    return Check("state dir", OK if writable else FAIL, str(root))


def check_config() -> tuple[Check, Config | None]:
    try:
        config = load_config()
    except AgentopsError as exc:
        return Check("config", FAIL, str(exc)), None
    if not config_file().exists():
        return Check(
            "config", WARN, "config.toml 不存在，使用内置默认值；运行 ao init 生成"
        ), config
    return Check("config", OK, f"max_sessions={config.max_sessions}"), config


def check_keychain() -> Check:
    try:
        token = auth.read_token()
    except AgentopsError as exc:
        return Check("keychain", FAIL, str(exc))
    problem = auth.token_shape_problem(token)
    if problem:
        return Check(
            "keychain",
            FAIL,
            f"{auth.KEYCHAIN_SERVICE} 里的值{problem}。请重新存入: "
            f'security add-generic-password -U -s {auth.KEYCHAIN_SERVICE} -a "$USER" -w',
        )
    return Check(
        "keychain", OK, f"{auth.KEYCHAIN_SERVICE} 存在，格式正确（{len(token)} 字符，未打印）"
    )


def check_auth() -> Check:
    """只确认 CLI 识别了 token。它不向服务器验证，真伪要用 --live。"""
    try:
        status = auth.auth_status(auth.claude_env())
    except AgentopsError as exc:
        return Check("auth", FAIL, str(exc))
    if status["rc"] != 0 and status["authMethod"] is None:
        return Check("auth", FAIL, f"claude auth status 退出码 {status['rc']}，没有返回登录信息")
    if status["authMethod"] == "oauth_token" and status["loggedIn"]:
        return Check("auth", OK, "CLI 识别 token（authMethod=oauth_token）；真伪见 live auth")
    return Check("auth", FAIL, f"authMethod={status['authMethod']}，token 没有被 CLI 采用")


def check_live_auth() -> Check:
    """发一次最小请求（haiku，1 轮）验证 token 真伪。花费极小，但会联网。"""
    try:
        env = auth.claude_env()
    except AgentopsError as exc:
        return Check("live auth", FAIL, str(exc))
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "reply with the single word ok",
                "--max-turns",
                "1",
                "--model",
                "haiku",
                "--output-format",
                "json",
                "--no-session-persistence",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("live auth", FAIL, f"无法运行 claude: {exc}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return Check("live auth", FAIL, f"claude 没有输出 JSON（退出码 {result.returncode}）")
    if result.returncode == 0 and not data.get("is_error"):
        return Check("live auth", OK, f"token 有效，估算花费 ${data.get('total_cost_usd', 0):.4f}")
    text = str(data.get("result", ""))[:80]
    return Check("live auth", FAIL, f"请求失败: {text}")


def check_repos(config: Config) -> tuple[Check, ...]:
    return tuple(
        _guard(f"repo {repo}", lambda repo=repo, raw=raw: _repo(config, repo, str(raw)))
        for repo, raw in config.repos.items()
    )


def _repo(config: Config, repo: str, raw: str) -> Check:
    path = config.repo_path(repo)
    if not path.is_dir():
        return Check(f"repo {repo}", WARN, f"{raw} 不存在（阶段 2 创建）")
    if not (path / ".git").exists():
        return Check(f"repo {repo}", WARN, f"{raw} 不是 git 仓库")
    return Check(f"repo {repo}", OK, raw)


def check_edgar_manual(config: Config) -> Check:
    return _guard("edgar_manual", lambda: _edgar_manual(config))


def _edgar_manual(config: Config) -> Check:
    path = config.data_path("edgar_manual")
    if not path.is_dir():
        return Check("edgar_manual", WARN, f"{path} 不存在")
    files = sorted(p.name for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        return Check("edgar_manual", WARN, "目录为空。需要手动下载: " + "；".join(EDGAR_EXPECTED))
    return Check("edgar_manual", OK, f"{len(files)} 个文件: {', '.join(files[:6])}")


def check_disk() -> Check:
    return _guard("disk", _disk)


def _disk() -> Check:
    usage = shutil.disk_usage(home() if home().exists() else "/")
    free_gb = usage.free / 1e9
    status = OK if free_gb >= MIN_FREE_GB else WARN
    return Check("disk", status, f"可用 {free_gb:.0f} GB")


def check_budget_reservations() -> Check:
    held = budget.reservations()
    if not held:
        return Check("budget", OK, "没有未结算预留")
    stale = budget.stale_reservations(budget.stale_hours())
    listing = "; ".join(
        f"{call}=${float(r.get('amount') or 0):.2f}{'(陈旧)' if call in stale else ''}"
        for call, r in held.items()
    )
    return Check("budget", WARN if stale else OK, f"预留 {len(held)} 条：{listing}")


def run_checks(*, include_auth: bool = True, live: bool = False) -> tuple[Check, ...]:
    config_check, config = check_config()
    checks: list[Check] = [
        check_tool("git"),
        check_tool("jq"),
        check_tool("just"),
        check_tool("uv"),
        check_tool("duckdb"),
        check_tool("python3.12"),
        check_claude_version(),
        *check_state_dirs(),
        config_check,
        check_keychain(),
    ]
    if include_auth:
        checks.append(check_auth())
    if live:
        checks.append(check_live_auth())
    if config is not None:
        checks.extend(check_repos(config))
        checks.append(check_edgar_manual(config))
    checks.append(_guard("budget", check_budget_reservations))
    checks.append(check_disk())
    return tuple(checks)


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(check.status == FAIL for check in checks) else 0
