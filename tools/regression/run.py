#!/usr/bin/env python
"""回归测试运行器（ctfy-agent）。

一次运行产出**一条 JSON 记录**（结构见 docs/testing/回归测试字段表.md），写入
``regression/runs/<run_id>.json``。若存在基线则逐项比对并打印 PASS/WARN/FAIL。

三档：
  smoke        —— 用 agent/testing/fakes 构造假适配器跑完整 Coordinator.run()；
                   确定性、无网络、无需 secret.env；采集字段。
  integration  —— 读 docs/secret.env → Settings.from_env() → 真实适配器只读探测
                   （whoami / list_challenges / 首题 get_challenge / scoreboard）
                   + 真调一次 gateway.complete()；不启动实例、不提交。
  solve        —— 完整 Coordinator.run() 连真实平台（开赛前会 competition_paused，
                   脚本优雅处理、不报错退出）。供开赛后用。

采集手段：非侵入的 Counting* 包装器包住各端口（PlatformPort / LLMPort /
ExecutionPort / ClockPort），统计 api_calls、各 VerdictStatus、instances_started、
每次调用耗时等——**不改** domain/ports 冻结契约，也不改 adapters。

安全：记录里绝不写入任何密钥；llm_base_host 只保留 host（urlparse().netloc）。
运行产物默认落在 regression/runs/（请在 .gitignore 加入 regression/runs/，见使用说明）。

用法：
    python tools/regression/run.py --mode smoke
    python tools/regression/run.py --mode integration --competition <cid>
    python tools/regression/run.py --mode solve --save-baseline
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform as _platform
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

# --- sys.path bootstrap: make `import agent` work when run as a script -------
# Running `python tools/regression/run.py` puts tools/regression on sys.path[0],
# not the repo root, so `import agent` would fail. Insert the repo root (two
# levels up from this file) explicitly.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent  # noqa: E402
from agent.domain.state import (  # noqa: E402
    Account,
    Challenge,
    Instance,
    LLMResponse,
    Msg,
    Question,
    RunResult,
    Scoreboard,
    ToolCall,
    ToolDef,
    Verdict,
    VerdictStatus,
)

# ===========================================================================
# 非侵入埋点：Counting* 包装器（结构实现各端口，委托给真实/假适配器）
# ===========================================================================


class CountingClock:
    """包装一个 ClockPort：now 透传，统计 sleep 次数与总等待时长。

    adapters/_resilience.with_retry 的退避、Retry-After 等待、以及 solver 的
    competition_paused 轮询都走 clock.sleep，所以这里的计数是「礼貌退避/等待」
    的一个可观测代理（在使用说明里标注为近似）。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.sleep_count = 0
        self.sleep_s_total = 0.0

    def now(self) -> float:
        return self._inner.now()

    async def sleep(self, seconds: float) -> None:
        self.sleep_count += 1
        self.sleep_s_total += max(0.0, seconds)
        await self._inner.sleep(seconds)

    async def sleep_until(self, deadline: float) -> None:
        # sleep_until 不计入 sleep_count（它多为「就绪等待」而非退避）。
        await self._inner.sleep_until(deadline)


class CountingPlatform:
    """包装 PlatformPort：统计调用次数、各方法耗时、判定分布、提交明细等。"""

    def __init__(self, inner: Any, *, t0: float) -> None:
        self._inner = inner
        self._t0 = t0  # perf_counter 起点，用于 time_to_first_flag_s
        self.calls: dict[str, int] = {}
        self.durations: dict[str, list[float]] = {}
        self.exceptions: list[str] = []
        # 提交明细：每条 {cid, qid, status, t_rel}
        self.submissions: list[dict[str, Any]] = []
        self.instances_started = 0
        self.instances_ready = 0
        self.time_to_first_flag_s: float | None = None
        # 便于组装记录时直接取用
        self.account: Account | None = None
        self.listed_challenges: list[Challenge] = []
        self.scoreboard_entries: int = 0

    def _rec(self, name: str, dt: float) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.durations.setdefault(name, []).append(dt)

    async def _timed(self, name: str, coro: Any) -> Any:
        start = time.perf_counter()
        try:
            out = await coro
        except Exception as e:  # noqa: BLE001 - 记录异常类型，向上重抛
            self.exceptions.append(type(e).__name__)
            self._rec(name, time.perf_counter() - start)
            raise
        self._rec(name, time.perf_counter() - start)
        return out

    async def whoami(self) -> Account:
        acct = await self._timed("whoami", self._inner.whoami())
        self.account = acct
        return acct

    async def list_challenges(self) -> list[Challenge]:
        out = await self._timed("list_challenges", self._inner.list_challenges())
        self.listed_challenges = list(out)
        return out

    async def get_challenge(self, cid: str) -> Challenge:
        return await self._timed("get_challenge", self._inner.get_challenge(cid))

    async def start_instance(self, cid: str) -> Instance:
        inst = await self._timed("start_instance", self._inner.start_instance(cid))
        self.instances_started += 1
        if getattr(inst, "ready", False):
            self.instances_ready += 1
        return inst

    async def submit(self, cid: str, qid: str, answer: str) -> Verdict:
        return await self._graded("submit", cid, qid, answer, self._inner.submit)

    async def submit_qa(self, cid: str, qid: str, answer: str) -> Verdict:
        return await self._graded("submit_qa", cid, qid, answer, self._inner.submit_qa)

    async def _graded(self, name: str, cid: str, qid: str, answer: str, fn: Any) -> Verdict:
        verdict = await self._timed(name, fn(cid, qid, answer))
        t_rel = time.perf_counter() - self._t0
        status = getattr(verdict, "status", None)
        self.submissions.append(
            {
                "cid": cid,
                "qid": qid,
                "status": status.value if isinstance(status, VerdictStatus) else str(status),
                "t_rel": t_rel,
            }
        )
        if status is VerdictStatus.CORRECT and self.time_to_first_flag_s is None:
            self.time_to_first_flag_s = t_rel
        return verdict

    async def scoreboard(self, competition_id: str) -> Scoreboard:
        sb = await self._timed("scoreboard", self._inner.scoreboard(competition_id))
        self.scoreboard_entries = len(getattr(sb, "entries", []) or [])
        return sb

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    # --- 派生统计 ---------------------------------------------------------
    @property
    def api_calls_total(self) -> int:
        return sum(self.calls.values())

    def verdict_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in VerdictStatus}
        for sub in self.submissions:
            if sub["status"] in counts:
                counts[sub["status"]] += 1
        return counts


