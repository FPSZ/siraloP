"""In-memory fakes: one faithful double per port.

These are the shipped reference doubles imported by our own tests and by
teammates' tests. They implement the frozen contracts in ``agent/ports/``
structurally (no inheritance, like the real adapters) and perform zero real
I/O: no sockets, no subprocesses, no clock, no disk. Everything is driven by
constructor arguments and recorded on public attributes so a test can assert on
exactly what the domain did.

Stdlib only. Keep faithful to the port semantics — a fake that lies is worse
than no fake.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from agent.domain.state import (
    Account,
    Challenge,
    ExecResult,
    Instance,
    InstanceStatus,
    LLMResponse,
    Msg,
    RunResult,
    Scoreboard,
    ToolDef,
    Verdict,
    VerdictStatus,
)

__all__ = [
    "FakeClock",
    "FakePlatform",
    "FakeLLM",
    "FakeExecution",
    "RecordingResultSink",
]


# --------------------------------------------------------------------------
# ClockPort
# --------------------------------------------------------------------------
class FakeClock:
    """Virtual, advanceable ``ClockPort`` — nothing ever really waits.

    Time starts at ``0.0`` and only ever moves forward (monotonic, matching the
    real contract). ``sleep`` advances time instead of blocking and records each
    waited duration on ``slept`` so a test can assert on pacing/backoff; call
    ``advance`` to move time forward by hand (e.g. past a ``Budget`` deadline).
    """

    def __init__(self) -> None:
        self._now: float = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        delta = max(0.0, seconds)
        self._now += delta
        self.slept.append(delta)

    async def sleep_until(self, deadline: float) -> None:
        self._now = max(self._now, deadline)

    def advance(self, seconds: float) -> None:
        """Move virtual time forward by ``seconds`` (negatives are ignored)."""
        self._now += max(0.0, seconds)


# --------------------------------------------------------------------------
# PlatformPort
# --------------------------------------------------------------------------
class FakePlatform:
    """Scriptable in-memory ``PlatformPort``.

    Args:
        challenges: the challenges returned by ``list_challenges`` / matched by
            ``get_challenge`` (by ``id``).
        answers: optional answer key mapping ``(cid, qid) -> correct answer``
            (a single string, or any iterable of acceptable strings). When a key
            is present, grading is strict against it; when it is absent the
            default heuristic applies (any answer containing ``"ctfy{"`` grades
            CORRECT), which keeps happy-path tests terse.
        verdicts: optional forced verdicts that win over grading — either one
            ``Verdict``/``VerdictStatus`` applied to every submission, or a
            mapping ``(cid, qid) -> Verdict | VerdictStatus`` (lets a test drive
            the CAPPED / UNGRADED / PAUSED control-loop branches).

    Every ``submit``/``submit_qa`` is appended to ``submissions`` as a
    ``(cid, qid, answer)`` tuple.
    """

    def __init__(
        self,
        challenges: list[Challenge] | None = None,
        *,
        answers: Mapping[tuple[str, str], str | Iterable[str]] | None = None,
        verdicts: Mapping[tuple[str, str], Verdict | VerdictStatus]
        | Verdict
        | VerdictStatus
        | None = None,
    ) -> None:
        self._challenges: list[Challenge] = list(challenges) if challenges else []
        self._answers: dict[tuple[str, str], str | Iterable[str]] = (
            dict(answers) if answers else {}
        )
        self._verdicts = verdicts
        self.submissions: list[tuple[str, str, str]] = []

    async def whoami(self) -> Account:
        return Account("fake", "fake")

    async def list_challenges(self) -> list[Challenge]:
        return list(self._challenges)

    async def get_challenge(self, cid: str) -> Challenge:
        for c in self._challenges:
            if c.id == cid:
                return c
        raise KeyError(cid)

    async def start_instance(self, cid: str) -> Instance:
        return Instance(
            id=f"inst-{cid}",
            host="chal.local",
            port=443,
            status=InstanceStatus.READY,
            poll_after_s=0.0,
        )

    async def submit(self, cid: str, qid: str, answer: str) -> Verdict:
        return self._grade(cid, qid, answer)

    async def submit_qa(self, cid: str, qid: str, answer: str) -> Verdict:
        return self._grade(cid, qid, answer)

    async def scoreboard(self, competition_id: str) -> Scoreboard:
        return Scoreboard()

    # -- grading helpers ----------------------------------------------------
    def _grade(self, cid: str, qid: str, answer: str) -> Verdict:
        self.submissions.append((cid, qid, answer))
        forced = self._forced_verdict(cid, qid)
        if forced is not None:
            return forced
        key = (cid, qid)
        if key in self._answers:
            return Verdict.ok() if self._matches(self._answers[key], answer) else Verdict.wrong()
        if isinstance(answer, str) and "ctfy{" in answer:
            return Verdict.ok()
        return Verdict.wrong()

    def _forced_verdict(self, cid: str, qid: str) -> Verdict | None:
        v = self._verdicts
        if v is None:
            return None
        if isinstance(v, Verdict):
            return v
        if isinstance(v, VerdictStatus):
            return Verdict(v)
        try:
            found = v[(cid, qid)]
        except (KeyError, TypeError):
            return None
        return found if isinstance(found, Verdict) else Verdict(found)

    @staticmethod
    def _matches(expected: str | Iterable[str], answer: str) -> bool:
        if isinstance(expected, str):
            return answer == expected
        try:
            return answer in expected
        except TypeError:
            return answer == expected


# --------------------------------------------------------------------------
# LLMPort
# --------------------------------------------------------------------------
class FakeLLM:
    """Deterministic ``LLMPort`` driven by a canned script.

    Construct with either a list of ``LLMResponse`` (served FIFO, one per
    ``complete``) or a callable ``messages -> LLMResponse`` (consulted every
    call, e.g. to branch on the latest message). Each call is appended to
    ``calls`` as ``{"messages", "model", "effort", "tools"}``. When a canned
    list is exhausted, further calls return ``LLMResponse(text="",
    stop_reason="stop")`` so a loop terminates instead of hanging.
    """

    def __init__(
        self,
        responses: list[LLMResponse] | Callable[[list[Msg]], LLMResponse] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        if responses is None:
            self._queue: list[LLMResponse] | None = []
            self._fn: Callable[[list[Msg]], LLMResponse] | None = None
        elif callable(responses):
            self._queue = None
            self._fn = responses
        else:
            self._queue = list(responses)
            self._fn = None

    async def complete(
        self,
        messages: list[Msg],
        *,
        model: str,
        effort: str = "medium",
        tools: list[ToolDef] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {"messages": messages, "model": model, "effort": effort, "tools": tools}
        )
        if self._fn is not None:
            return self._fn(messages)
        if self._queue:
            return self._queue.pop(0)
        return LLMResponse(text="", stop_reason="stop")


# --------------------------------------------------------------------------
# ExecutionPort
# --------------------------------------------------------------------------
class FakeExecution:
    """In-memory ``ExecutionPort`` — no real processes, no real files.

    Construct with either a ``cmd -> ExecResult`` mapping or a callable
    ``cmd -> ExecResult``. Unmapped commands echo back as
    ``ExecResult(stdout=cmd, exit_code=0)``. ``read_file``/``write_file`` share
    the ``files`` dict (bytes in, bytes out; reading a missing path raises
    ``FileNotFoundError``). Every ``run`` command is appended to ``commands``.
    """

    def __init__(
        self,
        responses: Mapping[str, ExecResult] | Callable[[str], ExecResult] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        if responses is None:
            self._map: dict[str, ExecResult] | None = None
            self._fn: Callable[[str], ExecResult] | None = None
        elif callable(responses):
            self._map = None
            self._fn = responses
        else:
            self._map = dict(responses)
            self._fn = None

    async def run(
        self, cmd: str, *, timeout_s: int = 60, workdir: str | None = None
    ) -> ExecResult:
        self.commands.append(cmd)
        if self._fn is not None:
            return self._fn(cmd)
        if self._map is not None and cmd in self._map:
            return self._map[cmd]
        return ExecResult(stdout=cmd, exit_code=0)

    async def read_file(self, path: str) -> bytes:
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def new_workspace(self, challenge_id: str) -> str:
        return f"/ws/{challenge_id}"


# --------------------------------------------------------------------------
# ResultSink
# --------------------------------------------------------------------------
class RecordingResultSink:
    """``ResultSink`` that records instead of writing — and never raises.

    Keeps the most recent result on ``last`` and counts calls on ``writes``, so
    a test can assert that the ``finally``-path result was written (and what it
    contained) without touching disk. Sync, like the real sink.
    """

    def __init__(self) -> None:
        self.last: RunResult | None = None
        self.writes: int = 0

    def write(self, result: RunResult) -> None:
        try:
            self.last = result
            self.writes += 1
        except Exception:
            # The port's one hard rule: a sink write must never raise.
            pass
