"""
Intra-Op Reassessment HTTP surface (PRD §8).

Routes:
    GET    /api/episodes/{patient_id}/intraop-form
    PATCH  /api/episodes/{patient_id}/intraop-form           (autosave)
    POST   /api/episodes/{patient_id}/intraop-form           (idempotent ensure)
    POST   /api/episodes/{patient_id}/intraop-form/lock      (surgeon)
    POST   /api/episodes/{patient_id}/intraop-form/reopen    (admin)
    POST   /api/episodes/{patient_id}/intraop-form/preview   (live tier preview)
    POST   /api/episodes/{patient_id}/intraop-form/pdf       (multipart upload)
    GET    /api/intraop-extractions/{extraction_id}
    GET    /api/intraop-extractions/{extraction_id}/stream   (SSE)
    GET    /api/episodes/{patient_id}/intraop-reassessments
    POST   /api/episodes/{patient_id}/switch-to-postop       (surgeon CTA on doctor.html)

Authentication: every route accepts an optional Bearer token resolved
through `get_staff_context_optional`. Admin-only routes additionally
verify `X-Admin-Token`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from auth_roles import (
    ALL_CLINICAL,
    WRITE_CLINICAL,
    require_roles,
)
from staff_context import StaffContext, get_staff_context_optional
from triage.intraop.apply import apply_intraop_reassessment
from triage.intraop.delta import compute_intraop_delta
from triage.intraop.extraction_job import run_extraction_job
from triage.intraop.extractor import IntraopExtractor, MockIntraopExtractor
from triage.intraop.form_validation import (
    or_duration_consistent_with_timestamps,
    validate_required_fields,
)
from triage.intraop.patient_state import (
    ensure_intraop_patient_state,
    get_anchor_procedure_family,
    get_current_tier,
    set_or_ended_at,
    set_or_started_at,
    set_phase,
    to_public,
)
from triage.intraop.resolve import resolve_final_tier
from triage.intraop.tuning import EXTRACTION, MODEL_VERSION, PROCEDURE_P90_MINUTES
from triage.intraop.types import HospitalProcedureStats


log = logging.getLogger("intraop.router")

router = APIRouter(tags=["intraop"])


# ─── Storage layout for uploaded PDFs ───────────────────────────────────────

_INTRAOP_UPLOAD_DIR = Path(
    os.getenv("INTRAOP_UPLOAD_DIR", os.getenv("UPLOAD_DIR", "/tmp/elysium-intraop"))
).resolve()
_INTRAOP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _patient_store(request: Request) -> Dict[str, Any]:
    return request.app.state.patient_store


def _team_store(request: Request):
    return request.app.state.team_store


def _resolve_patient(request: Request, patient_id: str, staff: Optional[StaffContext]) -> Dict[str, Any]:
    store = _patient_store(request)
    patient = store.get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if staff and staff.source == "tenant" and staff.tenant_id:
        if (patient.get("health_system_id") or "") != staff.tenant_id:
            raise HTTPException(status_code=404, detail="Patient not found")
    ensure_intraop_patient_state(patient)
    return patient


def _verify_admin(x_admin_token: Optional[str]) -> None:
    expected = os.getenv("ADMIN_AUTH_TOKEN") or os.getenv("ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Admin token required")


def _format_sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _actor_id(staff: Optional[StaffContext]) -> str:
    if not staff:
        return "anonymous"
    return staff.email or staff.role or "unknown"


def _get_extractor(request: Request) -> IntraopExtractor:
    """Resolve the extractor from app.state when set, otherwise pick by env."""
    e = getattr(request.app.state, "intraop_extractor", None)
    if e is not None:
        return e
    if os.getenv("INTRAOP_USE_LLM_EXTRACTOR") in ("1", "true", "TRUE"):
        from triage.intraop.extractor_llm import LlmIntraopExtractor
        return LlmIntraopExtractor()
    return MockIntraopExtractor()


def _form_response(form_record: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a stored form row for the API response."""
    return {
        "id":                 form_record["id"],
        "patientId":          form_record["patient_id"],
        "status":             form_record["status"],
        "orStartedAt":        form_record.get("or_started_at"),
        "orEndedAt":          form_record.get("or_ended_at"),
        "orDurationMinutes":  form_record.get("or_duration_minutes"),
        "fields":             form_record.get("fields") or {},
        "fieldOrigins":       form_record.get("field_origins") or {},
        "procedureSpecific":  form_record.get("procedure_specific"),
        "pdfBlobUrl":         form_record.get("pdf_blob_url"),
        "extractionId":       form_record.get("extraction_id"),
        "surgeonLockedBy":    form_record.get("surgeon_locked_by"),
        "surgeonLockedAt":    form_record.get("surgeon_locked_at"),
        "draftCompletedBy":   form_record.get("draft_completed_by"),
        "draftCompletedAt":   form_record.get("draft_completed_at"),
        "conservativeDefaultAppliedAt": form_record.get("conservative_default_applied_at"),
        "createdAt":          form_record.get("created_at"),
        "updatedAt":          form_record.get("updated_at"),
    }


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


