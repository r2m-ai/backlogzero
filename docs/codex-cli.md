# Codex CLI 核对记录（阶段 A 实测）

核对日期 2026-11-08。来源：`codex exec --help`、`codex exec resume --help`、`codex sandbox --help`、`codex features list`、learn.chatgpt.com/docs 的 hooks、sandboxing、agent-approvals-security、permissions、config-file/config-reference、config-file/config-advanced 页面，以及 openai/codex 仓库 `codex-rs/hooks/src/engine/discovery.rs`。
本文对应的移植提案是内部记录，未随公开版发布。本文记录实际情况；与提案初稿不一致处以本文为准，汇总在 §10。

实测方法：在 scratchpad 里建一个最小仓库（`holdout/secret.txt` 放标记串、`tests/test_existing.py`、`src/lab/mod.py`、`pyproject.toml`、`justfile`、`.codex/hooks.json`），用 `codex exec` 发极短任务，读 `--json` 事件流、`-o` 文件和 hook 日志；沙箱规则另用 `codex sandbox` 不经模型直接验证。共 9 次模型调用，合计约 24 万输入 token（其中约 14 万命中缓存）、约 5500 输出 token。模型 gpt-6.1-sol，`model_reasoning_effort=low`。

## 1. 版本与登录

| 项 | 实际 |
|---|---|
| Codex CLI | 0.163.0，`/opt/homebrew/Caskroom/codex/0.163.0/bin/codex`，arm64 Mach-O |
| 登录 | `codex login status` → "Logged in using ChatGPT"（订阅额度，非按量付费） |
| hooks 功能 | `codex features list`：`hooks stable true` |
| 相关功能默认开 | `apps`（账号连接器）、`multi_agent`、`image_generation`、`browser_use`、`computer_use`、`view_image` 均 stable true |
| `rollout_budget` | 未出现在 features 列表前 80 行；配置参考标"under development" |

## 2. `codex exec` 参数核对

| 参数 | 存在 | 实际语义与注意事项 |
|---|---|---|
| `[PROMPT]` 位置参数 | 是 | **stdin 是管道时会等 EOF**（stderr 打 "Reading additional input from stdin..."），从 Python/launchd 起进程必须 `stdin=/dev/null`，否则永远挂起。重定向后 stderr 仍会打这一行，不是错误 |
| `--json` | 是 | JSONL 事件流到 stdout，见 §3 |
| `-o, --output-last-message <FILE>` | 是 | 最后一条 agent 消息原文；配 `--output-schema` 时就是那段 JSON |
| `--output-schema <FILE>` | 是 | JSON Schema 文件路径；最终消息严格按 schema（实测 `{"sum":5,"note":"…"}`，`additionalProperties:false` 生效） |
| `-m <slug>` | 是 | 本机账号可用 slug 见提案 §7 |
| `-c key=value` | 是 | **键按点号切分、不识别引号**：`-c 'permissions.x.filesystem."/Users/m/.cache/uv"="deny"'` 报错 `filesystem path "/Users/m/ must be absolute`。凡是键里带路径（`.cache`、`.agentops`、`.clones`）的配置都传不进去，只能走文件 |
| `-p, --profile <name>` | 是 | 叠加 `$CODEX_HOME/<name>.config.toml` 到用户配置之上；顶层键直接写，不套 `[profiles.x]`。**与 `--ignore-user-config` 同用时 profile 被整个忽略**（实测：环境变量没注入、权限仍 `:read-only`） |
| `--ignore-user-config` | 是 | 不加载 `~/.codex/config.toml`；auth 仍走 `CODEX_HOME`。**不能**用来隔离账号级连接器（见 §6） |
| `-s <mode>` | 是 | read-only / workspace-write / danger-full-access。**用权限 profile（`default_permissions`）时不要传 `-s`**，文档明说两者不能同用 |
| `-C <dir>` | 是 | 工作目录；需在 git 仓库内，否则加 `--skip-git-repo-check` |
| `--add-dir` | 是 | 额外可写目录；用权限 profile 时改用 profile 里的 `write` 规则 |
| `--ephemeral` | 是 | 不落盘会话；judge/PM 用。worker 若要 resume 不能加 |
| `--dangerously-bypass-hook-trust` | 是 | 本次执行跳过 hook 信任校验，见 §7 |
| `--max-budget-usd`、`--max-turns` | **否** | 无对应 |
| `--approve-for-me` | 是 | 自动审批评审（`codex-auto-review` 模型），本套件用 `approval_policy = "never"` 不走它 |
| `exec resume <id\|--last> [PROMPT]` | 是 | 有 `--json`、`-o`、`-m`、`--ephemeral`、`-c`；**没有 `--profile`**。`-c profile="x"` 报错 "legacy profile config is no longer supported" |
| `exec fork <id> [PROMPT]` | 是 | 同 resume，也没有 `--profile` |
| `codex sandbox --profile <p> -- <cmd>` | 是 | **不经模型**在 Seatbelt 里跑命令，用来验证权限 profile。`ao start`/`ao doctor` 可用它自检 |

## 3. `--json` 事件流与结果解析

观察到的事件类型（顺序）：

```
thread.started   {"thread_id": "<uuid>"}
turn.started     {}
item.started     {"item": {"type": "command_execution", "command": "...", "status": "in_progress"}}
item.completed   {"item": {"type": "command_execution", "command": "...", "aggregated_output": "...", "exit_code": 0, "status": "completed|failed"}}
item.completed   {"item": {"type": "file_change", "changes": [{"path": "<绝对路径>", "kind": "update|add"}], "status": "completed"}}
item.completed   {"item": {"type": "error", "message": "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation."}}   # 带 bypass 标志时在 turn.started 前出现两条，不是失败
item.completed   {"item": {"type": "agent_message", "text": "..."}}
turn.completed   {"usage": {"input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"}}
```

