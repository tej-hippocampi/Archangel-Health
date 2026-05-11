"""
Lab / study threshold-based flag derivation.

Thresholds live in `tuning.LAB_THRESHOLDS`. The deriver picks the latest
value per lab name (by `drawn_at`) before applying thresholds. Sex-specific
anemia thresholds are resolved from the patient's social history (the only
place the schema carries demographic context in v1).
"""

from __future__ import annotations

from typing import Optional

from triage.tuning import LAB_THRESHOLDS
from triage.types import RecentLabsInput


_HB_ALIASES = ("hemoglobin", "hgb", "hb")
_ALBUMIN_ALIASES = ("albumin",)
_EGFR_ALIASES = ("egfr",)
_CREATININE_ALIASES = ("creatinine", "creat")
_HBA1C_ALIASES = ("hba1c", "a1c", "hemoglobin a1c")
_INR_ALIASES = ("inr",)
_PLATELETS_ALIASES = ("platelets", "plt")
_BNP_ALIASES = ("bnp",)
_NTPROBNP_ALIASES = ("nt-probnp", "ntprobnp", "nt probnp")
_LACTATE_ALIASES = ("lactate", "lactic acid")


def _matches(name: str, aliases: tuple[str, ...]) -> bool:
    n = name.lower()
    return any(a in n for a in aliases)


def _latest_value(labs, aliases: tuple[str, ...]) -> Optional[float]:
    candidates = [l for l in labs if _matches(l.name, aliases)]
    if not candidates:
        return None
    candidates.sort(key=lambda l: l.drawn_at or "", reverse=True)
    return candidates[0].value


def derive_lab_flags(
    labs_input: RecentLabsInput,
    *,
    sex: Optional[str] = None,  # "M" / "F" / None
) -> set[str]:
    """Return the set of lab- and study-driven flags."""
    flags: set[str] = set()
    labs = labs_input.labs

    # ─── Hemoglobin ─────────────────────────────────────────────────────────
    hb = _latest_value(labs, _HB_ALIASES)
    if hb is not None:
        women = (sex or "").upper().startswith("F")
        threshold = (
            LAB_THRESHOLDS["anemia_preop_hb_women"] if women
            else LAB_THRESHOLDS["anemia_preop_hb_men"]
        )
        if hb < LAB_THRESHOLDS["anemia_severe_hb"]:
            flags.add("ANEMIA_SEVERE")
            flags.add("ANEMIA_PREOP")
        elif hb < threshold:
            flags.add("ANEMIA_PREOP")

    # ─── Albumin ────────────────────────────────────────────────────────────
    albumin = _latest_value(labs, _ALBUMIN_ALIASES)
    if albumin is not None:
        if albumin < LAB_THRESHOLDS["albumin_malnutrition"]:
            flags.add("MALNUTRITION_SEVERE")
            flags.add("HYPOALBUMINEMIA")
        elif albumin < LAB_THRESHOLDS["albumin_low"]:
            flags.add("HYPOALBUMINEMIA")

    # ─── Renal (eGFR / creatinine) ─────────────────────────────────────────
    egfr = _latest_value(labs, _EGFR_ALIASES)
    creat = _latest_value(labs, _CREATININE_ALIASES)
    severe_renal = (
        (egfr is not None and egfr < LAB_THRESHOLDS["egfr_severe"])
        or (creat is not None and creat >= LAB_THRESHOLDS["creatinine_severe"])
    )
    low_renal = (egfr is not None and egfr < LAB_THRESHOLDS["egfr_low"])
    if severe_renal:
        flags.add("RENAL_IMPAIRMENT_SEVERE")
        flags.add("RENAL_IMPAIRMENT")
    elif low_renal:
        flags.add("RENAL_IMPAIRMENT")

    # ─── Glycemic control ───────────────────────────────────────────────────
    a1c = _latest_value(labs, _HBA1C_ALIASES)
    if a1c is not None:
        if a1c > LAB_THRESHOLDS["hba1c_severe"]:
            flags.add("GLYCEMIC_DYSCONTROL_SEVERE")
            flags.add("GLYCEMIC_DYSCONTROL")
        elif a1c > LAB_THRESHOLDS["hba1c_elevated"]:
            flags.add("GLYCEMIC_DYSCONTROL")

    # ─── Coagulation ────────────────────────────────────────────────────────
    inr = _latest_value(labs, _INR_ALIASES)
    if inr is not None and inr > LAB_THRESHOLDS["inr_coagulopathy"]:
        flags.add("COAGULOPATHY")

    # ─── Platelets ──────────────────────────────────────────────────────────
    plt = _latest_value(labs, _PLATELETS_ALIASES)
    if plt is not None:
        # Some labs report in 10³/µL, normalize: values < 1000 are assumed already in 10³/µL
        normalized = plt * 1000 if plt < 1000 else plt
        if normalized < LAB_THRESHOLDS["platelets_severe"]:
            flags.add("THROMBOCYTOPENIA_SEVERE")
            flags.add("THROMBOCYTOPENIA")
        elif normalized < LAB_THRESHOLDS["platelets_low"]:
            flags.add("THROMBOCYTOPENIA")

    # ─── BNP / Lactate (decompensation markers) ─────────────────────────────
    bnp = _latest_value(labs, _BNP_ALIASES)
    if bnp is not None and bnp > LAB_THRESHOLDS["bnp"]:
        flags.add("BNP_ELEVATED")

    nt = _latest_value(labs, _NTPROBNP_ALIASES)
    if nt is not None and nt > LAB_THRESHOLDS["nt_pro_bnp"]:
        flags.add("BNP_ELEVATED")

    lactate = _latest_value(labs, _LACTATE_ALIASES)
    if lactate is not None and lactate > LAB_THRESHOLDS["lactate"]:
        flags.add("LACTATE_ELEVATED")

    # ─── Echo / EF ──────────────────────────────────────────────────────────
    ef_values = [
        s.ejection_fraction for s in labs_input.studies
        if s.type == "ECHO" and s.ejection_fraction is not None
    ]
    if ef_values:
        lowest = min(ef_values)
        if lowest < LAB_THRESHOLDS["ef_severe"]:
            flags.add("LOW_EJECTION_FRACTION_SEVERE")
        elif lowest < LAB_THRESHOLDS["ef_low"]:
            flags.add("LOW_EJECTION_FRACTION")

    return flags