# ─── GET / POST / PATCH form ────────────────────────────────────────────────

class FormUpsertRequest(BaseModel):
    fields: Optional[Dict[str, Any]] = None
    field_origins: Optional[Dict[str, Any]] = Field(default=None, alias="fieldOrigins")
    procedure_specific: Optional[Dict[str, Any]] = Field(default=None, alias="procedureSpecific")
    or_started_at: Optional[str] = Field(default=None, alias="orStartedAt")
    or_ended_at: Optional[str] = Field(default=None, alias="orEndedAt")
    or_duration_minutes: Optional[int] = Field(default=None, alias="orDurationMinutes")

    class Config:
        populate_by_name = True


@router.get("/api/episodes/{patient_id}/intraop-form")
async def get_intraop_form(
    patient_id: str,
    request: Request,
    staff: Optional[StaffContext] = None,
):
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, ALL_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)
    form = store.get_intraop_form(patient_id)
    if not form:
        return JSONResponse({"form": None, "patient": to_public(patient)})
    return JSONResponse({"form": _form_response(form), "patient": to_public(patient)})


@router.post("/api/episodes/{patient_id}/intraop-form")
async def create_or_get_intraop_form(
    patient_id: str,
    request: Request,
    body: FormUpsertRequest = FormUpsertRequest(),
):
    """Idempotent: returns the existing form when one exists, else creates a NEW row."""
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    # If the patient just transitioned in, capture or_started_at / or_ended_at
    # from either the body or the patient blob (set by the doctor.html CTA).
    created = store.get_or_create_intraop_form(
        patient_id=patient_id,
        or_started_at=body.or_started_at or patient.get("or_started_at"),
        or_ended_at=body.or_ended_at or patient.get("or_ended_at"),
        or_duration_minutes=body.or_duration_minutes,
    )
    return JSONResponse({"form": _form_response(created), "patient": to_public(patient)})


def _merge_field_origins(
    existing_origins: Dict[str, Any],
    new_fields: Dict[str, Any],
    new_origins: Optional[Dict[str, Any]],
    actor: str,
) -> Dict[str, Any]:
    """Stamp new MANUAL origins for any newly-touched key the caller didn't
    explicitly annotate. Preserves existing origins (including AUTO_POP_*)
    unless the value changed."""
    merged: Dict[str, Any] = dict(existing_origins or {})
    explicit = new_origins or {}
    for k, v in new_fields.items():
        if k in explicit:
            merged[k] = explicit[k]
            continue
        prior = merged.get(k)
        if prior is None:
            merged[k] = {"origin": "MANUAL", "populated_at": _utc_iso(), "source": actor}
        else:
            # If surgeon edits an auto-pop value, preserve original_value, flip origin to MANUAL.
            existing_value_origin = prior.get("origin")
            if existing_value_origin in ("AUTO_POP_AIMS", "AUTO_POP_PDF"):
                merged[k] = {
                    "origin": "MANUAL",
                    "populated_at": _utc_iso(),
                    "source": actor,
                    "original_value": prior.get("source") and v,
                }
    return merged


