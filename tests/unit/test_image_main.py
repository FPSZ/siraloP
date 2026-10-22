"""Unit: the image entrypoint's CTFY-0001 §3 guarantee — a result.json is written
on EVERY exit path (dry-run, domain error, success) and main() always returns 0.
These are sync tests: main() owns its own event loop via anyio.run.
"""
from __future__ import annotations

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
