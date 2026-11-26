# /steam 多 Agent 协作

在 Claude Code Desktop 输入 `/steam 任务` 即可。Claude 在主聊天分工，把每个外部步骤交给一个内置子 Agent，由它运行本机已登录的 Codex 或 Antigravity CLI。你可以点开每个子 Agent 看它在做什么。主管等所有必需步骤交回结果后，在主聊天里统一答复。

Steam 默认只接受手动调用。Claude Code 使用 `/steam`，Codex 使用 `$steam`。Codex 可以检查环境、预览计划、查询结果，以及执行仅含 Codex 和 Antigravity 步骤的任务。包含 Claude 步骤的计划可以在 Claude Code 中执行或继续。

## 技能与运行环境

准备 Python 3.10 或更新版，以及准备使用的官方 CLI。默认的文档写作和界面动画设计在 Claude Code 会话中执行。需要脚本派发的固定型号 Claude 任务还需本机已登录的 Claude Code CLI（`claude`）。Codex 通过 `codex login` 选择 ChatGPT 账号，Antigravity 使用有相应权益的 Google 个人账号登录 `agy`。首次登录和项目授权在对应客户端完成。各 CLI 可通过当前会话的 PATH 或配置中的可执行文件绝对路径定位。Python 只用标准库。

解压后把整个 `steam` 文件夹放在以下位置。确认 `SKILL.md`、`config.json`、`scripts` 和 `agents` 是同级，避免多套一层 `steam`。Claude Code 从 `SKILL.md` 的文件头读取手动调用设置，Codex 从 `agents/openai.yaml` 读取。

