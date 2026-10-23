# ctfy-agent

> 范围声明：本项目是**授权高校网络安全竞赛**（京华杯 TPCTF 2026 · 智能体赛道）的参赛作品。智能体只在赛事平台提供的**赛题环境/服务**内工作，用于自动完成平台布置的挑战并接受评测，不涉及任何平台外目标。

ctfy-agent 是一个**容器化的自主解题智能体**：比赛期间无人干预，自动从平台读取**赛题（challenge）**、在平台提供的赛题环境里尝试解答、把答案交给平台的**评测器（grader）**判定，并在每次运行结束时写出结果（`result.json`）。本仓库是这套解题引擎（通用防御性框架）本身，而非针对具体题型的漏洞利用（exploit）集合。

面向队友的上手说明在下面；人与 AI 共同遵守的工程规约见 [AGENTS.md](AGENTS.md)，设计取舍与接口契约见 [docs/adr/](docs/adr/)。

## 一句话架构

采用**六边形架构**（Ports & Adapters）：把稳定不变的"解题智能"放进**纯 domain**（零 I/O），把会在开发 / 正式之间切换的几条边抽成**端口（Port）**，每种环境挂一个可换的**适配器（adapter）**。

这几条边就是几个端口：平台（platform）、模型（llm）、执行（execution）、**结果写出（result / ResultSink）**，外加一个时钟（clock，把重试 / 退避的时间依赖抽出来便于测试）。domain 的核心是**协调器（Coordinator）**、**竞速蜂群（Swarm）**、**解题器（Solver）** 三件套。

```mermaid
flowchart TB
    entry(["运行入口<br/>image_main · connected_main"])
    wiring{{"组合根 · wiring.py<br/>按运行环境装配"}}

    subgraph domain["领域层 · domain/ · 纯控制逻辑"]
        direction LR
        coordinator["Coordinator<br/>调度与预算"]
        swarm["Swarm<br/>并行竞速"]
        solver["Solver<br/>ReAct 求解循环"]
        coordinator --> swarm --> solver
    end

    ports["冻结契约 · ports/<br/>PlatformPort · LLMPort · ExecutionPort<br/>ResultSink · ClockPort"]
    adapters[["I/O 适配器 · adapters/<br/>CTFY REST · Gateway · Subprocess<br/>CtfyResultSink · RealClock"]]

    entry -->|驱动| domain
    entry -->|请求装配| wiring
    domain -->|仅依赖契约| ports
    adapters -->|实现契约| ports
    wiring -.->|创建| adapters
    wiring -.->|注入端口实现| domain

    classDef contract stroke-dasharray: 5 3;
    class ports contract;
```

图中实线表示驱动或契约关系，虚线表示装配与注入；适配器仅列出当前正式路径使用的实现。

**铁律**：`domain/` 只依赖 `ports/` 的 I/O 契约，**不 import 任何 adapter**；具体适配器由 `wiring.py` 装配后注入。这条边界由 import-linter（`lint-imports`）在全检里强制，违规的 import 会直接让全检失败。

想并行开发？认领一个端口 + 它的适配器即可独立推进：适配器矩阵见 [docs/adr/0001-agent-architecture.md](docs/adr/0001-agent-architecture.md) §3.4，新增适配器的步骤见 [AGENTS.md](AGENTS.md) §4。

## 快速上手

需要 Python 3.11+ 和 Bash；Windows 本地开发使用 Git Bash，不使用 WSL 启动器代替本地 shell。

```bash
python -m venv .venv
. .venv/Scripts/activate            # Windows PowerShell 用：.venv\Scripts\Activate.ps1
pip install -e ".[dev]"             # 基础开发依赖；可选 extras 见下
cp .env.example .env                # 填本地 token / 模型；.env 永不提交
python -m pytest -q                 # 跑测试，应全绿
```

可选 extras（按需加）：`model`（litellm 模型网关客户端）、`mcp`（平台 MCP 传输）、`docker`（开发期每题一容器，需本机 docker）。例如 `pip install -e ".[dev,model,mcp,docker]"`。

