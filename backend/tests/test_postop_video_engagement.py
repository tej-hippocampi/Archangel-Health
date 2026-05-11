"""
Unit tests for the post-op video engagement helpers (PRD §6).

Covers multi-session counting (≥60s gap rule), 90% completion event,
and the seven contributor flags computed by `determine_video_flags`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.scoring.video_engagement import (  # noqa: E402
    count_postop_video_sessions,
    determine_video_flags,
    last_postop_video_session_at,
)


def _ev(kind: str, when_iso: str, event_type: str = "PLAYED") -> dict:
    return {
        "video_kind": kind,
        "event_type": event_type,
        "session_id": "s1",
        "occurred_at": when_iso,
        "payload": {},
    }


def _t(secs: int) -> str:
    return (datetime(2026, 5, 1) + timedelta(seconds=secs)).isoformat()


# ─── Session counting ──────────────────────────────────────────────────────


def test_no_events_zero_sessions():
    assert count_postop_video_sessions([], video_kind="RED_FLAG") == 0


def test_single_play_one_session():
    events = [_ev("RED_FLAG", _t(0))]
    assert count_postop_video_sessions(events, video_kind="RED_FLAG") == 1


def test_rapid_scrubs_dedupe_into_one_session():
    """Two PLAYED events <60s apart count as a single session."""
    events = [_ev("RED_FLAG", _t(0)), _ev("RED_FLAG", _t(30))]
    assert count_postop_video_sessions(events, video_kind="RED_FLAG") == 1


def test_60s_gap_creates_new_session():
    events = [_ev("RED_FLAG", _t(0)), _ev("RED_FLAG", _t(60))]
    assert count_postop_video_sessions(events, video_kind="RED_FLAG") == 2


def test_more_than_60s_gap_creates_new_session():
    events = [_ev("RED_FLAG", _t(0)), _ev("RED_FLAG", _t(120))]
    assert count_postop_video_sessions(events, video_kind="RED_FLAG") == 2


def test_other_kind_excluded_from_count():
    events = [_ev("RED_FLAG", _t(0)), _ev("DIAGNOSIS_TREATMENT", _t(60))]
    assert count_postop_video_sessions(events, video_kind="RED_FLAG") == 1
    assert count_postop_video_sessions(events, video_kind="DIAGNOSIS_TREATMENT") == 1


def test_completed_events_do_not_create_sessions():
    """Only PLAYED events create sessions (PRD §6.1)."""
    events = [
        _ev("RED_FLAG", _t(0)),
        _ev("RED_FLAG", _t(120), event_type="COMPLETED"),
    ]
    assert count_postop_video_sessions(events, video_kind="RED_FLAG") == 1


def test_last_session_at_uses_latest_event():
    events = [_ev("RED_FLAG", _t(0)), _ev("RED_FLAG", _t(120))]
    assert last_postop_video_session_at(events, video_kind="RED_FLAG") == _t(120)


# ─── Engagement flags ──────────────────────────────────────────────────────


def _disch_iso() -> str:
    return datetime(2026, 5, 1).isoformat()


def _ev_after_discharge(kind: str, days: float, *, event_type: str = "PLAYED") -> dict:
    when = (datetime(2026, 5, 1) + timedelta(days=days)).isoformat()
    return _ev(kind, when, event_type=event_type)


def test_red_flag_viewed_by_d2_flag_fires():
    events = [_ev_after_discharge("RED_FLAG", 1)]
    flags = determine_video_flags(events, discharge_at_iso=_disch_iso(), days_since_discharge=3)
    assert flags["red_flag_video_viewed_by_d2"] is True
    assert flags["red_flag_video_viewed_by_d5"] is True


def test_red_flag_viewed_at_d3_does_not_count_as_d2():
    events = [_ev_after_discharge("RED_FLAG", 3)]
    flags = determine_video_flags(events, discharge_at_iso=_disch_iso(), days_since_discharge=4)
    assert flags["red_flag_video_viewed_by_d2"] is False
    assert flags["red_flag_video_viewed_by_d5"] is True


def test_red_flag_not_viewed_by_d5_fires_only_after_threshold():
    """The "not viewed" contributor only fires once we've passed the
    threshold day."""
    flags_d4 = determine_video_flags([], discharge_at_iso=_disch_iso(), days_since_discharge=4)
    assert flags_d4["red_flag_video_not_viewed_by_d5"] is False
    flags_d6 = determine_video_flags([], discharge_at_iso=_disch_iso(), days_since_discharge=6)
    assert flags_d6["red_flag_video_not_viewed_by_d5"] is True


def test_diagnosis_treatment_multiview_3_plus_at_d14():
    events = [
        _ev_after_discharge("DIAGNOSIS_TREATMENT", 1),
        _ev_after_discharge("DIAGNOSIS_TREATMENT", 5),  # ≥60s gap (4 days)
        _ev_after_discharge("DIAGNOSIS_TREATMENT", 9),
    ]
    flags = determine_video_flags(events, discharge_at_iso=_disch_iso(), days_since_discharge=14)
    assert flags["diag_treat_video_sessions_total"] == 3
    assert flags["diag_treat_video_viewed_3_or_more_by_d14"] is True


def test_diagnosis_treatment_not_viewed_by_d14():
    flags = determine_video_flags([], discharge_at_iso=_disch_iso(), days_since_discharge=15)
    assert flags["diag_treat_video_not_viewed_by_d14"] is True


def test_no_discharge_at_returns_safe_defaults():
    """If discharge_at is None (early in the episode) we don't crash and
    return all flags False."""
    flags = determine_video_flags([], discharge_at_iso=None, days_since_discharge=0)
    assert all(v is False for k, v in flags.items() if isinstance(v, bool))
