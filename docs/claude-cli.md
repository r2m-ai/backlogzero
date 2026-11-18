# Claude Code CLI 与 settings 核对记录

核对日期 2026-10-18。来源：`claude --help`、`claude --version`、code.claude.com/docs 的 cli-reference、headless、hooks、sandboxing、settings、env-vars 页面。
计划原文见 `docs/plan.md` 第 3 节、第 4 节 C6。本文记录实际情况，与计划不一致处以本文为准，汇总在最后一节。

## 1. 版本

| 工具 | 版本 | 备注 |
|---|---|---|
| Claude Code | 2.1.271 | 满足 ≥ 2.1.259 |
| just | 1.59.0 | brew |
| uv | 0.13.1 | brew |
| duckdb | 1.5.6 | brew |
| Python | 3.12.15 | `uv python install 3.12` |
| git | 系统自带 /usr/bin/git | |
| jq | 系统自带 /usr/bin/jq | |
| macOS | 26.5.2，arm64 | |

## 2. CLI 参数核对

| 参数 | 存在 | 实际语义与注意事项 |
|---|---|---|
| `--session-id <uuid>` | 是 | 必须是合法 UUID |
| `-n, --name <name>` | 是 | 会话显示名；`claude --resume <name>` 可按名恢复 |
| `-p, --print` | 是 | 非交互。退出码 0 成功，失败非零；认证失败会把失败写在 stdout 的 result 里 |
| `--output-format json` | 是 | 字段含 `result`、`session_id`、`total_cost_usd`、`is_error`、`num_turns`、`duration_ms`。**`--resume` 时 `total_cost_usd` 是整个会话的累计值**，`ao` 要按差值记每轮成本 |
| `--permission-mode auto` | 是 | 取值 acceptEdits / auto / bypassPermissions / manual(default) / dontAsk / plan。**`permissions.defaultMode: auto` 写在项目 settings 里无效**，只能用 CLI 参数或用户级 settings。计划用 CLI 参数，符合 |
| `--permission-prompts none` | 是 | 需 ≥ 2.1.259。`-p` 下凡是会弹询问的操作一律拒绝，并告诉模型不要重试；同时移除 AskUserQuestion 工具 |
| `--max-budget-usd <n>` | 是 | 仅 `-p`。客户端估算，**可能略超上限**，要留余量；子代理花费计入；`--resume` 恢复的历史花费不计入 |
| `-r, --resume <id>` | 是 | 配合 `-p` 可用；按 ID 会搜索本机所有项目 |
| `--model sonnet` | 是 | 别名 sonnet / opus / haiku / fable |
| `--settings <file-or-json>` | 是 | 覆盖同名键，仅本次会话；优先级仅次于 managed |
| `--bare` | 是 | **不用**。它跳过 hooks、钥匙串和 OAuth，只认 `ANTHROPIC_API_KEY`。官方说将来会成为 `-p` 的默认，所以 `ao doctor` 要用 `claude auth status` 检查 `authMethod` 是否仍为 `oauth_token` |
| `--add-dir` | 是 | 给额外目录读写权限，不读取其 `.claude/` 配置 |
| `--max-turns` | 是 | 可作为无人值守的额外保险 |
| `claude auth status` | 是 | 输出 JSON，`authMethod` 取值 none / claude.ai / oauth_token / api_key 等。`ao doctor` 用它 |

## 3. `-p` 模式关键事实

- `-p` 会执行项目 `.claude/settings.json` 里的 hooks，并且**不弹工作区信任对话框**。交互模式在新 worktree 里是否弹信任对话框，阶段 4 实测。
- SIGTERM 会让 `-p` 以 143 退出，当前回合不保存结果；要优雅结束用 SIGINT。`ao` 超时杀进程时先 SIGINT 再 SIGTERM。
- 回合结束后若有后台任务，`-p` 默认最多再等 10 分钟（`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`）。
- `--permission-prompts none` 配合 `--output-format stream-json` 时，被拒的请求出现在 `permission_denials`。

