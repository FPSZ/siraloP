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
import tempfile

from agent.adapters._redaction import RedactionError, redact_data
from agent.domain.state import RunResult


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
                payload = self._render(result)
            except RedactionError:
                # A collision cannot silently discard answers or retain an
                # earlier successful result. Persist an unambiguous failure.
                payload = self._render(RunResult(stop_reason="redaction_error: ambiguous_keys"))
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
        data = {key: redact_data(value) for key, value in dataclasses.asdict(result).items()}
        return json.dumps(data, ensure_ascii=False, indent=2)

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
