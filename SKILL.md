---
name: steam
description: 手动派发一次 Claude、Codex、Antigravity 协作任务。
argument-hint: "[任务 | --plan 任务 | --check | --resume 运行目录 | --result 运行目录]"
disable-model-invocation: true
user-invocable: true
allowed-tools:
  - 'Bash(py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py" *)'
  - 'Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py" *)'
  - 'Edit(./.workbench/steam-inputs/*.json)'
model: opus
effort: high
---

处理本次请求和当前项目。不要检索其他会话或个人记忆。只做用户要求的交付物。普通 Bug 把定位、修复和验证合并为一步。只做动画体验设计时交付时间线、可交互时刻和中断行为；需要实际制作粒子、贴图或接入工程时才另行拆分。性能结论要注明采样依据。缺少必需上下文时，请用户在手动调用中附上复现信息或文件路径。

在 Claude Code 中，参数取 `$ARGUMENTS`。用 Bash 工具调用入口脚本，只有 PowerShell 工具可用时使用 PowerShell。Windows 原生命令前缀为 `py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`，macOS、Linux 和 WSL 为 `python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`。每次直接调用一条命令，工作目录取当前项目目录，路径参数加引号。在 Codex 中，用当前 shell 和 Python 3.10+ 调用已加载的 `SKILL.md` 同目录下的 `scripts/flow.py`，参数取本次 `$steam` 调用中的内容。下面命令省略解释器和脚本路径。执行器由脚本派发，主管按 stdout 的 JSON 接续。

含 Claude 步骤的计划在 Claude Code 中执行。原生步骤由主管领取并提交回执，固定版本专项任务由脚本派发。在 Codex 中，若计划包含 Claude 步骤，用 `plan --save` 保存计划，并给出 Claude Code 的 `/steam --resume <运行目录>` 命令。在 Codex 中处理 `--resume` 时，先核对已保存计划的执行器。已开始的运行从 `run-state.json`（旧版运行使用 `state.json`）的步骤和路由快照确认执行器。含 Claude 步骤的运行由 Claude Code 恢复。

1. 裸命令直接回复简短用法。`--check` 只运行 `check --workspace <目录>`。`--result <目录>` 只读取 `result --run-dir "<目录>"`，在聊天里展示整项状态和各步骤进度。`recovery_required: true` 表示有步骤待核对回执，给出同目录的恢复命令。`--resume <目录>` 先读取同一目录的 `result`，展示已完成和剩余工作，再按第 5 步接续。其他任务从第 2 步开始。
2. 执行或预览时，Claude Code 用 `catalog --short --workspace "<项目目录>"` 一次取得路由和计划的 `input_path`。Codex 用 `catalog --short`。从可用 route 中挑选最少的步骤，模型、等级和额度由脚本读取。只读用户明确提供的少量相关材料。将每个步骤写成下例的 JSON，提供具体现象、授权范围和验收条件。无依赖时 `needs` 为空，有上下游关系时填写步骤 ID。脚本会把上游摘要和已验证产物传给下游。

   - 用户明确要求代码审查时，选 `review`，默认用 `read/text`。简报写明审查范围，要求按严重度给出问题、代码位置和验证建议。用户同时要求修复时，再安排依赖审查结果的修复步骤。
   - 梳理项目架构、模块职责、调用链或变更影响时，选 `project_analysis`，默认用 `read/text`，结论附文件或符号依据。根据帧时间、GPU 或内存采样定位瓶颈时，选 `perf_analysis`。
   - 编写或更新 README、接入指南、设计说明时，选 `documentation`。材料齐全时直接写；需要先调研工程行为时，用 `needs` 引用 `project_analysis`。写入指定文件用 `write/files` 并列出目标路径，只需回复正文用 `read/text`。

```json
{"steps":[{"id":"fix","route":"bug_fix","brief":"具体问题、目标和验收条件","needs":[],"access":"write","kind":"text","refs":[],"outputs":[]}]}
```

