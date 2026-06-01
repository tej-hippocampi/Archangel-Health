"""
Tuning constants for Post-Op Scoring & Re-Tiering v1.0 (PRD §15).

Frozen Python dicts in v1; persistence to `tuning.json` with hot-swap
is deferred. `get_config()` returns the JSON-serializable snapshot
rendered by the admin viewer at GET /admin/triage/postop/config.

Wound-photo-related entries are intentionally omitted (PRD §8 is out
of scope v1 per author guidance). The `disabled_in_v1` block surfaces
that decision in the admin viewer for documentation parity with the
PRD.
"""

from __future__ import annotations

from typing import Any


MODEL_VERSION = "postop-retier@1.1.0"
TUNING_VERSION = 2


# ─── Hard escalators (PRD §10.2) — any one ⇒ TIER_3 ──────────────────────────

HARD_ESCALATORS: list[dict[str, Any]] = [
    {
        "code": "PATIENT_SELF_FLAG_ACTIVE",
        "label": "Active patient self-flag (something doesn't feel right)",
        "source": "Self-flag flow",
    },
    {
        "code": "NEW_RED_FLAG_SYMPTOM",
        "label": "New red-flag symptom from daily check-in or survey Section A",
        "source": "Daily check-in item 8 / D-X survey",
    },
    {
        "code": "LOST_CONTACT_TIER3",
        "label": "Tier 3 patient silent across all channels for ≥24 h",
        "source": "Computed across check-in / ping / survey / video / self-flag",
    },
    {
        "code": "LOST_CONTACT_GENERAL",
        "label": "Any patient silent across all channels for ≥72 h",
        "source": "Computed across check-in / ping / survey / video / self-flag",
    },
    {
        "code": "DAY_X_SURVEY_RED_AND_RED_FLAG",
        "label": "D7 / D14 survey RED total AND any item-level red-flag chip",
        "source": "D7 / D14 survey",
    },
    {
        "code": "MULTIPLE_INCISION_FLAGS",
        "label": "≥2 incision-flag chips on the same submission, or any single chip on 3 consecutive days",
        "source": "Daily check-in item 5",
    },
    {
        "code": "CARE_COMPANION_RED_FLAG_TIER_3",
        "label": "Care Companion semantic escalation tier 3 with an unresolved chat:semantic* row",
        "source": "Care Companion chat (semantic escalation LLM)",
    },
    {
        "code": "TEACHBACK_FAILED_RED_FLAG_POSTLOOP",
        "label": "Patient cannot state red flags / emergency action after re-teaching",
        "source": "Teach-back (post-loop)",
    },
]

HARD_LABELS: dict[str, str] = {h["code"]: h["label"] for h in HARD_ESCALATORS}


# ─── Soft positive contributors (PRD §10.3.a) ────────────────────────────────
# Wound-photo-related contributors (WOUND_PHOTO_NOT_SUBMITTED_BY_D7, etc.)
# are intentionally absent — wound-photo feature excluded in v1.

POSTOP_POSITIVE_WEIGHTS: dict[str, int] = {
    # Daily check-in
    "CHECKIN_TIER_RED":                          3,
    "CHECKIN_TIER_ORANGE":                       1,
    "CHECKIN_MISSED":                            1,   # capped at +5 over rolling 7-day window
    "CHECKIN_MISSED_STREAK_3":                   2,
    "WOUND_CONCERN_FROM_CHECKIN":                2,
    "PAIN_TRAJECTORY_WORSE":                     1,

    # Day surveys
    "SURVEY_DAY_7_RED":                          3,
    "SURVEY_DAY_7_ORANGE":                       1,
    "SURVEY_DAY_7_MISSED":                       2,
    "SURVEY_DAY_14_RED":                         3,
    "SURVEY_DAY_14_ORANGE":                      1,
    "SURVEY_DAY_14_MISSED":                      2,
    "SURVEY_DAY_30_RED":                         2,
    "SURVEY_DAY_30_ORANGE":                      1,
    "SURVEY_DAY_30_MISSED":                      1,

    # Video engagement (negatives only — non-viewing only)
    "RED_FLAG_VIDEO_NOT_VIEWED_BY_D5":           2,
    "DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14": 1,

    # Med adherence (rolling 7-day)
    "MED_ADHERENCE_LOW":                         2,
    "MED_ADHERENCE_NON_RESPONSE_STREAK_3":       2,

    # Teach-back (post-loop outcomes only)
    "TEACHBACK_FAILED_MED_POSTLOOP":             2,
    "TEACHBACK_FAILED_CRITICAL_POSTLOOP":        2,
    "TEACHBACK_NOT_COMPLETED_BY_D5":             1,

    # ─── Care Companion (Triage Suite Pass 3 §3.3) ─────────────────────
    "CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2": 2,
    "CARE_COMPANION_NEVER_USED_BY_D7":           1,
}


