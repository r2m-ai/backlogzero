"""看板页面的 HTML 渲染（纯函数）：输入面板数据，输出整页 HTML；不读状态，也不改状态。

所有来自数据的文字都经过转义。表单字段（token / ref / sha）和动作路径由 board.py 的
handle_action 校验，这里只负责把它们原样放进页面。
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agentops.board_data import NOT_RAN, RAN, SLOT_BUSY, SLOT_FREE, Panel
from agentops.board_style import CSS, FAVICON
from agentops.workitem_model import STATUSES

Row = Mapping[str, object]

SCRIPT_PATH = "/board.js"  # 脚本单独一个地址，页面里不放内联脚本
SHORT_SHA = 7
METER_HOT_PERCENT = 90  # 用量到这个百分比，进度条变红
SPEND_WARN_RATIO = 0.8  # 今天花费到上限的这个比例，统计块变黄
KPI_CHIPS_FROM = 3  # 一行里逗号分开的项数到这个数，拆成小标签
KPI_FOLD_FROM = 9  # 带冒号的明细项数到这个数，收进折叠区

CLOSED_STATUSES = frozenset({"landed", "dropped"})
RUNNING_STATUSES = frozenset({"in_progress", "queued", "testing"})
WIDE_PANELS = frozenset({"attention", "workitems"})
WIDE_WHEN_FILLED = frozenset({"proposals"})
ACTION_FLAGS = ("can_approve", "can_requeue", "can_adopt", "can_drop")

TONES: Mapping[str, str] = {
    "landed": "good",
    RAN: "good",
    "needs_human": "warn",
    NOT_RAN: "warn",
    "returned": "bad",
    "in_progress": "info",
    "queued": "info",
    "testing": "info",
    "processing": "info",
    SLOT_BUSY: "info",
    "ready": "accent",
    "backlog": "",
    "dropped": "",
    SLOT_FREE: "",
    "P0": "bad",
    "P1": "warn",
}

LABELS: Mapping[str, str] = {
    "ref": "工作项",
    "item": "工作项",
    "repo": "仓库",
    "status": "状态",
    "state": "状态",
    "why": "原因",
    "hint": "提示",
    "approval_sha": "待批提交",
    "title": "标题",
    "priority": "优先级",
    "module": "模块",
    "trigger": "触发",
    "deps": "依赖",
    "blocked_by": "被阻塞",
    "reason": "理由",
    "acceptance": "验收标准",
    "proposed_at": "提出时间",
    "session": "会话",
    "mode": "模式",
    "branch": "分支",
    "since": "开始",
    "sha": "提交",
    "ts": "时间",
    "detail": "详情",
    "slot": "槽位",
    "when": "时间",
    "what": "事项",
    "generated": "生成时间",
    "pass": "通过",
    "warn": "警告",
    "error": "失败",
    "note": "备注",
    "source": "来源",
    "latest": "最新值",
    "path": "路径",
    "free_gb": "剩余 GB",
    "used_pct": "已用",
}
PANEL_LABELS: Mapping[tuple[str, str], str] = {("disk", "what"): "位置"}

TIMESTAMP = re.compile(r"^\d{4}-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d))?")
MONEY_PAIR = re.compile(r"^\$(\d+(?:\.\d+)?) / \$(\d+(?:\.\d+)?)$")
PERCENT = re.compile(r"^(\d+(?:\.\d+)?)%$")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _pill(text: str, count: int | None = None) -> str:
    tone = TONES.get(text, "")
    classes = f"pill {tone}" if tone else "pill"
    extra = "" if count is None else f"<b>{count}</b>"
    return f'<span class="{classes}">{_esc(text)}{extra}</span>'


def _meter(percent: float, label: str) -> str:
    width = max(0.0, min(100.0, percent))
    hot = " hot" if width >= METER_HOT_PERCENT else ""
    return f'<span class="meter{hot}"><i style="width:{width:.0f}%"></i></span>{_esc(label)}'


def _short_time(text: str) -> str:
    """ISO 时间只留「月-日 时:分」；不是时间就原样返回。"""
    found = TIMESTAMP.match(text)
    if not found:
        return text
    month, day, hour, minute, _second = found.groups()
    return f"{month}-{day} {hour}:{minute}{'Z' if text.endswith('Z') else ''}"


def _clock(text: str) -> str:
    found = TIMESTAMP.match(text)
    if not found or found.group(5) is None:
        return text
    return f"{found.group(3)}:{found.group(4)}:{found.group(5)}"


# ---- 单元格 ----------------------------------------------------------------


def _ref_cell(text: str) -> str:
    return f'<td class="mono ref">{_esc(text)}</td>'


def _sha_cell(text: str) -> str:
    return f'<td class="mono nowrap" title="{_esc(text)}">{_esc(text[:SHORT_SHA])}</td>'


def _mono_cell(text: str) -> str:
    return f'<td class="mono nowrap">{_esc(text)}</td>'


def _path_cell(text: str) -> str:
    return f'<td class="mono wrap">{_esc(text)}</td>'


def _name_cell(text: str) -> str:
    return f'<td class="nowrap">{_esc(text)}</td>'


def _time_cell(text: str) -> str:
    if not TIMESTAMP.match(text):
        return f'<td class="num">{_esc(text)}</td>'
    return f'<td class="nowrap dim" title="{_esc(text)}">{_esc(_short_time(text))}</td>'


def _status_cell(text: str) -> str:
    if text.isdigit():
        return f'<td class="num"><b>{_esc(text)}</b></td>'
    money = MONEY_PAIR.match(text)
    if money:
        spent, limit = float(money.group(1)), float(money.group(2))
        percent = spent / limit * 100 if limit else 0.0
        return f'<td class="num nowrap">{_meter(percent, text)}</td>'
    return f"<td>{_pill(text)}</td>"


def _priority_cell(text: str) -> str:
    if text in TONES:
        return f"<td>{_pill(text)}</td>"
    return f'<td class="dim">{_esc(text)}</td>'


def _percent_cell(text: str) -> str:
    found = PERCENT.match(text)
    if not found:
        return f"<td>{_esc(text)}</td>"
    return f'<td class="num nowrap">{_meter(float(found.group(1)), text)}</td>'


def _count_cell(tone: str) -> Callable[[str], str]:
    def render(text: str) -> str:
        css = f"num t-{tone}" if text.isdecimal() and int(text) > 0 else "num dim"
        return f'<td class="{css}">{_esc(text)}</td>'

    return render


def _clip_cell(text: str) -> str:
    return f'<td class="mono dim clip" title="{_esc(text)}">{_esc(text)}</td>'


def _text_cell(text: str) -> str:
    """长文字列：留一个最小宽度，窄窗口里宁可横向滚动也不挤成一字一行。"""
    return f'<td class="text">{_esc(text)}</td>'


def _dim_cell(text: str) -> str:
    return f'<td class="dim nowrap">{_esc(text)}</td>'


CELLS: dict[str, Callable[[str], str]] = {
    "ref": _ref_cell,
    "item": _ref_cell,
    "slot": _ref_cell,
    "sha": _sha_cell,
    "approval_sha": _sha_cell,
    "session": _mono_cell,
    "branch": _mono_cell,
    "path": _path_cell,
    "repo": _name_cell,
    "ts": _time_cell,
    "since": _time_cell,
    "generated": _time_cell,
    "latest": _time_cell,
    "proposed_at": _time_cell,
    "status": _status_cell,
    "state": _status_cell,
    "priority": _priority_cell,
    "used_pct": _percent_cell,
    "pass": _count_cell("good"),
    "warn": _count_cell("warn"),
    "error": _count_cell("bad"),
    "detail": _clip_cell,
    "title": _text_cell,
    "why": _text_cell,
    "hint": _text_cell,
    "reason": _text_cell,
    "acceptance": _text_cell,
    "module": _dim_cell,
    "trigger": _dim_cell,
    "when": _dim_cell,
}


def _cell(column: str, value: object) -> str:
    text = str(value)
    if text == "":
        return "<td></td>"
    if text == "-":
        return '<td class="dim">–</td>'
    render = CELLS.get(column)
    return render(text) if render else f"<td>{_esc(text)}</td>"


# ---- 操作按钮 --------------------------------------------------------------


def _form(action: str, token: str, fields: Mapping[str, str], label: str, css: str = "") -> str:
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{_esc(value)}">'
        for name, value in {"token": token, **fields}.items()
    )
    button = f'<button class="{css}">' if css else "<button>"
    return f'<form method="post" action="{action}">{hidden}{button}{_esc(label)}</button></form>'


def _actions(row: Row, token: str) -> str:
    ref = str(row.get("ref", ""))
    parts: list[str] = []
    if row.get("can_approve"):
        sha = str(row.get("approval_sha", ""))
        label = f"批准 {sha[:SHORT_SHA]}"
        parts.append(_form("/approve", token, {"ref": ref, "sha": sha}, label, "primary"))
    if row.get("can_requeue"):
        parts.append(_form("/requeue", token, {"ref": ref}, f"重新入队 {ref}"))
    if row.get("can_adopt"):
        parts.append(_form("/adopt", token, {"ref": ref}, f"采纳 {ref}", "primary"))
    if row.get("can_drop"):
        # 两步确认用 <details>，不用 JS confirm()：嵌入式浏览器会吞掉对话框
        confirm = _form("/drop", token, {"ref": ref}, f"确定放弃 {ref}", "danger")
        parts.append(f'<details class="confirm"><summary>放弃…</summary>{confirm}</details>')
    return "".join(parts)


# ---- 表格和面板 ------------------------------------------------------------


def _columns(first: Row) -> list[str]:
    names_the_repo = "ref" in first or "item" in first
    columns = [
        c
        for c in first
        if not c.startswith("can_") and c != "id" and not (c == "repo" and names_the_repo)
    ]
    if "ref" in columns and "title" in columns:
        rest = [c for c in columns if c not in ("ref", "title")]
        return ["ref", "title", *rest]
    return columns


def _label(panel_key: str, column: str) -> str:
    return PANEL_LABELS.get((panel_key, column)) or LABELS.get(column, column)


def _table(panel_key: str, rows: Sequence[Row], token: str) -> str:
    columns = _columns(rows[0])
    has_actions = any(row.get(flag) for row in rows for flag in ACTION_FLAGS)
    head = "".join(f'<th scope="col">{_esc(_label(panel_key, c))}</th>' for c in columns)
    body: list[str] = []
    for row in rows:
        cells = "".join(_cell(c, row.get(c, "")) for c in columns)
        buttons = f'<div class="act">{_actions(row, token)}</div>'
        actions = f'<td class="actions">{buttons}</td>' if has_actions else ""
        body.append(f"<tr>{cells}{actions}</tr>")
    action_head = "<th></th>" if has_actions else ""
    return (
        f"<table><thead><tr>{head}{action_head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _is_closed(row: Row) -> bool:
    """已经合入或放弃。"""
    return str(row.get("status", "")) in CLOSED_STATUSES


def _open_rows(rows: Sequence[Row]) -> list[Row]:
    return [r for r in rows if not _is_closed(r)]


def _status_counts(rows: Sequence[Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _workitems_body(panel: Panel, token: str) -> str:
    counts = _status_counts(panel.rows)
    ordered = [s for s in STATUSES if s in counts] + sorted(s for s in counts if s not in STATUSES)
    chips = "".join(_pill(status, counts[status]) for status in ordered)
    open_rows = _open_rows(panel.rows)
    closed_rows = [r for r in panel.rows if _is_closed(r)]
    parts = [f'<div class="chips">{chips}</div>']
    if open_rows:
        parts.append(_table(panel.key, open_rows, token))
    else:
        parts.append('<p class="empty">没有未结束的工作项</p>')
    if closed_rows:
        parts.append(
            f'<details class="fold" data-keep id="wi-closed">'
            f"<summary>已结束 {len(closed_rows)} 项</summary>"
            f"{_table(panel.key, closed_rows, token)}</details>"
        )
    return "".join(parts)


def _chip(part: str) -> str:
    name, _, number = part.strip().rpartition(" ")
    if not name:
        return f'<span class="chip">{_esc(part.strip())}</span>'
    return f'<span class="chip">{_esc(name)}<b>{_esc(number)}</b></span>'


def _kpi_line(index: int, line: str) -> str:
    label, colon, rest = line.partition("：")
    if colon:
        parts = rest.split("，")
        chips = f'<div class="chips">{"".join(_chip(p) for p in parts)}</div>'
        if len(parts) < KPI_FOLD_FROM:
            return f"<p><b>{_esc(label)}</b></p>{chips}"
        summary = f"{_esc(label)}（{len(parts)} 项）"
        return (
            f'<details class="fold" data-keep id="kpi-fold-{index}">'
            f"<summary>{summary}</summary>{chips}</details>"
        )
    parts = line.split("，")
    if len(parts) >= KPI_CHIPS_FROM:
        return f'<div class="chips">{"".join(_chip(p) for p in parts)}</div>'
    head, bracket, note = line.partition("（")
    return f'<p><b>{_esc(head)}</b> <span class="dim">{_esc(bracket + note)}</span></p>'


def _kpi_body(rows: Sequence[Row]) -> str:
    lines = "".join(_kpi_line(i, str(row.get("value", ""))) for i, row in enumerate(rows))
    return f'<div class="kpi">{lines}</div>'


def _body(panel: Panel, token: str) -> str:
    if panel.error:
        return f'<p class="err">面板出错：{_esc(panel.error)}</p>'
    if not panel.rows:
        return f'<p class="empty">{_esc(panel.note or "暂无数据")}</p>'
    if panel.key == "workitems":
        return _workitems_body(panel, token)
    if panel.key == "kpi":
        return _kpi_body(panel.rows)
    return _table(panel.key, panel.rows, token)


def _safe_body(panel: Panel, token: str) -> str:
    """渲染出错的面板只显示错误，不拖垮整页（和 board_data._safe 对数据出错的处理一致）。"""
    try:
        return _body(panel, token)
    except Exception as exc:  # noqa: BLE001 - 审批页必须能显示
        return f'<p class="err">面板出错：{_esc(type(exc).__name__)}: {_esc(exc)}</p>'


def _is_wide(panel: Panel) -> bool:
    return panel.key in WIDE_PANELS or (panel.key in WIDE_WHEN_FILLED and bool(panel.rows))


def _section(panel: Panel, token: str) -> str:
    classes = ["panel"]
    if _is_wide(panel):
        classes.append("wide")
    if panel.key == "attention" and _open_rows(panel.rows):
        classes.append("hot")
    count = f'<span class="count">{len(panel.rows)}</span>' if panel.rows else ""
    return (
        f'<section id="{_esc(panel.key)}" class="{" ".join(classes)}">'
        f"<header><h2>{_esc(panel.title)}</h2>{count}</header>"
        f'<div class="body">{_safe_body(panel, token)}</div></section>'
    )


# ---- 顶部统计块 ------------------------------------------------------------


def _tile(key: str, target: str, label: str, value: object, sub: str, *, warn: bool = False) -> str:
    """一块统计：key 是这块统计自己的标识，target 是点了跳到哪个面板。"""
    classes = "tile warn" if warn else "tile"
    return (
        f'<a class="{classes}" href="#{target}" data-stat="{key}">'
        f'<span class="tile-label">{_esc(label)}</span>'
        f'<b class="tile-value">{_esc(value)}</b>'
        f'<span class="tile-sub">{_esc(sub)}</span></a>'
    )


def _attention_tile(panel: Panel) -> str:
    pending = len(_open_rows(panel.rows))
    waiting = sum(1 for row in panel.rows if row.get("can_approve"))
    closed = len(panel.rows) - pending
    notes = [f"{waiting} 项待批准" if waiting else "没有待批准的"]
    if closed:
        notes.append(f"另有 {closed} 条已结束")
    return _tile(
        "attention", "attention", "需要你看", pending, "，".join(notes), warn=bool(pending)
    )


def _work_tiles(panel: Panel, sessions: Panel | None) -> list[str]:
    counts = _status_counts(panel.rows)
    running = sum(counts.get(s, 0) for s in RUNNING_STATUSES)
    session_note = f"会话 {len(sessions.rows)} 个" if sessions else "排队、测试、编写中"
    backlog_note = f"积压 {counts.get('backlog', 0)} 项"
    return [
        _tile("active", "sessions", "进行中", running, session_note),
        _tile("ready", "workitems", "待派发", counts.get("ready", 0), backlog_note),
        _tile("landed", "workitems", "已合入", counts.get("landed", 0), f"共 {len(panel.rows)} 项"),
    ]


def _spend_tile(panel: Panel) -> str:
    for row in panel.rows:
        money = MONEY_PAIR.match(str(row.get("status", "")))
        if money:
            spent, limit = float(money.group(1)), float(money.group(2))
            over = bool(limit) and spent / limit >= SPEND_WARN_RATIO
            value, sub = f"${money.group(1)}", f"上限 ${money.group(2)}"
            return _tile("spend", "today", "今天花费", value, sub, warn=over)
    return ""


def _slots_tile(panel: Panel) -> str:
    busy = sum(1 for row in panel.rows if row.get("state") == SLOT_BUSY)
    return _tile("slots", "slots", "重任务槽位", f"{busy} / {len(panel.rows)}", "占用 / 总数")


def _tiles(panels: Sequence[Panel]) -> str:
    usable = {p.key: p for p in panels if not p.error}
    tiles: list[str] = []
    if "attention" in usable:
        tiles.append(_attention_tile(usable["attention"]))
    if "workitems" in usable:
        tiles.extend(_work_tiles(usable["workitems"], usable.get("sessions")))
    if "today" in usable:
        tiles.append(_spend_tile(usable["today"]))
    if usable.get("slots") and usable["slots"].rows:
        tiles.append(_slots_tile(usable["slots"]))
    filled = "".join(t for t in tiles if t)
    return f'<nav class="tiles">{filled}</nav>' if filled else ""


# ---- 整页 ------------------------------------------------------------------


def render(
    panels: Sequence[Panel],
    *,
    token: str,
    now_text: str,
    brief: Path | None = None,
    message: str = "",
    refresh_seconds: int = 30,
) -> str:
    pending = sum(len(_open_rows(p.rows)) for p in panels if p.key == "attention")
    title = f"({pending}) agentops 看板" if pending else "agentops 看板"
    wide = "".join(_section(p, token) for p in panels if _is_wide(p))
    grid = "".join(_section(p, token) for p in panels if not _is_wide(p))
    brief_note = (
        f'<span title="{_esc(brief)}">最新简报 {_esc(brief.name)}</span>'
        if brief
        else "<span>还没有每日简报</span>"
    )
    flash = f'<p class="msg" role="status">{_esc(message)}</p>' if message else ""
    return (
        f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="refresh" content="{refresh_seconds}">'
        f'<title>{_esc(title)}</title><link rel="icon" href="{FAVICON}">'
        f"<style>{CSS}</style></head><body>"
        f'<header class="top"><div class="brand">'
        f'<img class="logo" src="{FAVICON}" alt=""><h1>agentops 看板</h1></div>'
        f'<div class="meta"><span title="{_esc(now_text)}">更新于 {_esc(_clock(now_text))}</span>'
        f"<span>每 {refresh_seconds} 秒刷新</span>{brief_note}</div></header>"
        f'<div class="page">{flash}{_tiles(panels)}{wide}'
        f'<main class="grid">{grid}</main></div>'
        f'<script src="{SCRIPT_PATH}" defer></script></body></html>'
    )
