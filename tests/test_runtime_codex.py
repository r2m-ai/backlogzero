"""阶段 B：runtime 适配层。Codex 后端的命令、事件解析、独立 CODEX_HOME、预算双口径，
以及 judge / PM 按仓库选运行时。全部用假的 codex / claude，不碰真实的 ~/.codex。"""

import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentops import budget, events, judge, llm, pm
from agentops import config as config_mod
from agentops import workitem_store as store
from agentops.errors import AgentopsError
from agentops.runtime import base
from agentops.runtime import codex as codex_rt

FAKE_CODEX = f"{sys.executable} {Path(__file__).parent / 'fake_codex.py'}"
FAKE_CLAUDE = f"{sys.executable} {Path(__file__).parent / 'fake_claude.py'}"
FIXTURES = Path(__file__).parent / "fixtures" / "codex"
VERDICT = {"action": "needs_human", "reason": "r", "hint": "h"}
SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}}
PM_DATA = {"priorities": [], "dispatch_order": [], "drop_candidates": [], "notes": "ok"}


def _config_text(main: Path, *, runtime: str | None = "codex", extra: str = "") -> str:
    repo = (
        f'[repos]\ndemo = "{main}"\n'
        if runtime is None
        else f'[repos.demo]\npath = "{main}"\nruntime = "{runtime}"\n'
    )
    return (
        f"[general]\ndaily_budget_usd = 10\n\n{repo}\n[steward]\nauto_dispatch = false\n{extra}\n"
    )


def _events(kind: str) -> list[dict]:
    return [dict(e.detail) for e in events.read_events() if e.type == kind]


@pytest.fixture
def codex_world(agentops_home, tmp_path, monkeypatch):
    monkeypatch.setenv(codex_rt.ENV_CODEX_CMD, FAKE_CODEX)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    monkeypatch.setenv("FAKE_CODEX_MODE", "structured=" + json.dumps(VERDICT))
    monkeypatch.delenv("FAKE_CODEX_USAGE", raising=False)
    monkeypatch.setenv(llm.ENV_CLAUDE_CMD, FAKE_CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "structured=" + json.dumps(VERDICT))
    # 审核人自己的 Codex 目录也指到临时目录：只放一个假的登录态
    user_codex = tmp_path / "user-codex"
    user_codex.mkdir()
    (user_codex / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(user_codex))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-must-be-dropped")
    main = tmp_path / "demo"
    main.mkdir()

    def configure(*, runtime="codex", extra=""):
        (agentops_home / "config.toml").write_text(_config_text(main, runtime=runtime, extra=extra))
        return config_mod.load_config()

    return SimpleNamespace(
        home=agentops_home,
        main=main,
        user_codex=user_codex,
        log=tmp_path / "codex.log",
        claude_log=tmp_path / "claude.log",
        configure=configure,
        config=configure(),
    )


def _log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ---- 配置 ----


def test_repo_table_form_selects_runtime_and_string_form_stays_claude(tmp_path):
    # Arrange
    data = tomllib.loads(
        '[repos]\nquant-lab = "~/work/quant-lab"\n\n'
        f'[repos.demo]\npath = "{tmp_path}"\nruntime = "codex"\n'
    )

    # Act
    config = config_mod.from_mapping(data)

    # Assert
    assert config.runtime_for("demo") == "codex"
    assert config.runtime_for("quant-lab") == "claude"
    assert config.repo_path("demo") == tmp_path
    assert set(config.repos) == {"quant-lab", "demo"}
    with pytest.raises(AgentopsError):
        config.runtime_for("nope")


def test_repo_table_needs_path_and_known_runtime(tmp_path):
    with pytest.raises(AgentopsError, match="path"):
        config_mod.from_mapping(tomllib.loads('[repos.demo]\nruntime = "codex"\n'))
    with pytest.raises(AgentopsError, match="runtime"):
        config_mod.from_mapping(
            tomllib.loads(f'[repos.demo]\npath = "{tmp_path}"\nruntime = "gemini"\n')
        )


