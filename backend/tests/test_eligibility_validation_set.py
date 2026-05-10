"""50-case deterministic regression suite for the evaluator (PRD §12.18).

Asserts: given the canonical extraction shape, the evaluator yields the
expected overall verdict on every fixture.

Real LLM accuracy (parse text → tool-output) is intentionally not tested
in-process — that's a Phase 6 manual benchmark. This suite locks in the
deterministic parts so we never regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from eligibility.evaluate import evaluate, overall_verdict  # noqa: E402
from tests.fixtures.eligibility.validation_cases import CASES  # noqa: E402


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_validation_case(case):
    verdicts = evaluate(case["extracted"], case["surgery_date"])
    overall = overall_verdict(verdicts)
    assert overall == case["expected_overall"], (
        f"{case['id']} ({case['name']}) — expected {case['expected_overall']}, got {overall}; "
        f"verdicts={verdicts}"
    )


def test_at_least_fifty_cases():
    assert len(CASES) >= 50
