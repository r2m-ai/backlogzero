# AI 研发流水线执行计划（MacBook Pro 版）

版本 1.3，2026-10-18。执行者：MacBook Pro 上的 Claude Code。
1.1 的改动：EDGAR 改为你手动下载、程序离线入库，用 yfinance 的申报列表做自动补充；全程不向任何外部服务提交个人信息。
1.2 的改动：明确目录放在家目录；阶段 2 用占位的 capture 和 nightly，阶段 5 只验证机制；工作项 8 改由夜间任务派发（新增 `trigger` 字段）。
1.3 的改动：
- 新增第 6A 节“评审节点”：用 Codex 按 审核人 的 execute-review-improve 流程做只读评审；
- 阶段 1 增加 `ao review` 命令；
- C6 写明“用 Bash 改测试”的第二道防线，阶段 3 加对应场景；
- C10 和阶段 5 增加审批接口防伪造的要求；
- 开场指令加第 7 条；新增完成标准 D13；第 3 节加安装 Codex 的步骤。

## 0. 一页摘要

**目标**：在 MacBook Pro 上参照公开分享的做法搭一套 AI 研发流水线，并跑通全部组件。业务内容用一个精简的量化仓库 `quant-lab`，题材参照公开帖子里提到的量化研究项目，代码和数据均为自建，只用来给流水线派活：
- 用免费日线数据（yfinance）和你手动下载的 SEC EDGAR 目录文件入库到 DuckDB。EDGAR 部分完全离线，另用 yfinance 的申报列表做自动补充；
- 用 dbt 做数据测试；
- 做一项“成交额第一”回测。

**流水线组件**（全部做，每个用最简形态）：

| # | 组件 | 参考做法 |
|---|---|---|
| C1 | 本地工作项，带规格就绪闸门 | GitHub issue 编号的工作项 |
| C2 | 一项一会话 + worktree + 全局编号 | `#622-Me` 式会话、`fix/215-…` 分支 |
| C3 | 合并队列（确定性部分） | merge queue、`just test-all` |
| C4 | steward 判断层（LLM，只在冲突、规则触发、测试失败时调用） | Merge steward |
| C5 | 重任务槽位，机器级，跨仓库共享 | test 2/2 |
| C6 | 护栏：hooks、沙箱、测试只读、审批外置 | AGENTS.md 规则、用 hook 主动汇报 |
| C7 | 事件日志和 KPI | 人类介入率、冲突率 |
| C8 | PM Agent：排优先级、派活、依赖完成后自动启动下游 | PM Agent |
| C9 | 夜间任务和定时采集（launchd） | nightly daily、capture、`/goal` |
| C10 | 看板（本地网页 127.0.0.1:8749） | Steward 看板、运维看板 |
| C11 | 研究卫生：试验登记、留出数据、placebo | placebo、坏价掩码 |

**不在本次范围**：真实交易、paper trading、Massive 付费数据、GitHub 同步、Codex、多机，以及程序自动访问 sec.gov。这些在第 10 节列为以后再加。

**隐私**：全程不向任何外部服务提交姓名、邮箱等个人信息。联网的只有 yfinance（不登录）和下载软件（Homebrew、uv），EDGAR 文件由你用浏览器手动下载。

**投入（估算）**：
- 工时约 27–55 小时。按每周 15–25 小时，日历上约 2–4 周，含一次完整的夜间运行。
- 搭建阶段约 20–40 个 Claude Code 会话；演示阶段 8 个工作项用无人值守会话，每个带预算上限。分两到三周做，订阅额度应该够用。Anthropic 不公布订阅额度，这一条是估算。

**交付方式**：按阶段执行。需要评审的阶段先由 Codex 只读评审、执行者逐条处置（第 6A 节），然后停下来汇报，审核人 说“继续”再进入下一阶段。全部完成后写一份《执行结果报告》，放在 `~/agentops/docs/execution-report.md`。

---

## 1. 给 Mac 上 Claude Code 的开场指令

把本文件放到 Mac 的 `~/agentops-plan.md`，在终端里 `cd ~` 打开 Claude Code，贴下面这段：

```
请执行 ~/agentops-plan.md 这份《AI 研发流水线执行计划》。规则：
1. 按第 6 节的阶段顺序做。每个阶段结束时停下，列出交付物、验收命令和结果，等我说“继续”。
2. 凡是需要密码、token、系统设置、后台登录项许可的步骤，写清命令让我自己执行，不要代填，也不要把凭据写进任何文件、日志或对话。
3. 先用 `claude --help`、`claude --version` 和官方文档（code.claude.com/docs）核对计划里的 CLI 参数和 settings 键名。不一致就以实际为准，记录到 ~/agentops/docs/claude-cli.md。
4. 计划里标了“验证”的地方，必须真的跑一遍并贴出输出。
5. 不确定时先问我，不要自行扩大范围。
6. 不要向任何外部服务提交我的姓名、邮箱等个人信息；不要写任何自动访问 sec.gov 的代码。EDGAR 只读我手动下载到 ~/data/quant-lab/edgar_manual/ 的文件。
7. 需要评审的阶段（见第 6A 节），在向我汇报之前先运行 `ao review`，按 execute-review-improve 流程逐条处置审稿意见；汇报时附上评审记录和处置表。
```

