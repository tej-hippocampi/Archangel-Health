"""Gold Standard — clinical conversation gold-data capture (PRD GoldCapture v0.1).

Doctor-portal-only feature surfaced as the "Gold Standard" sub-tab under
Population Analytics. Records the surgeon–patient conversation, drafts a clinical
note via the existing Anthropic LLM client, lets the surgeon correct it + tag
errors, de-identifies (automated + human QA), and exports schema-valid JSONL.

Routes (prefix ``/api/gold``):
  GET    /taxonomy                         error-label taxonomy (config-driven)
  POST   /visits                           allocate a gold visit (capture)
  POST   /visits/{id}/consent              record consent (or decline → discard)
  POST   /visits/{id}/audio                upload recording → kick draft pipeline
  GET    /visits                           list / queues (tenant-scoped)
  GET    /visits/{id}                      full record (decrypted)
  GET    /visits/{id}/stream               SSE draft progress
  POST   /visits/{id}/submit               surgeon gold-label submit → de-id
  POST   /visits/{id}/approve              operator QA approve → export-ready
  GET    /stats                            dashboard counters
  POST   /export                           JSONL + data dictionary (operator)

Role gates (PRD §4): capture = {surgeon, rn_coordinator}; review/submit =
{surgeon}; QA/approve/export = operator (system_admin, or the tenant team
director acting as operator in the single-tenant pilot — see ``_require_operator``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import field_crypto
from audit import audit_log
from auth_roles import require_roles
from compliance import subprocessors
from gold import config as gold_config
from gold import export as gold_export
from gold import pipeline as gold_pipeline
from gold import schema as gold_schema
from gold import store
from gold.deid import deidentify
from staff_context import StaffContext, get_staff_context_optional

log = logging.getLogger("gold.router")

router = APIRouter(prefix="/api/gold", tags=["gold"])

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/elysium-eligibility")).resolve()
GOLD_DIR = UPLOAD_DIR / "gold"
GOLD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

CAPTURE_ROLES = {"surgeon", "rn_coordinator"}
REVIEW_ROLES = {"surgeon"}
MAX_AUDIO_BYTES = 200 * 1024 * 1024  # 200 MB

# A5: the event loop only keeps weak refs to tasks — hold strong refs to our
# fire-and-forget pipeline/de-id tasks so they can't be GC'd mid-run.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    # Tests patch create_task to a no-op (returns None) and drive the coroutine
    # manually — only register real Task objects.
    if task is not None and hasattr(task, "add_done_callback"):
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _utc_iso() -> str:
    from datetime import datetime

    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _actor_id(staff: Optional[StaffContext]) -> str:
    if not staff:
        return "anonymous"
    return f"{staff.source}:{staff.email or ''}"


def _require_staff(staff: Optional[StaffContext]) -> StaffContext:
    if staff is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return staff


def _is_operator(staff: Optional[StaffContext]) -> bool:
    if not staff:
        return False
    role = (staff.role or "").strip().lower()
    # Operator = internal system_admin, or (single-tenant pilot) the tenant team
    # director acting as the QA/export operator on the doctor portal.
    return role == "system_admin" or bool(getattr(staff, "is_team_director", False))


def _require_operator(staff: Optional[StaffContext]) -> StaffContext:
    staff = _require_staff(staff)
    if not _is_operator(staff):
        raise HTTPException(status_code=403, detail="Operator role required (system_admin / team director).")
    return staff


def _tenant_scope(staff: StaffContext) -> tuple[Optional[str], str]:
    """Return (tenant_id, tenant_slug) for storing/scoping a visit."""
    if staff.source == "tenant":
        return staff.tenant_id, (staff.tenant_slug or "")
    # Landing/demo users have no tenant_id — scope them to the NULL tenant.
    return None, (staff.health_system_code or "")


def _assert_visit_access(visit: Optional[Dict[str, Any]], staff: StaffContext) -> Dict[str, Any]:
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found")
    tenant_id, _slug = _tenant_scope(staff)
    if (visit.get("tenant_id") or None) != (tenant_id or None):
        raise HTTPException(status_code=404, detail="Visit not found")
    return visit


def _audit(
    staff: Optional[StaffContext],
    request: Optional[Request],
    action: str,
    *,
    outcome: str = "success",
    resource: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    audit_log.record(
        actor_type=(staff.source if staff else "anonymous"),
        actor_id=_actor_id(staff),
        action=action,
        outcome=outcome,
        resource_type="gold_visit",
        resource=resource,
        source_ip=(request.client.host if request and request.client else None),
        user_agent=(request.headers.get("user-agent") if request else None),
        detail=detail,
    )


def _baa_on_file() -> bool:
    """Tenant BAA gate (PRD §10). Default on for the single-tenant pilot where the
    BAA is handled outside the app; flip ``GOLD_BAA_ON_FILE=0`` to block export."""
    return (os.getenv("GOLD_BAA_ON_FILE") or "1").strip().lower() not in ("0", "false", "no", "off")


async def _resolve_staff_with_query_fallback(request: Request) -> Optional[StaffContext]:
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return await get_staff_context_optional(authorization=auth_header)
    tok = request.query_params.get("token")
    if tok:
        return await get_staff_context_optional(authorization=f"Bearer {tok}")
    return None


def _public_visit(visit: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a visit for the surgeon/operator review UI (decrypted clinical text,
    no raw audio path)."""
    return {
        "id": visit.get("id"),
        "record_id": gold_schema.record_id_for(visit.get("tenant_slug") or "", visit.get("record_num") or 0),
        "status": visit.get("status"),
        "specialty": visit.get("specialty"),
        "encounter_type": visit.get("encounter_type"),
        "consent_given": visit.get("consent_given"),
        "consent_method": visit.get("consent_method"),
        "consent_timestamp": visit.get("consent_timestamp"),
        "baa_on_file": visit.get("baa_on_file"),
        "audio_duration_sec": visit.get("audio_duration_sec"),
        "difficulty_tags": visit.get("difficulty_tags") or [],
        "languages": visit.get("languages") or [],
        "stt_provider": visit.get("stt_provider"),
        "transcript": visit.get("transcript") or "",
        "transcript_turns": _safe_json(visit.get("transcript_turns")),
        "transcript_deid": visit.get("transcript_deid") or "",
        "ai_draft_note": visit.get("ai_draft_note") or "",
        "suggested_codes": visit.get("suggested_codes") or [],
        "gold_note": visit.get("gold_note") or "",
        "gold_note_deid": visit.get("gold_note_deid") or "",
        "error_labels": visit.get("error_labels") or [],
        "billing_codes": visit.get("billing_codes") or [],
        "prior_auth": visit.get("prior_auth"),
        "tasks": visit.get("tasks") or [],
        "clinician_review_seconds": visit.get("clinician_review_seconds"),
        "deid_method": visit.get("deid_method"),
        "deid_meta": visit.get("deid_meta"),
        "verified_by_operator": visit.get("verified_by_operator"),
        "pipeline_error": visit.get("pipeline_error"),
        "created_at": visit.get("created_at"),
        "updated_at": visit.get("updated_at"),
    }


