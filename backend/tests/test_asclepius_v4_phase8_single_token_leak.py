"""Asclepius V4 Build Spec — Phase 8: single-token leak check (M1).

`assert_no_answer_leakage` skipped any sealed leaf with fewer than 2 distinctive
tokens, so a sealed diagnosis of "PGNMID" or "MGRS" — the decisive acronyms most
damaging to leak — was never checked. Now a distinctive lone token is flagged when it
appears as a WHOLE token in the model-visible case, while ordinary clinical vocabulary
("anemia") stays safe. Spec Phase 8 tests 33-34 / Audit §M1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import ingestion as I  # noqa: E402


def _case(text: str) -> dict:
    return {"notes": [{"text": text, "model_visible": True}]}


# ── test 33 ───────────────────────────────────────────────────────────────────
def test_single_token_answer_leak_detected():
    """A sealed one-token answer (PGNMID) reproduced whole in the visible case is a
    hard AnswerLeakageError — the >=2-token rule used to skip it silently."""
    sealed = {"answer_key": {"diagnosis": "PGNMID"}}
    with pytest.raises(I.AnswerLeakageError):
        I.assert_no_answer_leakage(
            _case("Renal biopsy consistent with PGNMID; monotypic IgG3-kappa deposits."),
            sealed)
    # MGRS too — the other decisive acronym.
    with pytest.raises(I.AnswerLeakageError):
        I.assert_no_answer_leakage(
            _case("Findings place this on the MGRS spectrum."),
            {"answer_key": {"dx": "MGRS"}})


# ── test 34 ───────────────────────────────────────────────────────────────────
def test_common_single_token_no_false_positive():
    """An ordinary clinical single token (anemia) appearing in the visible case is NOT
    a leak — flagging it would quarantine real hospital data."""
    I.assert_no_answer_leakage(
        _case("Problem list: chronic kidney disease, anemia, hypertension."),
        {"answer_key": {"dx": "anemia"}})


def test_single_token_substring_does_not_fire():
    """Whole-token match only: a distinctive token must appear as its own word, never
    as a substring of a longer word (this path raises a hard error)."""
    # 'mgrs' as a substring inside a longer token must NOT fire.
    I.assert_no_answer_leakage(
        _case("The immunogramsystem printout was reviewed."),  # contains 'mgrs'? no — guard anyway
        {"answer_key": {"dx": "PGNMID"}})
    # A concrete substring case: sealed 'pgnmid' vs a made-up longer word containing it.
    I.assert_no_answer_leakage(
        _case("The pgnmidazole assay was negative."),  # 'pgnmid' is a substring, not a token
        {"answer_key": {"dx": "PGNMID"}})


def test_short_distinctive_token_not_checked():
    """A very short token (< 4 chars) is not checked as a single-token leak — too
    collision-prone to raise a hard error on. 'IgA' → 'iga' (len 3) is skipped."""
    I.assert_no_answer_leakage(
        _case("IgA deposits are present on immunofluorescence."),
        {"answer_key": {"dx": "IgA"}})
