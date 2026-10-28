"""Unit: the image entrypoint's CTFY-0001 §3 guarantee — a result.json is written
on EVERY exit path (dry-run, domain error, success) and main() always returns 0.
These are sync tests: main() owns its own event loop via anyio.run.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import agent.entrypoints.image_main as im
from agent.domain.state import Challenge, LLMResponse, Question, ToolCall
from agent.testing.fakes import (
    FakeClock,
    FakeExecution,
    FakeLLM,
    FakePlatform,
    RecordingResultSink,
)
from agent.wiring import Wiring


def test_dry_run_writes_minimal_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFY_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("CTFY_EVAL_DRY_RUN", "1")
    assert im.main() == 0
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert data["stop_reason"] == "dry_run"


def test_error_path_still_writes_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFY_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("CTFY_EVAL_DRY_RUN", raising=False)

    def boom(_settings):
        raise RuntimeError("no network in test")

    monkeypatch.setattr(im, "build_for_image", boom)
    assert im.main() == 0  # never crashes
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert data["stop_reason"].startswith("error")


def test_success_path_writes_solved(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFY_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("CTFY_EVAL_DRY_RUN", raising=False)
    ch = Challenge(id="c1", difficulty="hard", questions=[Question(id="q1", prompt="flag?")])

    def respond(_messages):
        return LLMResponse(
            tool_calls=[ToolCall("submit_flag", {"flag": "ctfy{win}", "question_id": "q1"})]
        )

    wiring = Wiring(
        platform=FakePlatform([ch]),
        llm=FakeLLM(respond),
        execution=FakeExecution(),
        result=RecordingResultSink(),
        clock=FakeClock(),
    )
    monkeypatch.setattr(im, "build_for_image", lambda _s: wiring)
    assert im.main() == 0
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert "q1" in data["solved"]


@pytest.mark.parametrize(
    ("run_mode", "answer"), [("instance", "ctfy{standard}"), ("qa", "A")]
)
def test_standard_briefing_runs_and_writes_verified_answer(
    tmp_path, monkeypatch, run_mode, answer
):
    monkeypatch.setattr("agent.config.os.environ", {"CTFY_WORK_DIR": str(tmp_path)})
    (tmp_path / "task.json").write_text(
        json.dumps({
            "schema_version": 2,
            "run_mode": run_mode,
            "challenge": {"id": "c1", "name": "Standard briefing", "difficulty": "hard"},
            "questions": [{"id": "q1", "prompt": "Provide the answer"}],
            "gated_questions": 0,
            "max_turns": 1,
        }),
        encoding="utf-8",
    )
    challenge = Challenge("c1", questions=[Question("q1")])

    class Platform(FakePlatform):
        async def start_instance(self, cid):
            assert run_mode == "instance"
            return await super().start_instance(cid)

        async def submit(self, cid, qid, candidate):
            assert run_mode == "instance"
            return await super().submit(cid, qid, candidate)

        async def submit_qa(self, cid, qid, candidate):
            assert run_mode == "qa"
            return await super().submit_qa(cid, qid, candidate)

    platform = Platform([challenge], answers={("c1", "q1"): answer})
    wiring = Wiring(
        platform=platform,
        llm=FakeLLM([
            LLMResponse(tool_calls=[ToolCall("submit_flag", {"flag": answer})])
        ]),
        execution=FakeExecution(),
        result=RecordingResultSink(),
        clock=FakeClock(),
    )
    monkeypatch.setattr(im, "build_for_image", lambda _s: wiring)

    assert im.main() == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["answers"] == {"q1": answer}
    assert result["solved"] == ["q1"]
    assert result["fully_solved"] is True
    assert result["turns"] == 1


def test_result_is_delivered_before_a_failing_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.config.os.environ", {"CTFY_WORK_DIR": str(tmp_path)})
    challenge = Challenge("c1", questions=[Question("q1", fmt="single")])
    delivered = []

    class BrokenCloseExecution(FakeExecution):
        async def aclose(self):
            delivered.append(json.loads((tmp_path / "result.json").read_text(encoding="utf-8")))
            raise RuntimeError("synthetic cleanup failure")

    w = Wiring(
        FakePlatform([challenge], answers={("c1", "q1"): "A"}),
        FakeLLM([LLMResponse(tool_calls=[ToolCall("submit_flag", {"flag": "A"})])]),
        BrokenCloseExecution(), RecordingResultSink(), FakeClock(),
    )
    monkeypatch.setattr(im, "build_for_image", lambda _settings: w)

    assert im.main() == 0
    assert delivered[0]["answers"] == {"q1": "A"}
    assert delivered[0]["fully_solved"] is True
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result == delivered[0]


async def test_image_cancellation_keeps_already_verified_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.config.os.environ",
        {"CTFY_WORK_DIR": str(tmp_path), "CTFY_EVAL_MAX_TURNS": "3"},
    )
    challenge = Challenge(
        "c1", questions=[Question("q1", fmt="single"), Question("q2", fmt="single")]
    )
    entered = asyncio.Event()

    class PausingModel:
        calls = 0

        async def complete(self, messages, *, model, effort="medium", tools=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    tool_calls=[ToolCall("submit_flag", {"flag": "A", "question_id": "q1"})],
                    input_tokens=7, output_tokens=3,
                )
            entered.set()
            await asyncio.Event().wait()

    w = Wiring(
        FakePlatform([challenge], answers={("c1", "q1"): "A"}),
        PausingModel(), FakeExecution(), RecordingResultSink(), FakeClock(),
    )
    monkeypatch.setattr(im, "build_for_image", lambda _settings: w)
    run = asyncio.create_task(im._run())
    await entered.wait()
    run.cancel()
    await run

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["answers"] == {"q1": "A"}
    assert result["solved"] == ["q1"]
    assert result["turns"] == 2
    assert result["input_tokens"] == 7
    assert result["output_tokens"] == 3
    assert result["fully_solved"] is False


def test_zero_wall_budget_starts_no_model_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.config.os.environ",
        {"CTFY_WORK_DIR": str(tmp_path), "CTFY_AGENT_MAX_WALL_S": "0"},
    )
    challenge = Challenge("c1", questions=[Question("q1", fmt="single")])
    llm = FakeLLM()
    w = Wiring(
        FakePlatform([challenge]), llm, FakeExecution(), RecordingResultSink(), FakeClock()
    )
    monkeypatch.setattr(im, "build_for_image", lambda _settings: w)

    assert im.main() == 0
    assert llm.calls == []
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["answers"] == {}
    assert result["fully_solved"] is False
