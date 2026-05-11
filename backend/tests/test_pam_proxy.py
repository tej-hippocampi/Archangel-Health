"""
Unit tests for the PAM-style proxy scorer (PRD §4.2).

Covers PRD AC-4.3 (deterministic output for any input set, including
all-1s, all-4s, mixed, with 0/1/2/3 N/As) and §13.7 (boundary handling
at 55.1 and 67.0 cutoffs).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.preop_retier import score_pam  # noqa: E402
from triage.preop_retier.pam_proxy import _level_for  # noqa: E402  (intentional private import for boundary tests)
from triage.preop_retier.types import PamResponse  # noqa: E402


def _responses(values: list) -> list[PamResponse]:
    """Build 13 ordered responses; values may include 'N_A'."""
    assert len(values) == 13
    return [PamResponse(item_index=i + 1, value=v) for i, v in enumerate(values)]


# ─── AC-4.3 — all 1s, all 4s, mixed ──────────────────────────────────────────

def test_all_strongly_disagree_is_low_zero():
    """13× value=1 → activation_score = 0.0, level = LOW."""
    r = score_pam(_responses([1] * 13))
    assert r.is_complete is True
    assert r.items_scored == 13
    assert r.raw_sum == 13
    assert r.raw_average == 1.0
    assert r.activation_score == 0.0
    assert r.level == "LOW"


def test_all_strongly_agree_is_high_one_hundred():
    """13× value=4 → activation_score = 100.0, level = HIGH."""
    r = score_pam(_responses([4] * 13))
    assert r.is_complete is True
    assert r.items_scored == 13
    assert r.raw_sum == 52
    assert r.activation_score == 100.0
    assert r.level == "HIGH"


def test_all_threes_is_moderate():
    """13× value=3 → average 3.0 → score 66.7 → MODERATE (≤ 67.0)."""
    r = score_pam(_responses([3] * 13))
    assert r.activation_score == 66.7
    assert r.level == "MODERATE"


def test_mixed_values_below_low_cutoff():
    """11 items at 2 / 2 N/A → average 2.0 → score 33.3 → LOW."""
    r = score_pam(_responses([2] * 11 + ["N_A", "N_A"]))
    assert r.is_complete is True
    assert r.items_scored == 11
    assert r.raw_average == 2.0
    assert r.activation_score == 33.3
    assert r.level == "LOW"


# ─── AC-4.2 — N/A handling and minimum scored items ──────────────────────────

def test_zero_n_a_is_complete():
    r = score_pam(_responses([3] * 13))
    assert r.is_complete is True
    assert r.items_scored == 13


def test_one_n_a_is_complete():
    r = score_pam(_responses([3] * 12 + ["N_A"]))
    assert r.is_complete is True
    assert r.items_scored == 12


def test_two_n_a_is_complete():
    r = score_pam(_responses([3] * 11 + ["N_A", "N_A"]))
    assert r.is_complete is True
    assert r.items_scored == 11


def test_three_n_a_is_complete():
    r = score_pam(_responses([3] * 10 + ["N_A", "N_A", "N_A"]))
    assert r.is_complete is True
    assert r.items_scored == 10
    assert r.activation_score == 66.7


def test_four_n_a_is_incomplete():
    """9 scored items → AC-4.2 floor not met → is_complete=False."""
    r = score_pam(_responses([3] * 9 + ["N_A", "N_A", "N_A", "N_A"]))
    assert r.is_complete is False
    assert r.items_scored == 9
    assert r.activation_score == 0.0
    assert r.level == "LOW"


# ─── §13.7 — Boundary handling at 55.1 / 67.0 ────────────────────────────────

def test_level_for_exactly_55_1_is_low():
    assert _level_for(55.1) == "LOW"


def test_level_for_just_above_55_1_is_moderate():
    assert _level_for(55.2) == "MODERATE"


def test_level_for_exactly_67_0_is_moderate():
    assert _level_for(67.0) == "MODERATE"


def test_level_for_just_above_67_0_is_high():
    assert _level_for(67.01) == "HIGH"


def test_score_just_below_low_cutoff_is_low():
    """11 items with raw_sum=29 → avg=2.636 → score=54.5 → LOW."""
    vals = [3] * 7 + [2] * 4 + ["N_A", "N_A"]
    r = score_pam(_responses(vals))
    assert r.items_scored == 11
    assert r.raw_sum == 29
    assert r.activation_score == 54.5
    assert r.level == "LOW"


def test_score_just_above_low_cutoff_is_moderate():
    """11 items with raw_sum=30 → avg=2.727 → score=57.6 → MODERATE."""
    vals = [3] * 8 + [2] * 3 + ["N_A", "N_A"]
    r = score_pam(_responses(vals))
    assert r.items_scored == 11
    assert r.raw_sum == 30
    assert r.activation_score == 57.6
    assert r.level == "MODERATE"


def test_score_just_above_high_cutoff_is_high():
    """10 items with raw_sum=31 → avg=3.1 → score=70.0 → HIGH."""
    vals = [3] * 9 + [4] + ["N_A"] * 3
    r = score_pam(_responses(vals))
    assert r.items_scored == 10
    assert r.raw_sum == 31
    assert r.activation_score == 70.0
    assert r.level == "HIGH"


# ─── AC-4.3 — determinism ────────────────────────────────────────────────────

def test_same_input_same_output():
    inp = _responses([3, 4, 2, 3, "N_A", 4, 1, 3, 2, 4, 3, "N_A", 3])
    a = score_pam(inp)
    b = score_pam(inp)
    assert a.model_dump() == b.model_dump()