# Cap applied to the rolling-window CHECKIN_MISSED contribution before
# CHECKIN_MISSED_STREAK_3 is added. Mirrors PRD §10.3.a comment.
CHECKIN_MISSED_ROLLING_CAP_POINTS = 5


# Human-readable labels for the admin viewer + audit row reasons.
POSTOP_POSITIVE_LABELS: dict[str, str] = {
    "CHECKIN_TIER_RED":                          "Daily check-in tiered RED",
    "CHECKIN_TIER_ORANGE":                       "Daily check-in tiered ORANGE",
    "CHECKIN_MISSED":                            "Daily check-in missed (per day, rolling 7-day)",
    "CHECKIN_MISSED_STREAK_3":                   "Daily check-ins missed 3+ days in a row",
    "WOUND_CONCERN_FROM_CHECKIN":                "Single incision-flag chip on daily check-in",
    "PAIN_TRAJECTORY_WORSE":                     "Pain trajectory worse + above expected curve",
    "SURVEY_DAY_7_RED":                          "Day 7 survey RED",
    "SURVEY_DAY_7_ORANGE":                       "Day 7 survey ORANGE",
    "SURVEY_DAY_7_MISSED":                       "Day 7 survey window missed",
    "SURVEY_DAY_14_RED":                         "Day 14 survey RED",
    "SURVEY_DAY_14_ORANGE":                      "Day 14 survey ORANGE",
    "SURVEY_DAY_14_MISSED":                      "Day 14 survey window missed",
    "SURVEY_DAY_30_RED":                         "Day 30 survey RED",
    "SURVEY_DAY_30_ORANGE":                      "Day 30 survey ORANGE",
    "SURVEY_DAY_30_MISSED":                      "Day 30 survey window missed",
    "RED_FLAG_VIDEO_NOT_VIEWED_BY_D5":           "Red-flag video not viewed by day 5",
    "DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14": "Diagnosis & treatment video not viewed by day 14",
    "MED_ADHERENCE_LOW":                         "Med adherence low (<5 of last 7 days = Yes)",
    "MED_ADHERENCE_NON_RESPONSE_STREAK_3":       "Med adherence non-response 3+ days in a row",
    "TEACHBACK_FAILED_MED_POSTLOOP":             "Teach-back post-loop medication misunderstanding",
    "TEACHBACK_FAILED_CRITICAL_POSTLOOP":        "Teach-back post-loop critical comprehension miss",
    "TEACHBACK_NOT_COMPLETED_BY_D5":             "Teach-back not completed by day 5",
    "CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2": "Care Companion tier-2 semantic escalation in last 24h",
    "CARE_COMPANION_NEVER_USED_BY_D7":           "Patient has not used the Care Companion chat by day 7 post-discharge",
}


POSTOP_POSITIVE_GROUPS: list[dict[str, Any]] = [
    {"name": "Daily check-in", "codes": [
        "CHECKIN_TIER_RED", "CHECKIN_TIER_ORANGE",
        "CHECKIN_MISSED", "CHECKIN_MISSED_STREAK_3",
        "WOUND_CONCERN_FROM_CHECKIN", "PAIN_TRAJECTORY_WORSE",
    ]},
    {"name": "Day-X surveys", "codes": [
        "SURVEY_DAY_7_RED", "SURVEY_DAY_7_ORANGE", "SURVEY_DAY_7_MISSED",
        "SURVEY_DAY_14_RED", "SURVEY_DAY_14_ORANGE", "SURVEY_DAY_14_MISSED",
        "SURVEY_DAY_30_RED", "SURVEY_DAY_30_ORANGE", "SURVEY_DAY_30_MISSED",
    ]},
    {"name": "Post-op videos", "codes": [
        "RED_FLAG_VIDEO_NOT_VIEWED_BY_D5",
        "DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14",
    ]},
    {"name": "Med adherence (rolling 7-day)", "codes": [
        "MED_ADHERENCE_LOW", "MED_ADHERENCE_NON_RESPONSE_STREAK_3",
    ]},
    {"name": "Teach-back (post-loop)", "codes": [
        "TEACHBACK_FAILED_MED_POSTLOOP",
        "TEACHBACK_FAILED_CRITICAL_POSTLOOP",
        "TEACHBACK_NOT_COMPLETED_BY_D5",
    ]},
    {"name": "Care Companion (Pass 3)", "codes": [
        "CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2",
        "CARE_COMPANION_NEVER_USED_BY_D7",
    ]},
]