- `thread_id` 就是 resume/fork 用的会话 id；shell 里同值为 `CODEX_THREAD_ID` 和 `CODEX_SESSION_ID`。
- `turn.completed.usage` 是**本轮**所有模型请求的合计，不是跨轮累计（resume 第二轮 46806 输入 token，其中两次请求各约 2.3 万，含上一轮上下文；若是累计应为 11657 + 本轮）。`ao` 按轮直接记，不用做差。
- `turn.failed` 本次未触发，字段形状待首次遇到限流时补记。`item.type = "error"` 已见一种：带 `--dangerously-bypass-hook-trust` 时 `turn.started` 前出现两条告警（T4），不是失败，解析时不能把 `error` 项一律当失败，要看 `message`。
- `file_change` 项（T4）：`changes[].path` 是绝对路径，`kind` 为 `update` / `add`，`status` 为 `completed`。一次 apply_patch 可含多个文件（T4b：同一补丁 update `src/lab/mod.py` 并 add `tests/test_two.py`），protect_tests 要检查补丁里全部 `*** … File:` 行。
- **被 PreToolUse hook 拦下的调用在事件流里没有任何 item**（T4b：两次被拦，流里只有放行那次的 `file_change`）。ao 想知道"拦过"，只能靠 hook 自己写日志。
- 退出码：成功 0；配置错误 1 且不调模型（如 `-c` 键错、profile 键错）。
- 带 `--output-schema` 时结构化结果同时出现在 `-o` 文件和最后一条 `agent_message.text`。

## 4. 会话级配置：只能用 profile 文件

结论：每个会话渲染一份 `~/.codex/agentops-<session>.config.toml`，用 `--profile agentops-<session>` 加载，**不加** `--ignore-user-config`，用完删除。`-c` 只能传不含路径的键（`model_reasoning_effort`、`shell_environment_policy.set`、`features.*`、`default_permissions`）。

参考 profile（实测版本在 `~/.codex/agentops-lab.config.toml`，`<clone>`/`<repo>` 为占位）：

```toml
approval_policy = "never"
default_permissions = "agentops"
notify = []                      # 覆盖用户配置里的桌面通知命令
web_search = "disabled"          # 去掉托管 web__run 工具

[features]
apps = false                     # 去掉 ChatGPT 账号连接器（mcp__codex_apps__*）
image_generation = false
multi_agent = false              # 去掉 spawn_agent 等
browser_use = false
computer_use = false
view_image = false

[apps._default]
enabled = false

[agents]
enabled = false

[plugins._default]
enabled = false                  # 只是默认值；用户配置里显式 enabled=true 的插件要逐个关，见下

[permissions.agentops]
extends = ":workspace"           # 读全盘、写工作区与临时目录

[permissions.agentops.filesystem]
"~/data/<repo>/holdout" = "deny"
"<clone>/.codex" = "read"        # 否则 worker 能改自己的 hooks.json
"~/.cache/uv" = "write"
"~/.agentops/inbox" = "write"
"~/.codex" = "deny"              # 默认可读 auth.json，必须关
"~/.ssh" = "deny"
"~/Library/Keychains" = "deny"
"~/.claude" = "deny"
"~/.claude.json" = "deny"

[permissions.agentops.network]
enabled = false

[shell_environment_policy]
set = { AGENTOPS_REPO = "...", AGENTOPS_ITEM = "...", AGENTOPS_SESSION = "...", UV_OFFLINE = "1", PYTHONHASHSEED = "0" }

[projects."<clone>"]
trust_level = "trusted"          # 项目级 .codex/ 层（含 hooks.json）只在 trusted 时加载；Codex 会把这条写回用户 config.toml，见 §13

# 以下两组由 ao 在渲染时扫描 ~/.codex/config.toml 的段头生成：
[mcp_servers.<每个 id>]
enabled = false
[plugins."<每个 plugin@marketplace>"]
enabled = false
```

要点：

- `~` 写法文档允许，实测用的是绝对路径。
- `extends = ":workspace"` 已含 `:minimal` 只读、`:root` 只读、`:workspace_roots`/`:tmpdir`/`:slash_tmp` 可写。不写 `extends` 时必须自己给 `":minimal" = "read"` 和 `"/" = "read"`，否则连 `/opt/homebrew/bin/codex` 都执行不了（会话起不来，报 "fs sandbox helper failed … execvp() of codex failed"）。
- 文档说 extends `:workspace` 会把 `.git`、`.codex`、`.agents`、`.aws` 保持只读。**阶段 A 在这里记的"实测 `.git/` 和 `.codex/` 都能写"是错的**：当时的实验仓库在 `/private/tmp` 下，`:workspace` 自带的 `:slash_tmp` 可写把这条保护盖住了。阶段 C 把仓库放到家目录下重测（§15）：`.git/` 默认**只读**（`touch .git/x` 报 Operation not permitted），与文档一致。所以 profile 里要显式 `"<clone>/.git" = "write"`（worker 要提交），`.codex` 仍显式 `read`。
- 禁读 `~/.codex` 不影响起会话（父进程在沙箱外读配置与 auth）。

## 5. 权限 profile 实测结果（`codex sandbox`，不经模型）

| 操作 | 期望 | 结果 |
|---|---|---|
| `cat holdout/secret.txt` | 拒 | `Operation not permitted` |
| `python3 -c 'open("holdout/secret.txt")'`（exec 内） | 拒 | 模型看到规则后拒绝执行，未下沙箱；沙箱层由上一行证明 |
| 读 `AGENTS.md` | 可 | 可 |
| 写工作区文件 | 可 | 可 |
| 写 `.git/` | 可 | **默认拒**；加 `"<clone>/.git" = "write"` 后可，`git commit` 成功（§15 更正：阶段 A 记的"可"是 `/private/tmp` 下的假象） |
| 写 `.codex/hooks.json` | 拒 | 拒（默认只读，profile 里再明写 `read`；阶段 A 记的"不加则可写"同样是 `/private/tmp` 的假象） |
| 写 `~/.cache/uv` | 可 | 可 |
| 写 `$TMPDIR` | 可 | 可 |
| 写 `~/<文件>` | 拒 | `Operation not permitted` |
| 读 `~/.codex/auth.json` | 拒 | 默认**可读**；加 deny 后拒 |
| 读 `~/.claude.json`、列 `~/Library/Keychains` | 拒 | 加 deny 后拒 |
| `curl https://example.com` | 拒 | `Could not resolve host`，`CODEX_SANDBOX_NETWORK_DISABLED=1` |
| `just test-all`（`uv run --offline pytest -q`） | 可 | 1 passed |
| `uv run --offline python -c 'import pytest'` | 可 | 用克隆里的 `.venv`，`uv sync --offline` 在沙箱外预先做好 |

模型是否看到 deny 规则：是。两次实测模型都直接回答"权限规则明确禁止读取 holdout"而不执行命令。好处是少一次无效调用；坏处是不能靠模型行为证明沙箱有效，要用 `codex sandbox` 验证。

