"""Independent cleanup failures cannot block result delivery or other resources."""
from __future__ import annotations

import asyncio

import pytest

from agent.entrypoints._resources import close_ports, split_timeout


async def test_stuck_close_is_cancelled_without_blocking_other_resources():
    cancelled = asyncio.Event()
    closed = asyncio.Event()

    class Stuck:
        async def aclose(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class Healthy:
        async def aclose(self):
            closed.set()

    await close_ports(Stuck(), Healthy(), timeout_s=0)

    assert cancelled.is_set()
    assert closed.is_set()


async def test_broken_close_does_not_cancel_an_independent_resource():
    closed = asyncio.Event()

    class Broken:
        async def aclose(self):
            raise RuntimeError("synthetic close failure")

    class Healthy:
        async def aclose(self):
            closed.set()

    await close_ports(Broken(), Healthy())

    assert closed.is_set()


@pytest.mark.parametrize("total_s", [0, 1, 100])
def test_shutdown_reservation_never_exceeds_the_total_budget(total_s):
    work_s, cleanup_s = split_timeout(total_s)

    assert work_s is not None
    assert work_s >= 0
    assert cleanup_s >= 0
    assert work_s + cleanup_s == total_s
    if total_s > 0:
        assert 0 < cleanup_s < total_s
