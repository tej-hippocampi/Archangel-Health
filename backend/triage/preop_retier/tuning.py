"""
Tuning constants for Pre-Op Re-Tier v1.0 (PRD §5.3 / §5.4 / §11).

Frozen Python dicts in v1; `tuning.json` persistence with versioning is
deferred. `get_config()` returns a JSON-serializable snapshot used by
the admin viewer at GET /admin/triage/preop-retier/config.
"""

from __future__ import annotations

from typing import Any


MODEL_VERSION = "preop-retier@1.0.0"
TUNING_VERSION = 1


# ─── 5.3 Soft contributor weights ────────────────────────────────────────────
# Signed integers. Positive = upgrade pressure; negative = downgrade pressure.

WEIGHTS: dict[str, int] = {
    # PAM (highest weight class per author guidance in PRD §5.3)
    "PAM_LEVEL_LOW":                          +5,   # also a hard escalator at T-24
    "PAM_LEVEL_MODERATE":                     +1,
    "PAM_LEVEL_HIGH":                         -3,
    "PAM_NOT_COMPLETED_BY_T_72":              +2,
    "PAM_NOT_COMPLETED_BY_T_24":              +3,   # additional, on top of T-72

    # Intake completion (mutual-exclusion ladders within milestones)
    "INTAKE_NOT_STARTED_BY_T_96":             +2,
    "INTAKE_NOT_STARTED_BY_T_72":             +3,   # replaces +2 (not additive)
    "INTAKE_STARTED_NOT_COMPLETE_BY_T_48":    +2,
    "INTAKE_NOT_COMPLETE_BY_T_24":            +4,
    "INTAKE_COMPLETE":                        -1,

    # Per-window surveys — mapped from existing scorer output
    "SURVEY_T_96_RED":                        +3,
    "SURVEY_T_96_ORANGE":                     +1,
    "SURVEY_T_96_GREEN":                       0,
    "SURVEY_T_96_MISSED":                     +2,

    "SURVEY_T_48_RED":                        +3,
    "SURVEY_T_48_ORANGE":                     +1,
    "SURVEY_T_48_GREEN":                       0,
    "SURVEY_T_48_MISSED":                     +2,

    "SURVEY_T_24_RED":                        +3,
    "SURVEY_T_24_ORANGE":                     +1,
    "SURVEY_T_24_GREEN":                       0,
    "SURVEY_T_24_MISSED":                     +2,

    # Engagement — pre-op video
    "VIDEO_VIEWED_AT_LEAST_ONCE_BY_T_72":     -1,
    "VIDEO_VIEWED_3_OR_MORE_BY_T_48":         -1,   # additional
    "VIDEO_NOT_VIEWED_BY_T_48":               +1,
    "VIDEO_NOT_VIEWED_BY_T_24":               +2,   # replaces +1 (not additive)

    # Engagement — battle-card
    "BATTLECARD_VIEWED_AT_LEAST_ONCE_BY_T_48": -1,
    "BATTLECARD_NOT_VIEWED_BY_T_24":           +1,

    # Cumulative engagement reward (caps to discourage gaming)
    "ENGAGEMENT_FULLY_COMPLETE_BY_T_24":       -1,
}


