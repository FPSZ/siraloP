# 京华杯 TPCTF 2026 · 智能体赛道 资料汇总

> 全称：**首届 CCF 网络安全智能挑战赛暨"京华杯"TPCTF 2026**。资料采集 2026-10-21，一切以官网/参赛群最新通知为准。
> 原始材料见 [docs/official/](official/)（公告、手册、奖项/赛程截图）、[docs/standards/](standards/)（平台规范 CTFY-0001/0002 原文）、[docs/platform/](platform/)（接口抓取的比赛与题库配置）。

---

## 0. 一页速览

- **这是一场 CTF**。智能体赛道 = 我们写一个**容器化 AI Agent，比赛期间全自动跑**（无人干预），连到题目环境自己探测、利用、交 flag。
- **出题范围只限"在线交互类"**：Web、Pwn、AI 安全三类。离线附件类（Crypto/Reverse/Misc）不在智能体赛道考察。
- **平台是清华 CTFy**（`ctfy.tsinghua.edu.cn`）。Agent 按 **CTFY-0001 协议**接入：平台把题目简报塞进容器 `/ctfy/work/`，Agent 干完把答案写回 `result.json` 即可，平台替你判分。
- **时间**：报名即日起～11/13；入围/初选赛 11/15；正式赛 11/21–22（每日 10:00–20:00）。名额**限 150 队**，可能先报先得。
- **奖金**：智能体赛道冠/亚/季 = 2万/1万/5千，另有两个单项奖各 3千/2千；都带 CCF 盖章证书。比人机协同赛道奖金高一档。
- **评分**：按 flag 数排名，同分比最早解出时间 → **又快又多**。
- **最大杠杆（平台自带评测榜实测）**：**"Claude Code 外壳 + 便宜的国产 flash 模型"** 性价比碾压；平台提供模型网关，GLM/DeepSeek/Kimi 都能走。Web 题现有 agent 已能解 ~75%，但 **AI 安全/蜜罐/多步企业靶场/间接提示注入类接近 0%**——这几类是胜负手。
- **2026 制胜范式（全网竞品印证，详见 §8）**：**多模型并行竞速（蜂群）+ 协调器复盘喂提示**，而非单 agent 死磕；外壳>模型。最快起步 = **fork `verialabs/ctf-agent`（BSidesSF 2026 冠军）把 CTFd 适配层换成 CTFY-0001/MCP**。
- **练习无门槛**：平台有 6000+ 道公开练习题（CTFARENA/XBOW/SQLILABS/UPLOADLABS/AGENTBENCH/AGENT-DEFENSE 等），随时可打，就是我们的训练场。

---

## 1. 赛制与时间线 [官方]

| 阶段 | 时间（北京时间） | 说明 |
|---|---|---|
| 报名 | 即日起 ~ **2026-11-13 20:00** | 平台报名，**需审核**；名额满可能提前截止 |
| 入围/初选赛 | **11-14（备用）、11-15 10:00–20:00** | 报名超限时可能 11-14 加测；否则同分先报先入 |
| 正式比赛 | **11-21 ~ 11-22，每日 10:00–20:00** | 分节进行；人机协同赛道只打 22 日 |
| 颁奖 | 暂定 12-05~06 深圳 CCF INFORSEC 论坛 | 冠亚季军代表受邀 |

- 平台上该赛道起止登记为 2026-11-15 ~ 11-22（UTC），内含 3 个 session（11/15、11/21、11/22 各 10:00–20:00）。**分节比赛**：session 之间平台会暂停，接口返回 `competition_paused` + 下次开始时间，Agent 应**按提示 sleep 而非空转**（空转白烧 token）。
- **名额**：智能体赛道 **≤150 队**，人机协同 ≤500 队（平台硬编码）。截至 10-21 智能体赛道已报 31 队。

## 2. 两条赛道 [官方]