def test_codex_models_effort_and_token_budgets_have_defaults_and_overrides():
    # Arrange
    defaults = config_mod.Config()
    overridden = config_mod.from_mapping(
        tomllib.loads(
            '[models]\nsteward = "opus"\n\n[models.codex]\npm = "gpt-6-luna"\n\n'
            '[effort.codex]\nsteward = ""\n\n[budget_tokens]\nsteward = 5000\n'
        )
    )

    # Assert：默认值来自 §7 第 2 项；[models] 平铺键仍归 Claude
    assert defaults.model_for("steward", "codex") == "gpt-6.1-sol"
    assert defaults.model_for("worker", "codex") == "gpt-6.1-sol"
    assert defaults.model_for("pm", "codex") == "gpt-6-astra"
    assert defaults.model_for("pm", "claude") == "sonnet"
    assert defaults.effort_for("steward", "codex") == "medium"
    assert defaults.effort_for("worker", "claude") is None
    assert defaults.token_limit("steward") == 200_000
    assert overridden.models["steward"] == "opus"
    assert overridden.model_for("steward", "claude") == "opus"
    assert overridden.model_for("pm", "codex") == "gpt-6-luna"
    assert overridden.model_for("steward", "codex") == "gpt-6.1-sol"
    assert overridden.effort_for("steward", "codex") is None
    assert overridden.token_limit("steward") == 5000
    assert overridden.token_limit("pm") == 400_000


def test_codex_price_table_defaults_and_override():
    defaults = config_mod.Config()
    overridden = config_mod.from_mapping(
        tomllib.loads('[codex.prices."gpt-6.1-sol"]\ninput = 1\ncached_input = 0.1\noutput = 5\n')
    )

    assert defaults.codex_prices["gpt-6.1-sol"]["input"] == 2.0
    assert overridden.codex_prices["gpt-6.1-sol"] == {
        "input": 1.0,
        "cached_input": 0.1,
        "output": 5.0,
    }
    assert "gpt-6-astra" in overridden.codex_prices
    with pytest.raises(AgentopsError):
        config_mod.from_mapping(tomllib.loads('[codex.prices."m"]\ninput = "cheap"\n'))


def test_default_config_toml_still_parses_with_claude_everywhere():
    config = config_mod.from_mapping(tomllib.loads(config_mod.DEFAULT_CONFIG_TOML))

    assert config.runtime_for("quant-lab") == "claude"
    assert config.models["pm"] == "sonnet"


# ---- Codex 后端 ----


def test_parse_events_reads_thread_usage_and_last_message_from_recorded_stream():
    parsed = codex_rt.parse_events((FIXTURES / "structured_ok.jsonl").read_text())

    assert parsed.thread_id == "01a1bd5b-583a-7890-9949-e8a12c2d3bdc"
    assert parsed.usage == base.Usage(9694, 7936, 0, 66, 29)
    assert parsed.usage.total == 9694 + 66
    assert json.loads(parsed.last_message)["action"] == "return_to_session"
    assert parsed.errors == () and parsed.warnings == ()
    assert parsed.turns == 1


def test_parse_events_ignores_hook_trust_warning_but_keeps_real_errors():
    warning = {
        "type": "item.completed",
        "item": {"type": "error", "message": "`--dangerously-bypass-hook-trust` is enabled. x"},
    }
    broken = {"type": "item.completed", "item": {"type": "error", "message": "something broke"}}
    failed = {"type": "turn.failed", "error": {"message": "usage limit reached"}}
    text = "\n".join(json.dumps(e) for e in (warning, warning, broken, failed)) + "\nnot json\n"

    parsed = codex_rt.parse_events(text)

    assert parsed.errors == ("usage limit reached",)
    assert parsed.warnings == ("something broke",)
    assert parsed.usage is None


