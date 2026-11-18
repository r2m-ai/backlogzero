"""试验登记走 inbox：会话把登记行投递到 ~/.agentops/inbox/trials/，steward 入账到登记簿。"""

import json
from pathlib import Path

import pytest

from agentops import config as config_mod
from agentops import events, paths, trials

VALID = {
    "ts": "2026-10-31T18:08:56",
    "strategy": "momentum_12_1_q5",
    "params": {"quantile": 5, "cost_bps": 10.0},
    "data_range": {"start": "2005-02-28", "end": "2024-12-31"},
    "seed": "0..199",
    "git_sha": "facde9f",
    "n_trials": 12,
}


def _line(**overrides) -> str:
    return json.dumps({**VALID, **overrides}, ensure_ascii=False)


@pytest.fixture
def world(agentops_home, tmp_path):
    registry = tmp_path / "data" / "trials.jsonl"
    (agentops_home / "config.toml").write_text("\n".join(["[data]", f'trials = "{registry}"', ""]))
    inbox = paths.inbox_dir() / "trials"
    inbox.mkdir(parents=True)
    return type(
        "World", (), {"config": config_mod.load_config(), "registry": registry, "inbox": inbox}
    )


def _types():
    return [e.type for e in events.read_events()]


def test_ingest_appends_session_lines_to_the_registry_and_removes_files(world):
    # Arrange：登记簿已有 2 行；inbox 里一个本仓库文件、一个别的仓库文件
    world.registry.parent.mkdir(parents=True)
    world.registry.write_text(_line(strategy="old1") + "\n" + _line(strategy="old2") + "\n")
    mine = world.inbox / "quant-lab-20261031T180856-123-abcd1234.jsonl"
    mine.write_text(_line() + "\n")
    other = world.inbox / "other-20261031T180856-1-deadbeef.jsonl"
    other.write_text(_line(strategy="other") + "\n")

    # Act
    count = trials.ingest(world.config, "quant-lab")

    # Assert：追加 1 行、标 source=session、原字段保留；本仓库文件删掉，别的仓库文件不动
    lines = [json.loads(line) for line in world.registry.read_text().splitlines() if line.strip()]
    assert count == 1 and len(lines) == 3
    assert lines[-1]["strategy"] == "momentum_12_1_q5" and lines[-1]["n_trials"] == 12
    assert lines[-1]["source"] == "session" and lines[-1]["inbox_file"] == mine.name
    assert not mine.exists() and other.exists()
    ingested = [e for e in events.read_events() if e.type == "trials_ingested"]
    assert ingested and ingested[-1].detail["count"] == 1 and ingested[-1].repo == "quant-lab"


def test_ingest_rejects_bad_lines_and_keeps_the_registry_intact(world):
    world.registry.parent.mkdir(parents=True)
    world.registry.write_text(_line(strategy="old") + "\n")
    bad_json = world.inbox / "quant-lab-20261031T180000-1-aaaaaaaa.jsonl"
    bad_json.write_text("{not json\n")
    missing = world.inbox / "quant-lab-20261031T180001-1-bbbbbbbb.jsonl"
    missing.write_text(json.dumps({"ts": "x", "strategy": "s"}) + "\n")

    count = trials.ingest(world.config, "quant-lab")

    assert count == 0
    assert world.registry.read_text().count("\n") == 1
    assert not bad_json.exists() and bad_json.with_suffix(".bad").exists()
    assert not missing.exists() and missing.with_suffix(".bad").exists()
    assert _types().count("inbox_rejected") == 2


def test_ingest_creates_the_registry_when_missing_and_is_quiet_when_idle(world):
    assert trials.ingest(world.config, "quant-lab") == 0  # inbox 空：不写文件、不记事件
    assert not world.registry.exists() and "trials_ingested" not in _types()
    (world.inbox / "quant-lab-20261031T180002-1-cccccccc.jsonl").write_text(_line() + "\n")

    assert trials.ingest(world.config, "quant-lab") == 1

    assert world.registry.exists() and world.registry.read_text().count("\n") == 1


def test_registry_path_defaults_to_the_quant_lab_data_dir(agentops_home):
    config = config_mod.Config()

    assert trials.registry_path(config) == Path("~/data/quant-lab/trials.jsonl").expanduser()


def test_steward_round_ingests_trials_even_with_an_empty_queue(world, agentops_home):
    from agentops import steward

    (agentops_home / "config.toml").write_text(
        "\n".join(
            [
                "[data]",
                f'trials = "{world.registry}"',
                "",
                "[repos]",
                f'quant-lab = "{world.registry.parent}"',
                "",
            ]
        )
    )
    config = config_mod.load_config()
    (world.inbox / "quant-lab-20261031T180003-1-dddddddd.jsonl").write_text(_line() + "\n")

    assert steward.process_repo(config, "quant-lab") is None

    assert world.registry.read_text().count("\n") == 1 and "trials_ingested" in _types()
