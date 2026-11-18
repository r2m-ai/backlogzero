"""试验登记的 inbox 入账。

会话沙箱写不了登记簿（~/data/<repo>/trials.jsonl），业务侧的 register_trial 把登记行投递到
~/.agentops/inbox/trials/<repo>-*.jsonl（一行一文件）；steward 每轮把它们追加进登记簿并删除文件。
登记簿只由人侧追加；入账的行加 source=session 和 inbox_file，便于事后追溯。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentops import events
from agentops.config import Config
from agentops.paths import inbox_dir

REQUIRED_KEYS = ("ts", "strategy", "params", "data_range", "seed", "git_sha")
DEFAULT_REGISTRY = "~/data/quant-lab/trials.jsonl"


def registry_path(config: Config, repo: str | None = None) -> Path:
    """登记簿路径。[data].trials 写成 "~/data/{repo}/trials.jsonl" 时按仓库分开；
    不含 {repo} 时所有仓库共用一个文件（旧行为）。"""
    raw = str(config.data.get("trials") or DEFAULT_REGISTRY)
    if repo is not None and "{repo}" in raw:
        raw = raw.replace("{repo}", repo)
    return Path(raw).expanduser()


def trials_inbox_dir() -> Path:
    return inbox_dir() / "trials"


def _parse(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError("不是 JSON 对象")
        missing = [key for key in REQUIRED_KEYS if key not in obj]
        if missing:
            raise ValueError(f"缺字段 {missing}")
        records.append(obj)
    return records


def ingest(config: Config, repo: str) -> int:
    """把 inbox 里属于 repo 的登记行追加进登记簿，返回入账行数。坏文件改名 .bad 并记事件。"""
    root = trials_inbox_dir()
    if not root.exists():
        return 0
    registry = registry_path(config, repo)
    count = 0
    for path in sorted(root.glob(f"{repo}-*.jsonl")):
        try:
            records = _parse(path)
        except (ValueError, OSError) as exc:
            bad = path.with_suffix(".bad")
            path.replace(bad)
            events.emit("inbox_rejected", repo=repo, detail={"file": str(bad), "error": str(exc)})
            continue
        registry.parent.mkdir(parents=True, exist_ok=True)
        with registry.open("a", encoding="utf-8") as handle:
            for record in records:
                stamped = {**record, "source": "session", "inbox_file": path.name}
                handle.write(json.dumps(stamped, ensure_ascii=False) + "\n")
        path.unlink()
        count += len(records)
    if count:
        events.emit(
            "trials_ingested", repo=repo, detail={"count": count, "registry": str(registry)}
        )
    return count
