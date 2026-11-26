---
name: steam
description: 由 Claude Code Desktop 拆解、派发、跟踪并验收 Claude、Codex、Antigravity 协作任务。
argument-hint: "[任务 | --plan 任务 | --check | --resume 运行目录 | --result 运行目录]"
disable-model-invocation: true
user-invocable: true
allowed-tools:
  - 'Bash(py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py" *)'
  - 'Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py" *)'
  - 'Write(./.workbench/steam-inputs/*.json)'
  - 'Edit(./.workbench/steam-inputs/*.json)'
  - 'Agent(general-purpose)'
model: opus
effort: high
---

在当前主聊天担任主管，接收用户中途的补充和停止指令，只处理本次授权的工作。用自然语言说明分工、重要发现与变更，最后直接交付完整结果。通过 Claude 内置原生子 Agent 调用已有的 Codex、Antigravity CLI。用户直接使用本技能。脚本负责领取步骤、启动 CLI、记录回执、恢复和检查产物。

### 入口

参数取 `$ARGUMENTS`。裸命令给简短用法。`--check` 仅执行 `check --workspace "<当前项目>"`。`--result` 只查询并展示各项状态。`--resume` 先查询原运行目录，说明已验收和剩余的工作，再继续同一计划。含 Claude 步骤的计划在 Claude Code 中执行；Codex 客户端可以预览保存这类计划，给出对应的 Claude Code 恢复命令。

下文的脚本命令通过技能自带的 `scripts/flow.py` 调用。Windows 使用 `py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`，macOS、Linux 和 WSL 使用 `python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`。Codex 用已加载技能的绝对路径和当前 shell。运行目录、计划和回执文件用带引号的路径传递。

将上面展开后的脚本命令原样传给子 Agent，保留引号和路径中的 `/`，使 Bash 匹配本技能声明的工具授权。Windows 上也沿用该拼写。

### 分工

某项工作的最佳执行器已达到本次计划的步数上限时，先合并适合同一次验收的工作。若仍须降低能力才能执行，向用户说明受限的步骤，由用户决定调整上限或改派。

1. 用 `catalog --short --workspace "<当前项目>"` 获取路由、已启用的模型和计划 `input_path`。先按任务需要的能力、工具及难度决定执行器与推理等级。预期质量相当时优先 Codex，其次 Antigravity，把 Claude Pro 留给主管和 Claude 擅长的专项。配置中的步数上限不代表订阅余额。默认路由适用时直接采用；改选时在主聊天给一句具体理由，同时在步骤指定 `agent`、`model`、`level`，取值来自目录。
2. 拆成可独立验收的最少步骤。一般 Bug 的定位、修改和验证可由同一人完成。明确要求 review 用 `review`，梳理架构与调用链用 `project_analysis`，基于采样定位性能问题用 `perf_analysis`，文档用 `documentation`。区分动画体验设计、资源制作、实际接入，按用户需要选择。依赖上游结果、会读到另一项尚在修改的文件、或可能修改同一资源时填写 `needs`。并行写入前明确文件归属。
3. 将计划写入 `input_path`，执行 `plan --save --workspace "<项目>" --spec "<文件>"`。示例如下。所有 `refs`、`outputs` 使用项目内相对路径。交付指定文件用 `write/files`，生成 PNG 用 `write/image`。事先未知改动文件可用 `write/text`，在简报要求报告 diff 和验证结果。一个步骤用一句话介绍，多步列出负责人、工作和依赖。会话提供任务清单工具时，同步用户能看懂的条目，续跑更新原条目。`--plan` 在此交付分工、目录和恢复命令。

```json
{"steps":[{"id":"analyze","route":"project_analysis","brief":"梳理奖励入口与调用链，给出文件与符号依据","needs":[],"access":"read","kind":"text","refs":[],"outputs":[]}]}
```

### 派发与跟踪

会话提供任务清单时，先将总任务设为 `in_progress`。子项逐项验收，只有所有必需回执都已核对、准备交付最终结果时才将总任务设为 `completed`。遇到失败或用户主动停止时保留真实状态，标明还差哪一项。

在同一轮发起所有已就绪、可独立运行的步骤。每一项都有自己的 ID 和原生任务标题，例如「梳理奖励调用链 · Codex」。保存句柄。**派发和验收是两次不同的事件，交出句柄后继续负责整个请求。**

