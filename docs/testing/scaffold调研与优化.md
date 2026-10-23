# Scaffold 调研与优化清单（2026-10-23）

## 为什么做这次调研

第一次测试（见 [测试记录.md](测试记录.md)）发现：失败几乎全在 CTFARENA 访问控制类，且
失败时**狂刷错 flag**。同模型对照证实瓶颈在 **scaffold 不在模型**——ctfy-agent 失败并
错提交 20~29 次的 CTFARENA-006/002/004，dsh（**同一个 deepseek-flash**）全部干净解出
（见测试记录「同模型对照」）。于是调研两路、只取**近半年**资料：
1. **dsh v0.2.1 源码**逐层读（它是被实证更强的现成参照）；
2. **2026-04 以后**的公开 CTF/pentest-agent SOTA 论文。

## 核心判断：两路独立来源收敛

最高杠杆的不是换模型、也不是堆提示词，而是三件事，dsh 实现与 2026 论文**一致指向**：

1. **观察截断是首要短板**。2026 多篇论文把失败归因于「解析非结构化 telemetry」——而我们
   每轮把观察尾截到 500/1200 字符，恰好把模型需要的结构化事实丢了。dsh 的做法相反：
   **保留完整历史，只对超大结果（>8192 字符）做头+尾裁剪（留头 4096 + 尾 1024、裁中间）**。
   我上一版移植把头+尾阈值设成 500，把小文件中段都掐了——方向之一对、参数全错（已回退）。
2. **提交纪律**。狂刷错 flag 是**结构性**失败（2026 多个开源框架都有），不是个案。要把
   `submit_flag` 变成受控动作：格式门 + 证据门（flag 必须在**真实工具输出**里出现过）+
   去重 + 硬性次数上限 + 到顶即认输。
3. **plan 要承载「诊断」而非仅「清单」**。2026 测得「planning–execution 解耦比模型规模更重要」；
   把「当前漏洞假设 / 已排除 / 下一步验证」放进跨轮持久的 plan 槽，是识别瓶颈的解法。

反面同样重要：**别堆提示词**。2026 实测 security-specific 长提示词**反而更差**（默认提示词
胜过安全定制版），4147 行 vs 55 行工具文档**无显著差异**。这**验证了我们现有的极简提示词**，
并否决了「往 prompt 里塞大段 web 侦察清单」的冲动——侦察应进 **plan 模板**，不进系统提示词。

## 现状 vs 发现（逐条，带我们代码位置）

| 维度 | 我们现在 | dsh / 2026 SOTA | 差距 |
|---|---|---|---|
| 观察截断 | 每条尾截 500/1200（`solver.py` `_tail`/`_HISTORY_OBS`） | dsh：>8192 才裁、留头4096+尾1024；SOTA：压成 findings 别丢中段 | **大**，首要修 |
| 提交纪律 | 已拒记忆（按 qid）+ 平台 cap；无"必须见于真实输出"证据门、无本地硬上限→认输 | 格式门+证据门+去重+硬上限+GIVE_UP | **大**，直击错29 |
| plan | 模型维护 todo（`prompts.py` PLAN_FORMAT），只是清单 | 承载 current_hypothesis/ruled_out/confirmation_step | 中 |
| 工具反馈 | exit+cwd+stdout/stderr+`(no output)`（`solver.py` `_parse_observation`） | dsh：`[exit code:N]` 契约 + spill 文件指针 + "失败先查"；SOTA：先给 findings 行 | 中 |
| 防循环 | stall 计数 + 命令指纹 set（`solver.py`） | dsh 重复提醒[3,5,8]（精确 tool+args、仅追加、用户发言重置）；SOTA：杀重复**不杀探索** | 中（注意别误杀探索）|
| 系统提示词 | 极简（`prompts.py`） | SOTA：短提示词更好，别扩 | **已对**，保持 |
| 上下文 | 极简重建（system+goal+plan+last-8） | SOTA 认同"长 transcript 退化"，但要配 findings 提取 + 滚动 current-state | 基本对，补 findings |
| 侦察清单 | web 块一句泛述 | 进 plan 模板（attack_surface 扫描→entry points），不进提示词 | 小 |

## ROI 可落地清单（均在 domain/dev 内，不碰 frozen ports / state.py）

