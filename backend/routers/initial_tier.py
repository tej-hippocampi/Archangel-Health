"""
Initial Pre-Op Triage HTTP surface (PRD §1 Initial Pre-Op Triage v1.0).

Routes:
    POST /api/triage/initial-tier/compute                   — pure preview
    POST /api/episodes/{episode_id}/initial-tier            — assign + persist
    POST /api/episodes/{episode_id}/initial-tier/override   — coordinator override
    GET  /api/triage/tuning/initial-tier/current            — read-only config
    POST /api/triage/tuning/initial-tier                    — admin no-op stub

The compute endpoint is a stateless preview; the assign endpoint persists
the algorithmic outcome onto the in-memory `_patient_store` blob (see
top-of-file Option B note in `team_store.py`) and writes an
`INITIAL_TIER_ASSIGNED` event row. The override endpoint requires
`reason` length ≥ 30 (PRD §13.13) and emits `INITIAL_TIER_OVERRIDDEN`.

The two `tuning` routes mirror the admin-portal contract surface used by
the other triage stages — the admin viewer continues to read tuning
through `/admin/triage/initial-tier/config`; these `/api/triage/tuning/...`
routes exist so the four stages have a uniform programmatic surface.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from auth_roles import ALL_CLINICAL, ALL_STAFF, WRITE_CLINICAL, require_roles
from staff_context import StaffContext, get_staff_context_optional
from triage import assign_initial_tier, get_config
from triage.types import InitialTierInput, Tier, TierAssignment


router = APIRouter(tags=["initial-tier"])


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
    return patient


def _verify_admin(x_admin_token: Optional[str]) -> None:
    expected = os.getenv("ADMIN_AUTH_TOKEN") or os.getenv("ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Admin token required")


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _actor_id(staff: Optional[StaffContext]) -> str:
    if not staff:
        return "anonymous"
    return staff.email or staff.role or "unknown"


def _persist_assignment(
    *,
    patient: Dict[str, Any],
    assignment: TierAssignment,
    input_snapshot: Dict[str, Any],
    team_store: Optional[Any] = None,
    patient_id: Optional[str] = None,
) -> None:
    """Denormalize the algorithmic outcome onto the in-memory patient blob
    AND write through to `episode_snapshots` (Pass 3 §1).

    The pre-op re-tier algorithm reads `initial_tier_was_hard_escalator`
    via the sticky-hard guard; the snapshot row keeps that flag alive
    across server restarts.
    """
    is_hard = bool(assignment.reasons and assignment.reasons[0].kind == "HARD")
    patient["initial_tier"] = assignment.tier
    patient["initial_tier_score"] = assignment.score
    patient["initial_tier_was_hard_escalator"] = is_hard
    patient["initial_tier_assigned_at"] = _utc_iso()
    patient["initial_tier_input_snapshot"] = input_snapshot
    patient["initial_tier_reasons"] = [r.model_dump() for r in assignment.reasons]
    patient["initial_tier_model_version"] = assignment.model_version
    patient["initial_tier_tuning_version"] = assignment.tuning_version
    # `current_tier` is the live tier the rest of the pipeline reads; we
    # only set it on initial assign — re-tier stages overwrite it later.
    patient.setdefault("current_tier", assignment.tier)
    if patient.get("current_tier") in (None, ""):
        patient["current_tier"] = assignment.tier

    if team_store is not None and patient_id:
        try:
            team_store.upsert_episode_snapshot(
                patient_id,
                initial_tier_was_hard_escalator=is_hard,
            )
        except Exception:
            # Snapshot write must never block the tier write itself.
            pass


# ─── 1. Pure preview ────────────────────────────────────────────────────────


@router.post("/api/triage/initial-tier/compute")
async def post_initial_tier_compute(
    payload: InitialTierInput,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Stateless preview — no persistence, no events. Pass-4 PRD §3.1
    treats `compute` as a clinical write surface (the surgeon's workflow
    decision tool), so NP/PA is blocked even though it's read-only at the
    storage layer."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    return assign_initial_tier(payload).model_dump()


# ─── 2. Assign + persist (idempotent) ───────────────────────────────────────


class AssignInitialTierRequest(BaseModel):
    """Wraps an `InitialTierInput` so future versions can attach metadata."""
    input: InitialTierInput


@router.post("/api/episodes/{episode_id}/initial-tier")
async def post_initial_tier_assign(
    episode_id: str,
    body: AssignInitialTierRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Assign the initial tier and persist onto `_patient_store[episode_id]`.

    Idempotent: if the same input snapshot has already been recorded, the
    persisted blob and event log are not duplicated. The tier reassignment
    itself is always recomputed (the algorithm is pure), so the response
    is always a fresh `TierAssignment`.
    """
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, episode_id, staff)
    team_store = _team_store(request)

    snapshot = body.input.model_dump()
    assignment = assign_initial_tier(body.input)

    prior_snapshot = patient.get("initial_tier_input_snapshot")
    is_repeat = prior_snapshot == snapshot
    if not is_repeat:
        _persist_assignment(
            patient=patient,
            assignment=assignment,
            input_snapshot=snapshot,
            team_store=team_store,
            patient_id=episode_id,
        )
        try:
            team_store.log_event(
                patient_id=episode_id,
                event_type="INITIAL_TIER_ASSIGNED",
                payload={
                    "tier": assignment.tier,
                    "score": assignment.score,
                    "reasonCodes": [r.code for r in assignment.reasons],
                    "isHardEscalator": bool(
                        assignment.reasons and assignment.reasons[0].kind == "HARD"
                    ),
                    "modelVersion": assignment.model_version,
                    "tuningVersion": assignment.tuning_version,
                    "actor": _actor_id(staff),
                },
            )
        except Exception:
            # Audit failure must never block the tier write.
            pass

    return {
        "ok": True,
        "idempotent": is_repeat,
        "assignment": assignment.model_dump(),
        "initialTier": patient.get("initial_tier"),
        "initialTierWasHardEscalator": bool(
            patient.get("initial_tier_was_hard_escalator", False)
        ),
        "currentTier": patient.get("current_tier"),
    }


