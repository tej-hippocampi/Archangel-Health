"""Unit tests for the structured per-check rationale (review/override screen)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eligibility.evaluate import (  # noqa: E402
    CHECK_LABELS,
    build_rationale,
)


def _full_pass_extraction() -> dict:
    return {
        "partA": {
            "status": "ACTIVE",
            "effectiveDate": "2020-01-01",
            "terminationDate": None,
            "sourceExcerpt": "EB*1**MA",
        },
        "partB": {
            "status": "ACTIVE",
            "effectiveDate": "2020-01-01",
            "terminationDate": None,
            "sourceExcerpt": "EB*1**MB",
        },
        "medicareAdvantage": {"enrolled": "NO", "sourceExcerpt": "Original Medicare"},
        "medicarePrimary": {"isPrimary": "YES", "sourceExcerpt": "Medicare primary"},
        "esrdBasis": {"isESRDBasis": "NO", "sourceExcerpt": "Basis: age"},
        "umwa": {"isUMWA": "NO", "sourceExcerpt": "(not present in documents)"},
    }


def test_rationale_covers_all_six_checks_in_order():
    entries = build_rationale(_full_pass_extraction(), "2026-06-01")
    assert [e["key"] for e in entries] == list(CHECK_LABELS.keys())
    for e in entries:
        assert e["label"] == CHECK_LABELS[e["key"]]
        assert e["criterion"]
        assert e["reasoning"]
        assert "sourceExcerpt" in e["evidence"]


def test_rationale_pass_has_no_recommended_action():
    entries = build_rationale(_full_pass_extraction(), "2026-06-01")
    assert all(e["verdict"] == "PASS" for e in entries)
    assert all(e["recommendedAction"] is None for e in entries)
    assert all(e["override"] is None for e in entries)


def test_rationale_coverage_reasoning_mentions_dates():
    entries = build_rationale(_full_pass_extraction(), "2026-06-01")
    part_a = next(e for e in entries if e["key"] == "partA_active")
    assert "2020-01-01" in part_a["reasoning"]
    assert "2026-06-01" in part_a["reasoning"]
    assert part_a["evidence"]["values"]["status"] == "ACTIVE"


def test_rationale_fail_and_unknown_carry_recommended_actions():
    extracted = _full_pass_extraction()
    extracted["medicareAdvantage"] = {
        "enrolled": "YES",
        "planName": "Humana Gold Plus",
        "sourceExcerpt": "Humana Gold Plus HMO",
    }
    extracted["medicarePrimary"] = {"isPrimary": "UNKNOWN", "sourceExcerpt": "(not present in documents)"}
    entries = {e["key"]: e for e in build_rationale(extracted, "2026-06-01")}

    ma = entries["not_ma"]
    assert ma["verdict"] == "FAIL"
    assert "Humana Gold Plus" in ma["reasoning"]
    assert ma["recommendedAction"]

    primary = entries["medicare_primary"]
    assert primary["verdict"] == "UNKNOWN"
    assert primary["recommendedAction"]


def test_rationale_annotates_overrides():
    extracted = _full_pass_extraction()
    extracted["umwa"] = {"isUMWA": "YES", "sourceExcerpt": "UMWA Health Plan member"}
    overrides = {
        "not_umwa": {"to": "PASS", "reason": "Confirmed non-UMWA via payer", "actor": "rn_1", "ts": "2026-06-10T00:00:00Z"}
    }
    entries = {e["key"]: e for e in build_rationale(extracted, "2026-06-01", overrides)}
    umwa = entries["not_umwa"]
    assert umwa["verdict"] == "PASS"
    assert umwa["override"]["originalVerdict"] == "FAIL"
    assert umwa["override"]["reason"] == "Confirmed non-UMWA via payer"
    assert umwa["recommendedAction"] is None


def test_rationale_empty_extraction_is_safe():
    entries = build_rationale({}, "2026-06-01")
    assert len(entries) == 6
    # UMWA defaults to UNKNOWN on truly empty input; everything else UNKNOWN too.
    for e in entries:
        assert e["verdict"] == "UNKNOWN"
        assert e["recommendedAction"]
        assert e["evidence"]["sourceExcerpt"] == "(not present in documents)"
