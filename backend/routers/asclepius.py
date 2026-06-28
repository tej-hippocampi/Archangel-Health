"""Asclepius — Expert Evaluation Portal API (PRD §7, §9.2).

Standalone-auth router mounted at ``/api/asclepius`` in ``main.py``. Business
logic lives in the ``backend/asclepius/`` package; this file is the HTTP surface.

Auth is the Asclepius-local JWT (NOT the clinical/tenant auth). Role gates:
  evaluator   -> queue + submit
  qa_reviewer -> QA queue + decisions (also admin)
  admin       -> everything (users, tasks, candidate-gen, export, dashboard)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from asclepius import agreement as asc_agreement
from asclepius import auth as asc_auth
from asclepius import corpus as asc_corpus
from asclepius import export as asc_export
from asclepius import generation as asc_generation
from asclepius import pipeline as asc_pipeline
from asclepius import profiles as asc_profiles
from asclepius import specialties as asc_specialties
from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_TAXONOMY_VERSION,
    BUYER_REQUEST_STATUSES,
    CONFIDENCE_LEVELS,
    DEFAULT_GROUNDING_MODE,
    ERROR_SEVERITIES,
    ERROR_TAXONOMY,
    EVIDENCE_SOURCE_TYPES,
    GROUNDED_PREMIUM_DISCLAIMER,
    GROUNDING_MODES,
    PREFERENCE_VARIANTS,
    REASONING_STEP_LABELS,
    TASK_SOURCES,
    VERDICTS,
    WHY_BETTER_TAGS,
)
from asclepius.critic import generate_candidates
from asclepius.schemas import (
    BatchFromRequest,
    BuyerIn,
    BuyerRequestIn,
    BuyerRequestStatusUpdate,
    CandidateGenRequest,
    CreateUserRequest,
    ExportRequest,
    GenerationRequest,
    LoginRequest,
    QADecisionRequest,
    SsoRequest,
    SubmissionIn,
    TaskUploadRequest,
)
from asclepius.store import get_store
from asclepius.validation import compute_dedupe_hash, grounding_status, is_grounded

log = logging.getLogger("asclepius.router")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius"])


def _store():
    return get_store()


def _blind_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Strip server-only fields (generator_model) before sending to an evaluator."""
    grounding_mode = task.get("grounding_mode") or DEFAULT_GROUNDING_MODE
    out = {
        "task_id": task["task_id"],
        "specialty": task.get("specialty"),
        "difficulty": task.get("difficulty"),
        "capture_reasoning": bool(task.get("capture_reasoning")),
        "grounding_mode": grounding_mode,
        # earn-more disclaimer surfaced near the verdict buttons only in required mode (opt §1.2)
        "grounding_disclaimer": GROUNDED_PREMIUM_DISCLAIMER if grounding_mode == "required" else None,
        "prompt": task.get("prompt"),
        "candidate_answers": [
            {"id": c.get("id"), "text": c.get("text", "")}
            for c in (task.get("candidate_answers") or [])
        ],
    }
    return out


