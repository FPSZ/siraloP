"""Image-mode entrypoint (CTFY-0001 §4). The platform starts this; cwd=/ctfy/work.

Two guarantees this module exists to keep (everything else is delegated):

  1. A ``result.json`` is written on EVERY exit path — normal return, domain
     error, or even KeyboardInterrupt/SystemExit/cancellation. "Not writing the
     file == failed run" (CTFY-0001 §3), so the write lives in a ``finally`` and
     the top-level ``except`` catches ``BaseException``.
  2. The process ALWAYS exits 0. The platform treats a nonzero exit as a crash;
     we would rather report a half-finished result than crash.

A dry-run (``task.dry_run``) writes a minimal valid result and makes no model
calls, so image registration can prove the container boots and emits a result.

Invoke as a module (never a relative path): ``python -m agent.entrypoints.image_main``
"""
from __future__ import annotations

import anyio

from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.briefing import load_task
from agent.config import Settings
from agent.domain.coordinator import Coordinator
from agent.domain.state import RunResult, Task
from agent.entrypoints._resources import close_ports
from agent.wiring import build_for_image


def _sink_for(task: Task) -> CtfyResultSink:
    """The result sink pointed at the task's resolved result path."""
    return CtfyResultSink(task.result_path, stdout_fallback=task.stdout_fallback)


async def _run() -> None:
    settings = Settings.from_env()
    task = load_task(settings)
    sink = _sink_for(task)

    # Dry-run: prove the image boots and writes a valid result, no model calls.
    if task.dry_run:
        sink.write(RunResult(stop_reason="dry_run"))
        return

    result = RunResult(stop_reason="not_started")
    wiring = None
    try:
        wiring = build_for_image(settings)
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
    except BaseException as e:  # noqa: B036 - must never exit without a result
        result = RunResult(stop_reason=f"error: {type(e).__name__}: {e}")
    finally:
        try:
            if wiring is not None:
                await close_ports(wiring.execution, wiring.llm, wiring.platform)
        finally:
            sink.write(result)


def main() -> int:
    try:
        anyio.run(_run)
    except BaseException as e:  # noqa: B036 - last resort: guard anyio.run itself
        # _run writes in its own finally; this only covers a failure to even
        # start the loop. Best-effort, never raising, always exit 0.
        try:
            s = Settings.from_env()
            CtfyResultSink(s.default_result_path, stdout_fallback=True).write(
                RunResult(stop_reason=f"fatal: {type(e).__name__}: {e}")
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
