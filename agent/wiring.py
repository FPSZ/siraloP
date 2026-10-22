"""Composition root: build concrete adapters from Settings and inject the domain.

This is the ONLY module that imports both the adapter implementations and the
domain wiring them together. Swap environments here (image vs dev) — nothing in
``domain/`` changes. Import side-effect free: no adapter is constructed at import
time, so ``import agent.wiring`` is always safe (e.g. in the smoke test), and the
funded-run key guard in GatewayAdapter only fires when a builder actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.adapters.clock import RealClock
from agent.adapters.execution.subprocess_exec import SubprocessAdapter
from agent.adapters.llm.gateway import GatewayAdapter
from agent.adapters.platform.ctfy_rest import CtfyRestAdapter
from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.config import Settings
from agent.ports.clock import ClockPort
from agent.ports.execution import ExecutionPort
from agent.ports.llm import LLMPort
from agent.ports.platform import PlatformPort
from agent.ports.result import ResultSink


@dataclass
class Wiring:
    """The injected port bundle a Coordinator runs on."""

    platform: PlatformPort
    llm: LLMPort
    execution: ExecutionPort
    result: ResultSink
    clock: ClockPort


def build_for_image(settings: Settings) -> Wiring:
    """Official submission wiring (CTFY-0001 §4).

    Platform REST + model gateway + in-container subprocess executor + the
    result sink pointed at ``settings.default_result_path`` (the entrypoint
    re-points it at ``task.json``'s ``result.path`` when that differs). All
    network goes to whatever ``CTFY_PLATFORM_URL`` / ``LLM_BASE_URL`` the
    platform injected — reachable even under ``internet_policy=deny``.
    """
    clock = RealClock()
    platform = CtfyRestAdapter(
        settings.platform_url, settings.token,
        clock=clock, competition_id=settings.competition_id,
    )
    llm = GatewayAdapter(
        settings.llm_base_url, settings.llm_api_key, settings.llm_flavor, clock=clock
    )
    execution = SubprocessAdapter()
    result = CtfyResultSink(settings.default_result_path)
    return Wiring(platform=platform, llm=llm, execution=execution, result=result, clock=clock)


def build_for_dev(settings: Settings) -> Wiring:
    """Local/dev wiring — the same ports as image mode, read from ``.env``.

    Kept deliberately identical to image mode so dev exercises the real adapters
    (the thing most likely to break is the platform/gateway plumbing, so dev
    should hit it). The result sink enables ``stdout_fallback`` so a failed disk
    write still surfaces the result on the console. Dev-only variants
    (CtfdAdapter, LocalKeyAdapter, DockerSandboxAdapter, CtfyMcpAdapter) are the
    documented swap points — construct them here once implemented.
    """
    clock = RealClock()
    platform = CtfyRestAdapter(
        settings.platform_url, settings.token,
        clock=clock, competition_id=settings.competition_id,
    )
    llm = GatewayAdapter(
        settings.llm_base_url, settings.llm_api_key, settings.llm_flavor, clock=clock
    )
    execution = SubprocessAdapter()
    result = CtfyResultSink(settings.default_result_path, stdout_fallback=True)
    return Wiring(platform=platform, llm=llm, execution=execution, result=result, clock=clock)