---

## 2. 目录与总体结构

```
~/agentops/                    流水线套件，独立 git 仓库，以后给其他项目复用
├─ pyproject.toml              Python 包，提供 `ao` 命令（uv 管理）
├─ src/agentops/               workitems / sessions / queue / steward / slots / events / pm / board / nightly
├─ hooks/                      给项目用的 hook 脚本（protect_tests、register_session、stop_report）
├─ templates/                  AGENTS.md、.pipeline.toml、justfile、issue 规格模板
├─ launchd/                    4 个 plist 模板（steward、board、capture、nightly）
├─ tests/                      套件自测，用临时 git 仓库做夹具
└─ docs/                       claude-cli.md、runbook.md、execution-report.md

~/.agentops/                   运行状态。agent 会话在沙箱里不可写，部分不可读
├─ workitems/quant-lab/NNN.md  工作项
├─ queue/quant-lab.jsonl       合并队列
├─ approvals/                  审批记录，只有人类通道能写
├─ events.jsonl                事件日志，KPI 由它计算
├─ sessions.jsonl              会话登记
├─ trials.jsonl                试验登记，只能追加
├─ slots/                      重任务槽位的锁文件
└─ config.toml                 并发数、每日预算、免打扰时段、模型选择

~/work/quant-lab/              业务仓库（git），main 只能由 steward 合入
~/work/.wt/quant-lab/<id>-<slug>/   每个工作项的 worktree
~/work/.wt/quant-lab/_integration/  steward 专用的集成 worktree
~/data/quant-lab/lake/         Parquet 数据湖，只有采集任务和夜间任务写入
~/data/quant-lab/holdout/      留出数据（2025-01-01 之后），agent 会话不可读
~/data/quant-lab/edgar_manual/ 你手动下载的 EDGAR 目录文件，程序只读
~/data/quant-lab/warehouse.duckdb   仓库库，单写者：只有 nightly 写
```

**位置要求**：以上目录都直接放在家目录下，不要放进 `~/Documents`、`~/Desktop` 或它们的子目录。原因有两个：
- 这两个文件夹如果开了 iCloud 同步，会去同步 git worktree 和 DuckDB 文件，容易出问题；
- macOS 对这两个文件夹有隐私保护，launchd 的后台任务访问时常报“Operation not permitted”。

Claude Code 的会话工作目录可以在别处，把这几个目录加进会话即可。

**设计原则**（来自前几轮评估）：
- 确定性的部分先做，LLM 只做判断；
- steward 只裁决、不写代码，有冲突就退回原会话；
- 测试结果只认队列自己跑出来的；
- 审批只认人类通道。

---

## 3. 环境准备

**审核人 亲自做**（含密码和凭据）：
1. 安装 Homebrew：按 brew.sh 官方命令执行，需要输入管理员密码。
2. 生成一年期 token：运行 `claude setup-token`，然后把 token 存进钥匙串：
   `security add-generic-password -s agentops-claude -a "$USER" -w`
   命令会提示你输入 token，不要把 token 写在命令行或文件里。
3. 用浏览器手动下载 EDGAR 目录文件，放到 `~/data/quant-lab/edgar_manual/`（阶段 0 由 Claude Code 先建好这个文件夹）。在浏览器里打开下面几个目录页，点文件名下载，不需要填任何信息，和普通浏览网页一样：
   - 季度目录：`https://www.sec.gov/Archives/edgar/full-index/2026/QTR2/` 和 `https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/` 里各一份 `form.idx`；
   - 季度交界附近的日目录：`https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/` 里 9 月 29 日、9 月 30 日两天的 form 文件，以及 `https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/` 里 10 月 1 日的那份。

   文件名以目录页上实际显示的为准。一共几个小文件。以后想更新，每季度手动下一次就够，不需要每天下。
4. 第 5 阶段装 launchd 任务时，macOS 会弹出“已添加后台项目”的通知。这是告知，不需要操作；确认那 4 个以 `com.agentops.` 开头的任务没有在 系统设置 → 通用 → 登录项与扩展 里被关掉即可。
5. 夜间保持插电、不合盖。脚本用 `caffeinate -i` 防止空闲休眠，不改系统电源设置。
6. 确认 Mac 上有 Codex 命令行版，并且已经用你的 ChatGPT 账号登录：终端里 `codex --version` 能显示版本即可。没有的话，按 OpenAI 官方文档安装，登录由你本人完成。另外，把 `execute-review-improve.md`（和本计划一起发给你的那份文件）放到 `~/agentops/docs/process/execute-review-improve.md`，阶段 1 建好目录后放进去就行。评审时代码会发给 OpenAI，就像执行时会发给 Anthropic 一样；仓库按 D12 不含个人信息。
7. （可选）减少匿名上报：`brew analytics off` 关掉 Homebrew 的匿名统计；Claude Code 也有关闭非必要上报的环境变量，让 Claude Code 先查官方文档确认变量名再设置。

