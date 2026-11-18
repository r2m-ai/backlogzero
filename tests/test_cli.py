import json

from agentops import cli
from agentops import workitems as wi
from agentops.paths import config_file


def test_wi_new_and_ready_gate(agentops_home, capsys):
    assert (
        cli.main(["wi", "new", "--repo", "quant-lab", "--title", "采集", "--module", "data"]) == 0
    )

    assert cli.main(["wi", "ready", "quant-lab#1"]) == 2
    assert "acceptance" in capsys.readouterr().err

    assert cli.main(["wi", "set", "quant-lab#1", "--add-acceptance", "tests pass"]) == 0
    assert cli.main(["wi", "ready", "quant-lab#1"]) == 0
    assert wi.load("quant-lab", 1).status == "ready"


def test_wi_new_with_ready_flag_needs_acceptance(agentops_home):
    assert cli.main(["wi", "new", "--repo", "r", "--title", "t", "--ready"]) == 2
    assert (
        cli.main(["wi", "new", "--repo", "r", "--title", "t", "--ready", "--acceptance", "x"]) == 0
    )


def test_wi_list_and_show_json(agentops_home, capsys):
    cli.main(["wi", "new", "--repo", "quant-lab", "--title", "a", "--acceptance", "x"])
    cli.main(["wi", "new", "--repo", "quant-lab", "--title", "b", "--depends", "1"])
    capsys.readouterr()

    assert cli.main(["wi", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [row["ref"] for row in listed] == ["quant-lab#1", "quant-lab#2"]
    assert listed[1]["blocked_by"] == [1]

    assert cli.main(["wi", "show", "quant-lab#2", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["depends_on"] == [1]


def test_wi_set_invalid_transition_is_rejected(agentops_home, capsys):
    cli.main(["wi", "new", "--repo", "r", "--title", "t"])

    assert cli.main(["wi", "set", "r#1", "--status", "landed"]) == 2
    assert cli.main(["wi", "set", "r#1", "--status", "landed", "--force"]) == 0


def test_events_emit_and_tail(agentops_home, capsys):
    code = cli.main(
        [
            "events",
            "emit",
            "hook_block",
            "--repo",
            "quant-lab",
            "--item",
            "1",
            "--kv",
            "path=tests/x.py",
        ]
    )
    assert code == 0
    capsys.readouterr()

    assert cli.main(["events", "tail", "--json", "--type", "hook_block"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["detail"]["path"] == "tests/x.py"
    assert rows[0]["item"] == 1


def test_session_register_list_end(agentops_home, capsys):
    args = [
        "session",
        "register",
        "--session-id",
        "s1",
        "--repo",
        "r",
        "--item",
        "2",
        "--mode",
        "headless",
    ]
    assert cli.main(args) == 0
    capsys.readouterr()

    assert cli.main(["session", "list", "--json"]) == 0
    assert [row["session_id"] for row in json.loads(capsys.readouterr().out)] == ["s1"]

    assert cli.main(["session", "end", "--session-id", "s1", "--rc", "0"]) == 0
    capsys.readouterr()
    assert cli.main(["session", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_init_is_idempotent(agentops_home):
    assert cli.main(["init"]) == 0
    assert cli.main(["init"]) == 0
    assert config_file().exists()


def test_doctor_runs(agentops_home, capsys):
    code = cli.main(["doctor", "--no-auth"])

    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "claude" in out
