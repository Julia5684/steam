# /steam 多 Agent 协作

在 Claude Code Desktop 的本地 Code 会话里，输入 `/steam 任务` 即可派工。平时继续正常使用 Claude。Claude 负责分析意图、选择任务类型，以及承担配置分给它的工作。Python 脚本负责执行顺序、模型参数、结果记录和文件检查。

Steam 默认只接受手动调用。Claude Code 使用 `/steam`，Codex 使用 `$steam`。Codex 可以检查环境、预览计划、查询结果，以及执行仅含 Codex 和 Antigravity 步骤的任务。包含 Claude 步骤的计划可以在 Claude Code 中执行或继续。

## 安装

准备 Python 3.10 或更新版，以及准备使用的官方 CLI。默认的文档写作和界面动画设计在 Claude Code 会话中执行。需要脚本派发的固定型号 Claude 任务还需本机已登录的 Claude Code CLI（`claude`）。Codex 通过 `codex login` 选择 ChatGPT 账号，Antigravity 使用有相应权益的 Google 个人账号登录 `agy`。首次登录和项目授权在对应客户端完成。各 CLI 可通过当前会话的 PATH 或配置中的可执行文件绝对路径定位。Python 只用标准库。

解压后把整个 `steam` 文件夹放在以下位置。确认 `SKILL.md`、`config.json`、`scripts` 和 `agents` 是同级，避免多套一层 `steam`。Claude Code 从 `SKILL.md` 的文件头读取手动调用设置，Codex 从 `agents/openai.yaml` 读取。

