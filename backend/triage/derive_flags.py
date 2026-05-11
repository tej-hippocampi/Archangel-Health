"""
Orchestrator: combine per-category flag derivers and split into
hard escalators vs weighted soft factors.

This is the boundary between "raw flags fired by the chart" and "flags
the scoring algorithm cares about". Two things happen here:

1. Combine flags from multiple sources (e.g. HTN_REQUIRING_MEDS only fires
   when a problem-list HTN code is present AND an antihypertensive med is
   prescribed).
2. Apply suppression rules from PRD §12 (e.g. COAGULOPATHY suppressed if
   the patient is on therapeutic anticoagulation).
"""

from __future__ import annotations

from typing import Optional, TypedDict

from triage import allergy_flags as allergy
from triage import icd10_flags as icd
from triage import lab_flags as labs
from triage import med_flags as meds
from triage import procedure_flags as proc
from triage import social_flags as social
from triage.tuning import HARD_LABELS, SOFT_LABELS, SOFT_WEIGHTS
from triage.types import InitialTierInput


class DerivedFlags(TypedDict):
    hard: list[str]                          # ordered list of hard-escalator codes (deduped)
    soft: list[tuple[str, int]]              # (flag, weight) for soft-factor scoring


def _infer_sex(input: InitialTierInput) -> Optional[str]:
    """Heuristic: PRD does not carry sex on inputs explicitly. Try to infer
    from the procedure notes if present; else None (men's anemia threshold
    is the more conservative default in lab_flags)."""
    notes = (input.procedure.notes or "").lower()
    if "female" in notes or " f " in f" {notes} ":
        return "F"
    if "male" in notes or " m " in f" {notes} ":
        return "M"
    return None


def _combine_diabetes(raw: set[str]) -> None:
    """Resolve the diabetes flag chain in-place.

    PRD: insulin-dependence overrides oral; either requires a DM diagnosis
    OR severe HbA1c-driven dyscontrol to fire as a soft factor.
    """
    has_dm = "DIABETES_TYPE_1" in raw or "DIABETES_TYPE_2" in raw
    has_insulin = "MED_INSULIN" in raw
    has_oral_dm = "MED_ORAL_DM" in raw

    if has_insulin and (has_dm or "DIABETES_TYPE_1" in raw):
        raw.add("DIABETES_INSULIN_DEPENDENT")
    elif has_insulin:
        raw.add("DIABETES_INSULIN_DEPENDENT")
    elif has_oral_dm and has_dm:
        raw.add("DIABETES_ORAL")

    raw.discard("DIABETES_TYPE_1")
    raw.discard("DIABETES_TYPE_2")
    raw.discard("MED_INSULIN")
    raw.discard("MED_ORAL_DM")


def _combine_htn(raw: set[str]) -> None:
    """HTN_REQUIRING_MEDS = HTN diagnosis AND antihypertensive med."""
    if "HTN_PRESENT" in raw and "MED_HTN_AGENT" in raw:
        raw.add("HTN_REQUIRING_MEDS")
    raw.discard("HTN_PRESENT")
    raw.discard("MED_HTN_AGENT")


def _suppress_coagulopathy_on_anticoag(raw: set[str]) -> None:
    """PRD §12.6 — INR elevation expected when on therapeutic AC."""
    if "ANTICOAGULANT_THERAPEUTIC" in raw and "COAGULOPATHY" in raw:
        raw.discard("COAGULOPATHY")


def _suppress_renal_on_dialysis(raw: set[str]) -> None:
    """Dialysis-dependent is a hard escalator and supersedes the renal soft flags."""
    if "DIALYSIS_DEPENDENT" in raw:
        raw.discard("RENAL_IMPAIRMENT")
        raw.discard("RENAL_IMPAIRMENT_SEVERE")


def _suppress_low_ef(raw: set[str]) -> None:
    """If severe-low EF (hard) is present, the soft 30–40 % bucket is irrelevant."""
    if "LOW_EJECTION_FRACTION_SEVERE" in raw:
        raw.discard("LOW_EJECTION_FRACTION")


def derive_flags(input: InitialTierInput) -> DerivedFlags:
    """Run every per-category deriver, combine cross-source flags, and split
    the result into hard escalators vs weighted soft factors."""

    raw: set[str] = set()
    raw |= proc.derive_procedure_flags(input)
    raw |= icd.derive_problem_flags(input.active_problems)
    raw |= meds.derive_med_flags(input.medications)
    raw |= allergy.derive_allergy_flags(input.allergies)
    raw |= social.derive_social_flags(input.social_history)
    raw |= labs.derive_lab_flags(input.recent_labs, sex=_infer_sex(input))

    # Cross-source combinations
    _combine_diabetes(raw)
    _combine_htn(raw)

    # Suppressions
    _suppress_coagulopathy_on_anticoag(raw)
    _suppress_renal_on_dialysis(raw)
    _suppress_low_ef(raw)

    # Split into hard vs soft, preserving a stable order for the reasons list.
    hard: list[str] = []
    soft: list[tuple[str, int]] = []
    for flag in sorted(raw):
        if flag in HARD_LABELS:
            hard.append(flag)
        elif flag in SOFT_WEIGHTS:
            soft.append((flag, SOFT_WEIGHTS[flag]))
        # else: intermediate flag (e.g. DIABETES_TYPE_2 if combine left it) — ignore

    return {"hard": hard, "soft": soft}
