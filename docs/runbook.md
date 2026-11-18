# 运维手册（runbook）

按阶段追加。每节写清：怎么装、怎么跑、怎么验证、出了问题看哪里。

## 0. 布局

| 路径 | 用途 |
|---|---|
| `~/agentops` | 套件仓库，提供 `ao` 命令 |
| `~/.agentops` | 运行状态：工作项、队列、审批、事件、会话、试验、槽位锁、inbox、config.toml |
| `~/work/quant-lab` | 业务仓库，main 只由 steward 合入 |
| `~/work/.wt/quant-lab/<id>-<slug>` | 每个工作项的 worktree |
| `~/data/quant-lab/{lake,holdout,edgar_manual}` | 数据湖、留出数据、手动下载的 EDGAR 目录 |

环境变量 `AGENTOPS_HOME` 可把运行状态改到别处，测试用临时目录。

## 1. 套件（阶段 1）

### 安装与自测

```bash
cd ~/agentops && just sync      # 建 .venv
just check                      # ruff + pytest
just install                    # 把 ao 装到 ~/.local/bin（uv tool，可编辑安装，改代码即生效）
ao init                         # 建 ~/.agentops 结构和默认 config.toml，可重复运行
ao doctor                       # 工具、版本、钥匙串、认证、目录检查；有 FAIL 退出码 1
```

### 认证

一年期 token 由 审核人 亲自生成并存入钥匙串，`ao` 启动任何 `claude` 进程时从钥匙串读出，注入 `CLAUDE_CODE_OAUTH_TOKEN`，不落盘、不进日志：

```bash
claude setup-token
security add-generic-password -s agentops-claude -a "$USER" -w     # 按提示粘贴
security add-generic-password -U -s agentops-claude -a "$USER" -w  # 换新 token 时用 -U 覆盖
```

`ao doctor` 的 `keychain` 行检查 token 格式（必须以 `sk-ant-oat01-` 开头，不打印内容），`auth` 行只确认 CLI 采用了它。
两者都不向服务器验证真伪；`ao doctor --live` 会发一次最小请求（haiku，1 轮）真正验证，装完 token 或换 token 后跑一次。
`ao doctor --no-auth` 跳过 auth 检查。

### 工作项

```bash
ao wi new --repo quant-lab --title "成交额第一回测" --type feat --module research \
  --priority P1 --depends 2 --acceptance "tests/... 覆盖月度调仓" --acceptance "just backtest ..." \
  --body-file spec.md
ao wi list [--repo quant-lab] [--status ready] [--json]
ao wi show quant-lab#4 [--json]
ao wi ready quant-lab#4                      # 规格就绪闸门：acceptance 为空会被拒
ao wi set quant-lab#4 --status in_progress   # 按状态机流转；--force 跳过（只给人用）
ao wi set quant-lab#4 --kv hint="steward 的说明" --kv returns=2   # 额外字段进 front matter
```

状态机：backlog → ready → in_progress → queued → testing → landed；分流到 returned、needs_human、dropped。
`trigger: nightly` 的工作项不参与依赖触发的自动派发，只在 `ao pm --dispatch --nightly` 时派发。
状态变化写 `wi_status` 事件。

### 事件

```bash
ao events emit hook_block --repo quant-lab --item 4 --session <id> --kv path=tests/x.py
ao events tail -n 20 [--type slot_wait] [--since 24h] [--repo quant-lab] [--item 4] [--json]
```

格式：`events.jsonl` 每行 `{ts, repo, item, session, type, detail}`，ts 带时区、毫秒精度。坏行跳过并在 stderr 警告。

### 重任务槽位

```bash
ao slot test --repo quant-lab --item 4 -- just test-all     # 槽位数取 config.toml [slots].test，默认 2
ao slot test --count 1 -- just backtest top_turnover         # 临时指定
```

拿不到槽先写 `slot_wait`，拿到写 `slot_acquired`（含等待秒数），结束写 `slot_released`（含 rc 和耗时）。锁文件在 `~/.agentops/slots/<name>.<i>.lock`，用 `fcntl.flock`，进程退出自动释放。

### 会话登记

```bash
ao session register --session-id <uuid> --repo quant-lab --item 4 --branch feat/4-x --worktree ~/work/.wt/... --mode headless
ao session list [--all] [--json]
ao session end --session-id <uuid> --rc 0 --cost-usd 1.25
```

`sessions.jsonl` 追加写，同一 session_id 的最后一条代表当前状态。

### 评审（ao review，计划第 6A 节）

Codex 做只读审稿人，Claude Code 做执行者。每个要评审的阶段一轮：执行 → 一次评审 → 一次修正。

```bash
ao review tag --phase 3                 # 阶段开始时：在套件和各业务仓库的 HEAD 打 phase-3-start 标签
# …本阶段工作…
vi AGENTOPS_HOME/reviews/phase-3.summary.md       # 执行者小结：做了什么、执行过的命令和结果、与计划不一致之处、本阶段要聚焦的问题
ao review prompt --phase 3               # 干跑：只生成 AGENTOPS_HOME/reviews/inputs/phase-3.prompt.md，不调用 codex，用来检查内容和脱敏
ao review run --phase 3                  # 跑测试、拼 prompt、codex exec 只读评审，写 AGENTOPS_HOME/reviews/phase-3.md
```

