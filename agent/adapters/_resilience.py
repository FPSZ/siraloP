"""Shared resilience helpers for the I/O adapters (retry with backoff).

Only adapters import this — the pure domain stays I/O- and retry-free. Waiting
goes through a ClockPort, so retries are deterministic under FakeClock in tests
(no real sleeping).

Contract used by adapters/llm/gateway.py and adapters/platform/ctfy_rest.py:

    raise TransientError(retry_after=<secs or None>) for a RETRYABLE failure
    (HTTP 429 / 5xx / a transport error), then wrap the call in with_retry(...).
    A non-retryable failure (auth / 4xx other than 429) must raise RuntimeError
    and is never retried.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from agent.ports.clock import ClockPort

T = TypeVar("T")


class TransientError(Exception):
    """A failure worth retrying. ``retry_after`` (seconds), when the server sent
    one, overrides the computed backoff. ``retryable`` is a structural marker
    that lets a caller distinguish a recoverable provider error from a hard
    failure without importing this adapter helper. Once the adapter's retry
    budget is exhausted, ``exhausted`` is set so an outer loop does not multiply
    the retry budget."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.retryable = True
        self.exhausted = False


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    clock: ClockPort,
    attempts: int = 4,
    base: float = 0.5,
    cap: float = 8.0,
    label: str = "",
) -> T:
    """Call ``fn`` (a zero-arg async callable), retrying on TransientError.

    Backoff is deterministic exponential, no jitter, so tests are reproducible:
    ``delay_i = min(cap, base * 2**i)`` — but a server-sent ``Retry-After`` always
    wins. Re-raises the last TransientError after ``attempts`` tries.
    """
    last: TransientError | None = None
    for i in range(attempts):
        try:
            return await fn()
        except TransientError as e:
            last = e
            if i == attempts - 1:
                break
            backoff = min(cap, base * (2**i))
            delay = e.retry_after if e.retry_after is not None else backoff
            await clock.sleep(delay)
    assert last is not None  # loop body only exits via return or this re-raise
    last.exhausted = True
    raise last