1. **【最高】重做观察截断**：别再每条尾截。小输出完整保留；仅对超大输出做头+尾（参照 dsh
   8192/4096/1024 量级，而非 500），中段用标记省略；长输出落盘并在观察里给"完整见 <path>"指针。
   更进一步：在 exit 行后加一行**提取的 findings**（端点/参数/状态码/版本/报错）。
2. **【最高】提交证据门 + 硬上限**：`submit_flag` 前校验——(a) 匹配 flag 正则；(b) 该串必须在
   最近 N 条**真实观察**里出现过（不接受只在模型"想法"里的）；(c) 去重（已 (qid,flag) 记忆已做）；
   (d) 每题累计提交硬上限，到顶转纯探索/认输。非法则不耗 grader 次数、回注合成观察。
3. **plan 升级为诊断对象**：加 `current_hypothesis / ruled_out / confirmation_step`，每轮更新；
   `ruled_out` 同时当防循环记忆。
4. **防循环**：加"距上次新 finding 的轮数"计数 + 命令重复指纹，触发注入"换假设/标记死路"提醒；
   step/retry 上限留在 harness。**注意**：2026 多个 benchmark 里"探索不足"是头号失败模式，
   所以**杀重复、别杀探索**，整体预算宜松。
5. **工具反馈补强**：观察里 exit 非 0 时明确提示"先排查失败再继续"；补落盘指针。
6. **失败分类 + 提交前自检**：OBSERVE 判失败时让模型一行分类（工具错 vs 策略错）再分支；
   `submit_flag` 前强制一轮"指出证明 flag 成立的那条观察"。预算够时可 best-of-k 新上下文重试。
7. **侦察进 plan 模板**：turn-0 按类别把通用攻击面扫描写进 plan（枚举端点/参数→源码/注释/JS 泄露→
   鉴权/会话→逐参数试 IDOR/注入/LFI·SSRF·`file://`/模板·反序列化），**保持系统提示词短**。

## 单条最大矛盾点

不是"极简上下文"本身（那被验证是对的），而是**每条观察的尾截断**——它正好丢掉 2026 认定的
头号失败原因（解析非结构化 telemetry）所需的结构化事实。不改这条，别的都事倍功半。先改 #1。

## 来源（均近半年，primary，带日期）

- Hackers or Hallucinators?（arXiv 2604.05719, 2026-04）：8/13 框架会幻觉 flag；换更强模型不消除（结构性）。
- A Second Look（arXiv 2605.21497, 2026-05）：漏洞识别是首要瓶颈；加 Planner 是最大单项提升（23/30 识别对）。
- CTFExplorer（arXiv 2602.08023v3, 2026-05）：提交带 exploration level + evidence type；dead-end 标记；预算 50%/80% 反思。
- Decoupling Reconnaissance and Exploitation（arXiv 2606.25332, 2026-06）：给准确漏洞上下文→~90%，自主侦察召回~50%，差在"解析非结构化 telemetry"。
- Baselines Before Architecture（arXiv 2607.13085, 2026-07）：security-specific 提示词反而更差；持久探索比省成本更重要；朴素 agent 在 XBOW 上追平专用 harness。
- When Agents Do Not Stop（arXiv 2607.01641, 2026-07）：别靠模型自停，step/retry/超时一律 harness 强制。
- A Survey of LLM-Driven Penetration Testing（arXiv 2607.02605, 2026-07）：planning–execution 解耦 > 模型规模；Summarizer 压 findings；失败分类与规划同等重要；STRIATUM 4147-vs-55 行提示词无显著差异。

（背景、非近半年、不作当前依据：EnIGMA 2409.16165、Cybench 2408.08926、PentestGPT 2308.06782、
Project Naptime 2024；其 ACI/验证/采样思想已被上面 2026 工作继承。）

dsh 源码参照（本机 D:\AI\deepseek-harness v0.2.1）：观察裁剪 `compaction-tool-result-pruner`
(8192/4096/1024)、bash 反馈 `tool-bash/render.ts`（`[exit code:N]` + `(no output)` + spill 指针）、
重复守卫 `guard/repeat-tool-reminder`（[3,5,8]）、运行时上下文 `runtime-context.ts`（supersede-in-place）。