@router.patch("/api/episodes/{patient_id}/intraop-form")
async def patch_intraop_form(
    patient_id: str,
    request: Request,
    body: FormUpsertRequest,
):
    """Autosave endpoint: merges supplied fields into the form blob.

    Pass-4 status-aware role gating (PRD §4.2):
      - status in {NEW, IN_PROGRESS, REOPENED}    → rn_coordinator
      - status == READY_FOR_SURGEON_REVIEW        → surgeon
      - status == LOCKED                          → 409 (admin REOPEN required)
    """
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    existing = store.get_intraop_form(patient_id) or store.get_or_create_intraop_form(patient_id=patient_id)
    current_status = existing.get("status") or "NEW"
    if current_status == "LOCKED":
        raise HTTPException(status_code=409, detail="Form is LOCKED — admin REOPEN required to edit.")

    role = (staff.role or "").lower() if staff else ""
    if current_status == "READY_FOR_SURGEON_REVIEW":
        if role != "surgeon":
            raise HTTPException(
                status_code=403,
                detail="Form is awaiting surgeon review — only the surgeon can edit. RNs may recall the draft.",
            )
    else:
        if role != "rn_coordinator":
            raise HTTPException(
                status_code=403,
                detail="Only the RN coordinator can edit the draft until it is marked ready for review.",
            )

    merged_fields = dict(existing.get("fields") or {})
    if body.fields:
        merged_fields.update(body.fields)
    merged_origins = _merge_field_origins(
        existing.get("field_origins") or {},
        body.fields or {},
        body.field_origins,
        actor=_actor_id(staff),
    )

    if current_status == "READY_FOR_SURGEON_REVIEW":
        new_status = "READY_FOR_SURGEON_REVIEW"
    else:
        new_status = "IN_PROGRESS" if current_status == "NEW" else current_status
        if current_status == "REOPENED":
            new_status = "IN_PROGRESS"
    missing = validate_required_fields(merged_fields)

    out = store.update_intraop_form_fields(
        patient_id=patient_id,
        fields=merged_fields,
        field_origins=merged_origins,
        procedure_specific=body.procedure_specific,
        or_started_at=body.or_started_at,
        or_ended_at=body.or_ended_at,
        or_duration_minutes=body.or_duration_minutes,
        status=new_status,
    )
    return JSONResponse({"form": _form_response(out), "missing": missing, "patient": to_public(patient)})


# ─── Lock / reopen ──────────────────────────────────────────────────────────

@router.post("/api/episodes/{patient_id}/intraop-form/lock")
async def lock_intraop_form(
    patient_id: str,
    request: Request,
):
    """Surgeon lock — finalizes the intra-op reassessment (PRD §4.5).

    Pass-4: surgeon role required AND form must be in `READY_FOR_SURGEON_REVIEW`.
    """
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, {"surgeon"})
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    form = store.get_intraop_form(patient_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intra-op form not found")
    status_now = form.get("status") or "NEW"
    if status_now == "LOCKED":
        raise HTTPException(status_code=409, detail="Form already locked")
    if status_now != "READY_FOR_SURGEON_REVIEW":
        if status_now in ("NEW", "IN_PROGRESS", "REOPENED"):
            raise HTTPException(
                status_code=409,
                detail="RN coordinator must mark the draft ready for review before lock.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Form must be in READY_FOR_SURGEON_REVIEW to lock (currently {status_now}).",
        )

    missing = validate_required_fields(form.get("fields") or {})
    if missing:
        return JSONResponse(
            status_code=422,
            content={"detail": "Missing required fields", "missing": missing},
        )
    if not or_duration_consistent_with_timestamps(form.get("fields") or {}):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "OR duration disagrees with start/end timestamps",
                "missing": ["or_duration_minutes"],
            },
        )

    locked = store.lock_intraop_form(patient_id=patient_id, surgeon_user_id=_actor_id(staff))
    if locked is None:
        raise HTTPException(status_code=409, detail="Form already locked")

    try:
        store.log_event(
            patient_id=patient_id,
            event_type="INTRAOP_FORM_LOCKED",
            payload={"actor": _actor_id(staff), "draftCompletedBy": form.get("draft_completed_by")},
        )
    except Exception:
        pass

    event = apply_intraop_reassessment(
        patient_id=patient_id,
        patient_store=_patient_store(request),
        team_store=store,
        triggered_by=f"SURGEON_LOCK:{_actor_id(staff)}",
    )

    return JSONResponse({
        "form": _form_response(store.get_intraop_form(patient_id) or {}),
        "reassessment": event.model_dump(),
        "patient": to_public(patient),
    })


