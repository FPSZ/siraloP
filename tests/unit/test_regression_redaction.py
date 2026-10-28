"""Run the real regression CLI output pipeline with local synthetic adapters."""
from __future__ import annotations

import json
import sys

import pytest

from agent.adapters._redaction import contains_secret
from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.domain.state import ExecResult, LLMResponse, RunResult, ToolCall
from tools.regression import run

_ENV_NAMES = (
    "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
)
_SECRET = 'synthetic-control-"quote"\\雪-token'
_PLAIN_SECRET = "p1-preview-credential-0123456789"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CTFY_TOKEN", _SECRET)
    monkeypatch.setenv("LLM_API_KEY", _PLAIN_SECRET)
    monkeypatch.setattr(run, "_git_info", lambda: ("synthetic", False))


def _install_dispatch(monkeypatch, *, artifact="safe"):

    class LocalLLM:
        def __init__(self):
            self.turn = 0

        async def complete(self, messages, *, model, effort, tools):
            self.turn += 1
            if self.turn == 1:
                command = "x" * 110 + _PLAIN_SECRET + " " + _SECRET
                return LLMResponse(
                    tool_calls=[ToolCall("execute", {"cmd": command, _SECRET: _SECRET})],
                    stop_reason="model-stop " + _SECRET,
                )
            return LLMResponse(text="x" * 110 + _PLAIN_SECRET + _SECRET, stop_reason=_SECRET)

    class LocalExecution:
        async def run(self, cmd, *, timeout_s, workdir):
            return ExecResult(stdout=_PLAIN_SECRET + "y" * 230, stderr=_SECRET)

    async def dispatch(mode, result_path, t0, args):
        settings, platform, _, _, clock = run._build_smoke(t0)
        settings.llm_model = "synthetic-model " + _SECRET
        llm = run.CountingLLM(LocalLLM())
        execution = run.CountingExecution(LocalExecution())
        response = await llm.complete([], model=settings.llm_model)
        await execution.run(response.tool_calls[0].arguments["cmd"])
        await llm.complete([], model=settings.llm_model)
        result = RunResult(
            answers={_SECRET: _SECRET, "smk-web-q1": "synthetic-answer"},
            solved=["smk-web-q1"], stop_reason="stop " + _SECRET, writeup=_SECRET,
        )
        if artifact == "collision":
            result.answers = {_SECRET: "first", "***": "second"}
            result.solved = [_SECRET, "***"]
            result.fully_solved = True
        if artifact == "safe":
            CtfyResultSink(result_path).write(result)
        elif artifact == "leak":
            with open(result_path, "w", encoding="utf-8") as stream:
                json.dump({"secret": _SECRET}, stream)
        elif artifact == "invalid":
            with open(result_path, "w", encoding="utf-8") as stream:
                stream.write("{invalid-json")
        elif artifact == "collision":
            # Simulate an execution-writable result falsely claiming success.
            # The runner must inspect its controller result, never trust this
            # safe-looking file to decide whether in-memory data was lossy.
            CtfyResultSink(result_path).write(RunResult(fully_solved=True))
        print("adapter stdout " + _SECRET)
        print("adapter stderr " + _SECRET, file=sys.stderr)
        return result, {
            "settings": settings, "platform": platform, "llm": llm,
            "execution": execution, "clock": clock, "challenges": run._smoke_challenges(),
            "notes": ["notes " + _SECRET],
        }

    monkeypatch.setattr(run, "_dispatch", dispatch)


def _argv(monkeypatch, out, baseline, *, save=True):
    argv = ["regression", "--mode", "smoke", "--out", str(out),
            "--baseline", str(baseline), "--trace"]
    if save:
        argv.append("--save-baseline")
    monkeypatch.setattr(sys, "argv", argv)


def _record(out):
    paths = [p for p in out.glob("*.json") if not p.name.startswith("result-")]
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_cli_redacts_every_file_and_terminal_before_preview_truncation(
    tmp_path, monkeypatch, capsys,
):
    # The synthetic secret also appears in output path messages. The actual
    # caller-chosen filesystem path must not be silently renamed.
    out = tmp_path / _PLAIN_SECRET
    baseline = tmp_path / (_PLAIN_SECRET + "-baseline.json")
    _install_dispatch(monkeypatch)
    _argv(monkeypatch, out, baseline)
    assert run.main() == 0
    captured = capsys.readouterr()
    assert not contains_secret(captured.out)
    assert not contains_secret(captured.err)
    files = list(out.iterdir()) + [baseline]
    assert len(files) == 5  # result, record, Markdown, trace, baseline
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not contains_secret(text)
        if path.suffix == ".json":
            assert not contains_secret(json.loads(text))
        # Full-token replacement must precede 120/240-byte preview slicing:
        # otherwise these recognizable fragments survive without a full token.
        assert _PLAIN_SECRET[:10] not in text
        assert _PLAIN_SECRET[-10:] not in text
    assert _record(out)["artifact"]["no_secret_leak"] is True
    assert json.loads(baseline.read_text(encoding="utf-8"))["artifact"]["no_secret_leak"] is True


