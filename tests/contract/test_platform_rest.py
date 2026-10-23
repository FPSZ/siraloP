"""Contract: CtfyRestAdapter maps *real* ctfy platform responses onto the frozen
domain models, resolves every VerdictStatus, treats ``competition_paused`` as a
wait (not a failure), and honors Retry-After through the ClockPort.

Every sample below is the shape verified against the live platform (see the
workflow brief / CTFY-0001 §1.2, §4). httpx.MockTransport only — no real network.
"""
from __future__ import annotations

import json
import traceback

import httpx
import pytest

from agent.adapters._resilience import TransientError
from agent.adapters.platform.ctfy_rest import CtfyRestAdapter
from agent.domain.state import InstanceStatus, VerdictStatus
from agent.testing.fakes import FakeClock

# --- real samples -----------------------------------------------------------
COMPETITION_ID = "c0d8b4e3dd3b42c095d63d7dfa66431f"

ME = {
    "id": "7e1c0d8b-0000-4000-8000-000000000001",
    "display_name": "JingRui Li",
    "email": "jingrui@example.edu",
    "token_kind": "fine_grained",
    "competition_teams": [
        {
            "competition_id": COMPETITION_ID,
            "team_id": "b2a1",
            "team_name": "Polaris",
            "role": "captain",
            "competition_phase": "upcoming",
            "starts_at": "2026-11-15T02:00:00Z",
            "ends_at": "2026-11-16T02:00:00Z",
            "running_instances": 0,
        }
    ],
}

CHALLENGE = {
    "id": "SAMPLE-001",
    "name": "Hidden Admin",
    "category": "web",
    "difficulty": "easy",
    "description": "Find the hidden admin panel and recover the secret.",
    "solves_count": 7,
    "launch_count": 22,
    "tags": ["web", "recon"],
    "questions": [
        {
            "id": "slug",
            "prompt": "The admin's recovery token",
            "mode": "dynamic",
            "choices": None,
            "requires": [],
        }
    ],
    "flag_solves": {"slug": 7},
    "attachments": [{"name": "setup.txt", "size": 643, "per_team": True}],
    "is_qa_only": False,
    "internet": "allow",
    "my_solved_question_ids": [],
    "my_fully_solved": False,
}

SCOREBOARD = {
    "items": [
        {
            "rank": 1,
            "team_id": "b2a1",
            "team_name": "Polaris",
            "solved": 0,
            "flags_solved": 0,
            "score": 0,
            "attempts": 0,
            "hints_used": 0,
            "last_solve_at": None,
        }
    ]
}

# The exact competition_paused body the live platform returns (403) while the
# event is between sessions. retry_after_s lives under `detail`.
PAUSED_BODY = {
    "code": "competition_paused",
    "detail": {
        "message": "The competition is paused between sessions.",
        "competition_id": COMPETITION_ID,
        "next_session_starts_at": "2026-11-15T02:00:00+00:00",
        "retry_after_s": 2057694,
    },
    "timestamp": "2026-10-21T00:00:00Z",
}


def _adapter(
    handler, clock: FakeClock | None = None, *, competition_id: str = COMPETITION_ID
) -> CtfyRestAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CtfyRestAdapter(
        "https://ctfy.test",
        "tok",
        clock=clock or FakeClock(),
        competition_id=competition_id,
        client=client,
    )


# --- whoami -----------------------------------------------------------------
async def test_whoami_maps_account_from_display_name():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/me"
        assert req.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=ME)

    acc = await _adapter(handler).whoami()
    assert acc.id == ME["id"]
    assert acc.name == "JingRui Li"


# --- list_challenges --------------------------------------------------------
async def test_list_challenges_uses_competition_route():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == f"/api/v1/competitions/{COMPETITION_ID}/challenges"
        return httpx.Response(
            200, json={"items": [CHALLENGE], "total": 1, "limit": 50, "offset": 0}
        )

    chals = await _adapter(handler).list_challenges()
    assert [c.id for c in chals] == ["SAMPLE-001"]
    assert chals[0].name == "Hidden Admin"
    assert chals[0].solves == 7