| | 智能体赛道（本队目标） | 人机协同赛道 |
|---|---|---|
| 形式 | 团队构建 AI Agent，**全自动**独立参赛 | ≤4 人选手 + AI 工具协同作战 |
| 人工干预 | **禁止**，运行期无人介入 | 全程人在，自由调度 AI |
| 出题范围 | **仅在线交互类**（Web/Pwn/AI 安全） | 全部六类 |
| 比赛天数 | 11/15 初选 + 11/21–22 正式 | 仅 11/22 |
| 奖金（冠/亚/季） | **2万 / 1万 / 5千** + 2 个单项奖(3千/2千) | 5千 / 3千 / 2千 |

- **组队**：≤4 人，须来自**同一法人单位**（同校/同公司）。队长接受命题贡献者协议（涉及"出题"环节，见 §3）。
- **参赛对象**：全日制高校在校生（专/本/研）+ 网安企业从业者，不限专业。
- **奖项档位**（两赛道各自算）：一等奖=有效排名第4名~前5%（有效参赛<80 则只设冠亚季）；二等=5%~10%；三等=10%~20%。**所以进前20%就有证书，冲奖要进前3**。

## 3. 智能体赛道技术要点 —— CTFY-0001 协议 [平台规范]

> 全文见 [standards/CTFY-0001_agent-protocol.md](standards/CTFY-0001_agent-protocol.md)。这是 Agent 与平台之间**唯一的接口契约**，镜像内部用什么语言/框架平台完全不管。

**最小可用 Agent 只有 3 步**：① 读 `/ctfy/work/README.md` 照做；② 把 `{"schema_version":2, "answers":{...}}` 写到 `/ctfy/work/result.json`；③ 退出码 0。**不写文件 = 判失败**（平台不读 stdout！这是上传镜像第一大坑）。

**平台塞给容器的东西**（环境变量 + `/ctfy/work/` 目录）：
- `task.json`（机器可读简报）+ `README.md`（同内容散文版）+ `mcp.json`（直接可用的 MCP 配置，指向平台工具服务）。
- 模型凭证：`LLM_FLAVOR`(anthropic/openai) + `LLM_API_KEY/BASE_URL/MODEL`，**并同时按 SDK 原生变量名给一份**（`ANTHROPIC_*`/`OPENAI_*`）。所以一个普通写法的 Claude Code / OpenAI SDK agent **零改动**就能跑。
- ⚠️ 坑：`LLM_API_KEY` 可能为空（该 run 没经费）→ 应**大声报错别 fallback 自己的 key**；`BASE_URL` 给了就必须用；anthropic 的 model 要用 `LLM_MODEL`，别让客户端按"tier"去读没资助的槽位。
- ⚠️ 容器 **cwd 已是 `/ctfy/work`**，`ENTRYPOINT` 别用相对路径（`["python","agent.py"]` 起不来，要写 `/app/agent.py`）。

**交答案两种方式**：① 懒人法——填 `result.json` 的 `answers`，平台在 run 结束替你提交；② **运行中实时提交**（`POST /api/v1/submissions` 或 MCP），更优，因为能立刻知道对错、且**有些题是 gated（答对一题才解锁下一题）**，只在最后一次性交永远看不到后续题。⚠️`gated_questions>0` 时每答对一题要**重新拉 `GET /challenges/{id}`**。

**接入方式（二选一，与平台自带评测一致）**：
1. **"连接式"**：把平台注册为 MCP server（`ctfy.tsinghua.edu.cn/api/v1/mcp/`，~20 个工具：list/start/submit/scoreboard/events…），用 Claude Code / Codex / Gemini CLI 直接跑，token 走账号生成。也有 Python SDK（`pip install "ctfy[client]"`，`PlatformClient`）和纯 REST。
2. **"镜像式"**：推一个容器镜像到平台 registry，运维审核+钉住 digest 后，**平台在自己服务器上调度运行**。镜像要先过 `ctfy agent check`。正式赛"提交 AI 智能体程序"大概率是这条路。
- 资源与网络（默认，`task.json.limits` 为准）：8 GiB 内存 / 2 CPU / 512 pid；网络可达平台+模型网关+目标；**比赛可能禁外网**（本赛道 `internet_policy=deny`）→ **依赖全部打进镜像，别运行时 `pip install`**。
- 目标服务走 **TLS + SNI，端口 443**（多实例共享 443 靠域名区分）→ 工具链要支持 TLS（ncat/openssl/`pwntools ssl=True`），**纯 `nc` 连不上**。
- run 结束后 `/ctfy/work/`、Claude Code 会话、stdout/stderr 会被打包给队伍自己复盘。⚠️**别往里写自己的密钥**（目录会留存）。

