"""
ICD-10 → clinical-flag derivation for active problems.

The mapping is intentionally a small starter list — production deployments
should expand it from a complete clinical reference. Every flag emitted
here is referenced either in `tuning.HARD_ESCALATORS` (short-circuits to
TIER_3) or `tuning.SOFT_WEIGHTS` (contributes to the weighted score).
"""

from __future__ import annotations

from triage.types import ActiveProblemsInput


# Each entry: (icd-10 prefix, flag code). Prefix match is case-insensitive
# and tested against the dotless code as well (e.g. "I10" matches "I10").
_ICD10_PREFIX_FLAGS: list[tuple[str, str]] = [
    # Cardiac
    ("I50",  "CHF_PRESENT"),                  # branched into RECENT vs HISTORY below
    ("I25",  "CAD"),
    ("I20",  "CAD"),                          # angina is part of CAD spectrum
    # Hypertension — fires HTN_REQUIRING_MEDS only if antihypertensives present
    ("I10",  "HTN_PRESENT"),
    ("I11",  "HTN_PRESENT"),
    ("I12",  "HTN_PRESENT"),
    ("I13",  "HTN_PRESENT"),
    ("I15",  "HTN_PRESENT"),

    # Pulmonary
    ("J44",  "SEVERE_COPD"),                  # COPD; PRD treats J44.* as severe-grade for v1
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

    # Sepsis (recent perioperative driver)
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


def _icd_matches(code: str, prefix: str) -> bool:
    code_n = code.replace(" ", "").upper()
    prefix_n = prefix.replace(" ", "").upper()
    if code_n == prefix_n:
        return True
    return code_n.startswith(prefix_n + ".") or code_n.startswith(prefix_n)


def derive_problem_flags(problems: ActiveProblemsInput) -> set[str]:
    """Return the set of clinical flags fired by the active-problems input."""
    flags: set[str] = set()

    for p in problems.problems:
        if p.status == "RESOLVED":
            continue
        for prefix, flag in _ICD10_PREFIX_FLAGS:
            if _icd_matches(p.icd10, prefix):
                flags.add(flag)

        # Branch CHF into RECENT (within 30 d) vs HISTORY based on onsetDate /
        # severity note, matching the PRD §5.1 hard escalator definition.
        if "CHF_PRESENT" in flags and "CHF_RECENT" not in flags:
            severity = (p.severity_note or "").lower()
            recent_keywords = ("acute", "decompensat", "recent", "<30", "within 30")
            if any(k in severity for k in recent_keywords):
                flags.add("CHF_RECENT")
            elif p.status == "ACTIVE":
                # Treat unspecified active CHF as historical unless flagged recent.
                flags.add("CHF_HISTORY_NOT_RECENT")
            else:
                flags.add("CHF_HISTORY_NOT_RECENT")

    # Functional status hard / soft flags
    if problems.functional_status == "TOTALLY_DEPENDENT":
        flags.add("FUNCTIONAL_TOTALLY_DEPENDENT")
    elif problems.functional_status == "PARTIALLY_DEPENDENT":
        flags.add("FUNCTIONAL_PARTIALLY_DEPENDENT")

    # BMI
    if problems.bmi is not None:
        if problems.bmi > 40:
            flags.add("BMI_OVER_40")
        elif problems.bmi < 18.5:
            flags.add("BMI_UNDER_18_5")

    # Drop the placeholder "present" markers — they are intermediate signals
    # used by `derive_flags.py` to combine with medications, not standalone flags.
    return flags
