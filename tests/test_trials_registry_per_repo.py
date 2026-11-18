"""登记簿按仓库分开：[data].trials 含 {repo} 时，各仓库的登记行入账到各自的文件。"""

import json

from agentops import config as config_mod
from agentops import paths, trials

VALID = {
    "ts": "2026-11-13T12:00:00",
    "strategy": "taker_v0",
    "params": {"edge_min": 0.05},
    "data_range": {"start": "2024-01-01", "end": "2026-10-31"},
    "seed": "0",
    "git_sha": "abc1234",
}


def _config(agentops_home, template: str):
    (agentops_home / "config.toml").write_text(f'[data]\ntrials = "{template}"\n')
    return config_mod.load_config()


def test_template_routes_each_repo_to_its_own_registry(agentops_home, tmp_path):
    config = _config(agentops_home, f"{tmp_path}/data/{{repo}}/trials.jsonl")
    inbox = paths.inbox_dir() / "trials"
    inbox.mkdir(parents=True)
    (inbox / "weather-lab-1.jsonl").write_text(json.dumps(VALID) + "\n")
    (inbox / "quant-lab-1.jsonl").write_text(json.dumps(VALID) + "\n")

    assert trials.ingest(config, "weather-lab") == 1
    assert trials.ingest(config, "quant-lab") == 1

    weather = tmp_path / "data" / "weather-lab" / "trials.jsonl"
    quant = tmp_path / "data" / "quant-lab" / "trials.jsonl"
    assert len(weather.read_text().splitlines()) == 1
    assert len(quant.read_text().splitlines()) == 1


def test_path_without_template_is_shared(agentops_home, tmp_path):
    shared = tmp_path / "shared.jsonl"
    config = _config(agentops_home, str(shared))
    assert trials.registry_path(config, "weather-lab") == shared
    assert trials.registry_path(config) == shared