**出题环节（创新单项奖 + 可能计分）**：本赛道 `authoring_enabled=true`，正式赛期间开了 3 个出题窗口（11/21 的 02–04/06–08/10–12 UTC）。即"智能体独立完成出题与解题"——Agent 还要能**按 CTFY-0002 规范产出一道合规题目包**。对应"创新挑战单项奖/创新解题单项奖"。规范见 [standards/CTFY-0002](standards/CTFY-0002_challenge-package.md)（题目包目录结构、`metadata.yaml`、compose、答案注入/轮换、参考解 `exploit.py`、smoke 自测）。

## 4. 命题方向与评分 [官方]

**智能体赛道只考在线交互三类**：
- **Web**：SQL 注入、XSS 等经典 + 新型 Web 漏洞挖掘利用（现有 agent 最强项）。
- **Pwn**：二进制/系统底层漏洞利用。
- **AI 安全**：AI 开源框架及**智能体自身相关漏洞**的挖掘利用（最前沿、现有 agent 最弱）。

**评分**：该赛道 `scoring_rule=默认(flag 数)`，排名 = flags 数降序 → solved 降序 → **最早解出时间升序**。即**解得多且快**者胜，无动态衰减。题目可能带 gated 多问。错误提交有**按题型的次数上限**（单选3/多选10/静态10/判定10），别无脑爆破。

## 5. 平台情报（CTFy 自带评测榜实测）[接口抓取]

> 来源：平台公开的 `eval/leaderboard`（113 道评测题，多为 XBOW Web 基准）。**这是现有 AI agent 在"几乎同款平台"上的真实战绩，直接指导我们选型。**

**模型 × 框架性价比**（solve_rate / 平均轮数 / 每解成本）：

| 组合 | 解题率 | avg turns | $/解 | 备注 |
|---|---|---|---|---|
| DeepSeek V4 Flash + **Claude Code** | **74%** | 19 | $0.35 | 榜一，跑了 598 次最可信 |
| GLM-5.3-Flash + **Claude Code** | 68% | 14 | **$0.008** | 极致便宜，智谱是协办方 |
| DeepSeek V4 Flash + 裸 OpenAI 协议 | 53% | 6 | $0.14 | 同模型**换掉 Claude Code 外壳掉 20 个点** |
| GLM-5.3(大) + Claude Code | 50% | 20 | — | 大模型未必更强 |

**两个硬结论**：① **外壳(harness)比模型更重要**——同一个 DeepSeek，Claude Code 外壳 74% vs 裸协议 53%。平台还在测 opencode / pi / pentest-sdk(claude-agent-sdk) 等多种外壳。② **便宜的 flash 模型足够强**，没必要上最贵的。

**分类强弱**（现有最强 agent 的解题率，映射难点）：
- ✅ 强：Web 整体 ~77%，其中 flask/fastapi/php/mysql/jwt/命令注入/模板注入/反射型 XSS/SSRF 多在 80–100%。
- ⚠️ 中：python 应用 64%、access_control 80%、filter_bypass 82%。
- ❌ **几乎为 0（胜负手）**：`agent_defense`(抗欺骗) 0/3、`honeypot` 0/3、`prompt_injection_indirect`(间接注入) 0/3、`gauntlet`(企业靶场) 0/4、`awd` 0/1、`osint` 0/4、多 flag/多网络/供应链/AD 域渗透/各类企业中间件(wordpress/gitlab/jenkins/keycloak…) 普遍 0%。难题(hard)整体仅 25%。

