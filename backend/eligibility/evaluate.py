"""Deterministic verdict logic for the 6 TEAM checks (PRD §7.3).

All inputs are plain dicts from the LLM extraction. No I/O. No exceptions.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

Verdict = str  # "PASS" | "FAIL" | "UNKNOWN"


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    # Try ISO first
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def coverage_active_on(c: Dict[str, Any], on: date) -> Verdict:
    """Is a given Part A/B coverage object active on the surgery date?

    PRD §11.8: termination date == surgery date → coverage is active THROUGH that day.
    """
    status = (c or {}).get("status")
    if status == "INACTIVE":
        return "FAIL"
    if status == "UNKNOWN" or status is None:
        return "UNKNOWN"
    # status == "ACTIVE"
    eff = _parse_date((c or {}).get("effectiveDate"))
    term = _parse_date((c or {}).get("terminationDate"))
    if eff and eff > on:
        return "FAIL"
    if term and term < on:
        return "FAIL"
    return "PASS"


def _yes_no_to_verdict(
    d: Optional[Dict[str, Any]],
    field: str,
    *,
    pass_on: str,  # "YES" or "NO"
) -> Verdict:
    """Convert a {field: YES|NO|UNKNOWN} object into a TEAM verdict.

    pass_on specifies which value of the YES/NO answer should produce PASS.
    """
    if not d:
        return "UNKNOWN"
    v = d.get(field)
    if v == pass_on:
        return "PASS"
    if v is None or v == "UNKNOWN":
        return "UNKNOWN"
    return "FAIL"


def evaluate(extracted: Dict[str, Any], surgery_date: str) -> Dict[str, Verdict]:
    """Evaluate the 6 checks from an extracted-fields dict.

    ``surgery_date`` is ISO YYYY-MM-DD.
    """
    on = _parse_date(surgery_date) or date.today()
    return {
        "partA_active": coverage_active_on(extracted.get("partA") or {}, on),
        "partB_active": coverage_active_on(extracted.get("partB") or {}, on),
        "not_ma": _yes_no_to_verdict(extracted.get("medicareAdvantage"), "enrolled", pass_on="NO"),
        "medicare_primary": _yes_no_to_verdict(
            extracted.get("medicarePrimary"), "isPrimary", pass_on="YES"
        ),
        "not_esrd_basis": _yes_no_to_verdict(
            extracted.get("esrdBasis"), "isESRDBasis", pass_on="NO"
        ),
        "not_umwa": _yes_no_to_verdict(extracted.get("umwa"), "isUMWA", pass_on="NO"),
    }


def overall_verdict(verdicts: Dict[str, Verdict]) -> str:
    """ELIGIBLE if all PASS; INELIGIBLE if any FAIL; BLOCKED_UNKNOWN otherwise."""
    values = list(verdicts.values())
    if not values:
        return "BLOCKED_UNKNOWN"
    if all(v == "PASS" for v in values):
        return "ELIGIBLE"
    if any(v == "FAIL" for v in values):
        return "INELIGIBLE"
    return "BLOCKED_UNKNOWN"


def apply_overrides(verdicts: Dict[str, Verdict], overrides: Dict[str, Dict[str, Any]]) -> Dict[str, Verdict]:
    """Merge per-field overrides (as stored on the check record) over computed verdicts."""
    out = dict(verdicts)
    for field, rec in (overrides or {}).items():
        to = (rec or {}).get("to")
        if to in ("PASS", "FAIL"):
            out[field] = to
    return out


CHECK_LABELS: Dict[str, str] = {
    "partA_active": "Part A active",
    "partB_active": "Part B active",
    "not_ma": "Original Medicare (not MA)",
    "medicare_primary": "Medicare primary payer",
    "not_esrd_basis": "Not ESRD-basis",
    "not_umwa": "Not UMWA",
}

CHECK_PLAIN_LANGUAGE: Dict[str, Dict[str, str]] = {
    "partA_active": {
        "PASS": "Part A (Hospital Insurance) is active on the surgery date.",
        "FAIL": "Part A is not active on the surgery date.",
        "UNKNOWN": "Part A coverage status could not be determined from the provided documents.",
    },
    "partB_active": {
        "PASS": "Part B (Medical Insurance) is active on the surgery date.",
        "FAIL": "Part B is not active on the surgery date.",
        "UNKNOWN": "Part B coverage status could not be determined from the provided documents.",
    },
    "not_ma": {
        "PASS": "Patient is on Original Medicare, not a Medicare Advantage plan.",
        "FAIL": "Patient is enrolled in a Medicare Advantage (Part C) plan on the surgery date.",
        "UNKNOWN": "Medicare Advantage enrollment could not be confirmed from the documents.",
    },
    "medicare_primary": {
        "PASS": "Medicare is the primary payer on the surgery date.",
        "FAIL": "Another payer is primary ahead of Medicare on the surgery date.",
        "UNKNOWN": "Primary-payer order could not be determined from the documents.",
    },
    "not_esrd_basis": {
        "PASS": "Medicare eligibility basis is age or disability (not ESRD).",
        "FAIL": "Medicare eligibility basis is End-Stage Renal Disease.",
        "UNKNOWN": "Medicare eligibility basis could not be determined.",
    },
    "not_umwa": {
        "PASS": "Patient is not enrolled in the UMWA Health Plan.",
        "FAIL": "Patient is enrolled in the UMWA Health Plan.",
        "UNKNOWN": "UMWA enrollment could not be confirmed.",
    },
}
