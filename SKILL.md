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
model: inherit
---

在当前主聊天担任主管，接收用户中途的补充和停止指令，只处理本次授权的工作。用自然语言说明分工、重要发现与变更，最后直接交付完整结果。通过 Claude 内置原生子 Agent 调用已有的 Codex、Antigravity CLI。用户直接使用本技能。脚本负责领取步骤、启动 CLI、记录回执、恢复和检查产物。

### 入口

参数取 `$ARGUMENTS`。裸命令给简短用法。`--check` 仅执行 `check --workspace "<当前项目>"`。`--result` 查询并展示各项状态。`--resume` 或“继续刚才的任务”沿用对话里的运行目录。没有目录时，用宿主文件工具在当前项目的 `.workbench/steam-run-*/` 中找对应计划，目标有歧义才请用户选择。先查询状态，再恢复原计划。含 Claude 步骤的计划在 Claude Code 中执行，Codex 客户端可以预览保存并给出恢复命令。

下文的脚本命令通过技能自带的 `scripts/flow.py` 调用。Windows 使用 `py -3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`，macOS、Linux 和 WSL 使用 `python3 "${CLAUDE_SKILL_DIR}/scripts/flow.py"`。Codex 用已加载技能的绝对路径和当前 shell。运行目录、计划和回执文件用带引号的路径传递。

将上面展开后的脚本命令原样传给子 Agent，保留引号和路径中的 `/`，使 Bash 匹配本技能声明的工具授权。Windows 上也沿用该拼写。

### 分工

某项工作的最佳执行器已达到本次计划的步数上限时，先合并适合同一次验收的工作。若仍须降低能力才能执行，向用户说明受限的步骤，由用户决定调整上限或改派。

1. 用 `catalog --short --workspace "<当前项目>"` 获取路由、已启用的模型和计划 `input_path`。先按任务需要的能力、工具及难度决定执行器与推理等级。预期质量相当时优先 Codex，其次 Antigravity，把 Claude Pro 留给主管和 Claude 擅长的专项。配置中的步数上限不代表订阅余额。默认路由适用时直接采用；改选时在主聊天给一句具体理由，同时在步骤指定 `agent`、`model`、`level`，取值来自目录。
2. 拆成可独立验收的最少步骤。一般 Bug 的定位、修改和验证可由同一人完成。明确要求 review 用 `review`，梳理架构与调用链用 `project_analysis`，基于采样定位性能问题用 `perf_analysis`，文档用 `documentation`。区分动画体验设计、资源制作、实际接入，按用户需要选择。依赖上游结果、会读到另一项尚在修改的文件、或可能修改同一资源时填写 `needs`。并行写入前明确文件归属。
3. 外部执行器只收到本步骤的简报、引用文件和依赖结果。把前文中仍适用的约束、用户刚补充的要求和验收条件写入 `brief`，把需要打开的项目内附件列入 `refs`。仅主聊天能看到的截图或文档，由主管查看并传递相关内容。工作依赖原件时，使用能访问原件的执行方式，或请用户提供可访问的文件。
4. 将计划写入 `input_path`，执行 `plan --save --workspace "<项目>" --spec "<文件>"`。返回的简报是预览，派工读取已保存的全文。所有 `refs`、`outputs` 使用项目内相对路径。交付指定文件用 `write/files`，生成 PNG 用 `write/image`。事先未知改动文件可用 `write/text`，在简报要求报告 diff 和验证结果。单步用一句话交代任务与执行器，多步列出分工和依赖。宿主提供任务清单时，以实际交付项建条目，续跑更新原条目。`--plan` 在此交付分工、目录和恢复命令。

```json
{"steps":[{"id":"analyze","route":"project_analysis","brief":"梳理奖励入口与调用链，给出文件与符号依据","needs":[],"access":"read","kind":"text","refs":[],"outputs":[]}]}
```

### 派发与跟踪

主管维护任务清单，派工时标为进行中，核对交付后标为完成。遇到失败或用户主动停止时保留实际状态。

在同一轮发起所有已就绪、可独立运行的步骤。每项保存步骤 ID、原生任务句柄和可辨认的标题，例如「梳理奖励调用链 · Codex」。主管负责到交付为止。

