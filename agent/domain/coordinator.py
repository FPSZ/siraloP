"""Coordinator: global scheduler over challenges. Keep it 'dumb but steady' (ADR-0002 §3).

Only: pick order (ROI — easy/medium first), allocate budget, build a Swarm per
challenge, accumulate results, honor the paused session. It NEVER solves a
challenge itself, and on ANY failure path it still returns a best-effort
RunResult (the entrypoint always writes it — CTFY-0001 §3: not writing == failed).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import cast

from agent.domain.bus import MessageBus
from agent.domain.solver import CliHarnessSolver, ReActSolver, SolverStrategy
from agent.domain.state import (
    Budget,
    Challenge,
    Instance,
    InstanceStatus,
    RunMode,
    RunResult,
    RunStatus,
    Task,
)
from agent.domain.swarm import Swarm
from agent.ports.clock import ClockPort
from agent.ports.execution import ExecutionPort
from agent.ports.llm import LLMPort
from agent.ports.platform import PlatformPort
from agent.ports.result import ResultSink

# ROI difficulty ordering: cheapest points first (ADR-0002 §7).
_DIFFICULTY_RANK = {"easy": 0, "medium": 1, "": 2, "hard": 3}
# Single deep lane per challenge. Racing CLONES (same model + same prompt, see
# _models) only split the shared budget and dilute each chain's depth — the
# opposite of what a 5-layer puzzle needs (docs/testing/工作流诊断.md). Keep width
# at 1 until lanes are GENUINELY diverse (distinct models / strategies).
_LANES_BY_DIFFICULTY = {"easy": 1, "medium": 1, "hard": 1}
_DEFAULT_LANES = 1


class Coordinator:
    def __init__(
        self,
        platform: PlatformPort,
        llm: LLMPort,
        execution: ExecutionPort,
        result: ResultSink,
        *,
        clock: ClockPort,
        task: Task | None = None,
        settings: object | None = None,
    ) -> None:
        self.platform = platform
        self.llm = llm
        self.execution = execution
        self.result = result
        self.clock = clock
        self.task = task
        self.settings = settings
        self.bus = MessageBus()
        self._current_result = RunResult(stop_reason="not_started")

    @property
    def current_result(self) -> RunResult:
        """Latest verified answers and accounting, including interrupted runs."""
        return self._current_result

    # -- model selection ----------------------------------------------------
    def _models(self) -> list[str]:
        """Race lanes over whatever models we were funded for (ADR-0002 §6).

        Falls back to a single placeholder id so the loop is still constructible
        with fakes / in dev; the real model id comes from Settings (LLM_MODEL).
        """
        primary = getattr(self.settings, "llm_model", "") or "default"
        # A second lane reuses the primary id unless the team wires a distinct
        # second model later; distinct ids are swapped in here, not in domain.
        return [primary, primary, primary]

    # -- ROI ordering -------------------------------------------------------
    @staticmethod
    def _roi_key(c: Challenge) -> tuple:
        diff = (c.difficulty or "").strip().lower()
        drank = _DIFFICULTY_RANK.get(diff, 2)
        has_open = 0 if c.unanswered() else 1  # challenges with work to do first
        # tiebreak by points/solves ROI: more solves (easier) first, then points.
        return (drank, has_open, -c.solves, -c.points)

    def _order(self, challenges: list[Challenge]) -> list[Challenge]:
        return sorted(challenges, key=self._roi_key)

    # -- budgeting ----------------------------------------------------------
    def _allocate_budget(self) -> Budget:
        """One run-level budget shared by every challenge and solver lane."""
        max_turns = (
            self.task.max_turns
            if self.task is not None
            else getattr(self.settings, "max_turns", None)
        )
        limits = self.task.limits if self.task is not None else {}
        max_tokens = limits.get("max_tokens")
        if not isinstance(max_tokens, int):
            max_tokens = getattr(self.settings, "max_tokens", None)
        max_wall_s = getattr(self.settings, "max_wall_s", None)
        return Budget(
            max_turns=max_turns if isinstance(max_turns, int) else None,
            max_tokens=max_tokens if isinstance(max_tokens, int) else None,
            deadline=(
                self.clock.now() + max_wall_s
                if isinstance(max_wall_s, (int, float)) else None
            ),
        )

    # -- lane construction --------------------------------------------------
    def _build_lanes(
        self,
        challenge: Challenge,
        budget: Budget,
        *,
        instance: Instance | None = None,
        run_mode: RunMode = RunMode.INSTANCE,
        on_progress: Callable[[], None] | None = None,
    ) -> tuple[list[SolverStrategy], list[object]]:
        """Build competing lanes against the same run budget and ready target."""
        diff = (challenge.difficulty or "").strip().lower()
        want = _LANES_BY_DIFFICULTY.get(diff, _DEFAULT_LANES)
        models = self._models()[:want] or ["default"]
        lanes: list[object] = []
        for model in models:
            lanes.append(
                ReActSolver(
                    self.llm,
                    self.execution,
                    self.platform,
                    self.bus,
                    model=model,
                    clock=self.clock,
                    budget=budget,
                    instance=instance,
                    run_mode=run_mode,
                    on_progress=on_progress,
                )
            )
        cli = getattr(self.settings, "harness_cli", "")
        if cli:
            lanes.append(
                CliHarnessSolver(
                    self.execution,
                    self.platform,
                    cli=cli,
                    clock=self.clock,
                    budget=budget,
                    instance=instance,
                    run_mode=run_mode,
                    on_progress=on_progress,
                )
            )
        strategies = cast(list[SolverStrategy], list(lanes))
        return strategies, lanes

    # -- the run ------------------------------------------------------------
    async def run(self) -> RunResult:
        self._current_result = RunResult()
        budget = self._allocate_budget()
        self.result.write(self._current_result)
        try:
            return await self._run_inner(budget)
        finally:
            self.result.write(self._current_result)

    async def _run_inner(self, budget: Budget) -> RunResult:
        result = self._current_result
        try:
            challenges = await self._obtain_challenges()
        except Exception as e:
            result.stop_reason = f"error: could not obtain challenges: {e!r}"
            return result

        ordered = self._order(challenges)
        if not ordered:
            result.stop_reason = "no challenges"
            return result

        statuses: list[RunStatus] = []
        for challenge in ordered:
            try:
                status = await self._solve_one(challenge, budget, result)
                statuses.append(status)
            except Exception as e:  # one bad challenge never sinks the run
                statuses.append(RunStatus.FAILED)
                result.stop_reason = f"error on {challenge.id}: {e!r}"
                continue

        result.fully_solved = len(statuses) == len(ordered) and all(
            s is RunStatus.SOLVED for s in statuses
        )
        if not result.stop_reason:
            if result.fully_solved:
                result.stop_reason = "completed"
            elif budget.deadline is not None and self.clock.now() >= budget.deadline:
                result.stop_reason = "timeout"
            elif budget.exhausted(now=self.clock.now()):
                result.stop_reason = "budget_exhausted"
            else:
                result.stop_reason = "incomplete"
        return result

    async def _obtain_challenges(self) -> list[Challenge]:
        if self.task is not None and self.task.challenge is not None:
            return [self.task.challenge]
        return list(await self.platform.list_challenges())

    def _run_mode(self, challenge: Challenge) -> RunMode:
        if self.task is not None and (
            self.task.raw.get("run_mode") or self.task.run_mode is not RunMode.INSTANCE
        ):
            return self.task.run_mode
        if challenge.questions and all(
            q.choices or q.fmt in ("single", "multi") for q in challenge.questions
        ):
            return RunMode.QA
        return RunMode.INSTANCE

    async def _prepare_instance(self, cid: str, budget: Budget) -> Instance | None:
        if budget.exhausted(now=self.clock.now()):
            return None
        instance = await self.platform.start_instance(cid)
        while instance.status is InstanceStatus.STARTING:
            if budget.exhausted(now=self.clock.now()):
                return None
            delay = instance.poll_after_s
            if budget.deadline is not None:
                delay = min(delay, max(0.0, budget.deadline - self.clock.now()))
            await self.clock.sleep(delay)
            if budget.exhausted(now=self.clock.now()):
                return None
            instance = await self.platform.start_instance(cid)
        if not instance.ready:
            raise RuntimeError(
                f"instance {instance.id!r} did not become ready "
                f"(status={instance.status.value})"
            )
        # A ready instance need not expose a network host: reverse / crypto /
        # misc challenges ship their work as staged attachments and keep the
        # instance only for the dynamic flag + grading (services:[], host="").
        # Requiring a host here wrongly rejected every attachment-only target.
        return instance

    async def _solve_one(
        self, challenge: Challenge, budget: Budget, result: RunResult
    ) -> RunStatus:
        mode = self._run_mode(challenge)
        if mode not in (RunMode.INSTANCE, RunMode.QA):
            raise ValueError(f"unsupported run mode: {mode.value}")
        if (
            challenge.questions
            and challenge.gated_questions == 0
            and all(q.answered for q in challenge.questions)
        ):
            return RunStatus.SOLVED
        if budget.exhausted(now=self.clock.now()):
            return RunStatus.GIVEN_UP
        if not challenge.questions or (
            not challenge.unanswered() and challenge.gated_questions > 0
        ):
            answered = {q.id for q in challenge.questions if q.answered}
            challenge = await self.platform.get_challenge(challenge.id)
            for question in challenge.questions:
                question.answered = question.answered or question.id in answered
            mode = self._run_mode(challenge)
        if not challenge.questions:
            raise ValueError(f"challenge {challenge.id!r} has no questions")
        if not challenge.unanswered():
            return (
                RunStatus.SOLVED
                if challenge.gated_questions == 0 and all(q.answered for q in challenge.questions)
                else RunStatus.GIVEN_UP
            )
        instance = None
        if mode is RunMode.INSTANCE:
            instance = await self._prepare_instance(challenge.id, budget)
            if instance is None:
                return RunStatus.GIVEN_UP
        if budget.exhausted(now=self.clock.now()):
            return RunStatus.GIVEN_UP
        baseline = (
            result.turns, result.tool_calls_made, result.input_tokens, result.output_tokens
        )
        lanes: list[object] = []

        def checkpoint() -> None:
            self._accumulate(lanes, result, baseline)
            self.result.write(result)

        strategies, lanes = self._build_lanes(
            challenge, budget, instance=instance, run_mode=mode, on_progress=checkpoint
        )
        lane_max = _LANES_BY_DIFFICULTY.get(
            (challenge.difficulty or "").strip().lower(), _DEFAULT_LANES
        )
        swarm = Swarm(strategies, clock=self.clock, max_lanes=max(lane_max, 1))
        try:
            return await swarm.solve(challenge)
        finally:
            checkpoint()

    def _accumulate(
        self, lanes: list[object], result: RunResult, baseline: tuple[int, int, int, int]
    ) -> None:
        """Refresh lane totals without adding cumulative counters more than once."""
        turns, tool_calls, input_tokens, output_tokens = baseline
        for lane in lanes:
            for qid, answer in getattr(lane, "answers", {}).items():
                result.answers.setdefault(qid, answer)
            for qid in getattr(lane, "solved_questions", []):
                if qid not in result.solved:
                    result.solved.append(qid)
            turns += getattr(lane, "turns", 0)
            tool_calls += getattr(lane, "tool_calls_made", 0)
            input_tokens += getattr(lane, "input_tokens", 0)
            output_tokens += getattr(lane, "output_tokens", 0)
        result.turns = turns
        result.tool_calls_made = tool_calls
        result.input_tokens = input_tokens
        result.output_tokens = output_tokens