class CountingLLM:
    """包装 LLMPort：统计调用次数、token、每次调用耗时。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0
        self.durations: list[float] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reasons: list[str] = []
        self.exceptions: list[str] = []
        self.actions: list[str] = []  # per turn: which tool the model called + preview

    async def complete(
        self,
        messages: list[Msg],
        *,
        model: str,
        effort: str = "medium",
        tools: list[ToolDef] | None = None,
    ) -> LLMResponse:
        start = time.perf_counter()
        self.calls += 1
        try:
            resp = await self._inner.complete(
                messages, model=model, effort=effort, tools=tools
            )
        except Exception as e:  # noqa: BLE001
            self.exceptions.append(type(e).__name__)
            self.durations.append(time.perf_counter() - start)
            raise
        self.durations.append(time.perf_counter() - start)
        self.input_tokens += getattr(resp, "input_tokens", 0) or 0
        self.output_tokens += getattr(resp, "output_tokens", 0) or 0
        if getattr(resp, "stop_reason", ""):
            self.stop_reasons.append(resp.stop_reason)
        calls = getattr(resp, "tool_calls", None) or []
        if calls:
            previews = []
            for tc in calls:
                args = getattr(tc, "arguments", {}) or {}
                arg = args.get("cmd") or args.get("answer") or args.get("flag") or ""
                previews.append(f"{getattr(tc, 'name', '?')}({str(arg)[:120]})")
            self.actions.append(" ; ".join(previews))
        else:
            text = (getattr(resp, "text", "") or "").strip().replace("\n", " ")
            self.actions.append(f"(no tool) {text[:120]}")
        return resp

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


class CountingExecution:
    """包装 ExecutionPort：统计命令数、耗时、超时数、workspace 数。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.run_calls = 0
        self.durations: list[float] = []
        self.timeouts = 0
        self.workspaces = 0
        self.exceptions: list[str] = []
        self.transcript: list[dict[str, Any]] = []  # cmd + exit + short output tail

    async def run(self, cmd: str, *, timeout_s: int = 60, workdir: str | None = None) -> Any:
        start = time.perf_counter()
        self.run_calls += 1
        try:
            res = await self._inner.run(cmd, timeout_s=timeout_s, workdir=workdir)
        except Exception as e:  # noqa: BLE001
            self.exceptions.append(type(e).__name__)
            self.durations.append(time.perf_counter() - start)
            self.transcript.append({"cmd": cmd[:300], "error": type(e).__name__})
            raise
        self.durations.append(time.perf_counter() - start)
        if getattr(res, "timed_out", False):
            self.timeouts += 1
        tail = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
        self.transcript.append({
            "cmd": cmd[:300],
            "exit": getattr(res, "exit_code", None),
            "timed_out": getattr(res, "timed_out", False),
            "tail": tail[-240:],
        })
        return res

    async def read_file(self, path: str) -> bytes:
        return await self._inner.read_file(path)

    async def write_file(self, path: str, data: bytes) -> None:
        await self._inner.write_file(path, data)

    async def new_workspace(self, challenge_id: str) -> str:
        self.workspaces += 1
        return await self._inner.new_workspace(challenge_id)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


# --- 真实 HTTP 层计数（仅 integration/solve 注入到 httpx 客户端） ----------
def _make_counting_transport() -> Any:
    """返回一个计 HTTP 请求/状态码/传输异常的 httpx 传输层（含计数器引用）。"""
    import httpx

    class _CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self.inner = inner
            self.requests = 0
            self.http_4xx = 0
            self.http_5xx = 0
            self.transport_errors = 0
            self.retryable_responses = 0  # 429/5xx/传输异常 —— api_retries 的代理
            self.retry_after_responses = 0  # 带 Retry-After 头的响应数

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests += 1
            try:
                resp = await self.inner.handle_async_request(request)
            except Exception:
                self.transport_errors += 1
                self.retryable_responses += 1
                raise
            sc = resp.status_code
            if 400 <= sc < 500:
                self.http_4xx += 1
            elif sc >= 500:
                self.http_5xx += 1
            if sc == 429 or sc >= 500:
                self.retryable_responses += 1
            if "retry-after" in resp.headers:
                self.retry_after_responses += 1
            return resp

        async def aclose(self) -> None:
            await self.inner.aclose()

    return _CountingTransport(httpx.AsyncHTTPTransport())


# ===========================================================================
# smoke：假适配器 + 完整 Coordinator.run()
# ===========================================================================

# 每个假赛题的「答案」直接从 echo 的命令回显里被 solver 当作候选 flag 提交，
# FakePlatform 默认启发式（含 "ctfy{" 即判 CORRECT）据此判对。确定性、无网络。
_SMOKE_FLAG = "ctfy{smoke_regression_ok}"


def _smoke_challenges() -> list[Challenge]:
    """三道覆盖不同 category/difficulty 的假赛题（填充 by_category/by_difficulty）。"""
    return [
        Challenge(
            id="SMK-WEB-1", name="Smoke Web", category="web", difficulty="easy",
            description="smoke web challenge",
            questions=[Question(id="smk-web-q1", prompt="find the flag", fmt="static")],
            solves=10,
        ),
        Challenge(
            id="SMK-CRY-1", name="Smoke Crypto", category="crypto", difficulty="medium",
            description="smoke crypto challenge",
            questions=[Question(id="smk-cry-q1", prompt="find the flag", fmt="static")],
            solves=4,
        ),
        Challenge(
            id="SMK-PWN-1", name="Smoke Pwn", category="pwn", difficulty="hard",
            description="smoke pwn challenge",
            questions=[Question(id="smk-pwn-q1", prompt="find the flag", fmt="static")],
            solves=1,
        ),
    ]


def _smoke_llm_script(messages: list[Msg]) -> LLMResponse:
    """确定性假模型：始终回一个 execute 工具调用，命令回显里带 flag。

    FakeExecution 默认把命令原样回显到 stdout；solver 从中抽到候选 flag 并提交，
    FakePlatform 判 CORRECT → 该题解出。给定 token 数以便预算/模型字段非零。
    """
    return LLMResponse(
        text="",
        tool_calls=[ToolCall(name="execute", arguments={"cmd": f"echo {_SMOKE_FLAG}"}, id="t1")],
        input_tokens=40,
        output_tokens=12,
        stop_reason="tool_use",
    )


