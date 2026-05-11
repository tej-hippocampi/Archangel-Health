"""
Unit tests for the intra-op form validation module.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop.form_validation import (  # noqa: E402
    REQUIRED_UNIVERSAL_FIELDS,
    or_duration_consistent_with_timestamps,
    validate_required_fields,
)


def _full_universal() -> dict:
    return {
        "documented_complication": False,
        "ebl": 100,
        "transfusion_total_units": 0,
        "conversion": "NO",
        "sustained_hypotension": False,
        "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False,
        "or_duration_minutes": 90,
        "difficult_airway": False,
        "net_fluid_balance": 0,
        "anesthesia_type": "GENERAL",
    }


def test_no_missing_when_all_universal_present():
    assert validate_required_fields(_full_universal()) == []


def test_each_missing_universal_is_reported():
    base = _full_universal()
    for key in REQUIRED_UNIVERSAL_FIELDS:
        f = dict(base)
        del f[key]
        missing = validate_required_fields(f)
        assert key in missing


def test_complication_requires_types_and_description():
    f = _full_universal()
    f["documented_complication"] = True
    missing = validate_required_fields(f)
    assert "complication_types" in missing
    assert "complication_description" in missing


def test_complication_satisfied_when_types_and_description_present():
    f = _full_universal()
    f.update({
        "documented_complication": True,
        "complication_types": ["VASCULAR_INJURY"],
        "complication_description": "minor bleed",
    })
    assert validate_required_fields(f) == []


def test_conversion_yes_requires_reason():
    f = _full_universal()
    f["conversion"] = "YES"
    missing = validate_required_fields(f)
    assert "conversion_reason" in missing


def test_aborted_requires_reason():
    f = _full_universal()
    f["procedural_aborted"] = True
    missing = validate_required_fields(f)
    assert "procedural_aborted_reason" in missing


def test_zero_and_false_are_present():
    """0 and False are valid values, not missing."""
    f = _full_universal()
    f["ebl"] = 0
    f["documented_complication"] = False
    f["transfusion_total_units"] = 0
    assert validate_required_fields(f) == []


def test_empty_string_counts_as_missing():
    f = _full_universal()
    f["anesthesia_type"] = ""
    missing = validate_required_fields(f)
    assert "anesthesia_type" in missing


# ─── OR-time consistency ─────────────────────────────────────────────────────

def test_or_duration_consistency_passes_when_within_one_minute():
    f = {
        "or_started_at": "2026-05-08T08:00:00",
        "or_ended_at":   "2026-05-08T09:30:00",
        "or_duration_minutes": 90,
    }
    assert or_duration_consistent_with_timestamps(f) is True


def test_or_duration_consistency_fails_when_out_of_band():
    f = {
        "or_started_at": "2026-05-08T08:00:00",
        "or_ended_at":   "2026-05-08T09:30:00",
        "or_duration_minutes": 120,
    }
    assert or_duration_consistent_with_timestamps(f) is False


def test_or_duration_consistency_skips_when_fields_missing():
    assert or_duration_consistent_with_timestamps({"or_duration_minutes": 90}) is True
    assert or_duration_consistent_with_timestamps({}) is True