**Claude Code 做**：
- `brew install git just uv jq duckdb`
- `uv python install 3.12`
- 记录各工具版本，确认 `claude --version` 不低于 2.1.259（要用到 `--permission-prompts none`）。
- 用 `claude --help` 核对：`--session-id`、`--name`、`-p`、`--output-format json`、`--permission-mode auto`、`--permission-prompts none`、`--max-budget-usd`、`--resume`、`--model`、`--settings`。

**风险提示**：官方说 `--bare` 将来会成为 `-p` 的默认模式，而 bare 模式不读订阅 token。脚本里不要加 `--bare`；Claude Code 升级后先跑一次 `ao doctor`。

---

## 4. 流水线组件规格

### C1 工作项

- **存储**：每个工作项一个文件 `~/.agentops/workitems/<repo>/<NNN>.md`，YAML front matter 加正文规格：

```yaml
---
id: 4
repo: quant-lab
title: 成交额第一回测
type: feat            # feat | fix | chore
module: research      # 同一模块同一时刻只能有一个会话
priority: P1          # P0–P3，艾森豪威尔矩阵
status: ready
depends_on: [2]
trigger: auto         # auto：依赖完成后自动派发；nightly：只由夜间任务派发
acceptance:           # 为空就不能进入 ready，这是规格就绪闸门
  - tests/research/test_top_turnover.py 覆盖月度调仓与 ETF 排除
  - just backtest top_turnover 产出 reports/top_turnover.md
session_id: null
branch: null
---
（背景、规格、约束）
```

- **状态流转**：backlog → ready → in_progress → queued → testing → landed，中途可能分流到 returned、needs_human 或 dropped。
- **命令**：`ao wi new | list | show | set | ready`。全局编号显示为 `quant-lab#4`。

### C2 一项一会话

`ao start quant-lab#4 [--headless --budget 3 --model sonnet]` 依次执行：
1. 检查依赖是否都已 landed；检查模块锁，同模块已有会话就拒绝。
2. 从 main 建 worktree `~/work/.wt/quant-lab/004-top-turnover`，分支 `feat/4-top-turnover`。
3. 生成 UUID，用 `--session-id <uuid> --name "quant-lab#4 research 成交额第一回测"` 启动 Claude Code。初始提示词包含工作项全文和 AGENTS.md 要点，并要求做完后执行 `ao land`。
   - 交互模式：在 Terminal 新窗口里打开会话。
   - 无人值守模式：`-p` 加 `--permission-mode auto --permission-prompts none --max-budget-usd <N> --output-format json`，把 `total_cost_usd` 写进事件。
4. 写 `session_start` 事件，更新工作项状态。

### C3 合并队列（steward 的确定性部分）

- 会话里执行 `ao land`：把 `{repo, item, branch, sha}` 追加到队列，写 `land_requested` 事件。
- `ao steward --loop` 作为常驻进程（launchd KeepAlive），每 20 秒按先进先出处理一项：
  1. **SHA 检查**：分支头和入队时不一致，说明入队后又有新提交，把这一项移到队尾重新排队，写 `requeued` 事件。这是参考做法里的规则。
  2. **集成**：取仓库锁，在 `_integration` worktree 里把分支 rebase 到 main。
     - 有冲突：abort，写 `rebase_conflict`，交给 C4 判断，工作项转为 returned。
  3. **测试**：取重任务槽位，执行 `just test-all`，带超时。
     - 失败：写 `tests_fail`，交给 C4 判断，转为 returned。
  4. **策略检查**：
     - 读 `.pipeline.toml`：`autonomy = "auto" | "approve"`。
     - 规则触发：删除 `src/` 下的文件；删掉一个公开函数（diff 里有 `-def ` 而没有对应的 `+def `）；改动受保护路径（AGENTS.md、.pipeline.toml、.claude/、justfile、tests/ 下已有的文件）。
     - 触发了规则又没有对应 `(item, sha)` 的审批记录：转为 needs_human，写事件，发通知。
  5. **合入**：`main` 快进到 rebase 后的提交，写 `landed`，清理 worktree，把下游工作项标为可派发。
- 只有 steward 能合入 main。项目里装一个 `pre-commit`/`pre-merge-commit` hook，拒绝在 main 上直接提交；steward 合入时带特定环境变量放行。

### C4 steward 判断层（LLM）

