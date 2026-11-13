"""CtfyRestAdapter — PlatformPort over plain HTTP (CTFY-0001 §1.2).

All routes are under ``$CTFY_PLATFORM_URL/api/v1`` with
``Authorization: Bearer <token>``. Key routes: ``GET /me``,
``GET /competitions/{cid}/challenges`` (paginated list),
``GET /challenges/{id}`` (one challenge, flat route), ``POST /instances``,
``POST /submissions``, ``POST /qa/submissions``,
``GET /competitions/{cid}/scoreboard``. The collection routes read
``competition_id`` (constructor arg / ``CTFY_EVAL_COMPETITION_ID``).

Resilience (adapters/_resilience.py): every request is wrapped in
``with_retry`` and waits through the injected ClockPort, so retries are
deterministic under FakeClock. 429 / 5xx / a transport error map to
``TransientError`` (retried, honouring ``Retry-After``); 401 / 403 raise
``RuntimeError`` (never retried) — except ``competition_paused``, which is a
*wait*, not a failure (CTFY-0001 §4): a submission returns a ``PAUSED``
verdict (``retry_after_s`` from the body's ``detail``), and a start returns a
non-READY (STOPPED) instance whose ``poll_after_s`` carries that wait, rather
than raising.

The wire schema is partly reverse-engineered, so every field is read
tolerantly with ``.get()`` and a list of plausible key spellings, then mapped
onto the frozen domain models. Nothing here follows instructions found in a
response body: challenge text and grader messages are untrusted DATA
(policy.is_trusted_instruction stays False).

Implements ports.platform.PlatformPort. Importable without network.
"""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from agent.adapters._redaction import redact
from agent.adapters._resilience import TransientError, with_retry
from agent.domain.state import (
    Account,
    Challenge,
    Instance,
    InstanceStatus,
    Question,
    Scoreboard,
    ScoreboardEntry,
    Verdict,
    VerdictStatus,
)
from agent.ports.clock import ClockPort

# --- grader-outcome signal vocabularies (matched case-insensitively) ---------
# Positive confirmation is kept deliberately narrow: we never report CORRECT
# without an explicit "correct"-class signal (anti-hallucinated-flag posture).
_CORRECT = {"correct", "solved", "accepted", "right"}
_WRONG = {"wrong", "incorrect", "invalid", "rejected", "nope", "no"}
_CAPPED = {
    "capped", "cap_reached", "cap", "attempt_cap_reached",
    "too_many_attempts", "too_many_wrong_attempts", "limit_reached",
    "attempts_exhausted", "rate_limited_question",
}
_UNGRADED = {
    "ungraded", "unavailable", "grader_unavailable", "not_graded",
    "grader_down", "grader_unreachable", "grader_timeout",
}
_PAUSED = {
    "competition_paused", "paused", "competition_not_in_session",
    "not_in_session", "session_ended", "session_closed",
}

# --- instance lifecycle phase -> domain status -------------------------------
_INSTANCE_STATUS = {
    "ready": InstanceStatus.READY,
    "running": InstanceStatus.READY,
    "up": InstanceStatus.READY,
    "healthy": InstanceStatus.READY,
    "active": InstanceStatus.READY,
    "available": InstanceStatus.READY,
    "starting": InstanceStatus.STARTING,
    "pending": InstanceStatus.STARTING,
    "provisioning": InstanceStatus.STARTING,
    "creating": InstanceStatus.STARTING,
    "building": InstanceStatus.STARTING,
    "queued": InstanceStatus.STARTING,
    "booting": InstanceStatus.STARTING,
    "stopped": InstanceStatus.STOPPED,
    "stopping": InstanceStatus.STOPPED,
    "down": InstanceStatus.STOPPED,
    "terminated": InstanceStatus.STOPPED,
    "expired": InstanceStatus.STOPPED,
    "error": InstanceStatus.ERROR,
    "failed": InstanceStatus.ERROR,
    "crashed": InstanceStatus.ERROR,
}


