"""CtfyResultSink — write result.json for the run (CTFY-0001 §3).

Implements agent.ports.result.ResultSink. This is the ONLY adapter that persists
the final RunResult, and 'not writing the file == failed run', so write() is the
one call that must always succeed-or-stay-silent: it is SYNC (called from a
``finally`` on every exit path, where there may be no event loop), it NEVER
raises (a write failure must not mask the caller's original error), and it writes
ATOMICALLY (temp file + os.replace) so a mid-write kill cannot leave truncated
JSON. Before writing it SCRUBS credentials so no secret can leak into the file.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from enum import Enum
from typing import Any

from agent.domain.state import RunResult

_MASK = "***"

# Credentials never belong in result.json. These env vars are the only source of
# secrets (see agent/config.py); we redact their literal values wherever they
# appear in the serialized text, plus any ``Bearer <token>`` authorization value.
_SECRET_ENV_VARS = ("LLM_API_KEY", "CTFY_TOKEN")

# Real tokens for these services are long; a floor keeps a blank/placeholder env
# value from colliding with ordinary JSON text (e.g. "true") and corrupting the
# output, while still catching every genuine credential.
_MIN_SECRET_LEN = 8

# A bearer token: the scheme word, then the opaque credential (JWT/base64url/etc.).
# The scheme's original casing is preserved; the credential is replaced wholesale.
_BEARER_RE = re.compile(r"(?i)(Bearer)\s+[A-Za-z0-9\-._~+/=]+")


def redact(text: str) -> str:
    """Return ``text`` with known credentials replaced by ``***``.

    Redacts the current values of the secret env vars (LLM_API_KEY, CTFY_TOKEN)
    and any ``Bearer <token>`` authorization value. Pure string transform over
    the already-serialized JSON, so it is trivial to unit-test in isolation.
    """
    out = text
    for name in _SECRET_ENV_VARS:
        secret = os.environ.get(name) or ""
        if len(secret) >= _MIN_SECRET_LEN and not secret.isspace():
            out = out.replace(secret, _MASK)
    out = _BEARER_RE.sub(r"\1 " + _MASK, out)
    return out


def _coerce(obj: Any) -> Any:
    """Recursively convert Enum members to their ``.value`` so JSON gets plain
    scalars regardless of how an Enum is declared (``str, Enum`` or otherwise)."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {_coerce(k): _coerce(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce(v) for v in obj]
    return obj


def _json_default(obj: Any) -> Any:
    """Last-resort encoder for any leaf JSON cannot handle; keeps write() from
    failing on an unexpected field type (best-effort: 'always write something')."""
    if isinstance(obj, Enum):
        return obj.value
    return str(obj)


class CtfyResultSink:
    """Write the RunResult to ``path`` as atomic, secret-scrubbed result.json.

    ``path`` is the full destination file (e.g. ``$CTFY_WORK_DIR/result.json``);
    the composition root supplies it. When ``stdout_fallback`` is True and the
    file cannot be written, the JSON is printed to stdout as a last resort.
    """

    def __init__(self, path: str, *, stdout_fallback: bool = False) -> None:
        self._path = path
        self._stdout_fallback = stdout_fallback

    def write(self, result: RunResult) -> None:
        # The whole body is guarded: write() is called from a ``finally`` on every
        # exit path and MUST NEVER raise, or it would mask the caller's error.
        try:
            try:
                payload = redact(self._render(result))
            except Exception:
                # Could not even serialize the result — nothing safe to write.
                return
            try:
                self._atomic_write(payload)
            except Exception:
                # File write failed (read-only FS, perms, disk full, mid-write
                # kill...). Fall back to stdout only if asked.
                if self._stdout_fallback:
                    try:
                        print(payload, flush=True)
                    except Exception:
                        pass
        except Exception:
            # Absolute last-resort guard. Stay silent; never propagate.
            pass

    def _render(self, result: RunResult) -> str:
        data = _coerce(dataclasses.asdict(result))
        return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)

    def _atomic_write(self, payload: str) -> None:
        dirname = os.path.dirname(self._path) or "."
        os.makedirs(dirname, exist_ok=True)
        # Temp file in the SAME directory so os.replace() is an atomic rename on
        # the same filesystem (works on both Linux and Windows).
        fd, tmp = tempfile.mkstemp(prefix=".result-", suffix=".tmp", dir=dirname)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # fsync unsupported here; replace is still atomic
            os.replace(tmp, self._path)
        except BaseException:
            # Do not leave a stray temp file behind, then let the caller decide.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
