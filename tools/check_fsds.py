"""只读核对手动下载的 SEC Financial Statement Data Sets（季度 zip）与 company_tickers.json。

不联网、不写任何文件。用法：
  uv run --offline python check_fsds.py [--dir DIR] [--start 2009q1] [--end 2024q4] [--deep]
--deep 会逐行扫 num.txt（每个文件几百万行，慢），统计行数并检查关键标签是否出现。
退出码：0 全部齐备且格式正确；1 有缺失或格式问题。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

DEFAULT_DIR = Path.home() / "data/quant-lab/edgar_manual/fsds"
TICKERS_FILE = Path.home() / "data/quant-lab/edgar_manual/tickers/company_tickers.json"
SUB_REQUIRED = ("adsh", "cik", "name", "sic", "former", "changed", "form", "period", "fy", "fp",
                "filed", "instance")
NUM_REQUIRED = ("adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom", "value")
KEY_TAGS = ("Assets", "StockholdersEquity", "NetIncomeLoss", "Revenues", "GrossProfit",
            "NetCashProvidedByUsedInOperatingActivities", "EntityCommonStockSharesOutstanding")
MAIN_FORMS = ("10-K", "10-Q")
QUARTER_RE = re.compile(r"^(\d{4})q([1-4])\.zip$")


def quarters(start: str, end: str) -> list[str]:
    (y0, q0), (y1, q1) = (int(start[:4]), int(start[-1])), (int(end[:4]), int(end[-1]))
    out, y, q = [], y0, q0
    while (y, q) <= (y1, q1):
        out.append(f"{y}q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def read_header(zf: zipfile.ZipFile, member: str) -> list[str]:
    with zf.open(member) as fh:
        first = io.TextIOWrapper(fh, encoding="utf-8", errors="replace").readline()
    return first.rstrip("\r\n").split("\t")


def check_sub(zf: zipfile.ZipFile) -> tuple[list[str], dict]:
    problems: list[str] = []
    with zf.open("sub.txt") as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        header = text.readline().rstrip("\r\n").split("\t")
        missing = [c for c in SUB_REQUIRED if c not in header]
        if missing:
            problems.append(f"sub.txt 缺列 {missing}")
            return problems, {}
        idx = {c: header.index(c) for c in ("form", "filed")}
        forms: Counter[str] = Counter()
        filed_min, filed_max, rows = "99999999", "00000000", 0
        for line in text:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < len(header):
                continue
            rows += 1
            forms[parts[idx["form"]]] += 1
            filed = parts[idx["filed"]]
            filed_min, filed_max = min(filed_min, filed), max(filed_max, filed)
    main = sum(forms[f] for f in MAIN_FORMS)
    if main == 0:
        problems.append("sub.txt 里没有 10-K/10-Q")
    return problems, {"rows": rows, "10-K/10-Q": main, "filed": f"{filed_min}–{filed_max}",
                      "top_forms": forms.most_common(4)}


def check_num(zf: zipfile.ZipFile, deep: bool) -> tuple[list[str], dict]:
    problems: list[str] = []
    header = read_header(zf, "num.txt")
    missing = [c for c in NUM_REQUIRED if c not in header]
    if missing:
        problems.append(f"num.txt 缺列 {missing}")
        return problems, {}
    if not deep:
        return problems, {"header_ok": True}
    tag_i = header.index("tag")
    seen: set[str] = set()
    rows = 0
    with zf.open("num.txt") as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        text.readline()
        for line in text:
            rows += 1
            parts = line.split("\t", tag_i + 1)
            if len(parts) > tag_i:
                tag = parts[tag_i]
                if tag in KEY_TAGS:
                    seen.add(tag)
    absent = [t for t in KEY_TAGS if t not in seen]
    if absent:
        problems.append(f"num.txt 关键标签缺失 {absent}")
    return problems, {"rows": rows, "key_tags_present": len(seen), "absent": absent}


def check_zip(path: Path, deep: bool) -> list[str]:
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for member in ("sub.txt", "num.txt"):
                if member not in names:
                    problems.append(f"缺 {member}")
            if problems:
                return problems
            sub_problems, sub_info = check_sub(zf)
            num_problems, num_info = check_num(zf, deep)
            problems += sub_problems + num_problems
            size_mb = path.stat().st_size / 1e6
            print(f"  {path.name}: {size_mb:6.1f} MB  sub={sub_info}  num={num_info}")
    except zipfile.BadZipFile:
        problems.append("不是有效 zip（可能下载不完整）")
    return problems


def check_tickers(path: Path) -> list[str]:
    if not path.exists():
        return [f"缺 {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path.name} 解析失败: {type(exc).__name__}"]
    rows = list(data.values()) if isinstance(data, dict) else data
    if not rows or not all(k in rows[0] for k in ("cik_str", "ticker", "title")):
        return [f"{path.name} 字段不对，期望 cik_str/ticker/title"]
    print(f"  {path.name}: {len(rows)} 条，示例字段 {sorted(rows[0].keys())}")
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--tickers", type=Path, default=TICKERS_FILE)
    ap.add_argument("--start", default="2009q1")
    ap.add_argument("--end", default="2024q4")
    ap.add_argument("--deep", action="store_true")
    args = ap.parse_args()

    expected = quarters(args.start, args.end)
    present = {m.group(0)[:-4]: p for p in sorted(args.dir.glob("*.zip"))
               if (m := QUARTER_RE.match(p.name))}
    missing = [q for q in expected if q not in present]
    extra = sorted(q for q in present if q not in expected)
    print(f"目录 {args.dir}: 应有 {len(expected)} 个季度，已有 {len(present)}，缺 {len(missing)}")
    if missing:
        print("  缺:", " ".join(missing))
    if extra:
        print("  范围外（不读）:", " ".join(extra))

    all_problems: dict[str, list[str]] = {}
    for q in expected:
        if q in present:
            problems = check_zip(present[q], args.deep)
            if problems:
                all_problems[q] = problems
    ticker_problems = check_tickers(args.tickers)
    if ticker_problems:
        all_problems["tickers"] = ticker_problems

    for key, problems in all_problems.items():
        print(f"问题 {key}: {'; '.join(problems)}")
    ok = not missing and not all_problems
    print("结论:", "全部齐备、格式正确" if ok else "有缺失或格式问题，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
