"""GatewayAdapter — LLMPort via the platform model gateway (CTFY-0001 §1.1).

The production LLM seam. Points at ``LLM_BASE_URL`` (reachable even under
``internet_policy=deny``) and speaks whichever wire format the gateway exposes,
selected by ``LLM_FLAVOR``:

* ``anthropic`` — POST ``/v1/messages`` (``x-api-key`` + ``anthropic-version``).
* ``openai`` (the default for any other value) — POST ``/v1/chat/completions``
  (``Authorization: Bearer``).

Credentials come only from Settings/env — never hard-coded. An empty API key is
a real, funded-run failure: the constructor raises so the run stops loudly
instead of silently falling back to a personal key.

Transient HTTP (429 / 5xx) and transport errors are mapped to
``TransientError`` and retried with backoff through the shared resilience layer
(waiting goes via the injected ``ClockPort``, so tests stay deterministic). Auth
and other 4xx are hard ``RuntimeError`` and never retried. No network happens at
import or construction time, and the ``httpx.AsyncClient`` can be injected
(e.g. with ``httpx.MockTransport``) for unit tests.

Implements ports.llm.LLMPort.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from agent.adapters._resilience import TransientError, with_retry
from agent.domain.state import LLMResponse, Msg, ToolCall, ToolDef
from agent.ports.clock import ClockPort

# Wire constants ------------------------------------------------------------
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_PATH = "/v1/messages"
_OPENAI_PATH = "/v1/chat/completions"
_DEFAULT_MAX_TOKENS = 4096

# Roles the OpenAI chat API accepts verbatim; "tool" needs a tool_call_id we do
# not carry in the plain Msg model, so it is folded into a user turn instead.
_OPENAI_ROLES = frozenset({"system", "user", "assistant"})

# effort -> provider knob (best-effort; "medium" means the provider default, so
# the hot path carries no extra field that a non-reasoning model might reject).
_OPENAI_EFFORT = {"low": "low", "high": "high"}
# effort -> anthropic extended-thinking budget in tokens (0 = plain response).
_ANTHROPIC_THINKING_BUDGET = {"high": 2048}


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into seconds, or None if absent/unparseable.

    Accepts both the delta-seconds form (``"12"``) and the HTTP-date form,
    always returning a non-negative delay.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _loads_tool_args(raw: Any) -> dict[str, Any]:
    """Coerce a tool-call argument payload into a dict.

    OpenAI sends ``arguments`` as a JSON *string*; some gateways pre-parse it to
    an object. Anything malformed degrades to ``{}`` rather than raising — a bad
    argument blob is untrusted tool output, not a reason to crash the turn.
    """
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class GatewayAdapter:
    """LLMPort backed by the platform model gateway (anthropic/openai flavors)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        flavor: str,
        *,
        clock: ClockPort,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            # CTFY-0001 §1.1: an empty key is a real state (unfunded run). Fail
            # loudly rather than fall back to a personal key.
            raise RuntimeError("LLM_API_KEY is empty — this run is not funded.")
        self.base_url = base_url
        self.api_key = api_key
        self.flavor = flavor  # "anthropic" | "openai" (default/other -> openai)
        self.clock = clock
        # No network here: AsyncClient construction opens no connections.
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=60)

    async def complete(
        self,
        messages: list[Msg],
        *,
        model: str,
        effort: str = "medium",
        tools: list[ToolDef] | None = None,
    ) -> LLMResponse:
        """One model turn. See ports.llm.LLMPort.complete."""
        if self.flavor == "anthropic":
            path = _ANTHROPIC_PATH
            headers = self._anthropic_headers()
            body = self._anthropic_payload(messages, model, effort, tools)
            parse = self._parse_anthropic
        else:
            path = _OPENAI_PATH
            headers = self._openai_headers()
            body = self._openai_payload(messages, model, effort, tools)
            parse = self._parse_openai

        async def _do() -> LLMResponse:
            try:
                resp = await self._client.post(path, headers=headers, json=body)
            except httpx.TransportError as e:  # timeouts + network errors
                raise TransientError(f"gateway transport error: {e}") from e
            status = resp.status_code
            if status == 429 or status >= 500:
                raise TransientError(
                    f"gateway {status}",
                    retry_after=_parse_retry_after(resp.headers.get("retry-after")),
                )
            if status >= 400:
                # 401/403 (auth) and any other 4xx: a hard failure, never retried.
                raise RuntimeError(f"gateway error {status}: {resp.text[:500]}")
            try:
                data = resp.json()
            except ValueError as e:
                # 2xx with a non-JSON body: likely a flaky proxy — worth a retry.
                raise TransientError(f"gateway returned non-JSON body: {e}") from e
            return parse(data)

        return await with_retry(_do, clock=self.clock, label=f"gateway:{self.flavor}")

    async def aclose(self) -> None:
        """Close the underlying HTTP client (no-op if one was injected elsewhere)."""
        await self._client.aclose()

    # -- anthropic ---------------------------------------------------------
    def _anthropic_headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": _ANTHROPIC_VERSION}

    def _anthropic_payload(
        self,
        messages: list[Msg],
        model: str,
        effort: str,
        tools: list[ToolDef] | None,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        conversation: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            # anthropic knows only user/assistant; fold tool/other into user.
            role = "assistant" if m.role == "assistant" else "user"
            conversation.append({"role": role, "content": m.content})

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": conversation,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.schema}
                for t in tools
            ]
        budget = _ANTHROPIC_THINKING_BUDGET.get(effort, 0)
        if 0 < budget < _DEFAULT_MAX_TOKENS:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return body

    def _parse_anthropic(self, data: dict[str, Any]) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Only `text` and `tool_use` carry answer content. Every other block type
        # is intentionally ignored — notably `thinking` (extended-thinking, and
        # the reasoning blocks GLM-4.6 returns on the bigmodel anthropic-compatible
        # endpoint), plus any future type. Skipping unknown blocks (rather than
        # raising) is what keeps a reasoning model's turn from crashing the loop.
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                        id=block.get("id", ""),
                    )
                )
        usage = data.get("usage") or {}
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            stop_reason=data.get("stop_reason") or "",
        )

    # -- openai ------------------------------------------------------------
    def _openai_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _openai_payload(
        self,
        messages: list[Msg],
        model: str,
        effort: str,
        tools: list[ToolDef] | None,
    ) -> dict[str, Any]:
        conversation = [
            {"role": m.role if m.role in _OPENAI_ROLES else "user", "content": m.content}
            for m in messages
        ]
        body: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "max_tokens": _DEFAULT_MAX_TOKENS,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema,
                    },
                }
                for t in tools
            ]
        if effort in _OPENAI_EFFORT:
            body["reasoning_effort"] = _OPENAI_EFFORT[effort]
        return body

    def _parse_openai(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append(
                ToolCall(
                    name=fn.get("name", ""),
                    arguments=_loads_tool_args(fn.get("arguments")),
                    id=tc.get("id", ""),
                )
            )
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            stop_reason=first.get("finish_reason") or "",
        )