@router.post("/api/episodes/{patient_id}/intraop-form/mark-ready-for-review")
async def mark_intraop_form_ready_for_review(
    patient_id: str,
    request: Request,
):
    """RN flips an IN_PROGRESS form to READY_FOR_SURGEON_REVIEW (PRD §4.4).

    Validates required fields and OR-duration consistency, persists draft
    attribution, opens an `escalations` row tagged `intraop:ready_for_review`,
    and logs `INTRAOP_FORM_READY_FOR_REVIEW`.
    """
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, {"rn_coordinator"})
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    form = store.get_intraop_form(patient_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intra-op form not found")
    status_now = form.get("status") or "NEW"
    if status_now == "LOCKED":
        raise HTTPException(status_code=409, detail="Form is LOCKED — cannot move back to review.")
    if status_now == "READY_FOR_SURGEON_REVIEW":
        raise HTTPException(status_code=409, detail="Form is already awaiting surgeon review.")
    if status_now not in ("NEW", "IN_PROGRESS", "REOPENED"):
        raise HTTPException(status_code=409, detail=f"Cannot mark a form in status {status_now} ready for review.")

    fields = form.get("fields") or {}
    missing = validate_required_fields(fields)
    if missing:
        return JSONResponse(
            status_code=422,
            content={"detail": "Missing required fields", "missing": missing},
        )
    if not or_duration_consistent_with_timestamps(fields):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "OR duration disagrees with start/end timestamps",
                "missing": ["or_duration_minutes"],
            },
        )

    actor = _actor_id(staff)
    moved = store.mark_intraop_form_ready_for_review(
        patient_id=patient_id,
        rn_user_id=actor,
    )
    if moved is None:
        raise HTTPException(status_code=409, detail="Form is no longer eligible to mark ready for review.")

    try:
        hs_id = patient.get("health_system_id")
        store.create_escalation(
            patient_id=patient_id,
            tier=2,
            trigger_type="intraop:ready_for_review",
            message=f"Intra-op form awaiting surgeon review (drafted by {actor}).",
            conversation_snapshot=[],
            health_system_id=hs_id,
        )
    except Exception:
        log.exception("intraop.mark_ready: escalation failed for patient_id=%s", patient_id)
    try:
        store.log_event(
            patient_id=patient_id,
            event_type="INTRAOP_FORM_READY_FOR_REVIEW",
            payload={"actor": actor},
        )
    except Exception:
        pass

    return JSONResponse({"form": _form_response(moved), "patient": to_public(patient)})


