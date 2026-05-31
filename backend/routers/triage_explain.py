"""Read-only fan-in for "Why this tier?" (Triage demo PRD §6)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from auth_roles import ALL_CLINICAL, require_roles
from staff_context import StaffContext, get_staff_context_optional
from tenant_constants import TRIAGEDM_CLINIC_CODE

router = APIRouter(tags=["triage-explain"])


def _patient_store(request: Request) -> Dict[str, Any]:
    return request.app.state.patient_store


def _team_store(request: Request):
    return request.app.state.team_store


async def _staff(authorization: Optional[str]) -> Optional[StaffContext]:
    return await get_staff_context_optional(authorization)


def _resolve_patient(request: Request, patient_id: str, staff: Optional[StaffContext]) -> Dict[str, Any]:
    store = _patient_store(request)
    patient = store.get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if staff and staff.source == "tenant" and staff.tenant_id:
        if (patient.get("health_system_id") or "") != staff.tenant_id:
            raise HTTPException(status_code=404, detail="Patient not found")
    return patient


def _reason_weight(r: Dict[str, Any]) -> int:
    w = r.get("weight")
    if w is None:
        return 0
    try:
        return int(w)
    except (TypeError, ValueError):
        return 0


def _top_contributing_reasons(
    *,
    patient: Dict[str, Any],
    ts,
    episode_id: str,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Merge tier-change reasons from recent events; dedupe by code; sort by weight desc."""
    candidates: List[Dict[str, Any]] = []

    pre = ts.list_preop_retier_events(episode_id, limit=3)
    for ev in pre:
        candidates.extend(ev.get("reasons") or [])

    post = ts.list_postop_retier_events(episode_id, limit=5)
    for ev in post:
        candidates.extend(ev.get("reasons") or [])

    intra = ts.list_intraop_reassessments(episode_id)
    if intra:
        candidates.extend(intra[-1].get("reasons") or [])

    by_code: Dict[str, Dict[str, Any]] = {}
    for r in candidates:
        code = str(r.get("code") or r.get("label") or "").strip()
        if not code:
            continue
        if code not in by_code or _reason_weight(r) >= _reason_weight(by_code[code]):
            by_code[code] = r

    merged = list(by_code.values())
    merged.sort(key=_reason_weight, reverse=True)
    return merged[:limit]


@router.get("/api/episodes/{episode_id}/triage-explain")
async def get_triage_explain(
    episode_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    staff = await _staff(authorization)
    require_roles(staff, ALL_CLINICAL)
    patient = _resolve_patient(request, episode_id, staff)
    ts = _team_store(request)

    snap = ts.get_episode_snapshot(episode_id) or {}
    events = ts.get_events(episode_id)
    initial_events = [e for e in events if e.get("event_type") == "INITIAL_TIER_ASSIGNED"]

    initial_reasons: List[Dict[str, Any]] = list(patient.get("initial_tier_reasons") or [])
    if not initial_reasons and initial_events:
        payload = initial_events[-1].get("payload") or {}
        initial_reasons = list(payload.get("reasons") or [])

    is_triage = (patient.get("clinic_code") or "").upper() == TRIAGEDM_CLINIC_CODE
    if is_triage:
        curated = patient.get("triage_explain_reasons")
        if curated:
            reasons = list(curated)
        else:
            ev_reasons = _top_contributing_reasons(patient=patient, ts=ts, episode_id=episode_id, limit=3)
            init_t = patient.get("initial_tier")
            cur_t = patient.get("current_tier")
            same = (
                init_t
                and cur_t
                and str(init_t).upper() == str(cur_t).upper()
            )
            if ev_reasons:
                reasons = ev_reasons
            elif same:
                reasons = []
            else:
                reasons = sorted((initial_reasons or [])[:], key=_reason_weight, reverse=True)[:3]
    else:
        reasons = initial_reasons

    preop_retier = ts.list_preop_retier_events(episode_id, limit=5)
    intraop = ts.list_intraop_reassessments(episode_id)
    postop_retier = ts.list_postop_retier_events(episode_id, limit=5)

    return {
        "patientId": episode_id,
        "currentTier": patient.get("current_tier"),
        "initialTier": patient.get("initial_tier"),
        "phase": patient.get("phase"),
        "reasons": reasons,
        "snapshot": {
            "initial_tier_was_hard_escalator": snap.get("initial_tier_was_hard_escalator"),
            "post_intake_tier": snap.get("post_intake_tier"),
            "post_intraop_tier": snap.get("post_intraop_tier"),
        },
        "recentPreopRetierEvents": preop_retier,
        "recentIntraopReassessments": intraop[-5:] if intraop else [],
        "recentPostopRetierEvents": postop_retier,
    }