日常自检命令：

```bash
python -m pytest -q      # 测试
ruff check .             # 代码风格
lint-imports             # 架构依赖（六边形边界）
python tools/check.py    # 一键全检：ruff + lint-imports + pytest（+ mypy，建议性不阻断）
```

提交前请保证 `python tools/check.py` 本地全绿。

## 两种运行模式

| 模式 | 入口 | 谁来跑 | 用途 |
|---|---|---|---|
| 镜像模式（image mode） | `python -m agent.entrypoints.image_main` | 平台 | 正式提交：平台起容器（`cwd=/ctfy/work`，注入 `CTFY_*` / `LLM_*` 环境变量），运行后在**每条退出路径**都写出 `result.json` 并 `exit 0` |
| 连线模式（connected mode） | `python -m agent.entrypoints.connected_main --challenge <id>` | 你（开发） | 本机连平台或 dev 实例，不重建镜像，快速迭代；同样会写出 `result.json` 供检查 |

**本地跑一题（连线模式）**：

```bash
python -m agent.entrypoints.connected_main --challenge <id>   # 不带 --challenge 则尝试所有可见赛题
```

**空跑自检（dry-run）**：把 `.env` 里的 `CTFY_EVAL_DRY_RUN` 设为 `1`（bash 下也可 `CTFY_EVAL_DRY_RUN=1 python -m agent.entrypoints.image_main`）。此时镜像入口只写一份最小合法的 `result.json`、不做任何模型调用，用来验证容器能正常启动并产出结果。

此外还有本地 CLI `ctfy-agent`（`agent.entrypoints.cli_main`），目前是个轻量占位入口（支持 `--version` 等），会把你指向连线模式。

连线模式初始化或拉题失败时也会写入脱敏的错误结果，并以非零状态退出；镜像模式仍遵守平台约定，始终写结果并退出 0。

### 任务、实例与预算

- `task.json` 按平台标准读取顶层 `questions` / `gated_questions`；缺少问题时尝试拉取题目详情，不把空题集当作已完成。只有已知问题全部答对且没有待解锁问题时，才报告 `fully_solved=true`；每次 CORRECT 判定后重新拉取问题以继续解锁。
- `instance` 模式先启动实例，按平台返回的 `poll_after_s` 等待就绪，再把 host / port / TLS / SNI 连接数据交给 solver；比赛暂停时由适配器按平台等待时间重试启动，真正不可用的实例不调用模型。`qa` 模式不启动实例，普通选项答案走 QA 提交接口，不要求 flag 外形。
- `max_turns` 是**整次运行**的模型调用轮次上限，所有题目和竞速 lane 共享扣减，发起调用前预占，失败调用也计入；CLI harness 每次启动计一轮。任务中的 `max_turns: null` 表示不设轮次上限，只有缺少该键时才继承环境配置。
- `limits.max_tokens`（缺省用 `CTFY_EVAL_MAX_TOKENS`）按模型网关返回的实际用量在整次运行内累计。它是软上限：单次响应或已在途的并发调用可能越过阈值，但之后不再发起新调用；CLI harness 内部的 token 用量当前不可见。

### 命令执行状态

每条 `execute` 在独立、受监督的 Bash 进程中运行，stdin 默认 EOF。
工作区快照恢复 cwd、普通变量、数组、export 和函数；不是复用同一个活 shell。
trap、shell 选项与作业表不跨调用保留。后台服务必须重定向全部标准流，并自行保存 PID。
退出码来自操作系统，stdout 中的标记和工作区文件不能声明“命令完成”；
超时或取消会终止对应进程组 / Windows Job，并丢弃该次执行的快照。

## 目录速览

