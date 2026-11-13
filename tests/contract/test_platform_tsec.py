"""Contract: TSecSdkAdapter honors PlatformPort against the real tsec-benchmark
SDK, driven through httpx.MockTransport (the SDK's actual routes —
``/openapi/v1/challenges{,/start,/submit,/close}`` and the VPN healthcheck).

Proves the TSecBench-specific mappings: flags become questions with progress,
range addresses become raw-TCP Instances, grader outcomes become Verdicts,
``DuplicateSubmit`` is idempotent success, and the SDK's retryable failures go
through the ClockPort. No real network, no VPN — fakes / MockTransport only.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent.adapters._resilience import TransientError
from agent.adapters.platform.tsec_sdk import TSecSdkAdapter
from agent.domain.state import InstanceStatus, VerdictStatus
from agent.testing.fakes import FakeClock

CHALLENGES = "/openapi/v1/challenges"
VPN_HOST = "10.0.100.58"


def _adapter(handler, clock: FakeClock | None = None) -> TSecSdkAdapter:
    return TSecSdkAdapter(
        "https://benchmark.test",
        "tok",
        clock=clock or FakeClock(),
        transport=httpx.MockTransport(handler),
    )


def _ch(
    code: str,
    *,
    flags: int,
    solved: int,
    completed: bool = False,
    status: str = "stopped",
    addr: list[str] | None = None,
    desc: str = "range desc",
    diff: str = "medium",
    score: int = 100,
) -> dict:
    return {
        "unique_code": code,
        "difficulty": diff,
        "level": 1,
        "total_score": score,
        "flag_count": flags,
        "correct_flag_count": solved,
        "is_completed": completed,
        "description": desc,
        "container_status": status,
        "container_addr": addr or [],
    }


def _submit(correct: bool, *, awarded: int = 10, cfc: int = 1, tfc: int = 1) -> dict:
    return {
        "correct": correct,
        "awarded": awarded,
        "cumulative_score": awarded,
        "correct_flag_count": cfc,
        "total_flag_count": tfc,
        "matched_flag_index": 0 if correct else None,
    }


# --- whoami / VPN pre-flight -------------------------------------------------
async def test_whoami_probes_vpn_and_maps_client_ip():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == VPN_HOST
        return httpx.Response(
            200, json={"status": "ok", "client_ip": "10.8.0.3", "time": "2026-11-13 10:00:00"}
        )

    acc = await _adapter(handler).whoami()
    assert acc.id == "10.8.0.3"
    assert acc.name == "tsecbench"


async def test_whoami_raises_when_vpn_not_ok():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "down"})  # status != ok

    with pytest.raises(RuntimeError, match="VPN"):
        await _adapter(handler).whoami()


# --- list_challenges: flags -> questions, progress --------------------------
async def test_list_maps_flags_to_questions_and_progress():
    data = [
        _ch("CYB-01", flags=2, solved=1),
        _ch("CYB-02", flags=1, solved=1, completed=True, score=50),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET" and req.url.path == CHALLENGES
        assert req.headers["BENCHMARK_TOKEN"] == "tok"
        return httpx.Response(200, json=data)

    chals = await _adapter(handler).list_challenges()
    assert [c.id for c in chals] == ["CYB-01", "CYB-02"]
    c1 = chals[0]
    assert c1.points == 100 and c1.difficulty == "medium"
    assert [q.id for q in c1.questions] == ["CYB-01#0", "CYB-01#1"]
    assert [q.answered for q in c1.questions] == [True, False]  # first solved
    assert all(q.fmt == "dynamic" for q in c1.questions)
    assert c1.unanswered()[0].id == "CYB-01#1"
    c2 = chals[1]
    assert len(c2.questions) == 1 and c2.questions[0].answered is True  # completed


async def test_get_challenge_filters_list():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_ch("A", flags=1, solved=0), _ch("B", flags=1, solved=0)])

    ch = await _adapter(handler).get_challenge("B")
    assert ch.id == "B"


async def test_get_challenge_missing_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_ch("A", flags=1, solved=0)])

    with pytest.raises(RuntimeError, match="not in the task"):
        await _adapter(handler).get_challenge("ZZZ")


# --- start_instance ---------------------------------------------------------
async def test_start_instance_starts_and_parses_addr():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == CHALLENGES and req.method == "GET":
            return httpx.Response(200, json=[_ch("CYB-01", flags=1, solved=0, status="stopped")])
        if req.url.path == f"{CHALLENGES}/start":
            assert req.url.params["unique_code"] == "CYB-01"
            return httpx.Response(
                200, json={"unique_code": "CYB-01", "container_addr": ["10.1.2.3:9000"]}
            )
        raise AssertionError(req.url.path)

    inst = await _adapter(handler).start_instance("CYB-01")
    assert inst.status is InstanceStatus.READY
    assert inst.host == "10.1.2.3" and inst.port == 9000
    assert inst.tls_sni is False
    assert f"{CHALLENGES}/start" in seen


async def test_start_instance_returns_already_running_without_restart():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == CHALLENGES and req.method == "GET":
            return httpx.Response(
                200,
                json=[_ch("CYB-01", flags=1, solved=0, status="available", addr=["10.9.9.9:80"])],
            )
        raise AssertionError("must not call start when the range is already up")

    inst = await _adapter(handler).start_instance("CYB-01")
    assert inst.ready and inst.host == "10.9.9.9" and inst.port == 80


# --- submit -----------------------------------------------------------------
async def test_submit_correct_sends_raw_flag():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and req.url.path == f"{CHALLENGES}/submit"
        assert json.loads(req.content) == {"unique_code": "CYB-01", "flag": "flag{x}"}
        return httpx.Response(200, json=_submit(True))

    v = await _adapter(handler).submit("CYB-01", "CYB-01#0", "flag{x}")
    assert v.status is VerdictStatus.CORRECT and v.correct


async def test_submit_wrong():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_submit(False, awarded=0, cfc=0))

    v = await _adapter(handler).submit("CYB-01", "CYB-01#0", "nope")
    assert v.status is VerdictStatus.WRONG


async def test_submit_duplicate_is_idempotent_correct():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "duplicate", "message": "already accepted"})

    v = await _adapter(handler).submit("CYB-01", "CYB-01#0", "flag{x}")
    assert v.status is VerdictStatus.CORRECT


async def test_submit_resource_unavailable_retries_then_recovers():
    hits = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/submit"):
            hits["n"] += 1
            if hits["n"] < 3:
                return httpx.Response(503, json={"code": "resource_unavailable", "message": "busy"})
            return httpx.Response(200, json=_submit(True))
        raise AssertionError(req.url.path)

    v = await _adapter(handler).submit("CYB-01", "CYB-01#0", "flag{x}")
    assert v.correct and hits["n"] == 3  # retried through the ClockPort, then recovered


async def test_submit_persistent_503_exhausts_to_transient():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "resource_unavailable", "message": "busy"})

    with pytest.raises(TransientError):
        await _adapter(handler).submit("CYB-01", "CYB-01#0", "flag{x}")


# --- qa / scoreboard / lifecycle --------------------------------------------
async def test_submit_qa_raises():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - never hit
        return httpx.Response(200, json={})

    with pytest.raises(RuntimeError, match="QA"):
        await _adapter(handler).submit_qa("c", "q", "a")


async def test_scoreboard_is_empty():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - never hit
        return httpx.Response(200, json=[])

    sb = await _adapter(handler).scoreboard("")
    assert sb.entries == [] and sb.retry_after_s is None


async def test_auto_close_previous_and_aclose_release_ranges():
    closed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == CHALLENGES and req.method == "GET":
            return httpx.Response(
                200, json=[_ch("A", flags=1, solved=0), _ch("B", flags=1, solved=0)]
            )
        if p == f"{CHALLENGES}/start":
            code = req.url.params["unique_code"]
            return httpx.Response(
                200, json={"unique_code": code, "container_addr": ["10.0.0.1:1000"]}
            )
        if p == f"{CHALLENGES}/close":
            code = req.url.params["unique_code"]
            closed.append(code)
            return httpx.Response(200, json={"unique_code": code, "closed": True})
        raise AssertionError(p)

    ad = _adapter(handler)
    await ad.start_instance("A")
    await ad.start_instance("B")  # starting B frees A
    assert closed == ["A"]
    await ad.aclose()  # releases the last open range (B)
    assert closed == ["A", "B"]