async def test_list_challenges_paginates_until_total_covered():
    # A server whose page size (2) is smaller than the requested limit: the
    # adapter must walk offsets until `total` is covered and dedupe.
    dataset = [
        {"id": "A", "name": "A"},
        {"id": "B", "name": "B"},
        {"id": "C", "name": "C"},
    ]
    seen_offsets: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        offset = int(req.url.params.get("offset", "0"))
        seen_offsets.append(offset)
        page = dataset[offset : offset + 2]  # server caps at 2 per page
        return httpx.Response(
            200, json={"items": page, "total": len(dataset), "limit": 2, "offset": offset}
        )

    chals = await _adapter(handler).list_challenges()
    assert [c.id for c in chals] == ["A", "B", "C"]
    assert seen_offsets == [0, 2]  # two pages, offset advanced by items returned


async def test_list_challenges_without_competition_id_raises():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200, json={"items": []})

    with pytest.raises(RuntimeError, match="CTFY_EVAL_COMPETITION_ID"):
        await _adapter(handler, competition_id="").list_challenges()


# --- get_challenge (flat route) ---------------------------------------------
async def test_get_challenge_maps_real_shape():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/challenges/SAMPLE-001"
        return httpx.Response(200, json=CHALLENGE)

    ch = await _adapter(handler).get_challenge("SAMPLE-001")
    assert ch.id == "SAMPLE-001"
    assert ch.name == "Hidden Admin"
    assert ch.category == "web"
    assert ch.difficulty == "easy"
    assert ch.solves == 7
    assert len(ch.questions) == 1
    q = ch.questions[0]
    assert q.id == "slug"
    assert q.fmt == "dynamic"          # questions[].mode -> Question.fmt
    assert q.requires == []
    assert q.choices == []             # null choices -> empty list
    assert q.answered is False


async def test_get_challenge_my_solved_marks_answered():
    body = {
        "id": "C9",
        "name": "Chain",
        "questions": [
            {"id": "q1", "prompt": "one", "mode": "static"},
            {"id": "q2", "prompt": "two", "mode": "static"},
        ],
        "my_solved_question_ids": ["q1"],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    ch = await _adapter(handler).get_challenge("C9")
    answered = {q.id: q.answered for q in ch.questions}
    assert answered == {"q1": True, "q2": False}


# --- start_instance ---------------------------------------------------------
async def test_start_instance_post_body_carries_competition_id():
    # The competition_id in the POST body is what keeps the start off the paused
    # default track; losing it is the exact regression that 403s on a live run.
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/instances"
        assert json.loads(req.content) == {
            "challenge_id": "SAMPLE-001",
            "competition_id": COMPETITION_ID,
        }
        return httpx.Response(
            200, json={"id": "i1", "host": "chal.local", "port": 443, "status": "ready"}
        )

    inst = await _adapter(handler).start_instance("SAMPLE-001")
    assert inst.status is InstanceStatus.READY
    assert inst.ready is True
    assert inst.host == "chal.local"


async def test_start_instance_without_competition_id_omits_it():
    def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content) == {"challenge_id": "SAMPLE-001"}
        return httpx.Response(200, json={"id": "i1", "status": "ready"})

    inst = await _adapter(handler, competition_id="").start_instance("SAMPLE-001")
    assert inst.status is InstanceStatus.READY


