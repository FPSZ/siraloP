# ADR-0001：智能体整体架构 —— Python + 六边形（Ports & Adapters）

- 状态：**已接受**
- 日期：2026-10-21
- 决策者：战队成员（京华杯 TPCTF 2026 智能体赛道）
- 相关：[CTFY-0001 协议](../standards/CTFY-0001_agent-protocol.md)、[资料汇总](../京华杯TPCTF2026_智能体赛道_资料汇总.md)

---

## 1. 背景与驱动力

> 范围声明：本项目是**授权高校网络安全竞赛**（京华杯 TPCTF 2026）的参赛作品，所有行为均限定在赛事平台提供的题目环境内、用于自动完成平台布置的挑战并评测智能体能力，不涉及任何平台外目标。

智能体赛道要求：我们提交一个**容器化 AI Agent**，比赛期间**无人干预**自动完成 Web/Pwn/AI 安全题。由此带来的硬约束（均见 CTFY-0001 与平台配置）：

| 约束 | 影响 |
|---|---|
| 两种接入：连线模式(MCP/REST) vs 镜像模式 | **入口要可换**：本地开发用连线模式，正式提交镜像模式 |
| 正式赛 `internet_policy=deny` | 依赖全打进镜像；只有**平台/模型网关/赛题环境**可达 |
| 容器 caps 全 drop、无 docker socket | **无 DinD**：不能像 ctf-agent 那样每题开子容器 |
| 资源 8GiB/2CPU/512pid | 并发蜂群要受限 |
| 赛题服务 TLS+SNI:443 | 执行层需 TLS 客户端 |
| 模型凭证由平台注入(`LLM_*`/`ANTHROPIC_*`) | 模型来源要可换：网关(正式) / 自备key(dev) |
| dev 阶段想先用 CTFd / 练习题验证 | 平台对接层要可换 |

**核心观察**：解题智能（coordinator + 多模型蜂群 + solver 循环）是**稳定不变**的，而它四周的环境（平台、模型、执行、启动方式）**每一条都要在 dev 和正式之间切换**。这正是六边形架构（Ports & Adapters）的典型场景。

## 2. 决策

1. **语言用 Python**。理由：① 蓝本 `verialabs/ctf-agent`（BSidesSF 2026 冠军）、CAI、Strix 全是 Python，核心可直接拆进 domain 复用；② LLM agent 生态（pydantic-ai/litellm/各 SDK/MCP）与 CTF 工具链（pwntools/angr/z3）均 Python 原生；③ 官方 `ctfy[client]` SDK 为 Python。
2. **架构用六边形**。把"会在 dev/正式间变化的边"抽成**端口(Port)**，每种环境提供一个**适配器(Adapter)**；解题智能放在**纯 domain**里，不碰任何 I/O。
3. **务实收敛**：只抽 4 个端口（Platform / LLM / Execution / 结果写出）+ 3 个驱动入口。不跨环境变化的东西（prompt、message bus）直接放 domain，**不为抽象而抽象**。

## 3. 详细架构

### 3.1 分层与依赖方向

```
            驱动适配器(入站)                     被驱动端口(出站) → 适配器
         ┌──────────────────┐
 镜像式→ │ ImageEntrypoint   │              ┌──────────────┐   PlatformPort
 连接式→ │ ConnectedRunner   │─── 调用 ───▶ │              │─▶ CtfyRest / CtfyMcp / Ctfd(dev)
 本地→  │ CliRunner         │              │   DOMAIN     │
         └──────────────────┘              │  (纯逻辑,    │   LLMPort
                                           │   零 I/O)    │─▶ Gateway / LocalKey(dev)
                                           │              │
      依赖只能由外向内：                    │ Coordinator  │   ExecutionPort
      adapters → ports → domain            │ Swarm        │─▶ Subprocess(正式) / DockerSandbox(dev)
      domain 不 import 任何 adapter         │ Solver       │
                                           │ State / Bus  │   ResultSink
                                           └──────────────┘─▶ CtfyResult (写 result.json)
```

**铁律**：`domain/` 不 import `adapters/`，只依赖 `ports/` 里的抽象。adapters 实现端口。`wiring.py`（组合根）在启动时按环境把具体 adapter 注入 domain。

### 3.2 目录结构

