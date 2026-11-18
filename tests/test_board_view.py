"""看板页面渲染（board_view）：纯函数，输入面板数据，输出整页 HTML。"""

from __future__ import annotations

import re
import threading
import urllib.request

import pytest
from sandboxing import SKIP_REASON, inside_sandbox

from agentops import board, board_view
from agentops import config as config_mod
from agentops.board_data import Panel

TOKEN = "ab" * 16
FULL_SHA = "abc1234def5678900000000000000000000000ff"


def _render(*panels: Panel, message: str = "") -> str:
    return board_view.render(
        panels, token=TOKEN, now_text="2026-11-17T10:00:00.000-08:00", message=message
    )


def _tile_value(page: str, key: str) -> str:
    found = re.search(rf'data-stat="{key}".*?<b class="tile-value">(.*?)</b>', page, re.S)
    assert found, f"没有 {key} 这块统计"
    return found.group(1)


def _queue_row(status: str, ts: str = "2026-11-17T09:05:03.123-08:00") -> dict[str, object]:
    return {
        "repo": "demo",
        "item": "demo#1",
        "status": status,
        "sha": "abc1234",
        "ts": ts,
        "detail": "{}",
    }


def _work_row(ref: str, status: str, title: str) -> dict[str, object]:
    return {
        "ref": ref,
        "status": status,
        "priority": "P1",
        "module": "core",
        "trigger": "auto",
        "deps": "-",
        "blocked_by": "-",
        "title": title,
    }


def test_empty_panel_shows_its_note_and_broken_panel_shows_its_error():
    # Arrange
    empty = Panel("sessions", "正在跑的会话", (), note="暂无会话")
    broken = Panel("disk", "磁盘", (), error="OSError: boom")

    # Act
    page = _render(empty, broken)

    # Assert
    assert "正在跑的会话" in page and "暂无会话" in page
    assert "磁盘" in page and "面板出错：OSError: boom" in page


def test_status_cells_render_as_toned_pills():
    # Arrange
    rows = [_queue_row("landed"), _queue_row("needs_human"), _queue_row("weird")]

    # Act
    page = _render(Panel("queue", "合并队列", rows))

    # Assert：认识的状态带色调，不认识的状态是中性标签
    assert 'class="pill good">landed<' in page
    assert 'class="pill warn">needs_human<' in page
    assert 'class="pill">weird<' in page


def test_approval_row_shows_short_sha_and_posts_full_sha_with_token():
    # Arrange
    row = {
        "ref": "demo#7",
        "repo": "demo",
        "id": 7,
        "status": "needs_human",
        "why": "needs_human",
        "hint": "",
        "approval_sha": FULL_SHA,
        "can_approve": True,
        "can_requeue": False,
        "can_drop": True,
    }

    # Act
    page = _render(Panel("attention", "需要你看", [row]))

    # Assert：单元格只显示 7 位，表单里是完整 SHA 和令牌
    assert f'title="{FULL_SHA}">abc1234<' in page
    assert 'action="/approve"' in page and f'name="token" value="{TOKEN}"' in page
    assert f'name="sha" value="{FULL_SHA}"' in page
    assert 'name="ref" value="demo#7"' in page
    assert 'action="/drop"' in page and "确定放弃 demo#7" in page
    assert 'action="/requeue"' not in page


def test_closed_work_items_are_folded_and_open_ones_stay_visible():
    # Arrange
    rows = [
        _work_row("demo#1", "landed", "已经合入的那项"),
        _work_row("demo#2", "in_progress", "还在做的那项"),
        _work_row("demo#3", "dropped", "放弃了的那项"),
    ]

    # Act
    page = _render(Panel("workitems", "工作项", rows))

    # Assert：未结束的在折叠区前面，已结束的在折叠区里面
    fold = page.index('id="wi-closed"')
    assert page.index("还在做的那项") < fold < page.index("已经合入的那项")
    assert fold < page.index("放弃了的那项")
    assert "已结束 2 项" in page


def test_summary_tiles_count_attention_and_in_progress_items():
    # Arrange
    attention_rows = [
        {"ref": "demo#1", "status": "needs_human", "can_approve": True, "approval_sha": FULL_SHA},
        {"ref": "demo#2", "status": "returned", "can_approve": False, "approval_sha": ""},
    ]
    work_rows = [
        _work_row("demo#1", "testing", "a"),
        _work_row("demo#2", "queued", "b"),
        _work_row("demo#3", "ready", "c"),
        _work_row("demo#4", "landed", "d"),
    ]

    # Act
    page = _render(
        Panel("attention", "需要你看", attention_rows), Panel("workitems", "工作项", work_rows)
    )

    # Assert
    assert _tile_value(page, "attention") == "2"
    assert _tile_value(page, "active") == "2"
    assert _tile_value(page, "ready") == "1"
    assert _tile_value(page, "landed") == "1"
    assert "1 项待批准" in page


def test_closed_items_in_attention_do_not_raise_the_alarm():
    # Arrange：面板里只剩一条已经合入、只是历史上退回过的记录
    rows = [{"ref": "demo#1", "status": "landed", "why": "退回 2 次", "can_approve": False}]

    # Act
    page = _render(Panel("attention", "需要你看", rows))

    # Assert：统计块和标题只数还要人处理的，面板不标黄，但记录仍然列出来
    assert _tile_value(page, "attention") == "0"
    assert "另有 1 条已结束" in page and "退回 2 次" in page
    assert 'class="tile warn"' not in page and "panel wide hot" not in page
    assert "<title>agentops 看板</title>" in page


