"""Contract: CtfyResultSink honors the frozen ResultSink semantics — atomic,
dir-creating, secret-scrubbing, and (the one hard rule) it NEVER raises.
"""
from __future__ import annotations

import json
import os

import pytest

from agent.adapters._redaction import redact
from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.domain.state import RunResult


@pytest.fixture(autouse=True)
def isolated_control_credentials(monkeypatch):
    for name in (
        "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_writes_and_roundtrips(tmp_path):
    p = tmp_path / "nested" / "deep" / "result.json"
    CtfyResultSink(str(p)).write(
        RunResult(solved=["q1"], answers={"q1": "ctfy{x}"}, fully_solved=True)
    )
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["solved"] == ["q1"]
    assert data["fully_solved"] is True
    assert data["answers"]["q1"] == "ctfy{x}"


def test_creates_missing_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "result.json"
    CtfyResultSink(str(p)).write(RunResult())
    assert p.exists()


def test_atomic_leaves_no_temp_litter(tmp_path):
    p = tmp_path / "result.json"
    CtfyResultSink(str(p)).write(RunResult(writeup="x" * 1000))
    assert [name for name in os.listdir(tmp_path)] == ["result.json"]


def test_never_raises_on_unwritable_path(tmp_path):
    # Parent is a FILE, so makedirs() on a child path fails — must stay silent.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    sink = CtfyResultSink(str(blocker / "sub" / "result.json"))
    sink.write(RunResult())  # no exception
    assert blocker.read_text() == "x"


def test_redact_masks_env_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-supersecret-value-123")
    monkeypatch.setenv("CTFY_TOKEN", "pf_tok_abcdef123456")
    out = redact("key=sk-supersecret-value-123 tok=pf_tok_abcdef123456 auth=Bearer zzz.yyy")
    assert "sk-supersecret-value-123" not in out
    assert "pf_tok_abcdef123456" not in out
    assert "zzz.yyy" not in out  # bearer credential scrubbed
    assert "***" in out


def test_secret_scrubbed_in_written_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "pf_tok_abcdef123456")
    p = tmp_path / "result.json"
    CtfyResultSink(str(p)).write(RunResult(writeup="leaked pf_tok_abcdef123456 oops"))
    assert "pf_tok_abcdef123456" not in p.read_text(encoding="utf-8")


def test_structured_secrets_keys_and_fallback_values(tmp_path, monkeypatch):
    secret = 'synthetic-"quoted"\\path-雪'
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    class OddValue:
        def __str__(self):
            return f"odd {secret} Bearer unknown.jwt"

    p = tmp_path / "result.json"
    CtfyResultSink(str(p)).write(
        RunResult(answers={secret: OddValue()}, writeup=f"Bearer {secret}")
    )
    data = json.loads(p.read_text(encoding="utf-8"))
    assert secret not in str(data)
    assert data["answers"] == {"***": "odd *** Bearer ***"}
    assert data["writeup"] == "Bearer ***"


def test_stdout_fallback_scrubs_structured_payload(tmp_path, monkeypatch, capsys):
    secret = 'synthetic-"quoted"\\fallback'
    monkeypatch.setenv("CTFY_TOKEN", secret)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    CtfyResultSink(str(blocker / "result.json"), stdout_fallback=True).write(
        RunResult(answers={secret: secret})
    )
    assert json.loads(capsys.readouterr().out)["answers"] == {"***": "***"}


def test_short_credential_does_not_rename_fixed_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "a")
    p = tmp_path / "result.json"
    CtfyResultSink(str(p)).write(RunResult(answers={"a": "a"}, writeup="a"))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["answers"] == {"***": "***"}
    assert data["writeup"] == "***"


def test_dynamic_key_collision_persists_unambiguous_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "CONTROL")
    p = tmp_path / "result.json"
    sink = CtfyResultSink(str(p))
    sink.write(RunResult(fully_solved=True))
    sink.write(RunResult(
        answers={"qCONTROL": "one", "q***": "two"},
        solved=["qCONTROL", "q***"], fully_solved=True,
    ))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["fully_solved"] is False
    assert data["answers"] == {}
    assert data["solved"] == []
    assert data["stop_reason"] == "redaction_error: ambiguous_keys"
    assert "CONTROL" not in p.read_text(encoding="utf-8")
    assert [path.name for path in tmp_path.iterdir()] == ["result.json"]
