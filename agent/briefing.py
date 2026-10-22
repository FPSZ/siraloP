"""Load the task briefing ($CTFY_WORK_DIR/task.json) into a domain Task.

This does file I/O (reads task.json), so it lives here, not in domain/. It
returns a pure ``agent.domain.state.Task``. It is deliberately tolerant: a
missing/!parseable task.json (local dev, or a malformed briefing) yields a
best-effort Task derived from Settings rather than crashing — the run must still
produce a result.json.
"""
from __future__ import annotations

import json
import os
from typing import Any

from agent.config import Settings
from agent.domain.state import Challenge, Question, RunMode, Task


def _question(d: dict[str, Any]) -> Question:
    return Question(
        id=str(d.get("id", "")),
        prompt=str(d.get("prompt", "")),
        requires=list(d.get("requires", []) or []),
        answered=bool(d.get("answered", False)),
        locked=bool(d.get("locked", False)),
        choices=list(d.get("choices", []) or []),
        fmt=str(d.get("format", d.get("fmt", ""))),
    )


def _challenge(d: dict[str, Any]) -> Challenge:
    return Challenge(
        id=str(d.get("id", "")),
        name=str(d.get("name", "")),
        category=str(d.get("category", "")),
        description=str(d.get("description", "")),
        difficulty=str(d.get("difficulty", "")),
    )


def load_task(settings: Settings) -> Task:
    """Parse $CTFY_WORK_DIR/task.json; fall back to Settings when absent."""
    path = os.path.join(settings.work_dir, "task.json")
    raw: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, json.JSONDecodeError):
        raw = {}

    result = raw.get("result", {}) if isinstance(raw.get("result"), dict) else {}
    chal = raw.get("challenge")
    challenge = _challenge(chal) if isinstance(chal, dict) else None
    if challenge is None and settings.challenge_id:
        challenge = Challenge(id=settings.challenge_id)

    if challenge is not None:
        challenge.questions = [_question(q) for q in (raw.get("questions") or [])]
        challenge.gated_questions = int(raw.get("gated_questions", 0) or 0)

    # max_turns: task.json wins, else env; null means "no budget" (never 0).
    mt = raw.get("max_turns", settings.max_turns)
    max_turns = mt if mt is None or isinstance(mt, int) else settings.max_turns

    return Task(
        run_mode=RunMode.parse(raw.get("run_mode")),
        schema_version=int(raw.get("schema_version", 2) or 2),
        challenge=challenge,
        competition_id=str(raw.get("competition_id", settings.competition_id) or ""),
        max_turns=max_turns,
        result_path=str(result.get("path", settings.default_result_path)),
        stdout_fallback=bool(result.get("stdout_fallback", False)),
        dry_run=bool(raw.get("dry_run", False)) or settings.dry_run,
        target_instance=raw.get("target_instance"),
        scope=list(raw.get("scope", []) or []),
        limits=raw.get("limits", {}) if isinstance(raw.get("limits"), dict) else {},
        raw=raw,
    )
