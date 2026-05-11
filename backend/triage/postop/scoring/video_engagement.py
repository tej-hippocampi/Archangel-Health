"""
Post-op video engagement helpers (PRD §6).

Two videos delivered post-op:
  - DIAGNOSIS_TREATMENT (recommended D1–D5; multiview reward through D14)
  - RED_FLAG            (recommended D1–D2; missed by D5 contributes positively)

The patient app emits `postop_video_played` on every distinct play
session and `postop_video_completed` on ≥90% completion. A new session
is defined by a ≥60s gap between consecutive `PLAYED` events for the
same `(patient, video_kind)` pair, deduping rapid scrubs.

The contributor flags returned by `determine_video_flags` are read
directly by the post-op re-tier delta computation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from triage.postop.tuning import VIDEO_CONFIG
from triage.postop.types import VideoKind


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def count_postop_video_sessions(
    events: list[dict[str, Any]],
    *,
    video_kind: Optional[VideoKind] = None,
    session_gap_seconds: Optional[int] = None,
) -> int:
    """Count distinct play sessions across the event stream.

    Sessions are defined by a ≥`session_gap_seconds` gap between
    consecutive `PLAYED` events for the same `video_kind`. Rapid scrubs
    within the same play do not create new sessions.

    PRD §6.3 AC-6.1: rapid scrubs deduplicate; ≥60s gaps separate.
    """
    gap = int(session_gap_seconds if session_gap_seconds is not None else VIDEO_CONFIG["session_gap_seconds"])

    relevant = [
        e for e in events or []
        if e.get("event_type") == "PLAYED" and (video_kind is None or e.get("video_kind") == video_kind)
    ]
    relevant.sort(key=lambda e: _parse_iso(e.get("occurred_at")) or datetime.min)

    if not relevant:
        return 0

    sessions = 1
    last_ts = _parse_iso(relevant[0].get("occurred_at"))
    for e in relevant[1:]:
        ts = _parse_iso(e.get("occurred_at"))
        if ts is None or last_ts is None:
            continue
        if (ts - last_ts) >= timedelta(seconds=gap):
            sessions += 1
        last_ts = ts
    return sessions


def last_postop_video_session_at(
    events: list[dict[str, Any]],
    *,
    video_kind: Optional[VideoKind] = None,
) -> Optional[str]:
    relevant = [
        e for e in events or []
        if (video_kind is None or e.get("video_kind") == video_kind)
    ]
    if not relevant:
        return None
    relevant.sort(key=lambda e: _parse_iso(e.get("occurred_at")) or datetime.min)
    return relevant[-1].get("occurred_at")


def _earliest_event_day(
    events: list[dict[str, Any]],
    *,
    video_kind: VideoKind,
    discharge_at: Optional[datetime],
    event_type: Optional[str] = None,
) -> Optional[int]:
    """Return the days-since-discharge integer of the earliest event for
    `video_kind` (and optional `event_type`)."""
    if discharge_at is None:
        return None
    matches = [
        e for e in events or []
        if e.get("video_kind") == video_kind
        and (event_type is None or e.get("event_type") == event_type)
    ]
    if not matches:
        return None
    timestamps = [
        ts for ts in (_parse_iso(e.get("occurred_at")) for e in matches) if ts is not None
    ]
    if not timestamps:
        return None
    earliest = min(timestamps)
    delta = (earliest - discharge_at).days
    return max(delta, 0)


def determine_video_flags(
    events: list[dict[str, Any]],
    *,
    discharge_at_iso: Optional[str],
    days_since_discharge: int,
) -> dict[str, bool]:
    """Compute the seven engagement contributor flags (PRD §6.2).

    Returns a dict with keys:
      - red_flag_video_viewed_by_d2
      - red_flag_video_viewed_by_d5
      - red_flag_video_not_viewed_by_d5
      - diag_treat_video_viewed_by_d5
      - diag_treat_video_viewed_3_or_more_by_d14
      - diag_treat_video_not_viewed_by_d14
      - diag_treat_video_sessions_total
    """
    cfg = VIDEO_CONFIG
    discharge = _parse_iso(discharge_at_iso)

    rf_first_day = _earliest_event_day(events, video_kind="RED_FLAG", discharge_at=discharge, event_type="PLAYED")
    dt_first_day = _earliest_event_day(events, video_kind="DIAGNOSIS_TREATMENT", discharge_at=discharge, event_type="PLAYED")

    rf_viewed_d2 = rf_first_day is not None and rf_first_day <= int(cfg["red_flag_early_day"])
    rf_viewed_d5 = rf_first_day is not None and rf_first_day <= int(cfg["red_flag_missed_day"])
    rf_not_viewed_by_d5 = (
        days_since_discharge > int(cfg["red_flag_missed_day"]) and not rf_viewed_d5
    )

    dt_viewed_d5 = dt_first_day is not None and dt_first_day <= int(cfg["diagnosis_treatment_early_day"])
    dt_sessions = count_postop_video_sessions(events, video_kind="DIAGNOSIS_TREATMENT")
    dt_multi_d14 = (
        dt_first_day is not None
        and dt_sessions >= int(cfg["diagnosis_treatment_multiview_min"])
        and (days_since_discharge >= int(cfg["diagnosis_treatment_missed_day"]))
    )
    dt_not_viewed_by_d14 = (
        days_since_discharge > int(cfg["diagnosis_treatment_missed_day"])
        and (dt_first_day is None)
    )

    return {
        "red_flag_video_viewed_by_d2":              bool(rf_viewed_d2),
        "red_flag_video_viewed_by_d5":              bool(rf_viewed_d5),
        "red_flag_video_not_viewed_by_d5":          bool(rf_not_viewed_by_d5),
        "diag_treat_video_viewed_by_d5":            bool(dt_viewed_d5),
        "diag_treat_video_viewed_3_or_more_by_d14": bool(dt_multi_d14),
        "diag_treat_video_not_viewed_by_d14":       bool(dt_not_viewed_by_d14),
        "diag_treat_video_sessions_total":          int(dt_sessions),
    }
