"""
`apply_postop_retier` — single tier-write path for the post-op stage (PRD §10).

Every code path that needs to materialize a post-op tier change goes
through this function:

  - signal commits         (daily check-in / survey / med-adherence /
                            video / self-flag) — synchronous after
                            the row is written
  - nightly batch          (`_postop_retier_nightly_loop`)
  - D7 / D14 / D30 cron    (after the missed-window watcher closes)
  - "Recompute now" UI     (`POST /api/episodes/{id}/postop-retier/run`)
  - lost-contact watcher   (after marking a patient silent)

The function is idempotent: calling it again with the same signal
state produces a fresh `PostOpReTierEvent` row with the same delta and
final tier (different id / created_at / triggered_by).

Wound-photo signals are intentionally absent (PRD §8 out of scope v1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from triage.postop.algo import re_tier_post_op
from triage.postop.mapping import resolve_post_op_tier
from triage.postop.patient_state import (
    ensure_postop_patient_state,
    get_daily_checkin_missed_streak,
    update_postop_retier_denorm,
)
from triage.postop.scoring import (
    compute_rolling_med_adherence,
    determine_video_flags,
    lost_contact_status,
)
from triage.postop.scoring.care_companion import (
    count_chat_sessions_last_7d,
    count_chat_sessions_total,
    has_open_chat_semantic_escalation,
    latest_semantic_escalation,
)
from triage.postop.scoring.daily_checkin import is_pain_above_expected_curve
from triage.postop.tuning import (
    DISABLED_IN_V1,
    MED_ADHERENCE_CONFIG,
    MODEL_VERSION,
    TUNING_VERSION,
)
from triage.postop.types import (
    PostOpReTierEvent,
    PostOpReTierInput,
    PostOpReTierResult,
)
from triage.types import Tier

# Subset of PRD §11 alert weights used to decide whether a re-tier
# event raises an escalation row. Wound-photo entries omitted.
_HARD_ALERT_WEIGHTS = {
    "PATIENT_SELF_FLAG_ACTIVE":            100,
    "NEW_RED_FLAG_SYMPTOM":                100,
    "LOST_CONTACT_TIER3":                   85,
    "LOST_CONTACT_GENERAL":                 75,
    "DAY_X_SURVEY_RED_AND_RED_FLAG":        85,
    "MULTIPLE_INCISION_FLAGS":              85,
}


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _days_since_discharge(patient: dict, now: Optional[datetime] = None) -> int:
    now = now or datetime.utcnow()
    discharge_at = patient.get("discharge_at")
    if not discharge_at:
        return 0
    try:
        ts = datetime.fromisoformat(str(discharge_at).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return 0
    return max((now - ts).days, 0)


def _coerce_tier(value: Any, *, default: Tier = "TIER_1") -> Tier:
    if value in ("TIER_1", "TIER_2", "TIER_3"):
        return value  # type: ignore[return-value]
    return default


def _gather_state(
    *,
    patient_id: str,
    patient: dict,
    team_store,
    now: Optional[datetime] = None,
) -> PostOpReTierInput:
    """Pull every signal source into a `PostOpReTierInput`. Pure read-side."""
    ensure_postop_patient_state(patient)
    now = now or datetime.utcnow()
    days_since = _days_since_discharge(patient, now=now)
    procedure_family = patient.get("anchor_procedure_family")
    # Pass 3 §1.3 — `episode_snapshots` is the source of truth for the
    # post-intra-op floor. The intra-op apply writes through to both the
    # blob and the snapshot row, so the snapshot is at least as fresh.
    # On cold start the blob may have lost the field; pull it back.
    snap = None
    try:
        snap = team_store.get_episode_snapshot(patient_id)
    except Exception:
        snap = None
    snap_floor = (snap or {}).get("post_intraop_tier") if snap else None
    if snap_floor:
        patient["post_intraop_tier"] = snap_floor
    floor = _coerce_tier(patient.get("post_intraop_tier"), default="TIER_1")
    current_tier = _coerce_tier(patient.get("current_tier"), default=floor)

    # Self-flag (PRD §9)
    has_self_flag = bool(team_store.has_active_self_flag(patient_id))

    # Latest daily check-in (PRD §4)
    last_checkin = team_store.list_recent_daily_checkin_responses(patient_id, limit=1)
    latest_checkin = last_checkin[0] if last_checkin else None
    last_checkin_tier = latest_checkin["tier"] if latest_checkin else None
    new_red_flag_today = bool(latest_checkin and latest_checkin.get("new_red_flag"))
    wound_concern_today = bool(latest_checkin and latest_checkin.get("wound_concern"))

    # Multiple incision flags (PRD §10.2)
    multi_today = bool(latest_checkin and len(latest_checkin.get("answers", {}).get("incision_flags") or []) >= 2)

    # Streak: how many of the last 3 consecutive episode-days had >=1 chip.
    flag_streak = 0
    if latest_checkin:
        last_day = int(latest_checkin.get("episode_day", 0) or 0)
        recent = team_store.list_daily_checkin_responses_in_range(
            patient_id, day_from=last_day - 6, day_to=last_day,
        )
        # Walk newest day downward; stop on first day that did not have
        # any chip (or no submission).
        by_day: dict[int, dict[str, Any]] = {}
        for r in recent:
            by_day.setdefault(int(r.get("episode_day", -1)), r)
        for d in range(last_day, last_day - 4, -1):
            row = by_day.get(d)
            if row is None:
                break
            chips = (row.get("answers") or {}).get("incision_flags") or []
            if not chips:
                break
            flag_streak += 1

    # Pain trajectory abnormal (PRD §4.2)
    pain_traj_abnormal = False
    if latest_checkin:
        pain_traj = (latest_checkin.get("pain_trajectory") or "").upper()
        pain_nrs_val = latest_checkin.get("pain_nrs")
        episode_day = int(latest_checkin.get("episode_day", 1) or 1)
        if pain_traj == "WORSE" and isinstance(pain_nrs_val, (int, float)):
            pain_traj_abnormal = is_pain_above_expected_curve(
                episode_day=episode_day, pain_nrs=int(pain_nrs_val),
            )

    # Rolling-window check-in summary (PRD §10.3.a). We only count days
    # as missed when the system actually sent a check-in but the patient
    # didn't respond — early days before the cron has run shouldn't
    # count against the patient.
    last_day_for_window = int(latest_checkin.get("episode_day", days_since) if latest_checkin else max(days_since, 0))
    window_lo = max(last_day_for_window - 6, 1)
    window_hi = max(last_day_for_window, 1)
    window_responses = team_store.list_daily_checkin_responses_in_range(
        patient_id, day_from=window_lo, day_to=window_hi,
    )
    sent_days_in_window = {
        d for d in range(window_lo, window_hi + 1)
        if team_store.has_daily_checkin_send(patient_id, d)
    }
    response_days = {int(r.get("episode_day", 0) or 0) for r in window_responses}
    missed_count_7d = sum(1 for d in sent_days_in_window if d not in response_days)
    red_count = sum(1 for r in window_responses if r.get("tier") == "RED")
    orange_count = sum(1 for r in window_responses if r.get("tier") == "ORANGE")

    # D-X surveys (PRD §5)
    surveys = {s["day"]: s for s in team_store.list_dayx_surveys(patient_id)}

    def _survey(d: int) -> tuple[Optional[str], bool, bool]:
        row = surveys.get(d)
        if not row:
            return None, False, False
        tier = row.get("tier") if row.get("status") == "COMPLETED" else None
        red_flag = bool((row.get("red_flags") or []) and row.get("status") == "COMPLETED")
        missed = row.get("status") == "MISSED"
        return tier, red_flag, missed

    d7_tier, d7_red_flag, d7_missed = _survey(7)
    d14_tier, d14_red_flag, d14_missed = _survey(14)
    d30_tier, d30_red_flag, d30_missed = _survey(30)

    # Video engagement (PRD §6)
    events = team_store.list_postop_video_events(patient_id)
    video_flags = determine_video_flags(
        events,
        discharge_at_iso=patient.get("discharge_at"),
        days_since_discharge=days_since,
    )

    # Med adherence rolling 7-day (PRD §7.2). The PRD's "low" / "high"
    # bands assume the cron has actually sent the 7 daily pings; before
    # that we suppress the bands so an early-recovery patient isn't
    # dinged for non-existent prompts.
    last_med_day = max(days_since, 1)
    med_window_lo = max(last_med_day - 6, 1)
    med_resps = team_store.list_med_adherence_responses(
        patient_id, day_from=med_window_lo, day_to=last_med_day,
    )
    med_summary = compute_rolling_med_adherence(
        responses=med_resps,
        now_episode_day=last_med_day,
    )
    streak_threshold = int(MED_ADHERENCE_CONFIG["non_response_streak_days"])
    pings_sent_in_window = sum(
        1 for d in range(med_window_lo, last_med_day + 1)
        if team_store.has_med_adherence_ping(patient_id, d)
    )
    if pings_sent_in_window < int(MED_ADHERENCE_CONFIG["rolling_window_days"]):
        med_summary = med_summary.model_copy(update={"high": False, "low": False})

    # Lost contact (PRD §10.2). Silence anchor = max(discharge, last_response).
    last_response_at = team_store.last_response_timestamp_across_channels(patient_id)
    lc = lost_contact_status(
        current_tier=current_tier,
        last_response_at_iso=last_response_at,
        discharge_at_iso=patient.get("discharge_at"),
        now=now,
    )

    # ─── Care Companion (Triage Suite Pass 3 §3.3) ─────────────────────────
    # Behind the `care_companion_enabled` tuning flag — when False, all
    # signals stay at their dataclass defaults, so the algorithm is a
    # no-op against this surface.
    cc_red_unresolved = False
    cc_tier2_24h = False
    cc_sessions_7d = 0
    cc_sessions_total = 0
    cc_past_d7 = False
    if DISABLED_IN_V1.get("care_companion_enabled"):
        cc_red_unresolved = bool(
            has_open_chat_semantic_escalation(team_store, patient_id)
            and (latest_semantic_escalation(team_store, patient_id) or {}).get("tier") == 3
        )
        latest_24h_window = now - timedelta(hours=24)
        latest_recent = latest_semantic_escalation(team_store, patient_id, since=latest_24h_window)
        cc_tier2_24h = bool(latest_recent and latest_recent.get("tier") == 2)
        cc_sessions_7d = int(count_chat_sessions_last_7d(team_store, patient_id, now=now))
        cc_sessions_total = int(count_chat_sessions_total(team_store, patient_id))
        cc_past_d7 = bool(days_since >= 7)

    return PostOpReTierInput(
        patient_id=patient_id,
        procedure_family=procedure_family,
        post_intraop_tier=floor,
        current_tier=current_tier,
        days_since_discharge=days_since,
        care_goal_changed=bool(patient.get("care_goal_changed")),
        has_active_self_flag=has_self_flag,
        last_checkin_tier=last_checkin_tier,  # type: ignore[arg-type]
        checkin_red_count_7d=red_count,
        checkin_orange_count_7d=orange_count,
        checkin_missed_count_7d=missed_count_7d,
        checkin_missed_streak=get_daily_checkin_missed_streak(patient),
        wound_concern_today=wound_concern_today,
        pain_trajectory_abnormal=pain_traj_abnormal,
        new_red_flag_symptom_today=new_red_flag_today,
        multiple_incision_flags_today=multi_today,
        incision_flag_streak=flag_streak,
        day7_tier=d7_tier,                   # type: ignore[arg-type]
        day7_red_flag=d7_red_flag,
        day7_missed=d7_missed,
        day14_tier=d14_tier,                 # type: ignore[arg-type]
        day14_red_flag=d14_red_flag,
        day14_missed=d14_missed,
        day30_tier=d30_tier,                 # type: ignore[arg-type]
        day30_red_flag=d30_red_flag,
        day30_missed=d30_missed,
        red_flag_video_viewed_by_d2=video_flags["red_flag_video_viewed_by_d2"],
        red_flag_video_viewed_by_d5=video_flags["red_flag_video_viewed_by_d5"],
        diag_treat_video_viewed_by_d5=video_flags["diag_treat_video_viewed_by_d5"],
        diag_treat_video_sessions_total=int(video_flags["diag_treat_video_sessions_total"]),
        diag_treat_video_viewed_by_d14=not video_flags["diag_treat_video_not_viewed_by_d14"],
        med_adherence_high=med_summary.high,
        med_adherence_low=med_summary.low,
        med_adherence_non_response_streak_3=med_summary.non_response_streak >= streak_threshold,
        lost_contact_tier3_24h=lc.tier3_24h,
        lost_contact_general_72h=lc.general_72h,
        care_companion_red_flag_unresolved=cc_red_unresolved,
        care_companion_tier2_within_24h=cc_tier2_24h,
        care_companion_chat_sessions_last_7d=cc_sessions_7d,
        care_companion_chat_sessions_total=cc_sessions_total,
        care_companion_episode_past_d7=cc_past_d7,
    )


def _set_current_tier(patient: dict, tier: Tier) -> None:
    """Mirror of `triage.intraop.patient_state.set_current_tier` without
    reaching into the intra-op module — keeps the post-op stage decoupled."""
    patient["current_tier"] = tier


def apply_postop_retier(
    *,
    patient_id: str,
    patient_store: dict,
    team_store,
    triggered_by: str,
    now: Optional[datetime] = None,
) -> PostOpReTierEvent:
    """Run the full post-op recompute cycle and persist the outcome.

    Returns the materialized `PostOpReTierEvent`. Callers running inside
    an event loop should wrap this in `with_patient_lock(patient_id)` to
    serialize concurrent recomputes (the cron + signal-submit paths
    already do).
    """
    patient = patient_store.get(patient_id)
    if patient is None:
        raise KeyError(f"unknown patient_id: {patient_id}")
    ensure_postop_patient_state(patient)

    state = _gather_state(
        patient_id=patient_id,
        patient=patient,
        team_store=team_store,
        now=now,
    )

    tier_before = state.current_tier
    result: PostOpReTierResult = re_tier_post_op(state)

    # Final tier resolves against current_tier (upward-only) — guards
    # against algorithm proposing a downgrade we already ruled out at
    # mapping-time but want to enforce explicitly.
    tier_after: Tier = resolve_post_op_tier(
        floor=result.floor,
        current_tier=tier_before,
        target_tier=result.proposed_tier,
    )
    changed = tier_after != tier_before

    event_id = uuid.uuid4().hex
    inputs_snapshot = state.model_dump()
    reasons_payload = [r.model_dump() for r in result.reasons]

    team_store.save_postop_retier_event(
        event_id=event_id,
        patient_id=patient_id,
        triggered_by=triggered_by,
        inputs_snapshot=inputs_snapshot,
        post_intraop_tier=state.post_intraop_tier,
        computed_delta=int(result.delta),
        computed_tier=result.proposed_tier,
        tier_before=tier_before,
        tier_after=tier_after,
        changed=changed,
        reasons=reasons_payload,
        model_version=result.model_version,
        tuning_version=result.tuning_version,
    )

    if changed:
        _set_current_tier(patient, tier_after)

    # Denormalize for the queue (PRD README §3.5).
    update_postop_retier_denorm(
        patient,
        last_run_at=_utc_iso(),
        last_delta=int(result.delta),
        top_reasons=reasons_payload[:3],
        last_tier=tier_after,
        model_version=result.model_version,
        tuning_version=result.tuning_version,
    )

    # Audit (event_logs). Cron passes skip unchanged recomputes to avoid
    # flooding the doctor timeline with duplicate POSTOP_RETIER_* rows.
    cron_trigger = str(triggered_by or "").startswith("CRON:")
    log_audit = changed or not cron_trigger
    try:
        if log_audit:
            team_store.log_event(
                patient_id=patient_id,
                event_type="POSTOP_RETIER_RECOMPUTED" if not changed else "POSTOP_RETIER_TIER_UPDATED",
                payload={
                    "eventId": event_id,
                    "tierBefore": tier_before,
                    "tierAfter": tier_after,
                    "computedDelta": int(result.delta),
                    "computedTier": result.proposed_tier,
                    "hardEscalator": result.hard_escalator_fired,
                    "triggeredBy": triggered_by,
                    "modelVersion": result.model_version,
                    "tuningVersion": result.tuning_version,
                },
            )
            if result.hard_escalator_fired and changed:
                team_store.log_event(
                    patient_id=patient_id,
                    event_type="POSTOP_RETIER_HARD_ESCALATOR_FIRED",
                    payload={
                        "eventId": event_id,
                        "reasonCodes": [r.code for r in result.reasons if r.kind == "HARD"],
                        "triggeredBy": triggered_by,
                    },
                )
    except Exception:
        # Audit failure must never block the tier write.
        pass

    # Escalation pipeline (PRD §11).
    _maybe_raise_escalation(
        patient_id=patient_id,
        team_store=team_store,
        result=result,
        tier_after=tier_after,
        triggered_by=triggered_by,
        event_id=event_id,
    )

    return PostOpReTierEvent(
        id=event_id,
        patient_id=patient_id,
        triggered_by=triggered_by,
        inputs_snapshot=inputs_snapshot,
        post_intraop_tier=state.post_intraop_tier,
        computed_delta=int(result.delta),
        computed_tier=result.proposed_tier,
        tier_before=tier_before,
        tier_after=tier_after,
        changed=changed,
        reasons=result.reasons,
        model_version=result.model_version,
        tuning_version=result.tuning_version,
        created_at=_utc_iso(),
    )


def _maybe_raise_escalation(
    *,
    patient_id: str,
    team_store,
    result: PostOpReTierResult,
    tier_after: Tier,
    triggered_by: str,
    event_id: str,
) -> None:
    """Raise an escalation row when a hard escalator fired or when the
    final tier reached TIER_3 due to soft delta. Mirrors the existing
    chat / pre-op escalation pattern (`_classify_and_create_escalation`).

    De-duplication: we only raise when there is no open escalation
    with the same trigger_type; subsequent re-tier calls do not stack.
    """
    if not result.hard_escalator_fired and tier_after != "TIER_3":
        return

    # Compose the trigger_type from the highest-priority reason.
    if result.hard_escalator_fired:
        hard_codes = [r.code for r in result.reasons if r.kind == "HARD"]
        trigger_code = hard_codes[0] if hard_codes else "POSTOP_HARD_ESCALATOR"
    else:
        # Soft path leading to TIER_3 — pick the heaviest contributor.
        positives = [r for r in result.reasons if r.kind == "POSITIVE"]
        if positives:
            heaviest = max(positives, key=lambda r: r.weight)
            trigger_code = f"POSTOP_RED_{heaviest.code}"
        else:
            trigger_code = "POSTOP_RED_NO_REASONS"

    if team_store.has_open_escalation(patient_id, trigger_code):
        return

    snapshot = [
        {
            "kind": r.kind, "code": r.code, "label": r.label, "weight": r.weight,
        }
        for r in result.reasons
    ]
    weight = _HARD_ALERT_WEIGHTS.get(trigger_code, 60)
    team_store.create_escalation(
        patient_id=patient_id,
        tier=int(tier_after.removeprefix("TIER_")),
        trigger_type=trigger_code,
        message=(
            f"Post-op re-tier escalation: {trigger_code} (event {event_id}, "
            f"weight {weight}, triggered_by {triggered_by})"
        ),
        conversation_snapshot=snapshot,
    )
