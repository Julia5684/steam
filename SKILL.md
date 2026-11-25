---
name: steam
description: 手动派发一次 Claude、Codex、Antigravity 协作任务。
argument-hint: "[任务 | --plan 任务 | --check | --resume 运行目录 | --result 运行目录]"
disable-model-invocation: true
user-invocable: true
allowed-tools:
  - 'Bash(py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py" *)'
  - 'Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py" *)'
context: fork
agent: general-purpose
background: false
model: opus
effort: high
---

处理本次请求和当前项目。不要检索其他会话或个人记忆。只做用户要求的交付物。普通 Bug 把定位、修复和验证合并为一步。只做动画体验设计时交付时间线、可交互时刻和中断行为；需要实际制作粒子、贴图或接入工程时才另行拆分。性能结论要注明采样依据。缺少必需上下文时，请用户在手动调用中附上复现信息或文件路径。

在 Claude Code 中，参数取 `$ARGUMENTS`。用 Bash 工具调用入口脚本，只有 PowerShell 工具可用时使用 PowerShell。Windows 原生命令前缀为 `py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`，macOS、Linux 和 WSL 为 `python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`。每次直接调用一条命令，工作目录取当前项目目录，路径参数加引号。在 Codex 中，用当前 shell 和 Python 3.10+ 调用已加载的 `SKILL.md` 同目录下的 `scripts/flow.py`，参数取本次 `$steam` 调用中的内容。下面命令省略解释器和脚本路径。执行器由脚本派发，主管按 stdout 的 JSON 接续。

含 Claude 步骤的计划在 Claude Code 中执行。原生步骤由主管领取并提交回执，固定版本专项任务由脚本派发。在 Codex 中，若计划包含 Claude 步骤，用 `plan --save` 保存计划，并给出 Claude Code 的 `/steam --resume <运行目录>` 命令。在 Codex 中处理 `--resume` 时，先核对已保存计划的执行器。已开始的运行从 `run-state.json`（旧版运行使用 `state.json`）的步骤和路由快照确认执行器。含 Claude 步骤的运行由 Claude Code 恢复。

1. 裸命令直接回复简短用法。`--check` 只运行 `check --workspace <目录>`。`--result <目录>` 只运行 `result --run-dir <目录>`。`--resume <目录>` 运行 `continue --run-dir <目录>`，按返回状态接续。其他任务从第 2 步开始。
2. 执行或预览时先运行 `catalog --short`，从可用 route 中挑选最少的步骤；模型、等级和额度由脚本读取。只读用户明确提供的少量相关材料。不要为派工提前自己调查整套代码。将每个步骤写成下例的 JSON，提供足够的具体现象、授权范围和验收条件。无依赖时 `needs` 为空。有上下游关系时用步骤 ID 表示。脚本会把上游摘要和已验证产物传给下游。

   - 用户明确要求代码审查时，选 `review`，默认用 `read/text`。简报写明审查范围，要求按严重度给出问题、代码位置和验证建议。用户同时要求修复时，再安排依赖审查结果的修复步骤。
   - 梳理项目架构、模块职责、调用链或变更影响时，选 `project_analysis`，默认用 `read/text`，结论附文件或符号依据。根据帧时间、GPU 或内存采样定位瓶颈时，选 `perf_analysis`。
   - 编写或更新 README、接入指南、设计说明时，选 `documentation`。材料齐全时直接写；需要先调研工程行为时，用 `needs` 引用 `project_analysis`。写入指定文件用 `write/files` 并列出目标路径，只需回复正文用 `read/text`。

```json
{"steps":[{"id":"fix","route":"bug_fix","brief":"具体问题、目标和验收条件","needs":[],"access":"write","kind":"text","refs":[],"outputs":[]}]}
```

3. `access` 取 `read` 或 `write`。修改代码但事先未知要改哪些文件，可用 `write/text` 并要求执行器报告 diff 和测试。交付指定文件用 `write/files`，填写目标路径。内置生图用 `write/image`，提供至少一个 .png 目标。`refs` 和 `outputs` 都是项目内相对文件路径。合并重复步骤；普通任务不安排全员复审。
4. 用户使用 `--plan` 时运行 `plan --save --workspace <目录> --spec -`。展示分工、返回的运行目录和对应客户端的 `--resume` 命令。正式执行用 `run --workspace <目录> --spec -`，脚本先校验完整计划。预览和执行的记录保存在项目的 `.workbench/<task_id>/`，续跑沿用原目录。计划和回执都以 UTF-8 JSON 从标准输入传入。Claude Code 的 Bash 命令末尾加 `<<'STEAM_JSON'`，下一行开始写 JSON，最后用独占一行的 `STEAM_JSON` 结束。其他情况下用当前 shell 的字面量文本机制，也可传入临时 UTF-8 JSON 文件。任务原文仅作为 JSON 数据传入。
5. 等待同一进程。达到工具等待时限时继续等待或查看同一运行目录。脚本通过 Claude Code CLI 执行需要固定完整型号的专项任务。仅在本次 `run` 或 `continue` 返回 `needs_claude` 和 `next` 时执行原生简报，保留 `next.claim` 用于提交。模型与等级均与主管相同时直接做，其他原生简报调用前台 general-purpose Agent，显式传目标模型别名与 effort。所选档位无法落实时如实登记失败。独立设计稿保留完整内容，较长设计可写用户指定的项目文件。
6. 以 `complete --run-dir <目录> --step <ID> --claim <next.claim> --reply -` 提交 UTF-8 JSON `{"status":"success","summary":"完整交付或文件位置、证据"}` 或 `{"status":"failed","summary":"具体原因"}`。回执格式有误时，用同一领取码更正回执即可。返回 `ready` 时再运行 `continue`。看到 `waiting_claude` 表示简报已经发出，报告等待中的步骤及运行目录，由领取简报的会话提交。任务是否执行完仍不确定时，保留现场供核实。
7. 汇报实际完成的步骤、用户需要的结果或文件、针对性验证和阻塞项。需要全文时仅查看本次运行对应的 `steps/<步骤ID>/step-result.json`（旧版运行使用 `result.json`）。避免把过程日志重复送进模型。给出本次运行目录，便于用户查验。

本次请求

$ARGUMENTS
