# agentops 使用手册

本手册是 [README](../README.md) 的详细版。本地 agent 研发流水线套件，提供 `ao` 命令：工作项、会话克隆、合并队列、看板、PM、评审和定时任务都从这里管。

运行状态默认在 `~/.agentops/`，可用环境变量 `AGENTOPS_HOME` 改到别处（测试用临时目录）。

## 布局

| 路径 | 用途 |
|---|---|
| `~/agentops` | 套件仓库（本仓库），提供 `ao` 命令 |
| `~/.agentops` | 运行状态：工作项、队列、审批、事件、会话、槽位锁、inbox、`config.toml` |
| `~/work/<repo>` | 业务仓库，main 只由 steward 合入 |
| `~/work/.clones` | 每个工作项的会话克隆 |

## 安装与自测

```bash
cd ~/agentops
just sync      # 建 .venv
just check     # ruff + pytest
just install   # 把 ao 装到 ~/.local/bin（可编辑安装），只在会话外由人执行
ao doctor      # 检查工具、认证、状态目录；有 FAIL 退出码 1
```

首次使用先运行 `ao init` 建 `~/.agentops` 结构和默认 `config.toml`（可重复运行）。

## ao 子命令一览

| 子命令 | 说明 |
|---|---|
| `wi` | 工作项 |
| `events` | 事件日志 |
| `slot` | 在重任务槽位里运行命令 |
| `session` | 会话登记 |
| `pm` | PM Agent：排优先级、写报告；`--dispatch` 派发 |
| `kpi` | 从事件日志算 KPI |
| `board` | 看板：只监听 127.0.0.1:8749 的本地网页 |
| `launchd` | 4 个 launchd 任务的 plist 渲染、状态和安装命令 |
| `capture` | 定时采集入口：每个仓库 `caffeinate -i just capture` |
| `nightly` | 夜间任务：`just nightly` → `ao pm` → `ao pm --dispatch --nightly` → 简报 |
| `brief` | 写今天的每日简报到 `~/.agentops/briefs/` |
| `repo` | 接入业务仓库 |
| `review` | Codex 只读评审一个阶段 |
| `start` | 为工作项建克隆并启动会话（交互开 Terminal 窗口；`--headless` 无人值守） |
| `resume` | 让被退回的无人值守工作项的原会话重做一轮 |
| `land` | （会话里）把当前克隆的分支送入合并队列 |
| `steward` | 合并队列的处理者：`--once` 处理一轮，`--loop` 常驻 |
| `approve` | 人类审批：绑定工作项和入队时的 SHA（会话内执行会被拒） |
| `clone` | 工作项的独立克隆 |
| `queue` | 查看合并队列 |
| `secret` | 第三方 API token 的存取（钥匙串 `agentops-<name>`） |
| `token` | 一年期 token 的存取 |
| `init` | 初始化 `~/.agentops`（可重复运行） |
| `doctor` | 检查工具、认证、状态目录 |

## 接入一个业务仓库

1. 初始化仓库：`ao repo init ~/work/<repo> --name <repo>`。缺 git 就 `git init`，复制 `AGENTS.md`、`.pipeline.toml`、`.claude/settings.json`、`justfile` 等模板并装 git 钩子；已有文件跳过，`--force` 覆盖。要让会话跑在 Codex 上，加 `--runtime codex`，会另外生成 `.codex/hooks.json`。
2. 登记路径：在 `~/.agentops/config.toml` 的 `[repos]` 加一行 `<repo> = "~/work/<repo>"`；Codex 仓库写成表：`[repos.<repo>]` 下 `path = "~/work/<repo>"`、`runtime = "codex"`（目前只支持无人值守会话）。常驻的 `ao board` 和 `ao steward` 要重启才认识新仓库。
3. 配置流水线：按仓库实际情况改它根目录的 `.pipeline.toml`（例如 `autonomy`、`prepare_cmd`、受保护路径），并确认 `justfile` 提供流水线要求的 `test-fast`、`test-all`、`capture`、`nightly`。

## Codex 运行时

初始化时运行 `ao repo init ~/work/<repo> --name <repo> --runtime codex`。
然后在 `~/.agentops/config.toml` 中将仓库登记为表：

```toml
[repos.<repo>]
path = "~/work/<repo>"
runtime = "codex"
```

- 目前只支持无人值守会话，启动时使用 `ao start --headless`。
- 被合并队列退回后不会自动重做，需要人工处理。
- `[budget_tokens]` 的日上限不计缓存命中，只算未缓存输入 + 输出（agentops#15）。
- 实测细节见 [codex-cli.md](codex-cli.md) 第 15 节。

## 详细文档

- [plan.md](plan.md)：计划
- [runbook.md](runbook.md)：运维手册（安装、各命令用法、排错）
- [claude-cli.md](claude-cli.md)：Claude CLI 实测记录
- [codex-cli.md](codex-cli.md)：Codex CLI 实测记录