# Engagement-audit (PRD §10.3.b) — detected and logged but contribute 0.
# Wound-photo entries omitted in v1.
POSTOP_ENGAGEMENT_AUDIT_FLAGS: list[str] = [
    "RED_FLAG_VIDEO_VIEWED_BY_D2",
    "DIAGNOSIS_TREATMENT_VIDEO_VIEWED_BY_D5",
    "DIAGNOSIS_TREATMENT_VIDEO_VIEWED_3_PLUS_BY_D14",
    "MED_ADHERENCE_HIGH",
    "TEACHBACK_PASSED_ALL",
    "CARE_COMPANION_ACTIVE_LAST_7D",
]

POSTOP_ENGAGEMENT_AUDIT_LABELS: dict[str, str] = {
    "RED_FLAG_VIDEO_VIEWED_BY_D2":                  "Red-flag video viewed by day 2",
    "DIAGNOSIS_TREATMENT_VIDEO_VIEWED_BY_D5":       "Diagnosis & treatment video viewed by day 5",
    "DIAGNOSIS_TREATMENT_VIDEO_VIEWED_3_PLUS_BY_D14": "Diagnosis & treatment video viewed 3+ times by day 14",
    "MED_ADHERENCE_HIGH":                           "Med adherence high (≥6 of last 7 days = Yes)",
    "TEACHBACK_PASSED_ALL":                         "Teach-back completed with all items passed",
    "CARE_COMPANION_ACTIVE_LAST_7D":                "Care Companion chat used ≥2 times in the last 7 days",
}


# ─── Soft delta cap + threshold mapping (PRD §10.3.a / §10.4) ────────────────

POSTOP_DELTA_CAP = 12

DELTA_THRESHOLDS: dict[str, int] = {
    "upgrade_1_min": 3,
    "upgrade_2_min": 6,
}


# ─── Daily check-in scoring (PRD §4.2) ───────────────────────────────────────

CHECKIN_CONFIG: dict[str, Any] = {
    "window_hours": 36,
    "tier_thresholds": {"green_min": 85, "orange_min": 70},
    "item_weights": {
        # Mapping of item key → relative weight (sums to 100).
        "pain_nrs":          20,
        "pain_trajectory":   10,
        "fever":             15,
        "incision_change":   5,
        "incision_flags":    15,
        "nausea":            5,
        "eating_drinking":   5,
        "red_flag_symptoms": 15,
        "walking":           5,
        "worry_level":       5,
    },
    # Pain expected curve: NRS values above this floor on a given
    # episode-day count as "above expected curve" for PAIN_TRAJECTORY_WORSE.
    # Default uses a conservative descending curve (PRD §4.2 item 2).
    "pain_expected_curve_floor": {
        # episode_day → max expected NRS that does NOT count as "above curve"
        1: 8, 2: 8, 3: 7, 4: 7, 5: 6, 6: 6, 7: 5,
        8: 5, 9: 5, 10: 5, 11: 4, 12: 4, 13: 4, 14: 4,
        15: 4, 16: 3, 17: 3, 18: 3, 19: 3, 20: 3,
        21: 3, 22: 2, 23: 2, 24: 2, 25: 2, 26: 2, 27: 2, 28: 2, 29: 2, 30: 2,
    },
}


# ─── Day-X surveys scoring (PRD §5.2) ────────────────────────────────────────

SURVEY_CONFIG: dict[str, Any] = {
    "window_hours": 48,
    # Per-day section weights A/B/C/D summing to 100.
    "section_weights": {
        7:  {"A": 40, "B": 20, "C": 25, "D": 15},
        14: {"A": 30, "B": 35, "C": 20, "D": 15},
        30: {"A": 20, "B": 45, "C": 15, "D": 20},
    },
    "tier_thresholds": {
        7:  {"green_min": 85, "orange_min": 70},
        14: {"green_min": 85, "orange_min": 72},
        30: {"green_min": 80, "orange_min": 65},
    },
    # Procedure-family-specific Section B PROM source.
    "section_b_proms": {
        "LEJR":               "KOOS_HOOS_JR",
        "SPINAL_FUSION":      "ODI",
        "CABG":               "SF12_PCS",
        "MAJOR_BOWEL":        "SF12_PCS",
        "HIP_FEMUR_FRACTURE": "KOOS_HOOS_JR",
    },
}


# ─── Med adherence (PRD §7.2 / §15) ──────────────────────────────────────────