prompt 的顺序固定：流程文件正文、审稿人启动语、计划全文、审核人已确认的决定（版本改动说明、隐私规则、决策记录；公开版没有 `docs/decisions.md`，该节写“本仓库没有决策记录”）、本阶段验收行和相关 D 条、材料包（执行者小结、claude-cli.md、从 `phase-N-start` 到工作区的 diff、测试输出）、输出要求。
发给审稿人之前把 config.toml `[review].redact` 里的词（默认空，由本机配置填写）替换为“审核人”，家目录绝对路径替换为 `~`。
没有 `phase-N-start` 标签的仓库视为本阶段新建，diff 与空树比，并在 prompt 里注明。
`uv.lock` 等 `[review].diff_exclude` 里的文件不进 diff；diff 超过 `max_diff_lines` 只保留尾部，测试输出同理。

审稿命令来自 `[review].codex_cmd`，默认 `codex exec -s read-only -C {repo} --skip-git-repo-check -o {output} -`，prompt 走标准输入。装好 Codex 后先看 `codex exec --help` 核对参数，不一致就改 config。环境变量 `AGENTOPS_CODEX_CMD` 可临时覆盖（测试用假 codex 就靠它）。

评审记录 `AGENTOPS_HOME/reviews/phase-N.md` 的结构：头部（时间、流程和计划版本、评审方式、命令、diff 范围、测试结果、效果记录三个空）、Codex 原文、处置表（编号、意见摘要、采纳/拒绝/待查证、依据或改动位置）、修正后复测。执行者处置完要回填效果记录：意见数、采纳数、真 bug 数。
输入存档在 `AGENTOPS_HOME/reviews/inputs/`：prompt、测试输出、codex 的原始输出和日志。
评审记录和输入存档默认都写在运行状态目录 `AGENTOPS_HOME/reviews/`（默认 `~/.agentops/reviews/`），不随仓库发布；`AGENTOPS_REVIEWS_DIR` 可改到别处。手工收集的证据（探测脚本、实测输出等）放 `AGENTOPS_HOME/reviews/evidence/<主题>/`，不要放进仓库。历史记录由人侧复制到该目录，文档里提到的 `reviews/...` 路径都指这里。
每次运行写 `review_run` 事件。

### 配置

`~/.agentops/config.toml`：`[general]` 并发、预算、免打扰；`[models]`、`[budget_usd]`；`[slots]`；`[repos]` 仓库路径；`[data]` 数据目录；`[review]` 审稿命令、脱敏词、diff 排除。缺的节用内置默认值。改后下一次 `ao` 命令生效，常驻进程需重启。

### 排错

- `ao doctor` 报 `keychain FAIL`：按上面"认证"一节存 token。
- `auth FAIL, authMethod=none`：token 失效或钥匙串条目为空，重新生成并用 `-U` 覆盖。
- 槽位一直等待：`ls ~/.agentops/slots/`，用 `lsof <lock 文件>` 看谁占着；持锁进程退出锁就释放，不需要手动删锁文件。

## 2. 业务仓库与判定器（阶段 2）

### 接入一个仓库

```bash
ao repo init ~/work/<repo> --name <repo>   # git init（缺则建）、复制模板、装 git 钩子；已有文件跳过，--force 覆盖
ao repo install-hooks ~/work/<repo>        # 只重装 git 钩子
```

模板在 `~/agentops/templates/`：`AGENTS.md`、`CLAUDE.md`（只有一行 `@AGENTS.md`）、`pipeline.toml` → `.pipeline.toml`、`claude/settings.json` → `.claude/settings.json`、`justfile`（流水线要求的 recipe 名）、`workitem-spec.md`（工作项正文模板）、`session-settings.json`（`ao start --settings` 用）、`git-hooks/`。

### quant-lab

```bash
cd ~/work/quant-lab
just sync          # 建 .venv，需要联网，只在会话外由人执行；worktree 由 ao start 在会话外执行
just test-fast     # ruff + pytest，约 1 秒
just test-all      # test-fast + 临时夹具库上 dbt build，约 5 秒。合并队列只认它
just capture       # 阶段 2 占位：只写 capture_noop 事件
just nightly       # 阶段 2 占位：写 nightly_start / nightly_end 事件
just backtest X    # 未实现策略时退出码 2（工作项 4）
just export-csv    # 旧命令，工作项 7 要删
```

所有 `uv run` 带 `--offline`，会话里不联网也能跑；dbt 关了匿名统计和版本检查。
刻意预埋的靶子：`src/quant_lab/research/metrics.py` 的 `summarize()`（工作项 4、5 冲突），`src/quant_lab/export_csv.py` 和 `just export-csv`（工作项 7 删除）。
夹具：`fixtures/prices_sample.csv`（含虚构的 NVDA 拆股）、`fixtures/edgar_form_sample.idx`（跨季度边界）。

### Claude Code hooks（`~/agentops/hooks/`）

项目的 `.claude/settings.json` 用 `$HOME/.local/bin/python3.12` 调用它们（系统 python3 是 3.9，太旧）。只依赖标准库。

| hook | 事件 | 做什么 |
|---|---|---|
| `protect_tests.py` | PreToolUse，匹配 Edit/Write/NotebookEdit/MultiEdit | 读 `.pipeline.toml` 的 `[protected]`：受保护路径一律拒；`existing_only` 目录下 main 上已有的文件拒、新文件放行。拒绝时退出码 2、原因进 stderr、写 `hook_block` 事件 |
| `register_session.py` | SessionStart（startup/resume） | `ao session register`，写 `session_start` 事件；有 `AGENTOPS_ITEM` 时把工作项提示放进上下文 |
| `stop_report.py` | Stop | 写 `session_stop` 事件，提醒 `ao land`。不跑测试 |

