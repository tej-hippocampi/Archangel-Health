"""Cohen's kappa + Jaccard inter-annotator agreement (opt §1.3, §4.12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.agreement import aggregate_kappa, cohens_kappa, jaccard  # noqa: E402


def test_kappa_perfect_agreement():
    pairs = [("A_better", "A_better"), ("B_better", "B_better"), ("both_inadequate", "both_inadequate")]
    assert cohens_kappa(pairs) == 1.0


def test_kappa_no_observations_is_none():
    assert cohens_kappa([]) is None
    assert cohens_kappa([("A_better", None)]) is None


def test_kappa_partial_agreement_below_one():
    # 3 of 4 agree, mixed categories -> kappa strictly between 0 and 1.
    pairs = [
        ("A_better", "A_better"),
        ("A_better", "A_better"),
        ("B_better", "B_better"),
        ("A_better", "B_better"),
    ]
    k = cohens_kappa(pairs)
    assert k is not None and 0.0 < k < 1.0


def test_kappa_chance_corrected_known_value():
    # Classic 2x2: raters agree on 2x2 contingency [[20,5],[10,15]].
    pairs = (
        [("yes", "yes")] * 20
        + [("yes", "no")] * 5
        + [("no", "yes")] * 10
        + [("no", "no")] * 15
    )
    # po = 35/50 = 0.7 ; pe = (25/50*30/50)+(25/50*20/50) = 0.3+0.2 = 0.5 ; kappa = 0.4
    assert cohens_kappa(pairs) == 0.4


def test_jaccard():
    assert jaccard(["dosing_error"], ["dosing_error"]) == 1.0
    assert jaccard([], []) == 1.0
    assert jaccard(["a", "b"], ["b", "c"]) == round(1 / 3, 4)
    assert jaccard(["a"], ["b"]) == 0.0


def test_aggregate_kappa_overall_and_by_specialty():
    observations = [
        {"specialty": "nephrology", "verdict_a": "A_better", "verdict_b": "A_better"},
        {"specialty": "nephrology", "verdict_a": "B_better", "verdict_b": "A_better"},
        {"specialty": "cardiology", "verdict_a": "B_better", "verdict_b": "B_better"},
    ]
    agg = aggregate_kappa(observations)
    assert agg["n"] == 3
    assert "nephrology" in agg["by_specialty"]
    assert "cardiology" in agg["by_specialty"]
    assert agg["observed_agreement"] == round(2 / 3, 4)