MED_ADHERENCE_CONFIG: dict[str, Any] = {
    "rolling_window_days":      7,
    "high_min_yes":             6,
    "low_max_yes":              4,
    "non_response_streak_days": 3,
    "ping_time_local":          "19:00",
    "response_window_end_local": "23:00",
}


# ─── Video engagement (PRD §6 / §15) ─────────────────────────────────────────

VIDEO_CONFIG: dict[str, Any] = {
    "red_flag_early_day":             2,
    "red_flag_missed_day":             5,
    "diagnosis_treatment_early_day":  5,
    "diagnosis_treatment_missed_day": 14,
    "diagnosis_treatment_multiview_min": 3,
    "session_gap_seconds":            60,
    "completion_pct":                 0.90,
}


# ─── Lost contact (PRD §10.2 / §15) ──────────────────────────────────────────

LOST_CONTACT_CONFIG: dict[str, int] = {
    "tier3_hours":   24,
    "general_hours": 72,
}


# ─── Cron cadence (PRD §10.6) ────────────────────────────────────────────────

CRON_CONFIG: dict[str, Any] = {
    "daily_checkin_send_local":         "09:00",
    "daily_checkin_window_hours":       36,
    "survey_send_local":                "09:00",
    "survey_window_hours":              48,
    "med_ping_local":                   "19:00",
    "med_non_response_close_local":     "23:00",
    "checkin_missed_watcher_minutes":   30,
    "survey_missed_watcher_minutes":    60,
    "lost_contact_watcher_minutes":     60,
    "nightly_retier_local":             "02:00",
    "scheduler_tick_seconds":           300,
}


# ─── Disabled-in-v1 surface (PRD §0 / §15) ───────────────────────────────────

DISABLED_IN_V1: dict[str, Any] = {
    "rpm_enabled":             False,
    "care_companion_enabled":  True,   # Triage Suite Pass 3 — flipped on
    "wound_photo_feature":     False,
}


# ─── Tier ladder ─────────────────────────────────────────────────────────────

_TIER_ORDER: tuple[str, ...] = ("TIER_1", "TIER_2", "TIER_3")


def step_up(t: str, n: int) -> str:
    """Move `n` steps toward TIER_3 (capped at TIER_3)."""
    idx = _TIER_ORDER.index(t)
    return _TIER_ORDER[min(idx + max(n, 0), len(_TIER_ORDER) - 1)]


def get_config() -> dict[str, Any]:
    """JSON-serializable snapshot of the current post-op tuning."""
    return {
        "modelVersion":              MODEL_VERSION,
        "tuningVersion":             TUNING_VERSION,
        "hardEscalators":            [dict(h) for h in HARD_ESCALATORS],
        "positiveWeights":           dict(POSTOP_POSITIVE_WEIGHTS),
        "positiveLabels":            dict(POSTOP_POSITIVE_LABELS),
        "positiveGroups":            [{"name": g["name"], "codes": list(g["codes"])} for g in POSTOP_POSITIVE_GROUPS],
        "engagementAuditFlags":      list(POSTOP_ENGAGEMENT_AUDIT_FLAGS),
        "engagementAuditLabels":     dict(POSTOP_ENGAGEMENT_AUDIT_LABELS),
        "deltaCap":                  POSTOP_DELTA_CAP,
        "deltaThresholds":           dict(DELTA_THRESHOLDS),
        "checkinMissedRollingCapPoints": CHECKIN_MISSED_ROLLING_CAP_POINTS,
        "checkinConfig":             dict(CHECKIN_CONFIG),
        "surveyConfig":              dict(SURVEY_CONFIG),
        "medAdherenceConfig":        dict(MED_ADHERENCE_CONFIG),
        "videoConfig":               dict(VIDEO_CONFIG),
        "lostContactConfig":         dict(LOST_CONTACT_CONFIG),
        "cronConfig":                dict(CRON_CONFIG),
        "disabledInV1":              dict(DISABLED_IN_V1),
        "combinationRules": [
            "Hard escalator — any single hard contributor sets the proposed tier to TIER_3 regardless of other fields.",
            "Soft delta is unsigned; positive contributors only. Sum is clamped to deltaCap (default 12).",
            "≥+3 → upgrade by 1 step; ≥+6 → upgrade by 2 steps.",
            "Upward-only — the post-op stage never algorithmically downgrades; the floor is post_intraop_tier.",
            "Engagement-audit flags are detected and logged but contribute 0 in v1.",
            "Wound-photo feature and RPM signals are explicitly disabled in v1.",
            "Care Companion (Pass 3): hard escalator on tier-3 verdict + open chat:semantic* row, +2 on tier-2 verdict in last 24h, audit-only when ≥2 chat sessions in 7d, +1 when never used by day 7.",
        ],
    }
