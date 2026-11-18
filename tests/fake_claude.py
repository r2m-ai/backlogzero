"""测试用的假 claude。通过 FAKE_CLAUDE_MODE 控制行为：
- structured=<json>：输出带 structured_output 的结果（判断层、PM 用）
- work：在 cwd 里加文件、提交、运行 ao land（模拟无人值守会话）
- idle：什么都不做，输出成功结果
- error：输出 is_error
记录收到的参数和提示词到 FAKE_CLAUDE_LOG（追加）。
"""

import json
import os
import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    prompt = args[-1] if args and not args[-1].startswith("-") else ""
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "args": args[:-1],
                        "prompt": prompt,
                        "cwd": os.getcwd(),
                        "env": {k: v for k, v in os.environ.items() if k.startswith("AGENTOPS_")},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    mode = os.environ.get("FAKE_CLAUDE_MODE", "idle")
    cost = float(os.environ.get("FAKE_CLAUDE_COST", "0.5"))
    session = next(
        (
            args[i + 1]
            for i, a in enumerate(args)
            if a in ("--session-id", "--resume") and i + 1 < len(args)
        ),
        "fake-session",
    )
    if "--fork-session" in args:
        session = f"{session[:8]}-fork-{os.getpid()}"
    result = {
        "type": "result",
        "is_error": False,
        "num_turns": 3,
        "total_cost_usd": cost,
        "session_id": session,
        "result": "ok",
    }
    if mode.startswith("structured="):
        result["structured_output"] = json.loads(mode[len("structured=") :])
    elif mode == "work":
        with open("work.txt", "a", encoding="utf-8") as handle:
            handle.write("done\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: fake session work"], check=True)
        ao = os.environ.get("AGENTOPS_AO") or os.path.expanduser("~/.local/bin/ao")
        land = subprocess.run([ao, "land"], capture_output=True, text=True)
        result["result"] = f"landed rc={land.returncode} {land.stderr.strip()[:120]}"
    elif mode == "error":
        result["is_error"] = True
        result["result"] = "Failed to authenticate. API Error: 401"
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["is_error"] else 0


if __name__ == "__main__":
    sys.exit(main())