async def test_start_instance_starting_then_ready_from_services():
    # Real lifecycle: POST 202 {id, status:"starting", start_wait} with no address
    # yet, then GET /instances/{id} returns ready with the services[] array (the
    # real Agent Track: tcp over tls_sni on 443). One start_instance call enriches.
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "POST":
            return httpx.Response(
                202, json={"id": "inst-9", "status": "starting", "start_wait": 5}
            )
        assert req.url.path == "/api/v1/instances/inst-9"
        return httpx.Response(
            200,
            json={
                "id": "inst-9",
                "status": "ready",
                "ttl": 10800,
                "network_topology": "tls_sni",
                "services": [
                    {
                        "service_type": "tcp",
                        "host": "inst-9.chal.ctfy.run",
                        "port": 443,
                        "access": "tls_sni",
                        "label": "app",
                    }
                ],
            },
        )

    inst = await _adapter(handler).start_instance("SAMPLE-001")
    assert calls == ["POST /api/v1/instances", "GET /api/v1/instances/inst-9"]
    assert inst.id == "inst-9"
    assert inst.status is InstanceStatus.READY
    assert inst.host == "inst-9.chal.ctfy.run"
    assert inst.port == 443
    assert inst.tls_sni is True


async def test_start_instance_benchmark_vpn_service_is_raw_tcp():
    # Benchmark comps publish services behind WireGuard: access="vpn", raw tcp.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(202, json={"id": "b1", "status": "starting"})
        return httpx.Response(
            200,
            json={
                "id": "b1",
                "status": "ready",
                "network_topology": "engagement",
                "vpn_backend": "wireguard",
                "services": [
                    {
                        "service_type": "tcp",
                        "host": "snt6orcpps3xfupeh.internal",
                        "port": 9999,
                        "access": "vpn",
                        "label": "app",
                    }
                ],
            },
        )

    inst = await _adapter(handler).start_instance("CYBENCH-001")
    assert inst.host == "snt6orcpps3xfupeh.internal"
    assert inst.port == 9999
    assert inst.tls_sni is False  # raw tcp over the VPN, not TLS+SNI


async def test_start_instance_returns_existing_on_duplicate_429():
    # The port contract is "Start (or return)": the caller polls readiness by
    # re-calling start_instance, so a second create collides with the instance we
    # already hold and the platform 429s the duplicate. Rather than fail the solve
    # (and orphan the instance it already made), the adapter looks our instance up
    # for this challenge and returns it ready.
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "POST":
            return httpx.Response(429, json={"code": "rate_limited"})
        if req.url.path == "/api/v1/instances":
            return httpx.Response(
                200,
                json={"items": [
                    {"id": "inst-7", "challenge_id": "SAMPLE-001", "status": "ready"}
                ]},
            )
        assert req.url.path == "/api/v1/instances/inst-7"
        return httpx.Response(
            200,
            json={
                "id": "inst-7",
                "status": "ready",
                "services": [
                    {"service_type": "tcp", "host": "inst-7.chal.ctfy.run",
                     "port": 443, "access": "tls_sni", "label": "app"}
                ],
            },
        )

    inst = await _adapter(handler, FakeClock()).start_instance("SAMPLE-001")
    assert inst.id == "inst-7"
    assert inst.status is InstanceStatus.READY
    assert inst.host == "inst-7.chal.ctfy.run"
    # the duplicate create 429'd (retried to exhaustion), then "or return" won
    assert calls.count("POST /api/v1/instances") == 4
    assert "GET /api/v1/instances" in calls
    assert "GET /api/v1/instances/inst-7" in calls


async def test_start_instance_reraises_429_without_our_instance():
    # A genuine quota/transient 429 with no instance of ours: the failure stands
    # (don't silently swallow it — the caller must see the start failed).
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(429, json={"code": "rate_limited"})
        return httpx.Response(200, json={"items": []})

    with pytest.raises(TransientError):
        await _adapter(handler, FakeClock()).start_instance("SAMPLE-001")


@pytest.mark.parametrize("status", [403, 409, 423])
async def test_start_instance_waits_for_paused_session_then_returns_ready(status):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, json=PAUSED_BODY)
        return httpx.Response(
            200, json={"id": "i1", "status": "ready", "host": "chal.local", "port": 443}
        )

    clock = FakeClock()
    inst = await _adapter(handler, clock).start_instance("SAMPLE-001")

    assert inst.ready
    assert inst.host == "chal.local"
    assert calls == 2
    assert clock.slept == [2057694]