@router.post("/api/episodes/{patient_id}/intraop-form/recall")
async def recall_intraop_form_draft(
    patient_id: str,
    request: Request,
):
    """RN pulls a READY_FOR_SURGEON_REVIEW draft back to IN_PROGRESS (PRD §4.4)."""
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, {"rn_coordinator"})
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    form = store.get_intraop_form(patient_id)
    if not form:
        raise HTTPException(status_code=404, detail="Intra-op form not found")
    status_now = form.get("status") or "NEW"
    if status_now != "READY_FOR_SURGEON_REVIEW":
        raise HTTPException(
            status_code=409,
            detail=f"Recall only applies to READY_FOR_SURGEON_REVIEW (currently {status_now}).",
        )

    actor = _actor_id(staff)
    recalled = store.recall_intraop_form_draft(patient_id=patient_id)
    if recalled is None:
        raise HTTPException(status_code=409, detail="Form is no longer in the review queue.")

    try:
        hs_id = patient.get("health_system_id")
        store.create_escalation(
            patient_id=patient_id,
            tier=2,
            trigger_type="intraop:draft_recalled",
            message=f"Intra-op draft recalled by {actor}.",
            conversation_snapshot=[],
            health_system_id=hs_id,
        )
    except Exception:
        log.exception("intraop.recall: escalation failed for patient_id=%s", patient_id)
    try:
        store.log_event(
            patient_id=patient_id,
            event_type="INTRAOP_FORM_RECALLED",
            payload={"actor": actor},
        )
    except Exception:
        pass

    return JSONResponse({"form": _form_response(recalled), "patient": to_public(patient)})


@router.get("/api/intraop-forms")
async def list_intraop_forms(
    request: Request,
    status: str = "READY_FOR_SURGEON_REVIEW",
):
    """Surgeon "Forms awaiting your review" surface (PRD §4.6).

    Tenant-scoped. Restricted to surgeons (the only role expected to act
    on the queue).
    """
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, {"surgeon"})
    if status not in ("READY_FOR_SURGEON_REVIEW", "IN_PROGRESS", "LOCKED", "REOPENED", "NEW"):
        raise HTTPException(status_code=400, detail="Unsupported status filter.")
    store = _team_store(request)
    rows = store.list_intraop_forms_by_status(status)
    items: List[Dict[str, Any]] = []
    pstore = _patient_store(request)
    for r in rows:
        pid = r.get("patient_id")
        patient = pstore.get(pid) if pid else None
        if staff and staff.source == "tenant" and staff.tenant_id:
            if not patient or (patient.get("health_system_id") or "") != staff.tenant_id:
                continue
        meta = {
            "patientName": (patient or {}).get("name") if patient else None,
            "currentTier": (patient or {}).get("current_tier") if patient else None,
            "procedure":   ((patient or {}).get("structured_data") or {}).get("procedure_name") if patient else None,
        }
        items.append({**_form_response(r), **meta})
    return JSONResponse({"items": items, "status": status})


