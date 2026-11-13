"""TSecBench entrypoint: drive the agent against a TSecBench 跑分任务.

Runs the standard Coordinator once per unsolved challenge through
``TSecSdkAdapter`` — ``list -> (per challenge) get -> start -> solve -> submit``;
the adapter auto-closes the previous range and ``aclose`` closes the last one.

Prerequisites (issued by the platform when the 跑分任务 is created, see the SDK
接入文档), passed in via the environment:

* ``BENCHMARK_BASE_URL`` / ``BENCHMARK_TOKEN`` — the task's API endpoint + token.
* the range **VPN must already be connected** — ``whoami`` runs the SDK's VPN
  pre-flight probe and aborts loudly (no challenge is touched) if it is not.
* ``LLM_*`` — the model config, read through ``Settings`` like every other entrypoint.

Self-contained wiring: this never imports/edits ``agent/wiring.py`` (that stays
ctfy-only), so the TSecBench path cannot affect the submission image. Each
challenge's result lands in ``--out-dir/<unique_code>.json`` (the same
``RunResult`` shape the platform submission writes), and a one-line summary is
printed at the end.

    BENCHMARK_BASE_URL=... BENCHMARK_TOKEN=... \
        python -m agent.entrypoints.tsec_main --timeout 1200 --out-dir tsec-results
"""
from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

import anyio

from agent.adapters._redaction import redact
from agent.adapters.clock import RealClock
from agent.adapters.execution.subprocess_exec import SubprocessAdapter
from agent.adapters.llm.gateway import GatewayAdapter
from agent.adapters.platform.tsec_sdk import TSecSdkAdapter
from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.config import Settings
from agent.domain.coordinator import Coordinator
from agent.domain.state import Challenge, RunMode, RunResult, Task
from agent.entrypoints._resources import close_ports, split_timeout


async def _solve_one(
    ch: Challenge,
    *,
    platform: TSecSdkAdapter,
    llm: GatewayAdapter,
    execution: SubprocessAdapter,
    clock: RealClock,
    settings: Settings,
    out_dir: Path,
    per_timeout: float | None,
) -> RunResult:
    """Run the Coordinator for one challenge, always writing its result.json."""
    sink = CtfyResultSink(str(out_dir / f"{ch.id}.json"), stdout_fallback=True)
    result = RunResult(stop_reason="not_started")
    coord: Coordinator | None = None
    work_timeout, _ = split_timeout(per_timeout)
    try:
        with anyio.fail_after(work_timeout):
            full = await platform.get_challenge(ch.id)
            task = Task(run_mode=RunMode.INSTANCE, challenge=full)
            coord = Coordinator(
                platform, llm, execution, sink,
                clock=clock, task=task, settings=replace(settings, max_wall_s=per_timeout),
            )
            result = await coord.run()
    except BaseException as error:  # noqa: B036 - never leak an unsafe traceback
        if coord is not None:
            result = coord.current_result
        result.fully_solved = False
        result.stop_reason = (
            "timeout" if isinstance(error, TimeoutError)
            else redact(f"error: {type(error).__name__}: {error}")
        )
    finally:
        sink.write(result)
    return result


async def _run(args: argparse.Namespace) -> list[tuple[str, RunResult]]:
    base_url = os.environ.get("BENCHMARK_BASE_URL", "")
    token = os.environ.get("BENCHMARK_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError(
            "BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set (issued by TSecBench "
            "when the 跑分任务 is created). Connect the range VPN first."
        )
    settings = Settings.from_env()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    clock = RealClock()
    platform = TSecSdkAdapter(base_url, token, clock=clock)
    llm = GatewayAdapter(
        settings.llm_base_url, settings.llm_api_key, settings.llm_flavor, clock=clock
    )
    execution = SubprocessAdapter()

    results: list[tuple[str, RunResult]] = []
    try:
        # VPN pre-flight + token/connectivity check; aborts before any challenge.
        await platform.whoami()
        challenges = await platform.list_challenges()
        targets = [
            c for c in challenges
            if (not args.challenge or c.id == args.challenge)
            and not (c.questions and all(q.answered for q in c.questions))
        ]
        for ch in targets:
            res = await _solve_one(
                ch, platform=platform, llm=llm, execution=execution,
                clock=clock, settings=settings, out_dir=out_dir,
                per_timeout=args.timeout,
            )
            results.append((ch.id, res))
            print(redact(f"[{ch.id}] {res.stop_reason}; solved={res.solved}"))
    finally:
        _, cleanup_timeout = split_timeout(args.timeout)
        await close_ports(execution, llm, platform, timeout_s=cleanup_timeout)
    return results


def main() -> int:
    p = argparse.ArgumentParser(description="ctfy-agent on TSecBench (self-hosted)")
    p.add_argument("--challenge", help="one unique_code to attempt (default: all unsolved)")
    p.add_argument(
        "--timeout", type=float, default=1200.0,
        help="per-challenge wall-clock budget in seconds (default 1200)",
    )
    p.add_argument("--out-dir", default="tsec-results", help="where per-challenge result.json go")
    args = p.parse_args()
    try:
        results = anyio.run(_run, args)
    except BaseException as error:  # noqa: B036 - startup/VPN/credential failure
        print(redact(f"tsec run aborted: {type(error).__name__}: {error}"))
        return 1
    solved = sum(1 for _, r in results if r.solved)
    print(redact(f"done: {len(results)} attempted, {solved} with a solve"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