会话环境变量（阶段 4 由 `ao start` 设置）：`AGENTOPS_ITEM`、`AGENTOPS_MODE`（interactive/headless）。`AGENTOPS_AO` 可指定 ao 的路径，默认 `~/.local/bin/ao`。ao 不可用时 hook 仍给出判定，只在 stderr 警告。

局限：hook 只拦工具调用，用 Bash 的 `sed` 改测试拦不住，由 steward 合并时的受保护路径检查兜底。

手动验证：

```bash
printf '{"session_id":"x","cwd":"%s","tool_name":"Edit","tool_input":{"file_path":"tests/test_metrics.py"}}' ~/work/quant-lab \
  | ~/.local/bin/python3.12 ~/agentops/hooks/protect_tests.py; echo "exit=$?"   # 期望 2
```

### main 保护（git 钩子）

装在业务仓库的 `.git/hooks/`，所有 worktree 共用，沙箱里不可写：

- `pre-commit`、`pre-merge-commit`：当前分支是 main 且没有 `AGENTOPS_STEWARD=1` 就拒绝。
- `reference-transaction`：任何把 `refs/heads/main` 改成不同值的引用更新（`git update-ref`、`git merge` 等）都拒绝，除非 `AGENTOPS_STEWARD=1`。
- 每次拦截写 `main_protect_block` 事件。
- `ao repo init` 顺带设 `gc.auto=0`：沙箱里 `packed-refs` 不可写，自动 gc 会报错；steward 可以在会话外定期 `git gc`。

steward 合入时设 `AGENTOPS_STEWARD=1`。人在 main 上提交也要带这个变量，并明白这绕过了队列。

### 沙箱

两层配置：
- 项目 `.claude/settings.json`：hooks、`permissions.deny`（Read holdout、Edit/Write approvals）、沙箱基础项（holdout `denyRead`，inbox 和 uv 缓存 `allowWrite`，网络白名单为空）。手动 `claude` 进项目也受保护。
- `templates/session-settings.json`：`ao start --settings` 传入，由 `repo.render_session_settings(repo, worktree)` 按仓库和 worktree 渲染。补上项目文件里无效的 `strictAllowlist`、`allowUnsandboxedCommands=false`，以及 worktree 提交需要的 `.git/objects|refs|logs` 和**本会话自己的** `.git/worktrees/<名字>` 写权限（不放行整个 `worktrees/`，别的会话和 `_integration` 的 HEAD、index 碰不到），同时 `denyWrite` `.git/refs/heads/main`、`packed-refs`、`hooks`、`config`。

`ao start` 一律带 `--settings <渲染结果> --setting-sources project,local`。阶段 2 实测：只靠项目文件，auto 模式仍能出网（分类器按命令放行域名）；用户级 `~/.claude/settings.json` 的 hooks（例如 ECC GateGuard）会进入所有会话，排除 user 来源后会话行为只由仓库和套件决定，项目 hooks 照常触发。

沙箱只管 shell 命令。Read 工具靠 `permissions.deny`。手动验证：`reviews/evidence/phase-2-sandbox/probe2.sh`（记录在运行状态目录，不随仓库发布） 是完整的探测脚本。

## 3. 合并队列与 steward（阶段 3）

### 独立克隆代替 worktree（审核人在阶段 2 评审后拍板）

每个工作项一份本地 `git clone`（硬链接对象）：`~/work/.clones/<repo>/<id>-<slug>/`，分支 `<type>/<id>-<slug>`。克隆里设本地身份 `agentops-session <session@localhost>`、装 main 保护钩子、`gc.auto=0`。会话的沙箱只能写自己的克隆；主仓库和其他克隆都在 cwd 之外本就不可写，`render_session_settings(repo, clone, main_repo=…, other_clones=…)` 再逐个 denyWrite 作双保险。合入或放弃后删除克隆。

```bash
ao clone create quant-lab#4      # 阶段 4 的 ao start 会调用它
ao clone list [--repo quant-lab]
ao clone remove quant-lab#4 --reason dropped
```

### 会话里：ao land

```bash
ao land            # 在克隆目录里执行；要求在工作分支上、工作区干净
```

沙箱里写不了队列和事件，所以它只把 `{repo, item, branch, sha, clone_path, session}` 投递到 `~/.agentops/inbox/land/`，steward 读 inbox 入队并写 `land_requested` 事件。repo 和 item 从克隆路径反推，或取 `AGENTOPS_REPO`/`AGENTOPS_ITEM`。

### steward

```bash
ao steward --once [--repo quant-lab]   # 每个仓库处理队列第一项
ao steward --loop [--poll 20]          # 常驻；launchd 模板 launchd/com.agentops.steward.plist（阶段 5 安装）
ao queue [--all] [--json]              # 看队列
```

每一项的处理顺序：
1. **SHA 检查**：克隆里分支头和入队时不一致 → 旧请求标 dropped，新 SHA 以 `requeued` 进队尾，写 `requeued` 事件。
2. **集成**：在主仓库的游离 worktree `~/work/.wt/<repo>/_integration` 里 `git fetch <克隆> <分支>`，检出分支头，`git rebase main`。冲突 → abort，写 `rebase_conflict`，工作项 returned（hint 里有冲突输出）。
3. **准备 + 测试**：`.pipeline.toml` 的 `prepare_cmd`（quant-lab 是 `uv sync --offline`）后，在 `ao slot test` 槽位里跑 `test_cmd`，带 `[steward].test_timeout_minutes` 超时，输出写 `~/.agentops/logs/<repo>/<请求id>.log`。失败 → `tests_fail`，returned，hint 带日志尾部。
4. **策略检查**（以 main 上的 `.pipeline.toml` 为准）：受保护路径有改动；`existing_only` 目录下 main 上已有文件被改或删（**第二道防线**，Bash 改测试在这里兜住）；删除 `src/` 下文件；删公开函数（`-def` 无对应 `+def`）。触发而没有 `(repo, item, 入队 sha)` 的审批记录 → `rule_triggered`、`needs_human`，桌面通知（免打扰时段只记事件），工作项记 `approval_sha`。`autonomy = "approve"` 时每次都要审批。
5. **合入**：主仓库 `git merge --ff-only`（带 `AGENTOPS_STEWARD=1`），写 `landed`，工作项 landed，删克隆，写 `dispatchable` 事件列出被解锁的下游工作项。

