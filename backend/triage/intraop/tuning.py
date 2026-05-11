"""
Tuning constants for Intra-Op Reassessment v1.0 (PRD §11).

Frozen Python dicts in v1; persistence to `tuning.json` with hot-swap
is deferred. `get_config()` returns the JSON-serializable snapshot
rendered by the admin viewer at GET /admin/triage/intraop/config.

Thresholds and defaults are anchored to ACS-NSQIP / STS / NHSN /
AAOS guidance (PRD §16) and reviewed quarterly against observed
outcomes.
"""

from __future__ import annotations

from typing import Any


MODEL_VERSION = "intraop-delta@1.0.0"
TUNING_VERSION = 1


# ─── Hard upgrades (PRD §5.1) — any one ⇒ TIER_3 ─────────────────────────────

HARD_UPGRADES: list[dict[str, Any]] = [
    {
        "code": "DOCUMENTED_COMPLICATION",
        "label": "Intra-operative complication documented",
        "applies_to_family": None,                 # any family
    },
    {
        "code": "DURAL_TEAR",
        "label": "Dural tear (CSF leak)",
        "applies_to_family": "SPINAL_FUSION",
    },
    {
        "code": "CONTAMINATION_CLASS_4",
        "label": "Wound contamination class 4 (dirty-infected)",
        "applies_to_family": "MAJOR_BOWEL",
    },
    {
        "code": "REQUIRED_MECHANICAL_BYPASS_SUPPORT",
        "label": "Required mechanical support to wean from bypass",
        "applies_to_family": "CABG",
    },
    {
        "code": "PROCEDURE_ABORTED",
        "label": "Procedure aborted",
        "applies_to_family": None,
    },
]

HARD_LABELS: dict[str, str] = {h["code"]: h["label"] for h in HARD_UPGRADES}


# ─── Soft thresholds (PRD §5.1 / §11) ────────────────────────────────────────

SOFT_THRESHOLDS: dict[str, int] = {
    # Universal
    "ebl_ml":                       500,
    "transfusion_units":            2,
    "vasopressor_sustained_min":    30,
    "spo2_hypoxia_threshold_pct":   90,
    "map_hypotension_threshold":    65,
    "map_hypotension_min_duration": 10,

    # Procedure-family-specific
    "cabg_cross_clamp_minutes":     90,
    "cabg_cpb_minutes":             120,
    "spinal_levels_aggregate":      4,
    "hip_femur_time_to_or_hours":   48,
}


# Human-readable labels for soft contributors (admin viewer + audit).
SOFT_LABELS: dict[str, str] = {
    "EBL_OVER_THRESHOLD":            "Estimated blood loss exceeds 500 ml threshold",
    "TRANSFUSION_AT_OR_OVER_2":      "Transfusion of 2 or more total units",
    "CONVERSION_MIS_TO_OPEN":        "Converted from minimally invasive to open",
    "SUSTAINED_HYPOTENSION":         "Sustained intra-operative hypotension (MAP <65, >10 min)",
    "SUSTAINED_VASOPRESSOR":         "Sustained vasopressor requirement (>30 min)",
    "HYPOXIA_EVENT":                 "Intra-operative hypoxia event (SpO2 <90% sustained)",
    "SIGNIFICANT_ARRHYTHMIA":        "Significant arrhythmia requiring intervention",
    "DIFFICULT_AIRWAY":              "Difficult airway encountered",
    "OR_TIME_OVER_P90":              "OR time exceeds hospital P90 for procedure family",
    "CABG_CROSS_CLAMP_OVER_90":      "CABG aortic cross-clamp time over 90 min",
    "CABG_CPB_OVER_120":             "CABG cardiopulmonary bypass time over 120 min",
    "SPINAL_FUSION_LEVELS_4_PLUS":   "Spinal fusion of 4 or more levels",
    "SPINAL_NEUROMONITORING_CHANGES": "Significant neuromonitoring changes",
    "LEJR_INTRAOPERATIVE_FRACTURE":  "Intra-operative fracture during LEJR",
    "BOWEL_CONTAMINATION_CLASS_3":   "Wound contamination class 3 (contaminated)",
    "HIP_FEMUR_TIME_TO_OR_OVER_48":  "Hip / femur fracture time-to-OR exceeds 48 h",
}


