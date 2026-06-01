"""
`apply_preop_retier` — single tier-write path for the pre-op stage.

Every code path that needs to materialize a pre-op tier change goes
through this function:

  - intake submit (after PAM scoring)
  - pre-op survey submit (T-96 / T-48 / T-24)
  - pre-op video / battle-card engagement events
  - "Recompute now" UI / cron triggers

The function is idempotent: calling it again with the same signal
state produces a fresh `preop_retier_events` row with the same delta
and final tier (different id / created_at / triggered_by).

State sourcing follows the Option B / event-stream architecture (see
`backend/team_store.py` top-of-file note):

    initial_tier / was_hard_escalator → in-memory `_patient_store` blob
    pam                                → `pam_assessments` (latest row)
    intake.status                      → `event_logs` (intake_started /
                                          intake_completed) + blob
    surveys                            → `survey_responses` rows where
                                          survey_type='preop'
    video.sessions                     → `event_logs preop_video_watched`
    battle_card.views                  → `event_logs BATTLECARD_VIEWED`
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from triage.preop_retier.algo import re_tier_preop
from triage.preop_retier.patient_state import (
    ensure_preop_retier_patient_state,
    update_preop_retier_denorm,
)
from triage.preop_retier.types import (
    BattleCardEngagement,
    IntakeState,
    IntakeStatus,
    PamResult,
    PreOpReTierInput,
    PreOpReTierResult,
    SurveyStatus,
    SurveyWindow,
    SurveyWindowState,
    VideoEngagement,
)
from triage.types import Tier


# Per-window survey_day mapping (matches `preop_survey.WINDOW_SURVEY_DAY`).
_WINDOW_TO_DAY: dict[SurveyWindow, int] = {
    "T_96": -4,
    "T_48": -2,
    "T_24": -1,
}
_DAY_TO_WINDOW: dict[int, SurveyWindow] = {v: k for k, v in _WINDOW_TO_DAY.items()}

# Hard-escalator-priority alert weights (mirrors postop._HARD_ALERT_WEIGHTS).
_HARD_ALERT_WEIGHTS = {
    "INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER": 90,
    "INTAKE_DISCLOSURE_HOUSING_INSTABILITY":      85,
    "INTAKE_DISCLOSURE_FOOD_INSECURITY":          70,
    "INTAKE_DISCLOSURE_TRANSPORTATION_BARRIER_DAY_OF": 80,
    "SURVEY_T48_RED_CRITICAL":                    85,
    "SURVEY_T24_RED_CRITICAL":                    90,
    "PAM_LEVEL_LOW_HARD":                          75,
    "TEACHBACK_FAILED_MED_HOLD_POSTLOOP":          85,
}


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _coerce_tier(value: Any, *, default: Tier = "TIER_1") -> Tier:
    if value in ("TIER_1", "TIER_2", "TIER_3"):
        return value  # type: ignore[return-value]
    return default


# ─── Time conversion helpers ───────────────────────────────────────────────


def _surgery_dt(patient: dict) -> Optional[datetime]:
    """Resolve the surgery datetime from the patient blob.

    Tries `surgery_at` first (set explicitly during scheduling), then
    falls back to `structured_data.procedure_date` parsed via the same
    helper used by the existing pre-op survey scheduler so behavior is
    consistent across stages.
    """
    raw = patient.get("surgery_at") or (
        (patient.get("structured_data") or {}).get("procedure_date")
    )
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        if "T" in raw or len(raw) > 12:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        # Date-only — default surgery hour matches preop_survey.parse_surgery_datetime.
        d = datetime.fromisoformat(raw[:10]).date()
        return datetime(d.year, d.month, d.day, 7, 0, 0)
    except Exception:
        return None


def _hours_until(surgery_dt: Optional[datetime], now: datetime) -> int:
    """Whole-hour count of `surgery_dt - now`, floored at 0.

    The re-tier algorithm consumes `hours_until_surgery` as a positive
    integer (PRD §0). Past-surgery (negative) collapses to 0.
    """
    if surgery_dt is None:
        return 96  # default ~4 days out when scheduling unknown
    diff = (surgery_dt - now).total_seconds() / 3600.0
    return max(int(diff), 0)


def _event_hours_before(now: datetime, surgery_dt: Optional[datetime], event_ts: str) -> Optional[int]:
    """Convert an ISO event timestamp into hours-before-surgery.

    Returns None if the timestamp can't be parsed or the surgery datetime
    is unknown (downstream callers treat None as "discard")."""
    if not event_ts or not surgery_dt:
        return None
    try:
        ts = datetime.fromisoformat(str(event_ts).replace("Z", "+00:00"))
        if ts.tzinfo:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None
    diff = (surgery_dt - ts).total_seconds() / 3600.0
    if diff < 0:
        return None
    return int(diff)


# ─── Signal-source readers ──────────────────────────────────────────────────


def _read_pam(patient_id: str, team_store) -> Optional[PamResult]:
    row = team_store.get_latest_pam_assessment(patient_id)
    if not row:
        return None
    try:
        return PamResult(
            raw_sum=int(row["raw_sum"]),
            items_scored=int(row["items_scored"]),
            raw_average=float(row["raw_average"]),
            activation_score=float(row["activation_score"]),
            level=row["level"],
            is_complete=bool(row.get("is_complete")),
        )
    except Exception:
        return None


def _read_intake_state(
    *,
    patient: dict,
    patient_id: str,
    team_store,
    surgery_dt: Optional[datetime],
) -> IntakeState:
    """Derive intake state from event_logs + blob.

    Status precedence: explicit `intake_status` on the patient blob wins
    if present (set by the intake-finalize handler in Phase 3); otherwise
    we infer from the existing `intake_started` / `intake_completed`
    events (PRD §3.2).
    """
    explicit = patient.get("intake_status")
    if explicit in ("NOT_REQUIRED", "NOT_STARTED", "STARTED", "COMPLETE"):
        status: IntakeStatus = explicit  # type: ignore[assignment]
    else:
        events = team_store.get_events(patient_id) or []
        has_started = any(e.get("event_type") == "intake_started" for e in events)
        has_completed = any(e.get("event_type") == "intake_completed" for e in events)
        if has_completed:
            status = "COMPLETE"
        elif has_started:
            status = "STARTED"
        else:
            status = "NOT_STARTED"

    started_at_h: Optional[int] = None
    completed_at_h: Optional[int] = None
    if surgery_dt is not None:
        events = team_store.get_events(patient_id) or []
        for e in events:
            if started_at_h is None and e.get("event_type") == "intake_started":
                started_at_h = _event_hours_before(datetime.utcnow(), surgery_dt, e.get("occurred_at"))
            if completed_at_h is None and e.get("event_type") == "intake_completed":
                completed_at_h = _event_hours_before(datetime.utcnow(), surgery_dt, e.get("occurred_at"))

    disclosures = list(patient.get("intake_disclosures") or [])

    return IntakeState(
        status=status,
        started_at_hours=started_at_h,
        completed_at_hours=completed_at_h,
        disclosures=disclosures,
    )


def _normalize_survey_status(tier_str: Optional[str]) -> SurveyStatus:
    """Map preop_survey lowercase tier to SurveyStatus uppercase."""
    if tier_str is None:
        return "PENDING"
    s = str(tier_str).strip().lower()
    if s == "green":
        return "GREEN"
    if s == "orange":
        return "ORANGE"
    if s == "red":
        return "RED"
    if s == "missed":
        return "MISSED"
    return "PENDING"


def _read_surveys(
    *,
    patient_id: str,
    team_store,
) -> list[SurveyWindowState]:
    """Read the three pre-op survey windows from `survey_responses`.

    Each window emits exactly one `SurveyWindowState`. Missing rows
    return PENDING. `has_critical_red_flag` is read from the row's
    answers payload (the existing preop scorer flips `red_flag_hit`
    to True for NPO violations and red-flag screen items).
    """
    rows = team_store.get_survey_responses(patient_id) or []
    by_day: dict[int, dict] = {
        int(r.get("survey_day", 0)): r
        for r in rows
        if str(r.get("survey_type") or "") == "preop"
    }

    out: list[SurveyWindowState] = []
    for window, day in _WINDOW_TO_DAY.items():
        row = by_day.get(day)
        if row is None:
            out.append(SurveyWindowState(window=window, status="PENDING"))
            continue
        status = _normalize_survey_status(row.get("tier"))
        critical = False
        if status == "RED":
            answers = row.get("answers") or []
            # Heuristic: any answer marked as a red flag in the source
            # data (the existing scorer flips items via "red"=True or
            # the well-known critical-item ids), or any t24_npo / no-ride
            # answer with a 0-score. Conservative — false positives are
            # safer than false negatives at the hard-escalator boundary.
            for a in answers:
                if isinstance(a, dict) and (a.get("red") is True or a.get("red_flag") is True):
                    critical = True
                    break
                qid = str((a or {}).get("id") or (a or {}).get("question_id") or "").lower() if isinstance(a, dict) else ""
                if qid in {"t24_last_solid", "t24_last_clear", "t24_ride_phone", "t24_adult_arrival"}:
                    if str((a or {}).get("answer", "")).strip().lower() in ("no", "still brown"):
                        critical = True
                        break
        out.append(SurveyWindowState(
            window=window,
            status=status,
            has_critical_red_flag=critical,
        ))
    return out


def _read_video_sessions(
    *,
    patient_id: str,
    team_store,
    surgery_dt: Optional[datetime],
) -> VideoEngagement:
    """Convert `preop_video_watched` event timestamps into per-session
    hours-before-surgery. The PRD §6.1 60-second-gap dedupe is applied
    upstream by the event submitter; here we treat each event as a
    distinct session."""
    if surgery_dt is None:
        return VideoEngagement(sessions=[])
    events = team_store.get_events(patient_id) or []
    sessions: list[int] = []
    seen_ts: set[str] = set()
    for e in events:
        et = str(e.get("event_type") or "")
        if et not in ("preop_video_watched", "PREOP_VIDEO_PLAYED"):
            continue
        ts = e.get("occurred_at")
        if not ts or ts in seen_ts:
            continue
        seen_ts.add(ts)
        h = _event_hours_before(datetime.utcnow(), surgery_dt, ts)
        if h is not None:
            sessions.append(h)
    return VideoEngagement(sessions=sessions)


def _read_battlecard_views(
    *,
    patient_id: str,
    team_store,
    surgery_dt: Optional[datetime],
) -> BattleCardEngagement:
    if surgery_dt is None:
        return BattleCardEngagement(views=[])
    events = team_store.get_events(patient_id) or []
    views: list[int] = []
    for e in events:
        et = str(e.get("event_type") or "")
        if et not in ("BATTLECARD_VIEWED", "preop_battlecard_viewed"):
            continue
        h = _event_hours_before(datetime.utcnow(), surgery_dt, e.get("occurred_at"))
        if h is not None:
            views.append(h)
    return BattleCardEngagement(views=views)


# ─── State assembly ─────────────────────────────────────────────────────────


def _gather_state(
    *,
    patient_id: str,
    patient: dict,
    team_store,
    now: Optional[datetime] = None,
) -> PreOpReTierInput:
    """Pull every signal source into a `PreOpReTierInput`. Pure read-side."""
    ensure_preop_retier_patient_state(patient)
    now = now or datetime.utcnow()
    surgery_dt = _surgery_dt(patient)
    hours_until = _hours_until(surgery_dt, now)

    initial_tier: Tier = _coerce_tier(patient.get("initial_tier"), default="TIER_1")
    # Pass 3 §1.3 — `episode_snapshots` is the source of truth. Initial-
    # tier writes go through to both the blob and the snapshot row, so
    # whenever a snapshot row exists it is at least as fresh as the
    # blob. On cold start the blob is freshly defaulted by
    # `ensure_preop_retier_patient_state`, so we'd otherwise miss the
    # bit; the snapshot row is the only place it survives a restart.
    snap = None
    try:
        snap = team_store.get_episode_snapshot(patient_id)
    except Exception:
        snap = None
    if snap is not None and "initial_tier_was_hard_escalator" in snap:
        was_hard = bool(snap.get("initial_tier_was_hard_escalator"))
        patient["initial_tier_was_hard_escalator"] = was_hard
    else:
        was_hard = bool(patient.get("initial_tier_was_hard_escalator"))

    pam = _read_pam(patient_id, team_store)
    intake = _read_intake_state(
        patient=patient,
        patient_id=patient_id,
        team_store=team_store,
        surgery_dt=surgery_dt,
    )
    surveys = _read_surveys(patient_id=patient_id, team_store=team_store)
    video = _read_video_sessions(
        patient_id=patient_id, team_store=team_store, surgery_dt=surgery_dt,
    )
    battle_card = _read_battlecard_views(
        patient_id=patient_id, team_store=team_store, surgery_dt=surgery_dt,
    )

    tb = patient.get("teachback") or {}
    if not isinstance(tb, dict):
        tb = {}
    pre_tb = tb.get("pre_op") if isinstance(tb.get("pre_op"), dict) else {}
    pre_tb = pre_tb or {}
    teachback_started = bool(pre_tb.get("started"))
    teachback_completed = bool(pre_tb.get("completed"))
    teachback_failed_med_hold = bool(pre_tb.get("failed_med_hold") or pre_tb.get("failed_med"))
    teachback_failed_fasting = bool(pre_tb.get("failed_fasting"))
    teachback_failed_critical = bool(pre_tb.get("failed_critical"))
    teachback_not_completed_by_t24 = bool(hours_until <= 24 and teachback_started and not teachback_completed)
    teachback_passed_all = bool(teachback_completed and str(pre_tb.get("final_status") or "").upper() == "PASS")

    return PreOpReTierInput(
        initial_tier=initial_tier,
        initial_tier_was_hard_escalator=was_hard,
        hours_until_surgery=hours_until,
        pam=pam,
        intake=intake,
        surveys=surveys,
        video=video,
        battle_card=battle_card,
        teachback_completed=teachback_completed,
        teachback_failed_med_hold=teachback_failed_med_hold,
        teachback_failed_fasting=teachback_failed_fasting,
        teachback_failed_critical=teachback_failed_critical,
        teachback_not_completed_by_t24=teachback_not_completed_by_t24,
        teachback_passed_all=teachback_passed_all,
    )


def _set_current_tier(patient: dict, tier: Tier) -> None:
    patient["current_tier"] = tier


# ─── Apply ──────────────────────────────────────────────────────────────────


def apply_preop_retier(
    *,
    patient_id: str,
    patient_store: dict,
    team_store,
    triggered_by: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run the full pre-op recompute cycle and persist the outcome.

    Returns a dict-form snapshot (mirrors the postop event row shape)
    so router callers can render a uniform response. Callers running
    inside an event loop should wrap this in `with_episode_lock(id)`
    to serialize concurrent recomputes.
    """
    patient = patient_store.get(patient_id)
    if patient is None:
        raise KeyError(f"unknown patient_id: {patient_id}")
    ensure_preop_retier_patient_state(patient)

    state = _gather_state(
        patient_id=patient_id,
        patient=patient,
        team_store=team_store,
        now=now,
    )

    tier_before: Tier = _coerce_tier(patient.get("current_tier"), default=state.initial_tier)
    result: PreOpReTierResult = re_tier_preop(state)

    tier_after: Tier = result.computed_tier
    changed = tier_after != tier_before

    event_id = uuid.uuid4().hex
    inputs_snapshot = state.model_dump()
    reasons_payload = [r.model_dump() for r in result.reasons]
    is_hard = bool(result.reasons and result.reasons[0].kind == "HARD")

    team_store.save_preop_retier_event(
        event_id=event_id,
        episode_id=patient_id,
        triggered_by=triggered_by,
        inputs_snapshot=inputs_snapshot,
        initial_tier=result.initial_tier,
        initial_tier_was_hard=bool(result.initial_tier_was_hard),
        computed_delta=int(result.delta),
        computed_tier=result.computed_tier,
        tier_before=tier_before,
        tier_after=tier_after,
        changed=changed,
        reasons=reasons_payload,
        model_version=result.model_version,
        tuning_version=result.tuning_version,
    )

    if changed:
        _set_current_tier(patient, tier_after)

    update_preop_retier_denorm(
        patient,
        last_run_at=_utc_iso(),
        last_delta=int(result.delta),
        top_reasons=reasons_payload[:3],
        last_tier=tier_after,
        model_version=result.model_version,
        tuning_version=result.tuning_version,
        initial_tier_was_hard=bool(result.initial_tier_was_hard),
    )

    try:
        team_store.log_event(
            patient_id=patient_id,
            event_type=(
                "PREOP_RETIER_TIER_UPDATED" if changed
                else "PREOP_RETIER_RECOMPUTED_NO_CHANGE"
            ),
            payload={
                "eventId": event_id,
                "tierBefore": tier_before,
                "tierAfter": tier_after,
                "computedDelta": int(result.delta),
                "computedTier": result.computed_tier,
                "softCapApplied": bool(result.soft_cap_applied),
                "hardEscalator": is_hard,
                "triggeredBy": triggered_by,
                "modelVersion": result.model_version,
                "tuningVersion": result.tuning_version,
            },
        )
        if is_hard:
            team_store.log_event(
                patient_id=patient_id,
                event_type="PREOP_RETIER_HARD_ESCALATOR_FIRED",
                payload={
                    "eventId": event_id,
                    "reasonCodes": [r.code for r in result.reasons if r.kind == "HARD"],
                    "triggeredBy": triggered_by,
                },
            )
    except Exception:
        # Audit failure must never block the tier write.
        pass

    _maybe_raise_escalation(
        patient_id=patient_id,
        team_store=team_store,
        result=result,
        tier_after=tier_after,
        changed=changed,
        triggered_by=triggered_by,
        event_id=event_id,
    )

    return {
        "id": event_id,
        "episode_id": patient_id,
        "triggered_by": triggered_by,
        "inputs_snapshot": inputs_snapshot,
        "initial_tier": result.initial_tier,
        "initial_tier_was_hard": bool(result.initial_tier_was_hard),
        "computed_delta": int(result.delta),
        "computed_tier": result.computed_tier,
        "soft_cap_applied": bool(result.soft_cap_applied),
        "tier_before": tier_before,
        "tier_after": tier_after,
        "changed": changed,
        "reasons": reasons_payload,
        "model_version": result.model_version,
        "tuning_version": result.tuning_version,
        "created_at": _utc_iso(),
    }


