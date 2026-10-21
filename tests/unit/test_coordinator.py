"""Unit: the Coordinator orders, delegates, and folds lane outcomes into a
RunResult (it never solves a challenge itself, and never crashes the run)."""
from __future__ import annotations

from agent.domain.coordinator import Coordinator
from agent.domain.state import LLMResponse, ToolCall
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
    assert result.stop_reason == "completed"


async def test_run_with_no_challenges(clock):
    coord = Coordinator(
        FakePlatform([]), FakeLLM(), FakeExecution(), RecordingResultSink(), clock=clock
    )
    result = await coord.run()
    assert result.fully_solved is False
    assert result.stop_reason == "no challenges"


async def test_run_survives_platform_error(clock):
    class Broken(FakePlatform):
        async def list_challenges(self):
            raise RuntimeError("platform down")

    coord = Coordinator(
        Broken(), FakeLLM(), FakeExecution(), RecordingResultSink(), clock=clock
    )
    result = await coord.run()  # must not raise
    assert result.fully_solved is False
    assert "error" in result.stop_reason
