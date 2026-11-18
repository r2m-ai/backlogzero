"""ao token setup：在足够宽的伪终端里运行 claude setup-token，把 token 直接写进钥匙串。

解决的问题：窄终端里 token 会折行，手工复制经常只拿到一行。这里 token 只经过内存：
从伪终端输出里取出，经 stdin 交给 security，不落盘、不进日志，终端回显时打码。
"""

from __future__ import annotations

import fcntl
import getpass
import os
import pty
import re
import select
import struct
import sys
import termios
import time
import tty
from collections.abc import Sequence

from agentops import auth
from agentops.auth import AuthError

TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{60,}")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")
DEFAULT_COLUMNS = 300
DEFAULT_ROWS = 50
SETUP_COMMAND = ("claude", "setup-token")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_token(transcript: str) -> str | None:
    match = TOKEN_RE.search(strip_ansi(transcript))
    return match.group(0) if match else None


def mask(text: str) -> str:
    """回显时把 token 主体打码，只留前缀。"""
    return TOKEN_RE.sub(lambda m: m.group(0)[:13] + "•" * 12, text)


def run_in_pty(
    command: Sequence[str],
    *,
    columns: int = DEFAULT_COLUMNS,
    rows: int = DEFAULT_ROWS,
    forward_stdin: bool = True,
) -> tuple[int, str]:
    """在指定宽度的伪终端里运行命令，转发用户按键和（打码后的）输出，返回退出码和完整输出。"""
    pid, fd = pty.fork()
    if pid == 0:  # 子进程
        try:
            os.execvp(command[0], list(command))
        except OSError as exc:  # pragma: no cover - 子进程里
            print(f"无法运行 {command[0]}: {exc}", file=sys.stderr)
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    captured = bytearray()
    stdin_fd = sys.stdin.fileno() if forward_stdin and sys.stdin.isatty() else None
    saved_attrs = termios.tcgetattr(stdin_fd) if stdin_fd is not None else None
    if stdin_fd is not None:
        tty.setcbreak(stdin_fd)
    try:
        while True:
            watch = [fd] if stdin_fd is None else [fd, stdin_fd]
            ready, _, _ = select.select(watch, [], [])
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                captured += data
                sys.stdout.write(mask(data.decode("utf-8", "replace")))
                sys.stdout.flush()
            if stdin_fd is not None and stdin_fd in ready:
                keys = os.read(stdin_fd, 1024)
                if keys:
                    os.write(fd, keys)
    finally:
        if stdin_fd is not None and saved_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_attrs)
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), captured.decode("utf-8", "replace")


def feed_pty(
    command: Sequence[str],
    lines: Sequence[str],
    *,
    prompts: Sequence[str] | None = None,
    timeout_seconds: float = 30.0,
) -> int:
    """在伪终端里运行命令，把 lines 当作键盘输入逐行“敲”进去，返回退出码。

    security 在有 TTY 时从 /dev/tty 读密码而不读 stdin，所以必须用伪终端喂，而不是管道；
    它读密码前还会清空输入缓冲区，所以每一行都要等对应的提示出现后再敲。
    输出只收集不显示（可能含回显）。
    """
    pid, fd = pty.fork()
    if pid == 0:  # 子进程
        try:
            os.execvp(command[0], list(command))
        except OSError:  # pragma: no cover - 子进程里
            os._exit(127)
    deadline = time.monotonic() + timeout_seconds
    buffer = bytearray()

    def read_some() -> bool:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            return True
        try:
            data = os.read(fd, 4096)
        except OSError:
            return False
        if not data:
            return False
        buffer.extend(data)
        return True

    def wait_for(prompt: str) -> bool:
        while time.monotonic() < deadline:
            if prompt.encode() in buffer:
                return True
            if not read_some():
                return False
        return False

    try:
        for index, line in enumerate(lines):
            if prompts is not None and not wait_for(prompts[index]):
                break
            buffer.clear()
            os.write(fd, (line + "\n").encode())
        while time.monotonic() < deadline and read_some():
            pass
    finally:
        os.close(fd)
    waited, status = os.waitpid(pid, os.WNOHANG)
    if waited == 0:
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        return 124
    return os.waitstatus_to_exitcode(status)


def store_token(token: str, *, service: str = auth.KEYCHAIN_SERVICE) -> None:
    """用伪终端把 token 交给 security，-U 覆盖已有条目。token 不出现在命令行参数里。"""
    account = os.environ.get("USER") or getpass.getuser()
    command = [auth.SECURITY_BIN, "add-generic-password", "-U", "-s", service, "-a", account, "-w"]
    try:
        rc = feed_pty(
            command,
            [token, token],
            prompts=("password data for new item:", "retype password for new item:"),
        )
    except OSError as exc:
        raise AuthError(f"写入钥匙串失败: {exc}") from exc
    if rc != 0:
        raise AuthError(f"写入钥匙串失败: security 退出码 {rc}")


def setup(*, columns: int = DEFAULT_COLUMNS) -> str:
    """运行 claude setup-token，取出 token 存进钥匙串，读回核对。返回给人看的结论。"""
    print(
        f"即将在 {columns} 列宽的伪终端里运行 `claude setup-token`。\n"
        "请按它的提示在浏览器里完成授权。\n"
        "token 会直接写入钥匙串，终端里只显示打码后的前缀，不落盘。\n"
    )
    rc, transcript = run_in_pty(SETUP_COMMAND, columns=columns)
    token = extract_token(transcript)
    del transcript
    if token is None:
        raise AuthError(f"claude setup-token 退出码 {rc}，输出里没有 token，未写入钥匙串")
    problem = auth.token_shape_problem(token)
    if problem:
        raise AuthError(f"取到的 token {problem}，没有写入钥匙串")
    store_token(token)
    stored = auth.read_token()
    if stored != token:
        raise AuthError("写入钥匙串后读回的内容不一致")
    return f"已写入钥匙串 {auth.KEYCHAIN_SERVICE}（{len(token)} 字符，未显示）"
