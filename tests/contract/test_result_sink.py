"""Contract: CtfyResultSink honors the frozen ResultSink semantics — atomic,
dir-creating, secret-scrubbing, and (the one hard rule) it NEVER raises.
"""
from __future__ import annotations

import json
import os

from agent.adapters.result.ctfy_result import CtfyResultSink, redact
from agent.domain.state import RunResult


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