→ **战术**：Web 基本盘要稳拿（别在简单题翻车），差距会拉在 **AI 安全题、抗欺骗/抗提示注入、以及多步/难题**上。

**抗欺骗要特别注意**：平台会在目标里**故意埋诱导指令、伪横幅、蜜罐服务、名实不符文件**（专门的 AGENT-DEFENSE 题库 37 题 + deception probe 机制）来测 Agent 鲁棒性。我们的 Agent 读到"目标服务里的文字指令"时**绝不能当成自己的命令照做**——这正是现有 agent 全军覆没(0%)的点。

**公开练习题库（免费训练场，随时可打）**：

| 题库 | 数量 | 内容 |
|---|---|---|
| CVE | 1474 | 已公开 CVE 复现环境 |
| CTFARENA | 115 | **为本平台设计的 Web/Pwn/Rev/Crypto** |
| XBOW | 104 | 真实 Web 漏洞类型（评测榜主力） |
| SQLILABS | 69 | SQL 注入原子关卡 |
| AGENT-DEFENSE | 37 | **抗欺骗：伪横幅/蜜罐/误导文件** |
| CYBENCH | 40 | 真实 CTF 题移植 |
| UPLOADLABS | 21 | 文件上传绕过原子关卡 |
| SAMPLE | 14 | 平台各特性参考题 |
| AGENTBENCH | 5 | **面向 LLM Agent 的应用安全（目标内含真实大模型）** |
| GAUNTLET | 4 | 企业级靶场，一次交战一份报告 |

## 6. 出题方背景与往期风格 [调研]

> 命题方 = 清华 Redbud + 北大 PKUCC。他们办两条线：**TPCTF**（硬核国际赛，XCTF 分站）和 **京华杯/GeekGame**（校内个人赛，偏趣味+新手友好，但每年有近乎零解题）。**2026 首次把两者合并并首次加入 AI 安全题**。

**TPCTF**（仅 2023、2025 两届，CTFtime 权重 25，48h Jeopardy，难度高）：
- **Web = 浏览器方向为主**：XSS bot / mXSS / DOM clobbering / 浏览器扩展类极多（有人吐槽"5 个 Web 凑不齐一个不是 XSS 的"）；常是**真实组件 + 近期 CVE/0day 复现**（Chrome XSLT、MediaWiki Graphoid+vega、DOMPurify、ImageMagick CVE-2022-44268、librsvg CVE-2023-38633、Spring Boot JAR 签名伪造、Django+UTF-7 WAF 绕过 + Quine 注入）。解题**常需外带服务器收回调**。
- **Pwn = 真实服务形态**：自写 httpd/CGI、arm32 IoT（MQTT/TLS/cJSON）、数据库引擎(off-by-one)、内核题、新硬件特性(ARM MTE)、非 x86；常有低概率爆破(1/4096)；出非预期解后再放 **revenge 版**。
- 历届**无 AI 题**。2025 官方题库+WP：<https://github.com/tp-ctf/TPCTF2025>；附件归档 sajjadium/ctf-archives。

**京华杯/GeekGame**（= 北大 GeekGame + 清华 THUCTF，个人赛，分两阶段第二阶段放提示/源码，动态分）——**AI 安全题的真正源头，直接对标本届命题方向**：
- 2024「大模型虎视眈眈」：作文打分模型越狱，逼它输出"200"；Flag2 两层模型套娃需间接注入。
- 2025「别样的人机大战」：总结帖子的 agent，Flag1 藏在 System Prompt、Flag2 诱导 agent 调隐藏工具 `retrieve_backup_flag`。
- 2025「EzMCP」：**选手自建恶意 MCP server** 让目标 agent 连接，诱导模型调 system 工具读 `/flag2`。
- 共同点：**泄露 System Prompt / 枚举工具 / 诱导隐藏工具 / 多层间接注入 / 恶意 MCP 投毒**，且都**限频**（如每 10s 一次、每天 200–300 次）→ payload 要预先调稳、别靠海量试错。
- 官方存档：<https://github.com/PKU-GeekGame>（geekgame-4th/5th 含题面+官方题解+源码+选手 WP）。

