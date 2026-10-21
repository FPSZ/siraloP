"""Connected / dev entrypoint: drive the agent from your machine against the
platform (or a dev instance) without rebuilding the submission image.

Uses the dev wiring. Like image mode it always writes a result.json locally, so
you can inspect exactly what a real run would submit. Fast iteration loop:
edit -> ``python -m agent.entrypoints.connected_main --challenge <id>``.
"""
from __future__ import annotations

import argparse
from dataclasses import replace

import anyio

from agent.briefing import load_task
from agent.config import Settings
from agent.domain.coordinator import Coordinator
from agent.domain.state import RunResult
from agent.wiring import build_for_dev


async def _run(args: argparse.Namespace) -> RunResult:
    settings = Settings.from_env()
    task = load_task(settings)
    wiring = build_for_dev(settings)

    # --challenge pins the run to a single challenge (handy while iterating).
    if args.challenge:
        challenge = await wiring.platform.get_challenge(args.challenge)
        task = replace(task, challenge=challenge)

    coord = Coordinator(
        wiring.platform,
        wiring.llm,
        wiring.execution,
        wiring.result,
        clock=wiring.clock,
        task=task,
        settings=settings,
    )
    result = RunResult(stop_reason="not_started")
    try:
        result = await coord.run()
    finally:
        wiring.result.write(result)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="ctfy-agent (connected/dev mode)")
    p.add_argument("--challenge", help="challenge id to attempt (default: all visible)")
    args = p.parse_args()
    result = anyio.run(_run, args)
    print(f"done: {result.stop_reason}; solved={result.solved}; answers={len(result.answers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