- **Claude Code 中的 CLI 步骤**。每步派一个内置 `general-purpose` 原生子 Agent，工具名及字段以当前宿主为准，常见为 `Agent` 或 `Task`。在 `description` 填可辨认的任务名及实际执行器，启用后台运行；独立步骤在同轮派发。这个子 Agent 只负责启动、等待和回送结果，可选宿主已提供的轻量型号，例如 `haiku`，否则使用可用的默认型号。它会使用 Claude 额度，实际工作使用计划中选定的外部模型。下面的派工简报由主管填好所有绝对路径后交给子 Agent；每个 ID 只派一次。
- **原生 Claude 步骤**。先执行 `continue --one-step --step "<ID>" --stream --run-dir "<目录>"`。只有收到新的 `next.claim` 才按 `next` 简报、模型和等级派原生 Agent，按 ID 保存领取码。原生工具不可用时，只有能确认当前会话的实际模型、等级均与该步骤相同时，主管才可直接做。否则据实提交失败原因。需要 Claude CLI 的步骤由上述启动器执行单步命令。
- **宿主差异**。Claude Code 若按需加载工具，先查找内置 `Agent` 或 `Task`。确实无法派发时，说明哪个步骤受阻，保留计划。在 Codex 等其他客户端运行纯 CLI 计划时，可用该客户端的 Shell 执行同一条单步命令，保存句柄并等待结果。

交给 CLI 子 Agent 的简报

```text
你是步骤 <ID> 的启动器。在项目 <项目绝对路径> 中，用 Bash 前台原样执行以下命令一次。
<py -3 或 python3> "<flow.py>" continue --one-step --step "<ID>" --agent "<执行器名>" --stream --run-dir "<目录>"
操作限于这条命令，以及宿主把它转入后台时用原句柄等待、取回最终输出。回执阅读、Git 检查和业务验收由主管负责。
命令退出即返回 ID、最终输出中该 ID 的状态及主管给定的回执路径 <回执绝对路径>。若返回 busy、执行错误或没有该 ID 的状态，原样报告给主管后结束。
```

优先等原生完成通知，让用户能在主聊天继续说话。详细工具活动显示在原生任务卡。主聊天仅在有影响结论的发现、阻塞、改派或需要用户决定时更新。通知无法取得或句柄丢失时，先查 `result --run-dir "<目录>"`。若有外部步骤在执行，可直接调用 `result --run-dir "<目录>" --wait 60 --wait-for state` 等待状态变化。纯原生 Claude 步骤等宿主通知。已取得正式回执后即可验收，同一 ID 的迟到通知只处理一次。

### 调整、恢复与验收

- 用户补充要求时，把它传入受影响的待执行步骤。外部 CLI 启动后收不到主聊天的新消息。若纠正使在途工作失效，用原生句柄停止受影响的任务，待确认退出后按新要求接续。若用户只说“停止”，停止派发，按原生句柄停止本次在途任务，核对命令退出和已产生的结果，说明实际停止或仍待确认的步骤，等用户指示再启动。
- 修改计划时，先查询状态，用 `input-path --workspace "<项目>"` 取得新路径，写入修订后的完整 `steps`，再用 `revise --run-dir "<目录>" --spec "<文件>"`。保留已开始步骤的原定义。重试前确认原执行已退出且已检查必要产物，使用 `retry` 列出需要重试的失败、中断或受阻 ID。能力明显下降的改派由用户决定。
- Claude 原生步骤用 `input-path` 准备新的回执文件，写入 `{"status":"success","summary":"交付结果和验证依据"}` 或 `failed` 加具体原因，再执行 `complete --run-dir "<目录>" --step "<ID>" --claim "<领取码>" --reply "<文件>"`。已领取的旧步骤只由持有领取码的会话交回。`busy` 时跟踪原调用；`recovery_required` 时逐项核对保存的回执。
- **恢复**。查询结果列出 `recovery_steps` 时，逐个执行 `continue --one-step --step "<ID>" --run-dir "<目录>"` 结算遗留回执。外部启动器失锁且没有回执时，步骤记为中断，保留已有成果。若宿主被强制关闭，失锁只证明启动器已退出，原 CLI 的退出状态单独核实；仍无法确认时标为待核实。活跃步骤跟踪原调用，旧运行沿用保存的执行顺序。
- **最终验收**。由主管按 ID 读一次完整回执，新运行位于 `steps/<ID>/step-result.json`，旧运行为 `steps/<ID>/result.json`。结合用户要求核对声明的产物或验证依据。单步只读分析直接给出结论、依据和影响；多步统一汇总。失败时推进独立工作，明确剩余阻塞。只有预览、需要恢复或用户要查看细节时才附运行目录。

本次请求

$ARGUMENTS