- 只在三种情况下调用：rebase 冲突、规则触发、测试失败。
- 每次都是无状态的单次调用：`claude -p --model sonnet --max-budget-usd 0.5 --output-format json`。输入只有冲突片段或 diff 摘要、相关工作项、测试输出尾部。
- 输出 JSON，例如 `{action: "return_to_session" | "needs_human" | "retry_flaky", reason, hint}`。
- 它从不自己改代码。退回时把 reason 和 hint 写进工作项。如果原会话是无人值守模式，就用 `claude --resume <session-id> -p "<steward 的说明>" ...` 让原会话重做，最多 2 轮；超过就转 needs_human。
- 会话间消息（cross-session messaging）是研究预览功能，可选。默认用“写进工作项 + resume”的方式，更稳定。

### C5 重任务槽位

- `ao slot test -- just test-all`：用 Python `fcntl.flock` 在 `~/.agentops/slots/test.{1..N}.lock` 上做 N 槽信号量，N 默认 2。拿不到槽就等待，并写 `slot_wait` 事件。
- 机器级共享，以后接入第二个仓库时同样生效。不用 Homebrew 的 flock，以保证 macOS 和 Linux 都能用。

### C6 护栏

项目里的 `.claude/settings.json`。键名以官方文档为准，配置完要逐条验证：
- **PreToolUse hook**（匹配 Edit、Write 等写操作）：执行 `protect_tests`，规则如下：
  - 允许新增测试文件；
  - 拒绝修改 `tests/` 和 `fixtures/` 下 main 上已存在的文件；
  - 拒绝修改 AGENTS.md、.pipeline.toml、.claude/；
  - 拒绝时用退出码 2，并把原因输出到 stderr；同时写 `hook_block` 事件。
- **SessionStart hook**：登记会话（session_id、工作项、分支、worktree）。
- **Stop hook**：写 `session_stop` 事件，提醒执行 `ao land`。不在这里跑全量测试，测试结果只认队列。
- **第二道防线**：PreToolUse hook 只能拦 Edit/Write 这类工具，agent 用 Bash 写文件（比如 `sed -i`、重定向）就能绕过。所以 steward 合并时还要再检查一遍：diff 里只要改动或删除了 main 上已有的测试和夹具文件，就算触发规则，转 needs_human（见 C3 第 4 步）。两道防线都要有测试覆盖。
- **沙箱**：开启 macOS 沙箱，写权限只给当前 worktree；`~/data/quant-lab/holdout/` 不可读，`~/.agentops/approvals/` 不可写；默认禁止出网，依赖在阶段 2 由人预先装好。
- **审批**：`ao approve` 只接受来自看板按钮或人在终端执行，记录写到 `~/.agentops/approvals/`。检测到在 Claude Code 会话内执行（找一个可识别的环境变量，先验证有没有）就拒绝。不接受“对任意会话说批准”这种方式，这是对参考做法的改进。

### C7 事件日志与 KPI

- **事件格式**：`events.jsonl` 每行一个事件，字段为 `{ts, repo, item, session, type, detail}`。
- **`ao kpi [--since 7d]` 输出的指标**：
  - 人类介入率 = needs_human 数 ÷ landed 数
  - 冲突率 = rebase_conflict 数 ÷ 入队处理次数
  - 返工率 = returned 数 ÷ land_requested 数
  - 回滚数
  - 每项成本：取自无人值守会话的 `total_cost_usd`，客户端估算
  - 平均排队时长
- **反向指标**：回滚数和返工率要和介入率放在一起看，防止为了压低介入率而少升级、少问人。

### C8 PM Agent

- `ao pm`：先做确定性计算（依赖是否就绪、KPI、规格就绪检查）。然后一次 `claude -p --model sonnet --max-budget-usd 1` 调用，读 backlog 摘要，给出 P0–P3 建议和下一批派发顺序，写到 `~/.agentops/pm-report.md`。
- 排序可以自动应用；把工作项设为 dropped（P3 砍掉）需要人在看板上确认。
- `ao pm --dispatch`：按优先级派发 ready 状态的工作项，并发不超过 `config.toml` 的 `max_sessions`（2–3），每项带预算，当日总预算到上限就停止。并发已满时，就绪的工作项排队，等有空位再派发。
- 某一项合入后，`trigger: auto` 的下游工作项自动派发；`trigger: nightly` 的工作项平时跳过，只在 `ao pm --dispatch --nightly` 时由 PM 提为 ready 并派发。

### C9 夜间任务与采集（launchd）

| 任务 | 时间（太平洋时间） | 内容 |
|---|---|---|
| `com.agentops.capture` | 每天 14:00（美股收盘后） | `caffeinate -i just capture`：拉 yfinance 日线；每周一顺带拉一次股票池的 `sec_filings`。EDGAR 手动目录不在定时任务里 |
| `com.agentops.nightly` | 每天 01:30 | 依次执行：`just nightly`（dbt build、数据新鲜度）→ `ao pm` → `ao pm --dispatch --nightly`（最多 2 项，带预算）→ 生成每日一页简报 |
| `com.agentops.steward` | 常驻 | `ao steward --loop` |
| `com.agentops.board` | 常驻 | `ao board`，只监听 127.0.0.1:8749 |