def test_build_command_is_read_only_ephemeral_and_reads_prompt_from_stdin(monkeypatch):
    monkeypatch.setenv(codex_rt.ENV_CODEX_CMD, "fake-codex --flag")

    command = codex_rt.build_command(
        model="gpt-6.1-sol",
        effort="medium",
        schema_path=Path("/t/s.json"),
        output_path=Path("/t/o.json"),
        workdir=Path("/w"),
    )
    plain = codex_rt.build_command(
        model="gpt-6.1-sol",
        effort=None,
        schema_path=Path("s"),
        output_path=Path("o"),
        workdir=Path("/w"),
    )

    def after(flag):
        return command[command.index(flag) + 1]

    assert command[:3] == ["fake-codex", "--flag", "exec"]
    assert "--json" in command and "--ephemeral" in command and "--skip-git-repo-check" in command
    assert after("--profile") == "judge" and "-s" not in command
    assert after("-C") == "/w" and after("-m") == "gpt-6.1-sol"
    assert after("-c") == "model_reasoning_effort=medium"
    assert after("--output-schema") == "/t/s.json" and after("-o") == "/t/o.json"
    assert command[-1] == "-"
    assert "--dangerously-bypass-hook-trust" not in command
    assert "-c" not in plain


def test_ensure_home_writes_isolation_config_links_auth_and_is_idempotent(codex_world):
    # Act
    home = codex_rt.ensure_home()
    first = (home / "config.toml").read_text()
    again = codex_rt.ensure_home()
    config = tomllib.loads(first)
    env = codex_rt.call_env()

    # Assert：独立 CODEX_HOME 在 agentops 状态目录下，关闭清单齐全，登录态是软链
    assert home == again == codex_world.home / "codex-home"
    assert (home / "config.toml").read_text() == first
    assert config["approval_policy"] == "never"
    assert config["web_search"] == "disabled" and config["notify"] == []
    for key in (
        "apps",
        "multi_agent",
        "image_generation",
        "browser_use",
        "computer_use",
        "view_image",
    ):
        assert config["features"][key] is False, key
    assert config["apps"]["_default"]["enabled"] is False
    assert config["agents"]["enabled"] is False
    assert config["plugins"]["_default"]["enabled"] is False
    auth = home / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (codex_world.user_codex / "auth.json").resolve()
    assert env["CODEX_HOME"] == str(home)
    assert "OPENAI_API_KEY" not in env


def test_judge_profile_has_no_tools_and_denies_secret_dirs(codex_world):
    home = codex_rt.ensure_home()

    profile = tomllib.loads((home / "judge.config.toml").read_text())
    rules = profile["permissions"]["judge"]["filesystem"]

    assert profile["default_permissions"] == "judge"
    assert profile["permissions"]["judge"]["extends"] == ":read-only"
    for key in ("shell_tool", "unified_exec", "sleep_tool"):
        assert profile["features"][key] is False, key
    assert rules[str(codex_world.user_codex)] == "deny"
    assert rules[str(home)] == "deny"
    assert rules[str(codex_world.home)] == "deny"
    assert rules[str(codex_world.home / "llm")] == "read"
    assert rules[str(Path.home() / ".ssh")] == "deny"
    assert "$" not in (home / "judge.config.toml").read_text()


def test_ensure_home_repairs_dangling_auth_link_and_rewrites_modified_config(codex_world):
    # Arrange：审核人登出过（软链悬空）；Codex 或人改过主配置
    home = codex_rt.codex_home()
    home.mkdir(parents=True)
    (home / "auth.json").symlink_to(codex_world.user_codex / "gone.json")
    (home / "config.toml").write_text('approval_policy = "on-request"\n')

    codex_rt.ensure_home()

    assert (home / "auth.json").resolve() == (codex_world.user_codex / "auth.json").resolve()
    assert tomllib.loads((home / "config.toml").read_text())["approval_policy"] == "never"
    assert not list(home.glob("*.tmp"))