## 4. Hooks（计划 C6）

- 事件名精确拼写：`PreToolUse`、`PostToolUse`、`SessionStart`、`Stop`、`SessionEnd`、`PermissionRequest`、`WorktreeCreate`、`WorktreeRemove` 等。
- 结构：
  ```json
  {"hooks": {"PreToolUse": [{"matcher": "Edit|Write|NotebookEdit", "hooks": [{"type": "command", "command": "/path/to/protect_tests"}]}]}}
  ```
- `matcher`：只含字母数字 `_ - , |` 时是精确名列表；`*` 或空匹配全部；其他字符按正则。
- PreToolUse 的 stdin JSON 字段：`session_id`、`cwd`、`hook_event_name`、`tool_name`、`tool_input`、`tool_use_id`、`transcript_path`、`permission_mode`。
- 退出码：0 放行（stdout 若是单个 JSON 会被应用）；**2 拦截，stderr 作为原因返回给模型**；其他码不拦截只提示。计划用退出码 2，符合。
- 也可退出 0 并输出 `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`。
- SessionStart 额外有 `source`（startup / resume / clear / compact / fork）、可选 `model`、`session_title`。
- Stop 有 `last_assistant_message`；退出 2 会阻止停止，**不要这样用**。
- **hooks 在沙箱外运行**，所以 hook 脚本可以直接写 `~/.agentops/events.jsonl`。
- hooks 可放在项目 `.claude/settings.json` 并随仓库提交，符合计划。

## 5. 沙箱（计划 C6）

已核实的键：`sandbox.enabled`、`sandbox.autoAllowBashIfSandboxed`、`sandbox.excludedCommands`、`sandbox.failIfUnavailable`、`sandbox.allowUnsandboxedCommands`、`sandbox.filesystem.allowWrite | denyWrite | denyRead | allowRead`、`sandbox.network.allowedDomains | deniedDomains | allowLocalBinding | allowUnixSockets | strictAllowlist`。

事实：
- **沙箱只管 shell 命令**（Bash 及其子进程）。Read / Edit / Write / WebFetch 工具不受沙箱约束。`denyRead` 拦不住 Read 工具。
  - holdout 目录要两层：`permissions.deny: ["Read(~/data/quant-lab/holdout/**)"]` 拦工具，`sandbox.filesystem.denyRead: ["~/data/quant-lab/holdout"]` 拦 shell。`deny` 规则在项目文件里立即生效，不等信任。
- macOS 用 Seatbelt，无需额外安装。默认只能写工作目录，网络走本机代理，允许域名列表默认为空。
- 沙箱内即使在允许写的目录里，也**拒绝写 Claude Code 自己加载配置的路径**（`.claude/` 等），无法豁免。这天然保护了 `.claude/`。AGENTS.md、justfile、`.pipeline.toml` 仍靠 hook 和 steward 策略检查。
- 路径写法：沙箱键用 `~/x` 或绝对路径；permission 规则用 `//绝对路径` 和 `/项目相对路径`。
- `sandbox.filesystem` 数组跨作用域合并，不是覆盖。
- `strictAllowlist` 写在项目文件里无效，只认用户级、managed、`--settings`。因此 `ao start` 用 `--settings` 传入沙箱配置。
- `osascript` / `open` 在沙箱内报 -600；git 走 SSH 在沙箱内失败。agent 会话都不需要这些。
- 验证方法：让会话跑 `touch ~/sandbox-probe` 应报 Operation not permitted；`curl --noproxy '*' https://example.com` 应报 Could not resolve host。

