"""
Tests for the IntraopExtractor protocol + MockIntraopExtractor.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop.extractor import (  # noqa: E402
    ExtractionContext,
    MockIntraopExtractor,
    confidence_bin,
    confidence_for,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_confidence_bins_at_documented_thresholds():
    """PRD §3.2 / §6.4 — boundaries: HIGH ≥0.85, MED 0.65–0.85, LOW <0.65."""
    assert confidence_bin(0.95) == "HIGH"
    assert confidence_bin(0.85) == "HIGH"
    assert confidence_bin(0.84) == "MED"
    assert confidence_bin(0.65) == "MED"
    assert confidence_bin(0.64) == "LOW"
    assert confidence_bin(0.0) == "LOW"


def test_confidence_for_canonical_mapping():
    """Self-rated HIGH/MED/LOW collapse to 0.95 / 0.75 / 0.50."""
    assert confidence_for("HIGH") == 0.95
    assert confidence_for("MED") == 0.75
    assert confidence_for("LOW") == 0.50
    assert confidence_for("BOGUS") == 0.0


def test_mock_extractor_lejr_payload():
    extractor = MockIntraopExtractor()
    ctx = ExtractionContext(patient_id="p1", procedure_family="LEJR")
    out = _run(extractor.extract(pdf_bytes=b"fake-pdf", context=ctx))
    assert out.model_version == "intraop-extractor-mock@1.0.0"
    assert out.fields["lejr_joint"] == "KNEE"
    assert out.field_confidences["lejr_joint"] == 0.95
    assert out.field_confidences["or_duration_minutes"] == 0.50  # LOW


def test_mock_extractor_cabg_payload():
    extractor = MockIntraopExtractor()
    ctx = ExtractionContext(patient_id="p2", procedure_family="CABG")
    out = _run(extractor.extract(pdf_bytes=b"fake-pdf", context=ctx))
    assert out.fields["number_of_grafts"] == 3
    assert out.fields["aortic_cross_clamp_minutes"] == 75
    assert out.field_confidences["aortic_cross_clamp_minutes"] == 0.75


def test_mock_extractor_is_deterministic():
    extractor = MockIntraopExtractor()
    ctx = ExtractionContext(patient_id="p3", procedure_family="SPINAL_FUSION")
    a = _run(extractor.extract(pdf_bytes=b"x", context=ctx))
    b = _run(extractor.extract(pdf_bytes=b"x", context=ctx))
    assert a.fields == b.fields
    assert a.field_confidences == b.field_confidences


def test_mock_extractor_failure_simulation_raises():
    extractor = MockIntraopExtractor(simulate_failure=True)
    ctx = ExtractionContext(patient_id="p4", procedure_family="LEJR")
    with pytest.raises(RuntimeError):
        _run(extractor.extract(pdf_bytes=b"x", context=ctx))


def test_low_confidence_fields_are_flagged_for_review():
    """LOW confidence (<0.65) values must be filterable for the review UI."""
    extractor = MockIntraopExtractor()
    ctx = ExtractionContext(patient_id="p5", procedure_family="LEJR")
    out = _run(extractor.extract(pdf_bytes=b"x", context=ctx))
    low = [name for name, c in out.field_confidences.items() if confidence_bin(c) == "LOW"]
    assert "or_duration_minutes" in low
    assert "net_fluid_balance" in low


def test_payload_warnings_surface_missing_fields():
    """Mock declares EBL is found, so no warning. Confirm the channel exists
    and is a list (UI iterates over it)."""
    extractor = MockIntraopExtractor()
    ctx = ExtractionContext(patient_id="p6", procedure_family="LEJR")
    out = _run(extractor.extract(pdf_bytes=b"x", context=ctx))
    assert isinstance(out.warnings, list)


def test_unknown_family_returns_universal_only():
    """When family is unknown / None, the extractor still returns the 11
    universal fields without any per-family extras."""
    extractor = MockIntraopExtractor()
    ctx = ExtractionContext(patient_id="p7", procedure_family=None)
    out = _run(extractor.extract(pdf_bytes=b"x", context=ctx))
    assert "ebl" in out.fields
    assert "lejr_joint" not in out.fields
    assert "number_of_grafts" not in out.fields