- **Claude Code 中的 CLI 步骤**。每步派一个内置 `general-purpose` 原生子 Agent，工具名及字段以当前宿主为准，常见为 `Agent` 或 `Task`。在 `description` 填可辨认的任务名及实际执行器，启用后台运行；独立步骤在同轮派发。这个子 Agent 只负责启动、等待和回送结果，可选宿主已提供的轻量型号，例如 `haiku`，否则使用可用的默认型号。它会使用 Claude 额度，实际工作使用计划中选定的外部模型。下面的派工简报由主管填好所有绝对路径后交给子 Agent；每个 ID 只派一次。
- **原生 Claude 步骤**。先执行 `continue --one-step --step "<ID>" --stream --run-dir "<目录>"`。只有收到新的 `next.claim` 才按 `next` 简报、模型和等级派原生 Agent。将领取码关联此 ID；有多个 Claude 步骤时分别保存。原生工具不可用且模型、等级均与主管一致时可直接做，否则据实提交失败原因。固定完整型号的 Claude CLI 步骤也通过上述原生子 Agent 执行单步命令。
- **宿主差异**。Claude Code 若按需加载工具，先查找内置 `Agent` 或 `Task`。确实无法派发时，说明哪个步骤受阻，保留计划。在 Codex 等其他客户端运行纯 CLI 计划时，可用该客户端的 Shell 执行同一条单步命令，保存句柄并等待结果。

交给 CLI 子 Agent 的简报

```text
执行已保存的 Steam 步骤 <ID>，实际执行器为 <执行器名>。
项目绝对路径为 <项目>，Steam 脚本绝对路径为 <flow.py>，运行目录为 <目录>。
任务简报为 <本步骤简报>。
在该项目中用 Bash 前台执行以下命令一次，公开活动保留在工具输出。
<py -3 或 python3> "<flow.py>" continue --one-step --step "<ID>" --agent "<执行器名>" --stream --run-dir "<目录>"
等待命令真正退出。若宿主把长命令转入后台，保存其句柄，使用宿主的等待工具继续等到终态并取回输出。
busy 时查询此运行并跟踪该 ID 的已有调用。完成前不要重复派工，不要自行实施步骤里的业务改动。
结束后检查 result --run-dir "<目录>"，从 steps 按 <ID> 核对状态，读取该 ID 的完整 step-result.json 回执，旧版运行沿用 result.json。
返回本步骤状态、关键发现、产物、验证结果和回执绝对路径。整批状态和命令退出码需要结合本 ID 的回执判断。
失败则报告实际原因，停止则保留停止状态。
```

有阶段发现、阻塞、改派或交付时才更新主聊天。例如「Codex 已定位到奖励写入入口，Antigravity 还在核对帧时间采样」。用户可展开各原生 Agent 查看 Bash 调用和公开执行过程。主管收到阶段消息或查询到活动后，再给出有依据的更新。后台运行时使用原生完成通知及 `result --run-dir "<目录>" --wait 60` 等待状态变化。有就绪工作继续派发，有在途工作等待回执；不要因输出静默编造进度，也不要把“已派发”作为最终回答。旧运行按保存时的执行顺序恢复。

### 调整、恢复与验收

- 新发现或用户纠正方向时，可以调整尚未启动的工作。先查询当前状态，用 `input-path --workspace "<项目>"` 取得新路径，写入修订后的完整 `steps`，再用 `revise --run-dir "<目录>" --spec "<文件>"`。保留已开始步骤的原定义，向用户简述改变的工作和原因。重试前核实原步骤没有继续运行、检查已有改动与产物；确实可以安全重试时，在修订 JSON 的 `retry` 数组列出失败或受影响的步骤 ID。可改派同等能力的执行器，能力明显下降时让用户决定。用户主动停止的任务须等用户指示后再启动。
- Claude 原生步骤用 `input-path` 准备新的回执文件，写入 `{"status":"success","summary":"交付结果和验证依据"}` 或 `failed` 加具体原因，再执行 `complete --run-dir "<目录>" --step "<ID>" --claim "<领取码>" --reply "<文件>"`。已领取的旧步骤只由持有领取码的会话交回。`busy` 时跟踪原调用；`recovery_required` 时逐项核对保存的回执。
- **最终验收**。每次收到 Agent 完成通知，查询 `result`，按 ID 核对落盘回执、产物与用户的验收条件。读回 `full_result` 指向的完整内容。任务全部成功、主管已经整合并在主聊天交付完整结果，才显示「全部完成（N/N）」。运行中保留进行状态；失败时先推进独立工作，处理不了时明确写「受阻」、已验收数量和所需决定。结束回复包括相关验证依据和本次运行目录。单任务直接交付结果，多任务统一汇总一次。

本次请求

$ARGUMENTS