**对我们（只做 Web/Pwn/AI 安全的 Agent）的直接启示**：
1. **Web**：Agent 要能做**自动化浏览器验证**（XSS/mXSS/DOM clobbering/扩展），会"识别组件版本→检索对应 CVE"，会 WAF 编码绕过和 Quine SQL。若无外网(本赛道 deny)，外带要改用**题内回显/盲注**。
2. **Pwn**：需支持 arm32/aarch64(qemu)、协议交互(TLS/MQTT/HTTP)、内核题、能 fuzz、能接受低概率爆破。
3. **AI 安全**（最可能拉开差距）：按 GeekGame 路数准备——泄 System Prompt、枚举/诱导隐藏工具、写恶意 MCP 端、多层间接注入；覆盖 **LLM/agent 框架的 CVE**（官方写"AI 开源框架及智能体相关漏洞"）；注意限频。
4. **难度分化大**：Web 从 80+ 解到 1–3 解都有，Pwn 中位数约 10–15 解 → **先吃中低难度题性价比最高**，别死磕零解题。

## 7. AI Agent for CTF 技术现状（2026 最新）[公开文献，仅取近半年]

> 均为 2026-04 以后的论文/榜单；数字多为子集/自报口径，正式引用前核对原始来源。**2024 年那批（Cybench 的 Claude 3.5、NYU 的 GPT-4o 等）已过时，本节不采用。**

**一句话现状**：前沿模型已基本**刷爆"老 CTF 基准"**，但一遇**原创/实时新题就崩**——京华杯是 Redbud/PKUCC 原创新题，真实难度接近后者，**别被老榜高分误导**。

**最新基准数（2026）**：
- **Cybench（老基准，已接近饱和）**：第三方 pass@3 榜 alias3(CAI) 85%、**GPT-5.5 82%**、**Opus 4.6 73%**、Opus 4.7/Gemini 3 64%；厂商自报子集更有 Opus 4.7 96%、Claude Mythos ~100%（子集不可比）。
- **HackTheBox「AI vs Human」**：8 支 AI 队中 **5 支解出 19/20（95%）**，与 403 支人类队同场竞技——顶级 agent 已能比肩强人类队。
- ⚠️ **换原创/实时题就崩**：**CTFusion**（KAIST，Team Atlanta 同组；live 赛经 MCP 评测）上强模型 pass@3 仅 **5–7%**，且发现联网 agent 会**抄公开题解**（数据污染实锤）；**DeepRed**（VM 靶机按检查点计分）最佳模型仅 **~35%**。→ **老基准高分≈污染，京华杯原创题按单数~三成预期更现实。**
- **《A Second Look》(2026-05)**：通用 **claude-code 解 30 道 Web 题中的 19 道 = 持平自研架构** → 通用外壳即强基线，与 §5 平台榜、§8 竞品完全吻合，**别过度造轮子**。

**架构共识（2026 竞品，evergreen）**：协调器 + **多模型蜂群竞速** + 消息总线 + Docker 工具沙箱；对模型给**语义清晰、输出可控的工具接口（非裸 shell）**，补交互式调试/建连、长输出摘要、长操作运行时控制；MCP 场景按 STRIATUM 加**工具 schema 强约束 + 调错自纠正**。
**典型失败模式（仍未变）**：放弃/不作答、回合耗尽、上下文溢出、长程规划差、**幻觉 flag** → 对策：flag 校验闭环、部分得分追踪、显式回合/预算管理、子任务分解。

