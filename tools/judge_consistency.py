"""阶段 B 验收：同一批 judge 输入分别经 Claude 与 Codex 后端各判一次，输出一致率与分歧样本。

用法（必须用临时 AGENTOPS_HOME，不往真实事件日志里写）：

    AGENTOPS_HOME=/tmp/judge-home uv run --offline python tools/judge_consistency.py \\
        --inputs inputs.jsonl --out report.md --raw results.json

临时 AGENTOPS_HOME 的 config.toml 里要有两个指向同一路径的仓库别名：
`[repos.judge-claude] runtime = "claude"` 与 `[repos.judge-codex] runtime = "codex"`。
每行输入：{"name", "trigger", "ref", "title", "module", "returns", "context", "baseline"}，
baseline 是当时线上裁决（可为空），只作参考，不参与一致率。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from agentops import budget, judge, llm
from agentops.config import Config, load_config
from agentops.paths import DEFAULT_HOME, home
from agentops.timeutil import now_iso

RUNTIMES = ("claude", "codex")
REPO_PREFIX = "judge-"
REASON_CHARS = 220


def _load_inputs(path: Path) -> tuple[Mapping[str, object], ...]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        if row.get("trigger") not in judge.TRIGGERS:
            raise SystemExit(f"输入 {row.get('name')} 的 trigger 不合法: {row.get('trigger')!r}")
    return tuple(rows)


def _judge_once(config: Config, prompt: str, runtime: str) -> dict[str, object]:
    started = time.monotonic()
    try:
        result = llm.call_structured(
            prompt,
            judge.SCHEMA,
            config=config,
            role="steward",
            label="judge",
            repo=f"{REPO_PREFIX}{runtime}",
        )
    except (llm.LlmError, budget.BudgetExceeded) as exc:
        return {
            "runtime": runtime,
            "error": str(exc),
            "seconds": round(time.monotonic() - started, 1),
        }
    usage = None if result.usage is None else result.usage.to_dict()
    return {
        "runtime": runtime,
        "action": str(result.data.get("action", "")),
        "reason": str(result.data.get("reason", "")),
        "hint": str(result.data.get("hint", "")),
        "cost_usd": result.cost_usd,
        "cost_estimated": result.cost_estimated,
        "usage": usage,
        "session_id": result.session_id,
        "seconds": round(time.monotonic() - started, 1),
    }


def _run(
    config: Config, rows: Sequence[Mapping[str, object]], runtimes: Sequence[str]
) -> list[dict]:
    results = []
    for index, row in enumerate(rows, 1):
        prompt = judge.render_prompt(
            str(row["trigger"]),
            ref=str(row["ref"]),
            title=str(row["title"]),
            module=str(row["module"]),
            returns=row.get("returns", 0),
            context=str(row["context"]),
        )
        verdicts = {rt: _judge_once(config, prompt, rt) for rt in runtimes}
        results.append({**row, "verdicts": verdicts})
        summary = " / ".join(f"{rt}={v.get('action') or 'ERROR'}" for rt, v in verdicts.items())
        print(f"[{index}/{len(rows)}] {row['name']}: {summary}", file=sys.stderr)
    return results


def _agree(verdicts: Mapping[str, Mapping[str, object]]) -> bool:
    """每个后端都给出了动作，且动作相同。"""
    actions = [v.get("action") for v in verdicts.values()]
    return all(actions) and len(set(actions)) == 1


def _totals(results: Sequence[Mapping], runtime: str) -> dict[str, float]:
    verdicts = [r["verdicts"][runtime] for r in results]
    tokens = sum(
        int((v.get("usage") or {}).get("input_tokens", 0))
        + int((v.get("usage") or {}).get("output_tokens", 0))
        for v in verdicts
    )
    return {
        "cost_usd": round(sum(float(v.get("cost_usd") or 0.0) for v in verdicts), 4),
        "seconds": round(sum(float(v.get("seconds") or 0.0) for v in verdicts), 1),
        "tokens": tokens,
        "errors": sum(1 for v in verdicts if "error" in v),
    }


def _report(config: Config, results: Sequence[Mapping], runtimes: Sequence[str]) -> str:
    agreed = [r for r in results if _agree(r["verdicts"])]
    lines = [
        "# judge 双后端一致率实测",
        "",
        f"- 时间：{now_iso()[:16]}；输入 {len(results)} 条"
        "（真实事件重放：rebase 冲突、测试失败、规则触发）。",
        "- 模型："
        + "；".join(
            f"{rt} = {config.model_for('steward', rt)}"
            f"（effort {config.effort_for('steward', rt) or '默认'}）"
            for rt in runtimes
        ),
        f"- **动作一致 {len(agreed)}/{len(results)}**。"
        "基线列是当时线上（Claude）的裁决，只作参考。",
        "",
        "| # | 输入 | 触发 | 基线 | " + " | ".join(runtimes) + " | 一致 |",
        "|---|---|---|---|" + "---|" * len(runtimes) + "---|",
    ]
    for index, r in enumerate(results, 1):
        cells = [
            str(
                r["verdicts"][rt].get("action")
                or f"错误：{r['verdicts'][rt].get('error', '')[:60]}"
            )
            for rt in runtimes
        ]
        lines.append(
            f"| {index} | {r['name']} | {r['trigger']} | {r.get('baseline') or '-'} | "
            + " | ".join(cells)
            + f" | {'是' if _agree(r['verdicts']) else '**否**'} |"
        )
    lines += [
        "",
        "## 用量",
        "",
        "| 后端 | 美元 | token（输入+输出） | 秒 | 失败 |",
        "|---|---|---|---|---|",
    ]
    for rt in runtimes:
        t = _totals(results, rt)
        estimated = "估" if rt == "codex" else "实"
        lines.append(
            f"| {rt} | {t['cost_usd']}（{estimated}） | {t['tokens'] or '-'} | "
            f"{t['seconds']} | {t['errors']} |"
        )
    lines += ["", "## 分歧与理由", ""]
    for index, r in enumerate(results, 1):
        tag = "" if _agree(r["verdicts"]) else "（分歧）"
        lines.append(f"### {index}. {r['name']}{tag}")
        lines.append("")
        for rt in runtimes:
            v = r["verdicts"][rt]
            if "error" in v:
                lines.append(f"- **{rt}**：调用失败：{v['error'][:REASON_CHARS]}")
                continue
            lines.append(f"- **{rt} → {v['action']}**：{v['reason'][:REASON_CHARS]}")
            if v.get("hint"):
                lines.append(f"  - hint：{v['hint'][:REASON_CHARS]}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Markdown 报告")
    parser.add_argument("--raw", type=Path, help="原始结果 JSON")
    parser.add_argument("--runtimes", default=",".join(RUNTIMES))
    parser.add_argument("--only", type=int, default=0, help="只跑前 N 条（试跑用）")
    parser.add_argument("--select", default="", help="只重跑这些序号（1 起，逗号分隔）")
    parser.add_argument("--base", type=Path, help="上一次的原始结果 JSON；未重跑的条目从这里取")
    args = parser.parse_args(argv)
    if home().resolve() == Path(DEFAULT_HOME).expanduser().resolve():
        raise SystemExit("请把 AGENTOPS_HOME 指到临时目录，不要往真实事件日志里写")
    runtimes = tuple(r.strip() for r in args.runtimes.split(",") if r.strip())
    config = load_config()
    for rt in runtimes:
        config.runtime_for(f"{REPO_PREFIX}{rt}")  # 别名缺了就在这里报错
    rows = _load_inputs(args.inputs)
    if args.only:
        rows = rows[: args.only]
    selected = {int(i) for i in args.select.split(",") if i.strip()}
    if selected:
        base = json.loads(args.base.read_text(encoding="utf-8")) if args.base else []
        rerun = _run(config, [rows[i - 1] for i in sorted(selected)], runtimes)
        fresh = dict(zip(sorted(selected), rerun, strict=True))
        results = [fresh.get(i) or base[i - 1] for i in range(1, len(rows) + 1)]
    else:
        results = _run(config, rows, runtimes)
    args.out.write_text(_report(config, results, runtimes), encoding="utf-8")
    if args.raw:
        args.raw.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告：{args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
