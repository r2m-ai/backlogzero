"""认证：从钥匙串读 token 注入 CLAUDE_CODE_OAUTH_TOKEN。token 不落盘、不进日志、不进报错信息。"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from types import MappingProxyType

from agentops.errors import AgentopsError

KEYCHAIN_SERVICE = "agentops-claude"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_PREFIX = "sk-ant-oat01-"
TOKEN_MIN_LENGTH = 100  # 实际 token 约 108 字符；更短说明粘贴时只复制了折行后的一部分
SECURITY_BIN = "/usr/bin/security"
KEYCHAIN_TIMEOUT = 15
# 这些变量会让 claude 改用 API key 计费，启动子进程前去掉，确保走订阅 token。
_DROPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class AuthError(AgentopsError):
    pass


def _security(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [SECURITY_BIN, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=KEYCHAIN_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthError("钥匙串无响应：可能已锁定，或需要在图形界面上授权一次") from exc
    except OSError as exc:
        raise AuthError(f"无法运行 {SECURITY_BIN}: {exc}") from exc


def _first_stderr_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = (result.stderr or "").strip().splitlines()
    return lines[0] if lines else f"退出码 {result.returncode}"


def token_present() -> bool:
    return _security(["find-generic-password", "-s", KEYCHAIN_SERVICE]).returncode == 0


def read_token() -> str:
    result = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"])
    if result.returncode != 0:
        raise AuthError(
            f"读取钥匙串 {KEYCHAIN_SERVICE} 失败: {_first_stderr_line(result)}。"
            f"没有的话先运行 claude setup-token，"
            f'再执行 security add-generic-password -s {KEYCHAIN_SERVICE} -a "$USER" -w'
        )
    token = result.stdout.strip()
    if not token:
        raise AuthError(f"钥匙串 {KEYCHAIN_SERVICE} 的内容为空，请用 -U 重新存入")
    return token


def token_shape_problem(token: str) -> str | None:
    """token 形状不对就返回原因；不返回 token 本身的任何片段。"""
    if not token.startswith(TOKEN_PREFIX):
        return f"不以 {TOKEN_PREFIX} 开头，不是 claude setup-token 生成的 token"
    if any(ch.isspace() for ch in token):
        return "含有空白字符，可能粘贴时多了换行"
    if len(token) < TOKEN_MIN_LENGTH:
        return f"只有 {len(token)} 个字符，正常约 108 个，粘贴时可能只复制了折行后的一部分"
    return None


def claude_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回一份带 token 的新环境变量字典，供启动 claude 子进程使用。"""
    source = os.environ if base is None else base
    return {**{k: v for k, v in source.items() if k not in _DROPPED_ENV}, TOKEN_ENV: read_token()}


def auth_status(env: Mapping[str, str] | None = None) -> Mapping[str, object]:
    """运行 claude auth status，只返回 authMethod、loggedIn、rc。子进程输出不进报错信息。"""
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            env=dict(env) if env is not None else None,
            timeout=60,
        )
    except OSError as exc:
        raise AuthError(f"无法运行 claude: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthError("claude auth status 超过 60 秒没有返回") from exc
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AuthError(f"claude auth status 没有输出 JSON（退出码 {result.returncode}）") from exc
    if not isinstance(data, dict):
        raise AuthError(f"claude auth status 输出不是 JSON 对象（退出码 {result.returncode}）")
    return MappingProxyType(
        {
            "authMethod": data.get("authMethod"),
            "loggedIn": bool(data.get("loggedIn")),
            "rc": result.returncode,
        }
    )
