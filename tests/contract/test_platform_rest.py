"""Contract: CtfyRestAdapter maps platform responses onto the domain, resolves
every VerdictStatus, treats 409/423 as PAUSED (a wait), and honors Retry-After
through the ClockPort. httpx.MockTransport — no real network.
"""
from __future__ import annotations

import httpx

from agent.adapters.platform.ctfy_rest import CtfyRestAdapter
from agent.domain.state import InstanceStatus, VerdictStatus
from agent.testing.fakes import FakeClock


def _adapter(handler, clock: FakeClock | None = None) -> CtfyRestAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CtfyRestAdapter("https://ctfy.test", "tok", clock=clock or FakeClock(), client=client)


async def test_whoami_maps_account():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/me"
        assert req.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"id": "u1", "name": "team-redbud"})

    acc = await _adapter(handler).whoami()
    assert acc.id == "u1"
    assert acc.name == "team-redbud"


async def test_get_challenge_maps_questions():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "name": "Login",
                "category": "web",
                "difficulty": "easy",
                "questions": [{"id": "q1", "prompt": "flag?", "format": "static"}],
            },
        )

    ch = await _adapter(handler).get_challenge("c1")
    assert ch.id == "c1"
    assert ch.category == "web"
    assert [q.id for q in ch.questions] == ["q1"]


async def test_start_instance_ready():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/instances"
        return httpx.Response(
            200, json={"id": "i1", "host": "chal.local", "port": 443, "status": "ready"}
        )

    inst = await _adapter(handler).start_instance("c1")
    assert inst.status is InstanceStatus.READY
    assert inst.host == "chal.local"


async def test_submit_verdict_statuses():
    cases = {
        "correct": VerdictStatus.CORRECT,
        "wrong": VerdictStatus.WRONG,
        "grader_unavailable": VerdictStatus.UNGRADED,
        "cap_reached": VerdictStatus.CAPPED,
        "competition_paused": VerdictStatus.PAUSED,
    }
    for signal, expected in cases.items():
        def handler(req: httpx.Request, _s=signal) -> httpx.Response:
            return httpx.Response(200, json={"status": _s})

        v = await _adapter(handler).submit("c1", "q1", "ctfy{x}")
        assert v.status is expected, f"signal {signal!r} -> {v.status}"


async def test_submit_http_409_is_paused():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={})

    v = await _adapter(handler).submit("c1", "q1", "ctfy{x}")
    assert v.status is VerdictStatus.PAUSED


async def test_submit_qa_routes_to_qa_endpoint():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"status": "correct"})

    v = await _adapter(handler).submit_qa("c1", "q1", "A")
    assert seen["path"] == "/api/v1/qa/submissions"
    assert v.status is VerdictStatus.CORRECT


async def test_retry_after_honored_through_clock():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "4"}, json={})
        return httpx.Response(200, json={"status": "correct"})

    clock = FakeClock()
    v = await _adapter(handler, clock).submit("c1", "q1", "ctfy{x}")
    assert v.status is VerdictStatus.CORRECT
    assert clock.slept == [4.0]


async def test_scoreboard_parses_entries():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entries": [{"rank": 1, "team": "A", "score": 100, "solved": 3}]},
        )

    sb = await _adapter(handler).scoreboard("comp1")
    assert sb.entries[0].team == "A"
    assert sb.entries[0].rank == 1
