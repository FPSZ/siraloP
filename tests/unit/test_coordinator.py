"""Unit: the Coordinator orders, delegates, and folds lane outcomes into a
RunResult (it never solves a challenge itself, and never crashes the run)."""
from __future__ import annotations

import asyncio

import pytest

from agent.domain.coordinator import Coordinator
from agent.domain.state import (
    Challenge,
    Instance,
    InstanceStatus,
    LLMResponse,
    Question,
    RunMode,
    Task,
    ToolCall,
)
from agent.testing.fakes import FakeExecution, FakeLLM, FakePlatform, RecordingResultSink
from tests.support import make_challenge


async def test_run_solves_single_challenge(clock):
    ch = make_challenge("c1", difficulty="hard", n_questions=1)  # hard -> 1 lane, deterministic

    def respond(_messages):
        return LLMResponse(
            tool_calls=[ToolCall("submit_flag", {"flag": "ctfy{win}", "question_id": "q1"})]
        )

    coord = Coordinator(
        FakePlatform([ch]), FakeLLM(respond), FakeExecution(), RecordingResultSink(), clock=clock
    )
    result = await coord.run()
    assert "q1" in result.solved
    assert result.answers["q1"] == "ctfy{win}"
    assert result.fully_solved is True


async def test_run_with_no_challenges(clock):
    coord = Coordinator(
        FakePlatform([]), FakeLLM(), FakeExecution(), RecordingResultSink(), clock=clock
    )
    result = await coord.run()
    assert result.fully_solved is False


async def test_run_survives_platform_error(clock):
    class Broken(FakePlatform):
        async def list_challenges(self):
            raise RuntimeError("platform down")

    coord = Coordinator(
        Broken(), FakeLLM(), FakeExecution(), RecordingResultSink(), clock=clock
    )
    result = await coord.run()  # must not raise
    assert result.fully_solved is False


@pytest.mark.parametrize(
    "challenge",
    [
        Challenge("empty"),
        Challenge("locked", questions=[Question("q1", locked=True)]),
    ],
)
async def test_missing_or_locked_questions_are_not_complete(clock, challenge):
    llm = FakeLLM()
    result = await Coordinator(
        FakePlatform([challenge]), llm, FakeExecution(), RecordingResultSink(), clock=clock
    ).run()

    assert not result.fully_solved
    assert result.answers == {}
    assert llm.calls == []


async def test_metadata_only_task_fetches_questions_before_solving(clock):
    full = make_challenge("c1", difficulty="hard")
    llm = FakeLLM([
        LLMResponse(tool_calls=[ToolCall("submit_flag", {"flag": "ctfy{win}"})])
    ])
    result = await Coordinator(
        FakePlatform([full]), llm, FakeExecution(), RecordingResultSink(),
        clock=clock, task=Task(challenge=Challenge("c1"), max_turns=1),
    ).run()

    assert result.fully_solved
    assert result.answers == {"q1": "ctfy{win}"}


async def test_instance_waits_for_readiness_before_model_work(clock):
    challenge = make_challenge(difficulty="hard")

    class StartingPlatform(FakePlatform):
        starts = 0
        ready = False

        async def start_instance(self, cid):
            self.starts += 1
            self.ready = self.starts == 2
            return Instance(
                cid, host="challenge.local", port=9443,
                status=InstanceStatus.READY if self.ready else InstanceStatus.STARTING,
                poll_after_s=4,
            )

    platform = StartingPlatform([challenge])

    def respond(_messages):
        assert platform.ready
        return LLMResponse(
            tool_calls=[ToolCall("submit_flag", {"flag": "ctfy{ready}"})]
        )

    result = await Coordinator(
        platform, FakeLLM(respond), FakeExecution(), RecordingResultSink(), clock=clock
    ).run()

    assert result.answers == {"q1": "ctfy{ready}"}
    assert result.fully_solved
    assert platform.starts == 2
    assert clock.slept == [4]


@pytest.mark.parametrize("status", [InstanceStatus.ERROR, InstanceStatus.STOPPED])
async def test_unavailable_instance_does_not_run_model_or_report_success(clock, status):
    challenge = make_challenge()

    class UnavailablePlatform(FakePlatform):
        async def start_instance(self, cid):
            return Instance(cid, status=status)

    llm = FakeLLM()
    result = await Coordinator(
        UnavailablePlatform([challenge]), llm, FakeExecution(), RecordingResultSink(),
        clock=clock,
    ).run()

    assert not result.fully_solved
    assert result.answers == {}
    assert llm.calls == []