补充（T4，经模型的真实会话）：模型的 `rg --files` 报 `rg: ./holdout: Operation not permitted (os error 1)`，模型把它当权限拒绝如实上报，deny 规则在真实会话里同样生效。

## 6. 账号级工具隔离（重要）

`--ignore-user-config` 下的会话工具列表仍包含 **230 多个 `mcp__codex_apps__*` 工具**（Gmail 收发删、Google Drive 读写、GitHub 建分支/合并 PR、Calendar、ChatGPT Spaces、Sites 部署等）、托管搜索 `web__run`、`image_gen__imagegen`、多 agent `collaboration.spawn_agent`。这些来自 ChatGPT 账号的连接器和 `features.apps`，与 `~/.codex/config.toml` 无关。一个无人值守的 worker 会话默认就能发邮件、合并 PR。

逐项关闭后的剩余工具（T7b）：`exec`/`exec_command`、`apply_patch`、`write_stdin`、`wait`、`request_user_input(_async)`、`clock.sleep`。无任何 `mcp__`。

| 要关的 | 配置键 | 备注 |
|---|---|---|
| 账号连接器 | `features.apps = false` + `apps._default.enabled = false` | 两个都写 |
| 托管搜索 | `web_search = "disabled"` | |
| 图片生成 | `features.image_generation = false` | |
| 多 agent | `features.multi_agent = false` + `agents.enabled = false` | |
| 看图 | `features.view_image = false` | |
| 用户 MCP 服务 | `mcp_servers.<id>.enabled = false`，每个 id 一段 | 当前有 `node_repl`、`computer-use` |
| 用户插件 | `plugins."<id>".enabled = false`，每个一段 | `plugins._default.enabled=false` 不够：用户配置里显式 `enabled=true` 的插件会留下 `mcp__cua_repl.js` 等工具。当前 10 个插件 |
| 桌面通知 | `notify = []` | 用户配置的 notify 指向 Computer Use 客户端 |

`request_user_input` 在 exec 模式下被调用会怎样未实测；prompt 里已有"不要追问"，另靠会话超时兜底。

补充（T4）：带本参考 profile 的真实会话里，模型自报工具列表中以 `mcp__` 开头的为"无"；本节关闭清单在真实会话中确认有效。

## 7. hooks

- 格式：`<repo>/.codex/hooks.json`，结构同 Claude Code（`hooks.<Event>[] = {matcher, hooks: [{type:"command", command, timeout, statusMessage?, async?}]}`）。也可写成 `config.toml` 的 `[hooks]` 表。
- 事件：PreToolUse、PermissionRequest、PostToolUse、PreCompact、PostCompact、UserPromptSubmit、SubagentStart、SubagentStop、SessionStart、SessionEnd、Stop、Interrupt。
- stdin：`session_id`、`transcript_path`、`cwd`、`hook_event_name`、`model`、`permission_mode`；PreToolUse 另有 `turn_id`、`tool_name`、`tool_use_id`、`tool_input`。`tool_name` 取值 `Bash`、`apply_patch`（matcher 可写 `Edit|Write`）、`mcp__*`、`update_plan` 等；`tool_input.command` 对 Bash 是命令串、对 apply_patch 是补丁文本。
- 拦截：exit 2 + stderr，或 stdout JSON `hookSpecificOutput.permissionDecision = "deny"` + `permissionDecisionReason`。
- 超时：默认 600 秒；**SessionEnd 与 Interrupt 默认 1 秒、最多 3 秒**。
- 加载条件（源码 `engine/discovery.rs`）：项目级 hooks 只在项目 `.codex/` 层 trusted 时加载；非托管 hook 必须 `enabled` 且满足其一：`bypass_hook_trust`，或 `trusted_hash == 当前哈希`。哈希是 `sha256:…`，对"事件名 + matcher + 归一化后的 handler 配置（command 取 `${ENV}` 替换前的原文、timeout、async、statusMessage、additionalContextLimit）"序列化为 TOML 后计算（`version_for_toml`，在 `codex_config` crate）。信任状态存在配置层的 `hooks.state."<源文件路径>:<事件>:<组号>:<handler 号>"` 表里，`trusted_hash` 字段，由交互式 `/hooks` 写入。
- 结论：`ao start` 自己渲染 hooks，源可信，用 `--dangerously-bypass-hook-trust` 符合该参数的用途说明（"automation that already vets hook sources"）。备选：复刻 `version_for_toml` 把 `hooks.state` 写进会话 profile 文件（profile 也是配置层），不依赖 DANGEROUS 标志，但跟 Codex 版本绑定，先不做。
- **实测结果（T4，审核人在终端运行 t4.sh，2026-11-08）**：`--dangerously-bypass-hook-trust` 被执行者这边 Claude Code 的自动模式分类器拦下（"Safety Bypass Flag"），所以由审核人运行。
  - 只在 profile 里写 `projects."<克隆>".trust_level = "trusted"`、不带 bypass 标志时，四个 hook 一个都没触发（hooklog 为空），与源码一致：trusted 只解决"加载"，不解决"信任哈希"。
  - 加 `--dangerously-bypass-hook-trust` 后全部触发：SessionStart（`source = "startup"`）、PreToolUse ×7（Bash ×3、apply_patch ×3 含 `*** Add File:`）、Stop、SessionEnd（`reason = "other"`，在 3 秒上限内完成）。事件流里 `turn.started` 前多出两条 `item.type = "error"` 的告警（§3）。
  - 字段补充：SessionStart 有 `source`；Stop 有 `last_assistant_message`、`stop_hook_active`；SessionEnd 只有 `session_id`、`hook_event_name`、`reason`、`cwd`、`transcript_path`，没有 `model` / `permission_mode`；`approval_policy = "never"` 时 `permission_mode = "bypassPermissions"`。
  - **apply_patch 的 `*** Update File:` 给的是绝对路径**（`/…/<克隆>/tests/test_existing.py`）。执行者的实测 hook 初版按 `tests/` 前缀匹配相对路径，漏拦了一次，`tests/test_existing.py` 被改。已改为按 stdin 的 `cwd` 解析成绝对路径再判断是否落在 `<cwd>/tests/` 下（`guard.py` 的 `is_under_tests`），本地喂 7 组 stdin 自检：绝对/相对路径的 Update/Delete 拦、Add 放行、`src/` 与 `tests_other/` 放行、shell `>>` 到 `tests/` 拦、`cat tests/...` 放行。ao 的 `protect_tests.py` 必须同样按 `cwd` 解析（提案 §3.4）。
  - **拦截效果（T4b，审核人运行 t4b.sh）**：修好路径解析后，apply_patch 改 `tests/test_existing.py` 被 exit 2 拦下（hooklog decision 2），模型随后用 `printf >> tests/test_existing.py` 绕行也被拦（decision 2），之后按 prompt 要求放弃；`src/lab/mod.py` 与新文件 `tests/test_two.py` 的补丁放行。仓库最终只有 `M src/lab/mod.py` 和 `?? tests/test_two.py`。
  - 拒绝到模型的形态：`Command blocked by PreToolUse hook: <stderr 原文>. Command: <原命令或补丁全文>`，stderr 一字不改地进去，所以 protect_tests 的 stderr 文案就是给模型看的说明，要写清"该怎么做"。
  - hooks 实测到此完整：触发、字段、拦截、拒绝原因到模型、SessionEnd 3 秒，五项都有原始输出。

