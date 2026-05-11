"""TEAM eligibility endpoints — PRD §8.

Routes (no router prefix; each route uses an explicit /api/eligibility-... path):
  POST   /api/eligibility-documents          multipart upload
  DELETE /api/eligibility-documents/{id}
  POST   /api/eligibility-checks             create + start pipeline
  GET    /api/eligibility-checks/{id}
  GET    /api/eligibility-checks/{id}/stream SSE progress
  POST   /api/eligibility-checks/{id}/override
  POST   /api/eligibility-checks/{id}/rerun
  POST   /api/eligibility-checks/{id}/finalize
  POST   /api/eligibility-batches            multipart fan-out upload
  GET    /api/eligibility-batches/{id}
  GET    /api/eligibility-batches/{id}/stream

  GET    /api/patient/{id}/postop-notes      Track B
  POST   /api/patient/{id}/postop-notes/confirm
  GET    /api/patient/{id}/preop-notes
  POST   /api/patient/{id}/preop-notes/confirm

  GET    /admin/audit/eligibility            audit viewer
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from eligibility import evaluate as eval_mod
from eligibility import format_detect, pipeline, store
from eligibility.parse_x12 import InvalidX12Error
from eligibility.parse_pdf import PDFEncryptedError
from staff_context import StaffContext, get_staff_context_optional

log = logging.getLogger("eligibility.router")

router = APIRouter(tags=["eligibility"])

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/elysium-eligibility")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


# ─── Helpers ────────────────────────────────────────────────────────────────
def _utc_iso() -> str:
    from datetime import datetime

    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _actor_id(staff: Optional[StaffContext]) -> str:
    if not staff:
        return "anonymous"
    return f"{staff.source}:{staff.email or ''}"


async def _resolve_staff_with_query_fallback(request: Request) -> Optional[StaffContext]:
    """EventSource can't set headers — fall back to ?token= for SSE routes."""
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return await get_staff_context_optional(authorization=auth_header)
    tok = request.query_params.get("token")
    if tok:
        return await get_staff_context_optional(authorization=f"Bearer {tok}")
    return None


def _patient_store(request: Request) -> Dict[str, Any]:
    return request.app.state.patient_store


def _assert_patient_access(patient_id: str, staff: Optional[StaffContext], store_dict: Dict[str, Any]) -> None:
    if patient_id not in store_dict:
        raise HTTPException(status_code=404, detail="Patient not found")
    if staff and staff.source == "tenant" and staff.tenant_id:
        d = store_dict[patient_id]
        if (d.get("health_system_id") or "") != staff.tenant_id:
            raise HTTPException(status_code=404, detail="Patient not found")


# ─── Draft patient lifecycle ────────────────────────────────────────────────
class DraftPatientRequest(BaseModel):
    name: str
    phone: Optional[str] = ""
    email: Optional[str] = ""
    mbi: Optional[str] = ""
    dob: Optional[str] = None
    scheduled_surgery_date: Optional[str] = None
    anchor_procedure: Optional[str] = None


