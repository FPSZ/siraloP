"""RealClock — the production ClockPort (monotonic time + real async sleep).

Implements agent.ports.clock.ClockPort. This is the ONLY adapter that reads the
wall/monotonic clock and performs real waiting for the control loop; the pure
domain waits exclusively through this port. Tests use
agent.testing.fakes.FakeClock (virtual, advanceable), so nothing real sleeps.
"""
from __future__ import annotations

import time

import anyio


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(max(0.0, seconds))

    async def sleep_until(self, deadline: float) -> None:
        await anyio.sleep(max(0.0, deadline - self.now()))
