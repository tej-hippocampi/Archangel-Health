"""
Helpers for the post-op-relevant fields on the in-memory patient blob.

The post-op stage extends the same `app.state.patient_store` dict the
intra-op stage already manages (see
`backend/triage/intraop/patient_state.py`). It adds five extra keys per
patient:

    post_intraop_tier            : the tier in effect immediately after
                                    apply_intraop_reassessment ran
                                    (immutable floor for post-op re-tier)
    post_intraop_tier_at         : ISO timestamp of the snapshot
    discharge_at                 : ISO timestamp of post-op-day-0
                                    discharge from the hospital
    home_time_zone               : IANA tz the patient lives in (used
                                    by cron sends to localize 09:00,
                                    19:00, etc.); v1 default = UTC
    daily_checkin_missed_streak  : consecutive days of missed daily
                                    check-ins (PRD §4.3)
    postop_retier_last_run_at    : ISO timestamp denormalized for queue
    postop_retier_last_delta     : int snapshot of the last delta
    postop_retier_top_reasons    : top-3 reasons rendered in the queue
    care_goal_changed            : bool — suppresses missed-engagement
                                    contributors when True (PRD §17.7)
    episode_status               : "ACTIVE" | "INTERRUPTED" | "CLOSED"

These keys are populated lazily by `ensure_postop_patient_state` so
post-op endpoints can read them immediately even before any signal
has fired.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from triage.types import Tier


_TIER_RANK = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3}


def ensure_postop_patient_state(patient: dict) -> dict:
    """Mutate the patient blob in place so it has every post-op field
    we depend on. Idempotent. Returns the patient dict for chaining."""
    if patient.get("post_intraop_tier") not in ("TIER_1", "TIER_2", "TIER_3", None):
        patient["post_intraop_tier"] = None
    patient.setdefault("post_intraop_tier", None)
    patient.setdefault("post_intraop_tier_at", None)
    patient.setdefault("discharge_at", None)
    patient.setdefault("home_time_zone", "UTC")
    patient.setdefault("daily_checkin_missed_streak", 0)
    patient.setdefault("postop_retier_last_run_at", None)
    patient.setdefault("postop_retier_last_delta", None)
    patient.setdefault("postop_retier_top_reasons", None)
    patient.setdefault("postop_retier_last_tier", None)
    patient.setdefault("postop_retier_version", None)
    patient.setdefault("postop_retier_tuning_version", None)
    patient.setdefault("care_goal_changed", False)
    patient.setdefault("episode_status", "ACTIVE")
    return patient


def get_post_intraop_tier(patient: dict) -> Optional[Tier]:
    ensure_postop_patient_state(patient)
    return patient.get("post_intraop_tier")


def set_post_intraop_tier(patient: dict, tier: Tier) -> None:
    """Upward-only floor write. Mirrors the same invariant enforced in
    `apply_intraop_reassessment` so any caller (admin reopen → re-lock,
    test fixtures) cannot accidentally lower the floor."""
    ensure_postop_patient_state(patient)
    prior = patient.get("post_intraop_tier")
    if prior is None or _TIER_RANK[tier] > _TIER_RANK.get(prior, 0):
        patient["post_intraop_tier"] = tier
    if not patient.get("post_intraop_tier_at"):
        patient["post_intraop_tier_at"] = datetime.utcnow().replace(microsecond=0).isoformat()


def bump_daily_checkin_missed_streak(patient: dict) -> int:
    ensure_postop_patient_state(patient)
    patient["daily_checkin_missed_streak"] = int(patient.get("daily_checkin_missed_streak") or 0) + 1
    return int(patient["daily_checkin_missed_streak"])


def reset_daily_checkin_missed_streak(patient: dict) -> None:
    ensure_postop_patient_state(patient)
    patient["daily_checkin_missed_streak"] = 0


def get_daily_checkin_missed_streak(patient: dict) -> int:
    ensure_postop_patient_state(patient)
    return int(patient.get("daily_checkin_missed_streak") or 0)


def set_discharge_at(patient: dict, ts_iso: str) -> None:
    ensure_postop_patient_state(patient)
    patient["discharge_at"] = ts_iso


def update_postop_retier_denorm(
    patient: dict,
    *,
    last_run_at: str,
    last_delta: int,
    top_reasons: list[dict[str, Any]],
    last_tier: Tier,
    model_version: str,
    tuning_version: int,
) -> None:
    """Denormalize the most recent re-tier outcome onto the patient blob
    for cheap rendering on the doctor / admin queue (PRD README §3.5)."""
    ensure_postop_patient_state(patient)
    patient["postop_retier_last_run_at"] = last_run_at
    patient["postop_retier_last_delta"] = int(last_delta)
    patient["postop_retier_top_reasons"] = list(top_reasons[:3])
    patient["postop_retier_last_tier"] = last_tier
    patient["postop_retier_version"] = model_version
    patient["postop_retier_tuning_version"] = int(tuning_version)


def to_public(patient: dict) -> dict[str, Any]:
    """Snapshot of the post-op-relevant subset for API responses.
    Intentionally excludes wound-photo-related fields (out of scope v1)."""
    ensure_postop_patient_state(patient)
    return {
        "postIntraOpTier":              patient.get("post_intraop_tier"),
        "postIntraOpTierAt":            patient.get("post_intraop_tier_at"),
        "dischargeAt":                  patient.get("discharge_at"),
        "homeTimeZone":                 patient.get("home_time_zone"),
        "dailyCheckinMissedStreak":     patient.get("daily_checkin_missed_streak", 0),
        "postOpReTierLastRunAt":        patient.get("postop_retier_last_run_at"),
        "postOpReTierLastDelta":        patient.get("postop_retier_last_delta"),
        "postOpReTierTopReasons":       patient.get("postop_retier_top_reasons"),
        "postOpReTierLastTier":         patient.get("postop_retier_last_tier"),
        "postOpReTierVersion":          patient.get("postop_retier_version"),
        "postOpReTierTuningVersion":    patient.get("postop_retier_tuning_version"),
        "careGoalChanged":              bool(patient.get("care_goal_changed")),
        "episodeStatus":                patient.get("episode_status", "ACTIVE"),
    }
