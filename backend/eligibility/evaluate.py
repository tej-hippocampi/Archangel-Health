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

CHECK_CRITERIA: Dict[str, str] = {
    "partA_active": (
        "Medicare Part A (Hospital Insurance) must be active on the surgery date — "
        "effective on or before that date, and not terminated before it."
    ),
    "partB_active": (
        "Medicare Part B (Medical Insurance) must be active on the surgery date — "
        "effective on or before that date, and not terminated before it."
    ),
    "not_ma": (
        "The patient must be on Original (fee-for-service) Medicare — not enrolled in a "
        "Medicare Advantage (Part C) plan on the surgery date."
    ),
    "medicare_primary": (
        "Medicare must be the primary payer on the surgery date, with no MSP arrangement "
        "placing another payer first."
    ),
    "not_esrd_basis": (
        "The Medicare entitlement basis must be age or disability — not End-Stage Renal Disease."
    ),
    "not_umwa": (
        "The patient must not be covered by the United Mine Workers of America Health Plan."
    ),
}

CHECK_RECOMMENDED_ACTIONS: Dict[str, str] = {
    "partA_active": (
        "Request a current X12 271 (or payer portal printout) showing Part A entitlement "
        "dates, or confirm coverage with the payer before overriding."
    ),
    "partB_active": (
        "Request a current X12 271 (or payer portal printout) showing Part B entitlement "
        "dates, or confirm coverage with the payer before overriding."
    ),
    "not_ma": (
        "Confirm enrollment via the payer portal or a 271 with plan-level detail — Part C "
        "contract IDs starting with H, R, or E indicate an MA plan."
    ),
    "medicare_primary": (
        "Verify MSP status with the payer (working-aged, workers' comp, or other primary "
        "coverage) before overriding."
    ),
    "not_esrd_basis": (
        "Confirm the entitlement basis (age/disability vs ESRD) on the patient's Medicare "
        "record — a kidney-disease diagnosis alone does not make the basis ESRD."
    ),
    "not_umwa": "Confirm with the patient or payer whether UMWA Health Plan coverage exists.",
}

# Maps each check to (extraction field, evidence value keys) for the rationale evidence block.
_CHECK_EVIDENCE_FIELDS: Dict[str, tuple] = {
    "partA_active": ("partA", ("status", "effectiveDate", "terminationDate")),
    "partB_active": ("partB", ("status", "effectiveDate", "terminationDate")),
    "not_ma": ("medicareAdvantage", ("enrolled", "contractId", "planName")),
    "medicare_primary": ("medicarePrimary", ("isPrimary", "secondaryReason")),
    "not_esrd_basis": ("esrdBasis", ("isESRDBasis",)),
    "not_umwa": ("umwa", ("isUMWA",)),
}


def _coverage_reasoning(c: Dict[str, Any], part: str, on: date) -> str:
    status = (c or {}).get("status")
    eff_raw = (c or {}).get("effectiveDate")
    term_raw = (c or {}).get("terminationDate")
    eff = _parse_date(eff_raw)
    term = _parse_date(term_raw)
    on_s = on.isoformat()
    if status is None or status == "UNKNOWN":
        return f"No {part} coverage status could be extracted from the provided documents."
    if status == "INACTIVE":
        return f"The documents report {part} as inactive."
    if eff and eff > on:
        return (
            f"{part} is reported ACTIVE, but its effective date {eff.isoformat()} is after "
            f"the surgery date {on_s}."
        )
    if term and term < on:
        return (
            f"{part} is reported ACTIVE, but coverage terminated {term.isoformat()}, before "
            f"the surgery date {on_s}."
        )
    eff_s = f"effective {eff.isoformat()}" if eff else "with no effective date stated"
    term_s = (
        f"terminating {term.isoformat()} (on/after the surgery date)"
        if term
        else "with no termination date on file"
    )
    return f"{part} is ACTIVE, {eff_s}, {term_s} — covering the surgery date {on_s}."


def _yes_no_reasoning(check: str, extracted: Dict[str, Any]) -> str:
    if check == "not_ma":
        d = extracted.get("medicareAdvantage") or {}
        v = d.get("enrolled")
        if v == "NO":
            return "The documents indicate Original Medicare with no Medicare Advantage enrollment."
        if v == "YES":
            plan = d.get("planName") or d.get("contractId")
            plan_s = f" ({plan})" if plan else ""
            return f"The documents show enrollment in a Medicare Advantage (Part C) plan{plan_s} on the surgery date."
        return "Medicare Advantage enrollment is not addressed in the provided documents."
    if check == "medicare_primary":
        d = extracted.get("medicarePrimary") or {}
        v = d.get("isPrimary")
        if v == "YES":
            return "The documents indicate Medicare is the primary payer with no MSP indicator pointing elsewhere."
        if v == "NO":
            why = d.get("secondaryReason")
            why_s = f" ({why})" if why else ""
            return f"The documents show another payer is primary ahead of Medicare{why_s}."
        return "Primary-payer (MSP) status is not addressed in the provided documents."
    if check == "not_esrd_basis":
        d = extracted.get("esrdBasis") or {}
        v = d.get("isESRDBasis")
        if v == "NO":
            return "The entitlement basis is age or disability — not ESRD."
        if v == "YES":
            return "The documents state the Medicare entitlement basis is End-Stage Renal Disease."
        return "The Medicare entitlement basis is not stated in the provided documents."
    # not_umwa
    d = extracted.get("umwa") or {}
    v = d.get("isUMWA")
    if v == "NO":
        return "No UMWA Health Plan coverage appears in the documents."
    if v == "YES":
        return "The documents show enrollment in the UMWA Health Plan."
    return "A payer entry partially suggests mine-industry coverage, but UMWA could not be confirmed."


def build_rationale(
    extracted: Dict[str, Any],
    surgery_date: str,
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> list:
    """Build the per-check structured rationale shown on the review/override screen.

    Deterministic (no LLM): criterion → evidence → reasoning → recommended action,
    mirroring the waypoint/rubric pattern from Anthropic's prior-auth-review skill.
    """
    extracted = extracted or {}
    overrides = overrides or {}
    on = _parse_date(surgery_date) or date.today()
    computed = evaluate(extracted, surgery_date)
    final = apply_overrides(computed, overrides)

    entries = []
    for key in CHECK_LABELS:
        field, value_keys = _CHECK_EVIDENCE_FIELDS[key]
        obj = extracted.get(field) or {}
        if key in ("partA_active", "partB_active"):
            part = "Part A" if key == "partA_active" else "Part B"
            reasoning = _coverage_reasoning(obj, part, on)
        else:
            reasoning = _yes_no_reasoning(key, extracted)

        override_rec = overrides.get(key)
        override_out = None
        if override_rec and final[key] != computed[key]:
            override_out = {
                "to": override_rec.get("to"),
                "reason": override_rec.get("reason"),
                "actor": override_rec.get("actor"),
                "ts": override_rec.get("ts"),
                "originalVerdict": computed[key],
            }

        entries.append(
            {
                "key": key,
                "label": CHECK_LABELS[key],
                "verdict": final[key],
                "criterion": CHECK_CRITERIA[key],
                "reasoning": reasoning,
                "evidence": {
                    "sourceExcerpt": obj.get("sourceExcerpt") or "(not present in documents)",
                    "values": {k: obj.get(k) for k in value_keys if obj.get(k) is not None},
                },
                "override": override_out,
                "recommendedAction": (
                    CHECK_RECOMMENDED_ACTIONS[key] if final[key] in ("FAIL", "UNKNOWN") else None
                ),
            }
        )
    return entries


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
