"""Unit: consume the platform's top-level task fields without losing questions."""
from __future__ import annotations

import json

import pytest

from agent.briefing import load_task
from agent.config import Settings
from agent.domain.state import RunMode


def test_standard_top_level_questions_reach_challenge(tmp_path):
    briefing = {
        "schema_version": 2,
        "run_mode": "instance",
        "challenge": {
            "id": "CTFARENA-001",
            "name": "DocuVault",
            "description": "Find the secret",
            "questions": [{"id": "stale"}],
            "gated_questions": 9,
        },
        "questions": [{"id": "flag", "prompt": "The vault master secret"}],
        "gated_questions": 2,
    }
    (tmp_path / "task.json").write_text(json.dumps(briefing), encoding="utf-8")

    task = load_task(Settings(work_dir=str(tmp_path)))

    assert task.run_mode is RunMode.INSTANCE
    assert task.challenge is not None
    assert task.challenge.id == "CTFARENA-001"
    assert task.challenge.name == "DocuVault"
    assert task.challenge.description == "Find the secret"
    assert [q.id for q in task.challenge.unanswered()] == ["flag"]
    assert task.challenge.questions[0].prompt == "The vault master secret"
    assert task.challenge.gated_questions == 2


def test_explicit_empty_top_level_questions_do_not_inherit_nested_values(tmp_path):
    briefing = {
        "challenge": {
            "id": "c1",
            "questions": [{"id": "stale", "prompt": "Not in the task question set"}],
            "gated_questions": 3,
        },
        "questions": [],
        "gated_questions": 0,
    }
    (tmp_path / "task.json").write_text(json.dumps(briefing), encoding="utf-8")

    task = load_task(Settings(work_dir=str(tmp_path)))

    assert task.challenge is not None
    assert task.challenge.unanswered() == []
    assert task.challenge.gated_questions == 0


def test_answered_state_survives_loading(tmp_path):
    questions = [
        {
            "id": "answered",
            "answered": True,
            "choices": ["A", "B"],
            "format": "single",
        },
        {"id": "open", "answered": False},
        {"id": "default"},
        {"id": "locked", "locked": True, "requires": ["open"]},
    ]
    briefing = {"challenge": {"id": "c1"}, "questions": questions}
    (tmp_path / "task.json").write_text(json.dumps(briefing), encoding="utf-8")

    task = load_task(Settings(work_dir=str(tmp_path)))

    assert task.challenge is not None
    assert [q.answered for q in task.challenge.questions] == [True, False, False, False]
    assert [q.id for q in task.challenge.unanswered()] == ["open", "default"]
    assert task.challenge.questions[0].choices == ["A", "B"]
    assert task.challenge.questions[0].fmt == "single"
    assert task.challenge.questions[3].requires == ["open"]


@pytest.mark.parametrize(
    ("briefing", "expected"),
    [({"max_turns": None}, None), ({}, 7), ({"max_turns": 4}, 4)],
)
def test_max_turns_distinguishes_null_from_missing(tmp_path, monkeypatch, briefing, expected):
    monkeypatch.setattr(
        "agent.config.os.environ",
        {"CTFY_WORK_DIR": str(tmp_path), "CTFY_EVAL_MAX_TURNS": "7"},
    )
    (tmp_path / "task.json").write_text(json.dumps(briefing), encoding="utf-8")
    settings = Settings.from_env()
    assert settings.max_turns == 7

    task = load_task(settings)

    assert task.max_turns == expected
