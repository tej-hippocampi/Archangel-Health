"""Asclepius V4 Build Spec — Phase 7: `blinded` nullable (H2).

The migration backfilled `blinded DEFAULT 1` and `_blinded_only` defaulted a missing
flag to True — so legacy rows were ASSERTED blinded and silently entered κ. κ is the
number a buyer audits; a metric that absorbs unverifiable observations is worth less
than one reporting a smaller honest n. Now the column is nullable with no default, and
only observations EXPLICITLY blinded enter κ; NULL rows are excluded and counted apart
from explicitly-unblinded ones. Spec Phase 7 tests 31-32 / Audit §H2.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.agreement import _blinded_only, aggregate_kappa  # noqa: E402


# ── test 31 ───────────────────────────────────────────────────────────────────
def test_null_blinded_excluded_from_kappa():
    """A row with `blinded IS NULL` is excluded from κ and counted in
    `excluded_unverified` — never assumed blinded."""
    observations = [
        {"specialty": "nephrology", "verdict_a": "A_better", "verdict_b": "A_better", "blinded": 1},
        {"specialty": "nephrology", "verdict_a": "B_better", "verdict_b": "B_better", "blinded": 1},
        # Legacy rows: blinding never verified → NULL. Must NOT enter κ.
        {"specialty": "nephrology", "verdict_a": "A_better", "verdict_b": "B_better", "blinded": None},
        {"specialty": "nephrology", "verdict_a": "A_better", "verdict_b": "B_better"},  # missing key
    ]
    agg = aggregate_kappa(observations, min_n=1)
    assert agg["n"] == 2, "only the two explicitly-blinded rows enter κ"
    assert agg["excluded_unverified"] == 2
    assert agg["excluded_unblinded"] == 0
    # Observed agreement is computed over the blinded pairs only (both agree → 1.0).
    assert agg["observed_agreement"] == 1.0


def test_explicit_unblinded_counted_separately_from_null():
    """An explicit `blinded=0` is a different exclusion (measured anchoring) from a
    NULL (unverifiable) — the two are reported apart."""
    observations = [
        {"verdict_a": "A_better", "verdict_b": "A_better", "blinded": 1},
        {"verdict_a": "A_better", "verdict_b": "B_better", "blinded": 0},     # explicit unblinded
        {"verdict_a": "A_better", "verdict_b": "B_better", "blinded": None},  # unverifiable
    ]
    agg = aggregate_kappa(observations, min_n=1)
    assert agg["n"] == 1
    assert agg["excluded_unblinded"] == 1
    assert agg["excluded_unverified"] == 1


# ── test 32 ───────────────────────────────────────────────────────────────────
def test_explicit_blinded_included():
    """`blinded=1` (and the Python bool True) still enter κ."""
    kept = _blinded_only([
        {"blinded": 1}, {"blinded": True}, {"blinded": 0},
        {"blinded": False}, {"blinded": None}, {},
    ])
    assert kept == [{"blinded": 1}, {"blinded": True}]

    observations = [
        {"specialty": "cards", "verdict_a": "A_better", "verdict_b": "A_better", "blinded": 1},
        {"specialty": "cards", "verdict_a": "B_better", "verdict_b": "B_better", "blinded": True},
    ]
    agg = aggregate_kappa(observations, min_n=1)
    assert agg["n"] == 2
    assert agg["excluded_unverified"] == 0 and agg["excluded_unblinded"] == 0
