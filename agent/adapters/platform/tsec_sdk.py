"""TSecSdkAdapter — PlatformPort over the TSec Benchmark player API.

Tencent 云鼎 `TSecBench` (https://tsecbench.zc.tencent.com) evaluates an agent by
having it drive the official ``tsec-benchmark`` Python SDK against hosted CVE /
云原生 ranges reached over a per-task VPN. The SDK's player-facing flow —
``list_challenges`` / ``start_challenge`` / ``submit_flag`` / ``close_challenge``
— maps onto the frozen ``PlatformPort`` so the existing control loop solves
TSecBench challenges unchanged, exactly like the ctfy adapter does for the
Jinghua Cup platform.

Prerequisites, issued by the platform after a 跑分 task is created (see the SDK's
接入文档): ``BENCHMARK_BASE_URL``, ``BENCHMARK_TOKEN`` and a **connected range
VPN** (challenge ``container_addr`` values are only reachable through it). This
adapter is the API edge; VPN setup happens outside it (wiring / the runner).

Mapping notes (semantics that differ from ctfy and how they are honored):

* **Flags, not questions.** A TSecBench challenge has ``flag_count`` flags and no
  per-flag prompt; the grader matches a submitted flag against any unsolved flag
  and returns ``matched_flag_index``. We model each flag as one ``Question``
  (``<code>#<i>``) sharing the challenge description, and mark the first
  ``correct_flag_count`` answered. ``submit`` ignores ``qid`` and submits the raw
  flag; a correct flag advances progress regardless of which question drove it.
* **No close in the port.** ``PlatformPort`` has no release method, but TSecBench
  caps active ranges and needs ``close_challenge`` to free a slot. The adapter
  closes the previously-open challenge when a new one starts, and closes the last
  one in ``aclose`` — the frozen port is untouched.
* **No QA / scoreboard in the SDK.** ``submit_qa`` is never reached (all
  questions are instance-style) and raises if called; ``scoreboard`` returns an
  empty board (metrics only).

Resilience mirrors the ctfy adapter: every SDK call runs through ``with_retry``
on the injected ClockPort, and the SDK's retryable failures
(``ResourceUnavailable`` 503, ``InternalError`` 500, transport ``TSecConnectionError``)
map to ``TransientError`` (honored deterministically under FakeClock). A
``DuplicateSubmit`` (the same flag already accepted) is idempotent → CORRECT.

Challenge text and grader messages are untrusted DATA (``policy.is_trusted_instruction``
stays False). The token lives only inside the SDK's header; it is never logged.
Importable without network (``tsec_benchmark`` builds its httpx client lazily and
opens no connection until a call is made).

Implements ports.platform.PlatformPort.
"""
from __future__ import annotations

from tsec_benchmark import (
    Challenge as TsecChallenge,
)
from tsec_benchmark import (
    DuplicateSubmit,
    InternalError,
    ResourceUnavailable,
    TSecBenchmarkAsync,
    TSecConnectionError,
    VpnCheckError,
)

from agent.adapters._redaction import redact
from agent.adapters._resilience import TransientError, with_retry
from agent.domain.state import (
    Account,
    Challenge,
    Instance,
    InstanceStatus,
    Question,
    Scoreboard,
    Verdict,
    VerdictStatus,
)
from agent.ports.clock import ClockPort

# --- container_status -> domain instance status ------------------------------
_READY = {"available", "running", "ready", "up", "active", "healthy"}
_STARTING = {"pending", "creating", "provisioning", "starting", "booting", "queued"}
_STOPPED = {"stopped", "stop_pending", "stopping", "down", "terminated", "expired"}
_ERROR = {"error", "failed", "crashed"}


