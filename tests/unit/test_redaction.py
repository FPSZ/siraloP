"""Observation-boundary redaction, independent of platform credentials/network."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from enum import Enum
from pathlib import Path

import pytest

from agent.adapters._redaction import (
    RedactionError,
    _secret_matcher,
    contains_secret,
    redact,
    redact_data,
)

_ENV_NAMES = (
    "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", _ENV_NAMES)
def test_aliases_and_short_secrets(monkeypatch, name):
    monkeypatch.setenv(name, "k")
    assert "k" not in redact("key=k")
    assert contains_secret({"key": "k"})
    assert not contains_secret(redact_data({"key": "k"}))


def test_structures_escaped_keys_unknown_objects_and_enums(monkeypatch):
    secret = 'synthetic-"quoted"\\雪'
    monkeypatch.setenv("LLM_API_KEY", secret)

    class Value(Enum):
        SECRET = secret

    class Odd:
        def __str__(self):
            return "odd=" + secret

    original = {secret: [secret, Odd(), Value.SECRET, {secret: "Bearer unknown.jwt"}]}
    cleaned = redact_data(original)
    encoded = json.dumps(cleaned, ensure_ascii=True)
    assert json.loads(encoded) == {"***": ["***", "odd=***", "***", {"***": "Bearer ***"}]}
    assert contains_secret(original)
    assert contains_secret(json.dumps(original, default=str, ensure_ascii=True))
    assert not contains_secret(encoded)


def test_unknown_challenge_bearer_is_preserved_only_when_requested(monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "control-token-synthetic")
    text = "Bearer challenge.jwt; known=control-token-synthetic"
    assert redact(text, redact_bearer=False) == "Bearer challenge.jwt; known=***"
    assert redact_data({"x": text}, redact_bearer=False)["x"] == "Bearer challenge.jwt; known=***"
    assert contains_secret(text)
    assert not contains_secret(redact(text))
    assert not contains_secret("Bearer ***")


def test_explicit_secrets_and_empty_environment(monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "   ")
    assert redact("normal", extra_secrets=("normal",)) == "***"
    assert contains_secret({"normal": "x"}, extra_secrets=("normal",))
    assert redact("normal") == "normal"


def test_scanner_errors_propagate_instead_of_reporting_clean():
    class Broken:
        def __str__(self):
            raise RuntimeError("synthetic scanner failure")

    with pytest.raises(RuntimeError):
        contains_secret(Broken())


def test_embedded_json_and_repr_log_fragments_are_scrubbed(monkeypatch):
    secret = 'synthetic-"quote"\\雪'
    monkeypatch.setenv("CTFY_TOKEN", secret)
    text = "json-prefix " + json.dumps(secret, ensure_ascii=True) + " repr=" + repr(secret)
    assert contains_secret(text)
    assert not contains_secret(redact(text))


@pytest.mark.parametrize("secret,encoded", [
    ("CONTROL", r"\u0043ONTROL"),
    ("CONTROL", r"C\u004fN\u0054R\u004fL"),
    ("a/b\n雪" + chr(0x20000), r"\u0061\/b\n\u96EA\uD840\uDC00"),
    ('q"\\', r"\u0071\u0022\\"),
])
def test_arbitrary_mixed_json_escape_spellings(monkeypatch, secret, encoded):
    monkeypatch.setenv("CTFY_TOKEN", secret)
    assert json.loads('"' + encoded + '"') == secret
    text = 'raw: {"token":"' + encoded + '"}'
    assert contains_secret(text)
    cleaned = redact(text)
    assert not contains_secret(cleaned)
    assert json.loads(cleaned.removeprefix("raw: ")) == {"token": "***"}
    matcher, max_span = _secret_matcher()
    assert matcher is not None
    assert matcher.fullmatch(encoded)
    assert max_span >= len(encoded)


def test_unknown_bearer_inside_mixed_escaped_json(monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "CONTROL")
    text = r'raw: {"auth":"\u0042earer\u0020a\u0062c.jwt","known":"C\u004fNTROL"}'
    assert contains_secret(text)
    cleaned = redact(text)
    assert json.loads(cleaned.removeprefix("raw: ")) == {"auth": "Bearer ***", "known": "***"}
    retained = redact(text, redact_bearer=False)
    assert json.loads(retained.removeprefix("raw: ")) == {"auth": "Bearer abc.jwt", "known": "***"}
    assert not contains_secret(cleaned)


def test_mapping_collision_is_explicit_and_does_not_echo_keys(monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "CONTROL")
    with pytest.raises(RedactionError) as raised:
        redact_data({"qCONTROL": "first", "q***": "second"})
    assert "CONTROL" not in str(raised.value)


def test_match_span_includes_utf16_pairs_and_short_escape_variants():
    matcher, span = _secret_matcher(extra_secrets=(chr(0x20000) + "\n",))
    encoded = r"\uD840\udc00\u000a"
    assert matcher is not None and matcher.fullmatch(encoded)
    assert span >= len(encoded)


def test_adversarial_near_matches_finish_without_regex_backtracking():
    # External watchdog: a regression must not freeze pytest's own interpreter.
    source = r'''
import json
import os
os.environ["LLM_API_KEY"] = "\\" * 36 + "A"
from agent.adapters._redaction import contains_secret, redact
near_matches = ["Bearer " + " " * 48 + "?", "\\" * 90 + "B"]
observed = [
    redact(value) == value and not contains_secret(value)
    for value in near_matches
]
print(json.dumps({
    "near_matches_preserved": observed,
    "key_masked": redact(os.environ["LLM_API_KEY"]),
}))
'''
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SystemRoot", "WINDIR")
        if name in os.environ
    }
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, "-c", source], env=environment, capture_output=True,
        text=True, timeout=5, check=True,
    )
    assert json.loads(result.stdout) == {
        "near_matches_preserved": [True, True], "key_masked": "***",
    }