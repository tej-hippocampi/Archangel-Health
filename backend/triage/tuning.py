"""
Tuning constants for Initial Pre-Op Triage v1.0.

PRD §10 specifies these as a versioned `tuning.json` reloadable at runtime.
For the first cut we keep them as frozen Python dicts — `get_config()`
serializes them for the admin viewer, and unit tests reference the same
constants so every weight/threshold is asserted in one place.
"""

from __future__ import annotations

from typing import Any


MODEL_VERSION = "initial-tier@1.0.0"
TUNING_VERSION = 1


# ─── 5.2 Soft factor weights ─────────────────────────────────────────────────

SOFT_WEIGHTS: dict[str, int] = {
    # Functional & demographic
    "FUNCTIONAL_PARTIALLY_DEPENDENT":   3,
    "AGE_75_PLUS":                      1,
    "BMI_OVER_40":                      1,
    "BMI_UNDER_18_5":                   2,

    # Cardiac
    "CAD":                              2,
    "CHF_HISTORY_NOT_RECENT":           2,
    "LOW_EJECTION_FRACTION":            2,   # 30–40 %
    "HTN_REQUIRING_MEDS":               1,

    # Pulmonary
    "SEVERE_COPD":                      3,
    "DYSPNEA_AT_REST_OR_MIN_EXERTION":  2,
    "OBSTRUCTIVE_SLEEP_APNEA":          1,
    "CURRENT_SMOKER":                   2,   # within 1 year
    "CURRENT_SMOKER_HEAVY":             1,   # additional, pack-years > 20

    # Renal
    "RENAL_IMPAIRMENT":                 2,   # eGFR < 60
    "RENAL_IMPAIRMENT_SEVERE":          3,   # eGFR < 30 (and not on dialysis — that is hard)

    # Endocrine
    "DIABETES_INSULIN_DEPENDENT":       2,
    "DIABETES_ORAL":                    1,
    "GLYCEMIC_DYSCONTROL":              1,   # HbA1c > 8
    "GLYCEMIC_DYSCONTROL_SEVERE":       2,   # additional, > 9.5

    # Hematologic / nutrition
    "ANEMIA_PREOP":                     1,
    "ANEMIA_SEVERE":                    2,
    "HYPOALBUMINEMIA":                  2,
    "MALNUTRITION_SEVERE":              3,   # additional, < 3.0
    "COAGULOPATHY":                     2,
    "THROMBOCYTOPENIA":                 1,
    "THROMBOCYTOPENIA_SEVERE":          3,   # additional, < 50k

    # Neuro
    "STROKE_HISTORY":                   1,
    "COGNITIVE_IMPAIRMENT":             2,

    # Pharmacological complexity
    "ANTICOAGULANT_THERAPEUTIC":        1,
    "DUAL_ANTIPLATELET":                1,
    "CHRONIC_STEROIDS":                 2,
    "IMMUNOSUPPRESSANTS":               2,
    "CHRONIC_OPIOIDS":                  1,
    "POLYPHARMACY_HIGH":                1,   # ≥10 active

    # Social
    "TRANSPORTATION_BARRIER":           1,
    "AT_RISK_ALCOHOL_OR_AUDIT_POS":     2,
    "ACTIVE_SUBSTANCE_USE":             2,
    "NEEDS_INTERPRETER":                1,

    # Allergy
    "PERIOP_ANAPHYLAXIS_HISTORY":       1,
}