# Soft contributors grouped for the admin viewer.
SOFT_GROUPS: list[dict[str, Any]] = [
    {"name": "Universal physiology / events", "codes": [
        "EBL_OVER_THRESHOLD", "TRANSFUSION_AT_OR_OVER_2",
        "SUSTAINED_HYPOTENSION", "SUSTAINED_VASOPRESSOR",
        "HYPOXIA_EVENT", "SIGNIFICANT_ARRHYTHMIA",
        "DIFFICULT_AIRWAY", "CONVERSION_MIS_TO_OPEN",
        "OR_TIME_OVER_P90",
    ]},
    {"name": "CABG-specific", "codes": [
        "CABG_CROSS_CLAMP_OVER_90", "CABG_CPB_OVER_120",
    ]},
    {"name": "Spinal fusion-specific", "codes": [
        "SPINAL_FUSION_LEVELS_4_PLUS", "SPINAL_NEUROMONITORING_CHANGES",
    ]},
    {"name": "LEJR-specific", "codes": [
        "LEJR_INTRAOPERATIVE_FRACTURE",
    ]},
    {"name": "Major bowel-specific", "codes": [
        "BOWEL_CONTAMINATION_CLASS_3",
    ]},
    {"name": "Hip / femur fracture-specific", "codes": [
        "HIP_FEMUR_TIME_TO_OR_OVER_48",
    ]},
]


# ─── Per-family OR-time benchmarks (PRD §5.2) ────────────────────────────────
# National-benchmark NSQIP P90s; replace with hospital-observed P90 once
# 50+ cases per family are available.

PROCEDURE_P90_MINUTES: dict[str, int] = {
    "LEJR":               120,
    "CABG":               270,
    "SPINAL_FUSION":      240,
    "HIP_FEMUR_FRACTURE": 150,
    "MAJOR_BOWEL":        210,
}


# ─── Conservative default (PRD §7.4) ─────────────────────────────────────────

CONSERVATIVE_DEFAULT: dict[str, int] = {
    "threshold_hours_after_or_end": 24,
    "upgrade_steps":                1,
}


# ─── Extraction (PRD §6 / §11) ───────────────────────────────────────────────

EXTRACTION: dict[str, Any] = {
    "model_version":            "intraop-extractor@1.0.0",
    "prompt_version":           "v1",
    "low_confidence_threshold":  0.65,
    "mid_confidence_threshold":  0.85,
    "timeout_sec":               30,
    "max_pdf_size_mb":           25,
    "confidence_map":            {"HIGH": 0.95, "MED": 0.75, "LOW": 0.50},
}


# ─── Cron cadence (PRD §7.4) ─────────────────────────────────────────────────

OVERDUE_WATCHER_INTERVAL_SECONDS = 15 * 60   # 15 minutes


# ─── Tier ladder (TIER_1 → TIER_3) — local mirror of triage.types.Tier ───────

_TIER_ORDER: tuple[str, ...] = ("TIER_1", "TIER_2", "TIER_3")


def step_up(t: str, n: int) -> str:
    """Move `n` steps toward TIER_3 (capped at TIER_3)."""
    idx = _TIER_ORDER.index(t)
    return _TIER_ORDER[min(idx + max(n, 0), len(_TIER_ORDER) - 1)]


# ─── Public snapshot for the admin viewer ────────────────────────────────────

def get_config() -> dict[str, Any]:
    """JSON-serializable snapshot of the current intra-op tuning."""
    return {
        "modelVersion":          MODEL_VERSION,
        "tuningVersion":         TUNING_VERSION,
        "hardUpgrades":          [dict(h) for h in HARD_UPGRADES],
        "softThresholds":        dict(SOFT_THRESHOLDS),
        "softLabels":            dict(SOFT_LABELS),
        "softGroups":            [{"name": g["name"], "codes": list(g["codes"])} for g in SOFT_GROUPS],
        "procedureP90Minutes":   dict(PROCEDURE_P90_MINUTES),
        "conservativeDefault":   dict(CONSERVATIVE_DEFAULT),
        "extraction":            dict(EXTRACTION),
        "overdueWatcherIntervalSeconds": OVERDUE_WATCHER_INTERVAL_SECONDS,
        "combinationRules": [
            "Hard upgrade — any single hard contributor sets the proposed tier to TIER_3 regardless of other fields.",
            "Soft aggregation — each soft contributor adds one step; ≥2 soft contributors aggregate to TIER_3.",
            "Most-conservative-wins resolution — the final tier is the higher-rank of the current and proposed tiers; the intra-op pass never downgrades.",
            "Conservative default — when no lock is recorded within 24 h of OR end, the system applies a 1-step tier upgrade with reason INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT.",
        ],
    }
