"""Synthetic regression publication, comparability and cancellable solve coverage."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from agent.adapters.result.ctfy_result import CtfyResultSink
from agent.config import Settings
from agent.domain.state import Challenge, Question, RunResult
from tools.regression import run


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "_git_info", lambda: ("synthetic", False))
    state = SimpleNamespace(solved=2, tokens=20, reason="completed")

    async def dispatch(mode, result_path, t0, args):
        settings, platform, llm, execution, clock = run._build_smoke(t0)
        challenges = [Challenge(id="cohort", questions=[Question(id="q1"), Question(id="q2")])]
        solved = ["q1", "q2"][:state.solved] if mode != "integration" else []
        result = RunResult(
            answers={qid: "synthetic answer" for qid in solved}, solved=solved,
            input_tokens=state.tokens,
            stop_reason="integration_ok" if mode == "integration" else state.reason,
        )
        CtfyResultSink(result_path, stdout_fallback=False).write(result)
        return result, {
            "settings": settings, "platform": platform, "llm": llm,
            "execution": execution, "clock": clock, "challenges": challenges,
        }

    monkeypatch.setattr(run, "_dispatch", dispatch)
    baseline = tmp_path / "baseline.json"

    def invoke(name, *, save=False, mode="smoke"):
        out = tmp_path / name
        argv = ["regression", "--mode", mode, "--out", str(out), "--baseline", str(baseline)]
        if save:
            argv.append("--save-baseline")
        monkeypatch.setattr(sys, "argv", argv)
        status = run.main()
        records = [path for path in out.glob("*.json") if not path.name.startswith("result-")]
        assert len(records) == 1
        return status, json.loads(records[0].read_text(encoding="utf-8"))

    return state, baseline, invoke


def test_fail_comparison_sets_process_status_and_persisted_status(pipeline):
    state, baseline, invoke = pipeline
    assert invoke("baseline", save=True)[0] == 0
    previous = baseline.read_bytes()
    state.solved = 1
    status, record = invoke("regressed")
    assert status == 1
    assert record["stability"]["exit_code"] == 1
    assert record["meta"]["comparison_status"] == "FAIL"
    assert record["artifact"]["no_secret_leak"] is True
    assert baseline.read_bytes() == previous


def test_warn_comparison_is_nonblocking(pipeline):
    state, baseline, invoke = pipeline
    assert invoke("baseline", save=True)[0] == 0
    base = json.loads(baseline.read_text(encoding="utf-8"))
    base["timing"]["total_wall_s"] = 1000000
    baseline.write_text(json.dumps(base), encoding="utf-8")
    state.tokens = 100
    status, record = invoke("warn")
    assert status == 0
    assert record["meta"]["comparison_status"] == "WARN"
    assert record["stability"]["exit_code"] == 0


@pytest.mark.parametrize("problem", [
    "zero", "failed", "legacy", "cohort", "model", "turns", "tokens", "wall",
    "malformed", "array", "malformed-mode", "malformed-score",
])
def test_unqualified_baselines_produce_auditable_failed_runs(pipeline, problem):
    _, baseline, invoke = pipeline
    assert invoke("baseline", save=True)[0] == 0
    base = json.loads(baseline.read_text(encoding="utf-8"))
    identity = base["meta"]["comparison_identity"]
    if problem == "zero":
        base["solving"].update(questions_solved=0, solve_rate=0)
    elif problem == "failed":
        base["stability"]["exit_code"] = 1
    elif problem == "malformed-mode":
        base["meta"]["mode"] = []
    elif problem == "malformed-score":
        base["solving"]["questions_solved"] = True
    elif problem == "legacy":
        del base["meta"]["comparison_identity"]
    elif problem == "cohort":
        identity["challenge_ids"] = ["other-cohort"]
    elif problem == "model":
        identity["model"] = "other-model"
    elif problem in {"turns", "tokens", "wall"}:
        identity[{"turns": "max_turns", "tokens": "max_tokens", "wall": "max_wall_s"}[problem]] = 17
    payload = (
        "{bad-json" if problem == "malformed"
        else json.dumps([] if problem == "array" else base)
    )
    baseline.write_text(payload, encoding="utf-8")
    previous = baseline.read_bytes()
    status, record = invoke("candidate")
    assert status == 1
    assert record["meta"]["comparison_status"] == "FAIL"
    assert record["stability"]["exit_code"] == 1
    assert record["artifact"]["no_secret_leak"] is True
    assert record["meta"]["comparison_details"]
    assert baseline.read_bytes() == previous


def test_revision_and_unlocked_question_ids_are_not_comparison_identity(pipeline):
    _, baseline, invoke = pipeline
    assert invoke("baseline", save=True)[0] == 0
    base = json.loads(baseline.read_text(encoding="utf-8"))
    base["meta"]["git_commit"] = "older-revision"
    base["per_challenge"][0]["questions_total"] = 123
    base["timing"]["total_wall_s"] = 1000000
    baseline.write_text(json.dumps(base), encoding="utf-8")
    assert invoke("candidate")[0] == 0


def test_integration_baseline_qualifies_without_solves(pipeline):
    _, baseline, invoke = pipeline
    status, record = invoke("integration-baseline", save=True, mode="integration")
    assert status == 0
    assert record["solving"]["questions_solved"] == 0
    assert record["solving"]["solve_rate"] == 0
    assert baseline.exists()
    assert invoke("integration-candidate", mode="integration")[0] == 0


@pytest.mark.parametrize(
    "reason,solved", [("completed", 0), ("timeout", 1), ("error: RuntimeError", 1)],
)
def test_bad_baseline_candidates_preserve_previous_bytes(pipeline, reason, solved):
    state, baseline, invoke = pipeline
    assert invoke("baseline", save=True)[0] == 0
    previous = baseline.read_bytes()
    state.reason, state.solved = reason, solved
    status, record = invoke("candidate", save=True)
    assert status == 1
    assert record["stability"]["exit_code"] == 1
    assert baseline.read_bytes() == previous


@pytest.mark.parametrize("failure", ["audit", "publication", "baseline-promotion"])
def test_publication_failure_never_replaces_previous_baseline(pipeline, monkeypatch, failure):
    _, baseline, invoke = pipeline
    assert invoke("baseline", save=True)[0] == 0
    previous = baseline.read_bytes()
    if failure == "audit":
        original = run._audit_outputs

        def audit(paths, **kwargs):
            return original(paths, **kwargs) if len(paths) == 1 else False

        monkeypatch.setattr(run, "_audit_outputs", audit)
    elif failure == "publication":
        def fail(*args, **kwargs):
            raise OSError("synthetic publication failure")

        monkeypatch.setattr(run, "_publish_candidates", fail)
    else:
        original_replace = run.os.replace

        def replace(source, destination):
            if destination == str(baseline):
                raise OSError("synthetic baseline promotion failure")
            original_replace(source, destination)

        monkeypatch.setattr(run.os, "replace", replace)
    status, _ = invoke("candidate", save=True)
    assert status == 1
    assert baseline.read_bytes() == previous


@pytest.fixture
def solve_runtime(monkeypatch, tmp_path):
    import agent.adapters.execution.subprocess_exec as execution_module
    import agent.adapters.llm.gateway as gateway_module
    import agent.adapters.platform.ctfy_rest as platform_module
    import agent.domain.coordinator as coordinator_module

    settings = Settings(
        work_dir=str(tmp_path), max_turns=9, max_tokens=101,
        llm_model="synthetic-model", max_wall_s=100,
    )
    state = SimpleNamespace(
        task=None, settings=None, behavior="success", closed=False,
        staged=False, staging_cancelled=False, config=settings,
    )
    challenge = Challenge(id="target", questions=[Question(id="question")])

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    class Platform:
        def __init__(self, *args, **kwargs):
            pass

        async def get_challenge(self, challenge_id):
            return challenge

    class Coordinator:
        def __init__(self, platform, llm, execution, sink, *, clock, task, settings):
            state.task, state.settings = task, settings
            self.current_result = RunResult(
                answers={"question": "answer"}, solved=["question"], turns=3,
                input_tokens=7, output_tokens=11,
            )

        async def run(self):
            if state.behavior == "error":
                raise RuntimeError("synthetic failure after progress")
            if state.behavior == "timeout":
                await asyncio.Event().wait()
            return self.current_result

    async def stage(*args):
        state.staged = True
        if state.behavior == "staging-timeout":
            try:
                await asyncio.Event().wait()
            finally:
                state.staging_cancelled = True
        return []

    async def detect(*args):
        return True

    async def close(*ports, timeout_s):
        # The result must already be durable when cleanup starts.
        state.saved_before_cleanup = json.loads(
            (tmp_path / "result.json").read_text(encoding="utf-8"),
        )
        state.close_budget = timeout_s
        state.closed = True

    monkeypatch.setattr(Settings, "from_env", staticmethod(lambda: state.config))
    monkeypatch.setattr(run, "_load_secret_env", lambda path: None)
    monkeypatch.setattr(run, "_make_counting_transport", lambda: None)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(platform_module, "CtfyRestAdapter", Platform)
    monkeypatch.setattr(gateway_module, "GatewayAdapter", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(execution_module, "SubprocessAdapter", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(coordinator_module, "Coordinator", Coordinator)
    monkeypatch.setattr(run, "_stage_attachments", stage)
    monkeypatch.setattr(run, "_detect_qa_only", detect)
    monkeypatch.setattr(run, "close_ports", close)
    # Avoid leaving incidental staging directories in these synthetic tests.
    monkeypatch.setattr(run.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    return state, tmp_path


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("cli_cap,task_cap,expected", [(5, 7, 5), (None, 7, 7), (None, None, 9)])
def test_effective_turn_precedence_and_targeted_denominators(
    solve_runtime, targeted, cli_cap, task_cap, expected,
):
    state, root = solve_runtime
    if task_cap is not None:
        (root / "task.json").write_text(json.dumps({"max_turns": task_cap}), encoding="utf-8")
    result, probes = asyncio.run(run._run_solve(
        str(root / "result.json"), 0, None, "unused", 20,
        challenge_id="target" if targeted else None, max_turns=cli_cap,
    ))
    assert state.task.max_turns == expected
    assert probes["settings"].max_turns == expected
    assert probes["settings"].max_wall_s == 18
    assert 0 < state.settings.max_wall_s <= 18
    assert state.close_budget == 2
    record = run._assemble_record(
        run_id="synthetic", mode="solve", started_at="start", ended_at="end",
        total_wall_s=1, exit_code=0, result=result,
        result_path=str(root / "result.json"), probes=probes, competition_id="synthetic",
    )
    assert record["budget"]["max_turns"] == expected
    assert record["budget"]["max_tokens"] == 101
    if targeted:
        assert record["solving"]["challenges_total"] == 1
        assert record["solving"]["questions_total"] == 1
        assert record["solving"]["solve_rate"] == 1
        assert record["meta"]["comparison_identity"]["challenge_ids"] == ["target"]


@pytest.mark.parametrize("behavior", ["timeout", "error"])
def test_timeout_and_error_keep_partial_progress_before_cleanup(solve_runtime, behavior):
    state, root = solve_runtime
    state.behavior = behavior
    result, _ = asyncio.run(run._run_solve(
        str(root / "result.json"), 0, None, "unused", 0.02, challenge_id="target",
    ))
    assert result.solved == ["question"]
    assert result.answers == {"question": "answer"}
    assert (result.turns, result.input_tokens, result.output_tokens) == (3, 7, 11)
    assert result.stop_reason == ("timeout" if behavior == "timeout" else "error: RuntimeError")
    assert state.saved_before_cleanup["solved"] == ["question"]
    assert state.closed


def test_wall_timeout_cancels_staging_and_still_persists_result(solve_runtime):
    state, root = solve_runtime
    state.behavior = "staging-timeout"
    result, probes = asyncio.run(run._run_solve(
        str(root / "result.json"), 0, None, "unused", 0.02, challenge_id="target",
    ))
    assert result.stop_reason == "timeout"
    assert state.staging_cancelled
    assert state.task is None
    assert state.saved_before_cleanup["stop_reason"] == "timeout"
    assert state.closed
    assert probes["selected_challenge_ids"] == ["target"]


def test_omitted_timeout_uses_settings_wall_limit(solve_runtime):
    state, root = solve_runtime
    _, probes = asyncio.run(run._run_solve(str(root / "result.json"), 0, None, "unused", None))
    assert probes["total_wall_limit_s"] == 100
    assert probes["settings"].max_wall_s == 95
    assert state.close_budget == 5


def test_task_token_limit_is_the_reported_effective_budget(solve_runtime):
    _, root = solve_runtime
    (root / "task.json").write_text(
        json.dumps({"limits": {"max_tokens": 202}}), encoding="utf-8",
    )
    _, probes = asyncio.run(run._run_solve(str(root / "result.json"), 0, None, "unused", 20))
    assert probes["settings"].max_tokens == 202


def test_omitted_timeout_keeps_regression_default(solve_runtime):
    state, root = solve_runtime
    state.config = replace(state.config, max_wall_s=None)
    _, probes = asyncio.run(run._run_solve(str(root / "result.json"), 0, None, "unused", None))
    assert probes["total_wall_limit_s"] == 1800
    assert probes["settings"].max_wall_s == 1795
    assert state.close_budget == 5


def test_zero_wall_budget_expires_before_staging(solve_runtime):
    state, root = solve_runtime
    result, _ = asyncio.run(run._run_solve(
        str(root / "result.json"), 0, None, "unused", 0, challenge_id="target",
    ))
    assert result.stop_reason == "timeout"
    assert not state.staged
    assert state.task is None
    assert state.saved_before_cleanup["stop_reason"] == "timeout"


def test_external_cancellation_keeps_progress_before_cleanup(solve_runtime):
    state, root = solve_runtime
    state.behavior = "timeout"

    async def scenario():
        task = asyncio.create_task(run._run_solve(
            str(root / "result.json"), 0, None, "unused", 100, challenge_id="target",
        ))
        while state.task is None:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert state.saved_before_cleanup["stop_reason"] == "cancelled"
    assert state.saved_before_cleanup["solved"] == ["question"]
    assert state.closed


@pytest.mark.parametrize(
    "helper", ["_stage_attachments", "_stage_instance_attachments", "_detect_qa_only"],
)
def test_staging_helpers_propagate_http_cancellation(monkeypatch, tmp_path, helper):
    client_type = httpx.AsyncClient

    async def scenario():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def request(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        transport = httpx.MockTransport(request)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs),
        )
        args = ["http://synthetic.invalid", "synthetic-token", "target"]
        if helper == "_stage_instance_attachments":
            args.extend(["competition", str(tmp_path)])
        elif helper == "_stage_attachments":
            args.append(str(tmp_path))
        task = asyncio.create_task(getattr(run, helper)(*args))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())