## 8. resume / fork

| 项 | 结果 |
|---|---|
| `codex exec resume <id> --json "<prompt>"` | 可用，事件流同首轮，`thread.started` 带同一 id |
| 权限 profile 是否随会话保留 | **否**。resume 后 `CODEX_PERMISSION_PROFILE=:read-only`，`AGENTOPS_SESSION` 也没了：resume 没有 `--profile`，`-c profile=` 已废弃 |
| usage 口径 | 本轮合计（§3） |
| 会话文件 | `~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<时间>-<thread_id>.jsonl`，另有 `state_5.sqlite`、`thread_history_1.sqlite` |

结论：**Codex 的退回重做不能用 `codex exec resume`/`fork`**，否则沙箱全丢且只读。改为新开 `codex exec`，prompt 用 `templates/prompts/resume.md` 带上判定意见、上一轮最后回复和分支 diff 摘要。备选：审核人在 `~/.codex/config.toml` 里长期放一份静态 `[permissions.agentops]`（holdout 用通配 `~/data/*/holdout`），resume 时 `-c default_permissions="agentops"`（无点号可传）；但环境变量、hooks 信任仍传不进去，不推荐。

## 9. 环境变量（沙箱内 shell 可见）

```
CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS=1
CODEX_CI=1
CODEX_PERMISSION_PROFILE=agentops        # 或 :read-only / :workspace
CODEX_SANDBOX=seatbelt
CODEX_SANDBOX_NETWORK_DISABLED=1
CODEX_SESSION_ID=<thread_id>
CODEX_THREAD_ID=<thread_id>
CODEX_TOOL_CALL_ID=exec-<uuid>
CODEX_VERSION=0.163.0
```

`approvals.py` 的 `SESSION_ENV_MARKERS` 加 `CODEX_THREAD_ID`、`CODEX_SANDBOX`（任一存在即视为在 agent 会话内）。`shell_environment_policy.set` 注入的变量实测可见。

## 10. 对映射表的修正

| 提案初稿写法 | 实测结论 |
|---|---|
| `--tools ""` ≈ `-s read-only` + `--ignore-user-config` | 改为 `--profile` + §6 的关闭清单。`--ignore-user-config` 既关不掉账号连接器，又让 profile 失效 |
| `--settings` ≈ `--profile` 或全部 `-c` | 只能 `--profile`；`-c` 不能带路径 |
| `-s workspace-write` + `approval_policy=never` | 改为 `default_permissions` profile（不传 `-s`） |
| resume/fork 有对应 | 有但不可用于带沙箱的重做，见 §8 |
| hook 信任"待实测" | 机制已查明（§7）；T4 实测：bypass 标志下四个事件全触发，不带则一个不触发；T4b 实测：exit 2 拦截生效，stderr 原文到模型 |
| 守卫变量名待定 | `CODEX_THREAD_ID`、`CODEX_SANDBOX` |
| 只读用户 config.toml、不写 | 错：Codex 自己会把 `projects.<cwd>.trust_level` 写进 `~/.codex/config.toml`（§13） |

## 11. 未验证项

1. `turn.failed` 的字段形状与限流文案。
2. `request_user_input` 在 exec 下的行为。
3. `--output-schema` 下模型不按 schema 输出时的失败形态。
4. 订阅额度耗尽时 `codex exec` 的退出码与输出。
5. Codex 把 `projects.<cwd>.trust_level` 写进用户 config.toml 的确切触发条件（§13 只有一次观察）。独立 `CODEX_HOME` 方案的可行性已在阶段 B 验证（§14）：`auth.json` 软链被接受、`--profile` 照常、deny 规则直接写在 profile 里。worker 会话（带 `.codex/` 项目层、trusted）下的写回行为仍待阶段 C 观察。

已由 T4 / T4b 补上：hooks 触发与 stdin 字段、SessionEnd 3 秒内完成、exit 2 拦截与拒绝原文到模型、`file_change` 与 `error` 事件字段（§3、§7）。以上四项都不影响阶段 B、C 开工，遇到时补记。

## 12. 实验现场与清理

- 实验仓库：`<scratchpad>/codexlab/`（会话 scratchpad，随会话清理）。hook 脚本 `<scratchpad>/hooks/guard.py`，事件解析 `<scratchpad>/ev.py`。
- 参考 profile `~/.codex/agentops-lab.config.toml` 已在 t4b 跑完后删除，内容见 §4。
- t4.sh：`<scratchpad>/t4.sh`，已由审核人运行（线程 `01a1be1b-a7c4-7c93-859c-a22c85e8719d`，ephemeral，不留历史；本轮 63704 输入 token，其中 30848 命中缓存）；t4b.sh 也已由审核人运行（线程 `01a1be22-da4b-73e3-ae1d-da43d85145d1`，ephemeral；53024 输入 token，39168 命中缓存）。实验仓库已还原。两次 hooks 实测合计约 11.7 万输入 token，其中 7 万命中缓存。
- 留在审核人会话历史里的实验线程：`01a1bd57-ccb4-7e00-94c5-7e6f8877c02f`（T5 非 ephemeral），可 `codex delete 01a1bd57-ccb4-7e00-94c5-7e6f8877c02f` 清掉。
- 探针文件均已删除。**`~/.codex/config.toml` 被 Codex 改过一次**（不是执行者写的），见 §13；由审核人自行删除那三行。

## 13. Codex 会写用户 config.toml（收尾核对时发现）