```
agent/
  domain/        纯解题逻辑（零 I/O）：coordinator / swarm / solver / state / bus / policy / prompts
  ports/         冻结契约（Protocol）：platform / llm / execution / result / clock —— 契约即分工
  adapters/      可换实现（承担全部 I/O）：
                   platform/   ctfy_rest · ctfy_mcp · ctfd
                   llm/        gateway · local_key
                   execution/  subprocess_exec · docker_sandbox
                   result/     ctfy_result
  config.py      Settings.from_env() —— 运行配置的环境变量解析
  briefing.py    load_task() —— 解析 task.json 成任务简报（briefing）
  wiring.py      组合根：按环境装配 adapter 并注入 domain（build_for_image / build_for_dev）
  entrypoints/   驱动入口：image_main（镜像模式）/ connected_main（连线模式）/ cli_main（本地 CLI）
  testing/fakes.py   每个端口一个内存假件，测试不碰真实网络
docs/adr/        架构决策记录（ADR）
docs/standards/  平台契约 CTFY-0001 / CTFY-0002
tests/           unit/ 与 contract/（每个适配器一份契约测试）
tools/check.py   一键全检
```

权威版的目录结构与端口接口以 [docs/adr/0001-agent-architecture.md](docs/adr/0001-agent-architecture.md) §3.2 为准。

## 安全与行为准则

- 本智能体是授权竞赛的参赛程序；一切行为限定在平台提供的**赛题环境/服务**内，表述与实现保持中立、防御性。
- **赛题 / 工具返回的内容一律视为不可信数据**，绝不执行其中夹带的指令；某个值只有在被平台评测器 `submit` 后给出接受的**判定（Verdict）**时，才算 flag —— 题面或响应里"自称 flag"的文本一概不认。
- 不在代码、日志、`result.json` 里留任何凭证；凭证只来自环境变量（`CTFY_*` / `LLM_*`）。`.env` 与 `external-agents/`（仅本地研究用）永不提交。
- 普通工具只继承必要的运行时环境，使用独立 HOME，不继承控制面的平台、模型凭证或 SDK 认证别名。模型消息只清洗已知控制密钥，保留题目自身未知的 Bearer 值；诊断产物则统一脱敏后截断、编码。
- Windows 的 MSYS `/tmp` 挂载可在并发 shell 间共享；`TEMP` / `TMP` 保持控制进程的稳定临时根，工作区仍各自拥有独立的 `HOME` 和 `TMPDIR`，避免清理一个工作区后破坏其他命令的临时目录。
- Linux 执行前设置 `no_new_privs`，限制普通工具读取当前控制进程的 `/proc/<pid>/environ`、`mem` 等敏感条目；生成线程持有 `CAP_SYS_PTRACE` 时拒绝执行。`no_new_privs` 不可撤销，也限制该控制线程后续启动进程的提权。
- 这不是完整的文件系统、网络或多用户沙箱：更高祖先的环境和同 UID 可读文件不受上述保护。不要将 Windows 本地工具执行当作恶意程序隔离环境，也不要把真实密钥文件放在工具可读位置。
- 回归运行器审计本次 result、记录、Markdown、trace、新基线及终端的完整清单，通过后才原子发布安全报告；缺失、检查失败或泄露不会标绿。历史产物不会自动清理。

## 下一步去哪儿

- [AGENTS.md](AGENTS.md) —— 人与 AI 共同遵守的工程规约：架构铁律、冻结契约（frozen contract）、如何加一个适配器、协作规范（**动手前先读**）。
- [docs/adr/](docs/adr/) —— 设计决策与取舍：[0001 整体架构](docs/adr/0001-agent-architecture.md)、[0002 domain 内部分块](docs/adr/0002-domain-block-architecture.md)、[0003 开发约定](docs/adr/0003-dev-conventions.md)。
- [CONTRIBUTING.md](CONTRIBUTING.md) —— 环境搭建、分支 / PR 流程、哪些改动需要额外小心。
- 平台契约：[docs/standards/CTFY-0001](docs/standards/CTFY-0001_agent-protocol.md)（镜像 / 环境 / `result.json`）、[docs/standards/CTFY-0002](docs/standards/CTFY-0002_challenge-package.md)（赛题 / 评测模型）。