def _build_smoke(
    t0: float,
) -> tuple[Any, CountingPlatform, CountingLLM, CountingExecution, CountingClock]:
    from agent.testing.fakes import FakeClock, FakeExecution, FakeLLM, FakePlatform

    clock = CountingClock(FakeClock())
    platform = CountingPlatform(FakePlatform(_smoke_challenges()), t0=t0)
    llm = CountingLLM(FakeLLM(_smoke_llm_script))
    execution = CountingExecution(FakeExecution())
    settings = SimpleNamespace(llm_model="fake-smoke-model", max_tokens=None, harness_cli="")
    return settings, platform, llm, execution, clock


async def _run_smoke(result_path: str, t0: float) -> tuple[RunResult, dict[str, Any]]:
    from agent.adapters.result.ctfy_result import CtfyResultSink
    from agent.domain.coordinator import Coordinator
    from agent.domain.state import Task

    settings, platform, llm, execution, clock = _build_smoke(t0)
    sink = CtfyResultSink(result_path, stdout_fallback=False)
    task = Task(max_turns=12, competition_id="smoke-competition")
    coord = Coordinator(
        platform, llm, execution, sink, clock=clock, task=task, settings=settings
    )
    result = RunResult(stop_reason="not_started")
    try:
        result = await coord.run()
    finally:
        sink.write(result)
    probes = {
        "platform": platform,
        "llm": llm,
        "execution": execution,
        "clock": clock,
        "http": None,
        "gateway_http": None,
        "settings": settings,
        "challenges": platform.listed_challenges or _smoke_challenges(),
    }
    return result, probes


# ===========================================================================
# integration：真实适配器只读探测 + 一次 gateway.complete()
# ===========================================================================


def _load_secret_env(path: str) -> None:
    """把 secret.env 的 KEY=value 读入 os.environ（优先 python-dotenv）。"""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"integration 需要 {path}（KEY=value 形式的平台/网关凭据），但文件不存在。"
        )
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=True)
        return
    except Exception:
        pass
    # 回退：自己按行解析（支持 # 注释、可选 export 前缀、去引号）。
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ[key] = val


def _stage_attachments(
    platform_url: str, token: str, challenge_id: str, work_root: str
) -> list[str]:
    """Download a challenge's attachments into the adapter ROOT so new_workspace()
    seeds each fresh per-lane workspace with them. Dev-only (mirrors the platform's
    image-mode /ctfy/work staging); best-effort, never raises."""
    import httpx

    ws = work_root
    os.makedirs(ws, exist_ok=True)
    base = platform_url.rstrip("/") + "/api/v1"
    headers = {"Authorization": f"Bearer {token}"}
    staged: list[str] = []
    try:
        with httpx.Client(timeout=30, headers=headers) as c:
            lst = c.get(f"{base}/challenges/{challenge_id}/attachments")
            names: list[str] = []
            if lst.status_code == 200:
                data = lst.json()
                rows = data if isinstance(data, list) else (
                    data.get("files") or data.get("items")
                    or data.get("attachments") or []
                )
                names = [
                    a["name"] for a in rows
                    if isinstance(a, dict) and isinstance(a.get("name"), str)
                ]
            for name in names:
                # keep the download inside the workspace (no path traversal)
                safe = os.path.basename(name)
                r = c.get(f"{base}/challenges/{challenge_id}/attachments/{name}")
                if r.status_code == 200:
                    with open(os.path.join(ws, safe), "wb") as fh:
                        fh.write(r.content)
                    staged.append(safe)
    except Exception:
        pass
    return staged


def _detect_qa_only(platform_url: str, token: str, challenge_id: str) -> bool:
    """Read is_qa_only from the raw challenge (the domain model drops it). Dev
    helper to pick run_mode; best-effort, never raises."""
    import httpx

    base = platform_url.rstrip("/") + "/api/v1"
    try:
        with httpx.Client(
            timeout=30, headers={"Authorization": f"Bearer {token}"}
        ) as c:
            r = c.get(f"{base}/challenges/{challenge_id}")
            if r.status_code == 200:
                return bool(r.json().get("is_qa_only"))
    except Exception:
        pass
    return False


async def _run_integration(
    result_path: str, t0: float, competition_override: str | None, secret_env_path: str
) -> tuple[RunResult, dict[str, Any]]:
    import httpx

    from agent.adapters.clock import RealClock
    from agent.adapters.llm.gateway import GatewayAdapter
    from agent.adapters.platform.ctfy_rest import CtfyRestAdapter
    from agent.adapters.result.ctfy_result import CtfyResultSink
    from agent.config import Settings

    _load_secret_env(secret_env_path)
    settings = Settings.from_env()
    cid = competition_override or settings.competition_id
    if competition_override:
        settings = replace(settings, competition_id=competition_override)

    clock = CountingClock(RealClock())

    plat_transport = _make_counting_transport()
    plat_client = httpx.AsyncClient(transport=plat_transport, timeout=30)
    platform = CountingPlatform(
        CtfyRestAdapter(
            settings.platform_url, settings.token,
            clock=clock, competition_id=cid, client=plat_client,
        ),
        t0=t0,
    )

    gw_transport = _make_counting_transport()
    gw_client = httpx.AsyncClient(
        base_url=settings.llm_base_url, transport=gw_transport, timeout=60
    )
    llm = CountingLLM(
        GatewayAdapter(
            settings.llm_base_url, settings.llm_api_key, settings.llm_flavor,
            clock=clock, client=gw_client,
        )
    )

    result = RunResult(stop_reason="integration_probe")
    challenges: list[Challenge] = []
    notes: list[str] = []
    try:
        # 只读探测，不启动实例、不提交。
        await platform.whoami()
        challenges = await platform.list_challenges()
        if challenges:
            await platform.get_challenge(challenges[0].id)
        if cid:
            await platform.scoreboard(cid)
        # 真调一次网关（短提示；LLMPort 无 max_tokens 形参，默认上限由适配器控制）。
        await llm.complete(
            [Msg("user", "reply with exactly: pong")],
            model=settings.llm_model or "glm-4.6", effort="low",
        )
        result.stop_reason = "integration_ok"
    except Exception as e:  # noqa: BLE001 - 探测失败也要写记录、优雅退出
        notes.append(f"integration probe error: {type(e).__name__}: {e}")
        result.stop_reason = f"integration_error: {type(e).__name__}"
    finally:
        result.input_tokens = llm.input_tokens
        result.output_tokens = llm.output_tokens
        CtfyResultSink(result_path, stdout_fallback=False).write(result)
        for c in (plat_client, gw_client):
            try:
                await c.aclose()
            except Exception:
                pass

    probes = {
        "platform": platform,
        "llm": llm,
        "execution": CountingExecution(SimpleNamespace()),
        "clock": clock,
        "http": plat_transport,
        "gateway_http": gw_transport,
        "settings": settings,
        "challenges": challenges,
        "notes": notes,
    }
    return result, probes