def test_fake_codex_without_user_login_still_renders_home(codex_world):
    (codex_world.user_codex / "auth.json").unlink()

    home = codex_rt.ensure_home()

    assert (home / "config.toml").exists() and not (home / "auth.json").exists()


def test_ensure_home_without_user_login_gives_login_instruction(codex_world, monkeypatch):
    (codex_world.user_codex / "auth.json").unlink()
    monkeypatch.delenv(codex_rt.ENV_CODEX_CMD)

    with pytest.raises(base.LlmError, match="codex login"):
        codex_rt.ensure_home()


def test_estimate_cost_uses_price_table_per_million_tokens():
    usage = base.Usage(1_000_000, 500_000, 0, 100_000, 20_000)
    prices = {"m": {"input": 2.0, "cached_input": 0.1, "output": 10.0}}

    assert codex_rt.estimate_cost("m", usage, prices) == pytest.approx(2.05)
    assert codex_rt.estimate_cost("unknown", usage, prices) is None


# ---- 分发层 ----


def test_call_structured_routes_codex_repo_to_codex_and_records_tokens(codex_world):
    # Act
    result = llm.call_structured(
        "判一下",
        SCHEMA,
        config=codex_world.config,
        role="steward",
        label="judge",
        repo="demo",
        item=1,
    )
    calls = _log(codex_world.log)
    (event,) = _events("llm_call")

    # Assert
    assert result.data == VERDICT and result.runtime == "codex" and result.cost_estimated is True
    assert result.usage.input_tokens == 1200 and result.session_id == "fake-thread"
    assert len(calls) == 1 and calls[0]["prompt"] == "判一下"
    assert calls[0]["env"]["CODEX_HOME"] == str(codex_world.home / "codex-home")
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "exec" in calls[0]["args"] and not codex_world.claude_log.exists()
    assert event["runtime"] == "codex" and event["role"] == "steward"
    assert event["model"] == "gpt-6.1-sol" and event["cost_estimated"] is True
    assert event["usage"]["input_tokens"] == 1200 and event["usage"]["output_tokens"] == 50
    # 1000 非缓存输入 × $2 + 200 缓存 × $0.1 + 50 输出 × $10，每百万 token
    assert event["cost_usd"] == pytest.approx((1000 * 2 + 200 * 0.1 + 50 * 10) / 1_000_000)
    assert budget.outstanding() == 0


def test_call_structured_keeps_claude_for_string_repos(codex_world):
    config = codex_world.configure(runtime=None)

    result = llm.call_structured("p", SCHEMA, config=config, role="pm", label="pm", repo="demo")
    (event,) = _events("llm_call")

    assert result.runtime == "claude" and result.cost_estimated is False
    assert result.cost_usd == 0.5 and result.data == VERDICT
    assert codex_world.claude_log.exists() and not codex_world.log.exists()
    assert event["runtime"] == "claude" and event["model"] == "sonnet" and event["role"] == "pm"


def test_codex_daily_token_cap_refuses_before_calling(codex_world):
    # Arrange：今天 steward 角色已经用掉 1100 token，上限 1000
    config = codex_world.configure(extra="[budget_tokens]\nsteward = 1000\n")
    events.emit(
        "llm_call",
        repo="demo",
        detail={
            "runtime": "codex",
            "role": "steward",
            "usage": {"input_tokens": 900, "output_tokens": 200},
        },
    )

    # Act / Assert
    with pytest.raises(budget.BudgetExceeded, match="token"):
        llm.call_structured("p", SCHEMA, config=config, role="steward", label="judge", repo="demo")
    assert not codex_world.log.exists()
    assert _events("budget_refused")
    # 其他角色不受影响
    llm.call_structured("p", SCHEMA, config=config, role="pm", label="pm", repo="demo")
    assert len(_log(codex_world.log)) == 1