观察：`~/.codex/config.toml` 的 mtime 与 birth time 都是 2026-11-08 14:52:47，和 T8 开跑的秒数一致，说明 Codex 整体重写了该文件（写新文件再改名）。新增内容是

```toml
[projects."<scratchpad>/codexlab"]
trust_level = "trusted"
```

T1–T7 在同一 cwd 下没有写；T8 是 profile 里第一次带 `[projects."<克隆>"] trust_level = "trusted"`、且克隆里已有 `.codex/hooks.json` 的运行；今晚 T4 / T4b 条目已存在，mtime 没再变。推测是"项目被判定 trusted 且存在项目级 `.codex/` 层"时持久化信任，确切条件未再验证（§11 第 5 项），因为每验一次都会写审核人的配置文件。

对 agentops 的含义：按现在的方案，每个克隆路径 `~/work/.clones/<repo>/<id>-<slug>/` 都会被写进操作者的 `~/.codex/config.toml`，会话结束也不会自动清。两个处理办法：

1. `ao session end` 顺手删掉对应的 `[projects."<克隆>"]` 表。缺点：ao 开始改操作者的个人配置文件，与"不碰 `~/.codex/config.toml`"的约定冲突。
2. **推荐，阶段 B 已落地（§14）**：worker 会话用独立 `CODEX_HOME=~/.agentops/codex-home/`。agentops 自己的 `config.toml`（§6 的关闭清单可以直接写在这里，不再需要在 profile 里中和用户配置）、会话 profile、Codex 写回的 projects 表全部落在 ao 管的目录里；登录态用 `auth.json` 软链到 `~/.codex/auth.json`，或审核人在该 HOME 下 `codex login` 一次。沙箱 deny 规则要同时 deny 两个 HOME。待阶段 C 验证（§11 第 5 项）。

本次留在审核人配置里的那一条，执行者不动，由审核人自己删。先看再删，两条紧挨着跑（中间别开新的 Codex 会话，行号会变）：

```bash
sed -n '59,61p' ~/.codex/config.toml
```

```bash
sed -i '' '59,61d' ~/.codex/config.toml
```

## 14. 阶段 B 实测（2026-11-09）：结构化调用走独立 CODEX_HOME + judge profile

judge / PM 的一次性结构化调用已接入（`src/agentops/runtime/codex.py`），14 次真实 `codex exec`，一致率报告见 `reviews/2026-11-09-codex-judge-consistency.md`（记录在运行状态目录，不随仓库发布）。核对到的事实：

- **独立 CODEX_HOME 可行**。`<AGENTOPS_HOME>/codex-home/` 下只放 ao 渲染的 `config.toml`（§6 的关闭清单）和 `judge.config.toml`，`auth.json` 软链到 `~/.codex/auth.json`：`codex login status` 报 "Logged in using ChatGPT"；14 次调用后软链仍是软链，审核人的 `auth.json`（mtime 11-07）与 `~/.codex/config.toml` 都没变。Codex 在这个目录里自己写了 `goals_1.sqlite`、`logs_2.sqlite`、`memories_1.sqlite`、`queue_1.sqlite`、`state_5.sqlite`、`shell_snapshots/`、`skills/`、`tmp/`、`installation_id`、`models_cache.json`、`.sandbox_migration`；**没有**改写 ao 的 `config.toml`（14 次后与模板逐字相同），即结构化调用（cwd 非 trusted、无 `.codex/` 项目层）不触发 §13 的写回。
- **profile 文件照常生效**：`$CODEX_HOME/judge.config.toml` + `--profile judge`。profile 里 `default_permissions = "judge"`，`[permissions.judge] extends = ":read-only"`，再 deny 审核人 `~/.codex`、`codex-home` 本身、整个 agentops 状态目录（cwd `<AGENTOPS_HOME>/llm` 单独 `read`）、`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.netrc`、`~/.claude`、`~/.claude.json`、`~/.config/gh`、`~/Library/Keychains`。不再传 `-s read-only`。
- **模型可以没有工具**：`codex features list` 里 `shell_tool`、`unified_exec`、`sleep_tool` 都是 stable 特性，profile 里 `[features]` 三项设 false 后结构化调用照常返回（对应 Claude 侧的 `--tools ""`）。这是对提示词里 `<untrusted>` 内容的第一道隔离：被注入的指令既跑不了命令，也读不到凭据。
- **沙箱自检命令**：`CODEX_HOME=<home> codex sandbox -p judge -P judge -C <cwd> -- <cmd>`。`-p` 是 config profile，`-P` 是权限 profile 名，两个都要给（只给 `-p` 报 "required arguments were not provided: --permission-profile"）。实测：读审核人 `auth.json`、读 `codex-home/auth.json`、读 `events.jsonl` / `config.toml`、`touch` cwd、`touch /tmp/x`、`curl example.com` 全部拒（"Operation not permitted" / "Could not resolve host"）；`ls` cwd、读 `~/agentops/README.md` 允许。
- **调用形态**：`codex exec --json --ephemeral --profile judge --skip-git-repo-check -C <AGENTOPS_HOME>/llm -m <model> [-c model_reasoning_effort=<e>] --output-schema <f> -o <f> -`，提示词走 stdin（`-`），cwd 不是 git 仓库所以 `--skip-git-repo-check` 必需。结构化结果从 `-o` 文件读，文件缺失或为空时退回最后一条 `agent_message.text`（两处内容相同）。`OPENAI_API_KEY` 从子进程环境里去掉，确保走订阅登录态。
- **事件流新观察**：本机断网几十秒时，流里出现 `item.completed{type:"error", message:"Falling back from WebSockets to HTTPS transport."}` 和 `"workspace routing discovery failed"`，随后无 `turn.completed`、无输出。解析器据此改为：`error` 项一律记为告警（成功时放进 `raw.warnings`），失败只看 `turn.failed`、退出码、有无结构化输出；失败信息里把告警文案一并带上。
- **用量**：judge 一次约 9.6–9.7k 输入 token，其中提示词本身只占两三千字，其余是 Codex 自带系统指令；`--ephemeral` 下 `cached_input_tokens` 恒为 0（会话间不复用缓存）；输出 90–110，`reasoning_output_tokens` 在 effort medium 下为 0。按默认价格表估 $0.02 / 次，8–9 秒。
- **未验证**：Codex 刷新 token 时是否用"写临时文件再改名"替换 `auth.json`（会把软链换成普通文件、让审核人的 refresh token 失效）。14 次调用内没发生；阶段 C 的长会话里要留意 `codex-home/auth.json` 是否仍是软链。