# ===========================================================================
# solve：完整 Coordinator.run() 连真实平台（优雅处理 competition_paused）
# ===========================================================================


async def _run_solve(
    result_path: str, t0: float, competition_override: str | None,
    secret_env_path: str, timeout_s: float,
    challenge_id: str | None = None, max_turns: int | None = None,
) -> tuple[RunResult, dict[str, Any]]:
    import httpx

    from agent.adapters.clock import RealClock
    from agent.adapters.execution.subprocess_exec import SubprocessAdapter
    from agent.adapters.llm.gateway import GatewayAdapter
    from agent.adapters.platform.ctfy_rest import CtfyRestAdapter
    from agent.adapters.result.ctfy_result import CtfyResultSink
    from agent.briefing import load_task
    from agent.config import Settings
    from agent.domain.coordinator import Coordinator
    from agent.domain.state import RunMode, Task

    _load_secret_env(secret_env_path)
    settings = Settings.from_env()
    if competition_override:
        settings = replace(settings, competition_id=competition_override)
    cid = settings.competition_id

    clock = CountingClock(RealClock())
    plat_transport = _make_counting_transport()
    plat_client = httpx.AsyncClient(transport=plat_transport, timeout=30)
    gw_transport = _make_counting_transport()
    gw_client = httpx.AsyncClient(
        base_url=settings.llm_base_url, transport=gw_transport, timeout=60
    )

    platform = CountingPlatform(
        CtfyRestAdapter(
            settings.platform_url, settings.token,
            clock=clock, competition_id=cid, client=plat_client,
        ),
        t0=t0,
    )
    llm = CountingLLM(
        GatewayAdapter(
            settings.llm_base_url, settings.llm_api_key, settings.llm_flavor,
            clock=clock, client=gw_client,
        )
    )

    # Dev-targeted single-challenge solve: pin the run to one challenge and stage
    # its attachments into the agent's per-challenge workspace (<root>/<cid>/),
    # which is exactly where SubprocessAdapter.new_workspace() lands, so the
    # agent's shell sees the files. This mirrors image-mode's /ctfy/work staging
    # without touching any frozen contract — it's dev plumbing in the harness.
    solve_notes: list[str] = []
    work_root: str | None = None
    qa_only = False
    if challenge_id:
        import tempfile

        work_root = tempfile.mkdtemp(prefix="solve-")
        staged = _stage_attachments(
            settings.platform_url, settings.token, challenge_id, work_root
        )
        # The platform tells image-mode the run_mode via task.json; dev must
        # mirror it. A qa_only challenge has no instance, so run it in QA mode —
        # else the coordinator tries to start an instance and 4xx's. The domain
        # Challenge model doesn't carry is_qa_only, so read it from the raw API.
        qa_only = _detect_qa_only(
            settings.platform_url, settings.token, challenge_id
        )
        solve_notes.append(
            f"targeted solve of {challenge_id}; staged attachments: "
            f"{staged or 'none'}; qa_only={qa_only}"
        )

    execution = CountingExecution(SubprocessAdapter(root=work_root))
    sink = CtfyResultSink(result_path, stdout_fallback=True)

    result = RunResult(stop_reason="not_started")
    notes: list[str] = list(solve_notes)
    try:
        # Build the task INSIDE the guarded block so a transient platform error
        # while fetching the target (e.g. a 503) is caught and still writes a
        # result — it's an infra blip, not an agent failure or a runner crash.
        if challenge_id:
            ch_obj = await platform.get_challenge(challenge_id)
            task = Task(
                run_mode=RunMode.QA if qa_only else RunMode.INSTANCE,
                challenge=ch_obj, competition_id=cid, max_turns=max_turns,
                result_path=result_path, stdout_fallback=True,
            )
        else:
            task = load_task(settings)
        coord = Coordinator(
            platform, llm, execution, sink, clock=clock, task=task, settings=settings
        )
        result = await asyncio.wait_for(coord.run(), timeout=timeout_s)
    except TimeoutError:
        notes.append(f"solve timed out after {timeout_s}s (bounded by --timeout)")
        result = RunResult(stop_reason="timeout")
    except Exception as e:  # noqa: BLE001 - 任何失败都写记录、不崩
        notes.append(f"solve error: {type(e).__name__}: {e}")
        result = RunResult(stop_reason=f"error: {type(e).__name__}")
    finally:
        sink.write(result)
        try:
            await execution.aclose()  # tear down persistent shell sessions
        except Exception:
            pass
        for c in (plat_client, gw_client):
            try:
                await c.aclose()
            except Exception:
                pass

    # 竞赛暂停：若出现 PAUSED 判定，在记录里标注（不视为错误）。
    paused = platform.verdict_counts().get(VerdictStatus.PAUSED.value, 0)
    if paused:
        notes.append(f"competition_paused seen {paused} time(s); handled as PAUSED, not error")

    probes = {
        "platform": platform,
        "llm": llm,
        "execution": execution,
        "clock": clock,
        "http": plat_transport,
        "gateway_http": gw_transport,
        "settings": settings,
        "challenges": platform.listed_challenges,
        "notes": notes,
    }
    return result, probes


# ===========================================================================
# 记录组装
# ===========================================================================


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return round(s[0], 6)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[idx], 6)


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 6) if xs else None


def _rsum(xs: list[float]) -> float | None:
    return round(sum(xs), 6) if xs else None


def _git_info() -> tuple[str, bool]:
    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=5
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except Exception:
            return None

    short = _git("rev-parse", "--short", "HEAD") or "nogit"
    status = _git("status", "--porcelain")
    dirty = bool(status) if status is not None else False
    return short, dirty


def _host_only(url: str) -> str:
    """只保留 host（netloc 去掉可能的 user:pass@），绝不含路径或密钥。"""
    if not url:
        return ""
    netloc = urlparse(url).netloc
    return netloc.rsplit("@", 1)[-1] if "@" in netloc else netloc


def _scan_secret_leak(result_text: str) -> bool:
    """True 表示未泄露。扫描 result.json 文本是否含已知密钥明文。"""
    for name in ("LLM_API_KEY", "CTFY_TOKEN"):
        secret = os.environ.get(name) or ""
        if len(secret) >= 8 and not secret.isspace() and secret in result_text:
            return False
    return True


