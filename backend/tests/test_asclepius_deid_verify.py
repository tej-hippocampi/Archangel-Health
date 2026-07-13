"""Pluggable de-identification VERIFIER tests (Data Provider Portal PRD §7.5).

Covers: a clean timeline-normalized case passes; a planted email/phone fails and
surfaces MASKED kinds (never the cleartext value); an unavailable requested
backend degrades to baseline (never a silent pass) with requested_backend /
fallback_reason set.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.deid_verify import (  # noqa: E402
    mask_findings,
    verify_case,
    verify_texts,
)


def _clean_case(**over):
    """A timeline-normalized, PHI-free case: offsets are ints, notes use relative
    ``[day -N]`` tokens (no calendar dates), no names/MRNs/contacts."""
    base = dict(
        case_source="real_deid",
        specialty="nephrology",
        demographics={"age_band": "70-79", "sex": "M"},
        problem_list=[{"condition": "CKD stage 4", "since_offset_days": -900}],
        medications=[{"drug": "hydrochlorothiazide", "dose": "25 mg", "route": "PO", "freq": "daily"}],
        vitals={"BP": "150/90", "HR": 88},
        lab_panels=[{"panel": "BMP", "collected_offset_days": -5, "results": [
            {"analyte": "Sodium", "value": 112, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"},
        ]}],
        notes=[{"note_type": "Consult", "author_role": "nephrology",
                "text": "[day -5] Euvolemic; started thiazide. [day 0] Na now 112."}],
    )
    base.update(over)
    return base


def test_clean_normalized_case_passes():
    res = verify_case(_clean_case(), backend="baseline")
    assert res["passed"] is True
    assert res["findings"] == []
    assert res["backend"] == "baseline"
    assert res["n_fields_scanned"] > 0


def test_planted_email_and_phone_fail_and_are_masked():
    email = "jane.doe@hospital.org"
    phone = "(415) 555-0142"
    case = _clean_case(notes=[{
        "note_type": "Consult", "author_role": "nephrology",
        "text": f"[day 0] Call patient at {phone} or {email} for follow-up.",
    }])
    res = verify_case(case, backend="baseline")

    assert res["passed"] is False
    # KINDS are present...
    assert "email" in res["findings"]
    assert "phone" in res["findings"]
    # ...but the raw planted values are NEVER in the findings (masking invariant).
    assert email not in res["findings"]
    assert phone not in res["findings"]
    for f in res["findings"]:
        assert "@" not in f
        assert "555" not in f
    # findings are sorted + unique
    assert res["findings"] == sorted(set(res["findings"]))


def test_unavailable_presidio_falls_back_to_baseline_never_silent_pass():
    # presidio_analyzer is not installed in this env → must fall back, NOT pass
    # silently. Run against a case that DOES carry PHI so a silent pass would be
    # a visible failure.
    email = "bob@clinic.net"
    case = _clean_case(notes=[{"note_type": "Consult", "author_role": "nephrology",
                               "text": f"[day 0] Contact {email}."}])
    res = verify_case(case, backend="presidio")

    assert res["backend"] == "baseline"          # which verifier ACTUALLY ran
    assert res["requested_backend"] == "presidio"
    assert res.get("fallback_reason")            # a non-empty reason string
    # the baseline scan still ran — the PHI is caught, not silently passed
    assert res["passed"] is False
    assert "email" in res["findings"]
    assert email not in res["findings"]


def test_unavailable_comprehend_medical_falls_back():
    # boto3/credentials absent → baseline fallback with reason, on a clean case.
    res = verify_case(_clean_case(), backend="comprehend_medical")
    assert res["backend"] == "baseline"
    assert res["requested_backend"] == "comprehend_medical"
    assert res.get("fallback_reason")
    assert res["passed"] is True
    assert res["findings"] == []


def test_verify_texts_direct():
    res = verify_texts(["all clear here", "[day -3] stable"], backend="baseline")
    assert res["passed"] is True
    assert res["findings"] == []
    assert "n_fields_scanned" not in res  # verify_texts omits field-count semantics

    dirty = verify_texts(["SSN 123-45-6789 on file"], backend="baseline")
    assert dirty["passed"] is False
    assert "ssn" in dirty["findings"]
    assert "123-45-6789" not in dirty["findings"]


def test_mask_findings_is_kind_only_passthrough():
    assert mask_findings(["email", "email", "phone"]) == ["email", "phone"]
    assert mask_findings([]) == []
    # coerces + de-dups + sorts, strips blanks
    assert mask_findings(["  ssn ", "", "mrn"]) == ["mrn", "ssn"]


def test_unknown_backend_resolves_to_baseline():
    res = verify_case(_clean_case(), backend="totally-made-up")
    assert res["backend"] == "baseline"
    # unknown != a requested optional backend, so no fallback bookkeeping
    assert "requested_backend" not in res
    assert res["passed"] is True