退回次数超过 `[steward].max_returns`（默认 2）转 needs_human。队列日志 `~/.agentops/queue/<repo>.jsonl` 只追加，一个请求的最新一行是当前状态。

**候选代码的沙箱**（阶段 3 评审 R1）：第 3 步的 `prepare_cmd` 和 `test_cmd` 跑的是候选分支里的代码，steward 用 macOS 自带的 `sandbox-exec`（Seatbelt）把它们关起来：只能写集成副本、本次专用临时目录（作为 `TMPDIR`）、`/tmp` 和 uv 缓存；写不了 `~/.agentops`、主仓库、克隆根、套件目录、`~/.claude`、钥匙串；不能联网。环境里不带 `AGENTOPS_STEWARD`、token 和 API key。`[steward].sandbox = false` 可关；`sandbox-exec` 不可用时写 `steward_warning` 事件并照常运行。
**崩溃恢复**（R2）：steward 每轮开始把上次停在 processing 的请求重新排队（`recovered` 事件）。整个流程幂等，已经快进过的分支再处理一遍会得到空 diff 并正常记 `landed`。

### 审批

```bash
ao approve quant-lab#7 --sha <approval_sha>   # 人在终端；看板按钮是另一条通道（阶段 5）
```

记录写 `~/.agentops/approvals/<repo>-<item>-<sha>.json`，绑定入队时的 SHA，分支再有提交就要重新审批。在 Claude Code 会话里执行（`CLAUDECODE` 或 `CLAUDE_CODE_CHILD_SESSION` 存在）一律拒绝。steward 每轮把已审批的 needs_human 工作项重新入队。

### 自测场景（tests/test_steward.py，临时仓库）

干净合入、rebase 冲突退回、测试失败退回、受保护路径转 needs_human、审批后合入、入队后新提交重新排队、Bash 改已有测试被第二道防线拦下，另有删 src 文件、删公开函数、退回超限、会话内 approve 被拒、脏工作区 land 被拒。克隆隔离的真实沙箱探测见 `reviews/evidence/phase-3-clone-isolation/`（记录在运行状态目录，不随仓库发布）。

### 排错

- `ao steward --once` 报“已有 steward 在运行”：`~/.agentops/steward/<repo>.lock` 被常驻进程占着，先停 launchd 任务。
- 集成 worktree 坏了：删掉 `~/work/.wt/<repo>/_integration`，steward 会重建。
- 工作项卡在 needs_human：`ao wi show` 看 `hint` 和 `approval_sha`，批准用 `ao approve`，放弃用 `ao wi set --status dropped`。

## 4. 会话、判断层和 PM（阶段 4）

### ao start / ao resume

```bash
ao start quant-lab#4                      # 交互：建克隆、准备依赖、写启动脚本，在 Terminal.app 新窗口里打开会话
ao start quant-lab#4 --headless --budget 3 --model sonnet   # 无人值守：前台跑到结束，记 total_cost_usd
ao start quant-lab#4 --dry-run            # 只准备，不启动；看 ~/.agentops/sessions/<uuid>/ 里的 prompt.md、settings.json、start.sh
ao resume quant-lab#4 --round 1           # 被退回后让原无人值守会话重做一轮（steward 会自动调用）
ao session prune                          # 把超过 [sessions].stale_after_hours 没更新的"运行中"会话标记结束
```

预检：状态必须是 ready，依赖都已 landed，同模块没有活动会话，运行中会话数小于 `max_sessions`（陈旧会话不计）。
每个会话一个目录 `~/.agentops/sessions/<uuid>/`，会话写不了它：`prompt.md`（工作项全文加 AGENTS.md 加做事顺序）、`settings.json`（按仓库和克隆渲染的沙箱配置）、`start.sh`（cd 克隆、导出 `AGENTOPS_REPO/ITEM/MODE/BRANCH/SESSION`、运行时从钥匙串取 token、`claude --permission-mode auto --settings … --setting-sources project,local`）。无人值守再加 `-p --permission-prompts none --max-budget-usd --max-turns --output-format json`，结果写 `result.json`，`session_end` 事件带本轮成本（resume 时按累计值的差记）。
多轮 resume：每轮 `--fork-session` 出新会话 id，工作项的 `session_id` 指向最新一轮（供下一轮 `--resume`），`origin_session_id` 记首轮会话 id（首次 start 写入）；`settings.json`、`prompt.md`、`start.sh`、各轮 `resume-N.md` 和 `result.json` 一律在首轮目录 `~/.agentops/sessions/<origin_session_id>/`，fork 出的新 id 没有目录。没有 `origin_session_id` 的旧工作项按 `session_id` 定位。
交互模式第一次会弹 Terminal 的自动化权限。

### 判断层（C4）