3. `access` 取 `read` 或 `write`。修改代码但事先未知要改哪些文件，可用 `write/text` 并要求执行器报告 diff 和测试。交付指定文件用 `write/files`，填写目标路径。内置生图用 `write/image`，提供至少一个 .png 目标。`refs` 和 `outputs` 都是项目内相对文件路径。合并重复步骤；普通任务不安排全员复审。
4. Claude Code 用原生 Write 将完整计划 JSON 写入 `input_path`，再用 `plan --save --workspace "<目录>" --spec "<计划文件>"` 取得分工和唯一运行目录。Shell 只接收文件路径。Codex 也可用 `--spec -` 从标准输入传入 UTF-8 JSON。建文件、校验及查询留在工具详情。单步在主聊天用一句话说明任务和执行器，例如「交给 Codex 分析项目结构，重点看模块职责和入口。」多步用简短清单写出分工和依赖。客户端有任务列表时，按步骤 ID 关联条目，续跑沿用原条目。`--plan` 在此交付计划、运行目录和对应客户端的 `--resume` 命令。正式执行直接接续同一目录。
5. 按计划顺序，每次只派发一个就绪步骤，使用 `continue --one-step --step "<ID>" --stream --run-dir "<目录>"`。新派发、重连和已有终态的判定均以这个运行目录为准。

   - **外部步骤（`dispatch: cli`）** 在 Claude Code 使用一次原生 `Agent` 调用，类型为 `general-purpose`，`description` 取用户认得的动作和执行器，例如「分析项目结构 · Codex」或「分析帧耗时 · Antigravity」。给它**已展开的脚本绝对路径、Python 命令、项目目录、运行目录和步骤 ID**。让它在项目目录前台运行上述单步命令，Shell 工具标题写「Codex 执行输出」等可识别的名称。让它保留实时输出，跟到该 ID 的落盘回执，再把结果交回。该 Agent 只负责这次派发和跟踪，工作简报、模型和等级由脚本交给配置的 CLI。Codex 客户端或缺少 `Agent` 工具时，由主管前台运行相同命令。
   - **原生步骤（`dispatch: native`）** 主管自己发起上述单步命令。只有本次返回 `needs_claude` 和新 `next` 才执行简报。保留 `next.claim`，用原生 `general-purpose` Agent 执行，标题同样写清任务和执行器，按 `next.model`、`next.level` 指定 `model`、`effort`。模型与等级均与主管相同时，可在 Agent 工具不可用时由主管直接做。其他情况无法按指定参数调用时，提交带具体原因的失败回执。原生 Agent 交回后，主管执行第 6 步。

   **运行期间**，脚本把外部 CLI 的公开事件逐行写入实时工具输出。客户端将 Agent 或 Shell 转到后台时保留其句柄，继续在本轮收取输出和完成通知。外部步骤运行中，用 `result --run-dir "<目录>" --wait 30` 分段等待新活动或终态。主聊天摘述 `steps[].activity` 里的实质进展，例如已读的文件、刚完成的检查或执行器公开的发现。同一活动只报一次，静默期保留已用时间。不要推测百分比。

   **接续**时先查 `result`。异步 Agent 尚在启动就等待原调用，`busy` 就跟踪持锁的任务。若展示用 Agent 已明确结束而步骤仍为 `pending`，由主管前台派发同一 ID。若 `recovery_required` 为真，对该 ID 单步核对已保存的回执。用户主动停止时尊重停止请求。
6. 原生步骤提交回执时，用 `input-path --workspace "<目录>"` 获取新 `path`，用 Write 写入 `{"status":"success","summary":"完整交付或文件位置、证据"}` 或 `{"status":"failed","summary":"具体原因"}`，再调用 `complete --run-dir "<目录>" --step "<ID>" --claim "<next.claim>" --reply "<回执文件>"`。格式有误时用原领取码及新文件更正。`waiting_claude` 表示原会话持有简报，由它提交回执；已结束且无法核实结果时标为待核实。
7. 按落盘回执汇报。子 Agent 的结束通知到达后核对步骤 ID 和状态。若步骤摘要带 `full_result`，从本次运行目录读取它指向的文件，交付完整结论或用户指定的产物。多步在每项完成时报告该项结论和剩余工作，再派发下一项。最后一项直接在主聊天交付答案、验证依据、结束时刻与总耗时，并附运行目录。全部成功时显示「全部完成（N/N）」。失败或中断时说明具体原因和受影响的依赖项。后台句柄、`running` 和 `busy` 都是中间状态，不能据此发结束回复。

本次请求

$ARGUMENTS