# Human-readable labels for the audit trail and admin viewer.
SOFT_LABELS: dict[str, str] = {
    "PAM_LEVEL_LOW":                           "PAM activation LOW",
    "PAM_LEVEL_MODERATE":                      "PAM activation MODERATE",
    "PAM_LEVEL_HIGH":                          "PAM activation HIGH",
    "PAM_NOT_COMPLETED_BY_T_72":               "PAM proxy not completed by T-72",
    "PAM_NOT_COMPLETED_BY_T_24":               "PAM proxy not completed by T-24",

    "INTAKE_NOT_STARTED_BY_T_96":              "Intake form not started by T-96",
    "INTAKE_NOT_STARTED_BY_T_72":              "Intake form not started by T-72",
    "INTAKE_STARTED_NOT_COMPLETE_BY_T_48":     "Intake started but not complete by T-48",
    "INTAKE_NOT_COMPLETE_BY_T_24":             "Intake not complete by T-24",
    "INTAKE_COMPLETE":                         "Intake form complete",

    "SURVEY_T_96_RED":                         "T-96 survey: red",
    "SURVEY_T_96_ORANGE":                      "T-96 survey: orange",
    "SURVEY_T_96_GREEN":                       "T-96 survey: green",
    "SURVEY_T_96_MISSED":                      "T-96 survey: missed",
    "SURVEY_T_48_RED":                         "T-48 survey: red",
    "SURVEY_T_48_ORANGE":                      "T-48 survey: orange",
    "SURVEY_T_48_GREEN":                       "T-48 survey: green",
    "SURVEY_T_48_MISSED":                      "T-48 survey: missed",
    "SURVEY_T_24_RED":                         "T-24 survey: red",
    "SURVEY_T_24_ORANGE":                      "T-24 survey: orange",
    "SURVEY_T_24_GREEN":                       "T-24 survey: green",
    "SURVEY_T_24_MISSED":                      "T-24 survey: missed",

    "VIDEO_VIEWED_AT_LEAST_ONCE_BY_T_72":      "Pre-op video viewed ≥1× by T-72",
    "VIDEO_VIEWED_3_OR_MORE_BY_T_48":          "Pre-op video viewed ≥3× by T-48",
    "VIDEO_NOT_VIEWED_BY_T_48":                "Pre-op video not viewed by T-48",
    "VIDEO_NOT_VIEWED_BY_T_24":                "Pre-op video not viewed by T-24",

    "BATTLECARD_VIEWED_AT_LEAST_ONCE_BY_T_48": "Battle-card viewed ≥1× by T-48",
    "BATTLECARD_NOT_VIEWED_BY_T_24":           "Battle-card not viewed by T-24",

    "ENGAGEMENT_FULLY_COMPLETE_BY_T_24":       "Fully engaged across all surfaces by T-24",
}


# Display grouping for the admin viewer.
WEIGHT_GROUPS: list[dict[str, Any]] = [
    {"name": "PAM activation", "codes": [
        "PAM_LEVEL_LOW", "PAM_LEVEL_MODERATE", "PAM_LEVEL_HIGH",
        "PAM_NOT_COMPLETED_BY_T_72", "PAM_NOT_COMPLETED_BY_T_24",
    ]},
    {"name": "Intake completion", "codes": [
        "INTAKE_NOT_STARTED_BY_T_96", "INTAKE_NOT_STARTED_BY_T_72",
        "INTAKE_STARTED_NOT_COMPLETE_BY_T_48", "INTAKE_NOT_COMPLETE_BY_T_24",
        "INTAKE_COMPLETE",
    ]},
    {"name": "Surveys — T-96", "codes": [
        "SURVEY_T_96_RED", "SURVEY_T_96_ORANGE", "SURVEY_T_96_GREEN", "SURVEY_T_96_MISSED",
    ]},
    {"name": "Surveys — T-48", "codes": [
        "SURVEY_T_48_RED", "SURVEY_T_48_ORANGE", "SURVEY_T_48_GREEN", "SURVEY_T_48_MISSED",
    ]},
    {"name": "Surveys — T-24", "codes": [
        "SURVEY_T_24_RED", "SURVEY_T_24_ORANGE", "SURVEY_T_24_GREEN", "SURVEY_T_24_MISSED",
    ]},
    {"name": "Engagement — pre-op video", "codes": [
        "VIDEO_VIEWED_AT_LEAST_ONCE_BY_T_72", "VIDEO_VIEWED_3_OR_MORE_BY_T_48",
        "VIDEO_NOT_VIEWED_BY_T_48", "VIDEO_NOT_VIEWED_BY_T_24",
    ]},
    {"name": "Engagement — battle-card", "codes": [
        "BATTLECARD_VIEWED_AT_LEAST_ONCE_BY_T_48", "BATTLECARD_NOT_VIEWED_BY_T_24",
    ]},
    {"name": "Cumulative engagement reward", "codes": [
        "ENGAGEMENT_FULLY_COMPLETE_BY_T_24",
    ]},
]


# ─── 5.2 Re-tier hard escalators (any one ⇒ TIER_3 regardless of initial) ────