def _assemble_record(
    *,
    run_id: str,
    mode: str,
    started_at: str,
    ended_at: str,
    total_wall_s: float,
    exit_code: int,
    result: RunResult,
    result_path: str,
    probes: dict[str, Any],
    competition_id: str,
) -> dict[str, Any]:
    platform: CountingPlatform = probes["platform"]
    llm: CountingLLM = probes["llm"]
    execution: CountingExecution = probes["execution"]
    clock: CountingClock = probes["clock"]
    http = probes.get("http")
    gw_http = probes.get("gateway_http")
    settings = probes["settings"]
    challenges: list[Challenge] = list(probes.get("challenges") or [])
    notes: list[str] = list(probes.get("notes") or [])

    # --- artifact：读回 result.json 校验 -------------------------------------
    result_written = os.path.exists(result_path)
    result_text = ""
    result_valid = False
    result_bytes = 0
    required_present = False
    answers_solved_consistent = False
    schema_version = result.schema_version
    if result_written:
        try:
            result_bytes = os.path.getsize(result_path)
            with open(result_path, encoding="utf-8") as f:
                result_text = f.read()
            data = json.loads(result_text)
            req = ("schema_version", "answers", "solved", "turns",
                   "input_tokens", "output_tokens", "stop_reason")
            required_present = all(k in data for k in req)
            result_valid = required_present
            schema_version = data.get("schema_version", schema_version)
            ans = data.get("answers", {}) or {}
            answers_solved_consistent = all(q in ans for q in (data.get("solved", []) or []))
        except Exception as e:  # noqa: BLE001
            notes.append(f"result.json parse error: {type(e).__name__}: {e}")
    no_secret_leak = _scan_secret_leak(result_text) if result_text else True

    # --- solving：按 qid→challenge 聚合 --------------------------------------
    solved_qids = list(result.solved)
    questions_total = sum(len(c.questions) for c in challenges)
    challenges_total = len(challenges)

    # 每题提交分布（从 CountingPlatform.submissions 按 cid 聚合）
    sub_by_cid: dict[str, dict[str, int]] = {}
    for sub in platform.submissions:
        d = sub_by_cid.setdefault(sub["cid"], {"submissions": 0, "wrong": 0, "capped": 0})
        d["submissions"] += 1
        if sub["status"] == VerdictStatus.WRONG.value:
            d["wrong"] += 1
        elif sub["status"] == VerdictStatus.CAPPED.value:
            d["capped"] += 1

    by_category: dict[str, dict[str, int]] = {}
    by_difficulty: dict[str, dict[str, int]] = {}
    per_challenge: list[dict[str, Any]] = []
    challenges_attempted = 0
    challenges_solved = 0
    for c in challenges:
        c_qids = {q.id for q in c.questions}
        c_solved = [q for q in solved_qids if q in c_qids]
        attempted = c.id in sub_by_cid or bool(c_solved)
        is_solved = bool(c_solved)
        if attempted:
            challenges_attempted += 1
        if is_solved:
            challenges_solved += 1
        cat = (c.category or "unknown").lower()
        diff = (c.difficulty or "unknown").lower()
        by_category.setdefault(cat, {"attempted": 0, "solved": 0})
        by_difficulty.setdefault(diff, {"attempted": 0, "solved": 0})
        if attempted:
            by_category[cat]["attempted"] += 1
            by_difficulty[diff]["attempted"] += 1
        if is_solved:
            by_category[cat]["solved"] += 1
            by_difficulty[diff]["solved"] += 1
        subd = sub_by_cid.get(c.id, {"submissions": 0, "wrong": 0, "capped": 0})
        per_challenge.append({
            "id": c.id,
            "name": c.name,
            "category": c.category,
            "difficulty": c.difficulty,
            "points": c.points,
            "attempted": attempted,
            "solved": is_solved,
            "questions_solved": len(c_solved),
            "questions_total": len(c.questions),
            "wall_s": None,            # 不可非侵入拆分到每题
            "turns": None,
            "tokens_total": None,
            "submissions": subd["submissions"],
            "wrong": subd["wrong"],
            "capped": subd["capped"],
            "stop_reason": None,
        })

    questions_answered = len({(s["cid"], s["qid"]) for s in platform.submissions})
    questions_solved = len(solved_qids)
    solve_rate = round(questions_solved / questions_total, 6) if questions_total else None

    # --- verdicts ------------------------------------------------------------
    vc = platform.verdict_counts()
    submissions_total = len(platform.submissions)
    wrong_rate = (
        round(vc[VerdictStatus.WRONG.value] / submissions_total, 6)
        if submissions_total else None
    )

    # --- budget --------------------------------------------------------------
    tokens_total = result.input_tokens + result.output_tokens
    _mt = getattr(settings, "max_turns", None)
    max_turns = _mt if isinstance(_mt, int) else None
    # Amortizing tokens over solves is meaningless with zero solves (e.g. the
    # read-only integration mode); leave it None so the comparison SKIPs it
    # instead of treating a raw token count as a per-solve regression.
    tokens_per_solve = (
        round(tokens_total / questions_solved, 6) if questions_solved else None
    )

    # --- timing（多数来自 Counting* 的 perf_counter 计时） --------------------
    model_durs = llm.durations
    connect_s = _rsum(platform.durations.get("whoami", []))
    list_s = _rsum(platform.durations.get("list_challenges", []))
    model_sum = _rsum(model_durs)
    tool_sum = _rsum(execution.durations)
    solve_s_total = None
    if total_wall_s is not None:
        solve_s_total = round(max(0.0, total_wall_s - (connect_s or 0.0) - (list_s or 0.0)), 6)
    avg_turn_latency = None
    if llm.calls:
        avg_turn_latency = round(((model_sum or 0.0) + (tool_sum or 0.0)) / llm.calls, 6)

    # --- http 派生（integration/solve 才有） ---------------------------------
    def _http(attr: str) -> int | None:
        return getattr(http, attr) if http is not None else None

    record: dict[str, Any] = {
        "meta": {
            "run_id": run_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "git_commit": _GIT_COMMIT,
            "git_dirty": _GIT_DIRTY,
            "mode": mode,
            "competition_id": competition_id,
            "competition_title": None,  # 冻结的 /me 端口只回 Account(id,name)，暂无赛事名
            "account_id": getattr(platform.account, "id", None),
            "account_name": getattr(platform.account, "name", None),
            "model": getattr(settings, "llm_model", "") or None,
            "llm_flavor": getattr(settings, "llm_flavor", "") or None,
            "llm_base_host": _host_only(getattr(settings, "llm_base_url", "") or ""),
            "agent_version": getattr(agent, "__version__", ""),
            "python": _platform.python_version(),
            "platform": _platform.platform(),
            "dry_run": bool(getattr(settings, "dry_run", False)),
        },
        "timing": {
            "total_wall_s": round(total_wall_s, 6),
            "connect_s": connect_s,
            "list_challenges_s": list_s,
            "time_to_first_flag_s": round(platform.time_to_first_flag_s, 6)
            if platform.time_to_first_flag_s is not None else None,
            "solve_s_total": solve_s_total,
            "avg_turn_latency_s": avg_turn_latency,
            "model_call_s_sum": model_sum,
            "model_call_s_avg": _avg(model_durs),
            "model_call_s_p95": _pct(model_durs, 0.95),
            "tool_exec_s_sum": tool_sum,
            "tool_exec_s_avg": _avg(execution.durations),
            "instance_start_s_avg": _avg(platform.durations.get("start_instance", [])),
            "submit_latency_s_avg": _avg(
                platform.durations.get("submit", []) + platform.durations.get("submit_qa", [])
            ),
        },
        "solving": {
            "challenges_total": challenges_total,
            "challenges_attempted": challenges_attempted,
            "challenges_solved": challenges_solved,
            "questions_total": questions_total,
            "questions_answered": questions_answered,
            "questions_solved": questions_solved,
            "fully_solved": result.fully_solved,
            "solve_rate": solve_rate,
            "gated_unlocked": None,  # 需 solver 内埋点，非侵入不可得
            "by_category": by_category,
            "by_difficulty": by_difficulty,
        },
        "verdicts": {
            "submissions_total": submissions_total,
            "correct": vc[VerdictStatus.CORRECT.value],
            "wrong": vc[VerdictStatus.WRONG.value],
            "capped": vc[VerdictStatus.CAPPED.value],
            "ungraded": vc[VerdictStatus.UNGRADED.value],
            "paused": vc[VerdictStatus.PAUSED.value],
            "wrong_rate": wrong_rate,
            "hallucinated_submits": None,  # 需 solver 内埋点
        },
        "budget": {
            "turns_used": result.turns,
            "max_turns": max_turns,
            "tokens_input": result.input_tokens,
            "tokens_output": result.output_tokens,
            "tokens_total": tokens_total,
            "tool_calls_made": result.tool_calls_made,
            "cost_usd": None,  # 需单价表；Budget.cost_usd 未经控制循环注入
            "budget_exhausted": None,
            "tokens_per_solve": tokens_per_solve,
        },
        "model": {
            "model_calls": llm.calls,
            "lanes_launched": None,   # 需 coordinator 埋点
            "escalations": None,
            "racing_used": None,
            "invalid_tool_calls": None,  # solver 熔断内部计数，非侵入不可得
            "avg_output_tokens_per_call": round(llm.output_tokens / llm.calls, 6)
            if llm.calls else None,
            "gateway_http_4xx": getattr(gw_http, "http_4xx", None) if gw_http else None,
            "gateway_http_5xx": getattr(gw_http, "http_5xx", None) if gw_http else None,
            "gateway_transport_errors": (
                getattr(gw_http, "transport_errors", None) if gw_http else None
            ),
        },
        "platform": {
            "api_calls_total": platform.api_calls_total,
            "api_calls_by_method": dict(platform.calls),
            "api_retries": getattr(http, "retryable_responses", None) if http else None,
            "retry_after_waits": getattr(http, "retry_after_responses", None) if http else None,
            "clock_sleeps": clock.sleep_count,
            "clock_wait_s_total": round(clock.sleep_s_total, 6),
            "instances_started": platform.instances_started,
            "instances_ready": platform.instances_ready,
            "scoreboard_entries": platform.scoreboard_entries,
            "http_4xx": _http("http_4xx"),
            "http_5xx": _http("http_5xx"),
            "transport_errors": _http("transport_errors"),
            "wire_requests": _http("requests"),
        },
        "stability": {
            "exit_code": exit_code,
            "result_json_written": result_written,
            "result_json_valid": result_valid,
            "stop_reason": result.stop_reason,
            "exceptions": (
                len(platform.exceptions) + len(llm.exceptions) + len(execution.exceptions)
            ),
            "exception_samples": sorted(set(
                platform.exceptions + llm.exceptions + execution.exceptions
            ))[:5],
            "tool_timeouts": execution.timeouts,
            "stalls_detected": None,  # solver 内部状态，非侵入不可得
        },
        "artifact": {
            "schema_version": schema_version,
            "required_fields_present": required_present,
            "answers_solved_consistent": answers_solved_consistent,
            "no_secret_leak": no_secret_leak,
            "bytes": result_bytes,
        },
        "per_challenge": per_challenge,
        "_notes": notes,
    }
    return record


