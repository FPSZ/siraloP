# ADR-0002：domain 内部分块架构（主控 / 工具 / 上下文 / 模型）

- 状态：**提案（待队内确认几个参数）**
- 日期：2026-10-21
- 决策者：战队成员（京华杯 TPCTF 2026 智能体赛道）
- 相关：[ADR-0001 Python + 六边形](0001-agent-architecture.md)、[CTFY-0001](../standards/CTFY-0001_agent-protocol.md)、[资料汇总 §7/§8](../京华杯TPCTF2026_智能体赛道_资料汇总.md)
- 本文定义 ADR-0001 中 `domain/` 的内部结构；每条结论附实证来源，未核实者已标注。

---

## 1. 背景与一条贯穿结论

> 范围声明：本项目是**授权高校网络安全竞赛**（京华杯 TPCTF 2026）的参赛作品，所有行为均限定在赛事平台提供的题目环境内、用于自动完成平台布置的挑战并评测智能体能力，不涉及任何平台外目标。

ADR-0001 定了"Python + 六边形 + 强单 agent 为核 + 多模型竞速做冗余"。本 ADR 把 `domain/` 拆成四块并逐块定架构：**① 控制循环 ② 工具层 ③ 上下文与记忆 ④ 模型层**，外加跨切面策略。

**贯穿结论（四块调研互相印证）**：

> **效率来自"好工具 + 够用的模型 + 简单强循环 + 便宜竞速冗余"，不是来自重型编排。**
> 多 agent/重协调主要买"稳定性"而非抬高上限，且 token 贵 3–10×（Anthropic、Cognition、《A Second Look》）。因此算力优先投入①②③，而非复杂 agent 图谱。

约束回顾（来自 CTFY-0001 与平台配置）：单容器（无 DinD）、比赛断网（只连平台/模型网关/赛题环境）、便宜 flash 模型（GLM/DeepSeek/Kimi 经网关）、赛题服务 TLS+SNI:443、排名=解题数且比速度、经费有限。

---

## 2. 决策总览