def test_codex_error_items_are_only_warnings_when_output_arrives(codex_world, monkeypatch):
    # Arrange：实测见过 "Falling back from WebSockets to HTTPS transport." 这类 error 项，
    # 之后照常出结果
    monkeypatch.setenv("FAKE_CODEX_MODE", "warned=" + json.dumps(VERDICT))

    result = llm.call_structured(
        "p", SCHEMA, config=codex_world.config, role="steward", label="judge", repo="demo"
    )

    assert result.data == VERDICT
    assert result.raw["warnings"] == ["Falling back from WebSockets to HTTPS transport."]


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("failed", "usage limit"),
        ("rc1", "unknown key"),
        ("garbage", "结构化输出"),
        ("warned-rc1", "routing discovery failed"),
    ],
)
def test_codex_failures_become_llm_error_and_still_settle(codex_world, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    with pytest.raises(llm.LlmError, match=message):
        llm.call_structured(
            "p", SCHEMA, config=codex_world.config, role="steward", label="judge", repo="demo"
        )

    assert budget.outstanding() == 0
    assert len(_log(codex_world.log)) == 1


def test_unknown_codex_model_records_zero_cost_with_price_unknown(codex_world):
    config = codex_world.configure(extra='[models.codex]\nsteward = "gpt-7-nova"\n')

    result = llm.call_structured(
        "p", SCHEMA, config=config, role="steward", label="judge", repo="demo"
    )
    (event,) = _events("llm_call")

    assert result.cost_usd == 0.0 and result.price_known is False
    assert event["price_known"] is False and event["cost_estimated"] is True


def test_non_llm_errors_still_release_budget_reservation(codex_world, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(codex_rt, "structured_call", boom)

    with pytest.raises(OSError):
        llm.call_structured(
            "p", SCHEMA, config=codex_world.config, role="steward", label="judge", repo="demo"
        )

    assert budget.outstanding() == 0


def test_timeout_cleans_temp_files_and_settles(codex_world, monkeypatch):
    import subprocess

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=1)

    monkeypatch.setattr(codex_rt.subprocess, "run", slow)

    with pytest.raises(llm.LlmError, match="分钟"):
        llm.call_structured(
            "p", SCHEMA, config=codex_world.config, role="steward", label="judge", repo="demo"
        )

    assert not list((codex_world.home / "llm").glob("codex-*"))
    assert budget.outstanding() == 0


# ---- 调用方 ----


def test_judge_uses_codex_runtime_for_codex_repo(codex_world):
    item = store.create(repo="demo", title="改 cik_map", module="data")

    decision = judge.decide(
        codex_world.config,
        trigger="tests_fail",
        item=item,
        context="FAILED tests/test_x.py::test_y",
        default="return_to_session",
    )
    (call,) = _log(codex_world.log)

    assert decision.action == "needs_human" and decision.source == "llm"
    assert "FAILED tests/test_x.py::test_y" in call["prompt"]
    assert call["args"][call["args"].index("-m") + 1] == "gpt-6.1-sol"
    assert not codex_world.claude_log.exists()


def test_pm_plan_uses_codex_pm_model_and_effort(codex_world, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "structured=" + json.dumps(PM_DATA))
    store.create(repo="demo", title="一个工作项")

    report = pm.plan(codex_world.config, "demo")
    (call,) = _log(codex_world.log)

    assert report.source == "llm"
    assert call["args"][call["args"].index("-m") + 1] == "gpt-6-astra"
    assert "model_reasoning_effort=high" in call["args"]


# ---- 兼容 ----


def test_llm_module_still_exposes_claude_helpers():
    command = llm.build_command("p", SCHEMA, model="sonnet", budget_usd=0.5, max_turns=4)

    assert llm.ENV_CLAUDE_CMD == "AGENTOPS_CLAUDE_CMD"
    assert "--json-schema" in command and command[-1] == "p"
    assert llm.LlmError is base.LlmError
