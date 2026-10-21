"""PlatformPort — talk to the competition platform (CTFY-0001).

FROZEN CONTRACT — changing a method signature requires an ADR + a team
heads-up (see CONTRIBUTING.md). Every adapter and every domain caller is written
against the semantics stated here; an ad-hoc change silently invalidates all of
them.

Implementations: adapters/platform/{ctfy_rest,ctfy_mcp,ctfd}.py
"""
from __future__ import annotations

from typing import Protocol

from agent.domain.state import (
    Account,
    Challenge,
    Instance,
    Scoreboard,
    Verdict,
)


class PlatformPort(Protocol):
    """All routes are under $CTFY_PLATFORM_URL/api/v1 with a bearer token.

    Pacing: implementations MUST honor ``Retry-After`` / ``poll_after_s`` from the
    platform (CTFY-0001 §1.2/§4) — sleep what the platform says rather than busy-
    polling, since every team shares one request budget.
    """

    async def whoami(self) -> Account:
        """Return the authenticated account (GET /me). Cheapest token check."""
        ...

    async def list_challenges(self) -> list[Challenge]:
        """List the challenges visible in this competition."""
        ...

    async def get_challenge(self, cid: str) -> Challenge:
        """Fetch one challenge, INCLUDING its current question set.

        Call this again after every correct answer: gated questions
        (``requires:``) only materialize once their prerequisite is solved
        (CTFY-0002 §2.5). An agent that reads the briefing once and stops sees a
        fraction of a gated challenge.
        """
        ...

    async def start_instance(self, cid: str) -> Instance:
        """Start (or return) this team's instance for a challenge.

        Returns an ``Instance`` whose ``host``/``port`` reach the service over
        TLS+SNI on 443. The instance may still be ``STARTING``; the caller polls
        readiness using ``poll_after_s`` (wait via ClockPort, do not busy-loop).
        """
        ...

    async def submit(self, cid: str, qid: str, answer: str) -> Verdict:
        """Submit a jeopardy answer; graded by the platform.

        Returns a ``Verdict`` — it does NOT raise for an ordinary outcome:
          * correct      -> VerdictStatus.CORRECT
          * incorrect    -> VerdictStatus.WRONG (with ``attempts_left``)
          * cap reached  -> VerdictStatus.CAPPED (no grading attempt was spent)
          * grader down  -> VerdictStatus.UNGRADED (CTFY-0002 §5.4: costs no attempt)
          * paused       -> VerdictStatus.PAUSED (session closed; retry later)
        Reserve exceptions for transport failures the resilience layer retries.
        """
        ...

    async def submit_qa(self, cid: str, qid: str, answer: str) -> Verdict:
        """Submit a QA (multiple-choice) answer (POST /api/v1/qa/submissions).

        Same Verdict contract as ``submit``. QA has no instance.
        """
        ...

    async def scoreboard(self, competition_id: str) -> Scoreboard:
        """Read the competition scoreboard (carries its own ``retry_after_s``)."""
        ...