| 环境 | 安装后的技能目录 |
| --- | --- |
| Windows 原生本地会话 | `%USERPROFILE%\.claude\skills\steam\` |
| macOS / Linux 本地会话 | `~/.claude/skills/steam/` |
| Windows 上的 WSL 会话 | 对应 Linux 发行版中的 `~/.claude/skills/steam/`，Python 和 CLI 也从该环境启动 |
| Codex | Windows 的 `%USERPROFILE%\.agents\skills\steam\`，macOS / Linux / WSL 的 `~/.agents/skills/steam/` |

个人技能目录可以直接链接到这份仓库，各项目共用同一个 `/steam`。新技能首次未出现在菜单中时，可新开本地 Code 会话。

## 使用

```text
/steam --check
/steam --plan 帮我做结算金币特效，并接入领奖按钮
/steam 分析并修复这个 Bug：连续点击领取两次会发两份奖励。复现日志在 logs/reward.txt
/steam 帮我给结算界面设计动画和体验，包含入场、可点击时刻和快速跳过
/steam 为结算界面生成一枚透明金币，输出到 assets/reward/coin.png
/steam review 当前 diff，重点看重复发奖风险，按严重度列出问题和验证建议
/steam 分析从领取按钮到发奖服务的调用链，标出关键文件和函数
/steam 根据这份接口说明更新 docs/rewards.md 中的接入示例
```

| 输入 | 结果 |
| --- | --- |
| `/steam --check` | 检查本地 CLI、可确认的登录状态及配置的模型和推理等级。目录缺少组合时返回 `attention`，在 `missing_models` 中列出差项。无法读取目录时也会提示。Claude 主管会运行一次。真实任务的权限和订阅剩余额度仍由各产品判定。 |
| `/steam --plan 任务` | 主管拆解，脚本校验步骤、依赖、额度和模型配置，在项目的 `.workbench/<task_id>/` 保存已校验的计划并展示将要派给谁。无需运行外部模型；稍后可用返回的 `/steam --resume` 命令直接执行同一计划。 |
| `/steam 任务` | 按路由分工。单步直接显示任务名和执行器，多步先列分工与依赖。展开任务可以看执行过程，结束后在当前聊天收到结论。 |
| `/steam --result 运行目录` | 查看各步骤的执行器、状态、最近的执行活动、已记录的起止时间及完成回执摘要。 |
| `/steam --resume 运行目录` | 执行已预览的计划，或从检查点继续。若预览后修改配置，先用新配置重新预览。已交给 Claude 原生会话的步骤会显示 `waiting_claude`，由原会话提交回执。中断时无法判定结果的外部调用会保留待核实记录。 |

也可以直接在项目目录的终端做环境检查，全程由本机脚本执行，不启动 Claude 主管。Windows PowerShell 示例：

```powershell
py -3 "$env:USERPROFILE\.claude\skills\steam\scripts\flow.py" check --workspace .
```

macOS、Linux 或 WSL 示例：

```bash
python3 ~/.claude/skills/steam/scripts/flow.py check --workspace .
```

### 在 Desktop 看子任务怎么执行

例如输入 `/steam 分析当前项目`，右栏可以出现「分析项目结构 · Codex」。这是普通的 Claude 子 Agent，标题标明实际执行工作的 CLI。在 **Background tasks** 点开条目，可以看到子 Agent 的简报、Bash 调用和工具输出。CLI 通过命令报告公开进展，显示时机会随 Desktop 的工具视图而异。[Claude Desktop 任务面板](https://code.claude.com/docs/en/desktop#watch-background-tasks)

主聊天报告关键发现和阻塞。只有所有必需步骤都已成功验收、结果已汇总交付，主管才报告全部完成。部分失败时推进不受影响的工作，核实已有产物后才能重试。用户停止的任务等待进一步指示。

Claude 主管和调用 CLI 的原生子 Agent 都会使用 Claude 额度。子 Agent 只启动指定步骤、等待并交回简短结果。业务推理使用该步骤选定的 Codex 或 Antigravity 模型，计入对应账号。

本次命令可附上相关日志、截图或参数的项目内路径。只要求界面动画设计时，`motion_ux` 交付时间线、可交互时刻和中断行为。同时要求接入工程时，`integration` 接续设计结果。

## 可选的路由与模型配置

默认分工可以直接使用。要定制路由、模型或步数上限时，编辑技能目录中的 **`config.json`**，所有项目共用这份配置。

| 配置项 | 管什么 |
| --- | --- |
| `agents.<名称>.enabled` | 该执行器是否参与派工。例如暂时停用 Antigravity 可改成 `false`。指向该执行器的任务需改派或重新启用。 |
| `agents.<名称>.models` | 允许该执行器用的模型，以及每个模型允许的推理等级。填写你账号实际可用且愿意使用的组合。 |
| `agents.<名称>.executable` | 本机 CLI 的命令名或原生可执行文件的绝对路径。Claude 的固定版本专项任务使用此项，主管由 Desktop 会话启动。 |
| `routes.<任务类型>` | 每类任务的默认执行器、模型和等级。主管可为单项工作一起指定目录内的 `agent`、`model`、`level`，并解释原因。能力相当时优先 Codex，再考虑 Antigravity 和 Claude。 |
| `limits.max_calls` / `max_steps` | 每项任务允许的派工步数。预检一次性检查整张计划。每个步骤内部可有多轮工具操作。这两项限制步数，订阅消耗取决于各产品的计量方式。 |
| `supervisor` | 主管自身的模型和等级。保持 `agent` 指向 Claude。 |

例如，默认代码审查由 `routes.review` 指定。

```json
"review": {
  "description": "代码审查，按严重度报告问题、位置和验证建议",
  "agent": "codex",
  "model": "gpt-6.2-astra",
  "level": "high"
}
```

任务路由、模型清单和派工上限在下次新任务生效。派发前会核对本机配置与保存的快照。配置变化后，先核对已交付和仍待处理的工作，再用新配置预览剩余步骤。默认整项最多 8 步，每个计划的 Codex、Antigravity、Claude 分别最多 5、2、2 步；这些上限不计量实际订阅余额。主管的派工和汇总另计。

**仅当更改主管模型或等级时**，在安装好的 `steam` 目录运行一次同步。Claude Code 在加载技能正文前从文件头读取这两个字段；脚本只同步这两个值。若忘记同步，执行器会提示。其他配置直接编辑 JSON 即可。

```powershell
py -3 scripts/sync_supervisor.py
```

```bash
python3 scripts/sync_supervisor.py
```

默认由 `opus / high` 主管拆分任务、派工和汇总结果。文档写作交给 `sonnet / medium` 原生子 Agent，界面动画设计交给 `opus / medium` 原生子 Agent。具体模型由 Claude Code 解析家族别名。缺少子 Agent 工具时，主管可直接处理与自身模型和档位相同的原生步骤。如果为专项任务指定了其他完整型号，脚本会通过本机 Claude Code CLI 传递该型号和档位。

Antigravity 按 `agy models` 返回的目录选择型号和档位。使用 Orchestrator 时，Carbon 对应 `gemini-pro-orchestrator`，Neon 对应 `gemini-pro-orchestrator-neon`。AGY 1.5.17 对这两个目录项使用产品默认档位，在 `agents` 和 `routes` 里填 `default`。需要固定档位时，选择目录里带相应档位的型号，例如 `gemini-4-neon` 配 `high` 会匹配 `gemini-4-neon-high`。`--check` 会列出当前目录缺少的组合。

图片生成和编辑由 `gpt-6.2-astra / high` 调用 Codex 内置生图工具。图片大小、用途与风格写在任务里，交付文件使用 PNG。

## 产物、记录与成本

工程改动和指定的交付文件写在本次项目目录。预览或直接执行时会创建项目内的 `.workbench/<task_id>/`，其中 `task_id` 使用唯一的 `steam-run-*` 名称。命令返回该路径，续跑沿用同一个目录。运行记录保存在本地，目录结构如下。

```text
.workbench/<task_id>/
├── run-state.json             # 计划、配置快照和步骤状态
├── run.lock                   # 保护计划和共享状态更新
└── steps/<步骤ID>/
    ├── step-input.json        # 步骤简报、执行器配置和上游依赖
    ├── step-result.json       # 完整结果和已验证的产物路径
    ├── progress.json          # 外部步骤最近的执行活动，运行中可读
    ├── progress.jsonl         # 外部步骤的按时间排列的近期事件
    ├── stdout.log             # 外部执行器标准输出
    ├── stderr.log             # 外部执行器标准错误
    └── claude-receipt.json    # Claude 原生步骤的原始 JSON 回执