| 环境 | 安装后的技能目录 |
| --- | --- |
| Windows 原生本地会话 | `%USERPROFILE%\.claude\skills\steam\` |
| macOS / Linux 本地会话 | `~/.claude/skills/steam/` |
| Windows 上的 WSL 会话 | 对应 Linux 发行版中的 `~/.claude/skills/steam/`，Python 和 CLI 也从该环境启动 |
| Codex | Windows 的 `%USERPROFILE%\.agents\skills\steam\`，macOS / Linux / WSL 的 `~/.agents/skills/steam/` |

若已安装旧 `/crew` 且只想保留一个入口，可把旧 `crew` 文件夹移到 `skills` 目录以外作备份。新技能首次未出现在菜单中时，可新开本地 Code 会话。安装不需要常驻服务或项目级配置。

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
| `/steam --plan 任务` | 主管拆解，脚本校验步骤、依赖、额度和模型配置，在系统临时目录保存已校验的计划并展示将要派给谁。无需运行外部模型；稍后可用返回的 `/steam --resume` 命令直接执行同一计划。 |
| `/steam 任务` | 脚本先执行相同的计划校验，再按依赖顺序派发；普通 Bug 通常合并为一次 Codex 调用。 |
| `/steam --result 运行目录` | 读取该次运行的状态和结果摘要。 |
| `/steam --resume 运行目录` | 执行已预览的计划，或从检查点继续。若预览后修改配置，先用新配置重新预览。已交给 Claude 原生会话的步骤会显示 `waiting_claude`，由原会话提交回执。中断时无法判定结果的外部调用会保留待核实记录。 |

也可以直接在项目目录的终端做环境检查，全程由本机脚本执行，不启动 Claude 主管。Windows PowerShell 示例：

```powershell
py -3 "$env:USERPROFILE\.claude\skills\steam\scripts\flow.py" check --workspace .
```

macOS、Linux 或 WSL 示例：

```bash
python3 ~/.claude/skills/steam/scripts/flow.py check --workspace .
```

Claude Code 每次在独立主管上下文中运行技能。需要使用前文所提日志、截图或参数时，在本次命令里提供可访问的项目内路径或必要信息。只要求界面动画设计时，`motion_ux` 交付时间线、可交互时刻和中断行为。同时要求接入工程时，`integration` 接续设计结果。

## 唯一需要维护的配置

编辑安装目录的 **`config.json`**。它是 UTF-8 标准 JSON。

| 配置项 | 管什么 |
| --- | --- |
| `agents.<名称>.enabled` | 该执行器是否参与派工。例如暂时停用 Antigravity 可改成 `false`。指向该执行器的任务需改派或重新启用。 |
| `agents.<名称>.models` | 允许该执行器用的模型，以及每个模型允许的推理等级。填写你账号实际可用且愿意使用的组合。 |
| `agents.<名称>.executable` | 本机 CLI 的命令名或原生可执行文件的绝对路径。Claude 的固定版本专项任务使用此项，主管由 Desktop 会话启动。 |
| `routes.<任务类型>` | 每类任务的说明、默认执行器 `agent`、模型 `model`、等级 `level`。修改分工时改这一行。 |
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

任务路由、模型清单和派工上限在下次新任务生效。正在进行的任务复用启动时保存的配置。默认每项任务最多 8 步，Codex 最多 5 步，Antigravity 最多 1 步，Claude 专项工作最多 2 步。主管本身的派工和汇总另计。若需要把不同模型的两项工作都交给 Antigravity，例如粒子制作加性能归因，可在发任务前将这一上限改为 2。

**仅当更改主管模型或等级时**，在安装好的 `steam` 目录运行一次同步。Claude Code 在加载技能正文前从文件头读取这两个字段；脚本只同步这两个值。若忘记同步，执行器会提示。其他配置直接编辑 JSON 即可。

```powershell
py -3 scripts/sync_supervisor.py
```

```bash
python3 scripts/sync_supervisor.py
```

默认由 `opus / high` 主管拆分任务、派工和汇总结果。文档写作交给 `sonnet / medium` 原生子 Agent，界面动画设计交给 `opus / medium` 原生子 Agent。具体模型由 Claude Code 解析家族别名。如果任务配置的模型和档位与主管相同，由主管直接执行。如果为专项任务指定了其他完整型号，脚本会通过本机 Claude Code CLI 传递该型号和档位。

Antigravity 特效制作选择 Orchestrator 的 Gemini 4 Carbon（`gemini-pro-orchestrator`，`--effort medium`）。性能瓶颈分析选择 Orchestrator 的 Gemini 4 Neon（`gemini-pro-orchestrator-neon`，`--effort high`）。脚本用 `agy models` 核对 Orchestrator 型号。其他 Gemini 系列按型号和档位匹配目录项，也可填写目录给出的完整 slug，配合 `default` 使用该项自带的档位。

图片生成和编辑由 `gpt-6.2-astra / high` 调用 Codex 内置生图工具。图片大小、用途与风格写在任务里，交付文件使用 PNG。

## 产物、记录与成本

工程改动和指定的交付文件写在本次项目目录。预览或直接执行时会在系统临时目录创建自己的 `steam-run-*` 目录。命令返回该路径。运行目录里的 `state.json` 保存计划、配置快照和步骤状态；`steps/<步骤ID>/task.json` 与 `result.json` 保存这一步的简报和结果。外部执行的详细诊断留在步骤目录，返回给主管的是短摘要。需要长期留档时保留这次运行目录；系统可能清理临时文件。

如果领取 Claude 步骤的会话已经结束，可核对工程改动、产物或原会话记录。核实结果后，从 `state.json` 中取出该步骤的 `claim`，用 `flow.py complete --run-dir <目录> --step <ID> --claim <领取码> --reply <回执文件>` 补交 JSON 回执。回执包含 `status`（`success` 或 `failed`）及非空的 `summary`。升级前已经发出的步骤沿用原命令，省略 `--claim`。仍无法判定结果时，保留待核实状态。

声明的目标文件会在调用前后计算 SHA-256；已有文件只有在内容更新后才计为本次产物。PNG 还会做格式和数据完整性检查。没有预先声明目标路径的代码修复，以执行器报告的改动和测试为准。图片观感、交互手感、真机性能仍需对应预览或实测。脚本记录工具状态和已获得的证据。对于 Codex 生图，记录中区分原生工具事件、本次原图匹配，以及只有有效产物与执行器自述的情况。

减少模型开销的地方是派工命令、依赖传递、结果归档与大段日志。继续使用各执行器自己的账号与配额。若只想使用套餐内含量，应在产品账号中核对是否已开启超额消费。

官方说明：[Claude Code 技能](https://code.claude.com/docs/en/skills) · [Claude 子 Agent](https://code.claude.com/docs/en/sub-agents) · [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode) · [Codex 内置生图](https://learn.chatgpt.com/docs/image-generation) · [Antigravity 非交互模式](https://antigravity.google/docs/cli/headless/)
