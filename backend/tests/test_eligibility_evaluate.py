"""Unit tests for the deterministic evaluator (PRD §7.3 + §11)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eligibility.evaluate import (  # noqa: E402
    apply_overrides,
    coverage_active_on,
    evaluate,
    overall_verdict,
)
from datetime import date  # noqa: E402


def test_coverage_active_basic():
    assert coverage_active_on(
        {"status": "ACTIVE", "effectiveDate": "2020-01-01", "terminationDate": None},
        date(2026, 6, 1),
    ) == "PASS"


def test_coverage_terminated_before():
    assert coverage_active_on(
        {"status": "ACTIVE", "effectiveDate": "2020-01-01", "terminationDate": "2024-01-01"},
        date(2026, 6, 1),
    ) == "FAIL"


def test_coverage_term_equals_surgery_is_pass():
    """PRD §11.8: termination date == surgery date → still active."""
    assert coverage_active_on(
        {"status": "ACTIVE", "effectiveDate": "2020-01-01", "terminationDate": "2026-06-01"},
        date(2026, 6, 1),
    ) == "PASS"


def test_coverage_inactive_status_fails():
    assert coverage_active_on({"status": "INACTIVE"}, date(2026, 6, 1)) == "FAIL"


def test_coverage_unknown_status_returns_unknown():
    assert coverage_active_on({"status": "UNKNOWN"}, date(2026, 6, 1)) == "UNKNOWN"
    assert coverage_active_on({}, date(2026, 6, 1)) == "UNKNOWN"


def test_evaluate_full_pass_set():
    extracted = {
        "partA": {"status": "ACTIVE"},
        "partB": {"status": "ACTIVE"},
        "medicareAdvantage": {"enrolled": "NO"},
        "medicarePrimary": {"isPrimary": "YES"},
        "esrdBasis": {"isESRDBasis": "NO"},
        "umwa": {"isUMWA": "NO"},
    }
    verdicts = evaluate(extracted, "2026-06-01")
    assert verdicts == {
        "partA_active": "PASS",
        "partB_active": "PASS",
        "not_ma": "PASS",
        "medicare_primary": "PASS",
        "not_esrd_basis": "PASS",
        "not_umwa": "PASS",
    }
    assert overall_verdict(verdicts) == "ELIGIBLE"


def test_evaluate_ma_enrolled_fails():
    extracted = {
        "partA": {"status": "ACTIVE"},
        "partB": {"status": "ACTIVE"},
        "medicareAdvantage": {"enrolled": "YES"},
        "medicarePrimary": {"isPrimary": "YES"},
        "esrdBasis": {"isESRDBasis": "NO"},
        "umwa": {"isUMWA": "NO"},
    }
    v = evaluate(extracted, "2026-06-01")
    assert v["not_ma"] == "FAIL"
    assert overall_verdict(v) == "INELIGIBLE"


def test_evaluate_unknown_returns_blocked():
    extracted = {
        "partA": {"status": "ACTIVE"},
        "partB": {"status": "ACTIVE"},
        "medicareAdvantage": {"enrolled": "UNKNOWN"},
        "medicarePrimary": {"isPrimary": "YES"},
        "esrdBasis": {"isESRDBasis": "UNKNOWN"},
        "umwa": {"isUMWA": "NO"},
    }
    v = evaluate(extracted, "2026-06-01")
    assert overall_verdict(v) == "BLOCKED_UNKNOWN"


def test_apply_overrides_preserves_existing_overrides():
    """PRD §11.16: overrides survive a re-run."""
    verdicts = {"not_ma": "FAIL", "partA_active": "PASS"}
    overrides = {"not_ma": {"to": "PASS", "reason": "verified by phone"}}
    out = apply_overrides(verdicts, overrides)
    assert out["not_ma"] == "PASS"
    assert overall_verdict(out) == "ELIGIBLE"