# ─── 3. Override (coordinator) ──────────────────────────────────────────────


class InitialTierOverrideRequest(BaseModel):
    targetTier: Tier = Field(..., description="TIER_1 | TIER_2 | TIER_3")
    reason: str = Field(..., min_length=30, description="Free-text rationale (≥30 chars)")


@router.post("/api/episodes/{episode_id}/initial-tier/override")
async def post_initial_tier_override(
    episode_id: str,
    body: InitialTierOverrideRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Coordinator override of the algorithmic initial tier.

    Per PRD §13.13, the override changes `current_tier` *but* leaves the
    `initial_tier_was_hard_escalator` flag unchanged so the pre-op re-tier
    sticky-hard guard still respects the underlying clinical condition.
    """
    staff = await _resolve_staff(authorization)
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, episode_id, staff)
    team_store = _team_store(request)

    if patient.get("initial_tier") is None:
        raise HTTPException(
            status_code=409,
            detail="Cannot override before initial tier is assigned",
        )

    prior_tier = patient.get("current_tier") or patient.get("initial_tier")
    patient["current_tier"] = body.targetTier
    patient["initial_tier_override"] = body.targetTier
    patient["initial_tier_override_reason"] = body.reason
    patient["initial_tier_override_by"] = _actor_id(staff)
    patient["initial_tier_override_at"] = _utc_iso()

    try:
        team_store.log_event(
            patient_id=episode_id,
            event_type="INITIAL_TIER_OVERRIDDEN",
            payload={
                "priorTier": prior_tier,
                "targetTier": body.targetTier,
                "reason": body.reason,
                "actor": _actor_id(staff),
            },
        )
    except Exception:
        pass

    return {
        "ok": True,
        "currentTier": patient["current_tier"],
        "initialTier": patient.get("initial_tier"),
        "initialTierWasHardEscalator": bool(
            patient.get("initial_tier_was_hard_escalator", False)
        ),
        "override": {
            "targetTier": body.targetTier,
            "reason": body.reason,
            "by": patient["initial_tier_override_by"],
            "at": patient["initial_tier_override_at"],
        },
    }


# ─── 4. Tuning (read + admin no-op) ─────────────────────────────────────────


@router.get("/api/triage/tuning/initial-tier/current")
async def get_initial_tier_tuning_current(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Read tuning snapshot. Pass-4: any authenticated staff role."""
    staff = await _resolve_staff(authorization)
    require_roles(staff, ALL_CLINICAL)
    return get_config()


@router.post("/api/triage/tuning/initial-tier")
async def post_initial_tier_tuning(
    request: Request,
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Admin contract surface (no-op deploy v1).

    Mirrors the prior triage stages — accepts (and ignores) a tuning
    payload, returns the current static config so client tooling can
    confirm a deploy round-trip. Use `/admin/triage/initial-tier/config`
    for the read-side rendered in the admin portal.
    """
    _verify_admin(x_admin_token)
    # Body is intentionally not bound — the PRD specifies a no-op
    # endpoint until a live-tuning store exists.
    try:
        await request.json()
    except Exception:
        pass
    return {"ok": True, "deployed": False, "config": get_config()}