async def test_start_instance_repeated_pauses_honor_header_and_body_waits():
    responses = iter([
        httpx.Response(409, headers={"Retry-After": "3"}, json={}),
        httpx.Response(
            403, json={"code": "competition_paused", "detail": {"retry_after_s": 5}}
        ),
        httpx.Response(
            200, json={"id": "i1", "status": "ready", "host": "chal.local", "port": 443}
        ),
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        return next(responses)

    clock = FakeClock()
    inst = await _adapter(handler, clock).start_instance("SAMPLE-001")

    assert inst.ready
    assert clock.slept == [3, 5]


async def test_start_instance_does_not_wait_after_final_session():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403, json={"code": "competition_not_in_session", "detail": {"retry_after_s": 5}}
        )

    clock = FakeClock()
    with pytest.raises(RuntimeError):
        await _adapter(handler, clock).start_instance("SAMPLE-001")
    assert calls == 1
    assert clock.slept == []


# --- submit: every VerdictStatus --------------------------------------------
async def test_submit_resolves_all_verdict_statuses():
    cases = {
        "correct": VerdictStatus.CORRECT,
        "wrong": VerdictStatus.WRONG,
        "cap_reached": VerdictStatus.CAPPED,
        "grader_unavailable": VerdictStatus.UNGRADED,
        "competition_paused": VerdictStatus.PAUSED,
    }
    for signal, expected in cases.items():
        def handler(req: httpx.Request, _s=signal) -> httpx.Response:
            if req.method == "GET" and req.url.path == "/api/v1/instances":
                return httpx.Response(200, json={"items": []})
            assert req.url.path == "/api/v1/submissions"
            return httpx.Response(200, json={"status": _s})

        v = await _adapter(handler).submit("SAMPLE-001", "slug", "ctfy{x}")
        assert v.status is expected, f"signal {signal!r} -> {v.status}"


async def test_submit_sends_answer_field():
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        # No live instance for this challenge: GET /instances yields an empty list
        # so no instance_id is attached (a static-flag jeopardy submission).
        if req.method == "GET" and req.url.path == "/api/v1/instances":
            return httpx.Response(200, json={"items": []})
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "correct"})

    await _adapter(handler).submit("SAMPLE-001", "slug", "ctfy{win}")
    assert seen["path"] == "/api/v1/submissions"
    assert seen["body"] == {
        "challenge_id": "SAMPLE-001",
        "question_id": "slug",
        "answer": "ctfy{win}",
        "competition_id": COMPETITION_ID,
    }


async def test_submit_attaches_instance_id_for_instance_challenge():
    # Instance challenges grade against the running instance: /submissions REQUIRES
    # instance_id (422 "instance_id: Field required" without it), so a correct flag
    # only scores when the submission carries it. The adapter looks up the live
    # instance for the challenge and attaches its id.
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v1/instances":
            return httpx.Response(200, json={"items": [
                {"id": "inst-42", "challenge_id": "SAMPLE-001", "status": "ready"}
            ]})
        if req.url.path == "/api/v1/instances/inst-42":
            return httpx.Response(200, json={"id": "inst-42", "status": "ready"})
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "correct"})

    v = await _adapter(handler).submit("SAMPLE-001", "flag", "FLAG{win}")
    assert v.status is VerdictStatus.CORRECT
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["instance_id"] == "inst-42"
    assert body["challenge_id"] == "SAMPLE-001"


async def test_submit_wrong_carries_attempts_left():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "wrong", "attempts_left": 4})

    v = await _adapter(handler).submit("SAMPLE-001", "slug", "ctfy{nope}")
    assert v.status is VerdictStatus.WRONG
    assert v.attempts_left == 4


async def test_submit_403_competition_paused_maps_to_paused():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=PAUSED_BODY)

    v = await _adapter(handler).submit("SAMPLE-001", "slug", "ctfy{x}")
    assert v.status is VerdictStatus.PAUSED
    # retry_after_s is lifted from the nested `detail` and preserved in the message.
    assert "retry_after_s=2057694" in v.message
    assert "next_session_starts_at=2026-11-15T02:00:00+00:00" in v.message


