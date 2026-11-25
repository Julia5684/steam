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
2. 执行或预览时先运行 `catalog --short`，从可用 route 中挑选最少的步骤；模型、等级和额度由脚本读取。只读用户明确提供的少量相关材料。不要为派工提前自己调查整套代码。将每个步骤写成下例的 JSON，提供足够的具体现象、授权范围和验收条件。无依赖时 `needs` 为空。有上下游关系时用步骤 ID 表示。脚本会把上游摘要和已验证产物传给下游。

   - 用户明确要求代码审查时，选 `review`，默认用 `read/text`。简报写明审查范围，要求按严重度给出问题、代码位置和验证建议。用户同时要求修复时，再安排依赖审查结果的修复步骤。
   - 梳理项目架构、模块职责、调用链或变更影响时，选 `project_analysis`，默认用 `read/text`，结论附文件或符号依据。根据帧时间、GPU 或内存采样定位瓶颈时，选 `perf_analysis`。
   - 编写或更新 README、接入指南、设计说明时，选 `documentation`。材料齐全时直接写；需要先调研工程行为时，用 `needs` 引用 `project_analysis`。写入指定文件用 `write/files` 并列出目标路径，只需回复正文用 `read/text`。

```json
{"steps":[{"id":"fix","route":"bug_fix","brief":"具体问题、目标和验收条件","needs":[],"access":"write","kind":"text","refs":[],"outputs":[]}]}
```

3. `access` 取 `read` 或 `write`。修改代码但事先未知要改哪些文件，可用 `write/text` 并要求执行器报告 diff 和测试。交付指定文件用 `write/files`，填写目标路径。内置生图用 `write/image`，提供至少一个 .png 目标。`refs` 和 `outputs` 都是项目内相对文件路径。合并重复步骤；普通任务不安排全员复审。
4. 在 Claude Code 中，每份计划或回执都先运行 `input-path --workspace "<项目目录>"`，用原生 Write 工具把完整 JSON 写入返回的 `path`。Write 成功后，将该文件的路径作为 `--spec` 或 `--reply` 的参数，路径加双引号。任务正文只进入文件。Codex 也可用 `-` 从标准输入传入 UTF-8 JSON。

   预览和正式执行都先用 `plan --save --workspace "<目录>" --spec "<计划文件>"` 取得已校验的分工和唯一运行目录。在当前聊天列出全部子任务，每项用一句话写清工作内容、执行器、依赖和当前状态，给出总数及运行目录。`--plan` 到此结束，标明尚未启动并给出对应客户端的 `--resume` 命令。正式执行直接接续同一目录。客户端提供可见任务列表时，也按步骤 ID 对应更新本次任务，续跑沿用已有条目。
5. 在前台用 `continue --one-step --run-dir "<目录>"` 每次推进一步。调用前在聊天点名即将派发的子任务，收到回执后先报告哪项在何时结束、结果如何、还有几项，再推进下一步。用脚本记录的时间显示当地时刻和耗时，旧记录缺少的时间留空。例如「项目结构分析（Codex）14:06 完成，用时 2 分。成功 1/2，接下来由 Claude 编写接入说明。」

   长步骤分段等待同一工具调用。保留工具返回的后台句柄，用 `result --run-dir "<目录>"` 查询进度，并继续收取原调用的输出。客户端允许分段等待时，约每分钟在聊天更新当前任务、已用时间和剩余项数。工具提示命令停止或句柄失效时，先查同一目录的 `result`。仍在运行就继续跟踪，`recovery_required` 为真则单步续跑以核对回执。`busy` 时等待持锁操作，重读状态后再接续。运行中保持本轮跟踪，只报实测进度。
6. 仅当本次 `continue` 返回 `needs_claude` 和 `next` 时执行原生简报，保留 `next.claim`。模型与等级均与主管相同时直接做，其他原生简报调用前台 general-purpose Agent，把 `next.model` 和 `next.level` 分别填入工具调用的 `model` 和 `effort`。工具缺少所需参数或拒绝目标值时，提交 `failed` 回执并注明原因。独立设计稿保留完整内容，较长设计可写用户指定的项目文件。

   按第 4 步写入新的回执文件，内容为 `{"status":"success","summary":"完整交付或文件位置、证据"}` 或 `{"status":"failed","summary":"具体原因"}`。以 `complete --run-dir "<目录>" --step <ID> --claim <next.claim> --reply "<回执文件>"` 提交。格式有误时，用原领取码及新输入文件更正。`waiting_claude` 表示原会话持有简报，由它提交回执。原会话仍在执行时跟踪回执，已结束且无法核实结果时明确标为待核实。每次 `complete` 或单步续跑返回 `ready`，都按第 5 步继续。
7. 收到整项终态后，在聊天中明确写出结束时刻、总耗时以及各子任务的结局。全部成功时报告「全部完成（N/N）」并交付结果和验证依据。遇到失败、中断或受阻，分别列出成功项、失败原因和因依赖未执行的步骤。中断时间表示确认中断的时刻。给出本次运行目录，需要完整结果时读取 `steps/<步骤ID>/step-result.json`（旧版为 `result.json`）。用户要求实际运行验证时，把验证写进步骤简报交给执行器。

本次请求

$ARGUMENTS