@router.post("/api/eligibility-draft-patient")
async def create_draft_patient(
    request: Request,
    body: DraftPatientRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Patient name is required")
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="Patient name must be 1-120 chars")

    mbi = (body.mbi or "").strip().upper()
    if mbi and not re.match(r"^[1-9][A-Z][A-Z0-9][0-9][A-Z][A-Z0-9][0-9][A-Z][A-Z][0-9]{2}$", mbi):
        raise HTTPException(status_code=400, detail="MBI format invalid")
    if body.scheduled_surgery_date:
        _validate_iso_date(body.scheduled_surgery_date, "scheduled_surgery_date")
    if body.dob:
        _validate_iso_date(body.dob, "dob")

    hs_id = staff.tenant_id if (staff and staff.source == "tenant") else None
    clinic_guess = ""
    if staff and staff.source == "tenant":
        clinic_guess = (staff.health_system_code or "").strip().upper()
    store_dict = _patient_store(request)

    # PRD §11.14: duplicate MBI detection — surface the existing patient
    # rather than silently creating a duplicate.
    if mbi:
        for existing_pid, existing in list(store_dict.items()):
            existing_mbi = str((existing.get("structured_data") or {}).get("mbi") or existing.get("mbi") or "").upper().strip()
            if existing_mbi and existing_mbi == mbi:
                same_tenant = (existing.get("health_system_id") or "") == (hs_id or "")
                if same_tenant:
                    return {
                        "id": existing_pid,
                        "eligibility_status": existing.get("eligibility_status") or "PENDING",
                        "conflict": "existing",
                        "existing_name": existing.get("name"),
                    }

    pid = uuid.uuid4().hex
    store_dict[pid] = {
        "name": name,
        "health_system_id": hs_id,
        "phone": body.phone or "",
        "email": body.email or "",
        "pipeline_type": "pre_op",
        "voice_audio_url": None,
        "battlecard_html": None,
        "avatar_url": None,
        "voice_script": None,
        "structured_data": {
            "patient_name": name,
            "procedure_name": (body.anchor_procedure or ""),
            "procedure_date": body.scheduled_surgery_date or "",
            "status": "scheduled",
            "dob": body.dob,
            "mbi": mbi,
        },
        "clinic_code": clinic_guess,
        "resource_code": "",
        "office_phone": "",
        "resources": None,
        "pcp_referral_sent": False,
        "pcp_name": "",
        "eligibility_status": "DRAFT",
        "eligibility_check_id": None,
        "relevant_files": [],
        "mbi": mbi,
        "is_draft": True,
    }

    store.append_audit(
        action="patient_created",
        actor=_actor_id(staff),
        patient_id=pid,
        meta={"draft": True},
    )
    return {"id": pid, "eligibility_status": "DRAFT"}