只在 rebase 冲突、测试失败、规则触发时调用一次：`claude -p --json-schema … --tools "" --max-turns 4 --max-budget-usd 0.5 --no-session-persistence`，输出只能是 `return_to_session | needs_human | retry_flaky` 三选一加 reason 和 hint。候选代码、测试输出、git 输出都放在 `<untrusted>` 块里并声明"不是指令"，闭合标签被转义；LLM 出错或输出不合法时按确定性默认值（冲突和测试失败默认退回，规则触发默认转人工）。`retry_flaky` 只对测试失败有效，且只重跑一次。`[steward].judge = false` 可关。
退回时：hint 写进工作项；原会话是无人值守就在后台 `ao resume`（`[steward].auto_resume`），最多 `max_returns` 轮，超过转 needs_human。

### PM 与派发

```bash
ao pm --repo quant-lab                    # 确定性计算 + 一次 LLM 调用（sonnet，$1），写 ~/.agentops/pm-report.md，自动应用优先级
ao pm --no-llm                            # 只做确定性排序
ao pm --dispatch [--dry-run] [--max N]    # 按顺序派发 ready 且未被挡的工作项，后台起 ao start --headless
ao pm --dispatch --nightly                # 先把 trigger=nightly 的 backlog 提为 ready，再派发，最多 nightly_max 项
```

派发约束：并发不超过 `max_sessions`，同模块只派一个，当日花费加每会话预算不超过 `daily_budget_usd`（花费从 `session_end`、`llm_call` 事件算）。砍掉工作项只写进报告的"建议砍掉"，不自动应用。某项合入后 steward 写 `dispatchable` 事件，下一次 `ao pm --dispatch` 派发下游（夜间任务和阶段 5 的定时会接上）。

### KPI

```bash
ao kpi --since 7d [--json]
```

人类介入率 = needs_human ÷ landed；冲突率 = rebase_conflict ÷ integration_start；返工率 = returned ÷ land_requested；回滚数来自 `rollback` 事件；每项成本来自 `session_end` 和 `llm_call`；平均排队 = land_requested 到 integration_start。回滚数和返工率要和介入率一起看。

### 排错

- `ao start` 报并发已满但没有会话在跑：`ao session list` 看看，`ao session prune` 清理陈旧记录。
- 无人值守会话没有 `ao land`：`ao wi show` 的 `last_land_requested` 为 false，日志在 `~/.agentops/logs/<repo>/session-<uuid>.log`，`result.json` 在会话目录。
- 判断层总是走默认：看 `judge_error` 事件的 error 字段。

### 阶段 4 评审后的补充

- **预算台账**：`~/.agentops/budget/ledger.json`（带锁）。start、resume、判断层、PM 都先 `reserve` 再运行，结束 `settle`；`ao pm --dispatch` 用剩余额度判断。当日已结算花费从 `session_end`、`llm_call` 事件算。余额不足时 start 和 resume 报错、判断层走默认动作、PM 走确定性排序，并写 `budget_refused` 事件。
- **启动锁**：`~/.agentops/start.lock`。预检、预留、占位、登记在锁内；克隆失败会回滚（`session_rollback` 事件）。
- **resume 用 `--fork-session`**：每轮是新会话 id，成本独立计；工作项的 `session_id` 跟着更新。
- **SessionEnd hook** 标记会话结束，Stop 不再释放并发（交互会话一次回答结束后还会继续）。
- **自动派发**：steward 合入后和队列空闲时调用 `pm.dispatch`；`[steward].auto_dispatch = false` 可关。派发过的工作项带 `dispatch_pending`，10 分钟内不重复派发。
- **交互模式**：`ao start` 第一次打开 Terminal.app 会弹「允许控制 Terminal」，30 秒内不点就超时并自动回滚；点允许后重跑即可。
- 已知局限：`<untrusted>`、`<steward-hint>` 的标签隔离是对模型的约束，不是证明；判断层没有工具、不能批准，是真正的权限边界。

## 5. 看板与定时任务（阶段 5）

### 看板

```bash
ao board                 # 只监听 127.0.0.1:8749（config [board].port），标准库实现，页面每 30 秒自刷新
```

10 个面板：需要你看（needs_human、退回 ≥ 2 次、PM 建议砍掉）、工作项、正在跑的会话、合并队列、重任务槽位、今天的计划和实际运行、dbt 结果（读主仓库 `dbt/target/run_results.json`）、数据新鲜度（`lake/freshness.json` 优先，否则按文件时间；EDGAR 手动目录超过 92 天提醒）、磁盘、KPI（7 天）。`/api/state` 返回同样的数据（JSON），`/healthz` 返回 ok。任何一个面板出错只显示错误，不拖垮整页。

审批通道：面板里的「批准」和「确认放弃」按钮 POST 到 `/approve`、`/drop`。防伪造：页面嵌入启动时随机生成的令牌，POST 必须带上；`Host` 必须是 `127.0.0.1:8749`，`Origin` 必须是 `http://127.0.0.1:8749`（没有 Origin 的请求也拒）；审批绑定 (工作项, SHA)，表单里的 sha 必须等于工作项当前的 `approval_sha`，否则 409。所有拒绝写 `board_rejected` 事件。批准走 `approvals.record(by="board")`，steward 下一轮重新入队。

手动验证：`curl -X POST -d token=x http://127.0.0.1:8749/approve` 应返回 403。

### 定时入口

