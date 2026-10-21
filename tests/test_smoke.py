"""Smoke tests: the skeleton imports and the pure domain pieces work."""
from __future__ import annotations

from agent.domain.bus import MessageBus
from agent.domain.policy import looks_like_flag
from agent.domain.state import Budget, Finding, RunResult


def test_imports():
    import agent  # noqa: F401
    import agent.entrypoints.image_main  # noqa: F401
    import agent.wiring  # noqa: F401


def test_flag_sanity():
    assert looks_like_flag("ctfy{abc_123}")
    assert not looks_like_flag("not a flag")


def test_budget_exhaustion():
    assert Budget(max_turns=3, used_turns=3).exhausted()
    assert not Budget(max_turns=3, used_turns=1).exhausted()


def test_bus_dedup():
    bus = MessageBus()
    f = Finding(source="s", category="recon", content="x")
    assert bus.publish("c1", f) is True
    assert bus.publish("c1", f) is False  # dedup
    assert len(bus.findings("c1")) == 1


def test_result_defaults():
    r = RunResult()
    assert r.schema_version == 2