- **认证**：夜间任务从钥匙串读 token，设为 `CLAUDE_CODE_OAUTH_TOKEN` 环境变量。不落盘、不进日志。
- **错过补跑**：launchd 的定时任务如果在休眠时错过，唤醒后会补跑一次。
- **免打扰**：22:00–08:00 不弹通知，只记事件。

### C10 看板（127.0.0.1:8749）

一个页面，每 30 秒自动刷新，用 Python 标准库或 FastAPI 实现，不要做复杂。面板如下：
1. 需要你看：needs_human、多次被退回、待确认的 dropped
2. 工作项：按状态分列，标出依赖和“被谁挡住”
3. 正在跑的会话
4. 各仓库的合并队列
5. 重任务槽位占用
6. 今天的计划和实际运行（来自事件日志）
7. dbt 结果
8. 数据新鲜度：yfinance 日线和申报列表的最新日期；EDGAR 手动目录最近一次放入的日期，超过一个季度就提醒你手动更新
9. 磁盘
10. KPI（7 天）

“批准”和“确认放弃”按钮通过 POST 请求写审批记录，这是唯一的人类审批通道。

**防伪造**：你浏览器里打开的任意网页，都可能偷偷向 127.0.0.1:8749 发请求，冒充你点“批准”。所以审批接口要做到：
- 只监听 127.0.0.1；
- 页面里嵌一个随机令牌，每次启动重新生成，POST 时必须带上；
- 检查请求的 Origin 和 Host 必须是 `http://127.0.0.1:8749`，否则拒绝；
- 审批记录绑定 `(工作项, SHA)`：批准之后如果分支又有新提交，旧的批准作废。

### C11 研究卫生

- **试验登记**：所有回测都通过 `just backtest <strategy>` 跑，先在 `trials.jsonl` 登记（策略、参数、数据区间、随机种子、git sha），再出结果。这个文件只能追加，agent 会话不可写。
- **留出数据**：2025-01-01 之后的数据放在 holdout 目录，agent 会话读不到。只能由人执行 `ao holdout-eval <strategy>`，只输出汇总指标。
- **可复现**：justfile 里固定 `PYTHONHASHSEED=0`；抽样前先排序。

---

## 5. 业务仓库 `quant-lab`（精简版）

- **股票池**：`config/universe.csv` 放约 60 只美股大盘股，横跨各行业，用 SPY 做基准，每只标明是否为 ETF。已知存在幸存者偏差：用的是今天还在的股票。
- **采集**（`just capture`）：
  - 用 yfinance 拉 2005 年至今的日线（未复权收盘价、复权收盘价、成交量），按年分区写 Parquet，可以重复运行而不重复写入。
  - 每周一用 yfinance 的 `Ticker.sec_filings` 拉股票池里每只股票的近期申报列表（文件类型、日期、链接），作为 EDGAR 的自动补充。能回溯多少年，在阶段 2 实测并记进 runbook。
  - yfinance 的条款只允许个人研究，这里只做研究用途。
- **EDGAR（离线）**：`just load-edgar` 读取 `~/data/quant-lab/edgar_manual/` 里你手动下载的目录文件，解析成表，字段为公司名、文件类型、CIK、申报日期（YYYY-MM-DD）、文件路径。不联网，也没有任何访问 sec.gov 的代码。
- **dbt-duckdb**：
  - 模型：`stg_prices`、`fct_stocks_daily`（成交额 = 未复权收盘价 × 成交量）、`fct_edgar_filings`（来自手动目录）、`fct_yf_filings`（来自 yfinance）。
  - 测试：
    - (ticker, date) 唯一、非空、价格大于 0；
    - 一个 warn 级的“单日变动超过 45% 且无已知拆股”测试；
    - 数据新鲜度测试；
    - 季度边界测试：9 月 30 日的申报必须出现在第三季度的数据里，不能被漏掉。
- **策略**：成交额第一。
  - 规则：每月月末，在非 ETF 股票里选过去 21 个交易日成交额最高的一只，持有一个月。
  - 用 vectorbt 或 pandas 实现，和 SPY 对比 CAGR、波动、Sharpe、最大回撤，输出 `reports/top_turnover.md`。
  - 只用 2024 年及以前的数据。
  - 和参考帖子里公开的 19.01% 对不上属正常（股票池和数据源都不同），报告里写明差异原因。
- **placebo**：在成交额前 10 名里随机选一只，固定 200 个种子，看原策略落在随机分布的哪个位置。
- **justfile**：`capture`、`build`、`test-fast`、`test-all`（ruff + pytest + 在夹具库上跑 dbt build，目标 3 分钟以内，全程不联网）、`backtest`、`nightly`。
- **AGENTS.md**：
  - 每个模块同一时刻只开一个会话；
  - 删功能或删公开函数要人批准；
  - 测试只能新增、不能修改已有的；
  - 做完执行 `ao land`；
  - 不改 justfile 和配置。