@router.delete("/api/eligibility-draft-patients/{patient_id}")
async def delete_draft_patient(
    patient_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Hard-delete a draft patient and all attached eligibility docs (PRD AC-S9).

    Refuses to delete patients without ``is_draft=True`` so it cannot be
    misused to evict a finalized patient from the roster.
    """
    store_dict = _patient_store(request)
    rec = store_dict.get(patient_id)
    if not rec:
        return {"ok": True, "already_gone": True}
    if not rec.get("is_draft"):
        # Refuse to delete non-draft patients via this endpoint
        raise HTTPException(status_code=409, detail="Cannot hard-delete a finalized patient via this endpoint")

    if staff and staff.source == "tenant" and staff.tenant_id:
        if (rec.get("health_system_id") or "") != staff.tenant_id:
            raise HTTPException(status_code=404, detail="Patient not found")

    # Remove uploaded docs
    for doc_id in list(rec.get("relevant_files") or []):
        d = store.get_doc(doc_id)
        if d:
            try:
                p = Path(d["path"])
                if p.exists():
                    p.unlink()
            except Exception as e:
                log.warning("Failed to unlink %s: %s", d.get("path"), e)
            store.delete_doc(doc_id)
    store_dict.pop(patient_id, None)
    store.append_audit(
        action="patient_deleted",
        actor=_actor_id(staff),
        patient_id=patient_id,
        meta={"draft": True},
    )
    return {"ok": True}


# ─── Upload / delete ────────────────────────────────────────────────────────
@router.post("/api/eligibility-documents")
async def upload_eligibility_document(
    request: Request,
    patientId: str = Form(...),
    file: UploadFile = File(...),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    store_dict = _patient_store(request)
    # Draft and real patients are both stored as full entries in the patient
    # store (draft has is_draft=True). The same access check applies.
    _assert_patient_access(patientId, staff, store_dict)

    contents = await file.read()
    size = len(contents)
    if size == 0:
        raise HTTPException(status_code=400, detail="File is empty")

    fmt = format_detect.detect_format(file.filename or "", contents[:4096])
    max_size = format_detect.max_size_for(fmt)
    if size > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"{fmt} exceeds {max_size // (1024 * 1024)}MB limit",
        )

    # PRD §11.2: detect password-protected PDFs at upload time so the user gets
    # an immediate, clear error rather than failing later in the pipeline.
    if fmt == "PDF":
        try:
            from eligibility.parse_pdf import parse_pdf as _parse_pdf
            _parse_pdf(contents)
        except PDFEncryptedError:
            raise HTTPException(status_code=422, detail="PDF is password-protected — please upload an unlocked copy.")
        except Exception:
            # Non-fatal at upload time; the pipeline will surface other parse errors.
            pass

    ext = os.path.splitext(file.filename or "")[1].lower() or ".bin"
    doc_id = uuid.uuid4().hex
    patient_dir = UPLOAD_DIR / patientId.replace("/", "_")
    patient_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = patient_dir / f"{doc_id}{ext}"
    dest.write_bytes(contents)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass

    sha256 = hashlib.sha256(contents).hexdigest()
    record = {
        "id": doc_id,
        "patient_id": patientId,
        "filename": file.filename or "unknown",
        "format": fmt,
        "size_bytes": size,
        "sha256": sha256,
        "path": str(dest),
        "status": "validated",
        "uploaded_at": _utc_iso(),
    }
    store.save_doc(doc_id, record)

    if patientId in store_dict:
        store_dict[patientId].setdefault("relevant_files", []).append(doc_id)

    store.append_audit(
        action="document_uploaded",
        actor=_actor_id(staff),
        patient_id=patientId,
        meta={"doc_id": doc_id, "format": fmt, "size_bytes": size},
    )

    return {
        "id": doc_id,
        "filename": record["filename"],
        "format": fmt,
        "sizeBytes": size,
        "sha256": sha256,
        "status": "validated",
    }


@router.get("/api/patient/{patient_id}/eligibility-documents")
async def list_patient_eligibility_documents(
    patient_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Return summary metadata for every doc currently attached to ``patient_id``.

    Useful for the doctor portal when reopening a draft, or for showing
    "previously uploaded files" alongside an in-progress eligibility check.
    """
    store_dict = _patient_store(request)
    _assert_patient_access(patient_id, staff, store_dict)
    patient = store_dict[patient_id]
    out: List[Dict[str, Any]] = []
    for did in patient.get("relevant_files") or []:
        d = store.get_doc(did)
        if not d:
            continue
        out.append({
            "id": d["id"],
            "filename": d.get("filename"),
            "format": d.get("format"),
            "sizeBytes": d.get("size_bytes"),
            "sha256": d.get("sha256"),
            "status": d.get("status"),
            "uploadedAt": d.get("uploaded_at"),
        })
    return {"documents": out}


@router.delete("/api/eligibility-documents/{doc_id}")
async def delete_eligibility_document(
    doc_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    rec = store.get_doc(doc_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Document not found")
    store_dict = _patient_store(request)
    pid = rec.get("patient_id", "")
    # Allow deleting a doc whose patient was already removed (orphan cleanup),
    # but still enforce tenant access if the patient is around.
    if pid and pid in store_dict:
        _assert_patient_access(pid, staff, store_dict)

    try:
        p = Path(rec["path"])
        if p.exists():
            p.unlink()
    except Exception as e:
        log.warning("Failed to unlink %s: %s", rec.get("path"), e)

    store.delete_doc(doc_id)
    if pid in store_dict:
        files = store_dict[pid].get("relevant_files") or []
        if doc_id in files:
            files.remove(doc_id)

    store.append_audit(
        action="document_deleted",
        actor=_actor_id(staff),
        patient_id=pid,
        meta={"doc_id": doc_id},
    )
    return {"ok": True}


# ─── Eligibility checks ─────────────────────────────────────────────────────
FREEFORM_NOTES_MAX_BYTES = 50_000  # ~50KB, well under any LLM context cap


class CreateCheckRequest(BaseModel):
    patientId: str
    documentIds: List[str] = Field(default_factory=list)
    freeformNotes: Optional[str] = None
    surgeryDate: Optional[str] = None  # ISO YYYY-MM-DD; can also come from patient record


def _validate_iso_date(s: str, field_name: str) -> None:
    """Reject obviously-malformed ISO dates before they hit the evaluator."""
    from datetime import date as _date

    try:
        _date.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a valid ISO date (YYYY-MM-DD)")


@router.post("/api/eligibility-checks")
async def create_eligibility_check(
    request: Request,
    body: CreateCheckRequest,
    background_tasks: BackgroundTasks,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    store_dict = _patient_store(request)
    _assert_patient_access(body.patientId, staff, store_dict)

    actor = _actor_id(staff)
    if not store.rate_limit_check(actor):
        raise HTTPException(status_code=429, detail="Rate limit exceeded (30 checks/hour)")

    patient = store_dict[body.patientId]
    sd = patient.get("structured_data") or {}
    surgery_date = body.surgeryDate or sd.get("procedure_date") or ""
    if not surgery_date:
        raise HTTPException(status_code=400, detail="Surgery date is required before running the eligibility check")
    _validate_iso_date(surgery_date, "surgeryDate")

    freeform = body.freeformNotes or ""
    if len(freeform.encode("utf-8")) > FREEFORM_NOTES_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Freeform notes exceed {FREEFORM_NOTES_MAX_BYTES // 1000}KB limit",
        )

    docs: List[Dict[str, Any]] = []
    for did in body.documentIds:
        d = store.get_doc(did)
        if not d:
            raise HTTPException(status_code=404, detail=f"Document {did} not found")
        # Defense-in-depth: ensure the doc actually belongs to this patient.
        # Without this check, a doctor could attach another patient's docs to
        # their own check by guessing IDs.
        if d.get("patient_id") and d.get("patient_id") != body.patientId:
            raise HTTPException(
                status_code=403,
                detail=f"Document {did} does not belong to patient {body.patientId}",
            )
        docs.append(d)

    if not docs and not freeform.strip():
        raise HTTPException(status_code=400, detail="Provide at least one document or freeform notes")

    # If the patient already has an in-flight check, refuse to start another —
    # the two pipelines would race and the patient record's eligibility_status
    # would flip-flop based on whichever finished last.
    prior_id = patient.get("eligibility_check_id")
    if prior_id:
        prior = store.get_check(prior_id)
        if prior and prior.get("status") in ("PARSING", "EXTRACTING", "EVALUATING"):
            raise HTTPException(
                status_code=409,
                detail="An eligibility check for this patient is already running — wait for it to finish.",
            )

    check_id = uuid.uuid4().hex
    queue = store.new_check_queue()
    record: Dict[str, Any] = {
        "id": check_id,
        "patient_id": body.patientId,
        "document_ids": [d["id"] for d in docs],
        "freeform_notes": body.freeformNotes or "",
        "surgery_date": surgery_date,
        "status": "PARSING",
        "stage": "PARSING",
        "created_at": _utc_iso(),
        "updated_at": _utc_iso(),
        "actor": actor,
        "verdicts": None,
        "overall_verdict": None,
        "extracted_fields": None,
        "overrides": {},  # field -> { to, reason, actor, ts }
        "error": None,
        "queue": queue,
        "ring": store.ring_buffer(),
    }
    store.save_check(check_id, record)

    patient["eligibility_check_id"] = check_id
    if patient.get("eligibility_status") in (None, "DRAFT"):
        patient["eligibility_status"] = "PENDING"

    store.append_audit(
        action="eligibility_check_started",
        actor=actor,
        patient_id=body.patientId,
        check_id=check_id,
        meta={"documents": [d["id"] for d in docs], "surgery_date": surgery_date},
    )

    asyncio.create_task(pipeline.run_pipeline(check_id, patient, docs, body.freeformNotes or "", surgery_date))

    return JSONResponse(status_code=202, content={"id": check_id, "status": "PARSING"})


def _serialize_check(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in rec.items() if k not in ("queue", "ring")}
    return out


@router.get("/api/eligibility-checks/{check_id}")
async def get_check(
    check_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    rec = store.get_check(check_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Check not found")
    _assert_patient_access(rec["patient_id"], staff, _patient_store(request))
    return _serialize_check(rec)


@router.get("/api/eligibility-checks/{check_id}/stream")
async def stream_check(check_id: str, request: Request):
    staff = await _resolve_staff_with_query_fallback(request)
    rec = store.get_check(check_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Check not found")
    _assert_patient_access(rec["patient_id"], staff, _patient_store(request))

    queue: asyncio.Queue = rec["queue"]
    ring = rec["ring"]

    async def generator():
        try:
            # Replay whatever we've already emitted (reconnect-friendly, PRD §11.13)
            for event in list(ring):
                yield _format_sse(event["event"], event["data"])
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    if rec.get("status") in ("DONE", "ERROR"):
                        return
                    continue
                yield _format_sse(event["event"], event["data"])
                if event["event"] in ("result", "error"):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.exception("SSE check stream %s failed", check_id)
            yield _format_sse("error", {"message": f"stream failed: {e}"})

    return StreamingResponse(generator(), media_type="text/event-stream")


def _format_sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


class OverrideRequest(BaseModel):
    field: str  # one of partA_active / partB_active / not_ma / medicare_primary / not_esrd_basis / not_umwa
    to: str = "PASS"
    reason: str


@router.post("/api/eligibility-checks/{check_id}/override")
async def override_field(
    check_id: str,
    body: OverrideRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    rec = store.get_check(check_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Check not found")
    _assert_patient_access(rec["patient_id"], staff, _patient_store(request))

    # An in-flight pipeline will overwrite verdicts on completion — refuse to
    # accept an override until the pipeline has settled.
    if rec.get("status") in ("PARSING", "EXTRACTING", "EVALUATING"):
        raise HTTPException(
            status_code=409,
            detail="Eligibility check is still running — overrides will apply once it completes.",
        )
    if rec.get("status") == "FINALIZED":
        raise HTTPException(
            status_code=409,
            detail="Check has been finalized — overrides are no longer accepted.",
        )

    allowed = {"partA_active", "partB_active", "not_ma", "medicare_primary", "not_esrd_basis", "not_umwa"}
    if body.field not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown field '{body.field}'")
    if body.to not in ("PASS", "FAIL"):
        raise HTTPException(status_code=400, detail="to must be PASS or FAIL")
    if not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="reason is required")

    before = dict(rec.get("verdicts") or {})
    overrides = rec.setdefault("overrides", {})
    overrides[body.field] = {
        "to": body.to,
        "reason": body.reason.strip(),
        "actor": _actor_id(staff),
        "ts": _utc_iso(),
    }
    verdicts = dict(before)
    verdicts[body.field] = body.to
    rec["verdicts"] = verdicts
    rec["overall_verdict"] = eval_mod.overall_verdict(verdicts)
    rec["updated_at"] = _utc_iso()

    store.append_audit(
        action="eligibility_override",
        actor=_actor_id(staff),
        patient_id=rec["patient_id"],
        check_id=check_id,
        before=before,
        after=rec["verdicts"],
        meta={"field": body.field, "to": body.to, "reason": body.reason},
    )

    return _serialize_check(rec)


@router.post("/api/eligibility-checks/{check_id}/rerun")
async def rerun_check(
    check_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    rec = store.get_check(check_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Check not found")
    store_dict = _patient_store(request)
    _assert_patient_access(rec["patient_id"], staff, store_dict)

    # Two pipelines emitting to the same record's queue would race —
    # refuse a re-run until the in-flight pipeline lands.
    if rec.get("status") in ("PARSING", "EXTRACTING", "EVALUATING"):
        raise HTTPException(
            status_code=409,
            detail="A pipeline run is already in progress for this check.",
        )
    if rec.get("status") == "FINALIZED":
        raise HTTPException(
            status_code=409,
            detail="This check has been finalized — start a new check from the patient detail view.",
        )

    actor = _actor_id(staff)
    if not store.rate_limit_check(actor):
        raise HTTPException(status_code=429, detail="Rate limit exceeded (30 checks/hour)")

    preserved_overrides = rec.get("overrides") or {}
    queue = store.new_check_queue()
    rec.update(
        {
            "status": "PARSING",
            "stage": "PARSING",
            "error": None,
            "queue": queue,
            "ring": store.ring_buffer(),
            "verdicts": None,
            "extracted_fields": None,
            "updated_at": _utc_iso(),
            "overrides": preserved_overrides,
        }
    )

    patient = store_dict[rec["patient_id"]]
    docs = [store.get_doc(did) for did in rec["document_ids"] if store.get_doc(did)]

    store.append_audit(
        action="eligibility_check_rerun",
        actor=actor,
        patient_id=rec["patient_id"],
        check_id=check_id,
    )

    asyncio.create_task(
        pipeline.run_pipeline(check_id, patient, docs, rec.get("freeform_notes") or "", rec["surgery_date"])
    )
    return {"id": check_id, "status": "PARSING"}


class FinalizeRequest(BaseModel):
    decision: str  # SAVE_AS_TEAM | SAVE_AS_STANDARD


@router.post("/api/eligibility-checks/{check_id}/finalize")
async def finalize_check(
    check_id: str,
    body: FinalizeRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    rec = store.get_check(check_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Check not found")
    store_dict = _patient_store(request)
    _assert_patient_access(rec["patient_id"], staff, store_dict)

    if body.decision not in ("SAVE_AS_TEAM", "SAVE_AS_STANDARD"):
        raise HTTPException(status_code=400, detail="decision must be SAVE_AS_TEAM or SAVE_AS_STANDARD")

    # Refuse to finalize while the pipeline is still running — the verdicts may
    # land moments later and would silently disagree with the saved decision.
    if rec.get("status") in ("PARSING", "EXTRACTING", "EVALUATING"):
        raise HTTPException(
            status_code=409,
            detail="Eligibility check is still running — please wait for it to finish or cancel.",
        )
    if rec.get("status") == "FINALIZED":
        raise HTTPException(
            status_code=409,
            detail="Check has already been finalized.",
        )

    patient = store_dict[rec["patient_id"]]
    overall = rec.get("overall_verdict") or "BLOCKED_UNKNOWN"

    if body.decision == "SAVE_AS_TEAM":
        if overall != "ELIGIBLE":
            raise HTTPException(
                status_code=409,
                detail=f"Cannot save as TEAM episode — overall verdict is {overall}. Resolve UNKNOWNs via override or re-run.",
            )
        patient["eligibility_status"] = "ELIGIBLE"
    else:
        # Standard episode: the user explicitly chose NOT to enroll in TEAM, so
        # clear the eligibility badge entirely. The patient still appears in
        # the roster with a normal pre-op pill.
        patient["eligibility_status"] = None

    # Promote draft to real patient
    patient.pop("is_draft", None)

    rec["status"] = "FINALIZED"
    rec["finalized_as"] = body.decision
    rec["updated_at"] = _utc_iso()

    store.append_audit(
        action="eligibility_finalized",
        actor=_actor_id(staff),
        patient_id=rec["patient_id"],
        check_id=check_id,
        after={"decision": body.decision, "overall": overall},
    )

    return {
        "id": check_id,
        "patient_id": rec["patient_id"],
        "decision": body.decision,
        "eligibility_status": patient["eligibility_status"],
    }


# ─── Batches (group upload) ─────────────────────────────────────────────────
@router.post("/api/eligibility-batches")
async def create_eligibility_batch(
    request: Request,
    files: List[UploadFile] = File(...),
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if len(files) == 0:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > 50:
        raise HTTPException(status_code=413, detail="Group upload capped at 50 files per batch")

    actor = _actor_id(staff)
    if not store.rate_limit_check(actor):
        raise HTTPException(status_code=429, detail="Rate limit exceeded (30 batches/hour)")

    # Size guard: load everything into memory (prototype constraint per PRD §13);
    # cap at 200 MB total per PRD §5.2.
    payloads: List[tuple[str, bytes]] = []
    total = 0
    for f in files:
        content = await f.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"File '{f.filename or 'unknown'}' is empty")
        total += len(content)
        if total > 200 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Batch exceeds 200 MB total")
        payloads.append((f.filename or "unknown", content))

    hs_id = staff.tenant_id if (staff and staff.source == "tenant") else None

    batch_id = uuid.uuid4().hex
    queue = store.new_check_queue()
    record: Dict[str, Any] = {
        "id": batch_id,
        "created_at": _utc_iso(),
        "updated_at": _utc_iso(),
        "actor": actor,
        "status": "PROCESSING",
        "created": [],
        "needs_review": [],
        "errors": [],
        "queue": queue,
        "ring": store.ring_buffer(),
    }
    store.save_batch(batch_id, record)

    store.append_audit(action="eligibility_batch_started", actor=actor, meta={"batch_id": batch_id, "files": len(files)})

    asyncio.create_task(pipeline.run_batch(batch_id, payloads, hs_id, actor, request.app))

    return JSONResponse(status_code=202, content={"id": batch_id, "status": "PROCESSING"})


def _serialize_batch(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if k not in ("queue", "ring")}


@router.get("/api/eligibility-batches/{batch_id}")
async def get_batch(batch_id: str):
    rec = store.get_batch(batch_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _serialize_batch(rec)


@router.get("/api/eligibility-batches/{batch_id}/stream")
async def stream_batch(batch_id: str, request: Request):
    _ = await _resolve_staff_with_query_fallback(request)  # authenticate; no tenant filter on batch
    rec = store.get_batch(batch_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue: asyncio.Queue = rec["queue"]
    ring = rec["ring"]

    async def generator():
        try:
            for event in list(ring):
                yield _format_sse(event["event"], event["data"])
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    if rec.get("status") in ("DONE", "ERROR"):
                        return
                    continue
                yield _format_sse(event["event"], event["data"])
                if event["event"] == "done":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.exception("SSE batch stream %s failed", batch_id)
            yield _format_sse("error", {"message": f"stream failed: {e}"})

    return StreamingResponse(generator(), media_type="text/event-stream")


# ─── Track B: notes endpoints ───────────────────────────────────────────────
class NotesConfirmRequest(BaseModel):
    text: str


def _extract_ai_notes(patient: Dict[str, Any], kind: str) -> str:
    """Best-effort extraction of AI-parsed notes already present on the patient record."""
    sd = patient.get("structured_data") or {}
    if kind == "postop":
        return (
            patient.get("postop_notes_confirmed_text")
            or sd.get("post_op_instructions")
            or sd.get("discharge_notes")
            or ""
        )
    return (
        patient.get("preop_notes_confirmed_text")
        or sd.get("pre_op_instructions")
        or sd.get("prep_notes")
        or ""
    )


@router.get("/api/patient/{patient_id}/postop-notes")
async def get_postop_notes(
    patient_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    store_dict = _patient_store(request)
    _assert_patient_access(patient_id, staff, store_dict)
    patient = store_dict[patient_id]
    confirmed = patient.get("postop_notes_confirmed_text")
    text = confirmed if confirmed is not None else _extract_ai_notes(patient, "postop")
    return {
        "text": text or "",
        "source": "confirmed" if confirmed else "ai",
        "updatedAt": patient.get("postop_notes_confirmed_at"),
    }


@router.post("/api/patient/{patient_id}/postop-notes/confirm")
async def confirm_postop_notes(
    patient_id: str,
    body: NotesConfirmRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    store_dict = _patient_store(request)
    _assert_patient_access(patient_id, staff, store_dict)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Notes cannot be empty before generating discharge materials")
    patient = store_dict[patient_id]
    patient["postop_notes_confirmed_text"] = text
    patient["postop_notes_confirmed_at"] = _utc_iso()

    store.append_audit(
        action="postop_notes_confirmed",
        actor=_actor_id(staff),
        patient_id=patient_id,
        meta={"len": len(text)},
    )

    try:
        await pipeline.regenerate_materials(patient, pipeline_type="post_op", notes_text=text)
    except Exception as e:
        log.exception("Discharge material generation failed for %s", patient_id)
        raise HTTPException(status_code=500, detail=f"Discharge material generation failed: {e}")

    store.append_audit(
        action="discharge_materials_generated",
        actor=_actor_id(staff),
        patient_id=patient_id,
    )

    return {"status": "ok", "materialsReady": True}


@router.get("/api/patient/{patient_id}/preop-notes")
async def get_preop_notes(
    patient_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    store_dict = _patient_store(request)
    _assert_patient_access(patient_id, staff, store_dict)
    patient = store_dict[patient_id]
    confirmed = patient.get("preop_notes_confirmed_text")
    text = confirmed if confirmed is not None else _extract_ai_notes(patient, "preop")
    return {
        "text": text or "",
        "source": "confirmed" if confirmed else "ai",
        "updatedAt": patient.get("preop_notes_confirmed_at"),
    }


@router.post("/api/patient/{patient_id}/preop-notes/confirm")
async def confirm_preop_notes(
    patient_id: str,
    body: NotesConfirmRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    store_dict = _patient_store(request)
    _assert_patient_access(patient_id, staff, store_dict)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Notes cannot be empty before generating preparation materials")
    patient = store_dict[patient_id]
    patient["preop_notes_confirmed_text"] = text
    patient["preop_notes_confirmed_at"] = _utc_iso()

    store.append_audit(
        action="preop_notes_confirmed",
        actor=_actor_id(staff),
        patient_id=patient_id,
        meta={"len": len(text)},
    )

    try:
        await pipeline.regenerate_materials(patient, pipeline_type="pre_op", notes_text=text)
    except Exception as e:
        log.exception("Prep material generation failed for %s", patient_id)
        raise HTTPException(status_code=500, detail=f"Prep material generation failed: {e}")

    store.append_audit(
        action="prep_materials_generated",
        actor=_actor_id(staff),
        patient_id=patient_id,
    )

    return {"status": "ok", "materialsReady": True}


# ─── Admin audit viewer ─────────────────────────────────────────────────────
@router.get("/admin/audit/eligibility")
async def list_audit_events(
    request: Request,
    limit: int = 500,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    """Return recent audit events. Requires an authenticated staff context.

    For tenant-scoped staff, results are filtered to events belonging to
    patients in their own tenant. Landing/demo doctors see all events that
    don't carry a tenant scope.
    """
    if not staff:
        raise HTTPException(status_code=401, detail="Authentication required")

    capped = min(max(limit, 1), 2000)
    events = store.list_audit(limit=capped)

    if staff.source == "tenant" and staff.tenant_id:
        store_dict = _patient_store(request)
        tenant_id = staff.tenant_id
        filtered: List[Dict[str, Any]] = []
        for e in events:
            pid = e.get("patient_id")
            if not pid:
                # Tenant-agnostic events (batch starts, anonymous actions) — show
                continue
            d = store_dict.get(pid)
            if d and (d.get("health_system_id") or "") == tenant_id:
                filtered.append(e)
        events = filtered
    return {"events": events}
