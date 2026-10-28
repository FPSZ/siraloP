"""Category routing, literal plan/QA parsing, bounded evidence memory and seeds."""
from __future__ import annotations

from agent.domain import prompts
from agent.domain.solver import (
    ReActSolver,
    _answer_from_text,
    _extract_plan,
    _merge_plan,
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


def test_plan_label_normalization_preserves_literal_data():
    literal = "`__key**__` $() 'quoted' **suffix**"
    assert (
        _strip_plan_markers(f"**EVIDENCE:** source => {literal}")
        == f"EVIDENCE: source => {literal}"
    )
    assert _strip_plan_markers(f"HYPOTHESIS: {literal}") == f"HYPOTHESIS: {literal}"
    assert _extract_plan(f"EVIDENCE: file.txt offset 42 => {literal}") == [
        f"EVIDENCE: file.txt offset 42 => {literal}",
    ]


def test_partial_plan_fields_are_merged_without_losing_evidence():
    current = _extract_plan(
        "HYPOTHESIS: /api key\nRULED OUT: /admin denied\n"
        "EVIDENCE: read config.ini => __secret**__\n[ ] inspect API"
    )
    merged = _merge_plan(current, _extract_plan("[x] inspected API\n[ ] verify result"))
    assert merged == [
        "HYPOTHESIS: /api key",
        "RULED OUT: /admin denied",
        "EVIDENCE: read config.ini => __secret**__",
        "[x] inspected API", "[ ] verify result",
    ]
    assert _merge_plan(merged, _extract_plan("HYPOTHESIS: key verified")) == [
        "HYPOTHESIS: key verified", *merged[1:],
    ]


def test_evidence_notes_are_bounded_and_oversized_literals_are_not_rewritten():
    plan = []
    for i in range(30):
        plan = _merge_plan(plan, _extract_plan(f"EVIDENCE: source-{i} => __literal**{i}"))
    assert len(plan) == 6
    assert plan[-1] == "EVIDENCE: source-29 => __literal**29"
    huge = "EVIDENCE: file.txt => " + "x" * 2000
    assert _merge_plan(plan, _extract_plan(huge)) == plan
    assert not any("clipped" in line for line in plan)


# -- QA answer extraction (bug 3: plan submitted as the answer) ---------------
def test_answer_from_text_strips_replan():
    assert _answer_from_text("HYPOTHESIS: foo\n[ ] bar\n51") == "51"
    assert _answer_from_text("EVIDENCE: config.ini => __key**__\nA") == "A"


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
