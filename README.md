# ctfy-agent

> 范围声明：本项目是**授权高校网络安全竞赛**（京华杯 TPCTF 2026 · 智能体赛道）的参赛作品。智能体只在赛事平台提供的**赛题环境/服务**内工作，用于自动完成平台布置的挑战并接受评测，不涉及任何平台外目标。

ctfy-agent 是一个**容器化的自主解题智能体**：比赛期间无人干预，自动从平台读取**赛题（challenge）**、在平台提供的赛题环境里尝试解答、把答案交给平台的**评测器（grader）**判定，并在每次运行结束时写出结果（`result.json`）。本仓库是这套解题引擎（通用防御性框架）本身，而非针对具体题型的漏洞利用（exploit）集合。

面向队友的上手说明在下面；人与 AI 共同遵守的工程规约见 [AGENTS.md](AGENTS.md)，设计取舍与接口契约见 [docs/adr/](docs/adr/)。

## 一句话架构

采用**六边形架构**（Ports & Adapters）：把稳定不变的"解题智能"放进**纯 domain**（零 I/O），把会在开发 / 正式之间切换的几条边抽成**端口（Port）**，每种环境挂一个可换的**适配器（adapter）**。

这几条边就是几个端口：平台（platform）、模型（llm）、执行（execution）、**结果写出（result / ResultSink）**，外加一个时钟（clock，把重试 / 退避的时间依赖抽出来便于测试）。domain 的核心是**协调器（Coordinator）**、**竞速蜂群（Swarm）**、**解题器（Solver）** 三件套。

```
                组合根 wiring 在启动时按环境装配
入口 entrypoints ─▶ domain（纯逻辑：Coordinator / Swarm / Solver）
                         │ 只依赖
                         ▼
                    ports（冻结契约 Protocol：platform / llm / execution / result / clock）
                         ▲ 被实现
                    adapters（可换，承担全部 I/O）
```

**铁律**：依赖只能由外向内 —— `adapters → ports → domain`，**domain 不 import 任何 adapter**。这条边界由 import-linter（`lint-imports`）在全检里强制，违规的 import 会直接让全检失败。

想并行开发？认领一个端口 + 它的适配器即可独立推进：适配器矩阵见 [docs/adr/0001-agent-architecture.md](docs/adr/0001-agent-architecture.md) §3.4，新增适配器的步骤见 [AGENTS.md](AGENTS.md) §4。

## 快速上手

需要 Python 3.11+。

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
  config.py      Settings.from_env() —— 唯一读环境变量的地方
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

## 下一步去哪儿

- [AGENTS.md](AGENTS.md) —— 人与 AI 共同遵守的工程规约：架构铁律、冻结契约（frozen contract）、如何加一个适配器、协作规范（**动手前先读**）。
- [docs/adr/](docs/adr/) —— 设计决策与取舍：[0001 整体架构](docs/adr/0001-agent-architecture.md)、[0002 domain 内部分块](docs/adr/0002-domain-block-architecture.md)、[0003 开发约定](docs/adr/0003-dev-conventions.md)。
- [CONTRIBUTING.md](CONTRIBUTING.md) —— 环境搭建、分支 / PR 流程、哪些改动需要额外小心。
- 平台契约：[docs/standards/CTFY-0001](docs/standards/CTFY-0001_agent-protocol.md)（镜像 / 环境 / `result.json`）、[docs/standards/CTFY-0002](docs/standards/CTFY-0002_challenge-package.md)（赛题 / 评测模型）。