def _maybe_raise_escalation(
    *,
    patient_id: str,
    team_store,
    result: PreOpReTierResult,
    tier_after: Tier,
    changed: bool,
    triggered_by: str,
    event_id: str,
) -> None:
    """Raise an escalation row when a hard escalator fired or when the
    final tier reached TIER_3 due to soft delta. Mirrors the post-op
    escalation pattern (`triage.postop.apply._maybe_raise_escalation`).

    De-duplication: only raise when no open escalation with the same
    trigger_type exists. Subsequent re-tier calls do not stack.
    """
    is_hard = bool(result.reasons and result.reasons[0].kind == "HARD")
    if not is_hard and tier_after != "TIER_3":
        return
    # Don't churn escalations on no-op repeats.
    if not is_hard and not changed:
        return

    if is_hard:
        hard_codes = [r.code for r in result.reasons if r.kind == "HARD"]
        trigger_code = hard_codes[0] if hard_codes else "PREOP_HARD_ESCALATOR"
    else:
        # Soft path leading to TIER_3 — pick the heaviest positive contributor.
        positives = [
            r for r in result.reasons
            if r.kind == "SOFT" and (r.weight or 0) > 0
        ]
        if positives:
            heaviest = max(positives, key=lambda r: (r.weight or 0))
            trigger_code = f"PREOP_RED_{heaviest.code}"
        else:
            trigger_code = "PREOP_RED_NO_REASONS"

    if team_store.has_open_escalation(patient_id, trigger_code):
        return

    snapshot = [
        {"kind": r.kind, "code": r.code, "label": r.label, "weight": r.weight}
        for r in result.reasons
    ]
    weight = _HARD_ALERT_WEIGHTS.get(trigger_code, 60)
    team_store.create_escalation(
        patient_id=patient_id,
        tier=int(tier_after.removeprefix("TIER_")),
        trigger_type=trigger_code,
        message=(
            f"Pre-op re-tier escalation: {trigger_code} (event {event_id}, "
            f"weight {weight}, triggered_by {triggered_by})"
        ),
        conversation_snapshot=snapshot,
    )
