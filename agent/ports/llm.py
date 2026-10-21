"""LLMPort — a single model call (one request/response, optional tool use).

FROZEN CONTRACT — see CONTRIBUTING.md before changing a signature.

Implementations: adapters/llm/{gateway,local_key}.py
"""
from __future__ import annotations

from typing import Protocol

from agent.domain.state import LLMResponse, Msg, ToolDef


class LLMPort(Protocol):
    async def complete(
        self,
        messages: list[Msg],
        *,
        model: str,
        effort: str = "medium",
        tools: list[ToolDef] | None = None,
    ) -> LLMResponse:
        """One model turn.

        Args:
            messages: the conversation so far (system/user/assistant/tool).
            model: the provider model id to request (from the platform's LLM_MODEL,
                NOT a tier name — never let the client resolve a tier the run was
                not funded for; CTFY-0001 §1.1).
            effort: reasoning effort, one of ``{"low", "medium", "high"}`` (the
                portable set); implementations map it to the provider's knob.
            tools: tool definitions to expose this turn, or None for plain text.

        Returns:
            An ``LLMResponse`` with ``text`` and/or ``tool_calls`` and the token
            counts (used for budget accounting).

        Raises:
            RuntimeError: on a genuine provider/auth failure. The resilience layer
                retries transient HTTP (429/5xx) with backoff; it does NOT retry a
                hard auth error. An unfunded run (empty key) fails at construction.
        """
        ...