HARD_ESCALATORS: list[dict[str, str]] = [
    {"code": "INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER",
     "label": "Intake disclosed: lives alone with no reliable caregiver",
     "source": "Intake interview"},
    {"code": "INTAKE_DISCLOSURE_HOUSING_INSTABILITY",
     "label": "Intake disclosed: housing instability",
     "source": "Intake interview"},
    {"code": "INTAKE_DISCLOSURE_FOOD_INSECURITY",
     "label": "Intake disclosed: food insecurity",
     "source": "Intake interview"},
    {"code": "INTAKE_DISCLOSURE_TRANSPORTATION_BARRIER_DAY_OF",
     "label": "Intake disclosed: no ride / no responsible adult on day of surgery",
     "source": "Intake interview"},
    {"code": "SURVEY_RED_FLAG_CRITICAL",
     "label": "T-48 or T-24 survey response on a critical red-flag item",
     "source": "Pre-op survey scorer"},
    {"code": "PAM_LEVEL_LOW_AT_T_24",
     "label": "PAM proxy LOW with no remediation window remaining (at T-24)",
     "source": "PAM proxy + cadence"},
]

HARD_LABELS: dict[str, str] = {h["code"]: h["label"] for h in HARD_ESCALATORS}


# ─── 5.4 Delta → tier mapping thresholds ─────────────────────────────────────

DELTA_THRESHOLDS: dict[str, int] = {
    "upgrade1_min":   3,    # delta ≥ +3 → upgrade 1 step
    "upgrade2_min":   6,    # delta ≥ +6 → upgrade 2 steps
    "downgrade1_max": -3,   # delta ≤ −3 → downgrade 1 step (subject to sticky guard)
}

STICKY_HARD_GUARD = True


# ─── 5.3 Soft cap ────────────────────────────────────────────────────────────

SOFT_CAP = 12   # clamp(raw_delta, -SOFT_CAP, +SOFT_CAP)


# ─── 5.6 Cadence checkpoints (encoded; cron itself out of scope for v1) ──────

CHECKPOINT_HOURS: list[int] = [96, 72, 48, 24, 0]


# ─── 4.2 PAM activation level cutoffs (PRD AC-4.3 + §13.7) ───────────────────

PAM_CUTOFFS: dict[str, float] = {
    "low":      55.1,
    "moderate": 67.0,
}


# ─── 6.x Engagement event policies ───────────────────────────────────────────

VIDEO_SESSION_GAP_SEC = 60        # PRD §6.1: ≥ 60 s gap defines a new session
VIDEO_COMPLETION_PCT = 90         # ≥ 90 % playback → preop_video_completed
BATTLECARD_DEDUP_MINUTES = 30     # PRD §6.2 dedup window


# ─── Public snapshot for the admin viewer ────────────────────────────────────

def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def get_config() -> dict[str, Any]:
    """JSON-serializable snapshot of the current re-tier tuning."""
    return {
        "modelVersion":             MODEL_VERSION,
        "tuningVersion":            TUNING_VERSION,
        "stickyHardGuard":          STICKY_HARD_GUARD,
        "softCap":                  SOFT_CAP,
        "deltaThresholds":          {_camel(k): v for k, v in DELTA_THRESHOLDS.items()},
        "checkpointHours":          list(CHECKPOINT_HOURS),
        "videoSessionGapSec":       VIDEO_SESSION_GAP_SEC,
        "videoCompletionPct":       VIDEO_COMPLETION_PCT,
        "battleCardDedupMinutes":   BATTLECARD_DEDUP_MINUTES,
        "pamCutoffs":               dict(PAM_CUTOFFS),
        "hardEscalators":           [dict(h) for h in HARD_ESCALATORS],
        "softWeights": {
            code: {"weight": WEIGHTS[code], "label": SOFT_LABELS.get(code, code)}
            for code in WEIGHTS
        },
        "softWeightGroups": [
            {"name": g["name"], "codes": list(g["codes"])} for g in WEIGHT_GROUPS
        ],
        "combinationRules": [
            "Mutual exclusion within a category — the most-specific contributor "
            "in a ladder replaces (not stacks with) the less-specific one.",
            "No double-counting across categories — surveys, PAM, intake "
            "completion, video, and battle-card are evaluated independently.",
            f"Soft cap — raw delta is clamped to ±{SOFT_CAP} before applying "
            "the delta → tier mapping.",
        ],
    }
