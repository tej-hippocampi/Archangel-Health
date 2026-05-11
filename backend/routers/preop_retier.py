"""
Pre-Op Re-Tier HTTP surface (PRD §1.3 / §3 / §4 / §6).

All signal-submit endpoints synchronously trigger `apply_preop_retier`
inside a per-episode async lock so the recomputed tier is fresh by the
time the response returns. Mirrors the post-op signal submission
contract.

Routes:
    POST /api/triage/preop-retier/compute              — pure preview
    POST /api/episodes/{episode_id}/preop-retier/run   — manual recompute
    POST /api/episodes/{episode_id}/pam                — submit PAM responses
    POST /api/events/preop-video                       — video play event
    POST /api/events/battlecard                        — battlecard view
    GET  /api/triage/tuning/preop-retier/current       — tuning snapshot
    POST /api/triage/tuning/preop-retier               — admin no-op stub
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, conlist

from auth_roles import (
    ALL_CLINICAL,
    WRITE_CLINICAL,
    require_patient_session,
    require_roles,
)
from staff_context import StaffContext, get_staff_context_optional
from triage.preop_retier import get_config, re_tier_preop, score_pam
from triage.preop_retier.apply import apply_preop_retier
from triage.preop_retier.locks import with_episode_lock
from triage.preop_retier.patient_state import (
    ensure_preop_retier_patient_state,
    to_public,
)
from triage.preop_retier.types import (
    PamResponse,
    PreOpReTierInput,
)


router = APIRouter(tags=["preop-retier"])


# ─── Helpers ────────────────────────────────────────────────────────────────


def _patient_store(request: Request) -> Dict[str, Any]:
    return request.app.state.patient_store


def _team_store(request: Request):
    return request.app.state.team_store


async def _resolve_staff(authorization: Optional[str]) -> Optional[StaffContext]:
    return await get_staff_context_optional(authorization)


def _resolve_patient(
    request: Request,
    patient_id: str,
    staff: Optional[StaffContext],
) -> Dict[str, Any]:
    store = _patient_store(request)
    patient = store.get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if staff and staff.source == "tenant" and staff.tenant_id:
        if (patient.get("health_system_id") or "") != staff.tenant_id:
            raise HTTPException(status_code=404, detail="Patient not found")
    ensure_preop_retier_patient_state(patient)
    return patient


def _verify_admin(x_admin_token: Optional[str]) -> None:
    expected = os.getenv("ADMIN_AUTH_TOKEN") or os.getenv("ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Admin token required")


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


async def _run_retier(
    *,
    request: Request,
    episode_id: str,
    triggered_by: str,
) -> Dict[str, Any]:
    """Run apply_preop_retier inside the per-episode lock. Returns the
    persisted snapshot dict (mirrors `postop` event-shape responses)."""
    patient_store = _patient_store(request)
    team_store = _team_store(request)
    if episode_id not in patient_store:
        raise HTTPException(status_code=404, detail="Patient not found")
    async with with_episode_lock(episode_id):
        return apply_preop_retier(
            patient_id=episode_id,
            patient_store=patient_store,
            team_store=team_store,
            triggered_by=triggered_by,
        )


# ─── 1. Pure preview ────────────────────────────────────────────────────────


@router.post("/api/triage/preop-retier/compute")
async def post_preop_retier_compute(
    state: PreOpReTierInput,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Stateless preview — no persistence, no events. Pass-4 PRD §3.2
    treats `compute` as a clinical write surface so NP/PA is blocked."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    return re_tier_preop(state).model_dump()


# ─── 2. Manual run / recompute ──────────────────────────────────────────────


class PreOpReTierRunRequest(BaseModel):
    triggered_by: Optional[str] = Field(
        default=None,
        description=(
            "Optional trigger label for audit. Defaults to "
            "'MANUAL:DOCTOR_UI' when called from the doctor recompute CTA."
        ),
    )


@router.post("/api/episodes/{episode_id}/preop-retier/run")
async def post_preop_retier_run(
    episode_id: str,
    body: PreOpReTierRunRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Manual recompute. Always writes a `preop_retier_events` row
    regardless of `changed`. Honors the per-episode lock."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    _resolve_patient(request, episode_id, staff)
    triggered_by = body.triggered_by or "MANUAL:DOCTOR_UI"
    snapshot = await _run_retier(
        request=request,
        episode_id=episode_id,
        triggered_by=triggered_by,
    )
    return {"ok": True, "event": snapshot}


# ─── 3. PAM submit ──────────────────────────────────────────────────────────


class PamSubmission(BaseModel):
    """Body schema for the PAM-13 proxy submit. Validation is the same
    one used by the algorithm-pure scorer (`triage.preop_retier.types.PamResponse`).
    """
    responses: conlist(PamResponse, min_length=1, max_length=13) = Field(  # type: ignore[valid-type]
        ...,
        description="1..13 PAM-13 proxy responses; items_scored ≥ 10 to be is_complete.",
    )


@router.post("/api/episodes/{episode_id}/pam")
async def post_pam_submission(
    episode_id: str,
    body: PamSubmission,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Score PAM, persist, then synchronously re-tier inside the episode lock.

    Pass-4: patient-session only — staff can't submit PAM responses on a
    patient's behalf. The patient app currently runs anonymously; if a
    staff Bearer is presented, return 403.
    """
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    patient = _resolve_patient(request, episode_id, staff)
    team_store = _team_store(request)

    result = score_pam(list(body.responses))

    from triage.preop_retier.tuning import MODEL_VERSION, TUNING_VERSION
    saved = team_store.save_pam_assessment(
        episode_id=episode_id,
        patient_id=episode_id,
        responses=[r.model_dump() for r in body.responses],
        raw_sum=int(result.raw_sum),
        items_scored=int(result.items_scored),
        raw_average=float(result.raw_average),
        activation_score=float(result.activation_score),
        level=result.level,
        is_complete=bool(result.is_complete),
        model_version=MODEL_VERSION,
        tuning_version=int(TUNING_VERSION),
        completed_at=_utc_iso() if result.is_complete else None,
    )

    try:
        team_store.log_event(
            patient_id=episode_id,
            event_type="PAM_ASSESSMENT_SAVED",
            payload={
                "assessmentId": saved.get("id"),
                "level": result.level,
                "activationScore": result.activation_score,
                "itemsScored": result.items_scored,
                "isComplete": bool(result.is_complete),
            },
        )
    except Exception:
        pass

    snapshot = await _run_retier(
        request=request,
        episode_id=episode_id,
        triggered_by="SIGNAL:INTAKE_PAM",
    )

    return {
        "ok": True,
        "activation_score": float(result.activation_score),
        "level": result.level,
        "is_complete": bool(result.is_complete),
        "items_scored": int(result.items_scored),
        "raw_average": float(result.raw_average),
        "assessment_id": saved.get("id") if saved else None,
        "retier": snapshot,
    }


