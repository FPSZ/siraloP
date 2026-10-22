# ADR-0003：开发约定（测试、工具链、协作）

- 状态：**已接受**
- 日期：2026-10-22
- 决策者：战队成员（京华杯 TPCTF 2026 智能体赛道）
- 相关：[ADR-0001 智能体整体架构](0001-agent-architecture.md)、[ADR-0002 domain 内部分块](0002-domain-block-architecture.md)、[CTFY-0001 协议](../standards/CTFY-0001_agent-protocol.md)

---

## 1. 背景

> 范围声明：本项目是**授权高校网络安全竞赛**（京华杯 TPCTF 2026）的参赛作品，所有行为均限定在赛事平台提供的题目环境内、用于自动完成平台布置的赛题（challenge）并由平台评测器（grader）判定，不涉及任何平台外目标。

多名队员与 AI 编码智能体在硬性赛程压力下**并行**开发同一套后端。只有当边界**显式且可被机器校验**时，并行才真正带来提速；否则合并热点（共享数据模型、端口签名）与悄悄发生的契约漂移会把提速吃掉。此外有三条绝不能破的底线：任何一次运行都必须产出 `result.json`、绝不泄露凭证、绝不上传 `external-agents/` 下的外部研究资料。

本 ADR 固化测试、工具链与协作三方面的约定，让这些边界能被 `python tools/check.py` 与 CI 自动守住，而不是只靠自觉。

## 2. 决定

**1. 端口（Port）是冻结契约（frozen contract），domain 保持纯净。** `agent/ports/*` 与 `agent/domain/state.py` 只在**深思熟虑后**改动（先出 ADR + 周知，或以独立小 PR 落地）。`agent/domain/` 只依赖 `ports` 抽象与 `state` 数据模型；**绝不** import 适配器（adapter）、组合根 `wiring`、入口 `entrypoints`，也不碰 `httpx`、`anyio`。这不靠自觉，而由 **import-linter**（配置见 `[tool.importlinter]`，命令 `lint-imports`）强制三条依赖方向：

- `agent.domain` 禁止 import `agent.adapters` / `agent.wiring` / `agent.entrypoints` / `httpx` / `anyio`；
- `agent.ports` 禁止 import `agent.adapters` / `agent.wiring` / `agent.entrypoints` / `httpx`；
- `agent.adapters` 禁止 import `agent.wiring` / `agent.entrypoints`。

**2. 先造假件（fake），再按契约测试。** `agent/testing/fakes.py` 为每个端口提供一个忠实的内存假件（fake）。每个适配器都配一个 `tests/contract/` 契约测试（用假件或 `httpx.MockTransport`），证明它确实遵守所属端口；更细的单元测试放在 `tests/unit/`。**任何测试都不得触碰真实网络或真实凭证。** `pytest` 以 `asyncio_mode = "auto"` 运行，异步测试无需加装饰器。

**3. 单一闸门。** `python tools/check.py` 把 ruff、import-linter、pytest 列为**必过**，把 mypy 列为**仅告警**（类型逐步补齐：进程级 `os.*` 调用与平台相关，目前只在 Linux 上类型干净）。CI（`.github/workflows/ci.yml`）在 push / PR 上跑同一道闸门。常用命令：

```bash
python -m pytest -q      # 测试
ruff check .             # 代码风格
lint-imports             # 架构依赖
python tools/check.py    # 一键全检：ruff + import-linter + pytest（+ mypy 告警）
```

ruff 规则见 `[tool.ruff]`（`line-length = 100`、`select = ["E", "F", "I", "UP", "B"]`）；其中 `UP042` 被显式忽略——`domain/state.py` 刻意使用 `(str, Enum)` 写法，而 UP042 改写成 `StrEnum` 会改变 `str()` 与 JSON 序列化行为。

**4. 文档放在工作现场。** 一份权威的根 `AGENTS.md`（被 `CLAUDE.md` 以 `@AGENTS.md` 引入），加上各目录下的 `AGENTS.md`（`agent/domain/`、`agent/ports/`、`agent/adapters/`、`tests/`；越深越具体者优先，工具读最近的一份）；`CONTRIBUTING.md` 讲协作机制；`docs/llms.txt` 作为面向机器的索引。语言分工见下一节。

**5. 协作护栏。** PR 小而单一目的；文件归属由 `.github/CODEOWNERS` 划定；`.github/pull_request_template.md` 提供自检清单。数据模型（`agent/domain/state.py`）变更单独成一个小 PR，再在其上叠加别的改动。`.env`、各类密钥、`external-agents/` 均已 gitignore，**绝不提交**；`result.json` 在写出前会抹除已知凭证。

**6. 不可让步的运行时保证。** 镜像模式（image mode）入口 `python -m agent.entrypoints.image_main`（CTFY-0001 §4）在**每一条**退出路径（正常、异常、取消）都写出合法 `result.json`，并**始终 exit 0**（CTFY-0001 §3：不写文件=判失败；平台把非零退出当崩溃）。结果写出（ResultSink，实现见 `agent/adapters/result/ctfy_result.py`）是同步的、原子的（临时文件 + `os.replace`）、永不抛异常。可用 `CTFY_EVAL_DRY_RUN=1` 做空跑（dry-run）自检：不调用模型，只验证容器能启动并产出一份合法结果。

## 3. 文档与语言约定

同一仓库里，文档有两类读者，语言分工据此确定：

- **面向人阅读的文档一律用中文**：ADR（`docs/adr/*`）、`README.md`、`CONTRIBUTING.md`。保留不译的仅限：代码块、shell 命令、文件路径、标识符（类/函数/变量/模块名）、环境变量名（`CTFY_*` / `LLM_*`）、URL 以及 `flag`。
- **面向 AI 智能体的文件保持英文**：根与各目录的 `AGENTS.md`、`CLAUDE.md`、`docs/llms.txt`。这类文件用英文编写，并以短句、命令、规则、边界为主（而非长篇散文），以最大化智能体的执行效率；不翻译成中文。
- **提交信息（commit / PR）用中文**，主题用祈使句（如 `feat: 新增 CtfdAdapter`、`fix: 遵守 Retry-After`），并保留 `Co-Authored-By` 尾注。

依据：面向人与面向机器的文档目标不同——前者要可读、好校对、便于评审，后者要让智能体以最少 token 命中规则与边界。两者混用一种语言反而都做不好，故按读者分语言。

## 4. 后果

- 方向错误的 import 或被破坏的端口契约会**当场在闸门失败**，而不是几天后以诡异的运行时 bug 冒出来。
- 新贡献者（无论人还是 AI）有一份短小、就近、权威的规则集，改动一处切片无需通读整棵代码树。
- mypy 当前不阻塞；收紧它（去掉 `ignore_missing_imports`、修好平台相关的 `os.*` 类型）是一项已登记的后续工作。
- 围绕 `ports/` 与 `state.py` 的一点"仪式感"，是安全并行的代价——这是有意为之。
- 文档分语言会带来少量维护成本（面向人、面向机器两套风格并存），但换来人读得顺、智能体跑得准，这笔开销值得。
