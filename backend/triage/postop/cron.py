"""
Cron passes for the Post-Op Scoring stage (PRD §10.6).

Each `run_*_pass` function is a side-effecty pure function over the
`TeamStore` + `_patient_store`. The `_postop_*_loop` async wrappers in
`backend/main.py` schedule them.

Cron passes:

  - run_daily_checkin_send_pass    : at 09:00 local each day, send the
                                     daily check-in to every patient in
                                     the post-op phase.
  - run_dayx_survey_send_pass      : on D7 / D14 / D30, send the survey.
  - run_med_adherence_send_pass    : at 19:00 local, send med-adherence ping.
  - run_med_adherence_close_pass   : at 23:00 local, close non-responses.
  - run_checkin_missed_watcher     : marks check-ins past 36h as missed
                                     and bumps the missed-streak counter.
  - run_dayx_missed_watcher        : marks D-X surveys past 48h as missed.
  - run_lost_contact_watcher       : recomputes lost-contact flags and
                                     fires re-tier when they trip.
  - run_nightly_retier_batch       : at 02:00, recompute every active
                                     post-op episode (state changes
                                     anchored to the calendar day, not
                                     individual signal commits).

Every pass is idempotent — repeated invocation in the same window does
not duplicate sends or events.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from triage.postop.apply import apply_postop_retier
from triage.postop.locks import with_patient_lock
from triage.postop.patient_state import (
    bump_daily_checkin_missed_streak,
    ensure_postop_patient_state,
)
from triage.postop.tuning import CRON_CONFIG


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _episode_day(patient: dict, *, now: Optional[datetime] = None) -> int:
    """1-indexed episode-day computed from `discharge_at`. Day 1 = the
    UTC day after discharge."""
    now = now or datetime.utcnow()
    discharge_at = patient.get("discharge_at")
    if not discharge_at:
        return 1
    try:
        ts = datetime.fromisoformat(str(discharge_at).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return 1
    return max((now - ts).days + 1, 1)


def _post_op_patients(patient_store: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for pid, patient in (patient_store or {}).items():
        if not isinstance(patient, dict):
            continue
        if patient.get("phase") != "post_op":
            continue
        out.append((pid, patient))
    return out


# ─── Send passes ────────────────────────────────────────────────────────────


def run_daily_checkin_send_pass(*, patient_store: dict, team_store) -> int:
    """Send the daily check-in to every active post-op patient. Returns
    the count of new sends actually written (idempotent: existing day
    rows are skipped)."""
    sent = 0
    for pid, patient in _post_op_patients(patient_store):
        ensure_postop_patient_state(patient)
        day = _episode_day(patient)
        if not team_store.has_daily_checkin_send(pid, day):
            if team_store.record_daily_checkin_send(patient_id=pid, episode_day=day):
                sent += 1
                team_store.log_event(
                    patient_id=pid,
                    event_type="POSTOP_DAILY_CHECKIN_SENT",
                    payload={"episodeDay": day},
                )
    return sent


def run_dayx_survey_send_pass(*, patient_store: dict, team_store) -> int:
    """Send D7/D14/D30 surveys on the appropriate days. Idempotent —
    upsert_dayx_survey_send only creates on first call."""
    sent = 0
    for pid, patient in _post_op_patients(patient_store):
        ensure_postop_patient_state(patient)
        day = _episode_day(patient)
        for d in (7, 14, 30):
            if day != d:
                continue
            existing = team_store.get_dayx_survey(pid, d)
            if existing:
                continue
            team_store.upsert_dayx_survey_send(
                patient_id=pid, day=d,
                procedure_family=patient.get("anchor_procedure_family"),
            )
            sent += 1
            team_store.log_event(
                patient_id=pid,
                event_type=f"POSTOP_DAY{d}_SURVEY_SENT",
                payload={"day": d, "episodeDay": day},
            )
    return sent


def run_med_adherence_send_pass(*, patient_store: dict, team_store) -> int:
    """Send the 19:00 local ping to every post-op patient on a med plan."""
    sent = 0
    for pid, patient in _post_op_patients(patient_store):
        ensure_postop_patient_state(patient)
        day = _episode_day(patient)
        if not team_store.has_med_adherence_ping(pid, day):
            if team_store.record_med_adherence_ping(patient_id=pid, episode_day=day):
                sent += 1
                team_store.log_event(
                    patient_id=pid,
                    event_type="POSTOP_MED_ADHERENCE_PING_SENT",
                    payload={"episodeDay": day},
                )
    return sent


# ─── Watcher passes ────────────────────────────────────────────────────────


def run_med_adherence_close_pass(
    *,
    team_store,
    now: Optional[datetime] = None,
    response_window_hours: int = 4,
) -> int:
    """At 23:00 local, mark every ping with no response as MISSED_NON_RESPONSE.
    `response_window_hours` mirrors the gap between the ping (19:00)
    and close (23:00).
    """
    now = now or datetime.utcnow()
    cutoff = (now - timedelta(hours=response_window_hours)).replace(microsecond=0).isoformat()
    pings = team_store.list_pings_without_response(cutoff_iso=cutoff)
    closed = 0
    for p in pings:
        team_store.upsert_med_adherence_response(
            patient_id=p["patient_id"],
            episode_day=int(p["episode_day"]),
            response="MISSED_NON_RESPONSE",
            responded_at=None,
        )
        team_store.log_event(
            patient_id=p["patient_id"],
            event_type="POSTOP_MED_ADHERENCE_NON_RESPONSE",
            payload={"episodeDay": int(p["episode_day"])},
        )
        closed += 1
    return closed


def run_checkin_missed_watcher(
    *,
    patient_store: dict,
    team_store,
    now: Optional[datetime] = None,
    window_hours: Optional[int] = None,
) -> int:
    """Mark daily check-in sends with no response past the 36h window
    as missed. Bumps the in-memory `daily_checkin_missed_streak`
    counter on the patient blob (PRD §4.3)."""
    now = now or datetime.utcnow()
    hours = int(window_hours if window_hours is not None else CRON_CONFIG["daily_checkin_window_hours"])
    cutoff = (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    rows = team_store.list_daily_checkin_sends_without_response(cutoff_iso=cutoff)
    marked = 0
    for r in rows:
        if team_store.mark_daily_checkin_miss(r["patient_id"], int(r["episode_day"])):
            patient = patient_store.get(r["patient_id"])
            if patient is not None:
                bump_daily_checkin_missed_streak(patient)
            team_store.log_event(
                patient_id=r["patient_id"],
                event_type="POSTOP_DAILY_CHECKIN_MISSED",
                payload={"episodeDay": int(r["episode_day"])},
            )
            marked += 1
    return marked


def run_dayx_missed_watcher(
    *,
    team_store,
    now: Optional[datetime] = None,
    window_hours: Optional[int] = None,
) -> int:
    """Mark D-X surveys with no submission past the 48h window as missed."""
    now = now or datetime.utcnow()
    hours = int(window_hours if window_hours is not None else CRON_CONFIG["survey_window_hours"])
    cutoff = (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    rows = team_store.list_overdue_dayx_surveys(cutoff_iso=cutoff)
    marked = 0
    for r in rows:
        if team_store.mark_dayx_survey_missed(r["patient_id"], int(r["day"])):
            team_store.log_event(
                patient_id=r["patient_id"],
                event_type=f"POSTOP_DAY{int(r['day'])}_SURVEY_MISSED",
                payload={"day": int(r["day"])},
            )
            marked += 1
    return marked


# ─── Async wrappers (used by main.py) ──────────────────────────────────────


async def run_lost_contact_watcher_async(*, patient_store: dict, team_store) -> int:
    """Recompute lost-contact for every post-op patient and trigger
    `apply_postop_retier` if the flags trip. Returns the count of
    patients whose tier moved upward."""
    moved = 0
    for pid, patient in _post_op_patients(patient_store):
        ensure_postop_patient_state(patient)
        before = patient.get("current_tier")
        try:
            async with with_patient_lock(pid):
                ev = apply_postop_retier(
                    patient_id=pid,
                    patient_store=patient_store,
                    team_store=team_store,
                    triggered_by="CRON:LOST_CONTACT_WATCHER",
                )
            if ev.changed and before != ev.tier_after:
                moved += 1
        except Exception as exc:  # pragma: no cover — defensive
            print(f"[postop-lost-contact-watcher] error for {pid}: {exc}")
    return moved


async def run_nightly_retier_batch_async(*, patient_store: dict, team_store) -> int:
    """Nightly 02:00 batch — recompute every active post-op episode so
    the queue picks up time-driven contributors (e.g. crossing the D5 /
    D14 thresholds, lost-contact, missed sends)."""
    moved = 0
    for pid, patient in _post_op_patients(patient_store):
        ensure_postop_patient_state(patient)
        before = patient.get("current_tier")
        try:
            async with with_patient_lock(pid):
                ev = apply_postop_retier(
                    patient_id=pid,
                    patient_store=patient_store,
                    team_store=team_store,
                    triggered_by="CRON:NIGHTLY_BATCH",
                )
            if ev.changed and before != ev.tier_after:
                moved += 1
        except Exception as exc:  # pragma: no cover — defensive
            print(f"[postop-nightly-retier] error for {pid}: {exc}")
    return moved
