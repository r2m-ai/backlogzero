"""从计划文档和流程文件里提取评审 prompt 需要的片段，并做姓名和路径脱敏。纯函数。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

NOT_FOUND = "（未在文档里找到对应内容）"
_HEADING = re.compile(r"^## ", re.MULTILINE)


def section(text: str, number: str) -> str:
    """返回以 '## <number>.' 或 '## <number> ' 开头的那一节，含标题，直到下一个二级标题。"""
    match = re.search(rf"^## {re.escape(number)}\.?\s.*$", text, re.MULTILINE)
    if not match:
        return NOT_FOUND
    following = _HEADING.search(text, match.end())
    end = following.start() if following else len(text)
    return text[match.start() : end].rstrip()


def preamble(text: str) -> str:
    """第一个二级标题之前的内容：标题行、版本和版本改动说明。"""
    first = _HEADING.search(text)
    return (text[: first.start()] if first else text).strip()


def table_rows(block: str, first_cell: str, *, prefix: bool = False) -> str:
    """表格里第一列等于 first_cell 的行；prefix=True 时也接受 "first_cell 其他文字"。"""

    def matches(cell: str) -> bool:
        return cell == first_cell or (prefix and cell.startswith(first_cell + " "))

    rows = [
        line
        for line in block.splitlines()
        if line.startswith("|") and matches(line.split("|")[1].strip())
    ]
    return "\n".join(rows) if rows else NOT_FOUND


def table_header(block: str) -> str:
    lines = block.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.startswith("|") and lines[index + 1].startswith("|--"):
            return f"{line}\n{lines[index + 1]}"
    return ""


def phase_row(plan: str, phase: int) -> str:
    block = section(plan, "6")
    return f"{table_header(block)}\n{table_rows(block, str(phase), prefix=True)}".strip()


def review_row(plan: str, phase: int) -> str:
    block = section(plan, "6A")
    return f"{table_header(block)}\n{table_rows(block, str(phase), prefix=True)}".strip()


def d_rows(plan: str, ids: Iterable[str]) -> str:
    block = section(plan, "8")
    rows = [table_rows(block, item_id) for item_id in ids]
    return f"{table_header(block)}\n" + "\n".join(rows)


def privacy_rule(plan: str) -> str:
    """开场指令第 6 条。"""
    for line in section(plan, "1").splitlines():
        if re.match(r"^\s*6\.\s", line):
            return line.strip()
    return NOT_FOUND


def reviewer_kickoff(process: str) -> str:
    """流程文件里的审稿人启动语（日常评审那条，不是匿名对照那条）。"""
    for line in process.splitlines():
        if line.startswith(">") and "你的角色是审稿人" in line and "独立评审" in line:
            return line.lstrip("> ").strip()
    return NOT_FOUND


def process_version(process: str) -> str:
    match = re.search(r"^# .*?·\s*(v[\d.]+)", process, re.MULTILINE)
    return match.group(1) if match else "未知"


def plan_version(plan: str) -> str:
    match = re.search(r"^版本\s*([\d.]+)", plan, re.MULTILINE)
    return match.group(1) if match else "未知"


def redact(text: str, names: Iterable[str], *, home: Path, replacement: str = "审核人") -> str:
    """把人名替换为 replacement，把家目录绝对路径替换为 ~。不碰其他内容。"""
    result = text.replace(str(home), "~")
    for name in names:
        if name:
            result = result.replace(name, replacement)
    return result
