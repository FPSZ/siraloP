"""Swarm: race a few solver strategies on ONE challenge; first verified flag wins.

Width per ADR-0002 §6: easy/medium race 2-3 lanes; a hard challenge gets a single
lane (bank the budget for escalation + stop-loss instead of breadth). The global
concurrency/resource cap (ADR-0001 §3.6) is enforced upstream, in the execution
adapter's semaphore; here we only bound the lane count via ``max_lanes``.

On the FIRST lane to return SOLVED we cancel the siblings and return immediately.
A lane that raises is treated as a failed lane, not a Swarm failure.
"""
from __future__ import annotations

import asyncio

from agent.domain.solver import SolverStrategy
from agent.domain.state import Challenge, RunStatus
from agent.ports.clock import ClockPort

# Statuses ranked best-to-worst so we can report the most informative outcome
# when no lane solved the challenge.
_RANK = {
    RunStatus.SOLVED: 3,
    RunStatus.RUNNING: 2,
    RunStatus.GIVEN_UP: 1,
    RunStatus.FAILED: 0,
}


class Swarm:
    def __init__(
        self,
        strategies: list[SolverStrategy],
        *,
        clock: ClockPort,
        max_lanes: int = 3,
    ) -> None:
        self.strategies = strategies
        self.clock = clock
        self.max_lanes = max(1, max_lanes)

    def _lane_count(self, challenge: Challenge) -> int:
        if (challenge.difficulty or "").strip().lower() == "hard":
            return 1  # don't race hard challenges — single lane + escalate/stop-loss
        return min(self.max_lanes, len(self.strategies))

    async def solve(self, challenge: Challenge) -> RunStatus:
        lanes = self.strategies[: self._lane_count(challenge)]
        if not lanes:
            return RunStatus.GIVEN_UP

        tasks = {asyncio.ensure_future(s.solve(challenge)) for s in lanes}
        best = RunStatus.FAILED
        try:
            pending = tasks
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    status = self._result_of(t)
                    if _RANK[status] > _RANK[best]:
                        best = status
                    if status is RunStatus.SOLVED:
                        return RunStatus.SOLVED
            return best
        finally:
            await self._cancel(tasks)

    @staticmethod
    def _result_of(task: asyncio.Future[RunStatus]) -> RunStatus:
        try:
            result = task.result()
        except asyncio.CancelledError:
            return RunStatus.FAILED
        except Exception:
            # a crashed lane is a failed lane, never a crashed Swarm
            return RunStatus.FAILED
        return result if isinstance(result, RunStatus) else RunStatus.FAILED

    @staticmethod
    async def _cancel(tasks: set[asyncio.Future[RunStatus]]) -> None:
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
