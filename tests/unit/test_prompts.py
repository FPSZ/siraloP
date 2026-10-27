"""prompts.py and the solver's plan/answer helpers were untested. Cover category
normalization/routing (CTFY-0002 §2.1), the plan parser's tolerance of the format
the prompt shows, QA answer extraction, per-category seed plans, and the scaffold's
key operational claims.
"""
from __future__ import annotations

from agent.domain import prompts
from agent.domain.solver import (
    ReActSolver,
    _answer_from_text,
    _extract_plan,
    _strip_plan_markers,
)
from agent.domain.state import Challenge


# -- category routing (CTFY-0002 §2.1: web|pwn|reverse|crypto|misc) -----------
def test_normalize_category_enum_and_aliases():
    assert prompts.normalize_category("reverse") == "reverse"
    assert prompts.normalize_category("Reverse") == "reverse"
    assert prompts.normalize_category("rev") == "reverse"
    assert prompts.normalize_category("binary") == "pwn"
    assert prompts.normalize_category("forensics") == "misc"
    assert prompts.normalize_category("Web Exploitation") == "web"
    assert prompts.normalize_category("crypto (RSA)") == "crypto"
    assert prompts.normalize_category("") == ""
    assert prompts.normalize_category("totally-unknown") == ""


def test_reverse_is_not_routed_to_pwn():
    rev = prompts.category_guidance("reverse")
    pwn = prompts.category_guidance("pwn")
    assert rev and pwn and rev != pwn
    assert "reverse" in rev.lower()


def test_crypto_and_misc_have_guidance():
    assert prompts.category_guidance("crypto").strip()
    assert prompts.category_guidance("misc").strip()
    # forensics routes to the misc block, not to nothing
    assert prompts.category_guidance("forensics") == prompts.category_guidance("misc")


def test_system_prompt_block_and_operational_claims():
    sp = prompts.system_prompt("reverse")
    assert "Category: reverse" in sp
    assert "/opt/ctf-tools.txt" in sp            # pointer, not a drifting hard-coded list
    assert "OFFLINE" in sp
    low = sp.lower()
    assert "background" in low and "time limit" in low   # timeout guidance
    assert "question_id" in sp                   # QA answer routing
    assert "bare value" in low                   # not every answer is word{...}


# -- plan parser tolerance (bug 1: format shown can't be parsed) --------------
def test_strip_plan_markers_peels_decoration():
    assert _strip_plan_markers("- `HYPOTHESIS: foo`") == "HYPOTHESIS: foo"
    assert _strip_plan_markers("1. [ ] do x") == "[ ] do x"
    assert _strip_plan_markers("**RULED OUT:** none") == "RULED OUT: none"
    assert _strip_plan_markers("* [x] done") == "[x] done"


def test_extract_plan_parses_the_format_it_shows():
    text = (
        "- `HYPOTHESIS: SQLi in /login`\n"
        "- `RULED OUT: SSTI`\n"
        "- `[ ] test ' payload`\n"
        "some prose that is not a plan line"
    )
    assert _extract_plan(text) == [
        "HYPOTHESIS: SQLi in /login",
        "RULED OUT: SSTI",
        "[ ] test ' payload",
    ]


def test_extract_plan_also_accepts_plain_lines():
    assert _extract_plan("HYPOTHESIS: x\n[ ] y\n[x] z") == [
        "HYPOTHESIS: x", "[ ] y", "[x] z",
    ]


# -- QA answer extraction (bug 3: plan submitted as the answer) ---------------
def test_answer_from_text_strips_replan():
    assert _answer_from_text("HYPOTHESIS: foo\n[ ] bar\n51") == "51"


def test_answer_from_text_refuses_plan_only():
    assert _answer_from_text("HYPOTHESIS: foo\n[ ] bar\nRULED OUT: baz") == ""


def test_answer_from_text_refuses_ambiguous_multiline():
    assert _answer_from_text("line one\nline two") == ""


# -- per-category seed plans (bug 2: only web was seeded) ---------------------
def test_seed_plan_is_category_specific():
    cases = {
        "web": "robots",
        "pwn": "checksec",
        "reverse": "decompile",
        "crypto": "pycryptodome",
        "misc": "binwalk",
    }
    for cat, needle in cases.items():
        seed = " ".join(ReActSolver._seed_plan(Challenge(id="X", category=cat))).lower()
        assert needle in seed, (cat, needle)
