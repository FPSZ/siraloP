"""Credential scrubbing before observation encoding or truncation.

Never apply these transforms to platform submissions or execution inputs.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from enum import Enum
from functools import lru_cache
from typing import Any

_SECRET_ENV_VARS = (
    "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
)
_MASK = "***"


class RedactionError(ValueError):
    """An observation cannot be scrubbed without losing its meaning."""


def _secrets(extra_secrets: tuple[str, ...]) -> tuple[str, ...]:
    values = (*extra_secrets, *(os.environ.get(name, "") for name in _SECRET_ENV_VARS))
    return tuple(sorted({s for s in values if s and not s.isspace()}, key=len, reverse=True))


@lru_cache(maxsize=1024)
def _character_pattern(char: str, *, include_raw: bool = True) -> tuple[str, int]:
    """Match one decoded character in raw, JSON or repr representation.

    Concatenating these units recognizes arbitrary mixed encodings without
    enumerating exponentially many whole-secret spellings. Case insensitivity
    applies only to hexadecimal escape digits, never to the credential itself.
    """
    variants = {re.escape(char)} if include_raw else set()
    for encoded in (json.dumps(char, ensure_ascii=False)[1:-1], repr(char)[1:-1]):
        if encoded != char:
            variants.add(re.escape(encoded))
    code = ord(char)
    if code <= 0xFFFF:
        encoded = "\\u" + f"{code:04x}"
        variants.add("(?i:" + re.escape(encoded) + ")")
        span = 6
    else:
        code -= 0x10000
        encoded = "\\u" + f"{0xD800 + (code >> 10):04x}" + "\\u" + f"{0xDC00 + (code & 0x3FF):04x}"
        variants.add("(?i:" + re.escape(encoded) + ")")
        span = 12
    # JSON optionally escapes a slash; repr can escape either quote character.
    if char in "/\"'":
        variants.add(re.escape("\\" + char))
    return "(?:" + "|".join(sorted(variants, key=len, reverse=True)) + ")", span


@lru_cache(maxsize=64)
def _compiled_secret_matcher(
    secrets: tuple[str, ...],
) -> tuple[re.Pattern[str] | None, int]:
    patterns: list[str] = []
    max_span = 0
    for secret in secrets:
        # A JSON backslash is encoded as two slashes or a Unicode escape. Do not
        # mix its one-character raw form into that grammar: overlapping one/two
        # slash choices cause exponential backtracking. Plaintext is a separate,
        # fixed-length alternative and still covers every original credential.
        units = [_character_pattern(char, include_raw=char != "\\") for char in secret]
        patterns.extend((re.escape(secret), "".join(pattern for pattern, _ in units)))
        max_span = max(max_span, sum(span for _, span in units))
    return (re.compile("|".join(patterns)) if patterns else None), max_span


def _secret_matcher(
    extra_secrets: tuple[str, ...] = (),
) -> tuple[re.Pattern[str] | None, int]:
    """Match original credentials or legal mixed JSON escapes, with bounded span.

    Cache by current credential values, not environment variable names, so key
    rotation and independent adapters do not accidentally reuse stale matches.
    """
    return _compiled_secret_matcher(_secrets(extra_secrets))


@lru_cache(maxsize=1)
def _bearer_matcher() -> re.Pattern[str]:
    scheme = "".join(
        "(?:" + _character_pattern(char.lower())[0]
        + "|" + _character_pattern(char.upper())[0] + ")"
        for char in "Bearer"
    )
    spaces = "(?:\\s|" + "|".join(
        _character_pattern(chr(code), include_raw=False)[0]
        for code in (*range(9, 14), *range(28, 33), 133, 160, 5760,
                     *range(8192, 8203), 8232, 8233, 8239, 8287, 12288)
    ) + ")"
    token = "(?:" + "|".join(
        _character_pattern(char)[0]
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~+/="
    ) + ")"
    return re.compile(r"(?<![A-Za-z0-9_])(" + scheme + ")" + spaces + "+" + token + "+")


_BEARER_RE = _bearer_matcher()


def redact(
    text: str, *, extra_secrets: tuple[str, ...] = (), redact_bearer: bool = True,
) -> str:
    """Scrub known credentials, including arbitrarily mixed JSON escapes."""
    matcher, _ = _secret_matcher(extra_secrets)
    if matcher is not None:
        text = matcher.sub(lambda _: _MASK, text)
    if redact_bearer:
        text = _BEARER_RE.sub(lambda match: match[1] + " " + _MASK, text)
    return text


def redact_data(
    value: Any, *, extra_secrets: tuple[str, ...] = (), redact_bearer: bool = True,
) -> Any:
    """Return scrubbed JSON-compatible data; reject ambiguous dynamic keys."""
    def clean(item: Any) -> Any:
        if isinstance(item, Enum):
            return clean(item.value)
        if isinstance(item, str):
            return redact(item, extra_secrets=extra_secrets, redact_bearer=redact_bearer)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                safe_key = redact(str(key.value if isinstance(key, Enum) else key),
                                  extra_secrets=extra_secrets, redact_bearer=redact_bearer)
                if safe_key in result:
                    raise RedactionError("redaction creates ambiguous mapping keys")
                result[safe_key] = clean(child)
            return result
        if isinstance(item, (list, tuple, set, frozenset)):
            return [clean(v) for v in item]
        return redact(str(item), extra_secrets=extra_secrets, redact_bearer=redact_bearer)

    return clean(value)


def contains_secret(value: Any, *, extra_secrets: tuple[str, ...] = ()) -> bool:
    """Inspect structures and embedded mixed-escaped JSON/repr text.

    Errors propagate so an incomplete audit cannot report success.
    """
    matcher, _ = _secret_matcher(extra_secrets)

    def check(item: Any) -> bool:
        if isinstance(item, Enum):
            return check(item.value)
        if isinstance(item, str):
            return bool((matcher is not None and matcher.search(item)) or _BEARER_RE.search(item))
        if isinstance(item, Mapping):
            return any(check(k) or check(v) for k, v in item.items())
        if isinstance(item, (list, tuple, set, frozenset)):
            return any(check(v) for v in item)
        if item is None or isinstance(item, (bool, int, float)):
            return False
        return check(str(item))

    return check(value)
