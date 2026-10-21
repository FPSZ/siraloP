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


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://gw.test", transport=httpx.MockTransport(handler))


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
