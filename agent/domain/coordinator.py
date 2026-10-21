"""Coordinator: global scheduler over challenges. Keep it 'dumb but steady' (ADR-0002 §3).

Only: pick order (ROI — easy/medium first), allocate budget, build a Swarm per
challenge, accumulate results, honor the paused session. It NEVER solves a
challenge itself, and on ANY failure path it still returns a best-effort
RunResult (the entrypoint always writes it — CTFY-0001 §3: not writing == failed).
"""
from __future__ import annotations

from agent.domain.bus import MessageBus
from agent.domain.solver import CliHarnessSolver, ReActSolver, SolverStrategy
from agent.domain.state import (
    Budget,
    Challenge,
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
# Race width by difficulty (easy/medium race, hard goes single-lane in the Swarm).
_LANES_BY_DIFFICULTY = {"easy": 3, "medium": 2, "hard": 1}
_DEFAULT_LANES = 2


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
    def _allocate_budget(self, n_challenges: int) -> Budget:
        """A per-challenge Budget from task.max_turns / limits (ADR-0001 §3.6)."""
        max_turns = None
        max_tokens = None
        if self.task is not None:
            if isinstance(self.task.max_turns, int):
                # share the run-level turn cap across challenges (floor of 1)
                max_turns = max(1, self.task.max_turns // max(1, n_challenges))
            limits = self.task.limits or {}
            mt = limits.get("max_tokens")
            if isinstance(mt, int):
                max_tokens = max(1, mt // max(1, n_challenges))
        st_tokens = getattr(self.settings, "max_tokens", None)
        if max_tokens is None and isinstance(st_tokens, int):
            max_tokens = max(1, st_tokens // max(1, n_challenges))
        return Budget(max_turns=max_turns, max_tokens=max_tokens)

    # -- lane construction --------------------------------------------------
    def _build_lanes(
        self, challenge: Challenge, budget: Budget
    ) -> tuple[list[SolverStrategy], list[object]]:
        """Build the solver lanes for one challenge.

        Returns (strategies, lanes): the Swarm consumes ``strategies`` while we
        keep the concrete ``lanes`` to read their answers/accounting afterwards.
        Each lane gets its OWN Budget copy (independent stop-loss per lane).
        """
        diff = (challenge.difficulty or "").strip().lower()
        want = _LANES_BY_DIFFICULTY.get(diff, _DEFAULT_LANES)
        models = self._models()[:want] or ["default"]
        lanes: list[object] = []
        for model in models:
            lane_budget = Budget(
                max_turns=budget.max_turns,
                max_tokens=budget.max_tokens,
                deadline=budget.deadline,
            )
            lanes.append(
                ReActSolver(
                    self.llm,
                    self.execution,
                    self.platform,
                    self.bus,
                    model=model,
                    clock=self.clock,
                    budget=lane_budget,
                )
            )
        cli = getattr(self.settings, "harness_cli", "")
        if cli:
            lanes.append(
                CliHarnessSolver(
                    self.execution, self.platform, cli=cli, clock=self.clock
                )
            )
        return list(lanes), lanes  # type: ignore[return-value]

    # -- the run ------------------------------------------------------------
    async def run(self) -> RunResult:
        result = RunResult()
        try:
            challenges = await self._obtain_challenges()
        except Exception as e:
            result.stop_reason = f"error: could not obtain challenges: {e!r}"
            return result

        ordered = self._order(challenges)
        if not ordered:
            result.stop_reason = "no challenges"
            return result

        budget = self._allocate_budget(len(ordered))
        statuses: list[RunStatus] = []
        for challenge in ordered:
            try:
                status = await self._solve_one(challenge, budget, result)
                statuses.append(status)
            except Exception as e:  # one bad challenge never sinks the run
                result.stop_reason = f"error on {challenge.id}: {e!r}"
                continue

        result.fully_solved = bool(ordered) and all(
            s is RunStatus.SOLVED for s in statuses
        )
        if not result.stop_reason:
            result.stop_reason = "completed"
        return result

    async def _obtain_challenges(self) -> list[Challenge]:
        if self.task is not None and self.task.challenge is not None:
            return [self.task.challenge]
        return list(await self.platform.list_challenges())

    async def _solve_one(
        self, challenge: Challenge, budget: Budget, result: RunResult
    ) -> RunStatus:
        if not challenge.unanswered():
            return RunStatus.SOLVED
        strategies, lanes = self._build_lanes(challenge, budget)
        lane_max = _LANES_BY_DIFFICULTY.get(
            (challenge.difficulty or "").strip().lower(), _DEFAULT_LANES
        )
        swarm = Swarm(strategies, clock=self.clock, max_lanes=max(lane_max, 1))
        status = await swarm.solve(challenge)
        self._accumulate(lanes, result)
        return status

    def _accumulate(self, lanes: list[object], result: RunResult) -> None:
        """Fold each lane's verified answers + accounting into the RunResult."""
        for lane in lanes:
            for qid, answer in getattr(lane, "answers", {}).items():
                result.answers.setdefault(qid, answer)
            for qid in getattr(lane, "solved_questions", []):
                if qid not in result.solved:
                    result.solved.append(qid)
            result.turns += getattr(lane, "turns", 0)
            result.tool_calls_made += getattr(lane, "tool_calls_made", 0)
            result.input_tokens += getattr(lane, "input_tokens", 0)
            result.output_tokens += getattr(lane, "output_tokens", 0)