对计划的影响：
- **会话在沙箱里写不了 `~/.agentops/`**，但计划要求会话内执行 `ao land` 入队、`just backtest` 登记 trials。改为 inbox 模式：只对 `~/.agentops/inbox/` 开 `allowWrite`，`ao land` 和 `ao trial register` 在会话内只写一个请求文件，steward 循环每 20 秒读 inbox，由它写队列、trials.jsonl 和 `land_requested` 事件。`approvals/`、`events.jsonl`、`trials.jsonl`、`workitems/` 对会话保持不可写。
- uv 缓存：会话内 `uv run` 需要写 `~/.cache/uv`，阶段 2 二选一：加 `allowWrite`，或在会话环境里设 `UV_CACHE_DIR` 指向 worktree 内。
- `BASH_DEFAULT_TIMEOUT_MS` 默认 2 分钟，`just test-all` 目标 3 分钟以内，会话 settings 的 `env` 里设为 600000。

## 6. permissions 与 settings 优先级

- 优先级从高到低：managed → `--settings` → `.claude/settings.local.json` → `.claude/settings.json` → `~/.claude/settings.json`。
- 键：`permissions.allow | deny | ask | additionalDirectories | defaultMode`，另有 `permissions.blockReadsOutsideWorkingDirectories`。
- 项目文件里的 `allow`、`additionalDirectories`、大部分 `env` 要等用户信任该文件夹才生效；`deny`、`ask` 立即生效。
- `defaultMode` 的 `auto` 和 `bypassPermissions` 不从项目文件生效。

## 7. 环境变量

| 变量 | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` 生成的一年期 token，夜间任务从钥匙串读出后设此变量 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 一把关掉自动更新、遥测、错误上报、反馈、发布说明、feature-flag 拉取。**任何非空值都算开启，包括 `0`**。阶段 4 先确认关掉 feature-flag 拉取不影响 auto 模式，再决定是否给 worker 会话加 |
| `DISABLE_TELEMETRY`、`DISABLE_ERROR_REPORTING` | 更窄的开关 |
| `CLAUDECODE=1` | Claude Code 给 Bash、hooks、MCP 子进程设的；IDE 集成终端也会设 |
| `CLAUDE_CODE_CHILD_SESSION=1` | 只有 Claude Code 自己起的子进程（Bash、Monitor、hooks）才有，比 `CLAUDECODE` 更可靠。**`ao approve` 检测到任一变量即拒绝** |
| `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` | 默认 120000 / 600000 |
| `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` | `-p` 回合结束后等后台任务的上限，默认 600000 |

## 8. 与计划的差异汇总

1. 沙箱 `denyRead` 不约束 Read 工具。holdout 加 `permissions.deny` 的 Read 规则，两层并用。
2. 会话在沙箱里不能写 `~/.agentops/`。`ao land`、`ao trial register` 改为写 `~/.agentops/inbox/`，由 steward 入账。hooks 在沙箱外，可直接写事件。
3. `permissions.defaultMode: auto` 和 `sandbox.network.strictAllowlist` 在项目文件里无效。`ao start` 用 `--permission-mode auto` 加 `--settings` 传入。
4. `-p` 模式不弹信任对话框，无人值守无此问题；交互模式阶段 4 实测。
5. `--bare` 将来可能成为 `-p` 默认且不读 OAuth。`ao doctor` 检查 `claude auth status` 的 `authMethod`。
6. `protect_tests` hook 只能拦 Edit / Write / NotebookEdit 工具；用 Bash 的 `sed` 改测试拦不住，由 steward 的"tests/ 下已有文件被改动"策略检查在合并时兜底。
7. `--resume` 时 `total_cost_usd` 为累计值，`ao` 按差值记账。

## 9. 补充：认证实测（阶段 0）

- 在 Claude Code 桌面 App 的会话里起 shell 运行 `claude auth status`，结果 `authMethod: none`、`loggedIn: false`。桌面 App 的登录态不会传给脚本起的 `claude` 进程。
- 设 `CLAUDE_CODE_OAUTH_TOKEN` 为钥匙串里的 token 后再运行，结果 `authMethod: oauth_token`、`loggedIn: true`。
- 结论：**`ao` 每次启动会话（交互、无人值守、steward 判断层、PM）都从钥匙串读 token 注入环境变量**，不只是夜间任务。token 只存在于子进程环境里，不落盘、不进日志。
- `claude auth status` 的 JSON 还有 `analyticsDisabled` 字段，`ao doctor` 可一并显示。

## 10. 补充：沙箱实测（阶段 2）

- 只用项目 `.claude/settings.json`（`sandbox.network.allowedDomains = []`）时，auto 模式下的 `curl https://example.com` **成功**：分类器按命令放行了域名。加 `--settings` 传入 `strictAllowlist: true` 后被拒。结论：网络封锁必须走 `--settings`，项目文件不够。
- holdout 两层保护都生效：Bash `cat` 报 Operation not permitted（沙箱 denyRead），Read 工具被 `permissions.deny` 拒绝。
- 写家目录被拒、系统临时目录可写、`~/.agentops/inbox` 可写（allowWrite）、`~/.agentops/approvals` 不可写。
- 用户级 `~/.claude/settings.json` 的 hooks（ECC GateGuard 等）会进入 `-p` 会话；`--setting-sources project,local` 可排除，项目 hooks 仍正常触发。
- 会话用 `--setting-sources project,local` 后用户级的署名设置也不生效：`templates/session-settings.json` 里写 `includeCoAuthoredBy: false` 和 `attribution: {commit: "", pr: ""}`（第三批发现会话提交带 Co-Authored-By 尾行）。
- 探测会话每次 9 轮、$0.15–0.28（客户端估算）。