@pytest.mark.parametrize("artifact", ["missing", "invalid", "leak"])
def test_cli_missing_invalid_or_unredacted_result_cannot_report_green(
    tmp_path, monkeypatch, capsys, artifact,
):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _install_dispatch(monkeypatch, artifact=artifact)
    _argv(monkeypatch, out, baseline)
    assert run.main() == 1
    captured = capsys.readouterr()
    assert "安全审计失败" in captured.err
    assert not contains_secret(captured.out + captured.err)
    assert _record(out)["artifact"]["no_secret_leak"] is False
    assert not baseline.exists()


def test_cli_exception_scrubs_diagnostic_record_without_publishing_baseline(
    tmp_path, monkeypatch, capsys,
):
    async def fail(*args):
        print("before failure " + _SECRET)
        raise RuntimeError("synthetic failure " + _SECRET)

    monkeypatch.setattr(run, "_dispatch", fail)
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _argv(monkeypatch, out, baseline)
    assert run.main() == 1
    captured = capsys.readouterr()
    assert not contains_secret(captured.out + captured.err)
    assert "RuntimeError" in captured.err
    assert not baseline.exists()
    for path in out.iterdir():
        assert not contains_secret(path.read_text(encoding="utf-8"))
    assert _record(out)["stability"]["exit_code"] == 1


def test_cli_comparison_scrubs_baseline_values_and_path_messages(tmp_path, monkeypatch, capsys):
    out, baseline = tmp_path / "out", tmp_path / (_PLAIN_SECRET + ".json")
    baseline.write_text(
        json.dumps({"meta": {"mode": _SECRET}, "solving": {"solve_rate": _SECRET}}),
        encoding="utf-8",
    )
    _install_dispatch(monkeypatch)
    _argv(monkeypatch, out, baseline, save=False)
    assert run.main() == 1
    captured = capsys.readouterr()
    assert "回归比对" in captured.out
    assert not contains_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    "kind", ["missing", "invalid", "leak", "escaped-key", "bearer", "scan-error"],
)
def test_manifest_audit_fails_closed(tmp_path, monkeypatch, kind):
    path = tmp_path / "artifact.json"
    if kind == "invalid":
        path.write_text("{", encoding="utf-8")
    elif kind == "leak":
        path.write_text(json.dumps({"x": _SECRET}), encoding="utf-8")
    elif kind == "escaped-key":
        path.write_text(json.dumps({_SECRET: "x"}, ensure_ascii=True), encoding="utf-8")
    elif kind == "bearer":
        path.write_text(json.dumps({"x": "Bearer unknown.jwt"}), encoding="utf-8")
    elif kind == "scan-error":
        path.write_text("{}", encoding="utf-8")

        def broken(*args, **kwargs):
            raise RuntimeError("synthetic scan failure")

        monkeypatch.setattr(run, "contains_secret", broken)
    assert run._audit_outputs([str(path)], stdout="", stderr="") is False


def test_manifest_checks_terminal_and_every_expected_file(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text("{}", encoding="utf-8")
    assert run._audit_outputs([str(clean)], stdout="clean", stderr="")
    assert not run._audit_outputs([str(clean)], stdout=_SECRET, stderr="")
    assert not run._audit_outputs([str(clean)], stdout="", stderr=_SECRET)
    assert not run._audit_outputs(
        [str(clean), str(tmp_path / "missing.trace.txt")], stdout="", stderr="",
    )
    assert not run._audit_outputs([], stdout="", stderr="")


def test_post_write_trace_failure_cannot_publish_baseline(tmp_path, monkeypatch, capsys):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _install_dispatch(monkeypatch)
    _argv(monkeypatch, out, baseline)
    original_write = run._write_outputs
    injected = False

    def corrupt_once(outputs):
        nonlocal injected
        original_write(outputs)
        if not injected:
            for path in outputs:
                if path.endswith(".trace.txt"):
                    with open(path, "w", encoding="utf-8") as stream:
                        stream.write(_SECRET)
                    injected = True

    monkeypatch.setattr(run, "_write_outputs", corrupt_once)
    assert run.main() == 1
    captured = capsys.readouterr()
    assert not contains_secret(captured.out + captured.err)
    assert _record(out)["artifact"]["no_secret_leak"] is False
    assert not baseline.exists()


def test_json_artifact_validation_does_not_depend_on_filename_suffix(tmp_path):
    path = tmp_path / "baseline.snapshot"
    path.write_text("{invalid", encoding="utf-8")
    assert not run._audit_outputs(
        [str(path)], stdout="", stderr="", json_paths=(str(path),),
    )


def test_cli_argument_errors_do_not_echo_known_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["regression", "--mode", _SECRET])
    assert run.main() == 2
    captured = capsys.readouterr()
    assert not contains_secret(captured.out + captured.err)