```

保存计划时先创建运行状态和步骤目录。新运行按步骤单独领取；同一 ID 的并发调用跟踪已有工作。终端用 `continue --one-step --step "<ID>" --stream --run-dir "<目录>"` 可查看实时活动，stderr 输出进度，stdout 保留结果 JSON。`result --run-dir "<目录>" --wait 60` 分段等待状态或活动变化。`revise --run-dir "<目录>" --spec "<计划文件>"` 可替换尚未开始的分工；显式重试时在文件的 `retry` 中列出 ID，保留先前回执。旧运行继续使用保存的执行方式。

Claude Code 会将计划和回执的输入 JSON 写到项目的 `.workbench/steam-inputs/`。每份输入使用独立文件名，Shell 命令只传文件路径。使用 Git 的项目可在自己的 `.gitignore` 中加入 `/.workbench/steam-run-*/` 和 `/.workbench/steam-inputs/`。任务结束后可按需归档或删除这些记录。

如果领取 Claude 步骤的会话已经结束，可核对工程改动、产物或原会话记录。核实结果后，从 `run-state.json`（旧版运行使用 `state.json`）中取出该步骤的 `claim`，用 `flow.py complete --run-dir <目录> --step <ID> --claim <领取码> --reply <回执文件>` 补交 JSON 回执。回执包含 `status`（`success` 或 `failed`）及非空的 `summary`。领取码引入前已经发出的步骤沿用原命令，省略 `--claim`。仍无法判定结果时，保留待核实状态。

声明的目标文件会在调用前后计算 SHA-256；已有文件只有在内容更新后才计为本次产物。PNG 还会做格式和数据完整性检查。没有预先声明目标路径的代码修复，以执行器报告的改动和测试为准。图片观感、交互手感、真机性能仍需对应预览或实测。脚本记录工具状态和已获得的证据。对于 Codex 生图，记录中区分原生工具事件、本次原图匹配，以及只有有效产物与执行器自述的情况。

减少模型开销的地方是派工命令、依赖传递、结果归档与大段日志。继续使用各执行器自己的账号与配额。若只想使用套餐内含量，应在产品账号中核对是否已开启超额消费。

官方说明：[Claude Code 技能](https://code.claude.com/docs/en/skills) · [Claude 子 Agent](https://code.claude.com/docs/en/sub-agents) · [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode) · [Codex 内置生图](https://learn.chatgpt.com/docs/image-generation) · [Antigravity 非交互模式](https://antigravity.google/docs/cli/headless/)