- **`.pipeline.toml`**：`autonomy = "auto"`，与参考做法里的量化仓库一致；受保护路径列表；`test_cmd = "just test-all"`；`max_sessions = 2`。

---

## 6. 执行阶段

每个阶段结束都停下来汇报，等 审核人 说“继续”。工时是估算，包含 审核人 审阅的时间。

| 阶段 | 工时 | 交付物 | 验收（必须实际运行） |
|---|---|---|---|
| 0 环境 | 0.5–1 h | 第 3 节的工具；`docs/claude-cli.md` 记录实际参数 | `just --version`、`uv --version`、`duckdb --version`、`claude --version` 都有输出；钥匙串里能读到 token，只验证存在，不打印 |
| 1 套件骨架 | 5–10 h | `ao` 的 workitems、events、slots、sessions；`~/.agentops/` 初始化；`ao doctor`；`ao review --phase N`（见第 6A 节） | 套件自测通过；起 3 个进程抢 2 个槽，必有 1 个等待并记 `slot_wait`；工作项状态流转测试覆盖规格就绪闸门 |
| 2 业务仓库和判定器 | 3–6 h | `quant-lab` 骨架：AGENTS.md、`.pipeline.toml`、justfile、夹具、空实现的 `test-all`；hooks 和沙箱配置；main 保护。`just capture` 和 `just nightly` 先做成占位：不做实际工作，正常退出，并写 `capture_noop`、`nightly_start`/`nightly_end` 事件。真正的采集和 dbt 由阶段 6 的工作项 1、2 走流水线完成 | `just test-all` 少于 3 分钟且通过；把模拟的 hook 输入 JSON 喂给 `protect_tests`，修改已有测试被拒（退出码 2），新增测试被放行；会话里读 holdout 失败；在 main 上直接提交被拒 |
| 3 合并队列和 steward（确定性部分） | 4–10 h | `ao land`、`ao steward --once/--loop`；集成 worktree；策略检查；launchd 常驻 | 在临时仓库夹具上逐一自测 7 个场景：干净合入、rebase 冲突退回、测试失败退回、受保护路径转 needs_human、有审批后合入、入队后新提交重新排队，以及用 Bash 改已有测试后被 steward 转为 needs_human |
| 4 会话、判断层和 PM | 6–12 h | `ao start`（交互和无人值守两种）；C4 判断层；`ao pm` 和 `--dispatch`；`ao kpi` | 无人值守启动一个演示工作项，走完 start → land → landed；人为制造冲突，判断层输出合法 JSON 并退回原会话；PM 报告生成；KPI 能算出来 |
| 5 看板和定时任务 | 5–10 h | `ao board`；4 个 launchd 任务；每日简报 | 只验证机制：`launchctl kickstart` 手动触发 capture 和 nightly（此时是占位）各一次，正常退出、事件写入、认证正常；浏览器打开 127.0.0.1:8749，10 个面板都能正常显示，数据面板显示“暂无数据”也算通过，但不能报错；用看板按钮完成一次审批（可以用临时测试工作项）；用 `curl` 模拟别的网页发审批请求（不带令牌，或 Origin 不对），必须被拒 |
| 6 演示验收 | 4–8 h，日历上 1–2 天，含一晚 | 第 7 节的 8 个工作项全部跑完；`docs/execution-report.md`；结束时截看板图（D8） | 第 8 节的完成标准逐条打勾 |

合计约 28–57 小时（估算），另加评审和处置每个阶段约 20–40 分钟。搭建阶段在普通的 Claude Code 会话里做，因为流水线自己还没建好；从阶段 6 开始，业务改动全部走流水线。可选：阶段 4 之后，套件自身的改动也可以走流水线，自己验证自己。

---

## 6A. 评审节点（Codex，按 execute-review-improve）

用 审核人 的 execute-review-improve 流程（v0.2.1，文件在 `~/agentops/docs/process/execute-review-improve.md`）。Claude Code 是执行者，Codex 是只读审稿人。每个需要评审的阶段走一轮：执行 → 一次评审 → 一次修正，不反复循环。

**哪些阶段评审**：

| 阶段 | 评审方式 | 重点 |
|---|---|---|
| 0 环境 | 不审 | — |
| 1 套件骨架 | 轻审：只问一个问题 | 重任务槽位的锁，在并发和进程崩溃时是否正确 |
| 2 业务仓库和护栏 | 必审 | hook 和沙箱能否被绕过、main 保护、留出数据隔离 |
| 3 合并队列和 steward | 必审，最重要 | 竞态、重新排队、审批和 SHA 绑定、失败后能否恢复、Bash 改测试能否被第二道防线挡住 |
| 4 会话、判断层和 PM | 必审 | 预算上限是否真生效、resume 会不会死循环、提示词注入的途径 |
| 5 看板和定时任务 | 轻审：只看审批接口 | 网页伪造请求、只监听本机、令牌 |
| 6 演示验收 | 必审，用报告评审模式 | 执行报告里 D1–D13 的每条结论，是否都有事件日志或截图作证据 |