def test_non_timestamp_values_in_time_columns_are_shown_plainly():
    # Arrange
    rows = [{"source": "demo tickers", "latest": "12345", "note": ""}]

    # Act
    page = _render(Panel("freshness", "数据新鲜度", rows))

    # Assert
    assert '<td class="num">12345</td>' in page


def test_spend_row_feeds_the_spend_tile():
    # Arrange
    rows = [
        {"when": "14:00 计划", "what": "capture（采集）", "status": "已运行"},
        {"when": "今天", "what": "花费（估算）", "status": "$3.50 / $20.0"},
    ]

    # Act
    page = _render(Panel("today", "今天的计划和实际运行", rows))

    # Assert
    assert _tile_value(page, "spend") == "$3.50"
    assert 'class="pill good">已运行<' in page


def test_cell_text_and_message_are_escaped():
    # Arrange
    rows = [_work_row("demo#1", "ready", "<script>alert(1)</script>")]

    # Act
    page = _render(Panel("workitems", "工作项", rows), message="<b>提示</b>")

    # Assert
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page
    assert "<b>提示</b>" not in page and "&lt;b&gt;提示&lt;/b&gt;" in page


def test_timestamps_are_shortened_with_full_value_in_title():
    # Arrange
    ts = "2026-11-17T09:05:03.123-08:00"

    # Act
    page = _render(Panel("queue", "合并队列", [_queue_row("landed", ts)]))

    # Assert
    assert f'title="{ts}">11-17 09:05<' in page


def test_usage_percent_renders_a_clamped_meter():
    # Arrange
    rows = [
        {"what": "home", "path": "/somewhere", "free_gb": "400", "used_pct": "25%"},
        {"what": "data", "path": "/elsewhere", "free_gb": "1", "used_pct": "130%"},
    ]

    # Act
    page = _render(Panel("disk", "磁盘", rows))

    # Assert
    assert 'style="width:25%"' in page and 'style="width:100%"' in page
    assert "25%" in page and "130%" in page


def test_long_kpi_breakdown_is_folded():
    # Arrange
    costs = "，".join(f"demo#{n} $0.1{n % 10}" for n in range(1, 31))
    rows = [
        {"metric": "合入 5，退回 1，冲突 0", "value": "合入 5，退回 1，冲突 0"},
        {"metric": "返工率 10%", "value": "返工率 10%（returned ÷ land_requested 40）"},
        {"metric": f"每项成本：{costs}", "value": f"每项成本：{costs}"},
    ]

    # Act
    page = _render(Panel("kpi", "KPI（7 天）", rows))

    # Assert：长明细进折叠区，摘要里有项数；短行直接显示
    fold = page.index("<details")
    assert fold < page.index("demo#30")
    assert page.index("返工率 10%") < fold
    assert "每项成本（30 项）" in page


def test_page_refreshes_itself_and_marks_pending_count_in_title():
    # Arrange
    rows = [{"ref": "demo#1", "status": "needs_human", "can_approve": False, "approval_sha": ""}]

    # Act
    page = _render(Panel("attention", "需要你看", rows))

    # Assert
    assert '<meta http-equiv="refresh" content="30">' in page
    assert "<title>(1) agentops 看板</title>" in page


def test_page_has_no_inline_script_and_loads_the_board_script_by_url():
    # Act
    page = _render(Panel("sessions", "正在跑的会话", (), note="暂无会话"))

    # Assert：脚本走单独的地址，页面里不出现内联 <script>
    assert "<script>" not in page
    assert '<script src="/board.js" defer></script>' in page


@pytest.fixture
def server(agentops_home, tmp_path):
    if inside_sandbox():
        pytest.skip(SKIP_REASON)
    (agentops_home / "config.toml").write_text(f'[repos]\ndemo = "{tmp_path}"\n')
    srv = board.make_server(config_mod.load_config(), 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_board_script_is_served_as_javascript(server):
    # Act
    with urllib.request.urlopen(server + "/board.js", timeout=10) as resp:
        status, kind, body = resp.status, resp.headers["Content-Type"], resp.read().decode()

    # Assert
    assert status == 200 and kind.startswith("text/javascript")
    assert "details[data-keep]" in body


def test_odd_digit_characters_in_count_columns_do_not_break_the_page():
    # Arrange：isdigit() 为真但 int() 解析不了的字符
    rows = [{"repo": "demo", "generated": "-", "pass": "²", "warn": "0", "error": "3", "note": ""}]

    # Act
    page = _render(Panel("dbt", "dbt 结果", rows))

    # Assert
    assert "²" in page and 'class="num t-bad">3<' in page and "面板出错" not in page


def test_a_panel_that_fails_to_render_shows_an_error_and_spares_the_rest(monkeypatch):
    # Arrange：让提交号这一列的渲染抛异常
    def boom(text: str) -> str:
        raise RuntimeError("render boom")

    monkeypatch.setitem(board_view.CELLS, "sha", boom)
    broken = Panel("queue", "合并队列", [_queue_row("landed")])
    healthy = Panel("sessions", "正在跑的会话", (), note="暂无会话")

    # Act
    page = _render(broken, healthy)

    # Assert：坏的面板只显示错误，其余面板照常
    assert "合并队列" in page and "面板出错：RuntimeError: render boom" in page
    assert "暂无会话" in page