# Human-readable labels for the reasons list and admin viewer.
SOFT_LABELS: dict[str, str] = {
    "FUNCTIONAL_PARTIALLY_DEPENDENT":   "Functional status: partially dependent",
    "AGE_75_PLUS":                      "Age ≥ 75",
    "BMI_OVER_40":                      "BMI > 40",
    "BMI_UNDER_18_5":                   "BMI < 18.5",
    "CAD":                              "Coronary artery disease",
    "CHF_HISTORY_NOT_RECENT":           "CHF history (not within 30 d)",
    "LOW_EJECTION_FRACTION":            "Low ejection fraction (30–40 %)",
    "HTN_REQUIRING_MEDS":               "HTN requiring medication",
    "SEVERE_COPD":                      "Severe COPD",
    "DYSPNEA_AT_REST_OR_MIN_EXERTION":  "Dyspnea at rest or minimal exertion",
    "OBSTRUCTIVE_SLEEP_APNEA":          "Obstructive sleep apnea",
    "CURRENT_SMOKER":                   "Current smoker (within 1 y)",
    "CURRENT_SMOKER_HEAVY":             "Heavy smoking history (> 20 pack-years)",
    "RENAL_IMPAIRMENT":                 "Renal impairment (eGFR < 60)",
    "RENAL_IMPAIRMENT_SEVERE":          "Severe renal impairment (eGFR < 30)",
    "DIABETES_INSULIN_DEPENDENT":       "Insulin-dependent diabetes",
    "DIABETES_ORAL":                    "Oral-agent diabetes",
    "GLYCEMIC_DYSCONTROL":              "Glycemic dyscontrol (HbA1c > 8)",
    "GLYCEMIC_DYSCONTROL_SEVERE":       "Severe glycemic dyscontrol (HbA1c > 9.5)",
    "ANEMIA_PREOP":                     "Pre-op anemia",
    "ANEMIA_SEVERE":                    "Severe anemia (Hb < 10)",
    "HYPOALBUMINEMIA":                  "Hypoalbuminemia (< 3.5)",
    "MALNUTRITION_SEVERE":              "Severe malnutrition (albumin < 3.0)",
    "COAGULOPATHY":                     "Coagulopathy (INR > 1.5 off therapy)",
    "THROMBOCYTOPENIA":                 "Thrombocytopenia (< 100k)",
    "THROMBOCYTOPENIA_SEVERE":          "Severe thrombocytopenia (< 50k)",
    "STROKE_HISTORY":                   "Stroke history",
    "COGNITIVE_IMPAIRMENT":             "Cognitive impairment",
    "ANTICOAGULANT_THERAPEUTIC":        "Therapeutic anticoagulation",
    "DUAL_ANTIPLATELET":                "Dual antiplatelet therapy",
    "CHRONIC_STEROIDS":                 "Chronic systemic steroids",
    "IMMUNOSUPPRESSANTS":               "Immunosuppressants",
    "CHRONIC_OPIOIDS":                  "Chronic opioids (> 90 d)",
    "POLYPHARMACY_HIGH":                "Polypharmacy (≥ 10 active meds)",
    "TRANSPORTATION_BARRIER":           "Transportation barrier",
    "AT_RISK_ALCOHOL_OR_AUDIT_POS":     "At-risk alcohol use / AUDIT positive",
    "ACTIVE_SUBSTANCE_USE":             "Active substance use",
    "NEEDS_INTERPRETER":                "Needs interpreter",
    "PERIOP_ANAPHYLAXIS_HISTORY":       "Severe allergy with peri-op implications",
}


# Soft-weight grouping for the admin display, mirroring PRD §5.2 comments.
SOFT_WEIGHT_GROUPS: list[dict[str, Any]] = [
    {"name": "Functional & demographic", "codes": [
        "FUNCTIONAL_PARTIALLY_DEPENDENT", "AGE_75_PLUS", "BMI_OVER_40", "BMI_UNDER_18_5",
    ]},
    {"name": "Cardiac", "codes": [
        "CAD", "CHF_HISTORY_NOT_RECENT", "LOW_EJECTION_FRACTION", "HTN_REQUIRING_MEDS",
    ]},
    {"name": "Pulmonary", "codes": [
        "SEVERE_COPD", "DYSPNEA_AT_REST_OR_MIN_EXERTION", "OBSTRUCTIVE_SLEEP_APNEA",
        "CURRENT_SMOKER", "CURRENT_SMOKER_HEAVY",
    ]},
    {"name": "Renal", "codes": [
        "RENAL_IMPAIRMENT", "RENAL_IMPAIRMENT_SEVERE",
    ]},
    {"name": "Endocrine", "codes": [
        "DIABETES_INSULIN_DEPENDENT", "DIABETES_ORAL",
        "GLYCEMIC_DYSCONTROL", "GLYCEMIC_DYSCONTROL_SEVERE",
    ]},
    {"name": "Hematologic / nutrition", "codes": [
        "ANEMIA_PREOP", "ANEMIA_SEVERE",
        "HYPOALBUMINEMIA", "MALNUTRITION_SEVERE",
        "COAGULOPATHY", "THROMBOCYTOPENIA", "THROMBOCYTOPENIA_SEVERE",
    ]},
    {"name": "Neuro", "codes": [
        "STROKE_HISTORY", "COGNITIVE_IMPAIRMENT",
    ]},
    {"name": "Pharmacological complexity", "codes": [
        "ANTICOAGULANT_THERAPEUTIC", "DUAL_ANTIPLATELET", "CHRONIC_STEROIDS",
        "IMMUNOSUPPRESSANTS", "CHRONIC_OPIOIDS", "POLYPHARMACY_HIGH",
    ]},
    {"name": "Social", "codes": [
        "TRANSPORTATION_BARRIER", "AT_RISK_ALCOHOL_OR_AUDIT_POS",
        "ACTIVE_SUBSTANCE_USE", "NEEDS_INTERPRETER",
    ]},
    {"name": "Allergy", "codes": [
        "PERIOP_ANAPHYLAXIS_HISTORY",
    ]},
]