**`ao review --phase N` 做的事**（参考 审核人 的 `info_verify/scripts/review_codex.py`）：
1. 先由脚本跑测试，保存输出。Codex 在只读沙箱里跑不了 pytest，所以只给它看结果。
2. 组装 prompt，内容按顺序为：
   - 流程文件正文；
   - 审稿人启动语；
   - 本计划全文，作为项目说明；
   - 审核人 已确认的决定：第 0 节的版本改动说明，以及开场指令第 6 条隐私规则；
   - 本阶段的验收条件（第 6 节对应那一行）和相关的 D 条；
   - 材料包：执行者写的本阶段小结，包括做了什么、执行过的命令和输出、与计划不一致的地方（`docs/claude-cli.md`）；
   - 本阶段的代码改动：每个阶段开始时打一个 git 标签，取从标签到现在的 diff；
   - 测试输出。
3. 运行 `codex exec -s read-only -C <仓库> --skip-git-repo-check -o <输出文件> -`，把 prompt 从标准输入传进去。具体参数以 Mac 上 `codex exec --help` 的实际输出为准。
4. 评审结果存到 `AGENTOPS_HOME/reviews/phase-N.md`。
5. 执行者在同一文件里逐条写处置：采纳（改了哪里）、拒绝（附依据）或待查证。修正完再跑一次测试。

**评判标准不由 Codex 生成**：唯一的依据是本计划、审核人 已确认的决定和本阶段的验收条件。审稿人只输出 skill 规定的四项：核对结果、最大失误、反馈解释、至多一条候选改动。候选改动如果涉及改计划本身，不在执行中途生效，带回给 审核人 确认后出新版计划。

**效果记录**：每次评审记下意见数、采纳数，以及其中有几条是真 bug。阶段 2、3 审完后看一下：如果平均每次评审连一条真问题都找不到，就缩减为只审阶段 3 和 6。

---

## 7. 演示工作项

| # | 标题 | 模块 | 参考做法 | 依赖 | 刻意安排的场景 |
|---|---|---|---|---|---|
| 1 | 采集：yfinance 日线写 Parquet，可重复运行 | data | capture 任务 | — | — |
| 2 | dbt 模型和数据测试（含单日大幅变动告警） | dbt | dbt 209 节点、拆股告警 | 1 | 依赖完成后自动启动 |
| 3 | 离线 EDGAR 目录入库（读手动下载的文件）+ 季度边界测试；接入 yfinance 申报列表作自动补充 | edgar | #220 | — | 与 1 并行；测试只用仓库里的小样本夹具，不依赖网络 |
| 4 | 成交额第一回测 | research | 成交额第一研究 | 2 | 与 5 改同一个 `research/metrics.py`，制造冲突 |
| 5 | placebo 框架（固定种子、先排序再抽样） | research2 | #221 | 2 | 冲突场景：后到者被退回，重做后合入 |
| 6 | 坏价掩码 + 开盘价成交规则 | data | #219 | 1 | 与 3 并行，验证重任务槽位排队 |
| 7 | 删除旧的 CSV 导出命令 | data | AGENTS.md：删功能要人批 | 1 | 审批场景：转 needs_human，在看板批准后合入 |
| 8 | 每日一页简报 | reports | nightly | 2 | 夜间场景：`trigger: nightly`，开始时放在 backlog，不跟着依赖自动派发；01:30 由 PM 提为 ready 并派发，无人值守完成 |

4 和 5 刻意分在两个模块，这样都能拿到会话；它们改同一个文件，在合并时才会冲突。另外还有三个脚本化场景，不依赖 agent 配合：
- 对 `protect_tests` 发一次模拟的修改请求，确认被拦；
- 入队后往分支追加一个提交，确认重新排队；
- 同时提交 3 个合并请求，确认槽位排队。

**预算**：无人值守会话默认 `--model sonnet --max-budget-usd 3`，判断层每次 0.5、PM 每次 1，当日总预算 15。这些是客户端估算的美元数，实际从订阅额度里扣。如果当周额度用得太快，就把并发降到 1。

---

## 8. 完成标准

