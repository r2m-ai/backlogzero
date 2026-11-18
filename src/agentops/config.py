"""~/.agentops/config.toml：并发数、预算、模型、槽位、仓库路径。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from agentops.errors import AgentopsError
from agentops.paths import config_file
from agentops.runtime.base import DEFAULT_RUNTIME, RUNTIMES

DEFAULT_CONFIG_TOML = """# agentops 运行配置。改后下一次 ao 命令生效；常驻进程需重启。

[general]
max_sessions = 2                  # 同时运行的 agent 会话上限
daily_budget_usd = 15             # 当日所有无人值守调用的预算上限（客户端估算）
quiet_hours = ["22:00", "08:00"]  # 免打扰时段：只记事件，不弹通知

[models]
worker = "sonnet"
steward = "sonnet"
pm = "sonnet"

[models.codex]                    # runtime = "codex" 的仓库用这组模型（审核人 2026-11-08 定）
worker = "gpt-6.1-sol"
steward = "gpt-6.1-sol"
pm = "gpt-6-astra"

[budget_usd]
worker = 3
steward = 0.5
pm = 1

[budget_tokens]                   # Codex 当日硬上限（未缓存输入 + 输出，不计缓存命中）
worker = 2000000
steward = 200000
pm = 400000

[slots]
test = 2                          # 重任务槽位数，机器级共享

[repos]
quant-lab = "~/work/quant-lab"
# 换运行时的仓库写成表：[repos.<name>] 下 path = "~/work/<name>"、runtime = "codex"
# （省略 runtime 即 claude）

[data]
edgar_manual = "~/data/quant-lab/edgar_manual"
holdout = "~/data/quant-lab/holdout"

[workspace]
clones = "~/work/.clones"        # 每个工作项一份独立克隆：<clones>/<repo>/<id>-<slug>
integration = "~/work/.wt"       # steward 的集成 worktree：<integration>/<repo>/_integration

[steward]
poll_seconds = 20                # 常驻循环的轮询间隔
test_timeout_minutes = 10        # 集成测试超时
max_returns = 2                  # 同一工作项被退回超过这个次数就转 needs_human
sandbox = true                   # 候选代码的测试用 sandbox-exec 隔离（只能写集成副本，不能联网）
judge = true                     # 冲突、测试失败、规则触发时调用一次 LLM 判断层（C4）
auto_resume = true               # 退回无人值守工作项时自动让原会话重做一轮
nightly_max = 2                  # ao pm --dispatch --nightly 最多派发几项
auto_dispatch = true             # 合入后（以及队列空闲时）由 steward 自动派发可派发的下游工作项

[sessions]
headless_timeout_minutes = 60    # 无人值守会话的总时长上限
stale_after_hours = 6            # 超过这么久没有新记录的"运行中"会话视为陈旧，不占并发

[board]
port = 8749                      # 只监听 127.0.0.1

[schedule]
capture = "14:00"                # 太平洋时间，美股收盘后
nightly = "01:30"

[review]
# ao review 的审稿命令。{repo} 是套件仓库路径，{output} 是审稿正文输出文件，prompt 走标准输入
codex_cmd = "codex exec -s read-only -C {repo} --skip-git-repo-check -o {output} -"
redact = []  # 发给审稿人前，这些词替换为“审核人”，例如 ["张三"]；家目录路径替换为 ~
diff_exclude = ["uv.lock", "*.lock"]  # diff 里排除的文件
max_diff_lines = 4000
max_test_lines = 300
timeout_minutes = 30

[codex.prices."gpt-6.1-sol"]      # 每百万 token 的美元（2026-11 公开价），只用于估算，看板标"估"
input = 2
cached_input = 0.1
output = 10