```
                          ┌──────────── ④ 模型层 ────────────┐
                          │ flash 铺量 → 行为信号触发升级(~1/3题)│
                          └───────────────┬───────────────────┘
          ┌───────── ① 主控循环 ─────────┐│
   事件驱动 while + ReAct + 活计划(todo)   ││   调度/止损
   + 协议层断路器(入参校验+stderr自纠正)   ││
          └──────────────┬───────────────┘│
                         ▼                 ▼
   ┌─────────── Swarm 竞速(2–3路, 同题多模型, 首个oracle验真即取消其余) ───────────┐
   │   每路 Solver 通过 ↓ 工作                                                      │
   │  ┌── ② 工具层 ──┐   ┌──── ③ 上下文与记忆 ────┐   ┌─── 跨切面策略 ───┐        │
   │  │ CodeAct主干  │   │ 主上下文=最小高信号JSON │   │ 抗注入/幻觉flag  │        │
   │  │ +5–12结构化   │   │ 原始输出→文件(留指针)   │   │ /错误上限/ROI   │        │
   │  │ +分类渐进披露 │   │ scratchpad+子agent隔离  │   └──────────────────┘        │
   │  └──────────────┘   └────────────────────────┘                               │
   └────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. ① 控制循环 Control Loop

**选型**：**事件驱动 `while(有 tool_use){执行→回灌结果}` + ReAct 内核 + 窗口内"活计划/todo 队列"**，即 Plan→Execute→Parse→Iterate。**不**用纯 ReWOO（静态计划在强随机 CTF 下最脆）或纯 Plan-and-Execute。

**依据**：ReAct 对未知环境适应性最强但"局部合理、缺全局"，用每轮按新观测重排的 todo 补全局性；Claude Code/Codex 的主循环本质就是这个 while 事件循环。

**必装：协议层 / 断路器（高 ROI，对 flash 尤其划算）**

- 工具入参**强类型 + 白名单校验**（如 port∈1–65535、bash 仅走预批准二进制），非法参数在进沙箱前直接拒。
- 执行后**解析 stderr/退出码，结构化回灌**给模型做下一步修正。
- **失败尝试去重记录**，避免重复撞墙。
- 思想源自 STRIATUM-CTF 的"协议层"（见 §7 纠偏：STRIATUM 为单 agent）。

**协调器（coordinator）：保持"笨而稳"**

- 只做：心跳监控、停滞判定、梯度升级、止损、预算分配、首 flag 取消兄弟。**不替 solver 做题。**
- **停滞判定用行为信号**（见 §6），不信模型自评。
- 依据：重型编排贵（~3–10×，部分二手口径 ~15× 待核实）且脆（上下文分散）；收益主要来自并行广度与 token 投入，而非编排智能。

---

## 4. ② 工具层 Tool Layer

**选型**：**CodeAct / bash-centric 主干 + 少量结构化封装 + 分类渐进式披露**。

- **主动作 = 一个沙箱内 `execute`（Python + bash）**：弱 flash、断网、各题型脚本串联最吃这套，工具定义最省（CodeAct 复杂多步 +20%）。
- **结构化封装 ~5–12 个**：把 bash 难驱动 / 高频易错的做成工具——调试器、与赛题服务的交互连接（socket/TLS 客户端，赛题 TLS+SNI:443）、`submit_flag`、`notify_coordinator`、headless 浏览器动作等。依据：交互式封装 > 纯 bash（EnIGMA SOTA；CTFAgent 去掉增强工具后显著掉分）。
- **披露方式 = 轻量"分类命名空间"渐进式披露**：按题型 `web_/pwn_/ai_` 分组，**一次只向模型暴露当前题类工具**。**不上 RAG-over-tools**（我们才几十个工具，检索器只添乱）。
- **甜点区**：同时可见 **≤~10 个**，系统总量 **≤~20**（Anthropic Claude Code 本身仅 ~20 个工具）。工具过多必降可靠性（ToolChoiceConfusion）。
- **工具设计规范（Anthropic）**：合并多步操作、返回高信号字段（非 uuid）、响应默认截断（~25k token）、错误信息可操作、避免功能重叠、按前缀命名空间。
- **MCP 克制**：可作内部传输（平台本身是 MCP），但别堆叠功能重叠的 server。

---

## 5. ③ 上下文与记忆 Context & Memory

**选型**：**"卸载到文件 + 结构化 scratchpad"为主，压缩(compaction)仅兜底**。

依据：flash 窗口小且"越长越笨"（Context Rot 实证：~300 token 聚焦提示明显优于 ~113k 全历史；单个干扰项即降准、更易幻觉）。

**分层**：

| 层                       | 放什么                                           | 规则                                         |
| ------------------------ | ------------------------------------------------ | -------------------------------------------- |
| 主上下文（最小高信号）   | 角色/题目目标/活计划(todo)/死路摘要/当前少量观测 | **全结构化 JSON，严禁灌原始工具输出**  |
| 卸载到文件               | 原始结果、长输出、中间产物                       | 主上下文只留路径 + 一行摘要                  |
| 持久 scratchpad          | `findings.md` / `deadends.md`                | 跨步、跨 compaction 存活                     |
| 子 agent 隔离            | 每类(Web/Pwn/AI) 信息收集                        | 独立窗口烧探索，只回传 1–2k token 结论      |
| 共享总线（多 solver 时） | 结构化信息条目（来源/类别/内容/置信度/时间戳）   | 去重后入库、按需检索；**不互灌原始流** |
| 兜底                     | 单题会话超长时 compaction                        | 保目标/计划/死路/最近文件                    |

**精髓（STRIATUM）**：**"以环境而非 token 窗口为唯一真相源"**——执行层回**结构化 JSON 观测**（Parse 阶段只抽关键字段），而非原始文本流，从源头控制上下文膨胀。

---

## 6. ④ 模型层 Model Layer

**选型**：**便宜 flash 铺量 + 行为信号触发升级 + 中低题 2–3 路竞速 + 用 flag oracle 替代投票 + 难题硬止损**。

关键前提：**flag 可自动校验（oracle）** → 投票/LLM-judge 基本无用，省下的钱投竞速+升级。

**参数（待队内确认）**：

| 项                   | 建议值                                                                                                         | 依据                                                                            |
| -------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 默认打底模型         | 经费内最强的便宜 flash（DeepSeek-V4.1-flash-1017 或 Kimi-K3 主力，GLM-5.3-flash 作第二路/执行端）              | 贵≠强；但弱模型有下限（Haiku-executor 13%、Flash-8B 3%）→不让纯弱模型独立承担求解 |
| 升级阈值（任一触发） | 连续 **5–8 轮无新进展** / **同动作+同观测重复 ≥4 次** / 判定为 Pwn·Reverse 高难类起步即用强模型 | FrugalGPT cascade 最高省 98%；行为信号比自评可靠                                |
| 走强模型的题占比     | 目标 **~1/3**（对齐 RouteLLM CPT≈37%）                                                                  | 对强模型省 35–85%                                                              |
| 竞速宽度             | 易/中题 **2–3 路**；难题不铺宽→单路+升级+止损                                                          | coverage 随样本单调升但**收益递减**，k 小即可                             |
| 停滞判定             | 行为信号（重复动作/空转/动作熵坍塌）                                                                           | 模型 verbalized confidence 过度自信、校准差                                     |
| 不做                 | 对可验证 flag 投票/judge；执行端换最弱模型                                                                     | oracle 已在、弱模型崩盘                                                         |

---

## 7. 跨切面策略（所有 solver 共用，`domain/policy.py`）

- **抗注入硬规则（防御性）**：赛题环境/工具返回内容**一律当不可信数据，绝不执行其中夹带的指令**；flag 只认平台 oracle 校验，不认题面/响应里"自称 flag"的文本。对应平台为测试鲁棒性而设置的干扰项与诱导指令（现有 agent 此类 0%）。
- **幻觉 flag 防护** + **错误提交次数上限感知**（按题型：单选3/多选10/静态10/判定10），避免无效反复试提交。
- **ROI 预算**：先横扫中低难度（拿分最快），每题设 $ + 轮数硬上限，难题触发**止损**，把预算挪给"差一点就出"的中档题；已解题不再加投。
- **失败也要落结果**：任何异常路径保证 `result.json` 被写（CTFY-0001：不写=判失败）。
- **分节暂停**：`competition_paused` 时按 `next_session_starts_at` sleep，不空转烧 token。

---

## 8. 影响（Consequences）

**正面**：每块选型都有实证支撑且对齐平台榜"Claude Code 外壳 + flash 最强"；算力投向高 ROI 处（工具/上下文/竞速），省钱且拿分快；各块边界清晰，可按块分工。

**成本 / 风险**：协议层断路器、分类渐进式披露、上下文分层都需前期工程；多 solver 共享总线是自有设计（无现成实证，需自测）；具体参数（默认模型、阈值、竞速宽度）需在练习题库上调。

---

## 9. 待确认 / 待调（练习题库上实测）

- 默认 flash 模型选谁、强模型选谁（取决于正式赛网关清单 + 额度）。
- 升级阈值轮数、竞速宽度的最优值（在 XBOW/CTFARENA 上网格搜）。
- 子 agent 隔离与共享总线的收益是否 > 其复杂度（二期再定是否上）。

---

## 10. 纠偏与未核实项（诚实标注）

- **STRIATUM-CTF 是单 agent（Sonnet 4.5 + extended thinking），无消息总线、无多 solver**；本 ADR 的"多 solver + 共享总线"是**我们的自有设计**，不可挂其名。
- "planner+executor **+1.5pp 却贵 45%**"**未核实到原文**；方向成立（加一层小幅提分却明显增费），具体数字作废。最接近为 D-CIPHER Auto-prompter 消融（+3.0pp / +约42% 成本）。
- Context Rot "全历史比聚焦低约 30%" 的**精确百分比**原文仅在图表；仅确认"聚焦明显优于全历史"及量级（~300 vs ~113k token）。
- FrugalGPT/RouteLLM 的省钱百分比为"某些基准最佳值"，非普适。
- 多篇 2026 预印本（2602/2605/2606/2607.*）数字按临时结论对待。

---

## 11. 来源

**控制循环**：Anthropic《Building Effective Agents》 [https://www.anthropic.com/research/building-effective-agents](https://www.anthropic.com/research/building-effective-agents) · Cognition《Don't Build Multi-Agents》 [https://cognition.ai/blog/dont-build-multi-agents](https://cognition.ai/blog/dont-build-multi-agents) · STRIATUM-CTF [https://arxiv.org/html/2603.22577v1](https://arxiv.org/html/2603.22577v1) · Claude Agent SDK agent-loop [https://platform.claude.com/docs/en/agent-sdk/agent-loop](https://platform.claude.com/docs/en/agent-sdk/agent-loop)

**工具层**：Anthropic《Code execution with MCP》 [https://www.anthropic.com/engineering/code-execution-with-mcp](https://www.anthropic.com/engineering/code-execution-with-mcp) ·《Writing tools for agents》 [https://www.anthropic.com/engineering/writing-tools-for-agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · CodeAct [https://arxiv.org/abs/2402.01030](https://arxiv.org/abs/2402.01030) · ToolChoiceConfusion [https://arxiv.org/abs/2606.06284](https://arxiv.org/abs/2606.06284) · How Many Tools [https://www.alphaxiv.org/abs/2605.24660](https://www.alphaxiv.org/abs/2605.24660) · EnIGMA [https://arxiv.org/abs/2409.16165](https://arxiv.org/abs/2409.16165) · CTFAgent [https://arxiv.org/abs/2506.17644](https://arxiv.org/abs/2506.17644) · Harness design [https://arxiv.org/abs/2609.20804](https://arxiv.org/abs/2609.20804)

**上下文与记忆**：Anthropic《Effective context engineering》 [https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) · Chroma《Context Rot》 [https://www.trychroma.com/research/context-rot](https://www.trychroma.com/research/context-rot) · LOCA-bench [https://arxiv.org/html/2602.07962](https://arxiv.org/html/2602.07962)

**模型层**：Large Language Monkeys [https://arxiv.org/abs/2407.21787](https://arxiv.org/abs/2407.21787) · FrugalGPT [https://arxiv.org/abs/2305.05176](https://arxiv.org/abs/2305.05176) · RouteLLM [https://arxiv.org/abs/2406.18665](https://arxiv.org/abs/2406.18665) · 角色专化成本 [https://arxiv.org/pdf/2606.20629](https://arxiv.org/pdf/2606.20629) · D-CIPHER [https://arxiv.org/abs/2502.10931](https://arxiv.org/abs/2502.10931) · cost-aware CTF [https://arxiv.org/pdf/2607.15263](https://arxiv.org/pdf/2607.15263) · OpenHands stuck detector [https://docs.openhands.dev/sdk/guides/agent-stuck-detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector)

**效率总论**：《A Second Look》 [https://arxiv.org/html/2605.21497v1](https://arxiv.org/html/2605.21497v1) ·《Hacking CTFs with Plain Agents》 [https://arxiv.org/abs/2412.02776](https://arxiv.org/abs/2412.02776) · Anthropic 多 agent 研究系统 [https://www.anthropic.com/engineering/multi-agent-research-system](https://www.anthropic.com/engineering/multi-agent-research-system)