| ID | 标准 | 怎么验证 |
|---|---|---|
| D1 | 8 个工作项里至少 7 个经队列合入，没有一次绕过队列 | `git log main` 的每个合入提交都能对应到 `landed` 事件；main 保护 hook 有拦截记录 |
| D2 | 至少发生一次冲突，被退回后重做并合入 | 事件序列：rebase_conflict → returned → land_requested → landed |
| D3 | 至少一次规则触发的审批，经看板批准后合入 | needs_human → approved（来源为看板）→ landed |
| D4 | 修改已有测试被拦 | `hook_block` 事件，以及脚本化场景的输出 |
| D5 | 重新排队和槽位排队都发生过 | `requeued` 事件和 `slot_wait` 事件 |
| D6 | 依赖完成后下游自动派发 | 2 合入后，4、5 自动派发；并发已满时先排队、有空位后进入 in_progress，也算通过。8 不参与，见 D7 |
| D7 | 一次完整的无人值守夜间运行 | 01:30 的 nightly：dbt build 通过 → PM 报告 → 把工作项 8 提为 ready 并派发 → 8 经队列合入 → 生成每日简报，全程没有超预算 |
| D8 | 看板 10 个面板都显示真实数据 | 阶段 6 结束时截图，存进执行报告 |
| D9 | KPI 报告 | `ao kpi --since 7d` 输出介入率、冲突率、返工率、回滚数、每项成本 |
| D10 | 研究卫生 | 回测前都在 `trials.jsonl` 登记；会话读不到 holdout；placebo 两次运行结果一致 |
| D11 | 可移植性 | 套件自测在一个新的临时仓库上通过；`templates/` 能在 10 分钟内接入一个空项目 |
| D12 | 隐私 | 两个仓库的代码、配置文件和 `~/.agentops/` 里搜不到邮箱地址；代码里没有访问 sec.gov 的地方；联网的只有 yfinance 和软件下载。git 提交记录里的作者邮箱只留在本机，不推送 |
| D13 | 独立评审 | 阶段 2、3、4、6 都有 `reviews/phase-N.md`（记录在运行状态目录，不随仓库发布），每条意见都有处置；采纳的意见能在代码或文档里找到对应改动 |

---

## 9. 风险和应对

| 风险 | 应对 |
|---|---|
| CLI 参数或 settings 键名和计划不一致 | 阶段 0 先核对，以实际为准并记录；`ao doctor` 每次升级后跑一遍 |
| launchd 环境下认证失败 | 只用钥匙串里的 token 加 `CLAUDE_CODE_OAUTH_TOKEN`；阶段 5 用 `launchctl kickstart` 手动验证 |
| 沙箱挡住了必要操作（uv 缓存、pytest 临时目录） | 依赖在会话外预装；沙箱放行项目 `.venv` 和系统临时目录；改了什么记进 runbook |
| 订阅周额度用完 | 并发 2；worker 用 Sonnet；每个会话和每天都有预算上限；搭建和演示分开在不同的日子做 |
| agent 走捷径（改测试、伪造完成） | 测试只读、结果只认队列、审批外置；执行报告里列出所有被拦截的事件 |
| yfinance 限流或接口变动 | 股票池保持小规模，失败重试，测试全用本地夹具；采集失败只影响数据新鲜度面板，不阻塞流水线 |
| yfinance 的 `sec_filings` 回溯年份不够 | 它只是补充；EDGAR 的核心数据来自手动目录 |
| 手动目录文件没放或放错 | 第 3 项的测试只用仓库里的小样本夹具，不受影响；`ao doctor` 检查文件夹并提示缺了哪些文件 |
| Mac 夜里休眠 | 插电、不合盖，配合 `caffeinate -i`；launchd 在唤醒后补跑 |
| 范围蔓延 | 第 0 节的“不在本次范围”清单；新想法记成 backlog 工作项，不在阶段内顺手做 |
| Codex 评审噪音多，或者和计划冲突 | 评判标准只用本计划和已确认的决定；和计划冲突的意见按“拒绝 + 依据”处理；按第 6A 节的效果记录决定是否缩减评审 |
| Mac 上没有 Codex 或没有登录 | 暂时跳过评审，在执行报告里注明；不影响流水线本身的验收 |

---

## 10. 以后再加（本次不做）

- GitHub 私有仓库同步和备份；用 Codex 做独立的第二审阅者。
- 评测集：从真实失败的工作项里挑 20–50 个金标任务，换模型或改规则前后各跑一次。
- OTel 成本追踪；语义冲突防线（契约测试、重叠检测）。
- 第二个仓库接入，验证跨仓库的槽位和看板。
- Massive 数据（先拿到许可的书面确认）、更大的股票池、paper trading。
- EDGAR 自动下载：SEC 要求请求头里填联系邮箱。到时可以用 iCloud+ 的“隐藏邮件地址”这类转发地址，不暴露真实邮箱。
- 接入你自己的期权规则和投资报表，作为新的工作项。

---

## 11. 执行结果报告模板

阶段 6 结束后写入 `~/agentops/docs/execution-report.md`，内容包括：
1. 实际环境和版本
2. 每个阶段的实际工时和问题
3. D1–D13 的勾选结果和证据（事件片段、截图）
4. 评审统计：每个阶段的意见数、采纳数、真 bug 数，以及是否缩减了评审
5. KPI 表
6. token 和预算消耗（客户端估算）
7. 计划与实际的偏差，以及原因
8. 已知问题
9. 下一步建议
