"""
Medication → clinical-flag derivation.

Substring matching on medication name is the v1 strategy (RxNorm-aware
matching is a follow-up). The `_HTN_AGENT_KEYWORDS` and `_INSULIN_KEYWORDS`
sets are used by the orchestrator to combine with active problems.
"""

from __future__ import annotations

from datetime import date, datetime

from triage.types import MedicationsInput


_ANTICOAGULANT_KEYWORDS = (
    "warfarin", "coumadin",
    "apixaban", "eliquis",
    "rivaroxaban", "xarelto",
    "dabigatran", "pradaxa",
    "edoxaban", "savaysa",
    "enoxaparin", "lovenox",
)

_ANTIPLATELET_KEYWORDS = (
    "aspirin", "asa",
    "clopidogrel", "plavix",
    "ticagrelor", "brilinta",
    "prasugrel", "effient",
)

_INSULIN_KEYWORDS = (
    "insulin", "humalog", "novolog", "lantus", "levemir",
    "tresiba", "humulin", "novolin", "basaglar", "toujeo",
)

_ORAL_DM_KEYWORDS = (
    "metformin", "glipizide", "glyburide", "glimepiride",
    "sitagliptin", "linagliptin", "saxagliptin",
    "empagliflozin", "dapagliflozin", "canagliflozin",
    "pioglitazone", "rosiglitazone",
    "exenatide", "liraglutide", "semaglutide", "ozempic", "trulicity",
)

_STEROID_KEYWORDS = (
    "prednisone", "prednisolone",
    "methylprednisolone", "medrol",
    "hydrocortisone", "dexamethasone",
)

_IMMUNOSUPPRESSANT_KEYWORDS = (
    "tacrolimus", "prograf",
    "cyclosporine", "neoral",
    "mycophenolate", "cellcept", "myfortic",
    "azathioprine", "imuran",
    "sirolimus", "rapamune",
    "adalimumab", "humira",
    "infliximab", "remicade",
    "etanercept", "enbrel",
    "rituximab", "rituxan",
)

_OPIOID_KEYWORDS = (
    "morphine", "oxycodone", "hydrocodone",
    "fentanyl", "methadone", "tramadol",
    "hydromorphone", "dilaudid", "buprenorphine",
    "percocet", "vicodin", "oxycontin",
)

_HTN_AGENT_KEYWORDS = (
    "lisinopril", "enalapril", "ramipril", "benazepril", "captopril",
    "losartan", "valsartan", "olmesartan", "irbesartan", "candesartan", "telmisartan",
    "amlodipine", "nifedipine", "diltiazem", "verapamil",
    "metoprolol", "atenolol", "carvedilol", "bisoprolol", "propranolol", "labetalol",
    "hydrochlorothiazide", "hctz", "chlorthalidone", "indapamide",
    "spironolactone",
    "hydralazine",
)

_BETA_BLOCKER_KEYWORDS = (
    "metoprolol", "atenolol", "carvedilol", "bisoprolol", "propranolol",
)

_LOOP_DIURETIC_KEYWORDS = ("furosemide", "lasix", "torsemide", "bumetanide")


def _matches_any(name: str, keywords: tuple[str, ...]) -> bool:
    return any(k in name for k in keywords)


def _days_since(start_date: str | None) -> int | None:
    if not start_date:
        return None
    try:
        d = datetime.fromisoformat(start_date).date() if "T" in start_date else date.fromisoformat(start_date)
    except (ValueError, TypeError):
        return None
    return (date.today() - d).days


def derive_med_flags(meds: MedicationsInput) -> set[str]:
    """Flags fired purely from the medications list."""
    flags: set[str] = set()

    antiplatelet_hits: list[str] = []

    for med in meds.medications:
        name = (med.name or "").lower()
        if not name:
            continue

        if _matches_any(name, _ANTICOAGULANT_KEYWORDS):
            flags.add("ANTICOAGULANT_THERAPEUTIC")

        if _matches_any(name, _ANTIPLATELET_KEYWORDS):
            antiplatelet_hits.append(name)

        if _matches_any(name, _INSULIN_KEYWORDS):
            flags.add("MED_INSULIN")

        if _matches_any(name, _ORAL_DM_KEYWORDS):
            flags.add("MED_ORAL_DM")

        if _matches_any(name, _STEROID_KEYWORDS):
            # PRD: chronic = >20 mg pred-equiv >30 d, or any dose >90 d.
            # Without dose normalization, treat any steroid started >30 d ago
            # OR explicitly indicated chronic as CHRONIC_STEROIDS.
            days = _days_since(med.start_date)
            chronic = (days is not None and days > 30) or "chronic" in (med.indication or "").lower()
            if chronic:
                flags.add("CHRONIC_STEROIDS")

        if _matches_any(name, _IMMUNOSUPPRESSANT_KEYWORDS):
            flags.add("IMMUNOSUPPRESSANTS")

        if _matches_any(name, _OPIOID_KEYWORDS):
            days = _days_since(med.start_date)
            if days is not None and days > 90:
                flags.add("CHRONIC_OPIOIDS")

        if _matches_any(name, _HTN_AGENT_KEYWORDS):
            flags.add("MED_HTN_AGENT")

        if _matches_any(name, _BETA_BLOCKER_KEYWORDS):
            flags.add("BETA_BLOCKER_ON_BOARD")

        if _matches_any(name, _LOOP_DIURETIC_KEYWORDS):
            flags.add("DIURETIC_LOOP")

    # Two distinct antiplatelet agents → DAPT
    if len({k for k in antiplatelet_hits}) >= 2:
        flags.add("DUAL_ANTIPLATELET")

    # Polypharmacy ≥ 10 active medications
    if len(meds.medications) >= 10:
        flags.add("POLYPHARMACY_HIGH")

    return flags
