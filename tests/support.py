"""Shared test helpers (plain functions, not fixtures).

Importable as ``from tests.support import make_challenge`` from any test module.
"""
from __future__ import annotations

from agent.domain.state import Challenge, Question


def make_question(qid: str = "q1", *, prompt: str = "find the flag", **kw) -> Question:
    return Question(id=qid, prompt=prompt, **kw)


def make_challenge(
    cid: str = "c1",
    *,
    category: str = "web",
    difficulty: str = "easy",
    n_questions: int = 1,
    questions: list[Question] | None = None,
) -> Challenge:
    qs = (
        questions
        if questions is not None
        else [make_question(f"q{i + 1}") for i in range(n_questions)]
    )
    return Challenge(
        id=cid,
        name=cid.upper(),
        category=category,
        difficulty=difficulty,
        questions=qs,
    )
