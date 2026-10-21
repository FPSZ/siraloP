"""Contract: RealClock is monotonic and really waits (briefly); with_retry backs
off through a ClockPort, honors Retry-After, and re-raises after its budget.
"""
from __future__ import annotations

import pytest

from agent.adapters._resilience import TransientError, with_retry
from agent.adapters.clock import RealClock
from agent.testing.fakes import FakeClock


async def test_realclock_monotonic_and_sleeps():
    c = RealClock()
    t0 = c.now()
    await c.sleep(0)  # zero sleep returns promptly
    await c.sleep_until(c.now())  # already past -> returns
    assert c.now() >= t0


async def test_with_retry_reraises_after_attempts():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise TransientError("boom", retry_after=2.0)

    clock = FakeClock()
    with pytest.raises(TransientError):
        await with_retry(fn, clock=clock, attempts=3)
    assert calls["n"] == 3
    assert clock.slept == [2.0, 2.0]  # waited between the 3 attempts (Retry-After wins)


async def test_with_retry_succeeds_after_transient():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransientError("transient")  # no retry_after -> exponential backoff
        return "ok"

    clock = FakeClock()
    assert await with_retry(fn, clock=clock, attempts=4) == "ok"
    assert calls["n"] == 2
    assert len(clock.slept) == 1  # one backoff wait before the successful retry