## 11. 补充：worktree 下的沙箱写规则（阶段 2 评审后实测）

- cwd 是 git worktree 时，Claude Code 自动把所属仓库的整个 `.git`（common dir）设为可写，不需要在 allowWrite 里写 `objects`、`refs`。
- `--settings` 里的 denyWrite 优先：被拒目录之下的 allowWrite 会被丢弃（文档“Keep developers from widening the policy”一节的锁），所以不能“整目录拒、子目录放行”。
- 可行做法：逐个 denyWrite 要保护的具体路径（其他 worktree 的 `.git/worktrees/<名字>`、`_integration`、`index`、`HEAD`、`refs/heads/main`、`packed-refs`、`hooks`、`config`）。
- git 钩子不受沙箱影响（git 是会话里的子进程，钩子随之运行），`reference-transaction` 可以按环境变量限制会话只更新自己的分支。

## 12. 偏差记录：独立克隆替代 worktree（阶段 3）

计划 C2 和第 2 节用 git worktree（`~/work/.wt/<repo>/<id>-<slug>/`）。阶段 2 实测：worktree 共用主仓库的 `.git`，Claude Code 自动把它设为可写，`--settings` 又不允许"整目录拒、子目录放行"，其他会话和 `_integration` 的元数据无法彻底隔离。审核人决定改为每个工作项一份独立克隆 `~/work/.clones/<repo>/<id>-<slug>/`，主仓库和其他克隆对会话一律不可写。steward 的集成副本仍是主仓库的 worktree。实测 6 项隔离全部符合预期（`reviews/evidence/phase-3-clone-isolation/`（记录在运行状态目录，不随仓库发布））。执行报告需注明：用独立克隆替代 worktree，原因是共用 .git 无法彻底隔离。

## 13. 补充：`sandbox-exec` 实测（阶段 3）

- macOS 26 仍带 `/usr/bin/sandbox-exec`，接受 SBPL profile：`(version 1) (allow default) (deny network*) (deny file-write* (subpath …)) (allow file-write* (subpath …))`，以最后匹配的规则为准，所以先 deny 大范围再 allow 子目录可行。
- 实测：写被拒目录报 Operation not permitted，`curl` 被拒，允许目录里 `git init`/`commit` 正常。
- 注意不要放行整个系统临时目录（`$TMPDIR` 是 `/var/folders/...`），任何放在它下面的东西都会被重新打开；steward 只放行一个专用的 `mkdtemp` 目录并把它设为候选的 `TMPDIR`。