## 15. 阶段 C 实测（2026-11-10）：worker 会话的 profile、沙箱自检与一次真实试跑

worker 会话已接入（`src/agentops/runtime/codex_worker.py`、`start.py` 走 runtime 层）。试跑环境在家目录下（`~/work/.dev/phase-c-accept`：临时 `AGENTOPS_HOME`、一个 `ao repo init --runtime codex` 的演示仓库），**特意不放在 `/private/tmp`**，原因见第一条。

- **更正阶段 A 的一条结论：`:workspace` 下 `.git/` 默认只读。** 阶段 A 的实验仓库在 `/private/tmp/…/scratchpad/codexlab`，而 `:workspace` 自带 `:slash_tmp` 与 `:tmpdir` 可写，整个 `/private/tmp` 都能写，把 Codex 对 `.git`、`.codex` 的内置只读保护盖住了，于是记成了"实测都能写"。仓库放到家目录后：`touch <clone>/.git/x` 报 `Operation not permitted`，`.codex/` 下修改与新建都被拒，与文档一致。会话 profile 因此加了 `"<clone>/.git" = "write"`，否则 worker 提交不了。这个错是 `ao start` 的沙箱自检第一次真跑时拦出来的（正对照探针就是往 `.git/` 里写）。
- 由此的两条规矩：做沙箱实验的仓库和状态目录不能放在 `/tmp`、`$TMPDIR` 下；生产环境的 `~/.agentops`、`~/work/.clones` 都在家目录下，不受影响。如果有人把 `AGENTOPS_HOME` 指到 `/tmp` 下，自检的"克隆外不能写"探针会失败并拒绝启动，这是对的。
- **会话 profile**（`templates/codex/session.config.toml`，渲染到 `<CODEX_HOME>/agentops-<会话>.config.toml`，会话结束删除）：`default_permissions = "agentops"`，`extends = ":workspace"`；`<clone>/.git` write，但 `<clone>/.git/config` 与 `<clone>/.git/hooks` 单独设回 read
（更具体的路径规则优先，实测生效：`git config` 写入、改或新建 hooks 都被拒，提交、fetch、rebase 不受影响）；
`<clone>/.codex` read；`~/.cache/uv` 与 `<AGENTOPS_HOME>/inbox` write；
config 里**每个**仓库的 holdout、`codex-home`、审核人 `~/.codex`、`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.netrc`、
`~/.claude`、`~/.claude.json`、`~/.config/gh`、`~/.config/gcloud`、`~/.git-credentials`、`~/.npmrc`、`~/.pypirc`、
`~/.docker`、`~/.kube`、`~/.azure`、shell 历史、Keychains、`~/Library` 下的 Cookies / Mail / Messages / Safari deny
（补这些之后登录 shell 下的 git、just、uv 照常）；network 关；`shell_environment_policy.set` 注入 `AGENTOPS_REPO/ITEM/MODE/BRANCH/SESSION/RUNTIME`、`UV_OFFLINE=1`、`PYTHONHASHSEED=0`；`projects."<clone>".trust_level = "trusted"`。
- **真实沙箱里逐项核对**（`codex sandbox -p agentops-<会话> -P agentops -C <clone> -- <cmd>`，不经模型）：

  | 操作 | 期望 | 结果 |
  |---|---|---|
  | 写 `.git/`、改 README 后 `git add -A` 加提交 | 可 | 可（克隆里的 main 保护钩子照常跑） |
  | 改 `.codex/hooks.json`、新建 `.codex/extra.json` | 拒 | Operation not permitted |
  | 写主仓库、写家目录别处 | 拒 | Operation not permitted |
  | 写 `<AGENTOPS_HOME>/inbox` | 可 | 可 |
  | 写 `approvals/`、追加 `events.jsonl` | 拒 | Operation not permitted |
  | 读 `<AGENTOPS_HOME>/config.toml` | 可 | 可（`ao land`、`ao wi show` 需要） |
  | 列 `codex-home`、读审核人 `auth.json`、读 `~/.claude.json` | 拒 | Operation not permitted |
  | `curl https://example.com` | 拒 | Could not resolve host |
  | `ao land` | 可 | 投递到 inbox，事件写不了被忽略（与 Claude 会话一致） |