```
agent/
  domain/                 # 核心,纯逻辑,可脱离平台单测
    coordinator.py        # 复盘进度、分配算力、给 solver 写技术提示
    swarm.py              # 每题拉起多策略并行竞速,先拿 flag 者胜
    solver.py             # SolverStrategy 协议 + ReActSolver / CliHarnessSolver
    state.py              # Challenge/Question/RunState/Budget 数据模型
    bus.py                # 进程内消息总线(solver 间共享发现)
    policy.py             # 跨切面策略:抗注入、预算/轮次、循环检测
    prompts.py
  ports/                  # 抽象接口(Protocol/ABC),domain 只认这些
    platform.py           # PlatformPort
    llm.py                # LLMPort
    execution.py          # ExecutionPort
    result.py             # ResultSink
  adapters/               # 具体实现,可换
    platform/ ctfy_rest.py  ctfy_mcp.py  ctfd.py
    llm/      gateway.py     local_key.py
    execution/ subprocess_exec.py  docker_sandbox.py
    result/   ctfy_result.py
  entrypoints/            # 驱动入口(main)
    image_main.py         # CTFY-0001 镜像式:读 /ctfy/work → 跑 → 写 result.json → exit 0
    connected_main.py     # 连接式:本地连平台(MCP/REST)
    cli_main.py           # 本地跑题目文件夹
  config.py               # 读环境变量/配置
  wiring.py               # 组合根:按环境装配 adapter 并注入 domain
Dockerfile                # 提交用镜像(全家桶:工具链+headless chromium+本包)
pyproject.toml
```

### 3.3 端口接口（契约，队友按此并行实现）

```python
# ports/platform.py —— 与平台交互(拉题/起实例/交flag/看榜/gated轮询)
class PlatformPort(Protocol):
    async def whoami(self) -> Account: ...
    async def list_challenges(self) -> list[Challenge]: ...
    async def get_challenge(self, cid: str) -> Challenge: ...          # gated 答对后重拉
    async def start_instance(self, cid: str) -> Instance: ...          # 返回赛题服务地址(TLS+SNI:443)
    async def submit(self, cid: str, qid: str, answer: str) -> Verdict: ...  # 含错误次数上限
    async def scoreboard(self, competition_id: str) -> Scoreboard: ...

# ports/llm.py —— 模型调用(底层对话)
class LLMPort(Protocol):
    async def complete(self, messages: list[Msg], *, model: str,
                       effort: str = "medium", tools: list[ToolDef] | None = None) -> LLMResponse: ...

# ports/execution.py —— 在"某个隔离环境"里执行,domain 不关心是容器还是子进程
class ExecutionPort(Protocol):
    async def run(self, cmd: str, *, timeout_s: int = 60, workdir: str | None = None) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def new_workspace(self, challenge_id: str) -> str: ...       # 按题隔离工作目录

# ports/result.py —— 结果落地(CTFY-0001 §3)
class ResultSink(Protocol):
    def write(self, result: RunResult) -> None: ...                    # 写 /ctfy/work/result.json
```

### 3.4 适配器矩阵（谁在哪种环境用）

| 端口 | 适配器 | 环境 | 说明 |
|---|---|---|---|
| Platform | `CtfyRestAdapter` | 正式/dev | 纯 HTTP，`$PLATFORM_URL/api/v1`，bearer |
| Platform | `CtfyMcpAdapter` | 正式/连线模式 | 走 `/api/v1/mcp/` 的 ~20 工具 |
| Platform | `CtfdAdapter` | dev | 复用 ctf-agent 逻辑，拿练习/CTFd 先跑通 |
| LLM | `GatewayAdapter` | **正式** | 指 `LLM_BASE_URL`，断网也可达；litellm/SDK 空构造 |
| LLM | `LocalKeyAdapter` | dev | 自备 key，本地快速迭代 |
| Execution | `SubprocessAdapter` | **正式** | **单容器内 subprocess**，无 DinD，按题分 workdir |
| Execution | `DockerSandboxAdapter` | dev | 复用 ctf-agent aiodocker，每题一容器(本地有 docker 时) |
| Result | `CtfyResultSink` | 正式 | 写 `$CTFY_WORK_DIR/result.json` |

> Solver 层另有两种策略，均通过上面端口工作：`ReActSolver`（domain 自己的 thought→action(run)→obs 循环，用 LLMPort+ExecutionPort）、`CliHarnessSolver`（subprocess 调 `claude`/`codex` CLI 当外壳，走 ExecutionPort；印证平台榜"Claude Code 外壳最强"）。