```bash
ao capture               # 每个仓库 caffeinate -i just capture；capture_start/capture_end 事件；日志 ~/.agentops/logs/<repo>/capture-<ts>.log；超时 [schedule].capture_timeout_minutes（默认 60，全市场首拉设 360）；EODHD 首拉约 5 万次调用、每小时约 1.4 万只，manifest 可断点续跑，超时后再跑一次即接着拉
ao nightly               # just nightly → ao pm → ao pm --dispatch --nightly → 等夜间派发项合入（[schedule].nightly_wait_minutes，默认 30）→ ao brief；每步失败只记事件（nightly_step_error），后面照跑
ao brief [--date]        # 每日一页简报到 ~/.agentops/briefs/<日期>.md：过去 24 小时的事件、合入项、需要你看、队列与会话、7 天 KPI
```

### launchd

```bash
ao launchd render        # 渲染 4 个 plist 到 ~/.agentops/launchd/（只写文件，不加载）
ao launchd commands      # 打印安装（复制进 ~/Library/LaunchAgents 并 launchctl bootstrap）、手动触发、卸载的命令
ao launchd status        # launchctl print 看是否已加载
```

安装会添加 4 个后台登录项（`com.agentops.steward`、`board`、`capture`、`nightly`），macOS 会提示“已添加后台项目”，按开场指令第 2 条由人执行。时间在 config `[schedule]`（太平洋时间：capture 14:00，nightly 01:30）；steward 和 board 常驻（KeepAlive）。日志在 `~/.agentops/logs/launchd/`。错过的定时任务在唤醒后补跑一次是 launchd 的默认行为。

手动触发：`launchctl kickstart -k gui/$(id -u)/com.agentops.capture`。卸载：`launchctl bootout gui/$(id -u)/com.agentops.<name>`。

### 排错

- 看板打不开：`ao launchd status` 看 board 是否已加载；`~/.agentops/logs/launchd/board.err.log`；端口被占用时改 config `[board].port` 并重新渲染安装。
- 按钮点了没反应：看 `board_rejected` 事件的 reason；页面过期（SHA 变了）会 409，刷新再点。
- 标回 ready 后 steward 没派发：派发后工作项上会留 `dispatch_pending` 时间戳，10 分钟内不重复派发（防止子进程还没启动就重派）。会话失败后重派要等窗口过去，或 `ao wi set <ref> --kv dispatch_pending=` 清掉。`ao start --dry-run` 不是纯只读：它会建克隆并把工作项置为 in_progress，用完要 `ao wi set --status ready --force` 并删克隆。
- 工作项转 needs_human 但没有 approval_sha：是无人值守会话结束却没投递 `ao land`（agent 按规则停下、报错或预算耗尽），hint 里有它最后回复的摘要，克隆还在。改完规格后 `ao wi set <ref> --status ready --force`，steward 循环会自动再派发；不要了就在看板点「确认放弃」。
- 定时任务没跑：`launchctl print gui/$(id -u)/com.agentops.nightly` 看 last exit code；`caffeinate` 只防空闲休眠，合盖仍会睡。

## 6. 演示验收期间的实测记录（阶段 6）

- **yfinance 申报列表能回溯多少**：2026-10-19（周一）首次刷新，58 只有数据（SPY、GOOGL、BRK-B 返回 404 "No fundamentals data"，只记警告），共 6048 条；每只最早一条集中在 2021-04 到 2022-02，也就是大约 5 年。要更早的申报只能靠手动下载的 EDGAR 目录。
- **大幅变动告警在真实数据上的命中**：MS 2008-10-13 单日 +87%（close 与 adj_close 同向），是真实事件，warn 级不阻塞 build。yfinance 的 Close 已按拆股调整，所以"close 变、adj_close 不变"的拆股特征在真实数据里不会出现，只在夹具里有。
- **空 EDGAR 目录**：首次真实 `just nightly` 因空 parquet 缺列类型让 `fct_edgar_filings` 报错（工作项 9 修复）。手动下载的目录文件放到 `~/data/quant-lab/edgar_manual/` 后 `just load-edgar` 或夜间任务会读。
- **Claude 用量上限**：订阅的会话额度用完时，无人值守会话启动即报 "You've hit your session limit"，流水线把工作项转 needs_human（hint 里有这句话），不会丢状态。额度重置后：删掉残留克隆 `~/work/.clones/quant-lab/<id>-*`，`ao wi set <ref> --status ready --force`，steward 循环会自动重派。并发 2 时四五个会话加上裁决、PM 调用，一小时内就可能触顶；额度紧张时把 `max_sessions` 降到 1。
- **重新排队的脚本化场景**：`ao land` 之后往分支再提交一次（哪怕空提交），steward 发现分支 tip 不等于入队 SHA 就写 `requeued` 并以新 SHA 排到队尾。

### 交互模式（ao start 不带 --headless）

- 第一次会弹 macOS「允许控制 Terminal」的权限框，30 秒内不点就超时回滚；点一次以后不再问。
- 每个克隆都是新目录，Claude Code 开窗后先问「是否信任这个文件夹」，按回车才开始；没人按它就一直等（没有 hook 事件、没有改动）。
- 回滚后工作项回到 ready，常驻 steward 会在 20 秒内把它无人值守接走；想留给交互会话就先 `ao wi set <ref> --status backlog`，开窗前再改回 ready。
- 做完 `ao land` 后在窗口里输入 `/exit`，SessionEnd hook 才会把会话记为结束。
- 会话里跑测试要用的临时文件必须放在克隆目录内（justfile 的 `dbt-fixture` 用 `.tmp/`）；`/var/folders` 和 `/tmp` 在会话沙箱里不可写。

## 7. 模型与思考强度