# ===========================================================================
# 基线比对
# ===========================================================================


def _dget(obj: dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _compare(
    record: dict[str, Any], baseline: dict[str, Any], thresholds: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """返回 (overall, rows)。overall ∈ {PASS,WARN,FAIL}。"""
    rows: list[dict[str, Any]] = []
    overall = "PASS"

    def _bump(level: str) -> None:
        nonlocal overall
        order = {"PASS": 0, "WARN": 1, "FAIL": 2}
        if order[level] > order[overall]:
            overall = level

    # 红线（与基线无关，看本次记录本身）
    redline_checks = {
        "exit_code_zero": _dget(record, "stability.exit_code") == 0,
        "result_json_written": _dget(record, "stability.result_json_written") is True,
        "result_json_valid": _dget(record, "stability.result_json_valid") is True,
        "no_secret_leak": _dget(record, "artifact.no_secret_leak") is True,
        "exceptions_zero": (_dget(record, "stability.exceptions") or 0) == 0,
    }
    for name in thresholds.get("redlines", []):
        ok = redline_checks.get(name, True)
        level = "PASS" if ok else "FAIL"
        if not ok:
            _bump("FAIL")
        rows.append({"metric": f"redline:{name}", "current": ok, "baseline": True,
                     "level": level, "kind": "redline"})

    # 指标比对
    for metric, rule in thresholds.get("metrics", {}).items():
        cur = _dget(record, metric)
        base = _dget(baseline, metric) if baseline else None
        if (cur is None or base is None
                or not isinstance(cur, (int, float))
                or not isinstance(base, (int, float))):
            rows.append({"metric": metric, "current": cur, "baseline": base,
                         "level": "SKIP", "kind": "metric"})
            continue
        severity = rule.get("severity", "WARN")
        direction = rule.get("direction", "higher_better")
        level = "PASS"
        detail = ""
        if direction == "higher_better":
            min_ratio = rule.get("min_ratio")
            if min_ratio is not None and base > 0 and cur < base * min_ratio:
                level = severity
                detail = f"{cur} < {round(base * min_ratio, 6)} (基线×{min_ratio})"
        else:  # lower_better
            max_ratio = rule.get("max_ratio")
            max_abs = rule.get("max_abs_increase")
            if max_ratio is not None and base > 0 and cur > base * max_ratio:
                level = severity
                detail = f"{cur} > {round(base * max_ratio, 6)} (基线×{max_ratio})"
            if max_abs is not None and (cur - base) > max_abs:
                level = severity
                detail = f"Δ={round(cur - base, 6)} > {max_abs}"
        if level != "PASS":
            _bump(level)
        rows.append({"metric": metric, "current": cur, "baseline": base,
                     "level": level, "detail": detail, "kind": "metric"})
    return overall, rows


def _print_comparison(
    overall: str, rows: list[dict[str, Any]], base_mode: str | None, cur_mode: str
) -> None:
    print("\n=== 回归比对（与基线） ===")
    if base_mode and base_mode != cur_mode:
        print(f"  ! 基线档位({base_mode}) 与本次({cur_mode}) 不一致——比对仅供参考。")
    for r in rows:
        mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "skip"}[r["level"]]
        line = f"  [{mark}] {r['metric']}: 本次={r['current']} 基线={r['baseline']}"
        if r.get("detail"):
            line += f"  ({r['detail']})"
        print(line)
    print(f"=== 总判定: {overall} ===")


# ===========================================================================
# CLI
# ===========================================================================

_GIT_COMMIT = "nogit"
_GIT_DIRTY = False


_GROUP_ORDER = [
    "meta", "timing", "solving", "verdicts", "budget", "model", "platform",
    "stability", "artifact",
]
_GROUP_TITLES = {
    "meta": "元信息", "timing": "计时", "solving": "解题", "verdicts": "判定",
    "budget": "预算", "model": "模型", "platform": "平台", "stability": "稳定性",
    "artifact": "产物",
}


def _fmt_cell(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "✓" if v else "✗"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _render_markdown(record: dict[str, Any]) -> str:
    """Full field table (every group + per_challenge) as Markdown — the record,
    rendered as the 表格 the field spec describes, one file per run."""
    m = record.get("meta", {})
    so = record.get("solving", {})
    st = record.get("stability", {})
    out: list[str] = []
    out.append(f"# 回归运行 `{m.get('run_id','?')}` — 档位 **{m.get('mode','?')}**")
    out.append("")
    out.append(f"- 时间：{m.get('started_at')} → {m.get('ended_at')}（墙钟 "
               f"{_fmt_cell(record.get('timing',{}).get('total_wall_s'))}s）")
    out.append(f"- commit：`{m.get('git_commit')}`{'（dirty）' if m.get('git_dirty') else ''}")
    out.append(f"- 结果：stop_reason=`{st.get('stop_reason')}` · exit={st.get('exit_code')} "
               f"· 异常={st.get('exceptions')} · 解题 "
               f"{so.get('questions_solved')}/{so.get('questions_total')}")
    for note in record.get("_notes", []) or []:
        out.append(f"- 备注：{note}")
    out.append("")
    out.append("| 组 | 字段 | 值 |")
    out.append("|---|---|---|")
    for g in _GROUP_ORDER:
        d = record.get(g, {})
        if not isinstance(d, dict):
            continue
        title = _GROUP_TITLES.get(g, g)
        for k, v in d.items():
            out.append(f"| {title} | `{k}` | {_fmt_cell(v)} |")
    per = record.get("per_challenge") or []
    if per:
        out.append("")
        out.append("## 每题 per_challenge")
        out.append("| id | 分类 | 难度 | 解出 | 问题解/总 | 提交 | 错 | 判定 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for c in per:
            out.append(
                f"| {c.get('id')} | {c.get('category')} | {c.get('difficulty')} "
                f"| {_fmt_cell(c.get('solved'))} "
                f"| {c.get('questions_solved')}/{c.get('questions_total')} "
                f"| {c.get('submissions')} | {c.get('wrong')} "
                f"| {_fmt_cell(c.get('stop_reason'))} |"
            )
    out.append("")
    return "\n".join(out)


def _render_trace(probes: dict[str, Any]) -> str:
    """Interleave the model's per-turn action with the command result it drove,
    so a reader can see exactly what the agent tried and why it stalled."""
    llm = probes.get("llm")
    ex = probes.get("execution")
    actions = list(getattr(llm, "actions", []) or [])
    transcript = list(getattr(ex, "transcript", []) or [])
    lines = ["===== AGENT 轨迹（逐轮）====="]
    n = max(len(actions), len(transcript))
    if n == 0:
        lines.append("  （无动作记录）")
    for i in range(n):
        if i < len(actions):
            lines.append(f"[turn {i+1}] 模型→ {actions[i]}")
        if i < len(transcript):
            t = transcript[i]
            if "error" in t:
                lines.append(f"          命令× {t['cmd']}  -> 异常 {t['error']}")
            else:
                to = " TIMEOUT" if t.get("timed_out") else ""
                tail = (t.get("tail") or "").replace("\n", " ⏎ ")
                lines.append(f"          命令$ {t['cmd']}")
                lines.append(f"          exit={t.get('exit')}{to}  out={tail[:220]}")
    return "\n".join(lines)


def _render_console(record: dict[str, Any]) -> str:
    """Compact aligned highlights for the terminal (full table goes to the .md)."""
    g = record.get
    so, ve, bu, mo, pl, ti, st = (
        g("solving", {}), g("verdicts", {}), g("budget", {}), g("model", {}),
        g("platform", {}), g("timing", {}), g("stability", {}),
    )
    rows = [
        ("解题", f"{so.get('questions_solved')}/{so.get('questions_total')} "
                f"(rate {_fmt_cell(so.get('solve_rate'))}, "
                f"fully={_fmt_cell(so.get('fully_solved'))})"),
        ("判定", f"correct={ve.get('correct')} wrong={ve.get('wrong')} "
                f"capped={ve.get('capped')} ungraded={ve.get('ungraded')} "
                f"paused={ve.get('paused')}"),
        ("预算", f"turns={bu.get('turns_used')} tokens={bu.get('tokens_total')} "
                f"tool_calls={bu.get('tool_calls_made')} model_calls={mo.get('model_calls')}"),
        ("平台", f"api={pl.get('api_calls_total')} {_fmt_cell(pl.get('api_calls_by_method'))} "
                f"4xx={_fmt_cell(pl.get('http_4xx'))} 5xx={_fmt_cell(pl.get('http_5xx'))}"),
        ("计时", f"wall={_fmt_cell(ti.get('total_wall_s'))}s "
                f"first_flag={_fmt_cell(ti.get('time_to_first_flag_s'))} "
                f"model_avg={_fmt_cell(ti.get('model_call_s_avg'))}s"),
        ("稳定", f"exit={st.get('exit_code')} 异常={st.get('exceptions')} "
                f"result_written={_fmt_cell(st.get('result_json_written'))} "
                f"no_secret_leak={_fmt_cell(g('artifact',{}).get('no_secret_leak'))}"),
    ]
    w = max(len(r[0]) for r in rows)
    lines = ["  ┌─ 字段速览 ─"]
    lines += [f"  │ {k.ljust(w)} │ {v}" for k, v in rows]
    per = record.get("per_challenge") or []
    for c in per:
        lines.append(
            f"  │   · {c.get('id')} [{c.get('category')}/{c.get('difficulty')}] "
            f"solved={_fmt_cell(c.get('solved'))} "
            f"q={c.get('questions_solved')}/{c.get('questions_total')} "
            f"submit={c.get('submissions')} wrong={c.get('wrong')}"
        )
    lines.append("  └─")
    return "\n".join(lines)


def _mode_runner(mode: str):
    return {"smoke": _run_smoke, "integration": _run_integration, "solve": _run_solve}[mode]


async def _dispatch(
    mode: str, result_path: str, t0: float, args: argparse.Namespace
) -> tuple[RunResult, dict[str, Any]]:
    secret_env = args.secret_env
    if mode == "smoke":
        return await _run_smoke(result_path, t0)
    if mode == "integration":
        return await _run_integration(result_path, t0, args.competition, secret_env)
    return await _run_solve(
        result_path, t0, args.competition, secret_env, args.timeout,
        args.challenge, args.max_turns,
    )


def main() -> int:
    global _GIT_COMMIT, _GIT_DIRTY
    p = argparse.ArgumentParser(description="ctfy-agent 回归测试运行器")
    p.add_argument("--mode", choices=["smoke", "integration", "solve"], default="smoke")
    p.add_argument("--competition", default=None, help="覆盖 env 里的 competition id")
    p.add_argument("--out", default=None, help="记录输出目录（默认 regression/runs/）")
    p.add_argument("--baseline", default=None, help="基线文件路径（默认 regression/baseline.json）")
    p.add_argument("--save-baseline", action="store_true", help="把本次记录另存为基线")
    p.add_argument("--secret-env", default=os.path.join(_REPO_ROOT, "docs", "secret.env"),
                   help="integration/solve 读取的凭据文件（KEY=value）")
    p.add_argument("--timeout", type=float, default=1800.0, help="solve 模式整体墙钟上限（秒）")
    p.add_argument("--challenge", default=None,
                   help="solve 档只做这一道题（dev 定向；附件会预拉到 agent 工作区）")
    p.add_argument("--max-turns", type=int, default=None,
                   help="solve 档每题轮次上限（dev 兜底，防止一道题跑太久）")
    p.add_argument("--trace", action="store_true",
                   help="打印并保存 agent 的逐轮动作 + 命令结果（看它到底做了什么）")
    args = p.parse_args()

    _GIT_COMMIT, _GIT_DIRTY = _git_info()
    out_dir = args.out or os.path.join(_REPO_ROOT, "regression", "runs")
    baseline_path = args.baseline or os.path.join(_REPO_ROOT, "regression", "baseline.json")
    os.makedirs(out_dir, exist_ok=True)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + _GIT_COMMIT
    result_path = os.path.join(out_dir, f"result-{run_id}.json")
    started_at = datetime.now(UTC).isoformat()
    competition_id = (
        args.competition or os.environ.get("CTFY_EVAL_COMPETITION_ID", "") or "smoke-competition"
    )

    exit_code = 0
    t0 = time.perf_counter()
    try:
        result, probes = asyncio.run(_dispatch(args.mode, result_path, t0, args))
    except BaseException as e:  # noqa: B036 - 脚本本身绝不崩溃退出非零
        print(f"[regression] 运行异常（已兜底）: {type(e).__name__}: {e}", file=sys.stderr)
        exit_code = 1
        result = RunResult(stop_reason=f"runner_error: {type(e).__name__}")
        probes = {
            "platform": CountingPlatform(SimpleNamespace(), t0=t0),
            "llm": CountingLLM(SimpleNamespace()),
            "execution": CountingExecution(SimpleNamespace()),
            "clock": CountingClock(SimpleNamespace(now=lambda: 0.0)),
            "http": None, "gateway_http": None,
            "settings": SimpleNamespace(), "challenges": [],
            "notes": [f"runner error: {e!r}"],
        }
    total_wall_s = time.perf_counter() - t0
    ended_at = datetime.now(UTC).isoformat()

    # integration/solve 的 competition_id 取实际用到的
    actual_cid = getattr(probes.get("settings"), "competition_id", "") or competition_id

    record = _assemble_record(
        run_id=run_id, mode=args.mode, started_at=started_at, ended_at=ended_at,
        total_wall_s=total_wall_s, exit_code=exit_code, result=result,
        result_path=result_path, probes=probes, competition_id=actual_cid,
    )

    record_path = os.path.join(out_dir, f"{run_id}.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    table_path = os.path.join(out_dir, f"{run_id}.md")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write(_render_markdown(record))
    print(f"[regression] 记录已写入: {record_path}")
    print(f"[regression] 字段表格: {table_path}")
    print(
        f"[regression] 档位={args.mode} 解出={record['solving']['questions_solved']}"
        f"/{record['solving']['questions_total']} "
        f"stop_reason={record['stability']['stop_reason']}"
    )
    print(_render_console(record))

    if args.trace:
        trace = _render_trace(probes)
        trace_path = os.path.join(out_dir, f"{run_id}.trace.txt")
        with open(trace_path, "w", encoding="utf-8") as f:
            f.write(trace)
        print(f"[regression] 轨迹已写入: {trace_path}")
        print(trace)

    # 基线比对 / 存基线
    if args.save_baseline:
        os.makedirs(os.path.dirname(baseline_path) or ".", exist_ok=True)
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"[regression] 已另存基线: {baseline_path}")
    elif os.path.exists(baseline_path):
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)
        thr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.json")
        with open(thr_path, encoding="utf-8") as f:
            thresholds = json.load(f)
        overall, rows = _compare(record, baseline, thresholds)
        _print_comparison(overall, rows, _dget(baseline, "meta.mode"), args.mode)
    else:
        print(f"[regression] 未找到基线 {baseline_path} —— 用 --save-baseline 存一个基线。")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
