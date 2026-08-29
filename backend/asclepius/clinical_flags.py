"""Product-agnostic clinical flag derivation: labs, medications, ICD-10 problems.

Salvaged from the peri-op triage engine (``triage/lab_flags.py``,
``triage/med_flags.py``, ``triage/icd10_flags.py``) when that product was
retired. The knowledge is not peri-op specific — a hemoglobin of 8.4 is anemia
whoever is reading the chart — so it outlives the surface it was written for.

Two things changed in the salvage, both deliberate:

* **Thresholds are inlined.** The original read ``triage.tuning.LAB_THRESHOLDS``,
  a table shared with a tier-scoring model that no longer exists. The numbers
  below are that table verbatim; they are now this module's own, and changing one
  is a change to this module rather than to a scoring model's tuning.
* **Inputs are plain dicts, not pydantic models.** The original consumed
  ``triage.types`` models built by an intake form. The rows this module actually
  sees now come from ``asclepius/real_cases.py``, which carries partner exports:
  a lab result is ``{"analyte", "value", "unit", "flag", ...}``, a medication is
  ``{"drug": "<order line>"}``, a problem is ``{"condition": ...}``. Every
  accessor therefore takes both spellings — ``analyte`` or ``name``, ``drug`` or
  ``name``, ``icd10`` or ``code`` — so a structured feed and an OCR'd export
  reach the same flags.

Nothing here is wired into the case pipeline. It ships importable and tested;
wiring it into ``curate_lab_panels`` is a separate change with its own review.

Every function is total: an unparseable row contributes no flags rather than
raising. A clinical deriver that throws on one bad row in a 149-row real export
is a deriver that runs on no real exports.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "LAB_THRESHOLDS",
    "ICD10_PREFIX_FLAGS",
    "derive_lab_flags",
    "derive_med_flags",
    "derive_problem_flags",
]


# ─────────────────────────────────────────────────────────────────────────────
# Shared row helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rows(value: Any) -> list[Mapping[str, Any]]:
    """Every mapping in ``value``; anything else is skipped, not raised on."""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    return [r for r in value if isinstance(r, Mapping)]


def _first_str(row: Mapping[str, Any], *keys: str) -> str:
    """The first key present with a non-empty string value."""
    for key in keys:
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _as_float(value: Any) -> Optional[float]:
    """``"12.4"`` and ``12.4`` are the same number; ``"POSITIVE"`` is not one.

    Real exports carry qualitative results in the same column as numeric ones,
    so this has to fail quietly rather than reject the row's whole panel.
    """
    if isinstance(value, bool):        # bool is an int subclass; not a lab value
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# §1 Labs and studies
# ─────────────────────────────────────────────────────────────────────────────

# Verbatim from the retired ``triage/tuning.py::LAB_THRESHOLDS``.
LAB_THRESHOLDS: dict[str, float] = {
    "anemia_preop_hb_women":   12.0,   # g/dL
    "anemia_preop_hb_men":     13.0,   # g/dL
    "anemia_severe_hb":        10.0,
    "albumin_low":              3.5,   # g/dL
    "albumin_malnutrition":     3.0,
    "egfr_low":                60.0,   # mL/min/1.73m²
    "egfr_severe":             30.0,
    "creatinine_severe":        2.0,   # mg/dL
    "hba1c_elevated":           8.0,   # %
    "hba1c_severe":             9.5,
    "inr_coagulopathy":         1.5,
    "platelets_low":       100000.0,   # /µL
    "platelets_severe":     50000.0,
    "bnp":                    400.0,   # pg/mL
    "nt_pro_bnp":            1800.0,
    "lactate":                  2.0,   # mmol/L
    "ef_low":                  40.0,   # %
    "ef_severe":               30.0,
}

_HB_ALIASES = ("hemoglobin", "haemoglobin", "hgb", "hb")
_ALBUMIN_ALIASES = ("albumin",)
_EGFR_ALIASES = ("egfr",)
_CREATININE_ALIASES = ("creatinine", "creat")
_HBA1C_ALIASES = ("hba1c", "a1c", "hemoglobin a1c", "haemoglobin a1c")
_INR_ALIASES = ("inr",)
_PLATELETS_ALIASES = ("platelets", "plt")
_BNP_ALIASES = ("bnp",)
_NTPROBNP_ALIASES = ("nt-probnp", "ntprobnp", "nt probnp")
_LACTATE_ALIASES = ("lactate", "lactic acid")

# Hemoglobin's aliases are substrings of HbA1c's ("hb" is in "hba1c", and
# "hemoglobin" is in "hemoglobin a1c"), so a plain substring match reads an A1c of
# 9.8 as a hemoglobin of 9.8 and fires ANEMIA_SEVERE on a patient with normal
# blood counts. The original had this bug; it was invisible because its input
# came from a form with a fixed analyte vocabulary. Real exports spell the analyte
# however the sending lab spells it, so the narrower aliases are checked FIRST and
# a row that matches one is not offered to hemoglobin.
_HB_EXCLUSIONS = _HBA1C_ALIASES


def _matches(name: str, aliases: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(alias in lowered for alias in aliases)


def _lab_name(row: Mapping[str, Any]) -> str:
    """``analyte`` is the asclepius/partner-export spelling; ``name`` the intake one."""
    return _first_str(row, "analyte", "name", "label")


def _lab_drawn_at(row: Mapping[str, Any]) -> str:
    """Sort key for "latest value wins". Empty sorts oldest, which is the intent:
    an undated row never displaces a dated one."""
    return _first_str(row, "drawn_at", "collected_at", "resulted_at", "observed_at")


def _latest_value(
    labs: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
    *,
    exclude: Sequence[str] = (),
) -> Optional[float]:
    """The most recently drawn numeric value whose analyte matches ``aliases``."""
    candidates = [
        row for row in labs
        if (name := _lab_name(row))
        and _matches(name, aliases)
        and not (exclude and _matches(name, exclude))
        and _as_float(row.get("value")) is not None
    ]
    if not candidates:
        return None
    candidates.sort(key=_lab_drawn_at, reverse=True)
    return _as_float(candidates[0].get("value"))


def _ejection_fractions(studies: Sequence[Mapping[str, Any]]) -> list[float]:
    """EF values from echocardiograms.

    ``type`` is the intake spelling and ``modality`` the export one; the value may
    be ``ejection_fraction`` or a bare ``ef``. A study that names no modality at
    all but does carry an EF is counted — only an echo reports one.
    """
    out: list[float] = []
    for study in studies:
        modality = _first_str(study, "type", "modality").upper()
        if modality and "ECHO" not in modality:
            continue
        ef = _as_float(study.get("ejection_fraction"))
        if ef is None:
            ef = _as_float(study.get("ef"))
        if ef is not None:
            out.append(ef)
    return out


def derive_lab_flags(
    labs: Any,
    studies: Any = None,
    *,
    sex: Optional[str] = None,  # "M" / "F" / None
) -> set[str]:
    """Lab- and study-driven clinical flags.

    ``labs`` is a sequence of result rows; ``studies`` a sequence of study rows.
    Both accept the asclepius export spelling (``analyte`` / ``modality``) and the
    structured-intake spelling (``name`` / ``type``). The latest value per analyte
    wins, by drawn-at date.

    ``sex`` selects the anemia threshold; anything not starting with ``F`` uses the
    male threshold, matching the original. Unknown sex therefore under-flags rather
    than over-flags mild anemia in women — a deliberate carry-over, since the
    severe threshold (10.0) is sex-independent and fires either way.
    """
    flags: set[str] = set()
    lab_rows = _rows(labs)
    study_rows = _rows(studies)

    # ─── Hemoglobin ─────────────────────────────────────────────────────────
    hb = _latest_value(lab_rows, _HB_ALIASES, exclude=_HB_EXCLUSIONS)
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
    albumin = _latest_value(lab_rows, _ALBUMIN_ALIASES)
    if albumin is not None:
        if albumin < LAB_THRESHOLDS["albumin_malnutrition"]:
            flags.add("MALNUTRITION_SEVERE")
            flags.add("HYPOALBUMINEMIA")
        elif albumin < LAB_THRESHOLDS["albumin_low"]:
            flags.add("HYPOALBUMINEMIA")

    # ─── Renal (eGFR / creatinine) ─────────────────────────────────────────
    egfr = _latest_value(lab_rows, _EGFR_ALIASES)
    creat = _latest_value(lab_rows, _CREATININE_ALIASES)
    severe_renal = (
        (egfr is not None and egfr < LAB_THRESHOLDS["egfr_severe"])
        or (creat is not None and creat >= LAB_THRESHOLDS["creatinine_severe"])
    )
    low_renal = egfr is not None and egfr < LAB_THRESHOLDS["egfr_low"]
    if severe_renal:
        flags.add("RENAL_IMPAIRMENT_SEVERE")
        flags.add("RENAL_IMPAIRMENT")
    elif low_renal:
        flags.add("RENAL_IMPAIRMENT")

    # ─── Glycemic control ───────────────────────────────────────────────────
    a1c = _latest_value(lab_rows, _HBA1C_ALIASES)
    if a1c is not None:
        if a1c > LAB_THRESHOLDS["hba1c_severe"]:
            flags.add("GLYCEMIC_DYSCONTROL_SEVERE")
            flags.add("GLYCEMIC_DYSCONTROL")
        elif a1c > LAB_THRESHOLDS["hba1c_elevated"]:
            flags.add("GLYCEMIC_DYSCONTROL")

    # ─── Coagulation ────────────────────────────────────────────────────────
    inr = _latest_value(lab_rows, _INR_ALIASES)
    if inr is not None and inr > LAB_THRESHOLDS["inr_coagulopathy"]:
        flags.add("COAGULOPATHY")

    # ─── Platelets ──────────────────────────────────────────────────────────
    plt = _latest_value(lab_rows, _PLATELETS_ALIASES)
    if plt is not None:
        # Some labs report in 10³/µL: values < 1000 are assumed already in 10³/µL.
        normalized = plt * 1000 if plt < 1000 else plt
        if normalized < LAB_THRESHOLDS["platelets_severe"]:
            flags.add("THROMBOCYTOPENIA_SEVERE")
            flags.add("THROMBOCYTOPENIA")
        elif normalized < LAB_THRESHOLDS["platelets_low"]:
            flags.add("THROMBOCYTOPENIA")

    # ─── BNP / lactate (decompensation markers) ─────────────────────────────
    # NT-proBNP's aliases contain no bare "bnp" substring collision risk in the
    # other direction — but "nt-probnp" DOES contain "bnp", so BNP must exclude it
    # or an NT-proBNP of 900 reads as a BNP of 900 and fires BNP_ELEVATED at less
    # than half the NT-proBNP threshold.
    bnp = _latest_value(lab_rows, _BNP_ALIASES, exclude=_NTPROBNP_ALIASES)
    if bnp is not None and bnp > LAB_THRESHOLDS["bnp"]:
        flags.add("BNP_ELEVATED")

    nt = _latest_value(lab_rows, _NTPROBNP_ALIASES)
    if nt is not None and nt > LAB_THRESHOLDS["nt_pro_bnp"]:
        flags.add("BNP_ELEVATED")

    lactate = _latest_value(lab_rows, _LACTATE_ALIASES)
    if lactate is not None and lactate > LAB_THRESHOLDS["lactate"]:
        flags.add("LACTATE_ELEVATED")

    # ─── Echo / EF ──────────────────────────────────────────────────────────
    ef_values = _ejection_fractions(study_rows)
    if ef_values:
        lowest = min(ef_values)
        if lowest < LAB_THRESHOLDS["ef_severe"]:
            flags.add("LOW_EJECTION_FRACTION_SEVERE")
        elif lowest < LAB_THRESHOLDS["ef_low"]:
            flags.add("LOW_EJECTION_FRACTION")

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# §2 Medications
# ─────────────────────────────────────────────────────────────────────────────
#
# Substring matching on the medication name. RxNorm-aware matching was a
# follow-up in the original and still is. ``_HTN_AGENT_KEYWORDS`` and
# ``_INSULIN_KEYWORDS`` were consumed by the retired orchestrator to combine
# with active problems; they are kept because the flags they emit
# (``MED_HTN_AGENT``, ``MED_INSULIN``) are exactly the combining signal a future
# consumer needs.

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

# "asa" as a bare substring matches "asacol", "amiodarone HCl in NaCl"… and any
# drug whose name happens to contain the letters. The original matched it as a
# substring and would fire DUAL_ANTIPLATELET on a patient taking aspirin plus
# mesalamine. Abbreviations are therefore matched as whole tokens.
_TOKEN_ONLY_KEYWORDS = frozenset({"asa", "hctz"})


def _tokenize(name: str) -> set[str]:
    return {t for t in "".join(c if c.isalnum() else " " for c in name).split() if t}


def _matches_any(name: str, keywords: Sequence[str], tokens: set[str]) -> bool:
    return any(
        (kw in tokens) if kw in _TOKEN_ONLY_KEYWORDS else (kw in name)
        for kw in keywords
    )


# Brand → generic, so a patient on "Plavix" and "clopidogrel" (the same drug under
# two names, which a merged multi-format export routinely produces) is not read as
# being on two antiplatelet agents.
_ANTIPLATELET_AGENTS: dict[str, str] = {
    "aspirin": "aspirin",
    "asa": "aspirin",
    "clopidogrel": "clopidogrel",
    "plavix": "clopidogrel",
    "ticagrelor": "ticagrelor",
    "brilinta": "ticagrelor",
    "prasugrel": "prasugrel",
    "effient": "prasugrel",
}


def _med_name(row: Mapping[str, Any]) -> str:
    """``drug`` is the asclepius/order-sheet spelling; ``name`` the intake one."""
    return _first_str(row, "name", "drug", "medication")


def _days_since(start_date: Any) -> Optional[int]:
    if not isinstance(start_date, str) or not start_date.strip():
        return None
    raw = start_date.strip()
    try:
        parsed = (
            datetime.fromisoformat(raw).date() if "T" in raw
            else date.fromisoformat(raw)
        )
    except (ValueError, TypeError):
        return None
    return (date.today() - parsed).days


def derive_med_flags(medications: Any) -> set[str]:
    """Flags fired purely from the medications list.

    ``medications`` is a sequence of rows; the drug name is read from ``name``,
    ``drug`` or ``medication``. Rows with no readable name are skipped but still
    count toward polypharmacy only if they carry a name — an unparseable blank
    row is not a drug.
    """
    flags: set[str] = set()
    rows = _rows(medications)

    antiplatelet_hits: set[str] = set()
    named = 0

    for med in rows:
        name = _med_name(med).lower()
        if not name:
            continue
        named += 1
        tokens = _tokenize(name)

        if _matches_any(name, _ANTICOAGULANT_KEYWORDS, tokens):
            flags.add("ANTICOAGULANT_THERAPEUTIC")

        # Recorded per matched AGENT, not per row: the original counted distinct
        # medication STRINGS, so "aspirin 81mg" and "aspirin 325mg" — one drug,
        # two orders — counted as dual antiplatelet therapy.
        for keyword in _ANTIPLATELET_KEYWORDS:
            if (keyword in tokens) if keyword in _TOKEN_ONLY_KEYWORDS else (keyword in name):
                antiplatelet_hits.add(_ANTIPLATELET_AGENTS[keyword])

        if _matches_any(name, _INSULIN_KEYWORDS, tokens):
            flags.add("MED_INSULIN")

        if _matches_any(name, _ORAL_DM_KEYWORDS, tokens):
            flags.add("MED_ORAL_DM")

        if _matches_any(name, _STEROID_KEYWORDS, tokens):
            # Chronic = >20 mg pred-equivalent for >30 d, or any dose >90 d.
            # Without dose normalization, treat any steroid started >30 d ago OR
            # explicitly indicated chronic as CHRONIC_STEROIDS.
            days = _days_since(med.get("start_date"))
            indication = _first_str(med, "indication").lower()
            if (days is not None and days > 30) or "chronic" in indication:
                flags.add("CHRONIC_STEROIDS")

        if _matches_any(name, _IMMUNOSUPPRESSANT_KEYWORDS, tokens):
            flags.add("IMMUNOSUPPRESSANTS")

        if _matches_any(name, _OPIOID_KEYWORDS, tokens):
            days = _days_since(med.get("start_date"))
            if days is not None and days > 90:
                flags.add("CHRONIC_OPIOIDS")

        if _matches_any(name, _HTN_AGENT_KEYWORDS, tokens):
            flags.add("MED_HTN_AGENT")

        if _matches_any(name, _BETA_BLOCKER_KEYWORDS, tokens):
            flags.add("BETA_BLOCKER_ON_BOARD")

        if _matches_any(name, _LOOP_DIURETIC_KEYWORDS, tokens):
            flags.add("DIURETIC_LOOP")

    # Two distinct antiplatelet AGENTS → DAPT.
    if len(antiplatelet_hits) >= 2:
        flags.add("DUAL_ANTIPLATELET")

    # Polypharmacy ≥ 10 active medications.
    if named >= 10:
        flags.add("POLYPHARMACY_HIGH")

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# §3 ICD-10 problems
# ─────────────────────────────────────────────────────────────────────────────
#
# The mapping is intentionally a small starter list — production deployments
# should expand it from a complete clinical reference.

# Each entry: (icd-10 prefix, flag code). Prefix match is case-insensitive and
# tested against the dotless code as well (e.g. "I10" matches "I10").
ICD10_PREFIX_FLAGS: list[tuple[str, str]] = [
    # Cardiac
    ("I50",  "CHF_PRESENT"),                  # branched into RECENT vs HISTORY below
    ("I25",  "CAD"),
    ("I20",  "CAD"),                          # angina is part of CAD spectrum
    # Hypertension — a consumer fires HTN_REQUIRING_MEDS only if
    # MED_HTN_AGENT is also present (see derive_med_flags).
    ("I10",  "HTN_PRESENT"),
    ("I11",  "HTN_PRESENT"),
    ("I12",  "HTN_PRESENT"),
    ("I13",  "HTN_PRESENT"),
    ("I15",  "HTN_PRESENT"),

    # Pulmonary
    ("J44",  "SEVERE_COPD"),                  # J44.* treated as severe-grade in v1
    ("G47.33", "OBSTRUCTIVE_SLEEP_APNEA"),

    # Renal
    ("N17",  "ACUTE_RENAL_FAILURE"),
    ("N18.5", "DIALYSIS_DEPENDENT"),          # ESRD stage 5
    ("N18.6", "DIALYSIS_DEPENDENT"),
    ("Z99.2", "DIALYSIS_DEPENDENT"),

    # Endocrine
    ("E10",  "DIABETES_TYPE_1"),
    ("E11",  "DIABETES_TYPE_2"),

    # Hematologic / hepatic
    ("D69",  "BLEEDING_DIATHESIS"),
    ("R18",  "ASCITES_30D"),                  # ascites
    ("K70.31", "ASCITES_30D"),

    # Sepsis
    ("A41",  "SEPSIS_48H"),
    ("R65.20", "SEPSIS_48H"),
    ("R65.21", "SEPSIS_48H"),

    # Ventilator dependence
    ("Z99.11", "VENTILATOR_DEPENDENT"),
    ("J96.10", "VENTILATOR_DEPENDENT"),
    ("J96.20", "VENTILATOR_DEPENDENT"),

    # Disseminated cancer (secondary/metastatic neoplasm codes)
    ("C77",  "DISSEMINATED_CANCER"),
    ("C78",  "DISSEMINATED_CANCER"),
    ("C79",  "DISSEMINATED_CANCER"),

    # Neuro
    ("I63",  "STROKE_HISTORY"),
    ("Z86.73", "STROKE_HISTORY"),
    ("F03",  "COGNITIVE_IMPAIRMENT"),
    ("G30",  "COGNITIVE_IMPAIRMENT"),

    # Dyspnea
    ("R06.00", "DYSPNEA_AT_REST_OR_MIN_EXERTION"),
    ("R06.02", "DYSPNEA_AT_REST_OR_MIN_EXERTION"),
    ("R06.03", "DYSPNEA_AT_REST_OR_MIN_EXERTION"),
]

_CHF_RECENT_KEYWORDS = ("acute", "decompensat", "recent", "<30", "within 30")


def _icd_matches(code: str, prefix: str) -> bool:
    """Prefix match, tolerant of spacing and of a dotted/dotless code.

    ``"I1 0."`` and ``"i10"`` both match ``"I10"``. The trailing-dot and
    whitespace tolerance exists because OCR'd problem lists carry both.
    """
    code_n = "".join(code.split()).upper().rstrip(".")
    prefix_n = "".join(prefix.split()).upper()
    if code_n == prefix_n:
        return True
    # A dotted prefix ("N18.5") must match the dotted code; a dotless one ("I50")
    # matches "I50", "I50.9" and "I509" alike.
    if "." in prefix_n:
        return code_n.startswith(prefix_n) or code_n.replace(".", "").startswith(
            prefix_n.replace(".", "")
        )
    return code_n.startswith(prefix_n)


def _problem_code(row: Mapping[str, Any]) -> str:
    return _first_str(row, "icd10", "icd10_code", "code")


def derive_problem_flags(
    problems: Any,
    *,
    functional_status: Optional[str] = None,
    bmi: Any = None,
) -> set[str]:
    """Clinical flags fired by the active-problem list.

    ``problems`` is a sequence of rows carrying an ICD-10 code under ``icd10``,
    ``icd10_code`` or ``code``. Rows with no code are skipped — the asclepius
    export's free-text ``condition`` field is deliberately NOT string-matched
    here: guessing an ICD-10 code from prose is a clinical claim this module is
    not entitled to make.

    ``functional_status`` and ``bmi`` were fields on the retired intake model;
    they are keyword arguments now so a consumer that has them can pass them and
    one that does not can leave them out.
    """
    flags: set[str] = set()

    for problem in _rows(problems):
        status = _first_str(problem, "status").upper()
        if status == "RESOLVED":
            continue
        code = _problem_code(problem)
        if not code:
            continue

        matched_chf = False
        for prefix, flag in ICD10_PREFIX_FLAGS:
            if _icd_matches(code, prefix):
                flags.add(flag)
                if flag == "CHF_PRESENT":
                    matched_chf = True

        # Branch CHF into RECENT (within 30 d) vs HISTORY. Decided from THIS
        # problem row's severity note. The original tested a flag set that
        # accumulated across rows, so once any row had fired CHF_PRESENT every
        # later problem in the list — a hangnail included — re-ran the branch and
        # could stamp CHF_HISTORY_NOT_RECENT over a CHF_RECENT already earned.
        if matched_chf:
            severity = _first_str(problem, "severity_note", "severity").lower()
            if any(k in severity for k in _CHF_RECENT_KEYWORDS):
                flags.add("CHF_RECENT")
            else:
                flags.add("CHF_HISTORY_NOT_RECENT")

    # A patient with a recent decompensation is not also "history, not recent".
    if "CHF_RECENT" in flags:
        flags.discard("CHF_HISTORY_NOT_RECENT")

    # ─── Functional status ──────────────────────────────────────────────────
    status = (functional_status or "").upper()
    if status == "TOTALLY_DEPENDENT":
        flags.add("FUNCTIONAL_TOTALLY_DEPENDENT")
    elif status == "PARTIALLY_DEPENDENT":
        flags.add("FUNCTIONAL_PARTIALLY_DEPENDENT")

    # ─── BMI ────────────────────────────────────────────────────────────────
    bmi_value = _as_float(bmi)
    if bmi_value is not None:
        if bmi_value > 40:
            flags.add("BMI_OVER_40")
        elif bmi_value < 18.5:
            flags.add("BMI_UNDER_18_5")

    return flags
