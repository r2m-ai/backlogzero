"""测试用的假 codex。认两种调用：

`codex exec --json … -` ：提示词从标准输入读。FAKE_CODEX_MODE 控制行为：
- structured=<json>：把 json 写进 -o 文件，事件流里给 agent_message 与 turn.completed（含 usage）
- failed：输出 turn.failed，退出码 1
- rc1：不输出事件，stderr 报配置错误，退出码 1
- garbage：-o 文件里写非 JSON
- warned=<json>：先给一条 error 项（传输回退告警），再照常成功
- warned-rc1：给一条 error 项后退出码 1，没有输出
- work：worker 会话——在 cwd 里加文件、提交、运行 ao land，最后回复写进 -o 文件
- idle：worker 会话什么都不做，只给最后回复
FAKE_CODEX_USAGE 可覆盖 turn.completed 的 usage。

`codex sandbox -p <profile> -P <权限 profile> -C <cwd> -- <命令…>` ：FAKE_CODEX_SANDBOX 控制：
- enforce（默认）：照 profile 的文件规则判断命令最后一个参数（touch 算写，cat / ls 算读，
  nc 算联网一律拒），允许就真的执行，否则报 Operation not permitted。没有规则时按 :workspace 的
  实测行为：读全盘，写只限 cwd，cwd 里的 .git、.codex 默认只读
- leaky：一律真的执行（沙箱没拦住）
- broken：一律失败（profile 没加载成功）
profile 文件不存在、不是合法 TOML、或没有对应的 [permissions.<名>]，按 broken 处理。

参数、提示词和关键环境变量追加到 FAKE_CODEX_LOG。
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_USAGE = {
    "input_tokens": 1200,
    "cached_input_tokens": 200,
    "cache_write_input_tokens": 0,
    "output_tokens": 50,
    "reasoning_output_tokens": 10,
}
LOGGED_ENV = ("CODEX_HOME", "OPENAI_API_KEY", "PATH")
DENIED = 1
READ_PROGRAMS = ("cat", "ls")
NETWORK_PROGRAMS = ("nc",)
READ_ONLY_BY_DEFAULT = (".git", ".codex")


def _value(args: list[str], flag: str) -> str | None:
    return next((args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)), None)


def _event(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _log(record: dict) -> None:
    log = os.environ.get("FAKE_CODEX_LOG")
    if not log:
        return
    env = {k: v for k, v in os.environ.items() if k in LOGGED_ENV or k.startswith("AGENTOPS_")}
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({**record, "cwd": os.getcwd(), "env": env}, ensure_ascii=False))
        handle.write("\n")


def _filesystem_rules(args: list[str]) -> dict[Path, str] | None:
    """权限 profile 的文件规则；profile 读不了或没有这个权限 profile 返回 None。"""
    name, permission = _value(args, "-p"), _value(args, "-P")
    if not name or not permission:
        return None
    path = Path(os.environ.get("CODEX_HOME", "")) / f"{name}.config.toml"
    try:
        profile = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = profile.get("permissions", {}).get(permission)
    if not isinstance(table, dict):
        return None
    return {Path(raw).resolve(): mode for raw, mode in table.get("filesystem", {}).items()}


def _allowed(rules: dict[Path, str], root: Path, target: Path, *, write: bool) -> bool:
    """照 :workspace 的实测行为：最具体的规则说了算；没有规则时读全盘，
    写只限工作区，而且工作区里的 .git、.codex 默认只读。"""
    matches = [
        (len(str(rule)), mode)
        for rule, mode in rules.items()
        if target == rule or rule in target.parents
    ]
    if matches:
        mode = max(matches)[1]
        return mode == "write" or (mode == "read" and not write)
    if not write:
        return True
    if target != root and root not in target.parents:
        return False
    guarded = [root / name for name in READ_ONLY_BY_DEFAULT]
    return not any(target == path or path in target.parents for path in guarded)


def sandbox(args: list[str]) -> int:
    command = args[args.index("--") + 1 :] if "--" in args else []
    _log({"sandbox": True, "args": args})
    mode = os.environ.get("FAKE_CODEX_SANDBOX", "enforce")
    rules = _filesystem_rules(args)
    if mode == "broken" or not command or rules is None:
        sys.stderr.write("error: failed to load profile\n")
        return DENIED
    if mode == "enforce":
        program = os.path.basename(command[0])
        root = Path(_value(args, "-C") or os.getcwd()).resolve()
        target = Path(command[-1]).resolve()
        permitted = program not in NETWORK_PROGRAMS and _allowed(
            rules, root, target, write=program not in READ_PROGRAMS
        )
        if not permitted:
            sys.stderr.write(f"{command[0]}: {command[-1]}: Operation not permitted\n")
            return DENIED
    return subprocess.run(command, check=False, stdout=subprocess.DEVNULL).returncode


def _work() -> str:
    """模拟无人值守 worker：加文件、提交、ao land。"""
    with open("work.txt", "a", encoding="utf-8") as handle:
        handle.write("done\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: fake codex session work"], check=True)
    ao = os.environ.get("AGENTOPS_AO") or os.path.expanduser("~/.local/bin/ao")
    land = subprocess.run([ao, "land"], capture_output=True, text=True)
    return f"landed rc={land.returncode} {land.stderr.strip()[:120]}"


def exec_(args: list[str]) -> int:
    prompt = sys.stdin.read() if args and args[-1] == "-" else ""
    _log({"args": args, "prompt": prompt})
    mode = os.environ.get("FAKE_CODEX_MODE", "structured={}")
    if mode == "rc1":
        sys.stderr.write("config error: unknown key `nope`\n")
        return 1
    _event({"type": "thread.started", "thread_id": "fake-thread"})
    _event({"type": "turn.started"})
    if mode.startswith("warned"):
        _event(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_w",
                    "type": "error",
                    "message": "Falling back from WebSockets to HTTPS transport.",
                },
            }
        )
    if mode == "warned-rc1":
        _event(
            {
                "type": "item.completed",
                "item": {"id": "item_x", "type": "error", "message": "routing discovery failed"},
            }
        )
        return 1
    if mode.startswith("warned="):
        mode = "structured=" + mode[len("warned=") :]
    if mode == "failed":
        _event({"type": "turn.failed", "error": {"message": "usage limit reached"}})
        return 1
    schema_path = _value(args, "--output-schema")
    if schema_path:
        with open(schema_path, encoding="utf-8") as handle:
            json.load(handle)  # schema 必须是合法 JSON 文件
    if mode == "work":
        text = _work()
    elif mode == "idle":
        text = "什么都没做。"
    elif mode == "garbage":
        text = "not json"
    else:
        text = mode[len("structured=") :]
    output = _value(args, "-o")
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text)
    _event(
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}}
    )
    usage = json.loads(os.environ.get("FAKE_CODEX_USAGE") or json.dumps(DEFAULT_USAGE))
    _event({"type": "turn.completed", "usage": usage})
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "sandbox":
        return sandbox(args[1:])
    return exec_(args)


if __name__ == "__main__":
    sys.exit(main())