- `~/.agentops/config.toml` 的 `[models]`（worker/steward/pm）和 `[effort]`（同样三个键，空字符串表示用 Claude Code 默认）。改 config 后常驻 steward 要 `launchctl kickstart -k gui/$(id -u)/com.agentops.steward` 重新加载。
- 单个工作项可覆盖：`ao wi new ... --model opus --effort high` 或 `ao wi set <ref> --model opus`；字段存在 front matter 的 `model`/`effort`。

## 8. 第三方数据 token 与 EODHD

- `ao secret set <name>` 从终端读入 token（不回显）存进钥匙串 `agentops-<name>`；`ao secret check <name>` 只报长度。`ao capture` / `ao nightly` 按 config `[secrets]`（名字 = 环境变量名，默认 `eodhd = "EODHD_API_TOKEN"`）把 token 注入 `just` 子进程的环境，业务代码只读环境变量；缺失时写 `<recipe>_secret_missing` 事件照跑。会话沙箱没有网络也读不到钥匙串。
- EODHD 接口：`exchange-symbol-list/US`（加 `delisted=1` 取退市列表，字段 Code/Name/Exchange/Type/Isin，没有退市日期，用日线最后一天代替）、`eod/<CODE>.US?from=2005-01-01`（open/high/low/close/adjusted_close/volume）、`user`（当日已用额度）。每次调用计 1 次，日上限 10 万，全市场约 2 万只在市加 3 万退市普通股，全量首拉要分两天。

## 9. PM 提案模式（第二批起）

- 开关在 `~/.agentops/config.toml` 的 `[pm]`：`propose = true` 后，夜间任务在 `pm.plan` 之后调一次 `pm_propose.propose`；也可手动 `ao pm --repo quant-lab --propose`。
- PM 读什么：`context_files`（仓库里人维护的研究背景，默认 `docs/research-log.md`，截到 `context_chars`）、工作项清单、最近 14 天合入项的最后回复摘录（`last_result`）。提示词在 `templates/prompts/propose.md`，这些内容都包在 `<untrusted>` 里，视为数据。
- 产出：合法的建议写成 **backlog** 工作项（`proposed_by: pm`、`pm_reason`、`proposed_at`），绝不自动 ready，更不会派发。拒绝规则：同名工作项已存在（任何非 dropped 状态）、type/priority 不合法、没有验收条目、depends_on 引用不存在的 id。每晚最多 `max_proposals` 条；待决定的建议达到 `max_open` 条就不再调用 LLM（记 `pm_propose_skipped`）。
- 人怎么决定：看板第 2 面板「PM 建议」，每条有「采纳」（backlog → ready，之后 steward 按常规派发）和「放弃…」两步确认；命令行等价于 `ao wi set quant-lab#N --status ready`。每日简报有「PM 建议（待你在看板采纳或放弃）」一节。
- 事件：`pm_proposed`（每条）、`pm_propose_run`（含被拒原因与 notes）、`pm_propose_skipped`、`pm_propose_error`、`proposal_adopted`。
- 成本：每晚一次 Opus/high 调用，约 $0.2–0.4；没有新背景时 PM 应给空列表。
- 排错：建议质量差 → 先改 `docs/research-log.md`（PM 只知道那里写的）；建议重复 → 检查标题是否只是措辞不同（去重只看标题）；改代码后看板和 steward 常驻进程要 `launchctl kickstart -k` 才会加载新代码，nightly 每次新起进程不用。

## 10. 试验登记走 inbox（会话 → steward）

- 会话沙箱只能写克隆目录和 `~/.agentops/inbox`，所以 `just backtest` 在会话里追加不了 `~/data/quant-lab/trials.jsonl`。quant-lab#27 之后，`quant_lab.trials.register` 在登记簿不可写时把这一行写成 `~/.agentops/inbox/trials/quant-lab-<UTC>-<pid>-<uuid8>.jsonl`；`count` 把 inbox 里待入账的行也算进 n_trials。
- steward 每轮（`process_repo`，与 land 请求的 inbox 一起）调 `trials.ingest`：校验每行是带 `ts/strategy/params/data_range/seed/git_sha` 的 JSON 对象，追加进登记簿并加 `source: session`、`inbox_file`，然后删文件；坏文件改名 `.bad` 并记 `inbox_rejected`。登记簿路径在 `[data] trials`。
- 面板缓存：`research/panel.py` 的缓存按输入文件 mtime 判断新鲜；每天 14:00 的采集以前总是重写 `lake/universe/monthly.parquet`，缓存天天过期，会话里重建缓存要写 `~/data` 会被拒（#23 的会话只好复制数据到本地跑）。quant-lab#26 之后内容不变不重写。如果再出现「会话里缓存过期」，由人在主仓库跑一次任意 `just backtest <策略>` 重建（约 30 秒）。

## 11. 用流水线维护 agentops 自己（第二个仓库，2026-11-07 起）

执行报告 §9 建议 6。agentops 作为第二个业务仓库接入，用来验证跨仓库的队列、槽位和看板，也用来修流水线自身的待办。