async def test_ready_instance_without_host_runs_model(clock):
    # Reverse / crypto / misc instances are ready but expose no network service
    # (services:[], host=""); the challenge is the staged attachment. A ready
    # host-less target must still run the model, not be rejected as unreachable.
    challenge = make_challenge(difficulty="hard")

    class HostlessPlatform(FakePlatform):
        async def start_instance(self, cid):
            return Instance(cid, host="", status=InstanceStatus.READY)

    def respond(_messages):
        return LLMResponse(tool_calls=[ToolCall("submit_flag", {"flag": "ctfy{attach}"})])

    result = await Coordinator(
        HostlessPlatform([challenge]), FakeLLM(respond), FakeExecution(),
        RecordingResultSink(), clock=clock,
    ).run()

    assert result.fully_solved
    assert result.answers == {"q1": "ctfy{attach}"}


async def test_qa_uses_no_instance_even_without_select_metadata(clock):
    challenge = make_challenge(difficulty="hard")

    class QaPlatform(FakePlatform):
        async def start_instance(self, cid):
            raise AssertionError("QA must not start infrastructure")

        async def submit(self, cid, qid, answer):
            raise AssertionError("QA must not use jeopardy grading")

    platform = QaPlatform([challenge], answers={("c1", "q1"): "A"})
    llm = FakeLLM([
        LLMResponse(tool_calls=[ToolCall("submit_flag", {"flag": "A"})])
    ])
    result = await Coordinator(
        platform, llm, FakeExecution(), RecordingResultSink(), clock=clock,
        task=Task(run_mode=RunMode.QA, challenge=challenge, max_turns=1),
    ).run()

    assert result.fully_solved
    assert result.answers == {"q1": "A"}


async def test_competing_lanes_obey_one_run_turn_budget(clock):
    class YieldingLLM(FakeLLM):
        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            await asyncio.sleep(0)
            return response

    challenge = make_challenge(difficulty="easy")
    llm = YieldingLLM(lambda _messages: LLMResponse(input_tokens=7, output_tokens=3))
    result = await Coordinator(
        FakePlatform([challenge]), llm, FakeExecution(), RecordingResultSink(),
        clock=clock, task=Task(challenge=challenge, max_turns=2),
    ).run()

    assert len(llm.calls) == result.turns == 2
    assert result.input_tokens == 14
    assert result.output_tokens == 6
    assert not result.fully_solved


@pytest.mark.parametrize("token_limit", [False, True])
async def test_later_challenges_cannot_reuse_spent_run_budget(clock, token_limit):
    challenges = [
        make_challenge(f"c{i}", difficulty="hard", questions=[Question(f"q{i}")])
        for i in range(3)
    ]
    llm = FakeLLM([
        LLMResponse(
            tool_calls=[ToolCall("submit_flag", {"flag": f"ctfy{{{i}}}"})],
            input_tokens=7, output_tokens=3,
        )
        for i in range(3)
    ])
    task = Task(
        max_turns=None if token_limit else 2,
        limits={"max_tokens": 20} if token_limit else {},
    )
    result = await Coordinator(
        FakePlatform(challenges), llm, FakeExecution(), RecordingResultSink(),
        clock=clock, task=task,
    ).run()

    assert result.answers == {"q0": "ctfy{0}", "q1": "ctfy{1}"}
    assert len(llm.calls) == result.turns == 2
    assert result.input_tokens + result.output_tokens == 20
    assert not result.fully_solved


async def test_zero_turn_budget_starts_no_model_or_instance(clock):
    challenge = make_challenge()

    class NoStartPlatform(FakePlatform):
        async def start_instance(self, cid):
            raise AssertionError("an exhausted run must not start an instance")

    llm = FakeLLM()
    result = await Coordinator(
        NoStartPlatform([challenge]), llm, FakeExecution(), RecordingResultSink(),
        clock=clock, task=Task(challenge=challenge, max_turns=0),
    ).run()

    assert not result.fully_solved
    assert result.turns == 0
    assert llm.calls == []
