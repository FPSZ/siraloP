"""Unit: the Swarm races lanes and cancels the siblings the instant one lane
returns a (grader-verified) SOLVED; a crashed lane is a failed lane, not a
crashed Swarm."""
from __future__ import annotations

import asyncio

from agent.domain.state import RunStatus
from agent.domain.swarm import Swarm
from tests.support import make_challenge


class _Solved:
    async def solve(self, _challenge) -> RunStatus:
        return RunStatus.SOLVED


class _Slow:
    def __init__(self) -> None:
        self.cancelled = False

    async def solve(self, _challenge) -> RunStatus:
        try:
            await asyncio.sleep(10)
            return RunStatus.GIVEN_UP
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _Crash:
    async def solve(self, _challenge) -> RunStatus:
        raise RuntimeError("lane blew up")


async def test_first_solved_cancels_siblings(clock):
    slow = _Slow()
    swarm = Swarm([slow, _Solved()], clock=clock, max_lanes=3)
    status = await swarm.solve(make_challenge())
    assert status is RunStatus.SOLVED
    assert slow.cancelled is True


async def test_crashed_lane_does_not_crash_swarm(clock):
    swarm = Swarm([_Crash()], clock=clock, max_lanes=3)
    status = await swarm.solve(make_challenge())
    assert status is RunStatus.FAILED


async def test_no_lanes_gives_up(clock):
    status = await Swarm([], clock=clock, max_lanes=3).solve(make_challenge())
    assert status is RunStatus.GIVEN_UP