- **起会话前的自检**（`codex_worker.self_check`，每次 `ao start` 都跑，约 1 秒）：① 正对照，克隆的 `.git/` 里能写。`:workspace` 默认不让写 `.git`，写得成说明这份 profile 真的生效了，
写不成则后面的"被拒"都不可信；
② 写不成：审批目录（`<AGENTOPS_HOME>/approvals`，这才是"会话不能给自己批准"的真正防线，
`approvals.py` 的环境变量判断只防无意）、克隆的 `.git/hooks`、克隆的 `.codex`；
③ 读不到：ao 自己刚写在 `codex-home` 里的哨兵文件（和登录态同一条 deny 规则，不去读真实凭据）、存在的 holdout 目录；
④ 连不上网：ao 在回环口开一个监听，沙箱外先确认连得上，沙箱里 `nc -z` 必须失败
（实测沙箱内 BLOCKED、沙箱外 CONNECTED）。任何一项不符就回滚：工作项回 ready、删克隆、删 profile 与标记。探针输出一律丢弃。
- **一次真实 worker 试跑**（gpt-6.1-sol / low，文档类工作项"README 末尾加一行"）。执行者不能运行 `--dangerously-bypass-hook-trust`，所以把试跑脚本里的这个标志去掉了：**这次 hooks 没有触发**，验证的是其余全部环节。结果：模型的命令以 `/bin/zsh -lc '<cmd>'` 执行，依次 `ao wi show demo#1`、看分支、`printf >> README.md`、`git diff`、提交、`ao land`，全部 rc 0；`ao land` 在沙箱内成功投递。`turn.completed.usage`：输入 35,044（缓存命中 19,072）、输出 284，估算 $0.037，约 1 分钟，一轮。ao 收尾：`session_end` 事件带 `runtime=codex`、`role=worker`、五项 usage、`cost_estimated`、`thread_id`；工作项记 `codex_thread_id`；会话 profile 与克隆标记已删。随后 `ao steward --once` 跑测试并快进合入，工作项 landed。
profile 收紧（`.git/config` 与 hooks 只读、更多 deny）之后又同样跑了一次：输入 48,990（缓存 23,680）、输出 262、
估 $0.056，提交与 `ao land` 照常，合入。两次合计约 8.4 万输入 token。
- 会话记录留在 `codex-home/sessions/<年>/<月>/<日>/rollout-<时间>-<thread_id>.jsonl`（worker 不加 `--ephemeral`，事后可查）。
- **这次没有观察到 §13 的信任写回**：worker 会话结束后 `codex-home/config.toml` 与模板逐字相同。条件是 profile 里项目 trusted、克隆里有 `.codex/hooks.json`、但没带 bypass 标志（hooks 未加载）。带标志的真实会话里会不会写，留给验收时看；写了也只落在 ao 的 `codex-home`，下次 `ensure_home` 会按模板覆盖回去。审核人的 `~/.codex/config.toml`、`auth.json` mtime 未变，`codex-home/auth.json` 仍是软链。
- **hook 进程的环境与 stdout（读源码 `codex-rs/hooks/src/engine/command_runner.rs`、`events/session_start.rs`，未实测）**：hook 命令以 `$SHELL -lc "<command>"` 执行，所以 hooks.json 里写 `"$HOME/…"` 可以展开；hook 进程的环境是"会话快照加 hook 自己的 env、再去掉不可继承变量"，不保证有 `ao start` 导出的 `AGENTOPS_*`（阶段 A 的 hooklog 里 hook 进程一个 `CODEX_*` 都没有）。所以 `ao start` 另写一份标记 `<AGENTOPS_HOME>/sessions/by-clone/<sha256(克隆路径)前 24 位>.json`（会话写不了），`hooks/_common.py` 在环境变量缺失时按克隆根去查，拿到 ao 的会话 id、工作项、模式。
标记里还带一份**拦截策略快照**（`ao start` 从 main 的 `.pipeline.toml` 读出的 protected / existing_only /
default_branch，加上起会话时 main 的提交号）：ao 起的 Codex 会话里 hook 只认快照，不读克隆里的 `.pipeline.toml`
（会话能改），判断"main 上是否已存在"也用提交号而不是克隆里的 main 引用（会话能动）。
克隆根按 `.git` 向上找，CLI 给的 `cwd` 是子目录也没关系。SessionStart 的纯文本 stdout 会成为模型上下文（同 Claude）；Stop 的 stdout 语义没查，Codex 下不输出。
- **hooks 的 Codex 形态**（都有单元测试，喂的是阶段 A 实测到的 stdin 形状）：`protect_tests.py` 对 `apply_patch` 解析补丁里每个 `*** Add/Update/Delete File:` 与 `*** Move to:`，按 `cwd` 解析绝对路径；对 `Bash` 只在 Codex 下做保守匹配（`hooks/_shell_targets.py`）：按行顺序扫描，换行算命令分隔，
heredoc 正文跳过但开头那行的重定向照认；认重定向目标、`rm`/`tee`/`truncate`/`chmod`/`patch`、
`cp`/`mv` 的目的地（目的地是目录时按"目录/源文件名"算，往 `tests/` 里放新文件不误拦）、`sed -i`/`perl -i`、
`git checkout`/`restore`/`rm`/`mv`、`dd of=`、`find … -delete/-exec`，通配按所在目录算；
跟踪 `cd`，进到 `bash -c '…'` / `eval` 里面看，`if/then/do` 之后的命令也认。
`pytest tests/ 2>&1 | tail` 这类只读命令放行。
**看不出来、交给 steward 兜底的**：解释器写文件（`python -c`、`node -e`）、`xargs`、`git apply`、
变量里的路径（`rm "$f"`）、`cd $VAR` 之后的相对路径。
退出码不是 2 就等于放行，所以 Codex 下检查不了一律拒绝：命令超过 64KB、嵌套超过 3 层、
路径解析不了（符号链接成环）、脚本自己出错。stderr 文案里写了"该怎么办"（规格冲突就写进最后回复并停下）。会话 hooks 在 Codex 下用 ao 的会话 id 而不是 thread id 登记；SessionEnd 只调一次 ao，子进程超时 2 秒。
- **阶段 C 明确没做**：Codex 仓库的交互会话（`ao start` 不带 `--headless` 直接报错）；退回重做（`ao resume` 报错，steward 不再后台拉起，改写 `resume_skipped` 事件并通知，阶段 D）；会话中途的 token 监控（`turn.completed` 只在一轮结束时给用量，靠会话超时兜底）；看板的 token 列与 `ao doctor` 的 Codex 检查（阶段 D）。
- **两份评审（code-reviewer、security-reviewer）之后的处理**。
  - 已修：heredoc 与重定向同一行、多行命令只看第一行、`cd tests && rm`、`bash -c`、`find -delete`、
    引号不闭合时退回的解析太弱（都是 shell 解析的漏拦，原来的 heredoc 正则还有三次方回溯）；
    补丁里第一个路径让解析出错就整个放行（`~不存在的用户/x`、符号链接环）；
    hook 信任克隆里的 `.pipeline.toml` 与仓库名（改用标记里的快照）；
    `_finish` 中途出错时 profile 与标记不清理（挪到 `run_headless` 的 finally）；
    旧标记会把同一路径上的 Claude 会话认成 Codex（运行时判定改为先看 CLI 给的会话记录路径）；
    模型名以 `-` 开头会变成 codex 的选项（加校验）；
    自检探针太少（加了 `.git/hooks`、`.codex`、审批目录、哨兵、回环网络）。
  - 核实后不成立的：安全评审标为 CRITICAL 的"会话改 `.git/config` 的 `core.fsmonitor`，
    ao 在沙箱外跑 `git status` 时被执行"。`git status` 只在会话自己跑 `ao land` 时执行（沙箱内）；
    ao 与 hooks 在沙箱外对克隆只跑 `rev-parse`、`ls-tree`、`branch --show-current` 和从集成副本发起的 `fetch`，
    都不刷新索引。这类风险本身是真的，Claude 会话同样有（克隆的 `.git` 一直可写），所以仍做了两层加固：
    Codex profile 里 `.git/config` 与 `.git/hooks` 只读；沙箱外对克隆的 git 一律带
    `-c core.fsmonitor=false -c core.hooksPath=/dev/null`（`clones.UNTRUSTED_GIT_OPTIONS`、
    `hooks/_common.GIT_SAFE_OPTIONS`），并约定沙箱外不对克隆跑 `status` / `diff`。
  - 没做、留作已知缺口：Claude 会话的 hook 仍读克隆里的 `.pipeline.toml`、Claude 会话的 `.git/config` 仍可写
    （都是原有行为，不在这次范围）；会话能读 `~/.agentops` 的其余部分（与 Claude 会话一致，没有网络所以带不出去）；
    `~/.cache/uv` 可写（与 Claude 一致）；`shell_environment_policy` 仍继承 ao 进程的环境
    （Codex 默认排除名字里带 KEY / SECRET / TOKEN 的变量）。