- **接入**：`ao repo init ~/agentops --name agentops`；`~/.agentops/config.toml` 的 `[repos]` 加 `agentops = "~/agentops"`；`.pipeline.toml`：`autonomy = "approve"`、`prepare_cmd = "uv sync --offline"`、`max_sessions = 1`。justfile 补了流水线要求的 `test-fast`、`test-all`（= check）、`capture`、`nightly`（后两个是空操作：定时任务对每个仓库都跑 `just capture` / `just nightly`），`uv run` 一律 `--offline`。
- **自举的特殊性**：`~/.local/bin/ao` 是对 `~/agentops` 的可编辑安装（uv 收据 `editable = "~/agentops"`）；launchd 的 steward/board、会话里的 Claude Code hooks（`$HOME/agentops/hooks/*.py`）都直接运行 main 上的代码。会话在克隆里改，不影响运行中的 ao；steward 快进合入 main 的那一刻起，下一次 ao 调用就用新代码。所以每次合入都要人批；批之前人侧在主仓库拉候选分支无沙箱跑一遍 `just test-all`（原因见下条）。
- **受保护路径**（会话改了会被拦或转人工）：`hooks/`、`launchd/`、`templates/session-settings.json`、`templates/claude/`、`templates/prompts/`、`pyproject.toml`、`uv.lock`、`docs/decisions.md`（内部记录，未随公开版发布）、`docs/plan.md`、`docs/reviews/`。`templates/git-hooks/` 等只在人侧 `ao repo init` / `install-hooks` 时用，不保护。
- **测试与沙箱**：agentops 的测试套件自己要跑 steward（嵌套 sandbox-exec 被拒，rc 71）、绑 localhost（`deny network*`）、写钥匙串，这些在 steward 的候选沙箱和会话沙箱里都不可用。`tests/sandboxing.py` 的 `inside_sandbox()`（探测：再套一层 `sandbox-exec` 是否被拒）让套件自适应：沙箱里被测 steward 写 `sandbox = false`；看板 HTTP 测试（6 个）、真实钥匙串测试、r1 沙箱测试跳过。核对（2026-11-07）：主仓库无沙箱 179 过（86 秒）；干净克隆 + steward 沙箱 + `TMPDIR=run_tmp`：`uv sync --offline` 过，`just test-all` 171 过 8 跳过（82 秒）。因此涉及 `board.py`、`sandbox.py`、`token_setup.py` 的改动在流水线里测不全，批准前人侧要无沙箱跑一遍。
- **人侧新规矩**：往 `~/agentops` main 提交现在也要 `AGENTOPS_STEWARD=1`（装了同一套 main 保护钩子），提交前确认 `ao session list` 里没有 agentops 的在途会话。
- **常驻进程**：`ao board` 和 `ao steward` 启动时读一次 config，`[repos]` 改了要重启两者才认识 agentops（命令见 `ao launchd commands`）。
- **已知**：夜间 `ao pm --propose` 对每个仓库都跑；agentops 没有 `docs/research-log.md`，PM 会在「没有研究背景文件」下提案（每晚约 $0.2，看板「PM 建议」里放弃即可）。按仓库关 PM 提案是一个待办。
- **第一批工作项**（规格在 `reviews/evidence/selfhost-batch-1/`（记录在运行状态目录，不随仓库发布），都在 backlog，人在看板置 ready 后派发）：agentops#1 README 入门页（文档类演练，$1）→ #2 看板「数据新鲜度」去写死 → #3 pre-commit 在途会话守卫 → #4 ao resume 第二轮（P1，$4）；quant-lab#113 测试夹具默认隔离 config/。合入 #3 后要对两个仓库重跑 `ao repo install-hooks`。

## 12. 导出公开快照

公开版从当前提交导出干净快照，不带提交历史。`.gitattributes` 用 `export-ignore` 排除了内部记录（`docs/decisions.md`、`docs/execution-report.md`、`docs/proposals/`），它们留在本机仓库里照常维护。`git archive` 读的是已提交的 `.gitattributes`，所以先提交再导出。

```
OUT=$(mktemp -d)                          # 必须是空目录
git archive HEAD | tar -x -C "$OUT"
ls -R "$OUT/docs"                         # 确认没有 decisions.md、execution-report.md、proposals/
git -C "$OUT" init -b main
git -C "$OUT" add -A
git -C "$OUT" config user.name  "<公开用的名字>"    # 占位符，由人填
git -C "$OUT" config user.email "<公开用的邮箱>"    # 占位符，由人填
git -C "$OUT" commit -m "chore: 公开快照"
```

提交前显式设置作者身份的原因：不设的话 git 会用本机用户名和主机名当作者，公开后会随提交带出去。

公开版里 `ao review` 读不到 `docs/decisions.md` 时不报错，prompt 的“4.3 决策记录”一节写“（本仓库没有决策记录）”。推送到 GitHub 由人操作，会话和流水线不推送。

### 之后每次同步

首次上传之后，每次想更新公开版，在源仓库提交完改动后运行：

```
ao export --to <公开仓库的本机副本> [--from <源仓库>]    # --from 默认当前目录
```

命令只准备，不提交、不推送、不联网。它先检查源和目标都是干净的 git 仓库且互不包含，再用 `git archive HEAD` 取导出内容（遵守 `export-ignore`），扫描 `[review] redact` 里的词、本机家目录路径和用户名：有命中就拒绝，只列文件和行号，目标不被改动。

同步只动目标里 git 跟踪的文件：导出里有而目标没跟踪的是新增，内容或可执行位不同的是修改，目标跟踪而导出里没有的是删除。目标里被 `.gitignore` 忽略的文件（如 `.venv/`）原样保留，也不出现在变化摘要里。如果要新增的路径与目标里被忽略的文件同名，命令拒绝并列出这些路径，不改任何文件，由人处理。没有变化时输出“公开版已是最新”。

之后由人在目标副本里看差异、设置提交身份并提交推送（命令结束时会打印这些步骤）：

```
git -C <副本> diff
git -C <副本> config user.name  "<公开用的名字>"
git -C <副本> config user.email "<公开用的邮箱>"
git -C <副本> add -A
git -C <副本> commit -m "<type>: <说明>"
git -C <副本> push
```