# ─── 5.4 Procedure-family base risk ──────────────────────────────────────────

PROCEDURE_BASE: dict[str, int] = {
    "LEJR":               0,
    "SPINAL_FUSION":      2,
    "MAJOR_BOWEL":        3,
    "CABG":               4,
    "HIP_FEMUR_FRACTURE": 3,
}

PROCEDURE_LABELS: dict[str, str] = {
    "LEJR":               "Lower Extremity Joint Replacement",
    "SPINAL_FUSION":      "Spinal Fusion",
    "MAJOR_BOWEL":        "Major Bowel",
    "CABG":               "Coronary Artery Bypass Graft",
    "HIP_FEMUR_FRACTURE": "Hip / Femur Fracture",
}


# ─── 5.3 Score → tier thresholds ─────────────────────────────────────────────

SCORE_TO_TIER: dict[str, int] = {
    "tier3_min": 8,
    "tier2_min": 4,
}


# ─── 5.1 Hard escalators (any one ⇒ TIER_3, short-circuit) ───────────────────

HARD_ESCALATORS: list[dict[str, str]] = [
    {"code": "EMERGENCY_CASE",
     "label": "Emergency case",
     "source": "Procedure"},
    {"code": "SEPSIS_48H",
     "label": "Systemic sepsis within 48 h",
     "source": "Problems / Labs"},
    {"code": "VENTILATOR_DEPENDENT",
     "label": "Ventilator dependent",
     "source": "Problems"},
    {"code": "DISSEMINATED_CANCER",
     "label": "Disseminated cancer",
     "source": "Problems"},
    {"code": "DIALYSIS_DEPENDENT",
     "label": "Dialysis dependent (ESRD)",
     "source": "Problems / Meds"},
    {"code": "CHF_RECENT",
     "label": "CHF within 30 days",
     "source": "Problems"},
    {"code": "LOW_EJECTION_FRACTION_SEVERE",
     "label": "Severe low ejection fraction (EF < 30 %)",
     "source": "Studies"},
    {"code": "ASCITES_30D",
     "label": "Ascites within 30 days",
     "source": "Problems / Labs"},
    {"code": "FUNCTIONAL_TOTALLY_DEPENDENT",
     "label": "Functional status: totally dependent",
     "source": "Problems"},
    {"code": "PRIOR_30D_READMISSION",
     "label": "Prior 30-day readmission",
     "source": "Problems"},
    {"code": "HOUSING_INSTABILITY",
     "label": "Housing instability (homeless or unstable)",
     "source": "Social"},
    {"code": "FOOD_INSECURITY",
     "label": "Food insecurity",
     "source": "Social"},
    {"code": "LIVES_ALONE_NO_CAREGIVER",
     "label": "Lives alone with no reliable caregiver",
     "source": "Social"},
    {"code": "CABG_WITH_EF_UNDER_30",
     "label": "CABG with EF < 30 %",
     "source": "Procedure × Studies"},
]

HARD_LABELS: dict[str, str] = {h["code"]: h["label"] for h in HARD_ESCALATORS}


# ─── 4.6 Lab thresholds ──────────────────────────────────────────────────────

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


# ─── Public snapshot for the admin viewer ────────────────────────────────────

def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def get_config() -> dict[str, Any]:
    """Return a JSON-serializable snapshot of the current tuning config."""
    return {
        "modelVersion": MODEL_VERSION,
        "tuningVersion": TUNING_VERSION,
        "scoreToTier": {_camel(k): v for k, v in SCORE_TO_TIER.items()},
        "hardEscalators": [dict(h) for h in HARD_ESCALATORS],
        "softWeights": {
            code: {"weight": SOFT_WEIGHTS[code], "label": SOFT_LABELS.get(code, code)}
            for code in SOFT_WEIGHTS
        },
        "softWeightGroups": [
            {"name": g["name"], "codes": list(g["codes"])} for g in SOFT_WEIGHT_GROUPS
        ],
        "procedureBase": [
            {
                "family": fam,
                "label": PROCEDURE_LABELS.get(fam, fam),
                "base": base,
            }
            for fam, base in PROCEDURE_BASE.items()
        ],
        "labThresholds": {_camel(k): v for k, v in LAB_THRESHOLDS.items()},
    }