# ─── Meta ─────────────────────────────────────────────────────────────────────
@router.get("/taxonomy")
async def get_taxonomy(_user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    return {
        "taxonomy_version": ASCLEPIUS_TAXONOMY_VERSION,
        "config_version": ASCLEPIUS_CONFIG_VERSION,
        "verdicts": list(VERDICTS),
        "confidence_levels": list(CONFIDENCE_LEVELS),
        "why_better_tags": list(WHY_BETTER_TAGS),
        "error_tags": list(ERROR_TAXONOMY),
        "error_severities": list(ERROR_SEVERITIES),
        "task_sources": list(TASK_SOURCES),
        "grounding_modes": list(GROUNDING_MODES),
        "grounding_disclaimer": GROUNDED_PREMIUM_DISCLAIMER,
        "evidence_source_types": list(EVIDENCE_SOURCE_TYPES),
        "reasoning_step_labels": list(REASONING_STEP_LABELS),
        "preference_variants": list(PREFERENCE_VARIANTS),
        "export_profiles": asc_profiles.list_profiles(),
    }


# ─── Auth ─────────────────────────────────────────────────────────────────────
@router.post("/auth/login")
async def login(body: LoginRequest):
    store = _store()
    user = asc_auth.authenticate(store, body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    store.log_event(entity_type="user", entity_id=user["id"], event_type="login", actor=user["id"])
    return {"token": asc_auth.create_token(user), "user": asc_auth.public_user(user)}


@router.post("/auth/sso")
async def sso(body: SsoRequest):
    """Exchange a valid doctor-portal session for an Asclepius session (SSO).

    Lets a clinician who is already signed into the doctor portal enter the
    evaluator portal without re-typing credentials. Access stays restricted: the
    presented ``tenant_staff`` token must be valid/unrevoked AND map (by email) to
    an existing, active Asclepius account. We do NOT auto-provision — an
    authenticated doctor without an evaluator account still gets the login form,
    so the standalone Asclepius auth plane remains the source of truth for who may
    evaluate (PRD §3, §7.1)."""
    # Local import keeps the asclepius package import-graph standalone; the SSO
    # bridge is the one deliberate touch-point into the clinical/tenant auth plane.
    from tenant_jwt import decode_tenant_staff_token

    payload = decode_tenant_staff_token(body.token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired doctor session")
    email = (payload.get("sub") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Doctor session is missing an identity")
    store = _store()
    user = store.get_user_by_email(email)
    if not user or not user.get("active"):
        raise HTTPException(
            status_code=403,
            detail="No evaluator account is provisioned for this clinician. "
            "Contact your program administrator.",
        )
    store.log_event(
        entity_type="user", entity_id=user["id"], event_type="sso_login", actor=user["id"]
    )
    return {"token": asc_auth.create_token(user), "user": asc_auth.public_user(user)}


@router.get("/auth/me")
async def me(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    return asc_auth.public_user(user)


# ─── Users (admin) ────────────────────────────────────────────────────────────
@router.post("/users")
async def create_user(
    body: CreateUserRequest, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    if body.role not in ("evaluator", "admin", "qa_reviewer"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if store.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    user = store.create_user(
        email=body.email,
        password=body.password,
        role=body.role,
        specialty=body.specialty,
        board_cert=body.board_cert,
        years_experience=body.years_experience,
    )
    store.log_event(entity_type="user", entity_id=user["id"], event_type="user_created", actor=_admin["id"])
    return asc_auth.public_user(user)


@router.get("/users")
async def list_users(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    return {"users": [asc_auth.public_user(u) for u in _store().list_users()]}


# ─── Tasks ────────────────────────────────────────────────────────────────────
@router.post("/tasks")
async def upload_tasks(
    body: TaskUploadRequest, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    created = []
    for t in body.tasks:
        if not (t.prompt or "").strip():
            continue
        task = store.insert_task(
            task_id=t.task_id,
            prompt=t.prompt,
            specialty=t.specialty,
            difficulty=t.difficulty,
            capture_reasoning=t.capture_reasoning,
            source=t.source,
            candidate_answers=[c.model_dump() for c in t.candidate_answers],
            max_labels=t.max_labels,
            grounding_mode=t.grounding_mode,
            buyer_request_id=t.buyer_request_id,
            created_by=admin["id"],
        )
        created.append(task["task_id"])
    store.log_event(
        entity_type="task", event_type="tasks_uploaded", actor=admin["id"], payload={"count": len(created)}
    )
    return {"created": created, "count": len(created)}


@router.post("/tasks/upload-file")
async def upload_tasks_file(
    file: UploadFile = File(...), admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Accept a JSON (list or {tasks:[...]}) or CSV task batch (PRD §4.3, §6.1)."""
    raw = (await file.read()).decode("utf-8", errors="replace")
    name = (file.filename or "").lower()
    tasks: List[Dict[str, Any]] = []
    if name.endswith(".csv"):
        tasks = _parse_csv_tasks(raw)
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # not JSON — fall back to CSV parsing for unlabeled uploads
            tasks = _parse_csv_tasks(raw)
            if not tasks:
                raise HTTPException(status_code=400, detail=f"Invalid JSON/CSV: {exc}")
            data = None
        if data is not None:
            tasks = data.get("tasks") if isinstance(data, dict) else data
            if not isinstance(tasks, list):
                raise HTTPException(status_code=400, detail="JSON must be a list of tasks or {tasks:[...]}")

    store = _store()
    created = []
    for t in tasks:
        prompt = (t.get("prompt") or "").strip()
        if not prompt:
            continue
        task = store.insert_task(
            task_id=t.get("task_id"),
            prompt=prompt,
            specialty=t.get("specialty") or "general",
            difficulty=t.get("difficulty") or "medium",
            capture_reasoning=bool(t.get("capture_reasoning")),
            source=t.get("source") or "lab_supplied",
            candidate_answers=t.get("candidate_answers") or [],
            max_labels=int(t.get("max_labels") or 1),
            grounding_mode=t.get("grounding_mode") or "optional",
            created_by=admin["id"],
        )
        created.append(task["task_id"])
    store.log_event(
        entity_type="task", event_type="tasks_uploaded_file", actor=admin["id"],
        payload={"count": len(created), "filename": file.filename},
    )
    return {"created": created, "count": len(created)}


def _parse_csv_tasks(raw: str) -> List[Dict[str, Any]]:
    rows = list(csv.DictReader(io.StringIO(raw)))
    out: List[Dict[str, Any]] = []
    for row in rows:
        row = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        prompt = row.get("prompt", "")
        if not prompt:
            continue
        cands = []
        a = row.get("answer_a") or row.get("candidate_a")
        b = row.get("answer_b") or row.get("candidate_b")
        if a:
            cands.append({"id": "A", "text": a, "generator_model": row.get("generator_model_a") or "csv"})
        if b:
            cands.append({"id": "B", "text": b, "generator_model": row.get("generator_model_b") or "csv"})
        cr = (row.get("capture_reasoning") or "").lower() in ("1", "true", "yes")
        out.append(
            {
                "task_id": row.get("task_id") or None,
                "prompt": prompt,
                "specialty": row.get("specialty") or "general",
                "difficulty": row.get("difficulty") or "medium",
                "capture_reasoning": cr,
                "source": row.get("source") or "lab_supplied",
                "candidate_answers": cands,
                "max_labels": int(row.get("max_labels") or 1),
                "grounding_mode": row.get("grounding_mode") or "optional",
            }
        )
    return out


@router.post("/tasks/generate")
async def generate_task(
    body: CandidateGenRequest, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Generate two candidate answers via the LLM and store them as a task."""
    if not (body.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    cands = await generate_candidates(body.prompt, specialty=body.specialty)
    if len(cands) < 2:
        raise HTTPException(
            status_code=503,
            detail="Candidate generation unavailable (no LLM key configured or generation failed).",
        )
    store = _store()
    task = store.insert_task(
        prompt=body.prompt,
        specialty=body.specialty,
        difficulty=body.difficulty,
        capture_reasoning=body.capture_reasoning,
        source="internal_prompt_bank",
        candidate_answers=cands,
        max_labels=body.max_labels,
        grounding_mode=body.grounding_mode,
        created_by=admin["id"],
    )
    store.log_event(
        entity_type="task", entity_id=task["task_id"], event_type="task_generated", actor=admin["id"]
    )
    return {"task_id": task["task_id"]}


# ─── Seedmaker auto-generation (Mode A, PRD §7, §10) ──────────────────────────
@router.post("/generation/{specialty}")
async def generate_specialty_tasks(
    specialty: str,
    body: GenerationRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Generate ``count`` validated tasks (prompt + 2 candidates) for a specialty.

    nephrology is the only enabled specialty in v1; any other returns 400. With
    no LLM configured, returns 503 (we never emit ungated synthetic tasks)."""
    store = _store()
    if body.grounding_mode not in GROUNDING_MODES:
        raise HTTPException(status_code=400, detail="Invalid grounding_mode")
    try:
        result = await asc_generation.generate_tasks(
            store,
            specialty=specialty,
            n=body.count,
            difficulty_mix=body.difficulty_mix,
            capture_reasoning=body.capture_reasoning,
            grounding_mode=body.grounding_mode,
            max_labels=body.max_labels,
            buyer_request_id=body.buyer_request_id,
            created_by=admin["id"],
        )
    except asc_specialties.SpecialtyNotEnabled as exc:
        raise HTTPException(status_code=400, detail={"error": "specialty_not_enabled", "message": str(exc)})
    except asc_generation.GenerationDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except asc_corpus.CorpusError as exc:
        raise HTTPException(status_code=500, detail=f"Seed corpus error: {exc}")
    return result


@router.get("/generation/jobs")
async def list_generation_jobs(
    specialty: Optional[str] = None, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    return {"jobs": _store().list_generation_jobs(specialty=specialty)}


@router.get("/generation/seed-corpus")
async def get_seed_corpus(
    specialty: str = "nephrology", _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    try:
        return asc_corpus.corpus_metadata(specialty)
    except asc_specialties.SpecialtyNotEnabled as exc:
        raise HTTPException(status_code=400, detail={"error": "specialty_not_enabled", "message": str(exc)})
    except asc_corpus.CorpusError as exc:
        raise HTTPException(status_code=500, detail=f"Seed corpus error: {exc}")


@router.get("/specialties")
async def list_specialties(_user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    return {"specialties": asc_specialties.list_specialties()}


@router.get("/tasks")
async def list_tasks(
    specialty: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    tasks = _store().list_tasks(specialty=specialty)
    # admin view keeps generator_model; add submission counts for visibility
    store = _store()
    for t in tasks:
        t["submission_count"] = store.submission_count_for_task(t["task_id"])
    return {"tasks": tasks}


@router.get("/tasks/next")
async def next_task(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    store = _store()
    task = store.next_task_for_evaluator(evaluator_id=user["id"], specialty=user.get("specialty"))
    if not task:
        # fall back to any-specialty queue for admins/QA who have no specialty set
        if not user.get("specialty"):
            task = store.next_task_for_evaluator(evaluator_id=user["id"], specialty=None)
    return {"task": _blind_task(task) if task else None}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, _user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    task = _store().get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _blind_task(task)}


# ─── Submissions ──────────────────────────────────────────────────────────────
@router.post("/submissions")
async def submit(
    body: SubmissionIn, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    store = _store()
    sid = body.submission_id or f"s-{uuid.uuid4().hex[:12]}"

    # Idempotent submit (PRD §10): replaying the same submission_id returns the
    # existing result rather than double-capturing.
    existing = store.get_submission(sid)
    if existing:
        records = store.records_for_submission(sid)
        return {
            "submission_id": sid,
            "status": existing["status"],
            "issues": (existing.get("qa_reason") or "").split(",") if existing.get("qa_reason") else [],
            "record_count": len(records),
            "critic": existing.get("critic"),
            "agreement_score": existing.get("agreement_score"),
        }

    task = store.get_task(body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if body.verdict not in VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid verdict")
    if body.confidence not in CONFIDENCE_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid confidence")

    payload = body.model_dump()

    # Grounding Mode = required (opt §1.2): hard-gate Submit until the rationale
    # (and, on reasoning tasks, every step) carries a valid evidence anchor. This
    # mirrors the frontend submit-gating and is a non-silent 400.
    grounding_mode = task.get("grounding_mode") or "optional"
    if grounding_mode == "required":
        ok, reasons = grounding_status(task, payload)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "grounding_required",
                    "message": "This premium task requires at least one valid evidence anchor (citation) "
                               "on your rationale" + (" and on each reasoning step" if "missing_step_anchor" in reasons else "") + " before submitting.",
                    "reasons": reasons,
                },
            )

    annotator = store.annotator_block(user)
    dedupe_hash = compute_dedupe_hash(task, payload)
    grounded = is_grounded(task, payload)

    submission = store.insert_submission(
        submission_id=sid,
        task_id=body.task_id,
        evaluator_id=user["id"],
        verdict=body.verdict,
        chosen_id=body.chosen_id,
        rejected_id=body.rejected_id,
        confidence=body.confidence,
        time_spent_sec=body.time_spent_sec,
        payload=payload,
        annotator=annotator,
        dedupe_hash=dedupe_hash,
        grounded=grounded,
        grounding_mode=grounding_mode,
        status="submitted",
    )
    store.log_event(
        entity_type="submission",
        entity_id=sid,
        event_type="captured",
        actor=user["id"],
        payload={"task_id": body.task_id, "verdict": body.verdict, "time_spent_sec": body.time_spent_sec},
    )

    result = await asc_pipeline.process_submission(store, task, submission)
    store.refresh_task_status(body.task_id)
    return result


@router.get("/submissions")
async def list_submissions(
    status: Optional[str] = None,
    specialty: Optional[str] = None,
    limit: int = 500,
    _qa: Dict[str, Any] = Depends(asc_auth.require_qa),
):
    subs = _store().list_submissions(status=status, specialty=specialty, limit=limit)
    return {"submissions": subs}


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: str, _qa: Dict[str, Any] = Depends(asc_auth.require_qa)
):
    store = _store()
    sub = store.get_submission(submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    sub["records"] = store.records_for_submission(submission_id)
    sub["task"] = store.get_task(sub["task_id"])
    return sub


# ─── QA ─────────────────────────────────────────────────────────────────────--
@router.get("/qa/queue")
async def qa_queue(_qa: Dict[str, Any] = Depends(asc_auth.require_qa)):
    return {"submissions": _store().list_submissions(status="needs_qa")}


@router.post("/qa/{submission_id}/decision")
async def qa_decision(
    submission_id: str,
    body: QADecisionRequest,
    reviewer: Dict[str, Any] = Depends(asc_auth.require_qa),
):
    store = _store()
    sub = store.get_submission(submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    new_status = asc_pipeline.apply_qa_decision(
        store, sub, decision=body.decision, reviewer_id=reviewer["id"], notes=body.notes
    )
    return {"submission_id": submission_id, "status": new_status}


# ─── Export ─────────────────────────────────────────────────────────────────--
@router.post("/exports")
async def create_export(
    body: ExportRequest, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    try:
        manifest = asc_export.build_export(
            store,
            created_by=admin["id"],
            profile=body.profile,
            specialty=body.specialty,
            difficulty=body.difficulty,
            record_type=body.record_type,
            since=body.since,
            until=body.until,
            grounded_only=body.grounded_only,
            confidence_floor=body.confidence_floor,
            min_agreement=body.min_agreement,
            buyer_request_id=body.buyer_request_id,
            note=body.note,
        )
    except asc_export.ExportValidationError as exc:
        # A mapped line failed the buyer profile schema — fail the batch loudly.
        raise HTTPException(status_code=422, detail=str(exc))
    except asc_profiles.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return manifest


@router.get("/profiles")
async def list_export_profiles(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    return {"profiles": asc_profiles.list_profiles()}


@router.get("/exports")
async def list_exports(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    return {"exports": _store().list_exports()}


@router.get("/exports/{export_id}/download")
async def download_export(
    export_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    export = store.get_export(export_id)
    if not export:
        raise HTTPException(status_code=404, detail="Export not found")
    data = asc_export.zip_export(export)
    headers = {"Content-Disposition": f'attachment; filename="{export_id}.zip"'}
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers=headers)


# ─── Buyers & buyer requests (opt §2.5) ───────────────────────────────────────
@router.post("/buyers")
async def create_buyer(body: BuyerIn, admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    buyer = store.create_buyer(
        name=body.name, contact=body.contact, export_profile=body.export_profile, notes=body.notes
    )
    store.log_event(entity_type="buyer", entity_id=buyer["buyer_id"], event_type="buyer_created", actor=admin["id"])
    return buyer


@router.get("/buyers")
async def list_buyers(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    return {"buyers": _store().list_buyers()}


@router.post("/buyer-requests")
async def create_buyer_request(
    body: BuyerRequestIn, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    if not store.get_buyer(body.buyer_id):
        raise HTTPException(status_code=404, detail="Buyer not found")
    if body.source not in TASK_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid source")
    if body.grounding_mode not in GROUNDING_MODES:
        raise HTTPException(status_code=400, detail="Invalid grounding_mode")
    constraints = {
        "specialty": body.specialty,
        "difficulty": body.difficulty,
        "capture_reasoning": body.capture_reasoning,
        "grounding_mode": body.grounding_mode,
        "volume": body.volume,
        "max_labels": body.max_labels,
    }
    uploaded = [t.model_dump() for t in body.prompts]
    req = store.create_buyer_request(
        buyer_id=body.buyer_id,
        source=body.source,
        export_profile=body.export_profile,
        constraints=constraints,
        uploaded=uploaded,
        note=body.note,
        created_by=admin["id"],
    )
    store.log_event(
        entity_type="buyer_request", entity_id=req["request_id"],
        event_type="buyer_request_created", actor=admin["id"], payload={"buyer_id": body.buyer_id},
    )
    return req


@router.get("/buyer-requests")
async def list_buyer_requests(
    buyer_id: Optional[str] = None, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    return {"buyer_requests": _store().list_buyer_requests(buyer_id=buyer_id)}


@router.get("/buyer-requests/{request_id}")
async def get_buyer_request(request_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    req = _store().get_buyer_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Buyer request not found")
    return req


@router.post("/buyer-requests/{request_id}/status")
async def set_buyer_request_status(
    request_id: str, body: BuyerRequestStatusUpdate, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    if not store.get_buyer_request(request_id):
        raise HTTPException(status_code=404, detail="Buyer request not found")
    if body.status not in BUYER_REQUEST_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {list(BUYER_REQUEST_STATUSES)}")
    store.update_buyer_request_status(request_id, body.status)
    return {"request_id": request_id, "status": body.status}


@router.post("/buyer-requests/{request_id}/batch")
async def batch_from_request(
    request_id: str, body: BatchFromRequest, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Spin up a task batch from a buyer request in one step (opt §2.5).

    Tasks inherit the request's constraints (incl. grounding_mode) and stamp the
    request id + source into every record's provenance. With uploaded prompts we
    grade exactly what the buyer sent; with constraints-only + ``count`` we invoke
    the Seedmaker engine (still our prompts, their spec) — PRD §10."""
    store = _store()
    req = store.get_buyer_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Buyer request not found")

    c = req.get("constraints") or {}
    source = req.get("source") or "internal_prompt_bank"
    grounding_mode = c.get("grounding_mode") or "optional"
    capture_reasoning = bool(c.get("capture_reasoning"))
    difficulty = c.get("difficulty") or "medium"
    specialty = c.get("specialty") or "nephrology"
    max_labels = int(c.get("max_labels") or 1)

    # Prompts: those uploaded on the request + any passed at batch time.
    uploaded = list(req.get("uploaded") or []) + [t.model_dump() for t in body.prompts]
    created: List[str] = []

    for t in uploaded:
        prompt = (t.get("prompt") or "").strip()
        if not prompt:
            continue
        task = store.insert_task(
            prompt=prompt,
            specialty=t.get("specialty") or specialty,
            difficulty=t.get("difficulty") or difficulty,
            capture_reasoning=bool(t.get("capture_reasoning", capture_reasoning)),
            source=source,
            candidate_answers=t.get("candidate_answers") or [],
            max_labels=int(t.get("max_labels") or max_labels),
            grounding_mode=t.get("grounding_mode") or grounding_mode,
            buyer_request_id=request_id,
            created_by=admin["id"],
        )
        created.append(task["task_id"])

    # Constraints-only: invoke the Seedmaker engine to generate ``count`` validated
    # tasks (prompt + 2 candidates) grounded in the seed corpus, stamped to this
    # buyer request. Requires an LLM (503 if disabled — never ungated tasks).
    gen_summary: Optional[Dict[str, Any]] = None
    if body.count and not uploaded:
        try:
            gen_summary = await asc_generation.generate_tasks(
                store,
                specialty=specialty,
                n=body.count,
                capture_reasoning=capture_reasoning,
                grounding_mode=grounding_mode,
                max_labels=max_labels,
                buyer_request_id=request_id,
                created_by=admin["id"],
            )
            created.extend(gen_summary.get("created") or [])
        except asc_specialties.SpecialtyNotEnabled as exc:
            raise HTTPException(status_code=400, detail={"error": "specialty_not_enabled", "message": str(exc)})
        except asc_generation.GenerationDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    store.update_buyer_request_status(request_id, "in_progress")
    store.log_event(
        entity_type="buyer_request", entity_id=request_id, event_type="batch_created",
        actor=admin["id"], payload={"count": len(created)},
    )
    out = {"request_id": request_id, "created": created, "count": len(created)}
    if gen_summary is not None:
        out["generation"] = {
            "job_id": gen_summary.get("job_id"),
            "accepted": gen_summary.get("accepted"),
            "dropped": gen_summary.get("dropped"),
            "shortfall": gen_summary.get("shortfall"),
        }
    return out


# ─── Dashboard (admin) ────────────────────────────────────────────────────────
@router.get("/stats")
async def stats(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    grounded = store.grounded_counts()
    grounded_pct = (
        round(100 * grounded["submissions_grounded"] / grounded["submissions_total"], 1)
        if grounded["submissions_total"]
        else 0.0
    )
    return {
        "status_counts": store.status_counts(),
        "qa_pass_rate": store.qa_pass_rate(),
        "average_agreement": store.average_agreement(),
        "kappa": asc_agreement.aggregate_kappa(store.list_agreement_observations()),
        "grounded": {**grounded, "grounded_pct": grounded_pct},
        "flaw_catch_rate": store.flaw_catch_rate(),
        "evaluator_throughput": store.evaluator_throughput(),
        "contributor_stats": store.contributor_stats(),
        "export_count": len(store.list_exports(limit=1000)),
        "task_count": len(store.list_tasks(limit=100000)),
        "generation_jobs": len(store.list_generation_jobs(limit=10000)),
    }


@router.get("/events")
async def events(
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    limit: int = 200,
    _qa: Dict[str, Any] = Depends(asc_auth.require_qa),
):
    return {"events": _store().list_events(entity_type=entity_type, entity_id=entity_id, limit=limit)}