class CtfyRestAdapter:
    """PlatformPort talking to the ctfy platform REST API over HTTP."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        clock: ClockPort,
        competition_id: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + "/api/v1"
        # The token lives only in a header built here; never logged or persisted.
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        self._redaction_secrets = (token,)
        self._clock = clock
        # Which competition the collection routes (list_challenges, scoreboard)
        # read from. The platform publishes it as CTFY_EVAL_COMPETITION_ID; empty
        # is a real state (challenge-only runs), so the guard lives at call time.
        self._competition_id = competition_id
        # Construction opens no connection (httpx is lazy), so import/build is
        # network-free. An injected client is reused as-is and never closed by us.
        self._client = client if client is not None else httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        # challenge_id -> live instance id, remembered from start_instance so a
        # submission can carry the instance_id the grader requires (the flag is a
        # per-team dynamic value tied to the running instance).
        self._instance_ids: dict[str, str] = {}

    # ------------------------------------------------------------------ port --
    async def whoami(self) -> Account:
        resp = await self._request("GET", "/me")
        self._checked(resp, "GET /me")
        d = self._unwrap(self._json_dict(resp), "data", "user", "account", "result")
        return Account(
            id=self._as_str(self._pick(d, "id", "user_id", "account_id", "uid", "sub")),
            # Real /me carries display_name (no name/username), so it is the
            # primary spelling; the rest stay as tolerant fallbacks.
            name=self._as_str(
                self._pick(d, "display_name", "name", "username", "handle",
                           "team_name", "team", default="")
            ),
        )

    async def list_challenges(self) -> list[Challenge]:
        if not self._competition_id:
            raise RuntimeError(
                "list_challenges needs a competition id, but none was configured. "
                "Set CTFY_EVAL_COMPETITION_ID (passed to CtfyRestAdapter as "
                "competition_id) to the competition to enumerate."
            )
        path = f"/competitions/{self._competition_id}/challenges"
        out: list[Challenge] = []
        seen: set[str] = set()
        limit = 50
        offset = 0
        # Page until the server's `total` is covered (authoritative when present),
        # falling back to a short/empty page. The cap bounds a misbehaving server.
        for _ in range(200):
            resp = await self._request(
                "GET", path, params={"limit": limit, "offset": offset}
            )
            self._checked(resp, f"GET {path}")
            data = self._json(resp)
            items = self._as_list(data, "items", "challenges", "data", "results")
            for c in items:
                parsed = self._parse_challenge(c)
                if parsed is not None and parsed.id not in seen:
                    seen.add(parsed.id)
                    out.append(parsed)
            if not items:
                break
            offset += len(items)
            total = self._as_int(data.get("total"), default=-1) if isinstance(data, dict) else -1
            if total >= 0:
                if offset >= total:
                    break
            elif len(items) < limit:
                break
        return out

    async def get_challenge(self, cid: str) -> Challenge:
        resp = await self._request("GET", f"/challenges/{cid}")
        self._checked(resp, f"GET /challenges/{cid}")
        d = self._unwrap(self._json_dict(resp), "data", "challenge", "result")
        if isinstance(d, dict):
            d.setdefault("id", cid)  # guarantee an id even if the body omits it
        parsed = self._parse_challenge(d)
        if parsed is None:
            raise RuntimeError(
                f"GET /challenges/{cid}: unparseable response {self._snippet(resp)}"
            )
        return parsed

    async def start_instance(self, cid: str) -> Instance:
        cached_id = self._instance_ids.get(cid)
        if cached_id:
            detail = await self._get_instance_detail(cached_id)
            if detail is not None:
                cached = self._instance_from_detail(detail, fallback_id=cached_id)
                if cached.status not in (InstanceStatus.STOPPED, InstanceStatus.ERROR):
                    return cached
            # A stopped/expired instance no longer satisfies "return"; permit a
            # fresh create on the next path.
            self._instance_ids.pop(cid, None)
        # The POST body MUST carry competition_id: without it the platform routes
        # the start to the account's default (Agent Track) competition, which is
        # paused outside a session and 403s (verified against the live API). When
        # no competition is configured (challenge-only dev runs) we omit it and
        # let the platform use its own default.
        body: dict[str, Any] = {"challenge_id": cid}
        if self._competition_id:
            body["competition_id"] = self._competition_id
        # A paused competition is a wait inside this I/O adapter, not a
        # fabricated terminal instance. Keep the frozen Instance status truthful.
        try:
            while True:
                resp = await self._request("POST", "/instances", json=body)
                pause_delay = self._instance_pause_delay(resp)
                if pause_delay is None:
                    break
                await self._clock.sleep(pause_delay)
        except TransientError:
            # Honor the frozen contract's "Start (or return)": the platform
            # allows ~1 live instance per team and 429s a *duplicate* create
            # while ours is still STARTING/READY. The caller polls readiness by
            # re-calling start_instance (per the port docstring), so a second
            # create naturally collides — return the instance we already hold
            # instead of hammering create and leaking it. If none is actually
            # ours, the 429 is a genuine transient/quota failure: re-raise.
            existing = await self._find_team_instance(cid)
            if existing is not None:
                if existing.id:
                    self._instance_ids[cid] = existing.id
                return existing
            raise
        self._checked(resp, "POST /instances")
        d = self._unwrap(self._json_dict(resp), "data", "instance", "result")
        inst = self._instance_from_detail(d, fallback_id=cid, resp=resp)
        # The create reply (202) is {id, status:"starting", start_wait} with no
        # services yet; the connectable host/port only appear once the instance
        # is ready, via GET /instances/{id}. When the create is not ready and has
        # no service address yet, fetch the authoritative current detail so one
        # poll still returns real host/port the moment the instance comes up.
        if inst.status is not InstanceStatus.READY and inst.id and not inst.host:
            detail = await self._get_instance_detail(inst.id)
            if detail is not None:
                inst = self._instance_from_detail(detail, fallback_id=inst.id)
        if inst.id:
            self._instance_ids[cid] = inst.id
        return inst

    async def _get_instance_detail(self, iid: str) -> dict[str, Any] | None:
        """Best-effort GET /instances/{id} for the current state. Never raises:
        enrichment is optional, so a transport/HTTP error just yields None and the
        caller keeps the create-reply instance (STARTING) and polls again."""
        try:
            resp = await self._request("GET", f"/instances/{iid}")
        except Exception:
            return None
        if not resp.is_success:
            return None
        return self._unwrap(self._json_dict(resp), "data", "instance", "result")

    async def _find_team_instance(self, cid: str) -> Instance | None:
        """Return this team's existing instance for ``cid`` (the "or return" half
        of start_instance), enriched with real host/port, or None.

        Never raises: a lookup that itself 429s / errors just yields None and the
        original create failure stands. GET /instances lists the team's live
        instances ({"items": [{id, challenge_id, status, ...}]}); the list rows
        carry no service address, so fetch the authoritative detail for a match.
        """
        try:
            resp = await self._request("GET", "/instances")
        except TransientError:
            return None
        if not resp.is_success:
            return None
        data = self._json_dict(resp)
        items = data.get("items")
        if not isinstance(items, list):
            items = self._unwrap(data, "data", "result").get("items", [])
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("challenge_id") == cid:
                iid = it.get("id")
                detail = await self._get_instance_detail(iid) if iid else None
                return self._instance_from_detail(
                    detail if detail is not None else it, fallback_id=iid or cid
                )
        return None

    def _instance_from_detail(
        self, d: dict[str, Any], *, fallback_id: str, resp: httpx.Response | None = None
    ) -> Instance:
        """Map an instance body (POST create reply or GET detail) onto Instance.

        The ready shape nests the connection under ``services: [{service_type,
        host, port, access, label}]`` (benchmark uses access="vpn" on raw tcp;
        the real Agent Track uses access="tls_sni" on 443). Fall back to flat
        top-level host/port for tolerance.
        """
        svc = self._primary_service(d)
        src = svc if svc is not None else d
        poll = self._as_float(
            self._pick(
                d, "poll_after_s", "poll_after", "retry_after_s", "retry_after",
                "start_wait",
            ),
            default=None,
        )
        if poll is None and resp is not None:
            poll = self._retry_after(resp)
        return Instance(
            id=self._as_str(self._pick(d, "id", "instance_id", "iid", default=fallback_id)),
            host=self._as_str(
                self._pick(src, "host", "hostname", "address", "ip", "sni",
                           "server", default="")
            ),
            port=self._as_int(self._pick(src, "port", "tcp_port"), default=443),
            tls_sni=self._service_tls(src),
            status=self._instance_status(d),
            poll_after_s=poll if poll is not None else 2.0,
            expires_at=self._expires_at(d),
        )

    @classmethod
    def _primary_service(cls, d: dict[str, Any]) -> dict[str, Any] | None:
        """The connectable service from a ready instance's ``services`` array, or
        None when the instance is still starting (no services published yet)."""
        raw = cls._pick(d, "services", "endpoints", default=None)
        svcs = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
        if not svcs:
            return None

        def rank(s: dict[str, Any]) -> tuple[bool, bool]:
            has_host = bool(
                cls._pick(s, "host", "hostname", "address", "ip", "server")
            )
            label = cls._as_str(
                cls._pick(s, "label", "name", "service_type", default="")
            ).lower()
            primary = any(w in label for w in ("app", "main", "web", "http", "service"))
            return (has_host, primary)

        return max(svcs, key=rank)

    @classmethod
    def _service_tls(cls, s: dict[str, Any]) -> bool:
        """Whether to speak TLS+SNI to a service. ``access`` is authoritative when
        present (tls_sni -> True, vpn/raw tcp -> False); else infer from scheme."""
        access = cls._as_str(cls._pick(s, "access", "exposure", default="")).lower()
        if access in ("tls_sni", "sni", "tls", "https"):
            return True
        if access in ("vpn", "raw", "tcp", "plain"):
            return False
        return cls._tls_flag(s)

    async def submit(self, cid: str, qid: str, answer: str) -> Verdict:
        body: dict[str, Any] = {
            "challenge_id": cid, "question_id": qid, "answer": answer,
        }
        if self._competition_id:
            body["competition_id"] = self._competition_id
        # Instance challenges grade against the running instance (the flag is a
        # per-team dynamic value), so /submissions REQUIRES instance_id — without
        # it the platform 422s ("instance_id: Field required") and a correct flag
        # never scores. Use the id remembered from start_instance; if absent (e.g.
        # the adapter never started it this run), look it up once.
        iid = self._instance_ids.get(cid)
        if not iid:
            found = await self._find_team_instance(cid)
            iid = found.id if found is not None else ""
            if iid:
                self._instance_ids[cid] = iid
        if iid:
            body["instance_id"] = iid
        resp = await self._request("POST", "/submissions", json=body)
        return self._grade(resp)

    async def submit_qa(self, cid: str, qid: str, answer: str) -> Verdict:
        # QA grading needs competition_id in the body (the platform 422s without
        # it: "competition_id: Field required"), verified against the live API.
        body: dict[str, Any] = {
            "challenge_id": cid, "question_id": qid, "answer": answer,
        }
        if self._competition_id:
            body["competition_id"] = self._competition_id
        resp = await self._request("POST", "/qa/submissions", json=body)
        return self._grade(resp)

    async def scoreboard(self, competition_id: str) -> Scoreboard:
        # The port passes the competition explicitly; fall back to the one fixed
        # at construction so a caller with only a configured adapter still works.
        cid = competition_id or self._competition_id
        if not cid:
            raise RuntimeError(
                "scoreboard needs a competition id (argument or the adapter's "
                "configured competition_id / CTFY_EVAL_COMPETITION_ID)."
            )
        resp = await self._request("GET", f"/competitions/{cid}/scoreboard")
        self._checked(resp, f"GET /competitions/{cid}/scoreboard")
        data = self._json(resp)
        items = self._as_list(
            data, "items", "entries", "standings", "scoreboard", "data",
            "results", "teams",
        )
        entries = [
            e for e in (self._parse_entry(x, i) for i, x in enumerate(items))
            if e is not None
        ]
        retry = None
        if isinstance(data, dict):
            retry = self._as_float(
                self._pick(data, "retry_after_s", "retry_after"), default=None
            )
            if retry is None:
                meta = data.get("meta")
                if isinstance(meta, dict):
                    retry = self._as_float(
                        self._pick(meta, "retry_after_s", "retry_after"), default=None
                    )
        if retry is None:
            retry = self._retry_after(resp)
        return Scoreboard(entries=entries, retry_after_s=retry)

    async def aclose(self) -> None:
        """Close the HTTP client, but only the one we created ourselves."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------- transport --
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one request, retrying only TransientError (429/5xx/transport).

        A non-retryable status (4xx) is returned to the caller unraised so the
        caller can map it (auth error, or a gradable submission outcome).
        """
        async def _once() -> httpx.Response:
            return await self._send(method, path, json=json, params=params)

        label = redact(f"{method} {path}", extra_secrets=self._redaction_secrets)
        return await with_retry(_once, clock=self._clock, label=label)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            resp = await self._client.request(
                method, self._base + path,
                headers=self._headers, json=json, params=params,
            )
        except httpx.HTTPError as exc:  # transport: connect/read/timeout/protocol
            detail = redact(
                f"{method} {path}: {exc!r}", extra_secrets=self._redaction_secrets
            )
            raise TransientError(detail) from None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(
                f"{method} {path} -> HTTP {resp.status_code}",
                retry_after=self._retry_after(resp),
            )
        return resp

    def _checked(self, resp: httpx.Response, label: str) -> None:
        """Raise RuntimeError on any non-2xx left after retry (incl. 401/403)."""
        if resp.is_success:
            return
        kind = "auth failed" if resp.status_code in (401, 403) else "request failed"
        raise RuntimeError(
            f"{label}: {kind} (HTTP {resp.status_code}) {self._snippet(resp)}"
        )

    # --------------------------------------------------------------- grading --
    def _grade(self, resp: httpx.Response) -> Verdict:
        """Map a submission response onto a Verdict (CTFY-0001 §4, CTFY-0002 §5).

        Ordinary outcomes (wrong / capped / ungraded / paused) are returned, not
        raised; only a hard auth failure or a truly unrecognizable error raises.
        """
        data = self._json_dict(resp)
        inner = self._unwrap(data, "data", "result", "submission")
        merged: dict[str, Any] = {**data, **inner} if isinstance(inner, dict) else dict(data)
        # A `competition_paused` error nests its fields under `detail`
        # (message / next_session_starts_at / retry_after_s); lift them so the
        # paused branch and _paused_message can read them at the top level.
        detail = data.get("detail")
        if isinstance(detail, dict):
            merged = {**merged, **detail}
        signals = {
            str(merged[k]).strip().lower()
            for k in ("code", "status", "result", "verdict", "state", "outcome", "grade")
            if isinstance(merged.get(k), str)
        }
        message = self._as_str(
            self._pick(merged, "message", "detail", "error", "reason",
                       "msg", "description", default="")
        )
        attempts = self._attempts_left(merged)
        status = resp.status_code

        # 1) Paused — a wait, never a challenge failure (CTFY-0001 §4).
        if (
            status in (409, 423)
            or (signals & _PAUSED)
            or self._flag(merged, "paused") is True
        ):
            return Verdict(VerdictStatus.PAUSED, self._paused_message(merged, message), attempts)

        # 2) Hard auth / forbidden (non-paused) — not an ordinary outcome.
        if status in (401, 403):
            raise RuntimeError(
                f"submit: auth failed (HTTP {status}) {self._snippet(resp)}"
            )

        # 3) Cap reached — checked before grading, costs no attempt (CTFY-0002 §5.5).
        if (signals & _CAPPED) or self._flag(merged, "capped") is True:
            return Verdict(
                VerdictStatus.CAPPED, message, attempts if attempts is not None else 0
            )

        # 4) Grader unavailable — neither right nor wrong, costs no attempt (§5.4).
        if (signals & _UNGRADED) or self._flag(merged, "graded") is False:
            return Verdict(VerdictStatus.UNGRADED, message, attempts)

        # 5) Correct — only on an explicit positive signal.
        if (
            (signals & _CORRECT)
            or self._flag(merged, "correct") is True
            or self._flag(merged, "is_correct") is True
            or self._flag(merged, "solved") is True
        ):
            return Verdict(VerdictStatus.CORRECT, message, attempts)

        # 6) Wrong.
        if (
            (signals & _WRONG)
            or self._flag(merged, "correct") is False
            or self._flag(merged, "is_correct") is False
            or self._flag(merged, "solved") is False
        ):
            return Verdict(VerdictStatus.WRONG, message, attempts)

        # 7) Unrecognized: a 2xx with no positive signal is treated as WRONG
        #    (never claim CORRECT without evidence); anything else is an error.
        if resp.is_success:
            return Verdict(VerdictStatus.WRONG, message or self._snippet(resp), attempts)
        raise RuntimeError(
            f"submit: unexpected response (HTTP {status}) {self._snippet(resp)}"
        )

    @staticmethod
    def _paused_message(merged: dict[str, Any], message: str) -> str:
        parts = [message or "competition paused"]
        nxt = merged.get("next_session_starts_at")
        if nxt:
            parts.append(f"next_session_starts_at={nxt}")
        ra = merged.get("retry_after_s", merged.get("retry_after"))
        if ra is not None:
            parts.append(f"retry_after_s={ra}")
        return "; ".join(str(p) for p in parts)

    def _instance_pause_delay(self, resp: httpx.Response) -> float | None:
        """Return the platform's wait for a paused start, otherwise None."""
        if resp.is_success:
            return None
        data = self._json_dict(resp)
        merged: dict[str, Any] = dict(data)
        detail = data.get("detail")
        if isinstance(detail, dict):
            merged = {**merged, **detail}
        signals = {
            str(merged[k]).strip().lower()
            for k in ("code", "status", "result", "state", "outcome")
            if isinstance(merged.get(k), str)
        }
        if "competition_not_in_session" in signals:
            return None  # No future session remains; do not wait forever.
        is_paused = (
            resp.status_code in (409, 423)
            or bool(signals & _PAUSED)
            or self._flag(merged, "paused") is True
        )
        if not is_paused:
            return None
        retry = self._as_float(
            self._pick(merged, "retry_after_s", "retry_after"), default=None
        )
        if retry is None:
            retry = self._retry_after(resp)
        return retry if retry is not None else 2.0

    @staticmethod
    def _attempts_left(d: dict[str, Any]) -> int | None:
        for k in ("attempts_left", "remaining_attempts", "attempts_remaining",
                  "remaining", "attempts_left_count"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
        maxa, used = d.get("max_attempts"), d.get("attempts", d.get("wrong_attempts"))
        if isinstance(maxa, (int, float)) and isinstance(used, (int, float)):
            return max(0, int(maxa) - int(used))
        return None

    # --------------------------------------------------------------- parsing --
    @classmethod
    def _parse_challenge(cls, c: Any) -> Challenge | None:
        if not isinstance(c, dict):
            return None
        cid = cls._as_str(cls._pick(c, "id", "challenge_id", "cid", "slug", "key"))
        if not cid:
            return None
        questions: list[Question] = []
        raw_qs = cls._pick(c, "questions", "question_list", "quests", default=[])
        if isinstance(raw_qs, list):
            for q in raw_qs:
                parsed = cls._parse_question(q)
                if parsed is not None:
                    questions.append(parsed)
        # Real <challenge> carries my_solved_question_ids: the ids this team has
        # already solved. Mark them answered so unanswered() skips them and the
        # gating re-fetch (get_challenge after a correct answer) is accurate.
        solved_ids = set(
            cls._as_str_list(
                cls._pick(c, "my_solved_question_ids", "solved_question_ids", default=[])
            )
        )
        if solved_ids:
            for q in questions:
                if q.id in solved_ids:
                    q.answered = True
        gated_n = cls._as_int(
            cls._pick(c, "gated_questions", "gated", "gated_count"),
            default=sum(1 for q in questions if q.locked),
        )
        return Challenge(
            id=cid,
            name=cls._as_str(cls._pick(c, "name", "title", default="")),
            category=cls._as_str(cls._pick(c, "category", "cat", default="")),
            description=cls._as_str(
                cls._pick(c, "description", "desc", "prompt", "summary", default="")
            ),
            questions=questions,
            gated_questions=gated_n,
            difficulty=cls._as_str(cls._pick(c, "difficulty", "level", default="")),
            points=cls._as_int(
                cls._pick(c, "points", "value", "score", "point"), default=0
            ),
            solves=cls._as_int(
                cls._pick(c, "solves_count", "solves", "solve_count", "solved_count"),
                default=0,
            ),
        )

    @classmethod
    def _parse_question(cls, q: Any) -> Question | None:
        if not isinstance(q, dict):
            return None
        qid = cls._as_str(cls._pick(q, "id", "qid", "question_id", "key"))
        if not qid:
            return None
        return Question(
            id=qid,
            prompt=cls._as_str(
                cls._pick(q, "prompt", "question", "text", "title", default="")
            ),
            requires=cls._as_str_list(
                cls._pick(q, "requires", "depends_on", "gated_by", default=[])
            ),
            answered=(
                cls._flag(q, "answered") is True
                or cls._flag(q, "solved") is True
                or cls._flag(q, "is_solved") is True
            ),
            locked=cls._flag(q, "locked") is True or cls._flag(q, "gated") is True,
            choices=cls._as_str_list(cls._pick(q, "choices", "options", default=[])),
            fmt=cls._as_str(cls._pick(q, "format", "fmt", "mode", "type", default="")),
        )

    @classmethod
    def _parse_entry(cls, e: Any, idx: int) -> ScoreboardEntry | None:
        if not isinstance(e, dict):
            return None
        return ScoreboardEntry(
            rank=cls._as_int(
                cls._pick(e, "rank", "place", "position", "pos"), default=idx + 1
            ),
            team=cls._as_str(
                cls._pick(e, "team", "team_name", "name", "username",
                          "handle", default="")
            ),
            score=cls._as_float(
                cls._pick(e, "score", "points", "value", "total"), default=0.0
            )
            or 0.0,
            solved=cls._as_int(
                cls._pick(e, "solved", "solves", "solve_count", "num_solved"),
                default=0,
            ),
        )

    @classmethod
    def _instance_status(cls, d: dict[str, Any]) -> InstanceStatus:
        raw = cls._pick(d, "status", "state", "phase")
        if isinstance(raw, str):
            mapped = _INSTANCE_STATUS.get(raw.strip().lower())
            if mapped is not None:
                return mapped
        ready = cls._flag(d, "ready")
        if ready is True:
            return InstanceStatus.READY
        # No readiness signal: assume still STARTING so the caller polls once
        # more rather than attacking a service that is not up yet.
        return InstanceStatus.STARTING

    @classmethod
    def _tls_flag(cls, d: dict[str, Any]) -> bool:
        for k in ("tls_sni", "sni", "tls"):
            flag = cls._flag(d, k)
            if flag is not None:
                return flag
        scheme = cls._as_str(cls._pick(d, "scheme", "protocol", default="")).lower()
        if scheme in ("https", "tls"):
            return True
        if scheme in ("http", "tcp"):
            return False
        return True  # platform default: challenge services are TLS+SNI on 443

    def _expires_at(self, d: dict[str, Any]) -> float | None:
        """Map a server TTL to a monotonic deadline in ClockPort.now scale.

        Only a *relative* lifetime can be mapped (now + ttl); an absolute wall
        timestamp cannot be converted to the monotonic clock, so it is dropped.
        """
        for k in ("expires_in", "expires_in_s", "ttl", "ttl_s", "lifetime_s"):
            secs = self._as_float(d.get(k), default=None)
            if secs is not None:
                return self._clock.now() + secs
        return None

    # ------------------------------------------------------- response helpers --
    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return {}

    @classmethod
    def _json_dict(cls, resp: httpx.Response) -> dict[str, Any]:
        data = cls._json(resp)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _unwrap(d: Any, *keys: str) -> dict[str, Any]:
        if not isinstance(d, dict):
            return {}
        for k in (keys or ("data", "result")):
            inner = d.get(k)
            if isinstance(inner, dict):
                return inner
        return d

    @staticmethod
    def _as_list(data: Any, *keys: str) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, list):
                    return v
        return []

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        """Parse a ``Retry-After`` header: delta-seconds or an HTTP-date."""
        raw = resp.headers.get("retry-after")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            pass
        return None

    def _snippet(self, resp: httpx.Response, limit: int = 200) -> str:
        try:
            text = resp.text
        except Exception:
            return ""
        safe = redact(text, extra_secrets=self._redaction_secrets)
        return " ".join(safe.split())[:limit]

    # --------------------------------------------------------- value coercion --
    @staticmethod
    def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    @staticmethod
    def _flag(d: dict[str, Any], key: str) -> bool | None:
        """Tri-state read of a boolean-ish field: True / False / None (absent)."""
        v = d.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes", "y"):
                return True
            if s in ("false", "0", "no", "n"):
                return False
        return None

    @staticmethod
    def _as_str(v: Any, default: str = "") -> str:
        if isinstance(v, str):
            return v
        return default if v is None else str(v)

    @staticmethod
    def _as_str_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v if x is not None]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    @staticmethod
    def _as_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default

    @staticmethod
    def _as_float(v: Any, default: float | None = None) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