@router.post("/api/episodes/{patient_id}/intraop-form/reopen")
async def reopen_intraop_form(
    patient_id: str,
    request: Request,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """Reopen a LOCKED form. Pass-4: accepts either a valid admin token OR a
    surgeon Bearer whose email matches `surgeon_locked_by`."""
    expected = os.getenv("ADMIN_AUTH_TOKEN") or os.getenv("ADMIN_TOKEN")
    is_admin = bool(expected and x_admin_token == expected)

    staff = await get_staff_context_optional(request.headers.get("authorization"))
    actor_label = "admin"
    if not is_admin:
        if staff is None:
            raise HTTPException(
                status_code=401,
                detail="Reopen requires admin token or the locking surgeon's session.",
            )
        if (staff.role or "").lower() != "surgeon":
            raise HTTPException(
                status_code=403,
                detail="Only the locking surgeon (or admin) can reopen a locked form.",
            )
        existing = _team_store(request).get_intraop_form(patient_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Intra-op form not found")
        locker = (existing.get("surgeon_locked_by") or "").lower()
        actor_email = (staff.email or "").lower()
        if not locker or locker != actor_email:
            raise HTTPException(
                status_code=403,
                detail="Only the locking surgeon may reopen this form.",
            )
        actor_label = staff.email or "surgeon"

    patient = _resolve_patient(request, patient_id, staff if not is_admin else None)
    store = _team_store(request)
    reopened = store.reopen_intraop_form(patient_id=patient_id)
    if reopened is None:
        raise HTTPException(status_code=409, detail="Form is not LOCKED — cannot reopen")
    try:
        store.log_event(
            patient_id=patient_id,
            event_type="INTRAOP_FORM_REOPENED",
            payload={"actor": actor_label},
        )
    except Exception:
        pass
    return JSONResponse({"form": _form_response(reopened), "patient": to_public(patient)})


# ─── Live preview (right-rail tier indicator) ───────────────────────────────

@router.post("/api/episodes/{patient_id}/intraop-form/preview")
async def preview_intraop_tier(
    patient_id: str,
    request: Request,
):
    """Live tier preview. Pass-4: any clinical staff role (read-only)."""
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, ALL_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)
    form = store.get_intraop_form(patient_id)
    fields = (form or {}).get("fields") or {}

    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    fields = {**fields, **(body.get("fields") or body or {})}

    family = get_anchor_procedure_family(patient)
    current_tier = get_current_tier(patient)

    from triage.intraop.types import IntraopForm
    try:
        form_obj = IntraopForm(**fields)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Invalid form fields: {e}")

    delta = compute_intraop_delta(
        form_obj, family,
        HospitalProcedureStats(or_duration_p90_minutes=PROCEDURE_P90_MINUTES),
        current_tier,
    )
    final = resolve_final_tier(current_tier, delta.proposed_tier)
    return JSONResponse({
        "currentTier": current_tier,
        "proposedTier": delta.proposed_tier,
        "finalTier": final,
        "hardUpgradeApplied": delta.hard_upgrade_applied,
        "upgradeSteps": delta.upgrade_steps,
        "reasons": [r.model_dump() for r in delta.reasons],
        "modelVersion": MODEL_VERSION,
    })


# ─── PDF upload + extraction job ────────────────────────────────────────────

@router.post("/api/episodes/{patient_id}/intraop-form/pdf")
async def upload_intraop_pdf(
    patient_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    """Upload AIMS / chart PDF for extraction. Pass-4: clinical write role."""
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, WRITE_CLINICAL)
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    contents = await file.read()
    max_bytes = EXTRACTION["max_pdf_size_mb"] * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"PDF exceeds {EXTRACTION['max_pdf_size_mb']} MB limit",
        )

    # Persist the upload to disk under $UPLOAD_DIR/intraop/<patient>/<ext>.pdf.
    target_dir = _INTRAOP_UPLOAD_DIR / patient_id
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    extraction_id = uuid.uuid4().hex
    target_path = target_dir / f"{extraction_id}.pdf"
    target_path.write_bytes(contents)
    blob_url = f"file://{target_path}"

    # Make sure a form row exists, then enqueue the extraction.
    store.get_or_create_intraop_form(patient_id=patient_id)
    store.save_intraop_extraction(
        extraction_id=extraction_id,
        patient_id=patient_id,
        pdf_blob_url=blob_url,
        status="PENDING",
        model_version=EXTRACTION["model_version"],
        prompt_version=EXTRACTION["prompt_version"],
    )

    extractor = _get_extractor(request)
    sd = patient.get("structured_data") or {}
    asyncio.create_task(run_extraction_job(
        extraction_id=extraction_id,
        patient_id=patient_id,
        pdf_bytes=contents,
        pdf_blob_url=blob_url,
        procedure_family=get_anchor_procedure_family(patient),
        procedure_name=sd.get("procedure_name"),
        extractor=extractor,
        team_store=store,
    ))

    return JSONResponse({
        "extractionId": extraction_id,
        "status": "PENDING",
        "pdfBlobUrl": blob_url,
    })


# ─── Extraction status (polling + SSE) ──────────────────────────────────────

