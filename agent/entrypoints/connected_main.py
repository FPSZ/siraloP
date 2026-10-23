"""Connected / dev entrypoint: drive the agent from your machine against the
platform (or a dev instance) without rebuilding the submission image.

Uses the dev wiring. Like image mode it always writes a result.json locally, so
you can inspect exactly what a real run would submit. Fast iteration loop:
edit -> ``python -m agent.entrypoints.connected_main --challenge <id>``.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from dataclasses import replace

import anyio

from agent.adapters._redaction import redact
from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.briefing import load_task
from agent.config import Settings
from agent.domain.coordinator import Coordinator
from agent.domain.state import RunResult
from agent.entrypoints._resources import close_ports
from agent.wiring import build_for_dev


async def _run(args: argparse.Namespace) -> RunResult:
    result = RunResult(stop_reason="not_started")
    sink = CtfyResultSink(Settings().default_result_path, stdout_fallback=True)
    wiring = None
    try:
        settings = Settings.from_env()
        sink = CtfyResultSink(settings.default_result_path, stdout_fallback=True)
        task = load_task(settings)
        sink = CtfyResultSink(task.result_path, stdout_fallback=True)
        wiring = build_for_dev(settings)
        # Include challenge fetching in the same result/cleanup boundary as run.
        if args.challenge:
            challenge = await wiring.platform.get_challenge(args.challenge)
            task = replace(task, challenge=challenge)
        coord = Coordinator(
            wiring.platform,
            wiring.llm,
            wiring.execution,
            sink,
            clock=wiring.clock,
            task=task,
            settings=settings,
        )
        result = await coord.run()
    except BaseException as error:  # noqa: B036 - do not expose an unsafe traceback
        result = RunResult(stop_reason=redact(f"error: {type(error).__name__}: {error}"))
    finally:
        try:
            if wiring is not None:
                await close_ports(wiring.execution, wiring.llm, wiring.platform)
        finally:
            sink.write(result)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="ctfy-agent (connected/dev mode)")
    p.add_argument("--challenge", help="challenge id to attempt (default: all visible)")
    cli_stdout, cli_stderr = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(cli_stdout), contextlib.redirect_stderr(cli_stderr):
            args = p.parse_args()
    except SystemExit as error:
        print(redact(cli_stdout.getvalue()), end="")
        print(redact(cli_stderr.getvalue()), end="", file=sys.stderr)
        return error.code if isinstance(error.code, int) else 1
    try:
        result = anyio.run(_run, args)
    except BaseException as error:  # noqa: B036 - event-loop startup/cleanup errors
        result = RunResult(stop_reason=redact(f"error: {type(error).__name__}: {error}"))
        try:
            CtfyResultSink(
                Settings.from_env().default_result_path, stdout_fallback=True
            ).write(result)
        except Exception:
            pass
        print(result.stop_reason, file=sys.stderr)
        return 1
    print(redact(
        f"done: {result.stop_reason}; solved={result.solved}; answers={len(result.answers)}"
    ))
    return 1 if result.stop_reason.startswith("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