### 3.5 数据流（镜像模式正式赛，顺利路径）

```
1. 平台起容器(cwd=/ctfy/work, 注入 LLM_*/CTFY_* 环境变量)
2. image_main.py: config 读环境 → wiring 装配
     Platform=CtfyRest(或Mcp)  LLM=Gateway  Exec=Subprocess  Result=CtfyResultSink
3. Coordinator.run():
     a. platform.list_challenges()
     b. 对每题 → Swarm.solve(challenge):
          - exec.new_workspace(cid)；若有实例 platform.start_instance(cid)
          - 并行拉起 N 个 SolverStrategy 竞速(受资源信号量限制)
          - solver 循环: LLM 决策 → exec.run(bash,连 TLS 赛题服务) → 观察 → ...
          - 命中 → platform.submit(cid, qid, flag) → 真对则广播到 bus,停该题其余 solver
          - gated: 答对后 platform.get_challenge(cid) 重拉解锁下一问
     c. coordinator 读各 solver 轨迹,给卡住的写技术提示(回 bus)
4. 收尾: result.write(RunResult(answers/solved/writeup)) → exit 0
```

### 3.6 资源预算（对齐 8GiB/2CPU/512pid）

- **全局并发信号量**：同时活跃的 solver × 每 solver 的子进程数 要远低于 512 pid；`SubprocessAdapter` 复用 ctf-agent 的 `configure_semaphore()`，但把 50 调到个位数。
- **内存大户单独限流**：headless chromium / SageMath 这类，限制同时只跑 1–2 个（Web 题要浏览器，必须内置但不能并发爆内存）。
- **预算/轮次**：`domain/policy.py` 统一管 `CTFY_EVAL_MAX_TURNS`（可能为 null=无限）与 token 预算；`competition_paused` 时按 `next_session_starts_at` sleep，不空转烧钱。

### 3.7 跨切面策略（`domain/policy.py`）

- **抗注入(硬规则，防御性)**：赛题环境/工具返回内容**一律视为不可信数据**，**绝不执行其中夹带的指令**；flag 只认平台 `submit` 的判定，不认题面/响应里"自称 flag"的文本。对应平台为测试鲁棒性而设置的干扰项与诱导指令（现有 agent 在此类 0%）。
- **幻觉 flag 防护**：submit 前做格式/来源校验，错误提交受"按题型次数上限"约束，避免无效反复试提交。
- **失败也要落结果**：任何异常路径都要保证 `result.json` 被写（CTFY-0001：不写=判失败）。

## 4. 影响（Consequences）

**正面**
- dev 与正式两套环境**只换适配器**，核心解题逻辑写一次、单测不依赖平台。
- 分工清晰：可按"端口/适配器"认领并行开发（见 §3.4），接口即契约。
- 最大化复用 ctf-agent：其 coordinator/swarm/solver/prompts 进 domain，三个 I/O 文件重写成适配器。
- 换模型/换平台/上下容器都是局部改动，赛前调整风险低。

**负面 / 成本**
- 比"直接 fork 平铺"多一层端口抽象，初期多写样板。→ 用 §2.3 的"只抽会变的边"控制。
- 团队需对齐端口契约后才好并行，前期要花半天把接口敲定。

## 5. 备选方案（为何不选）

- **直接 fork ctf-agent 平铺改**：最快但 dev/正式、CTFd/CTFy、DinD/单容器全缠在一起，改一处牵全身，难并行。
- **TypeScript + MCP/Agent SDK**：MCP/Agent SDK 为 TS 原生、类型安全好，但**放弃 ctf-agent 全部复用**、CTF 工具链与官方 SDK 需绕行；时间成本不划算。端口契约语言无关，若后续想换可保留本 ADR 的接口设计。

## 6. 待确认（影响适配器实现，赛前问主办）

- 正式赛最终收**镜像模式**还是也收**连线模式** → 决定主入口优先级。
- 模型网关**可用模型清单 + 每队 token/经费额度**。
- 单容器内**能否联多进程/是否有 pid 更严限制**；镜像大小上限。
- 是否有 Pwn 题占比（评测榜几乎全 Web）→ 决定执行层要不要重投 arm/qemu、调试器。
