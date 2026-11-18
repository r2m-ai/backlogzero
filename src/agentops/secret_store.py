"""ao secret set|check <name>：把第三方 API token 存进钥匙串（agentops-<name>），只验证不打印。

值从 getpass 读取（终端不回显），经伪终端交给 security，不出现在命令行参数、文件、日志里。
"""

from __future__ import annotations

import getpass
import re

from agentops import auth, token_setup
from agentops.auth import AuthError

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
MIN_LENGTH = 16


def service_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise AuthError(f"名字 {name!r} 不合法：小写字母开头，只能有小写字母、数字、- 和 _")
    return f"agentops-{name}"


def value_problem(value: str) -> str | None:
    if not value:
        return "为空"
    if any(ch.isspace() for ch in value):
        return "含空白字符（粘贴时可能带了换行或空格）"
    if len(value) < MIN_LENGTH:
        return f"只有 {len(value)} 个字符，不像 API token"
    return None


def read_secret(name: str) -> str:
    """供采集等会话之外的任务读取；失败抛 AuthError。"""
    service = service_name(name)
    result = auth._security(["find-generic-password", "-s", service, "-w"])
    if result.returncode != 0:
        raise AuthError(f"钥匙串里没有 {service}，先运行 ao secret set {name}")
    return result.stdout.strip()


def exists(name: str) -> int | None:
    """存在则返回长度，不存在返回 None。"""
    try:
        return len(read_secret(name))
    except AuthError:
        return None


def set_secret(name: str, *, value: str | None = None) -> str:
    """交互式存入：value 为 None 时用 getpass 读取。返回给人看的结论（不含值）。"""
    service = service_name(name)
    secret = (
        value if value is not None else getpass.getpass(f"粘贴 {name} 的 API token（不回显）: ")
    )
    secret = secret.strip()
    problem = value_problem(secret)
    if problem:
        raise AuthError(f"token {problem}，没有写入钥匙串")
    token_setup.store_token(secret, service=service)
    if read_secret(name) != secret:
        raise AuthError("写入钥匙串后读回的内容不一致")
    return f"已写入钥匙串 {service}（{len(secret)} 字符，未显示）"


def check(name: str) -> str:
    service = service_name(name)
    length = exists(name)
    if length is None:
        raise AuthError(f"钥匙串里没有 {service}")
    return f"{service} 存在（{length} 字符，未打印）"
