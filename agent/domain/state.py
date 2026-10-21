"""Core data models (pure). Shared by ports and domain.

Keep these as plain, serializable value objects — no I/O, no framework.

NOTE (coordination): this module is the designed merge hotspot. Adding a field
here is cheap; two people editing it the same day is the one guaranteed
conflict. Land schema changes as their own small PR (owner: domain/state), then
parallelize the behavior that consumes each field.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# run modes (CTFY-0001 §2) — the five the platform may dispatch
# --------------------------------------------------------------------------
class RunMode(str, Enum):
    """Which kind of task this run is. Derived by the platform, never chosen.

    The agent track only ever sees INSTANCE and QA; the other three are modeled
    so an unexpected mode fails loudly (write a result, stop) instead of being
    silently mis-run as INSTANCE.
    """

    INSTANCE = "instance"      # jeopardy challenge with infrastructure
    QA = "qa"                  # multiple-choice, no instance
    DEFENCE = "defence"        # AWD+ (out of scope for the agent track)
    AWD = "awd"                # classic attack & defence (out of scope)
    ENGAGEMENT = "engagement"  # GAUNTLET range (out of scope)

    @classmethod
    def parse(cls, value: str | None) -> RunMode:
        """Parse a task.json run_mode string, defaulting to INSTANCE.

        >>> RunMode.parse("qa") is RunMode.QA
        True
        >>> RunMode.parse(None) is RunMode.INSTANCE
        True
        """
        if not value:
            return cls.INSTANCE
        try:
            return cls(value)
        except ValueError:
            return cls.INSTANCE


# --------------------------------------------------------------------------
# platform-facing
# --------------------------------------------------------------------------
@dataclass
class Account:
    """The authenticated principal (GET /me)."""

    id: str
    name: str = ""


@dataclass
class Question:
    """One question of a challenge.

    `locked` is True while a `requires:` gate has not been satisfied; its prompt
    may be empty until it unlocks (re-fetch the challenge after a correct answer).
    `choices` is populated only for QA (multiple-choice) questions.
    """

    id: str
    prompt: str = ""
    requires: list[str] = field(default_factory=list)
    answered: bool = False
    locked: bool = False
    choices: list[str] = field(default_factory=list)   # QA only
    fmt: str = ""                                       # single | multi | static | dynamic | ...


@dataclass
class Challenge:
    """A challenge and its (possibly gated) questions."""

    id: str
    name: str = ""
    category: str = ""            # web | pwn | ai | ...
    description: str = ""
    questions: list[Question] = field(default_factory=list)
    gated_questions: int = 0      # how many questions are still gated
    difficulty: str = ""          # easy | medium | hard | ""
    points: int = 0               # for ROI ordering (0 if unknown)
    solves: int = 0

    def unanswered(self) -> list[Question]:
        """Questions not yet answered and not currently locked."""
        return [q for q in self.questions if not q.answered and not q.locked]


class InstanceStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class Instance:
    """A running challenge environment (the service the agent interacts with)."""

    id: str
    host: str = ""                # challenge-service address
    port: int = 443               # TLS+SNI shares 443
    tls_sni: bool = True
    status: InstanceStatus = InstanceStatus.READY
    poll_after_s: float = 2.0     # how long to wait before re-checking readiness
    expires_at: float | None = None  # monotonic deadline (ClockPort.now scale), or None

    @property
    def ready(self) -> bool:
        return self.status is InstanceStatus.READY


class VerdictStatus(str, Enum):
    """Outcome of a submission. Distinguishes the four cases that drive different
    control-loop decisions (a bare bool would collapse them)."""

    CORRECT = "correct"
    WRONG = "wrong"
    CAPPED = "capped"       # per-question wrong-attempt cap hit; no attempt spent on grading
    UNGRADED = "ungraded"   # grader unavailable; per CTFY-0002 §5.4 costs no attempt
    PAUSED = "paused"       # competition paused; retry after the session resumes


@dataclass
class Verdict:
    """Result of submitting an answer. Use the `.correct` property for the hot path."""

    status: VerdictStatus
    message: str = ""
    attempts_left: int | None = None

    @property
    def correct(self) -> bool:
        return self.status is VerdictStatus.CORRECT

    @classmethod
    def ok(cls, message: str = "", attempts_left: int | None = None) -> Verdict:
        return cls(VerdictStatus.CORRECT, message, attempts_left)

    @classmethod
    def wrong(cls, message: str = "", attempts_left: int | None = None) -> Verdict:
        return cls(VerdictStatus.WRONG, message, attempts_left)


@dataclass
class ScoreboardEntry:
    rank: int
    team: str
    score: float = 0.0
    solved: int = 0


@dataclass
class Scoreboard:
    entries: list[ScoreboardEntry] = field(default_factory=list)
    retry_after_s: float | None = None


# --------------------------------------------------------------------------
# model-facing
# --------------------------------------------------------------------------
@dataclass
class Msg:
    role: str                    # system | user | assistant | tool
    content: str


@dataclass
class ToolDef:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


# --------------------------------------------------------------------------
# execution-facing
# --------------------------------------------------------------------------
@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    truncated: bool = False


# --------------------------------------------------------------------------
# the task briefing (CTFY-0001 §1.2 task.json)
# --------------------------------------------------------------------------
@dataclass
class Task:
    """The machine-readable briefing loaded from $CTFY_WORK_DIR/task.json.

    Carries everything the platform tells us about this run so the control loop
    never has to guess (run_mode, the challenge, limits, where the result goes).
    """

    run_mode: RunMode = RunMode.INSTANCE
    schema_version: int = 2
    challenge: Challenge | None = None
    competition_id: str = ""
    max_turns: int | None = None
    result_path: str = "/ctfy/work/result.json"
    stdout_fallback: bool = False
    dry_run: bool = False
    target_instance: str | None = None   # engagement: estate already running
    scope: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# run state / result
# --------------------------------------------------------------------------
class RunStatus(str, Enum):
    RUNNING = "running"
    SOLVED = "solved"
    GIVEN_UP = "given_up"
    FAILED = "failed"


@dataclass
class Budget:
    """Turn / token / wall-clock caps for one unit of work (a run or a lane).

    Pure value object — it does not read any clock. The control loop passes the
    current monotonic time (from ClockPort) into `exhausted(now=...)`.
    """

    max_turns: int | None = None
    max_tokens: int | None = None
    deadline: float | None = None     # monotonic (ClockPort.now scale), or None
    used_turns: int = 0
    used_tokens: int = 0
    cost_usd: float = 0.0

    def exhausted(self, now: float | None = None) -> bool:
        """True once any configured cap is reached.

        >>> Budget(max_turns=3, used_turns=3).exhausted()
        True
        >>> Budget(max_turns=3, used_turns=1).exhausted()
        False
        >>> Budget(deadline=100.0).exhausted(now=101.0)
        True
        """
        if self.max_turns is not None and self.used_turns >= self.max_turns:
            return True
        if self.max_tokens is not None and self.used_tokens >= self.max_tokens:
            return True
        if self.deadline is not None and now is not None and now >= self.deadline:
            return True
        return False

    def charge(self, *, turns: int = 0, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.used_turns += turns
        self.used_tokens += input_tokens + output_tokens


@dataclass
class Finding:
    """A structured note shared on the bus (no raw tool streams)."""

    source: str
    category: str
    content: str
    confidence: float = 0.5
    ts: float = 0.0


@dataclass
class RunResult:
    """What we write to result.json (CTFY-0001 §3). Every field is optional; the
    platform reads what it needs. `answers` is the post-run submission channel."""

    schema_version: int = 2
    answers: dict[str, str] = field(default_factory=dict)
    solved: list[str] = field(default_factory=list)
    fully_solved: bool = False
    turns: int = 0
    tool_calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    writeup: str = ""
    stop_reason: str = ""
