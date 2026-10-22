"""Contract: GatewayAdapter maps both gateway wire formats onto LLMResponse,
retries transient HTTP through the ClockPort, and fails loudly on an empty key.
Uses httpx.MockTransport — no real network.
"""
from __future__ import annotations

import httpx
import pytest

from agent.adapters.llm.gateway import GatewayAdapter
from agent.domain.state import Msg, ToolDef
from agent.testing.fakes import FakeClock

# The real 智谱 GLM anthropic-compatible endpoint. Requests must land at
# {base}/v1/messages — note httpx appends to the base path, so /v1/messages does
# NOT replace the base's own /api/anthropic segment.
_GLM_BASE = "https://open.bigmodel.cn/api/anthropic"


def _client(handler, base_url: str = "https://gw.test") -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


async def test_anthropic_response_mapping():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/messages"
        assert req.headers["x-api-key"] == "key123"
        assert req.headers["anthropic-version"]
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "execute", "id": "t1", "input": {"cmd": "ls"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "tool_use",
            },
        )

    g = GatewayAdapter(
        "https://gw.test", "key123", "anthropic", clock=FakeClock(), client=_client(handler)
    )
    r = await g.complete([Msg("system", "s"), Msg("user", "u")], model="m")
    assert r.text == "hello"
    assert r.tool_calls[0].name == "execute"
    assert r.tool_calls[0].arguments == {"cmd": "ls"}
    assert (r.input_tokens, r.output_tokens) == (10, 5)
    assert r.stop_reason == "tool_use"


async def test_anthropic_real_glm_ignores_thinking_block():
    """A faithful GLM-4.6 reply (thinking + text) over the bigmodel endpoint.

    Pins two things at once: the request lands at ``{base}/v1/messages`` with the
    anthropic auth headers, and the parser drops the ``thinking`` block while
    returning only the ``text`` content plus the real usage/stop_reason.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        # httpx appends to the base path: /v1/messages keeps /api/anthropic.
        assert str(req.url) == _GLM_BASE + "/v1/messages"
        assert req.url.path.endswith("/v1/messages")
        assert req.headers["x-api-key"] == "glm-key"
        assert req.headers["anthropic-version"]  # anthropic-compatible auth
        return httpx.Response(
            200,
            json={
                "id": "msg_01abc",
                "type": "message",
                "role": "assistant",
                "model": "glm-4.6",
                "content": [
                    {"type": "thinking", "thinking": "user says ping", "signature": "sig"},
                    {"type": "text", "text": "pong"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 17, "output_tokens": 25, "cache_read_input_tokens": 0},
            },
        )

    g = GatewayAdapter(
        _GLM_BASE, "glm-key", "anthropic", clock=FakeClock(), client=_client(handler, _GLM_BASE)
    )
    r = await g.complete([Msg("user", "ping")], model="glm-4.6")
    assert r.text == "pong"  # the thinking block contributed nothing
    assert r.tool_calls == []
    assert r.input_tokens == 17
    assert r.output_tokens == 25
    assert r.stop_reason == "end_turn"


async def test_anthropic_real_glm_thinking_then_tool_use():
    """GLM-4.6 reply whose content is a thinking block followed by a tool_use
    block — the thinking is ignored, the tool_use becomes a ToolCall."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == _GLM_BASE + "/v1/messages"
        return httpx.Response(
            200,
            json={
                "id": "msg_02def",
                "type": "message",
                "role": "assistant",
                "model": "glm-4.6",
                "content": [
                    {"type": "thinking", "thinking": "I should list files", "signature": "s2"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01xyz",
                        "name": "execute",
                        "input": {"cmd": "ls -la"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 31, "output_tokens": 12},
            },
        )

    g = GatewayAdapter(
        _GLM_BASE, "glm-key", "anthropic", clock=FakeClock(), client=_client(handler, _GLM_BASE)
    )
    r = await g.complete(
        [Msg("user", "list the files")],
        model="glm-4.6",
        tools=[ToolDef("execute", "run a shell command", {"type": "object"})],
    )
    assert r.text == ""  # only a thinking + tool_use block; no text content
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "execute"
    assert r.tool_calls[0].arguments == {"cmd": "ls -la"}
    assert r.tool_calls[0].id == "toolu_01xyz"
    assert r.stop_reason == "tool_use"
    assert (r.input_tokens, r.output_tokens) == (31, 12)


async def test_openai_response_mapping():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer key123"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "hi",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "execute", "arguments": '{"cmd": "ls"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    g = GatewayAdapter(
        "https://gw.test", "key123", "openai", clock=FakeClock(), client=_client(handler)
    )
    r = await g.complete(
        [Msg("user", "u")], model="m", tools=[ToolDef("execute", "run a cmd", {"type": "object"})]
    )
    assert r.text == "hi"
    assert r.tool_calls[0].arguments == {"cmd": "ls"}  # JSON-string args decoded
    assert (r.input_tokens, r.output_tokens) == (7, 3)


async def test_retries_429_then_succeeds_via_clock():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    clock = FakeClock()
    g = GatewayAdapter("https://gw.test", "k", "anthropic", clock=clock, client=_client(handler))
    r = await g.complete([Msg("user", "u")], model="m")
    assert r.text == "ok"
    assert calls["n"] == 2
    assert clock.slept == [3.0]  # honored the Retry-After, via the clock (no real wait)


async def test_hard_4xx_raises_runtimeerror():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    g = GatewayAdapter("https://gw.test", "k", "openai", clock=FakeClock(), client=_client(handler))
    with pytest.raises(RuntimeError):
        await g.complete([Msg("user", "u")], model="m")


def test_empty_key_raises_at_construction():
    with pytest.raises(RuntimeError):
        GatewayAdapter("https://gw.test", "", "anthropic", clock=FakeClock())
