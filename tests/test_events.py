import json
import threading
from datetime import timedelta

import pytest

from agentops import events
from agentops.paths import events_file
from agentops.timeutil import now, parse_duration, parse_iso


def test_emit_appends_one_jsonl_line_with_timezone(agentops_home):
    events.emit(
        "session_start", repo="quant-lab", item=4, session="abc", detail={"mode": "headless"}
    )

    lines = events_file().read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "session_start"
    assert parsed["item"] == 4
    assert parsed["detail"]["mode"] == "headless"
    assert parse_iso(parsed["ts"]).tzinfo is not None


def test_read_and_filter_by_type_repo_item(agentops_home):
    events.emit("a", repo="r1", item=1)
    events.emit("b", repo="r1", item=2)
    events.emit("a", repo="r2", item=1)

    all_events = events.read_events()
    assert [e.type for e in all_events] == ["a", "b", "a"]
    assert len(events.filter_events(all_events, types=("a",))) == 2
    assert len(events.filter_events(all_events, repo="r1")) == 2
    assert len(events.filter_events(all_events, repo="r1", item=2)) == 1


def test_filter_since(agentops_home):
    events.emit("x")
    all_events = events.read_events()

    assert len(events.filter_events(all_events, since=now() - timedelta(hours=1))) == 1
    assert len(events.filter_events(all_events, since=now() + timedelta(hours=1))) == 0


def test_concurrent_appends_do_not_interleave(agentops_home):
    def worker(tag: int) -> None:
        for n in range(50):
            events.emit("tick", detail={"tag": tag, "n": n, "pad": "x" * 200})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = events_file().read_text().splitlines()
    assert len(lines) == 400
    assert all(json.loads(line)["type"] == "tick" for line in lines)


def test_corrupt_line_is_skipped_with_warning(agentops_home, capsys):
    events.emit("good")
    with open(events_file(), "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    events.emit("good")

    loaded = events.read_events()

    assert [e.type for e in loaded] == ["good", "good"]
    assert "events.jsonl" in capsys.readouterr().err


def test_parse_duration():
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        parse_duration("tomorrow")
