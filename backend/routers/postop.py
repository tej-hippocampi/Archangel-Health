"""
Post-Op Scoring & Re-Tiering HTTP surface (PRD §14).

All signal-submit endpoints synchronously trigger `apply_postop_retier`
inside a per-patient async lock so the recomputed tier is fresh by the
time the response returns.

Wound-photo endpoints (PRD §14.4) are intentionally absent — wound
feature out of scope v1.

Routes:
    GET    /api/episodes/{patient_id}/postop                   — patient blob view
    POST   /api/episodes/{patient_id}/postop/discharge         — set discharge_at
    POST   /api/episodes/{patient_id}/postop/checkin           — daily check-in submit
    POST   /api/episodes/{patient_id}/postop/survey/{day}      — D7/D14/D30 survey submit
    POST   /api/episodes/{patient_id}/postop/med-adherence     — adherence response
    POST   /api/episodes/{patient_id}/postop/video-event       — PLAYED / COMPLETED event
    POST   /api/episodes/{patient_id}/postop/self-flag         — patient one-tap flag
    POST   /api/episodes/{patient_id}/postop/self-flag/resolve — RN closes a flag
    POST   /api/episodes/{patient_id}/postop/care-goal-changed — RN sets pivot flag
    POST   /api/episodes/{patient_id}/postop-retier/run        — manual recompute
    GET    /api/episodes/{patient_id}/postop-retier-events     — audit list
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from auth_roles import (
    ALL_CLINICAL,
    WRITE_CLINICAL,
    require_patient_session,
    require_roles,
)
from staff_context import StaffContext, get_staff_context_optional
from triage.postop.apply import apply_postop_retier
from triage.postop.locks import with_patient_lock
from triage.postop.patient_state import (
    bump_daily_checkin_missed_streak,
    ensure_postop_patient_state,
    reset_daily_checkin_missed_streak,
    set_discharge_at,
    to_public,
)
from triage.postop.scoring.daily_checkin import score_daily_checkin
from triage.postop.scoring.day_survey import score_day_survey
from triage.postop.types import (
    DailyCheckinAnswers,
    DayXSurveyAnswers,
    MedAdherenceResponseValue,
    VideoEventType,
    VideoKind,
)


router = APIRouter(tags=["postop"])


# ─── Helpers ────────────────────────────────────────────────────────────────


def _patient_store(request: Request) -> Dict[str, Any]:
    return request.app.state.patient_store


def _team_store(request: Request):
    return request.app.state.team_store


async def _resolve_staff(authorization: Optional[str]) -> Optional[StaffContext]:
    return await get_staff_context_optional(authorization)


def _resolve_patient(request: Request, patient_id: str, staff: Optional[StaffContext]) -> Dict[str, Any]:
    store = _patient_store(request)
    patient = store.get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if staff and staff.source == "tenant" and staff.tenant_id:
        if (patient.get("health_system_id") or "") != staff.tenant_id:
            raise HTTPException(status_code=404, detail="Patient not found")
    ensure_postop_patient_state(patient)
    return patient


def _episode_day_for_now(patient: Dict[str, Any]) -> int:
    """Best-effort: compute a 1-indexed episode-day from `discharge_at`."""
    discharge_at = patient.get("discharge_at")
    if not discharge_at:
        return 1
    try:
        ts = datetime.fromisoformat(str(discharge_at).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return 1
    return max((datetime.utcnow() - ts).days + 1, 1)


async def _trigger_retier(
    *,
    patient_id: str,
    request: Request,
    triggered_by: str,
):
    """Synchronously run apply_postop_retier inside the per-patient lock."""
    patient_store = _patient_store(request)
    team_store = _team_store(request)
    async with with_patient_lock(patient_id):
        return apply_postop_retier(
            patient_id=patient_id,
            patient_store=patient_store,
            team_store=team_store,
            triggered_by=triggered_by,
        )


# ─── GET patient post-op view ───────────────────────────────────────────────


@router.get("/api/episodes/{patient_id}/postop")
async def get_postop_view(
    patient_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Read post-op blob. Pass-4: any clinical staff role; patients hit
    their own dashboard which uses a different code path (`/patient/{id}`).
    """
    staff = await _resolve_staff(authorization)
    require_roles(staff, ALL_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    return to_public(patient)


# ─── Discharge (PRD §14.1) ──────────────────────────────────────────────────


class DischargeRequest(BaseModel):
    discharge_at: str = Field(..., description="ISO timestamp of post-op-day-0 discharge")


@router.post("/api/episodes/{patient_id}/postop/discharge")
async def post_discharge(
    patient_id: str,
    body: DischargeRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Record discharge timestamp. Pass-4: clinical write role (RN or surgeon)."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    set_discharge_at(patient, body.discharge_at)
    team_store = _team_store(request)
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_DISCHARGE_RECORDED",
        payload={"discharge_at": body.discharge_at},
    )
    return {"ok": True, **to_public(patient)}


# ─── Daily check-in submit (PRD §14.2) ──────────────────────────────────────


class DailyCheckinRequest(BaseModel):
    episode_day: Optional[int] = None
    answers: DailyCheckinAnswers


@router.post("/api/episodes/{patient_id}/postop/checkin")
async def post_daily_checkin(
    patient_id: str,
    body: DailyCheckinRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Patient-submitted daily check-in. Pass-4: patient-session only."""
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    patient = _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    episode_day = int(body.episode_day or _episode_day_for_now(patient))

    scored = score_daily_checkin(body.answers)
    team_store.save_daily_checkin_response(
        patient_id=patient_id,
        episode_day=episode_day,
        submitted_at=None,
        answers=body.answers.model_dump(),
        raw_total=float(scored.raw_total),
        tier=scored.tier,
        red_flags=list(scored.red_flags),
        new_red_flag=bool(scored.new_red_flag_symptom),
        wound_concern=bool(scored.wound_concern),
        pain_nrs=int(scored.pain_nrs),
        pain_trajectory=scored.pain_trajectory,
        item_scores=dict(scored.item_scores),
    )
    reset_daily_checkin_missed_streak(patient)
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_DAILY_CHECKIN_SUBMITTED",
        payload={
            "episodeDay": episode_day,
            "tier": scored.tier,
            "rawTotal": scored.raw_total,
            "newRedFlag": scored.new_red_flag_symptom,
            "woundConcern": scored.wound_concern,
        },
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by="SIGNAL:DAILY_CHECKIN",
    )
    return {
        "ok": True,
        "tier": scored.tier,
        "newRedFlagSymptom": scored.new_red_flag_symptom,
        "woundConcern": scored.wound_concern,
        "retier": ev.model_dump(),
    }


# ─── Day-X survey submit (PRD §14.3) ────────────────────────────────────────


class DayXSurveyRequest(BaseModel):
    answers: DayXSurveyAnswers


@router.post("/api/episodes/{patient_id}/postop/survey/{day}")
async def post_dayx_survey(
    patient_id: str,
    day: int,
    body: DayXSurveyRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    if int(day) not in (7, 14, 30):
        raise HTTPException(status_code=400, detail="day must be 7, 14, or 30")
    staff = await _resolve_staff(authorization)
    # Patient-submitted survey. Pass-4: patient-session only.
    require_patient_session(staff)
    patient = _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    family = patient.get("anchor_procedure_family")
    scored = score_day_survey(day=int(day), answers=body.answers, procedure_family=family)
    team_store.upsert_dayx_survey_send(
        patient_id=patient_id, day=int(day), procedure_family=family,
    )
    team_store.submit_dayx_survey(
        patient_id=patient_id,
        day=int(day),
        section_scores=dict(scored.section_scores),
        total_score=float(scored.total_score),
        tier=scored.tier,
        red_flags=list(scored.red_flags),
        raw_answers=body.answers.model_dump(),
    )
    team_store.log_event(
        patient_id=patient_id,
        event_type=f"POSTOP_DAY{int(day)}_SURVEY_SUBMITTED",
        payload={
            "day": int(day),
            "tier": scored.tier,
            "totalScore": scored.total_score,
            "redFlags": scored.red_flags,
        },
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by=f"SIGNAL:DAY_{int(day)}_SURVEY",
    )
    return {"ok": True, "scored": scored.model_dump(), "retier": ev.model_dump()}


# ─── Med adherence response (PRD §14.5) ─────────────────────────────────────


class MedAdherenceRequest(BaseModel):
    episode_day: Optional[int] = None
    response: MedAdherenceResponseValue


@router.post("/api/episodes/{patient_id}/postop/med-adherence")
async def post_med_adherence(
    patient_id: str,
    body: MedAdherenceRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Patient-submitted med-adherence response. Pass-4: patient-session only."""
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    patient = _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    episode_day = int(body.episode_day or _episode_day_for_now(patient))

    team_store.upsert_med_adherence_response(
        patient_id=patient_id,
        episode_day=episode_day,
        response=body.response,
    )
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_MED_ADHERENCE_RESPONSE",
        payload={"episodeDay": episode_day, "response": body.response},
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by="SIGNAL:MED_ADHERENCE",
    )
    return {"ok": True, "retier": ev.model_dump()}


# ─── Post-op video event (PRD §14.6) ────────────────────────────────────────


class VideoEventRequest(BaseModel):
    video_kind: VideoKind
    event_type: VideoEventType
    session_id: str
    occurred_at: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


@router.post("/api/episodes/{patient_id}/postop/video-event")
async def post_video_event(
    patient_id: str,
    body: VideoEventRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Patient-submitted post-op video event. Pass-4: patient-session only."""
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    team_store.record_postop_video_event(
        patient_id=patient_id,
        video_kind=body.video_kind,
        event_type=body.event_type,
        session_id=body.session_id,
        occurred_at=body.occurred_at,
        payload=body.payload or {},
    )
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_VIDEO_EVENT",
        payload={
            "videoKind": body.video_kind,
            "eventType": body.event_type,
            "sessionId": body.session_id,
        },
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by=f"SIGNAL:VIDEO_{body.event_type}",
    )
    return {"ok": True, "retier": ev.model_dump()}


# ─── Patient self-flag (PRD §14.7 / §9) ─────────────────────────────────────


class SelfFlagRequest(BaseModel):
    free_text: Optional[str] = None


@router.post("/api/episodes/{patient_id}/postop/self-flag")
async def post_self_flag(
    patient_id: str,
    body: SelfFlagRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Patient one-tap self-flag. Pass-4: patient-session only."""
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    flag_id = team_store.create_self_flag(
        patient_id=patient_id,
        free_text=body.free_text or None,
        source="PATIENT_APP",
    )
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_PATIENT_SELF_FLAG",
        payload={"flagId": flag_id, "freeText": bool(body.free_text)},
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by="SIGNAL:SELF_FLAG",
    )
    return {"ok": True, "flagId": flag_id, "retier": ev.model_dump()}


class SelfFlagResolveRequest(BaseModel):
    flag_id: int
    resolved_by: str


@router.post("/api/episodes/{patient_id}/postop/self-flag/resolve")
async def post_self_flag_resolve(
    patient_id: str,
    body: SelfFlagResolveRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """RN closes a patient self-flag. Pass-4: alert resolve = `rn_coordinator` only."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, {"rn_coordinator"})
    _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    ok = team_store.resolve_self_flag(flag_id=body.flag_id, resolved_by=body.resolved_by)
    if not ok:
        raise HTTPException(status_code=404, detail="self-flag not found or already resolved")
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_PATIENT_SELF_FLAG_RESOLVED",
        payload={"flagId": body.flag_id, "resolvedBy": body.resolved_by},
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by=f"ACTION:SELF_FLAG_RESOLVED:{body.resolved_by}",
    )
    return {"ok": True, "retier": ev.model_dump()}


# ─── Care-goal pivot (PRD §17.7) ────────────────────────────────────────────


class CareGoalRequest(BaseModel):
    care_goal_changed: bool


@router.post("/api/episodes/{patient_id}/postop/care-goal-changed")
async def post_care_goal_changed(
    patient_id: str,
    body: CareGoalRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """RN-driven care-goal pivot flag. Pass-4: clinical write role."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    patient["care_goal_changed"] = bool(body.care_goal_changed)
    team_store = _team_store(request)
    team_store.log_event(
        patient_id=patient_id,
        event_type="POSTOP_CARE_GOAL_CHANGED",
        payload={"careGoalChanged": bool(body.care_goal_changed)},
    )
    ev = await _trigger_retier(
        patient_id=patient_id, request=request,
        triggered_by="ACTION:CARE_GOAL_CHANGED",
    )
    return {"ok": True, "retier": ev.model_dump()}


# ─── Manual recompute (PRD §14.8 / §13) ─────────────────────────────────────


class RetierRunRequest(BaseModel):
    triggered_by: Optional[str] = None


@router.post("/api/episodes/{patient_id}/postop-retier/run")
async def post_retier_run(
    patient_id: str,
    body: Optional[RetierRunRequest],
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Manual recompute. Pass-4: clinical write role (RN or surgeon)."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    _resolve_patient(request, patient_id, staff)
    triggered_by = (body and body.triggered_by) or "MANUAL:DOCTOR_UI"
    ev = await _trigger_retier(
        patient_id=patient_id, request=request, triggered_by=triggered_by,
    )
    return {"ok": True, "retier": ev.model_dump()}


# ─── Audit list ─────────────────────────────────────────────────────────────


@router.get("/api/episodes/{patient_id}/postop-retier-events")
async def list_retier_events(
    patient_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    limit: int = 100,
):
    """Audit list. Pass-4: any clinical staff role (read-only includes NP/PA)."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, ALL_CLINICAL)
    _resolve_patient(request, patient_id, staff)
    team_store = _team_store(request)
    rows = team_store.list_postop_retier_events(patient_id, limit=int(limit))
    return {"events": rows}