def test_controller_key_collision_cannot_publish_success(tmp_path, monkeypatch, capsys):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _install_dispatch(monkeypatch, artifact="collision")
    _argv(monkeypatch, out, baseline)
    assert run.main() == 1
    captured = capsys.readouterr()
    assert not contains_secret(captured.out + captured.err)
    report = _record(out)
    assert report["solving"]["fully_solved"] is False
    assert report["solving"]["questions_solved"] == 0
    assert report["stability"]["exit_code"] == 1
    assert report["artifact"]["no_secret_leak"] is False
    result_path = next(out.glob("result-*.json"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["fully_solved"] is False
    assert result["answers"] == {}
    assert result["stop_reason"] == "redaction_error: ambiguous_keys"


def test_mixed_escaped_logs_do_not_bypass_complete_runner_audit(tmp_path, monkeypatch, capsys):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _install_dispatch(monkeypatch)
    original = run._dispatch
    encoded = rf"\u{ord(_PLAIN_SECRET[0]):04x}" + _PLAIN_SECRET[1:]
    mixed = 'raw: {"token":"' + encoded + '"}'

    async def dispatch(*args):
        result, probes = await original(*args)
        result.stop_reason = mixed
        result.writeup = mixed
        probes["notes"].append(mixed)
        probes["llm"].actions.append(mixed)
        probes["execution"].transcript.append({"cmd": mixed, "tail": mixed, "exit": 0})
        CtfyResultSink(args[1]).write(result)
        print(mixed)
        print(mixed, file=sys.stderr)
        return result, probes

    monkeypatch.setattr(run, "_dispatch", dispatch)
    _argv(monkeypatch, out, baseline)
    assert run.main() == 0
    captured = capsys.readouterr()
    assert encoded not in captured.out + captured.err
    assert not contains_secret(captured.out + captured.err)
    for path in [*out.iterdir(), baseline]:
        text = path.read_text(encoding="utf-8")
        assert encoded not in text
        assert not contains_secret(text)
    assert _record(out)["artifact"]["no_secret_leak"] is True


def test_baseline_promotion_follows_audit_and_run_publication(tmp_path, monkeypatch):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    previous = b'{"previous": "valid baseline bytes"}'
    baseline.write_bytes(previous)
    _install_dispatch(monkeypatch)
    _argv(monkeypatch, out, baseline)
    original_audit, original_publish = run._audit_outputs, run._publish_candidates
    audited = False
    published = False

    def audit(paths, **kwargs):
        nonlocal audited
        assert baseline.read_bytes() == previous
        ok = original_audit(paths, **kwargs)
        if len(paths) > 1:
            audited = ok
        return ok

    def publish(candidates, *, record_path):
        nonlocal published
        assert audited
        assert baseline.read_bytes() == previous
        assert str(baseline) not in candidates
        original_publish(candidates, record_path=record_path)
        published = True

    original_replace = run.os.replace

    def replace(source, destination):
        if destination == str(baseline):
            assert published
            assert _record(out)["artifact"]["no_secret_leak"] is True
        original_replace(source, destination)

    monkeypatch.setattr(run, "_audit_outputs", audit)
    monkeypatch.setattr(run, "_publish_candidates", publish)
    monkeypatch.setattr(run.os, "replace", replace)
    assert run.main() == 0
    assert baseline.read_bytes() != previous
    assert not list(out.glob(".regression-*"))
    assert not list(tmp_path.glob(".regression-*"))


def test_false_output_write_failure_never_leaves_a_green_report(tmp_path, monkeypatch, capsys):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _install_dispatch(monkeypatch)
    _argv(monkeypatch, out, baseline)
    original = run._write_outputs

    def write_first_then_fail(outputs):
        first = next(iter(outputs))
        original({first: outputs[first]})
        raise OSError("synthetic write failure " + _SECRET)

    monkeypatch.setattr(run, "_write_outputs", write_first_then_fail)
    assert run.main() == 1
    assert _record(out)["artifact"]["no_secret_leak"] is False
    assert _record(out)["stability"]["exit_code"] == 1
    assert not baseline.exists()
    captured = capsys.readouterr()
    assert not contains_secret(captured.out + captured.err)


def test_staged_candidate_corruption_cannot_publish_true(tmp_path, monkeypatch, capsys):
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    _install_dispatch(monkeypatch)
    _argv(monkeypatch, out, baseline)
    original = run._stage_candidates

    def stage_and_corrupt(outputs):
        candidates = original(outputs)
        with open(candidates[str(baseline)], "w", encoding="utf-8") as stream:
            json.dump({"token": _SECRET}, stream)
        return candidates

    monkeypatch.setattr(run, "_stage_candidates", stage_and_corrupt)
    assert run.main() == 1
    assert _record(out)["artifact"]["no_secret_leak"] is False
    assert not baseline.exists()
    assert not list(out.glob(".regression-*"))
    captured = capsys.readouterr()
    assert not contains_secret(captured.out + captured.err)