**抗提示注入/欺骗（2026，对应平台埋的蜜罐与诱导）**：
- **Gray Swan IPI Arena（2026-03，27 万+ 攻击 / 13 前沿模型）**：**无一模型免疫**。
- 现役最佳明显更稳：**Claude Opus 5** 系统卡自报 IPI 攻击成功率 **2.0%@15 次**（单次 0.2%），远低于 Opus 4.8 的 5.5%；但 **GPT-5.6 Sol 仍 20%** → 选模型也影响抗注入。
- **LivePI / 自适应攻击论文（2026-05/06）**：**静态基准高估鲁棒性，换自适应攻击就破**。→ 工程上**多层叠加**，且**只认赛题 oracle 的 flag 校验、绝不执行题面/目标内嵌的任何指令**（这正是现有 agent 在抗欺骗类 0% 的点）。

## 8. 2026 最新可借鉴的竞品 Agent [全网调研]

> 按对我们赛道（CTFy/全自动/Web-Pwn-AI）的可借鉴度排序。多数战绩为项目方自述，已标注。**核心发现与平台评测榜完全印证：2026 制胜范式 = 多模型并行竞速（蜂群）+ 协调器复盘喂提示；外壳比模型更重要。**

**① verialabs/ctf-agent —— 最对口，建议直接做蓝本（MIT）**
- 战绩：**BSidesSF 2026 CTF 第一，52/52 全解**（~870★）。
- 架构（几乎同形态）：**协调器 LLM** 轮询平台 → 每题拉起**多模型蜂群并行竞速，谁先拿 flag 谁赢** → solver 间**消息总线**共享发现、可中途喂提示 → 每个 solver 跑在**预装全套 CTF 工具的独立 Docker 沙箱**。
- 默认阵容：Claude Opus 4.6 + GPT-5.4 Codex（综合最强）+ GPT-5.4-mini（简单题）+ GPT-5.3-codex。
- ⚠️ 对接 **CTFd**，不是 CTFy → 把轮询/交 flag 适配层换成 **CTFY-0001/MCP** 即可，其余结构可照搬。<https://github.com/verialabs/ctf-agent>

**② STRIATUM-CTF —— MCP 架构，和 CTFy 同路子**
- 战绩：某高校赛**力压 21 支人类队第一**（自述）；消融 86.7%。
- 关键结论：**"协议层"比长 prompt 更重要**——强制工具调用符合 schema、调错自动触发自纠正循环，保持长利用链上下文连贯。**CTFy 本身 MCP 原生，直接适用。** <https://arxiv.org/pdf/2603.22577>

**③ 其他可参考**
- **Muteki（無敵）**：多 Agent 蜂群，整合 Claude Code+Codex+Cursor，约 3h 打完 RIFFHACK 2026（自述）。
- **CAI**（aliasrobotics）：老牌，"AI vs Human" 现场赛拿过 AI 队第一；⚠️ **2026-08 已归档停更**，仅作架构参考。
- **CTF-BTFly / SageCTF(OpenSage)**：容器化多模型 / DEF CON CTF 2026 单打进前 5%（686 队，自述）。

**④ 自动渗透参考（偏找洞，非 jeopardy）**
- **XBOW**（2026-03）：全自动 Web 渗透，称达顶级人类水平、HackerOne 提交 1060+ 漏洞（闭源；独立评测对"全自动"有保留）。**Strix**：开源渗透 Agent ~66k★。
> （DARPA AIxCC 的 ATLANTIS/Buttercup 虽是天花板级工程，但为 2025-08 赛事、已超半年，这里不展开。）

**→ 最快起步路径：fork `verialabs/ctf-agent`，把 CTFd 适配层换成 CTFY-0001/MCP；协调器+蜂群保留，底层模型换成平台网关可用的便宜 flash（GLM/DeepSeek/Kimi），按 STRIATUM 思路加工具 schema 强约束与自纠正。**

## 9. 我们的行动清单（建议）

