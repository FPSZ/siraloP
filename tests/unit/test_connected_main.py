"""Connected CLI failures produce safe diagnostics and a usable result artifact."""
from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace

import pytest

from agent.domain.state import Challenge, LLMResponse, Question, ToolCall
from agent.entrypoints import connected_main
from agent.testing.fakes import (
    FakeClock,
    FakeExecution,
    FakeLLM,
    FakePlatform,
    RecordingResultSink,
)
from agent.wiring import Wiring


@pytest.mark.parametrize("failure_stage", ["initialization", "challenge_fetch"])
def test_failure_diagnostics_do_not_expose_credentials(
    tmp_path, monkeypatch, capsys, failure_stage,
):
    secret = 'synthetic-connected-"credential\\with-unicode-雪'
    monkeypatch.setattr(
        "agent.config.os.environ", {"CTFY_WORK_DIR": str(tmp_path), "CTFY_TOKEN": secret}
    )
    result_path = tmp_path / "custom-result.json"
    (tmp_path / "task.json").write_text(
        json.dumps({"result": {"path": str(result_path)}}), encoding="utf-8"
    )

    class Platform(FakePlatform):
        async def get_challenge(self, cid):
            raise RuntimeError("platform diagnostic " + secret)

    def build(_settings):
        if failure_stage == "initialization":
            raise RuntimeError("initialization diagnostic " + secret)
        return Wiring(
            Platform(), FakeLLM(), FakeExecution(), RecordingResultSink(), FakeClock()
        )

    monkeypatch.setattr(connected_main, "build_for_dev", build)
    monkeypatch.setattr(sys, "argv", ["connected", "--challenge", "c1"])
    assert connected_main.main() == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert json.dumps(secret, ensure_ascii=False)[1:-1] not in captured.out + captured.err
    assert "Traceback" not in captured.err
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert "RuntimeError" in result["stop_reason"]
    assert secret not in result["stop_reason"]
    assert result["fully_solved"] is False
    assert result["answers"] == {}


def test_connected_cli_still_delivers_a_verified_qa_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agent.config.os.environ", {"CTFY_WORK_DIR": str(tmp_path)})
    challenge = Challenge("c1", questions=[Question("q1", fmt="single")])
    platform = FakePlatform([challenge], answers={("c1", "q1"): "A"})
    wiring = Wiring(
        platform,
        FakeLLM([LLMResponse(tool_calls=[ToolCall("submit_flag", {"flag": "A"})])]),
        FakeExecution(), RecordingResultSink(), FakeClock(),
    )
    monkeypatch.setattr(connected_main, "build_for_dev", lambda _settings: wiring)
    monkeypatch.setattr(sys, "argv", ["connected", "--challenge", "c1"])
    assert connected_main.main() == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["answers"] == {"q1": "A"}
    assert result["fully_solved"] is True
    assert capsys.readouterr().err == ""


def test_argument_error_does_not_echo_known_credential(tmp_path, monkeypatch, capsys):
    secret = "synthetic-connected-argument-credential"
    monkeypatch.setattr(
        "agent.config.os.environ", {"CTFY_WORK_DIR": str(tmp_path), "CTFY_TOKEN": secret}
    )
    monkeypatch.setattr(sys, "argv", ["connected", "--unsupported", secret])
    assert connected_main.main() == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


async def test_connected_cancellation_preserves_partial_results(tmp_path, monkeypatch):
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
    monkeypatch.setattr(connected_main, "build_for_dev", lambda _settings: w)
    run = asyncio.create_task(connected_main._run(Namespace(challenge="c1")))
    await entered.wait()
    run.cancel()
    result = await run

    saved = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result.answers == saved["answers"] == {"q1": "A"}
    assert saved["turns"] == 2
    assert saved["input_tokens"] == 7
    assert saved["output_tokens"] == 3
    assert saved["fully_solved"] is False
