# AGENTS.md — agentops 的 agent 工作规则

你在 agentops 流水线里工作，而这个仓库就是流水线自己。每个会话只负责一个工作项，在自己的独立克隆目录和分支上改动。主仓库 `~/agentops` 和别的克隆你写不了。

## 自举须知（和业务仓库不同的地方）

- 正在运行的 `ao`（`~/.local/bin/ao`）、launchd 里的 steward/board、以及你会话里的 hooks，用的都是主仓库 `~/agentops` main 上的代码，**不是你克隆里的代码**。你的改动只有被 steward 合入 main 之后才生效。
- 不要运行 `just install`、`uv tool install`、`ao launchd ...`，不要动 `~/.agentops/` 里的运行状态。
- 测试一律用 `tests/conftest.py` 的 `agentops_home` 夹具，把状态指到临时目录；任何测试都不能读写真实的 `~/.agentops`。
- `hooks/`、`launchd/`、`templates/session-settings.json`、`templates/claude/`、`templates/codex/`、`templates/prompts/` 是流水线的护栏，`docs/decisions.md`、`docs/plan.md`、`docs/reviews/` 是人的记录：这些都由人维护，你改了会被拦下或转人工。
- 本仓库每次合入都要人批（`autonomy = approve`），`ao land` 之后等人就行。

## 必须遵守

1. **一项一会话**：只做本会话被派的工作项，不顺手做别的。
2. **测试只能新增，不能修改已有的**：`tests/` 下 main 上已有的文件不要改。需要改已有测试，说明规格有问题：把冲突写在你最后的回复里然后停下，不要再 `ao land`。
3. **不改配置**：`AGENTS.md`、`CLAUDE.md`、`.pipeline.toml`、`justfile`、`.claude/`、`.codex/`、`pyproject.toml`、`uv.lock` 由人维护。不要安装新包。
4. **删功能要人批**：删除 `src/` 下的文件、删除公开函数，合并时会转给人审批，请在提交说明里写清原因。
5. **只在本分支提交**，不要碰 `main`；不要 push。
6. **做完执行 `ao land`**：提交后运行 `ao land`，把分支送入合并队列。测试结果只认队列自己跑出来的。
7. **不联网**：依赖已预装（`uv sync --offline` 已在会话外执行）。

## 做事顺序

1. 读工作项全文（`ao wi show agentops#<id>`），确认验收条目。
2. 先写测试，再实现。`just test-fast` 快速自检（ruff + pytest -x），`just test-all` 完整跑一遍。
3. 提交：`git add -A && git commit -m "<type>: <说明>"`，提交信息第一行用 feat/fix/chore/docs 开头。
4. `ao land`。之后不要再改动分支；如果队列退回，会在工作项里写明原因和提示。