def _safe_json(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return None
    return val


def _list_item(visit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": visit.get("id"),
        "record_id": gold_schema.record_id_for(visit.get("tenant_slug") or "", visit.get("record_num") or 0),
        "status": visit.get("status"),
        "specialty": visit.get("specialty"),
        "encounter_type": visit.get("encounter_type"),
        "audio_duration_sec": visit.get("audio_duration_sec"),
        "difficulty_tags": visit.get("difficulty_tags") or [],
        "error_label_count": len(visit.get("error_labels") or []),
        "created_at": visit.get("created_at"),
        "updated_at": visit.get("updated_at"),
    }


# ─── Taxonomy ─────────────────────────────────────────────────────────────────
@router.get("/taxonomy")
async def get_taxonomy(staff: Optional[StaffContext] = Depends(get_staff_context_optional)):
    _require_staff(staff)
    return gold_config.load_taxonomy()


# ─── Create / consent / audio ─────────────────────────────────────────────────
class CreateVisitRequest(BaseModel):
    specialty: Optional[str] = None
    encounter_type: Optional[str] = None


@router.post("/visits")
async def create_visit(
    request: Request,
    body: CreateVisitRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, CAPTURE_ROLES)
    tenant_id, tenant_slug = _tenant_scope(staff)
    visit_id = uuid.uuid4().hex
    store.create_visit(
        visit_id=visit_id,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        specialty=(body.specialty or gold_config.default_specialty()),
        encounter_type=(body.encounter_type or gold_config.default_encounter_type()),
        created_by=_actor_id(staff),
    )
    _audit(staff, request, "gold_visit_created", resource=visit_id)
    return {"id": visit_id, "status": store.ST_CAPTURING}


class ConsentRequest(BaseModel):
    consent_given: bool
    consent_method: str = "in_app_verbal"  # in_app_verbal | e_signature
    signature_image: Optional[str] = None  # data URL when consent_method == e_signature
    patient_name: Optional[str] = None  # PHI — stored encrypted, used only for name redaction, never exported


@router.post("/visits/{visit_id}/consent")
async def record_consent(
    visit_id: str,
    body: ConsentRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, CAPTURE_ROLES)
    visit = _assert_visit_access(store.get_visit(visit_id), staff)

    if body.consent_method not in ("in_app_verbal", "e_signature"):
        raise HTTPException(status_code=400, detail="consent_method must be in_app_verbal or e_signature")

    if not body.consent_given:
        # Declined → discard everything; keep only an anonymous declined counter.
        tenant_id, _slug = _tenant_scope(staff)
        store.record_declined(tenant_id)
        store.delete_visit(visit_id)
        _audit(staff, request, "gold_consent_declined", resource=visit_id)
        return {"id": visit_id, "status": store.ST_CONSENT_DECLINED}

    store.update_visit(
        visit_id,
        consent_given=1,
        consent_method=body.consent_method,
        consent_timestamp=_utc_iso(),
        baa_on_file=1 if _baa_on_file() else 0,
        signature_image=body.signature_image,
        patient_name=(body.patient_name or None),
    )
    _audit(staff, request, "gold_consent_given", resource=visit_id, detail={"method": body.consent_method})
    return {"id": visit_id, "status": store.ST_CAPTURING, "consent_given": True}


@router.post("/visits/{visit_id}/audio")
async def upload_audio(
    visit_id: str,
    request: Request,
    file: UploadFile = File(...),
    difficulty_tags: Optional[str] = Form(None),  # JSON array string
    languages: Optional[str] = Form(None),        # JSON array string
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, CAPTURE_ROLES)
    visit = _assert_visit_access(store.get_visit(visit_id), staff)

    if not visit.get("consent_given"):
        raise HTTPException(status_code=409, detail="Consent must be recorded before uploading audio.")

    # A8: a second upload would spawn a second pipeline and overwrite the
    # transcript — only accept audio while the visit is still capturing.
    if visit.get("status") != store.ST_CAPTURING:
        raise HTTPException(
            status_code=409,
            detail=f"Audio already received for this visit (status {visit.get('status')}).",
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Audio file is empty")
    if len(contents) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio exceeds 200 MB limit")

    mime = file.content_type or "audio/webm"
    ext = ".webm"
    if "ogg" in mime:
        ext = ".ogg"
    elif "mp4" in mime or "m4a" in mime:
        ext = ".m4a"
    elif "wav" in mime:
        ext = ".wav"
    elif "mpeg" in mime or "mp3" in mime:
        ext = ".mp3"

    slug = (visit.get("tenant_slug") or "default").replace("/", "_") or "default"
    visit_dir = GOLD_DIR / slug / visit_id
    visit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = visit_dir / f"audio{ext}"

    blob = field_crypto.encrypt_bytes(contents)  # passthrough when no key configured
    await asyncio.to_thread(dest.write_bytes, blob)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass

    store.update_visit(
        visit_id,
        audio_path=str(dest),
        audio_mime=mime,
        difficulty_tags=_parse_json_list(difficulty_tags),
        languages=_parse_json_list(languages),
        status=store.ST_DRAFTING,
    )
    _audit(staff, request, "gold_audio_uploaded", resource=visit_id, detail={"bytes": len(contents), "mime": mime})

    # Kick the async draft pipeline (STT → draft note) and stream progress.
    store.new_queue(visit_id)
    _spawn(_run_pipeline(visit_id, str(dest), mime))

    return JSONResponse(status_code=202, content={"id": visit_id, "status": store.ST_DRAFTING})


async def _run_pipeline(visit_id: str, enc_path: str, mime: str) -> None:
    """Decrypt audio to a temp file (if encrypted) and run the draft pipeline."""
    tmp_path = enc_path
    cleanup = False
    try:
        raw = await asyncio.to_thread(Path(enc_path).read_bytes)
        if field_crypto.is_encrypted_bytes(raw):
            import tempfile

            plain = field_crypto.decrypt_bytes(raw)
            fd, tmp_path = tempfile.mkstemp(suffix=Path(enc_path).suffix, prefix="gold_aud_")
            with os.fdopen(fd, "wb") as fh:
                fh.write(plain or b"")
            cleanup = True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("gold audio decrypt failed for %s: %s", visit_id, exc)
    try:
        await gold_pipeline.run_draft_pipeline(visit_id, tmp_path, mime)
    finally:
        if cleanup:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _parse_json_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except json.JSONDecodeError:
        return [s.strip() for s in raw.split(",") if s.strip()]


# ─── List / get / stream ──────────────────────────────────────────────────────
@router.get("/visits")
async def list_visits(
    request: Request,
    status: Optional[str] = None,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, CAPTURE_ROLES | {"system_admin"})
    tenant_id, _slug = _tenant_scope(staff)
    visits = store.list_visits(tenant_id=tenant_id, tenant_scoped=True, status=status)
    return {"visits": [_list_item(v) for v in visits]}


@router.get("/visits/{visit_id}")
async def get_visit(
    visit_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, CAPTURE_ROLES | {"system_admin"})
    visit = _assert_visit_access(store.get_visit(visit_id), staff)
    _audit(staff, request, "gold_visit_viewed", resource=visit_id)
    return _public_visit(visit)


def _format_sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/visits/{visit_id}/stream")
async def stream_visit(visit_id: str, request: Request):
    staff = await _resolve_staff_with_query_fallback(request)
    staff = _require_staff(staff)
    visit = _assert_visit_access(store.get_visit(visit_id), staff)

    queue = store.get_queue(visit_id)
    ring = store.get_ring(visit_id)
    _TERMINAL = (store.ST_NEEDS_REVIEW, store.ST_ERROR)

    async def generator():
        try:
            for event in list(ring):
                yield _format_sse(event["event"], event["data"])

            # A6: if the pipeline already reached a terminal state, replay the
            # ring (above), emit the terminal event from persisted status, then
            # close immediately — no waiting on a (now-dropped) live queue.
            cur = store.get_visit(visit_id) or {}
            st = cur.get("status")
            if queue is None or st in _TERMINAL:
                if st == store.ST_ERROR:
                    yield _format_sse("error", {"message": cur.get("pipeline_error") or "pipeline failed"})
                else:
                    yield _format_sse("result", {"status": st})
                store.drop_streams(visit_id)
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    cur = store.get_visit(visit_id) or {}
                    if cur.get("status") in (store.ST_NEEDS_REVIEW, store.ST_ERROR):
                        return
                    continue
                yield _format_sse(event["event"], event["data"])
                if event["event"] in ("result", "error"):
                    store.drop_streams(visit_id)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.exception("SSE gold stream %s failed", visit_id)
            yield _format_sse("error", {"message": f"stream failed: {e}"})

    return StreamingResponse(generator(), media_type="text/event-stream")


# ─── Surgeon gold-label submit ────────────────────────────────────────────────
class BillingCode(BaseModel):
    system: str  # ICD-10 | CPT
    code: str
    verified_by: str = "clinician"


class ErrorLabel(BaseModel):
    type: str
    subtype: Optional[str] = None
    severity: str = "medium"
    section: Optional[str] = None
    original_text: Optional[str] = ""
    corrected_text: Optional[str] = ""
    clinician_verified: bool = True


class PriorAuth(BaseModel):
    drug_or_service: str
    justification_text: Optional[str] = ""
    outcome: str = "pending"  # approved | denied | pending


class SubmitRequest(BaseModel):
    gold_note: str
    error_labels: List[ErrorLabel] = Field(default_factory=list)
    billing_codes: List[BillingCode] = Field(default_factory=list)
    prior_auth: Optional[PriorAuth] = None
    clinician_review_seconds: Optional[int] = None
    difficulty_tags: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    tasks: Optional[List[str]] = None  # surgeon-confirmed workflow tasks (B2)


@router.post("/visits/{visit_id}/submit")
async def submit_visit(
    visit_id: str,
    body: SubmitRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, REVIEW_ROLES)
    visit = _assert_visit_access(store.get_visit(visit_id), staff)

    # A8: only a record awaiting review may be submitted (no re-submit churn).
    if visit.get("status") != store.ST_NEEDS_REVIEW:
        raise HTTPException(status_code=409, detail=f"Visit is not reviewable (status {visit.get('status')}).")

    gold_note = (body.gold_note or "").strip()
    if not gold_note:
        raise HTTPException(status_code=400, detail="gold_note cannot be empty")

    for code in body.billing_codes:
        if code.system not in ("ICD-10", "CPT"):
            raise HTTPException(status_code=400, detail="billing code system must be ICD-10 or CPT")

    valid_types = gold_config.taxonomy_types()
    for lbl in body.error_labels:
        if lbl.type not in valid_types:
            raise HTTPException(status_code=400, detail=f"unknown error label type: {lbl.type}")

    valid_tasks = set(gold_config.workflow_tasks())
    tasks = [t for t in (body.tasks or []) if t in valid_tasks]

    updates: Dict[str, Any] = {
        "gold_note": gold_note,
        "error_labels": [lbl.model_dump() for lbl in body.error_labels],
        "billing_codes": [c.model_dump() for c in body.billing_codes],
        "prior_auth": body.prior_auth.model_dump() if body.prior_auth else None,
        "tasks": tasks,
        "clinician_review_seconds": body.clinician_review_seconds,
        "clinician_id_hashed": gold_schema.hash_clinician(_actor_id(staff)),
        "submitted_by": _actor_id(staff),
        "submitted_by_role": (staff.role or "surgeon"),
        "status": store.ST_DEIDENTIFYING,
        "submitted_at": _utc_iso(),
    }
    if body.difficulty_tags is not None:
        updates["difficulty_tags"] = body.difficulty_tags
    if body.languages is not None:
        updates["languages"] = body.languages
    store.update_visit(visit_id, **updates)

    _audit(
        staff, request, "gold_visit_submitted", resource=visit_id,
        detail={"error_labels": len(body.error_labels), "review_seconds": body.clinician_review_seconds},
    )

    _spawn(_run_deid(visit_id))
    return {"id": visit_id, "status": store.ST_DEIDENTIFYING}


async def _run_deid(visit_id: str) -> None:
    visit = store.get_visit(visit_id)
    if not visit:
        return
    try:
        result = await deidentify(
            transcript=visit.get("transcript") or "",
            gold_note=visit.get("gold_note") or "",
            ai_draft_note=visit.get("ai_draft_note") or "",
            error_labels=visit.get("error_labels") or [],
            prior_auth=visit.get("prior_auth"),
            patient_name=visit.get("patient_name") or None,
            visit_id=visit_id,
        )
        store.update_visit(
            visit_id,
            transcript_deid=result["transcript_deid"],
            gold_note_deid=result["gold_note_deid"],
            ai_draft_note_deid=result["ai_draft_note_deid"],
            error_labels_deid=result["error_labels_deid"],
            prior_auth_deid=result["prior_auth_deid"],
            deid_method=result["method"],
            deid_method_detail=result["method_detail"],
            deid_meta={"placeholders": result["placeholders"], "method_detail": result["method_detail"]},
            status=store.ST_NEEDS_QA,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gold de-id failed for %s", visit_id)
        store.update_visit(visit_id, status=store.ST_ERROR, pipeline_error=f"de-id failed: {exc}")


# ─── Operator QA approve ──────────────────────────────────────────────────────
class ApproveRequest(BaseModel):
    transcript_deid: Optional[str] = None
    gold_note_deid: Optional[str] = None


@router.post("/visits/{visit_id}/approve")
async def approve_visit(
    visit_id: str,
    body: ApproveRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_operator(staff)
    visit = _assert_visit_access(store.get_visit(visit_id), staff)

    # A4: never re-approve an already export-ready record.
    if visit.get("status") != store.ST_NEEDS_QA:
        raise HTTPException(status_code=409, detail=f"Visit is not awaiting QA (status {visit.get('status')}).")

    # A4: independent human QA — the person who submitted the gold label may not
    # also approve their own de-id (the guarantee buyers pay for). The
    # director-as-operator fallback stays, but the second-person rule is enforced.
    approver = _actor_id(staff)
    submitter = visit.get("submitted_by")
    self_qa = bool(submitter) and submitter == approver
    if self_qa and not gold_config.allow_self_qa():
        raise HTTPException(
            status_code=409,
            detail="QA must be performed by a different person than the submitter "
                   "(set GOLD_ALLOW_SELF_QA=1 to override in the pilot).",
        )

    updates: Dict[str, Any] = {
        "verified_by_operator": 1,
        "approved_at": _utc_iso(),
        "approved_by": approver,
        "status": store.ST_EXPORT_READY,
    }
    if body.transcript_deid is not None:
        updates["transcript_deid"] = body.transcript_deid.strip()
    if body.gold_note_deid is not None:
        updates["gold_note_deid"] = body.gold_note_deid.strip()
    store.update_visit(visit_id, **updates)

    _audit(
        staff, request, "gold_visit_approved", resource=visit_id,
        detail={"self_qa_override": True} if self_qa else None,
    )
    return {"id": visit_id, "status": store.ST_EXPORT_READY}


# ─── Dashboard stats ──────────────────────────────────────────────────────────
@router.get("/stats")
async def get_stats(
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_staff(staff)
    require_roles(staff, CAPTURE_ROLES | {"system_admin"})
    tenant_id, _slug = _tenant_scope(staff)
    counts = store.status_counts(tenant_id, tenant_scoped=True)
    declined = store.declined_count(tenant_id, tenant_scoped=True)

    needs_review = counts.get(store.ST_NEEDS_REVIEW, 0)
    needs_deid = counts.get(store.ST_NEEDS_QA, 0) + counts.get(store.ST_DEIDENTIFYING, 0)
    export_ready = counts.get(store.ST_EXPORT_READY, 0)
    exported = counts.get(store.ST_EXPORTED, 0)
    submitted = needs_deid + export_ready + exported

    # Surgeon contribution: visits whose clinician hash matches this actor.
    my_hash = gold_schema.hash_clinician(_actor_id(staff))
    contributions = store.contributions_by_clinician(tenant_id, tenant_scoped=True)
    my_contrib = contributions.get(my_hash, 0)

    rate = float(os.getenv("GOLD_RATE_PER_RECORD_USD") or "25")
    return {
        "is_operator": _is_operator(staff),
        "queues": {
            "needs_review": needs_review,
            "needs_deid": needs_deid,
            "export_ready": export_ready,
            "exported": exported,
        },
        "totals": {
            "captured": sum(counts.values()),
            "submitted": submitted,
            "declined": declined,
        },
        "surgeon": {
            "visits_contributed": my_contrib,
            "amount_earned_usd": round(my_contrib * rate, 2),
            "rate_per_record_usd": rate,
        },
    }


# ─── Export ───────────────────────────────────────────────────────────────────
class ExportRequest(BaseModel):
    visit_ids: Optional[List[str]] = None
    specialty: Optional[str] = None
    since: Optional[str] = None  # YYYY-MM-DD
    difficulty_tag: Optional[str] = None
    task: Optional[str] = None  # filter by workflow task (B2)
    destination_label: str = "manual-download"
    pseudonymize: bool = True
    export_format: str = "record"  # record | sft_messages | both (B4)


@router.post("/export")
async def export_records(
    body: ExportRequest,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    staff = _require_operator(staff)
    tenant_id, _slug = _tenant_scope(staff)

    all_ready = store.list_visits(tenant_id=tenant_id, tenant_scoped=True, status=store.ST_EXPORT_READY)
    by_id = {v["id"]: v for v in all_ready}

    if body.visit_ids:
        selected = [by_id[i] for i in body.visit_ids if i in by_id]
    else:
        selected = list(all_ready)

    if body.export_format not in ("record", "sft_messages", "both"):
        raise HTTPException(status_code=400, detail="export_format must be record | sft_messages | both")

    # Optional filters
    if body.specialty:
        selected = [v for v in selected if (v.get("specialty") or "") == body.specialty]
    if body.since:
        selected = [v for v in selected if (v.get("created_at") or "")[:10] >= body.since]
    if body.difficulty_tag:
        selected = [v for v in selected if body.difficulty_tag in (v.get("difficulty_tags") or [])]
    if body.task:
        selected = [v for v in selected if body.task in gold_schema.derive_tasks(v)]

    if not selected:
        raise HTTPException(status_code=400, detail="No export-ready records match the selection.")

    if not _baa_on_file():
        raise HTTPException(status_code=409, detail="Export blocked: BAA not on file for this tenant (GOLD_BAA_ON_FILE).")

    jsonl, exported, rejected = gold_export.build_export(selected, pseudonymize=body.pseudonymize)
    if not exported:
        raise HTTPException(
            status_code=409,
            detail={"message": "All selected records failed schema validation.", "rejected": rejected},
        )

    # Mark only the validated visits as EXPORTED (match on their canonical record_id).
    rejected_record_ids = {r["record_id"] for r in rejected}
    exported_visit_ids: List[str] = []
    for v in selected:
        rid = gold_schema.record_id_for(v.get("tenant_slug") or "", v.get("record_num") or 0)
        if rid in rejected_record_ids:
            continue
        store.update_visit(
            v["id"], status=store.ST_EXPORTED, exported_at=_utc_iso(), export_destination=body.destination_label
        )
        exported_visit_ids.append(v["id"])

    _audit(
        staff, request, "gold_export", resource=None,
        detail={
            "count": len(exported),
            "rejected": len(rejected),
            "destination": body.destination_label,
            "pseudonymized": body.pseudonymize,
            "export_format": body.export_format,
            "visit_ids": exported_visit_ids,
        },
    )

    resp: Dict[str, Any] = {
        "count": len(exported),
        "rejected": rejected,
        "destination_label": body.destination_label,
        "export_format": body.export_format,
        "schema_version": gold_schema.SCHEMA_VERSION,
        "data_dictionary": gold_export.data_dictionary_md(),
        "dataset_card": gold_export.dataset_card_md(exported),
        "croissant": gold_export.croissant_json(exported),
        "records": exported,
    }
    if body.export_format in ("record", "both"):
        resp["jsonl"] = jsonl
    if body.export_format in ("sft_messages", "both"):
        resp["sft_jsonl"] = gold_export.sft_messages_jsonl(exported)
    return resp
