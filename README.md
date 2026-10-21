# ctfy-agent

授权高校网络安全竞赛（**京华杯 TPCTF 2026 · 智能体赛道**）的参赛作品：一个在赛事平台提供的题目环境内、无人干预自动完成挑战的容器化 AI Agent。所有行为限定在平台布置的题目范围内。

架构见 [docs/adr/0001-agent-architecture.md](docs/adr/0001-agent-architecture.md)（Python + 六边形）与 [docs/adr/0002-domain-block-architecture.md](docs/adr/0002-domain-block-architecture.md)（domain 内部四块）。

## 结构（六边形）

```
agent/
  domain/        纯解题逻辑(零 I/O)：coordinator / swarm / solver / state / bus / policy / prompts
  ports/         抽象接口(Protocol)：platform / llm / execution / result —— 契约即分工
  adapters/      具体实现(可换)：platform(ctfy_rest|ctfy_mcp|ctfd) / llm(gateway|local_key)
                 / execution(subprocess|docker_sandbox) / result(ctfy_result)
  entrypoints/   驱动入口：image_main(镜像式) / connected_main(连接式) / cli_main(本地)
  config.py      读环境变量
  wiring.py      组合根：按环境装配 adapter 注入 domain
```

依赖方向：`adapters → ports → domain`。**domain 不 import 任何 adapter。**

## 分工建议（认领一个端口/适配器即可并行）
- Platform 端口 + CtfyRest/CtfyMcp/Ctfd 适配器
- LLM 端口 + Gateway/LocalKey 适配器
- Execution 端口 + Subprocess/DockerSandbox 适配器
- domain：coordinator / swarm / solver（核心）

## 开发
```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,model,mcp,docker]"
cp .env.example .env    # 填本地 token/模型
pytest -q
# 本地连接式跑一题（先把 CtfyRest + Gateway + Subprocess 实现了）
python -m agent.entrypoints.connected_main --help
```