[codex.prices."gpt-6-astra"]
input = 10
cached_input = 1
output = 50
"""

# Codex 模型每百万 token 的美元（input / cached_input / output），2026-11 公开价，只用于估算
DEFAULT_CODEX_PRICES: Mapping[str, Mapping[str, float]] = MappingProxyType(
    {
        "gpt-6.1-sol": MappingProxyType({"input": 2.0, "cached_input": 0.10, "output": 10.0}),
        "gpt-6-astra": MappingProxyType({"input": 10.0, "cached_input": 1.0, "output": 50.0}),
    }
)


def _frozen(mapping: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class Config:
    max_sessions: int = 2
    daily_budget_usd: float = 15.0
    quiet_hours: tuple[str, str] = ("22:00", "08:00")
    models: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"worker": "sonnet", "steward": "sonnet", "pm": "sonnet"})
    )
    budget_usd: Mapping[str, float] = field(
        default_factory=lambda: _frozen({"worker": 3.0, "steward": 0.5, "pm": 1.0})
    )
    # 思考强度（claude --effort）：空字符串表示不传，用 Claude Code 默认
    effort: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"worker": "", "steward": "medium", "pm": "high"})
    )
    # Codex 运行时的模型与思考强度（model_reasoning_effort）；
    # [models] / [effort] 的平铺键继续归 Claude
    models_codex: Mapping[str, str] = field(
        default_factory=lambda: _frozen(
            {"worker": "gpt-6.1-sol", "steward": "gpt-6.1-sol", "pm": "gpt-6-astra"}
        )
    )
    effort_codex: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"worker": "", "steward": "medium", "pm": "high"})
    )
    # Codex 侧的当日 token 硬上限（输入 + 输出），按角色；美元预算对 Codex 只是估算口径
    budget_tokens: Mapping[str, int] = field(
        default_factory=lambda: _frozen({"worker": 2_000_000, "steward": 200_000, "pm": 400_000})
    )
    # Codex 模型每百万 token 的价格，只用于估算
    codex_prices: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: _frozen(DEFAULT_CODEX_PRICES)
    )
    slots: Mapping[str, int] = field(default_factory=lambda: _frozen({"test": 2}))
    repos: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"quant-lab": "~/work/quant-lab"})
    )
    # 每个仓库的运行时（claude / codex）；不在表里的仓库用 claude
    repo_runtimes: Mapping[str, str] = field(default_factory=lambda: _frozen({}))
    data: Mapping[str, str] = field(
        default_factory=lambda: _frozen(
            {
                "edgar_manual": "~/data/quant-lab/edgar_manual",
                "holdout": "~/data/quant-lab/holdout",
                "trials": "~/data/quant-lab/trials.jsonl",  # 试验登记簿，只由人侧（steward）追加
            }
        )
    )
    workspace: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"clones": "~/work/.clones", "integration": "~/work/.wt"})
    )
    steward: Mapping[str, object] = field(
        default_factory=lambda: _frozen(
            {
                "poll_seconds": 20,
                "test_timeout_minutes": 10,
                "max_returns": 2,
                "sandbox": True,
                "judge": True,
                "auto_resume": True,
                "nightly_max": 2,
                "auto_dispatch": True,
            }
        )
    )
    sessions: Mapping[str, object] = field(
        default_factory=lambda: _frozen({"headless_timeout_minutes": 60, "stale_after_hours": 6})
    )
    board: Mapping[str, object] = field(default_factory=lambda: _frozen({"port": 8749}))
    # PM 提案：夜间由 PM 读研究背景提出 backlog 工作项，人在看板采纳或放弃
    pm: Mapping[str, object] = field(
        default_factory=lambda: _frozen(
            {
                "propose": False,
                "max_proposals": 2,
                "max_open": 3,
                "context_files": ("docs/research-log.md",),
                "context_chars": 6000,
            }
        )
    )
    # 会话之外的定时任务（just capture / nightly）从钥匙串注入的 token：名字 -> 环境变量名
    secrets: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"eodhd": "EODHD_API_TOKEN"})
    )
    schedule: Mapping[str, str] = field(
        default_factory=lambda: _frozen({"capture": "14:00", "nightly": "01:30"})
    )
    review: Mapping[str, object] = field(
        default_factory=lambda: _frozen(
            {
                "codex_cmd": (
                    "codex exec -s read-only -C {repo} --skip-git-repo-check -o {output} -"
                ),
                "redact": [],
                "diff_exclude": ["uv.lock", "*.lock"],
                "max_diff_lines": 4000,
                "max_test_lines": 300,
                "timeout_minutes": 30,
            }
        )
    )

    def slot_count(self, name: str, default: int = 2) -> int:
        return int(self.slots.get(name, default))

    def repo_path(self, repo: str) -> Path:
        if repo not in self.repos:
            raise AgentopsError(f"config.toml 的 [repos] 里没有 {repo!r}")
        return Path(str(self.repos[repo])).expanduser()

    def runtime_for(self, repo: str) -> str:
        if repo not in self.repos:
            raise AgentopsError(f"config.toml 的 [repos] 里没有 {repo!r}")
        return str(self.repo_runtimes.get(repo, DEFAULT_RUNTIME))

    def model_for(self, role: str, runtime: str) -> str:
        table, where = (
            (self.models_codex, "models.codex") if runtime == "codex" else (self.models, "models")
        )
        if role not in table:
            raise AgentopsError(f"config.toml 的 [{where}] 里没有 {role!r}")
        return str(table[role])

    def effort_for(self, role: str, runtime: str) -> str | None:
        """空字符串表示不传，用 CLI 默认。"""
        table = self.effort_codex if runtime == "codex" else self.effort
        return str(table.get(role) or "") or None

    def token_limit(self, role: str) -> int:
        if role not in self.budget_tokens:
            raise AgentopsError(f"config.toml 的 [budget_tokens] 里没有 {role!r}")
        return int(self.budget_tokens[role])

    def clones_root(self) -> Path:
        return Path(str(self.workspace["clones"])).expanduser()

    def integration_root(self) -> Path:
        return Path(str(self.workspace["integration"])).expanduser()

    def steward_int(self, key: str) -> int:
        return int(self.steward[key])  # type: ignore[arg-type]

    def data_path(self, key: str) -> Path:
        if key not in self.data:
            raise AgentopsError(f"config.toml 的 [data] 里没有 {key!r}")
        return Path(str(self.data[key])).expanduser()


def _section(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise AgentopsError(f"config.toml 的 [{name}] 必须是一个表")
    return value


def _int(section: Mapping[str, object], key: str, default: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentopsError(f"config.toml 的 {where}.{key} 必须是整数，得到 {value!r}")
    return value


def _float(section: Mapping[str, object], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AgentopsError(f"config.toml 的 {where}.{key} 必须是数字，得到 {value!r}")
    return float(value)


def _typed_table(
    section: Mapping[str, object], where: str, kind: type, label: str
) -> dict[str, object]:
    for key, value in section.items():
        if isinstance(value, bool) or not isinstance(value, kind):
            raise AgentopsError(f"config.toml 的 {where}.{key} 必须是{label}，得到 {value!r}")
    return dict(section)


def _split_codex(
    section: Mapping[str, object], where: str
) -> tuple[dict[str, object], dict[str, object]]:
    """[models] / [effort] 的平铺键归 Claude，子表 codex 归 Codex。"""
    codex = section.get("codex", {})
    if not isinstance(codex, dict):
        raise AgentopsError(f"config.toml 的 [{where}.codex] 必须是一个表")
    plain = {k: v for k, v in section.items() if k != "codex"}
    return (
        _typed_table(plain, where, str, "字符串"),
        _typed_table(codex, f"{where}.codex", str, "字符串"),
    )


def _repos(section: Mapping[str, object]) -> tuple[dict[str, str], dict[str, str]]:
    """[repos] 的值是路径字符串（claude），或带 path、可选 runtime 的表。

    返回 (路径表, 运行时表)。
    """
    paths: dict[str, str] = {}
    runtimes: dict[str, str] = {}
    for name, value in section.items():
        if isinstance(value, str):
            paths[name] = value
            continue
        if not isinstance(value, dict):
            raise AgentopsError(f"config.toml 的 repos.{name} 必须是路径字符串或带 path 的表")
        path = value.get("path")
        if not isinstance(path, str) or not path:
            raise AgentopsError(f"config.toml 的 repos.{name} 缺少 path")
        runtime = value.get("runtime", DEFAULT_RUNTIME)
        if runtime not in RUNTIMES:
            raise AgentopsError(
                f"config.toml 的 repos.{name}.runtime 必须是 {' / '.join(RUNTIMES)}，"
                f"得到 {runtime!r}"
            )
        paths[name] = path
        runtimes[name] = str(runtime)
    return paths, runtimes


def _prices(section: Mapping[str, object]) -> dict[str, Mapping[str, float]]:
    prices: dict[str, Mapping[str, float]] = {}
    for model, table in section.items():
        if not isinstance(table, dict):
            raise AgentopsError(f"config.toml 的 codex.prices.{model} 必须是一个表")
        numbers = _typed_table(table, f"codex.prices.{model}", int | float, "数字")
        prices[model] = _frozen({k: float(v) for k, v in numbers.items()})  # type: ignore[arg-type]
    return prices


def from_mapping(data: Mapping[str, object]) -> Config:
    general = _section(data, "general")
    defaults = Config()
    quiet = general.get("quiet_hours", list(defaults.quiet_hours))
    if not (isinstance(quiet, list) and len(quiet) == 2):
        raise AgentopsError(
            'config.toml 的 general.quiet_hours 必须是两个时间，例如 ["22:00", "08:00"]'
        )
    models, models_codex = _split_codex(_section(data, "models"), "models")
    effort, effort_codex = _split_codex(_section(data, "effort"), "effort")
    budgets = {
        k: float(v)
        for k, v in _typed_table(
            _section(data, "budget_usd"), "budget_usd", int | float, "数字"
        ).items()
    }
    slots = _typed_table(_section(data, "slots"), "slots", int, "整数")
    repos, runtimes = _repos(_section(data, "repos")) if "repos" in data else (None, {})
    budget_tokens = _typed_table(_section(data, "budget_tokens"), "budget_tokens", int, "整数")
    prices = _prices(_section(_section(data, "codex"), "prices"))
    paths = _typed_table(_section(data, "data"), "data", str, "字符串")
    return Config(
        max_sessions=_int(general, "max_sessions", defaults.max_sessions, "general"),
        daily_budget_usd=_float(general, "daily_budget_usd", defaults.daily_budget_usd, "general"),
        quiet_hours=(str(quiet[0]), str(quiet[1])),
        models=_frozen({**defaults.models, **models}),
        effort=_frozen({**defaults.effort, **effort}),
        budget_usd=_frozen({**defaults.budget_usd, **budgets}),
        slots=_frozen({**defaults.slots, **slots}),
        repos=_frozen(defaults.repos if repos is None else repos),
        repo_runtimes=_frozen(runtimes),
        models_codex=_frozen({**defaults.models_codex, **models_codex}),
        effort_codex=_frozen({**defaults.effort_codex, **effort_codex}),
        budget_tokens=_frozen({**defaults.budget_tokens, **budget_tokens}),
        codex_prices=_frozen({**defaults.codex_prices, **prices}),
        data=_frozen({**defaults.data, **paths}),
        workspace=_frozen(
            {
                **defaults.workspace,
                **_typed_table(_section(data, "workspace"), "workspace", str, "字符串"),
            }
        ),
        steward=_frozen({**defaults.steward, **_section(data, "steward")}),
        sessions=_frozen({**defaults.sessions, **_section(data, "sessions")}),
        board=_frozen({**defaults.board, **_section(data, "board")}),
        pm=_frozen({**defaults.pm, **_section(data, "pm")}),
        secrets=_frozen(
            {
                **defaults.secrets,
                **_typed_table(_section(data, "secrets"), "secrets", str, "字符串"),
            }
        ),
        schedule=_frozen(
            {
                **defaults.schedule,
                **_typed_table(_section(data, "schedule"), "schedule", str, "字符串"),
            }
        ),
        review=_frozen({**defaults.review, **_section(data, "review")}),
    )


def load_config(path: Path | None = None) -> Config:
    target = path or config_file()
    if not target.exists():
        return Config()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise AgentopsError(f"{target} 解析失败: {exc}") from exc
    return from_mapping(data)