@router.get("/api/intraop-extractions/{extraction_id}")
async def get_intraop_extraction(extraction_id: str, request: Request):
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, ALL_CLINICAL)
    store = _team_store(request)
    rec = store.get_intraop_extraction(extraction_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Extraction not found")
    return JSONResponse({
        "id":               rec["id"],
        "status":           rec["status"],
        "fields":           rec.get("fields") or {},
        "fieldConfidences": rec.get("field_confidences") or {},
        "warnings":         rec.get("warnings") or [],
        "modelVersion":     rec.get("model_version"),
        "promptVersion":    rec.get("prompt_version"),
        "errorMessage":     rec.get("error_message"),
        "startedAt":        rec.get("started_at"),
        "completedAt":      rec.get("completed_at"),
    })


@router.get("/api/intraop-extractions/{extraction_id}/stream")
async def stream_intraop_extraction(extraction_id: str, request: Request):
    """SSE poller — emits `status` updates until COMPLETE / FAILED."""
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, ALL_CLINICAL)
    store = _team_store(request)

    async def generator():
        last_status: Optional[str] = None
        for _ in range(120):  # ~2 minutes ceiling
            rec = store.get_intraop_extraction(extraction_id)
            if not rec:
                yield _format_sse("error", {"message": "Extraction not found"})
                return
            if rec["status"] != last_status:
                yield _format_sse("status", {
                    "id":     rec["id"],
                    "status": rec["status"],
                    "fields": rec.get("fields") or {},
                    "warnings": rec.get("warnings") or [],
                })
                last_status = rec["status"]
            if rec["status"] in ("COMPLETE", "FAILED"):
                return
            await asyncio.sleep(1.0)
        yield _format_sse("error", {"message": "Stream timeout"})

    return StreamingResponse(generator(), media_type="text/event-stream")


# ─── Reassessment history ───────────────────────────────────────────────────

@router.get("/api/episodes/{patient_id}/intraop-reassessments")
async def list_intraop_reassessments(patient_id: str, request: Request):
    """Reassessment audit list. Pass-4: any clinical staff role."""
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, ALL_CLINICAL)
    _resolve_patient(request, patient_id, staff)
    store = _team_store(request)
    rows = store.list_intraop_reassessments(patient_id)
    return JSONResponse({
        "items": [
            {
                "id":                  r["id"],
                "preOrCurrentTier":    r["pre_or_current_tier"],
                "proposedTier":        r["proposed_tier"],
                "finalTier":           r["final_tier"],
                "hardUpgradeApplied":  r["hard_upgrade_applied"],
                "upgradeSteps":        r["upgrade_steps"],
                "reasons":             r["reasons"],
                "isConservativeDefault": r["is_conservative_default"],
                "procedureFamily":     r.get("procedure_family"),
                "modelVersion":        r.get("model_version"),
                "tuningVersion":       r.get("tuning_version"),
                "triggeredBy":         r.get("triggered_by"),
                "triggeredAt":         r.get("triggered_at"),
            } for r in rows
        ],
    })


# ─── Switch to post-op CTA (doctor.html) ────────────────────────────────────

class SwitchToPostopRequest(BaseModel):
    or_started_at: Optional[str] = Field(default=None, alias="orStartedAt")
    or_ended_at: Optional[str] = Field(default=None, alias="orEndedAt")

    class Config:
        populate_by_name = True


@router.post("/api/episodes/{patient_id}/switch-to-postop")
async def switch_to_postop(
    patient_id: str,
    request: Request,
    body: SwitchToPostopRequest = SwitchToPostopRequest(),
):
    """Surgeon-triggered transition: marks OR as ended, ensures a form row,
    and returns the form id so the UI can navigate to the intra-op form.
    Pass-4: surgeon role required.
    """
    staff = await get_staff_context_optional(request.headers.get("authorization"))
    require_roles(staff, {"surgeon"})
    patient = _resolve_patient(request, patient_id, staff)
    store = _team_store(request)

    or_ended_at = body.or_ended_at or _utc_iso()
    if body.or_started_at:
        set_or_started_at(patient, body.or_started_at)
    set_or_ended_at(patient, or_ended_at)
    set_phase(patient, "intra_op")

    form = store.get_or_create_intraop_form(
        patient_id=patient_id,
        or_started_at=patient.get("or_started_at"),
        or_ended_at=or_ended_at,
    )
    try:
        store.log_event(
            patient_id=patient_id,
            event_type="OR_ENDED",
            payload={"orEndedAt": or_ended_at, "actor": _actor_id(staff)},
        )
    except Exception:
        pass

    return JSONResponse({"form": _form_response(form), "patient": to_public(patient)})
