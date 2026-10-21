"""ResultSink — write the run result (CTFY-0001 §3).

FROZEN CONTRACT — see CONTRIBUTING.md before changing a signature.

Implementations: adapters/result/ctfy_result.py
"""
from __future__ import annotations

from typing import Protocol

from agent.domain.state import RunResult


class ResultSink(Protocol):
    def write(self, result: RunResult) -> None:
        """Write result.json to the result path (CTFY-0001 §3).

        INTENTIONALLY SYNC (do not make it async): it is called from a ``finally``
        on every exit path, including error/cancellation unwinding where there may
        be no running event loop. It MUST be best-effort and NEVER raise — a write
        failure must not mask the original error — and it MUST write atomically
        (temp file + rename) so a mid-write kill cannot leave truncated JSON.
        'Not writing the file == failed run', so this is the one call that must
        always succeed-or-stay-silent.
        """
        ...