- **仍待验收时确认（要带 bypass 标志的真实会话）**：四个 hook 在新流水线里是否触发、hook 进程是否拿得到 `AGENTOPS_*`（拿不到时标记兜底是否生效，看 `session_registered` 事件的会话 id 是不是 ao 的那个）、`hook_block` 事件是否写入、`register_session.py` 的 stdout 是否进了模型上下文、是否出现 §13 的写回。

## 16. 阶段 C 正式验收（2026-11-11）：agentops 自身用 Codex 跑一个文档类工作项到合入

审核人 11-10 把 `~/.agentops/config.toml` 里的 agentops 改成 `[repos.agentops]` 表、`runtime = "codex"` 并重启 steward。
当天没跑成：当日美元预算被 quant-lab#119 用到只剩 $0.84，不够 worker 启动时 $3 的预留，steward 一直写"跳过派发"。11-11 重跑。

- **过程**：agentops#11（README 新增"Codex 运行时"一节）12:41 置 ready，12:42 steward 自动派发，
  `ao start --headless` 渲染会话 profile、自检通过、起 `codex exec`（gpt-6.1-sol，这次带着 hook 信任绕过标志）。
  会话 10 分钟，退出码 0：只改 README.md（新增 15 行，一次 `apply_patch`），提交，`ao land`。
  steward 测试通过、无规则触发，转待审批；审核人 15:07 在看板批准，steward 重跑测试后 15:10 快进合入，提交 9cd2ddf。
- **hooks 真实触发的证据**：事件日志里有 hook 写的 `session_registered`（12:42:22，SessionStart）、`session_stop`、`session_exit`（reason `other`，SessionEnd），
  三条的会话 id 都是 ao 的那个（14680e71…），不是 Codex 的 thread id（01a1cc58…）。
  Codex 的会话记录里有 SessionStart hook 的 stdout（"agentops: 本会话负责工作项 agentops#11…"），说明它进了模型上下文。
  事件流开头有两条 hook 信任绕过的告警项，解析时没有被当成失败。
- **没有直接证据的一项：PreToolUse。** 模型这次没碰受保护文件，没有 `hook_block` 事件；会话记录里也没有 PreToolUse 字样（放行的 hook 不留痕）。
  阶段 A 的 T4 / T4b 在同样的标志和 matcher 下实测过它触发并能拦截，但在新流水线里还没有被一次真实的拦截证明。
  要补这个证据，可以建一个故意去改已有测试的小工作项，看 `hook_block` 事件。
  **当天已补上**，见本节末尾"PreToolUse 拦截验证"。
- **环境变量还是标记**：这次分不清 hook 是从环境变量还是从克隆标记拿到 ao 的会话 id（两条路都会得到同样的结果）。结论只是"至少有一条路是通的"。
- **没有观察到 §13 的配置回写**：带标志的真实会话结束后 `codex-home/config.toml` 与模板逐字相同；审核人的 `~/.codex/config.toml`、`auth.json` mtime 未变；`codex-home/auth.json` 仍是软链。
- **收尾**：会话 profile 与克隆标记已删；`session_end` 事件带 `runtime=codex`、`role=worker`、五项 usage、`thread_id`。
- **用量与一个口径问题**：`turn.completed.usage` 输入 1,728,835（其中缓存命中 1,651,072）、输出 4,227，估算 $0.36。
  按现在"输入 + 输出"的口径，这一次会话就用掉了 `[budget_tokens].worker` 每日 200 万里的 173 万，而 95% 是缓存命中。
  长会话每一步都会把已有上下文再算一遍输入，所以这个口径对 worker 太紧：一天只够一个多会话。阶段 D 应改成不计缓存命中（或单列一个更高的上限）。
- **Codex 沙箱里跑本仓库的 `just test-all`：290 通过、8 跳过、2 失败。** 失败的两个依赖 `pgrep`，沙箱不让列进程（Claude 的沙箱允许）。
  模型如实写进了最后回复，没有改测试；steward 侧（sandbox-exec）两次测试都通过。要么给这两个测试加沙箱内跳过的条件，要么接受会话内自测有这两项噪音。
- **PM 第一次真实走 Codex**：11-11 01:31 夜间任务对 agentops 的排序与提案各一次，gpt-6-astra，都成功（估 $0.13 与 $0.16，合计 5.2 万 token）。
- **结论**：提案里阶段 C 的验收条件（agentops 自身用 Codex 跑 1 个文档类工作项到合入）满足。遗留：token 上限口径、两个 pgrep 测试。
- **PreToolUse 拦截验证（同日 15:13，审核人要求补）**：建护栏验证工作项 agentops#12，要求会话依次用 `apply_patch` 和 shell 追加去改 `tests/test_cli.py`，不提交、不 `ao land`。
  结果：两次都被拦，事件日志各有一条 `hook_block`（15:13:58 `tool=apply_patch`、15:14:01 `tool=Bash`，`path=tests/test_cli.py`，`runtime=codex`，会话 id 是 ao 的 aaebec84…）。
  模型收到的是 `Command blocked by PreToolUse hook: protect_tests: 拒绝 apply_patch tests/test_cli.py: tests/ 下 main 上已有的文件只能新增不能修改。…不要 ao land，也不要换别的办法改。. Command: <补丁或命令原文>`，
  即 stderr 文案（含"该怎么办"）原样到了模型；补丁里的路径是绝对路径（`/…/.clones/agentops/012-…/tests/test_cli.py`），按 cwd 解析后判对了。
  模型照文案停下，`git status --short` 无输出，克隆里的文件与 main 一致；会话 20 秒、输入 5.2 万 token、估 $0.04。
  会话结束没有 `ao land`，按既有逻辑转 needs_human 并通知；验证项随后标为 dropped，克隆已删。
  至此四个 hook 在新流水线里都有真实触发的证据，PreToolUse 的两条拦截路径（补丁、shell）也都被真实证明。
- **口径已改（agentops#15）**：`[budget_tokens]` 日上限改为不计缓存命中，只算未缓存输入 + 输出（`Usage.budget_tokens`）；数值不变。
