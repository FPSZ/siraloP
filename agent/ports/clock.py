"""ClockPort — the time seam.

FROZEN CONTRACT — see CONTRIBUTING.md before changing a signature.

The pure domain must not call ``time.*`` / ``asyncio.sleep`` directly (it would
break the zero-I/O rule and make tests non-deterministic). All waiting —
instance readiness, Retry-After, the competition_paused sleep-until-next-session
— goes through this port. Real impl: adapters/clock.py (monotonic). Test impl:
agent/testing/fakes.py FakeClock (advanceable, no real waiting).
"""
from __future__ import annotations

from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> float:
        """Monotonic seconds. Only differences are meaningful (not wall time)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for a duration (non-negative; 0 yields)."""
        ...

    async def sleep_until(self, deadline: float) -> None:
        """Sleep until ``now() >= deadline`` (returns immediately if already past)."""
        ...
