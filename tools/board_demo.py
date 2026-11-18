"""用虚构数据渲染一页看板，给文档截图用。不读任何真实状态，不联网。

用法：
    uv run python tools/board_demo.py <输出目录>

输出 light.html、dark.html（分别固定为浅色和深色）和 board.js。在输出目录里起一个静态服务
（例如 python -m http.server），用浏览器打开后截图，存成 docs/images/board-light.png 和
docs/images/board-dark.png。下面的仓库名、工作项、数字全部是编的。
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentops import board_style, board_view
from agentops.board_data import Panel

SHA_A = "3f9c2e7a51d04b6e8a7c9d2f1b3e5a7c9d1f2b4e"
SHA_B = "a81b04cd22ef5a9037c61d48e2b90f7715c3d6aa"
DARK_QUERY = "(prefers-color-scheme:dark)"
NOW = "2026-03-02T10:24:00.000-08:00"


def _work(ref: str, status: str, priority: str, module: str, title: str, deps: str = "-") -> dict:
    return {
        "ref": ref,
        "status": status,
        "priority": priority,
        "module": module,
        "trigger": "auto",
        "deps": deps,
        "blocked_by": deps,
        "title": title,
    }


def _queue(item: str, status: str, sha: str, clock: str, detail: str) -> dict:
    return {
        "repo": item.split("#")[0],
        "item": item,
        "status": status,
        "sha": sha,
        "ts": f"2026-03-02T{clock}:00.000-08:00",
        "detail": detail,
    }


def demo_panels() -> tuple[Panel, ...]:
    attention = [
        {
            "ref": "shop-api#42",
            "status": "needs_human",
            "why": "needs_human",
            "hint": "autonomy=approve，每次合入都要人批",
            "approval_sha": SHA_A,
            "can_approve": True,
            "can_drop": True,
        },
        {
            "ref": "shop-api#39",
            "status": "needs_human",
            "why": "needs_human；退回 2 次",
            "hint": "改动了受保护路径 config/",
            "approval_sha": SHA_B,
            "can_requeue": True,
            "can_drop": True,
        },
    ]
    proposals = [
        {
            "ref": "data-lab#18",
            "title": "给每日采集加缺口检查",
            "module": "capture",
            "priority": "P2",
            "reason": "昨天的简报里有两天数据缺口没人发现",
            "acceptance": "缺口超过一天时简报里有一行提示",
            "proposed_at": "2026-03-02T01:34",
            "can_adopt": True,
            "can_drop": True,
        }
    ]
    workitems = [
        _work("shop-api#39", "needs_human", "P1", "config", "把限流阈值挪进配置文件"),
        _work("shop-api#40", "landed", "P1", "orders", "订单导出接口支持分页"),
        _work("shop-api#41", "in_progress", "P0", "auth", "登录失败次数超限后锁定账号"),
        _work("shop-api#42", "needs_human", "P1", "orders", "订单导出加上 CSV 格式"),
        _work("shop-api#43", "testing", "P2", "docs", "补全接口文档里的错误码表"),
        _work("shop-api#44", "ready", "P2", "orders", "导出任务失败时重试三次", deps="42"),
        _work("data-lab#16", "landed", "P1", "capture", "采集任务加超时"),
        _work("data-lab#17", "returned", "P2", "report", "周报里加一张留存曲线"),
        _work("data-lab#18", "backlog", "P2", "capture", "给每日采集加缺口检查"),
        _work("data-lab#12", "dropped", "P3", "report", "月报改成 PDF"),
    ]
    sessions = [
        {
            "session": "9c41e7d2",
            "item": "shop-api#41",
            "mode": "headless",
            "branch": "feat/41-login-lockout",
            "since": "2026-03-02T10:12:44.120-08:00",
        }
    ]
    queue = [
        _queue("shop-api#40", "landed", "5d2e91a", "09:41", '{"landed_sha": "5d2e91a0c3"}'),
        _queue("shop-api#42", "needs_human", SHA_A[:7], "10:02", '{"autonomy": "approve"}'),
        _queue("shop-api#43", "processing", "77aa0c1", "10:20", '{"cmd": "just test-all"}'),
        _queue("data-lab#17", "returned", "c04f1be", "08:15", '{"reason": "tests_failed"}'),
    ]
    slots = [{"slot": "test.1", "state": "占用"}, {"slot": "test.2", "state": "空闲"}]
    today = [
        {"when": "14:00 计划", "what": "capture（采集）", "status": "未运行"},
        {"when": "01:30 计划", "what": "nightly（dbt → PM → 派发 → 简报）", "status": "已运行"},
        {"when": "今天", "what": "合入", "status": "3"},
        {"when": "今天", "what": "转人工", "status": "2"},
        {"when": "今天", "what": "花费（估算）", "status": "$4.10 / $15.0"},
    ]
    dbt = [
        {
            "repo": "data-lab",
            "generated": "2026-03-02T09:30:11Z",
            "pass": 21,
            "warn": 1,
            "error": 0,
        },
        {"repo": "shop-api", "generated": "-", "pass": "-", "warn": "-", "error": "-"},
    ]
    freshness = [
        {"source": "data-lab captured_at", "latest": "2026-03-02T09:12:16", "note": ""},
        {"source": "data-lab rows", "latest": "1284033", "note": ""},
    ]
    disk = [
        {"what": "home", "path": "~/.agentops", "free_gb": "412", "used_pct": "38%"},
        {"what": "data", "path": "~/data", "free_gb": "36", "used_pct": "93%"},
    ]
    costs = "，".join(f"shop-api#{n} $0.{n}" for n in range(30, 44))
    kpi = [
        {"value": "合入 24，needs_human 9，退回 3，冲突 0，重排队 1，回滚 0，hook 拦截 2"},
        {"value": "人类介入率 38%（needs_human ÷ landed）"},
        {"value": "返工率 11%（returned ÷ land_requested 27）"},
        {"value": "平均排队 0.8 秒"},
        {"value": "成本合计 $11.42（客户端估算），会话 27 个"},
        {"value": f"每项成本：{costs}"},
    ]
    return (
        Panel("attention", "需要你看", attention),
        Panel("proposals", "PM 建议", proposals),
        Panel("workitems", "工作项", workitems),
        Panel("sessions", "正在跑的会话", sessions),
        Panel("queue", "合并队列", queue),
        Panel("slots", "重任务槽位", slots),
        Panel("today", "今天的计划和实际运行", today),
        Panel("dbt", "dbt 结果", dbt),
        Panel("freshness", "数据新鲜度", freshness),
        Panel("disk", "磁盘", disk),
        Panel("kpi", "KPI（7 天）", kpi),
    )


def render_demo() -> str:
    page = board_view.render(
        demo_panels(), token="0" * 32, now_text=NOW, brief=Path("briefs/2026-03-02.md")
    )
    return page.replace('<meta http-equiv="refresh" content="30">', "")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    page = render_demo()
    if page.count(DARK_QUERY) != 1:
        print("样式里的深色媒体查询写法变了，更新 DARK_QUERY", file=sys.stderr)
        return 1
    (out / "light.html").write_text(page.replace(DARK_QUERY, f"{DARK_QUERY} and (max-width:0px)"))
    (out / "dark.html").write_text(page.replace(DARK_QUERY, "(min-width:0px)"))
    (out / "board.js").write_text(board_style.SCRIPT)
    print(f"已写入 {out}/light.html、dark.html、board.js")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
