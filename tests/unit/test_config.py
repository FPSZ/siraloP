"""Settings.from_env resolves the model credential/endpoint/model from the
generic LLM_* names AND the provider SDK's own names (CTFY-0001 §1.1): the
platform may publish them only under ANTHROPIC_*/OPENAI_*, with LLM_* empty.
"""
from __future__ import annotations

import pytest

from agent.config import Settings

_LLM_NAMES = (
    "LLM_FLAVOR", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _LLM_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_generic_llm_vars_win(clean_env):
    for k, v in {
        "LLM_FLAVOR": "anthropic", "LLM_API_KEY": "generic-key",
        "LLM_BASE_URL": "https://gw/generic", "LLM_MODEL": "m-generic",
        # SDK-native present too, but the explicit generic values take precedence
        "ANTHROPIC_AUTH_TOKEN": "sdk-key", "ANTHROPIC_BASE_URL": "https://gw/sdk",
    }.items():
        clean_env.setenv(k, v)
    s = Settings.from_env()
    assert (s.llm_flavor, s.llm_api_key, s.llm_base_url, s.llm_model) == (
        "anthropic", "generic-key", "https://gw/generic", "m-generic",
    )


def test_anthropic_sdk_vars_fill_empty_generic(clean_env):
    # The platform funded this run only via the SDK-native names; LLM_* are absent.
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-token")
    clean_env.setenv("ANTHROPIC_API_KEY", "x-api-key-val")
    clean_env.setenv("ANTHROPIC_BASE_URL", "https://gateway.internal")
    clean_env.setenv("ANTHROPIC_MODEL", "claude-x")
    s = Settings.from_env()
    assert s.llm_flavor == "anthropic"           # inferred from the family present
    assert s.llm_api_key == "bearer-token"       # bearer preferred over x-api-key
    assert s.llm_base_url == "https://gateway.internal"
    assert s.llm_model == "claude-x"


def test_anthropic_falls_back_to_x_api_key_when_no_bearer(clean_env):
    clean_env.setenv("LLM_FLAVOR", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "only-x-api-key")
    s = Settings.from_env()
    assert s.llm_api_key == "only-x-api-key"


def test_openai_sdk_vars_fill_empty_generic(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-openai")
    clean_env.setenv("OPENAI_BASE_URL", "https://oai.internal")
    clean_env.setenv("OPENAI_MODEL", "gpt-x")
    s = Settings.from_env()
    assert (s.llm_flavor, s.llm_api_key, s.llm_base_url, s.llm_model) == (
        "openai", "sk-openai", "https://oai.internal", "gpt-x",
    )


def test_unfunded_run_resolves_to_empty_key(clean_env):
    # Nothing set anywhere: the key stays "" so the gateway fails loudly later
    # instead of spending another account (CTFY-0001 §1.1).
    s = Settings.from_env()
    assert s.llm_api_key == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", None), ("0", 0.0), ("0.25", 0.25), ("-1", None), ("nan", None), ("inf", None)],
)
def test_application_wall_limit_requires_finite_nonnegative_seconds(
    clean_env, value, expected
):
    clean_env.setenv("CTFY_AGENT_MAX_WALL_S", value)

    assert Settings.from_env().max_wall_s == expected