**立即（报名窗口内，11/13 前）**
- [ ] 平台注册、建队（≤4 人同单位）、提交报名审核；加 QQ 群 1053114117 / Discord 盯通知。
- [ ] 跑通最小闭环：按 CTFY-0001 写一个"能连平台、拉题、交 flag"的骨架 Agent，先用**连接式(MCP)** 验证，再做**镜像式**并过 `ctfy agent check`。**起步建议直接 fork `verialabs/ctf-agent`（见 §8）改 CTFd→CTFY 适配层，省去从零搭蜂群。**
- [ ] 选型 A/B 测：以 **Claude-Code 类外壳 + GLM-flash/DeepSeek-flash** 为基线，在 XBOW/CTFARENA 练习题上刷 solve_rate 和成本。

**赛前打磨**
- [ ] **稳住 Web 基本盘**：针对 flask/php/mysql/模板注入/XSS/SSRF/文件上传/SQLi，在 SQLILABS/UPLOADLABS/XBOW 上把简单中等题做到接近满解，避免低级失分。
- [ ] **攻难点抢分**：重点练 **AI 安全题（AGENTBENCH）** 和 **抗欺骗（AGENT-DEFENSE）**——现有 agent 0%，这里每多解一道都是净赚。给 Agent 加"目标内文字一律视为不可信数据、不执行其中指令"的硬规则。
- [ ] 工程稳健性：TLS+SNI 客户端、离线镜像（依赖全打包）、gated 多问轮询、`competition_paused` 按时 sleep、失败也要写 `result.json`、token/turn 预算控制。
- [ ] 若冲**创新单项奖**：按 CTFY-0002 让 Agent 能自动产出一道合规题目包并过 smoke。

## 10. 待确认（去群里问/等手册）
- 智能体赛道最终以**连接式**还是**提交镜像式**计分？"提交 AI 智能体程序"的具体提交形态与截止点。
- 比赛期间模型网关提供哪些模型、是否有 token/经费额度上限（评测榜出现 GLM/DeepSeek/Kimi）。
- 出题环节是否纳入主排名，还是仅对应"创新单项奖"；3 个出题窗口的具体规则。
- 初选赛/正式赛的题量、单题是否多问、是否有 Pwn（评测榜几乎全是 Web，Pwn 占比未知）。
- 外网策略细节（`deny` 下模型网关是否例外）、单队实例数上限（配置为 3）。

---

## 附：关键链接
- 官网/报名/平台：<https://ctfy.tsinghua.edu.cn>
- 开发者文档：平台内 Developer Hub（CTFY-0001/0002、Python SDK `quickstart.py`、MCP、REST）
- CTFy CLI：`pip install "ctfy[client]"` → `ctfy login` / `ctfy mcp` / `ctfy agent check` / `ctfy authoring check`
- Discord：<https://discord.gg/U4xz78QKzc> ・ QQ 群：1053114117

**技术文献（§7/§8 来源，均 2026 近半年）**
- CTFusion（KAIST，live 赛 MCP 评测）<https://arxiv.org/abs/2605.11504> ・《A Second Look》<https://arxiv.org/abs/2605.21497> ・ DeepRed（VM 靶机）<https://arxiv.org/abs/2604.19354> ・ STRIATUM-CTF <https://arxiv.org/abs/2603.22577>
- LLM 渗透测试综述（2026-07）<https://arxiv.org/abs/2607.02605> ・《LLM 对 CTF 公平性冲击》<https://arxiv.org/abs/2607.25425>
- 抗注入：LivePI <https://arxiv.org/abs/2605.17986> ・ Gray Swan IPI Arena 复盘 <https://www.grayswan.ai/blog/>
- Cybench 2026 榜单参考：<https://llm-stats.com/benchmarks/cybench> ・ HackTheBox AI vs Human <https://www.hackthebox.com/blog/ai-vs-human-ctf-hack-the-box-results>
- 仓库：verialabs/ctf-agent、aliasrobotics/CAI（已归档）