async def test_submit_http_409_is_paused():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={})

    v = await _adapter(handler).submit("SAMPLE-001", "slug", "ctfy{x}")
    assert v.status is VerdictStatus.PAUSED


async def test_submit_plain_403_still_raises():
    # A forbidden that is NOT a pause is a hard auth failure, not a verdict.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Forbidden"})

    with pytest.raises(RuntimeError, match="auth failed"):
        await _adapter(handler).submit("SAMPLE-001", "slug", "ctfy{x}")


# --- submit_qa --------------------------------------------------------------
async def test_submit_qa_routes_to_qa_endpoint():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "correct"})

    v = await _adapter(handler).submit_qa("QA-1", "q1", "A")
    assert seen["path"] == "/api/v1/qa/submissions"
    # QA grading 422s without competition_id — it MUST ride in the body.
    assert seen["body"] == {
        "challenge_id": "QA-1", "question_id": "q1", "answer": "A",
        "competition_id": COMPETITION_ID,
    }
    assert v.status is VerdictStatus.CORRECT


async def test_submit_qa_grades_real_response_shape():
    # The live QA verdict shape: {correct, attempts_remaining, ...} (no "status").
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "s1", "correct": False, "attempts_remaining": 9,
                       "challenge_fully_solved": False}
        )

    v = await _adapter(handler).submit_qa("SAMPLE-005", "flag", "nope")
    assert v.status is VerdictStatus.WRONG
    assert v.attempts_left == 9


async def test_submit_qa_403_competition_paused_maps_to_paused():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=PAUSED_BODY)

    v = await _adapter(handler).submit_qa("QA-1", "q1", "A")
    assert v.status is VerdictStatus.PAUSED


# --- resilience -------------------------------------------------------------
async def test_retry_after_honored_through_clock():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "4"}, json={})
        return httpx.Response(200, json={"status": "correct"})

    clock = FakeClock()
    v = await _adapter(handler, clock).submit("SAMPLE-001", "slug", "ctfy{x}")
    assert v.status is VerdictStatus.CORRECT
    assert clock.slept == [4.0]


# --- scoreboard -------------------------------------------------------------
async def test_scoreboard_parses_real_entries():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == f"/api/v1/competitions/{COMPETITION_ID}/scoreboard"
        return httpx.Response(200, json=SCOREBOARD)

    sb = await _adapter(handler).scoreboard(COMPETITION_ID)
    assert len(sb.entries) == 1
    e = sb.entries[0]
    assert e.rank == 1
    assert e.team == "Polaris"   # team_name -> ScoreboardEntry.team
    assert e.score == 0.0
    assert e.solved == 0


async def test_scoreboard_falls_back_to_configured_competition_id():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == f"/api/v1/competitions/{COMPETITION_ID}/scoreboard"
        return httpx.Response(200, json=SCOREBOARD)

    # Empty argument -> the adapter's configured competition_id is used.
    sb = await _adapter(handler).scoreboard("")
    assert sb.entries[0].team == "Polaris"


async def test_error_body_is_scrubbed_before_diagnostic_truncation():
    token = "TOKEN_BOUNDARY_0123456789"

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(403, text="x" * 195 + token + " forbidden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CtfyRestAdapter(
            "https://platform.test", token, clock=FakeClock(), client=client
        )
        with pytest.raises(RuntimeError) as caught:
            await adapter.get_challenge("c1")
    assert "TOKEN" not in str(caught.value)
    assert "***" in str(caught.value)


async def test_transport_error_traceback_does_not_expose_constructor_token():
    token = 'synthetic-platform-"credential\\0123456789'

    def handler(request):
        raise httpx.ReadTimeout("transport diagnostic " + token, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CtfyRestAdapter(
            "https://platform.test", token, clock=FakeClock(), client=client
        )
        with pytest.raises(TransientError) as caught:
            await adapter.get_challenge("c1")
    rendered = "".join(traceback.format_exception(caught.value))
    assert token not in rendered
    assert "synthetic-platform-" not in rendered
