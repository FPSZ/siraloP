"""Pytest fixtures: fresh fakes per test (asyncio_mode=auto, so async tests need
no decorator). Nothing here touches the network, the disk, or a real clock.
"""
from __future__ import annotations

import pytest

from agent.testing.fakes import (
    FakeClock,
    FakeExecution,
    FakeLLM,
    FakePlatform,
    RecordingResultSink,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def execution() -> FakeExecution:
    return FakeExecution()


@pytest.fixture
def sink() -> RecordingResultSink:
    return RecordingResultSink()
