"""Unit: the image entrypoint's CTFY-0001 §3 guarantee — a result.json is written
on EVERY exit path (dry-run, domain error, success) and main() always returns 0.
These are sync tests: main() owns its own event loop via anyio.run.
"""
from __future__ import annotations

import json

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