# ─── 4. Pre-Op video event (PRD §6.1) ───────────────────────────────────────


class PreOpVideoEvent(BaseModel):
    episode_id: str
    session_id: str = Field(..., description="Client-side dedup key (e.g. window.crypto)")
    duration_sec: int = Field(0, ge=0)
    completed_session: bool = False


@router.post("/api/events/preop-video")
async def post_preop_video_event(
    body: PreOpVideoEvent,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Record a pre-op video play event, dedupe within 60s for same
    `(episode_id, session_id)`, then trigger a re-tier.

    Pass-4: patient-session only — clinicians don't generate video-watched
    events; the patient app posts these directly.
    """
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    _resolve_patient(request, body.episode_id, staff)
    team_store = _team_store(request)

    is_dup = _is_recent_event_for_session(
        team_store=team_store,
        patient_id=body.episode_id,
        event_type="PREOP_VIDEO_PLAYED",
        session_id=body.session_id,
        within_seconds=60,
    )
    if not is_dup:
        team_store.log_event(
            patient_id=body.episode_id,
            event_type="PREOP_VIDEO_PLAYED",
            payload={
                "sessionId": body.session_id,
                "durationSec": int(body.duration_sec),
                "completedSession": bool(body.completed_session),
            },
        )
        # Mirror the existing patient-app convention so other readers
        # (preop_survey scoring, doctor analytics) keep seeing
        # `preop_video_watched` events.
        team_store.log_event(
            patient_id=body.episode_id,
            event_type="preop_video_watched",
            payload={"sessionId": body.session_id},
        )

    snapshot = await _run_retier(
        request=request,
        episode_id=body.episode_id,
        triggered_by="SIGNAL:PREOP_VIDEO_PLAYED",
    )
    return {"ok": True, "deduped": is_dup, "retier": snapshot}


# ─── 5. Battle-card event (PRD §6.2) ────────────────────────────────────────


class BattleCardEvent(BaseModel):
    episode_id: str
    dwell_ms: int = Field(0, ge=0)
    scroll_depth_pct: int = Field(0, ge=0, le=100)


@router.post("/api/events/battlecard")
async def post_battlecard_event(
    body: BattleCardEvent,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Record a battle-card view event, dedupe within 30 minutes for
    the patient, then trigger a re-tier.

    Pass-4: patient-session only — same rationale as `/api/events/preop-video`.
    """
    staff = await _resolve_staff(authorization)
    require_patient_session(staff)
    _resolve_patient(request, body.episode_id, staff)
    team_store = _team_store(request)

    is_dup = _is_recent_event(
        team_store=team_store,
        patient_id=body.episode_id,
        event_type="BATTLECARD_VIEWED",
        within_seconds=30 * 60,
    )
    if not is_dup:
        team_store.log_event(
            patient_id=body.episode_id,
            event_type="BATTLECARD_VIEWED",
            payload={
                "dwellMs": int(body.dwell_ms),
                "scrollDepthPct": int(body.scroll_depth_pct),
            },
        )

    snapshot = await _run_retier(
        request=request,
        episode_id=body.episode_id,
        triggered_by="SIGNAL:BATTLECARD_VIEWED",
    )
    return {"ok": True, "deduped": is_dup, "retier": snapshot}


# ─── 6. Tuning (read + admin no-op) ─────────────────────────────────────────


@router.get("/api/triage/tuning/preop-retier/current")
async def get_preop_retier_tuning_current(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Read tuning snapshot. Pass-4: any authenticated staff role."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, ALL_CLINICAL)
    return get_config()


@router.post("/api/triage/tuning/preop-retier")
async def post_preop_retier_tuning(
    request: Request,
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Admin contract surface (no-op deploy v1).

    Use `/admin/triage/preop-retier/config` for the read-side rendered
    in the admin portal.
    """
    _verify_admin(x_admin_token)
    try:
        await request.json()
    except Exception:
        pass
    return {"ok": True, "deployed": False, "config": get_config()}


# ─── Internal: dedupe ───────────────────────────────────────────────────────


def _is_recent_event_for_session(
    *,
    team_store,
    patient_id: str,
    event_type: str,
    session_id: str,
    within_seconds: int,
) -> bool:
    """Return True if there's an event of `event_type` whose payload
    `sessionId` matches the given session_id and was logged within the
    last `within_seconds`. Used by the 60-second video dedupe."""
    events = team_store.get_events(patient_id) or []
    if not events:
        return False
    cutoff = datetime.utcnow().timestamp() - int(within_seconds)
    for e in events:
        if e.get("event_type") != event_type:
            continue
        try:
            ts_str = str(e.get("occurred_at") or "")
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            if ts_naive.timestamp() < cutoff:
                continue
        except Exception:
            continue
        payload = e.get("payload") or {}
        if isinstance(payload, dict) and str(payload.get("sessionId")) == str(session_id):
            return True
    return False


def _is_recent_event(
    *,
    team_store,
    patient_id: str,
    event_type: str,
    within_seconds: int,
) -> bool:
    """Return True if any event of `event_type` was logged within the
    last `within_seconds` for the patient."""
    events = team_store.get_events(patient_id) or []
    if not events:
        return False
    cutoff = datetime.utcnow().timestamp() - int(within_seconds)
    for e in events:
        if e.get("event_type") != event_type:
            continue
        try:
            ts_str = str(e.get("occurred_at") or "")
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            if ts_naive.timestamp() >= cutoff:
                return True
        except Exception:
            continue
    return False