class TSecSdkAdapter:
    """PlatformPort talking to TSecBench through the ``tsec-benchmark`` SDK."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        clock: ClockPort,
        timeout: float = 30.0,
        transport: object | None = None,
        sdk: TSecBenchmarkAsync | None = None,
    ) -> None:
        self._clock = clock
        # The token lives only inside the SDK's BENCHMARK_TOKEN header; redact it
        # from every retry label so it never reaches a log.
        self._secrets = (token,)
        # auto_check_vpn is disabled here: we never enter the SDK's async context
        # manager (its httpx client is built in __init__), and we expose the VPN
        # probe explicitly through whoami() so the caller controls when it runs.
        self._sdk = sdk if sdk is not None else TSecBenchmarkAsync(
            base_url, token, timeout=timeout,
            transport=transport, auto_check_vpn=False,
        )
        # The challenge whose range is currently open (at most one active slot).
        self._open: str | None = None

    # ------------------------------------------------------------------ port --
    async def whoami(self) -> Account:
        """Cheapest connectivity+auth check: the VPN pre-flight probe.

        A reachable probe proves both the range VPN is up and the token's task is
        valid (the healthcheck is only routable inside the task's VPN). Maps the
        probe's VPN-assigned client IP onto Account; a failed probe is fatal.
        """
        try:
            res = await self._call(self._sdk.check_vpn, "check_vpn")
        except VpnCheckError as exc:
            raise RuntimeError(
                f"TSecBench VPN pre-flight failed: {exc.message} "
                f"(detail={exc.detail}). Connect the task's range VPN first."
            ) from None
        return Account(id=res.client_ip or "tsecbench", name="tsecbench")

    async def list_challenges(self) -> list[Challenge]:
        items = await self._call(self._sdk.list_challenges, "list_challenges")
        return [self._to_challenge(c) for c in items]

    async def get_challenge(self, cid: str) -> Challenge:
        """TSecBench has no single-challenge route; filter the (progress-bearing)
        list. There is no question gating, so this only refreshes flag progress."""
        for c in await self._call(self._sdk.list_challenges, "list_challenges"):
            if c.unique_code == cid:
                return self._to_challenge(c)
        raise RuntimeError(f"get_challenge: {cid!r} not in the task's challenge set")

    async def start_instance(self, cid: str) -> Instance:
        # One active range at a time: free the previously-open one first.
        if self._open is not None and self._open != cid:
            await self._safe_close(self._open)
            self._open = None
        # "Start or return": if this challenge's range is already up with an
        # address, reuse it instead of re-starting (which could hit the
        # active-limit / resource pool).
        current = await self._find(cid)
        if current is not None and current.container_addr:
            self._open = cid
            return self._instance(cid, current.container_addr, current.container_status)
        started = await self._call(
            lambda: self._sdk.start_challenge(cid), "start_challenge"
        )
        self._open = cid
        if started.container_addr:
            return self._instance(cid, started.container_addr, "available")
        # Started but no address yet: report STARTING with the current status so
        # the caller polls (by calling start_instance again).
        refreshed = await self._find(cid)
        if refreshed is not None:
            return self._instance(cid, refreshed.container_addr, refreshed.container_status)
        return self._instance(cid, [], "pending")

    async def submit(self, cid: str, qid: str, answer: str) -> Verdict:
        """Submit a flag (``qid`` is informational — TSecBench grades per flag,
        not per question). DuplicateSubmit is idempotent success."""
        try:
            res = await self._call(
                lambda: self._sdk.submit_flag(cid, answer), "submit_flag"
            )
        except DuplicateSubmit as exc:
            return Verdict(VerdictStatus.CORRECT, f"duplicate (already accepted): {exc.message}")
        status = VerdictStatus.CORRECT if res.correct else VerdictStatus.WRONG
        msg = (
            f"awarded={res.awarded} "
            f"progress={res.correct_flag_count}/{res.total_flag_count}"
        )
        return Verdict(status, msg)

    async def submit_qa(self, cid: str, qid: str, answer: str) -> Verdict:
        raise RuntimeError(
            "TSecBench has no QA submissions; every challenge is instance-style "
            "(submit_qa should never be reached for this platform)."
        )

    async def scoreboard(self, competition_id: str) -> Scoreboard:
        # The player SDK exposes no scoreboard; return an empty board so metrics
        # collection degrades cleanly rather than failing the run.
        return Scoreboard(entries=[], retry_after_s=None)

    async def aclose(self) -> None:
        """Release the last open range, then close the SDK's HTTP client."""
        if self._open is not None:
            await self._safe_close(self._open)
            self._open = None
        await self._sdk.aclose()

    # --------------------------------------------------------------- helpers --
    async def _find(self, cid: str) -> TsecChallenge | None:
        """Current SDK Challenge for ``cid`` (progress + container state), or None.

        Never raises for a plain miss: a transient failure still propagates as
        TransientError via _call, but an absent code just yields None so the
        caller can fall through to a fresh start."""
        for c in await self._call(self._sdk.list_challenges, "list_challenges"):
            if c.unique_code == cid:
                return c
        return None

    async def _safe_close(self, cid: str) -> None:
        """Best-effort ``close_challenge``; swallow every failure (releasing a
        range must never abort the run)."""
        try:
            await self._call(lambda: self._sdk.close_challenge(cid), "close_challenge")
        except Exception:
            pass

    async def _call(self, fn, label: str):
        """Run one SDK coroutine through with_retry, mapping the SDK's retryable
        failures onto TransientError (honored on the injected ClockPort)."""
        async def _once():
            try:
                return await fn()
            except (ResourceUnavailable, InternalError, TSecConnectionError) as exc:
                raise TransientError(
                    redact(str(exc), extra_secrets=self._secrets)
                ) from None

        return await with_retry(
            _once, clock=self._clock,
            label=redact(label, extra_secrets=self._secrets),
        )

    def _to_challenge(self, c: TsecChallenge) -> Challenge:
        n = max(1, int(c.flag_count))
        solved = int(c.correct_flag_count)
        prompt = c.description or ""
        questions = [
            Question(
                id=f"{c.unique_code}#{i}",
                prompt=prompt,
                answered=(c.is_completed or i < solved),
                fmt="dynamic",
            )
            for i in range(n)
        ]
        return Challenge(
            id=c.unique_code,
            name=c.unique_code,
            category="",
            description=prompt,
            questions=questions,
            gated_questions=0,
            difficulty=c.difficulty,
            points=int(c.total_score),
            solves=0,
        )

    def _instance(self, cid: str, addrs: list[str], container_status: str) -> Instance:
        host, port = self._split_addr(addrs[0]) if addrs else ("", 0)
        status = InstanceStatus.READY if addrs else self._status_of(container_status)
        return Instance(
            id=cid,
            host=host,
            port=port,
            tls_sni=False,            # range services are raw TCP over the VPN
            status=status,
            poll_after_s=2.0,
        )

    @staticmethod
    def _split_addr(addr: str) -> tuple[str, int]:
        a = (addr or "").strip()
        if ":" in a:
            host, _, raw = a.rpartition(":")
            try:
                return host, int(raw)
            except ValueError:
                return a, 0
        return a, 0

    @staticmethod
    def _status_of(raw: str) -> InstanceStatus:
        s = (raw or "").strip().lower()
        if s in _READY:
            return InstanceStatus.READY
        if s in _STOPPED:
            return InstanceStatus.STOPPED
        if s in _ERROR:
            return InstanceStatus.ERROR
        if s in _STARTING:
            return InstanceStatus.STARTING
        # Unknown status with no address: assume still coming up so the caller
        # polls once more rather than attacking a service that is not ready.
        return InstanceStatus.STARTING
