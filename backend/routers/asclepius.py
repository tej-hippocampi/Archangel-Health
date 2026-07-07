"""Asclepius — Expert Evaluation Portal API (PRD §7, §9.2).

Standalone-auth router mounted at ``/api/asclepius`` in ``main.py``. Business
logic lives in the ``backend/asclepius/`` package; this file is the HTTP surface.

Auth is the Asclepius-local JWT (NOT the clinical/tenant auth). Role gates:
  evaluator   -> queue + submit
  qa_reviewer -> QA queue + decisions (also admin)
  admin       -> everything (users, tasks, candidate-gen, export, dashboard)
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from asclepius import agreement as asc_agreement
from asclepius import auth as asc_auth
from asclepius import corpus as asc_corpus
from asclepius import credentials as asc_credentials
from asclepius import export as asc_export
from asclepius import generation as asc_generation
from asclepius import pipeline as asc_pipeline
from asclepius import profiles as asc_profiles
from asclepius import specialties as asc_specialties
from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_TAXONOMY_VERSION,
    BUYER_REQUEST_STATUSES,
    CREDENTIAL_SUMMARY_LEGAL_DISCLAIMER,
    CREDENTIAL_SUMMARY_WATERMARK,
    CONFIDENCE_LEVELS,
    DEFAULT_GROUNDING_MODE,
    DEFAULT_INDEPENDENT_MODE,
    ERROR_SEVERITIES,
    ERROR_TAG_REASONS,
    ERROR_TAXONOMY,
    EVIDENCE_SOURCE_TYPES,
    GROUNDED_PREMIUM_DISCLAIMER,
    GROUNDING_MODES,
    INDEPENDENT_MODES,
    PORTAL_VERSIONS,
    PREFERENCE_VARIANTS,
    PROMPT_FLAGGED_TASK_STATUS,
    PROMPT_REVIEW_VERDICTS,
    REASONING_STEP_LABELS,
    STEP_CORRECTION_REASONS,
    TASK_SOURCES,
    VALUE_TIERS,
    VERDICTS,
    WHY_BETTER_TAGS,
    assist_min_confidence,
    normalize_independent_mode,
    normalize_portal_version,
    value_per_minute_target,
)
from asclepius import stt as asc_stt
from asclepius import value as asc_value
from asclepius.critic import (
    generate_candidates,
    generate_candidates_ex,
    run_prelabel,
    run_reasoning_pregrade,
    run_reasoning_split,
)
from asclepius.constants import (
    company_name as _company_name,
    non_circumvention_notice as _non_circumvention_notice,
)
from asclepius.schemas import (
    BatchFromRequest,
    BuyerIn,
    BuyerRequestIn,
    BuyerRequestStatusUpdate,
    CandidateGenRequest,
    ContributorCredentialsIn,
    CreateUserRequest,
    CredentialSummaryRequest,
    ExportRequest,
    GenerationRequest,
    IndependentAnswer,
    LoginRequest,
    PrelabelRequest,
    QADecisionRequest,
    ReasoningSplitRequest,
    ScopedExportRequest,
    SsoRequest,
    SubmissionIn,
    TaskUploadRequest,
)
from asclepius.store import get_store
from asclepius.validation import compute_dedupe_hash, grounding_status, is_grounded, residual_identifiers

log = logging.getLogger("asclepius.router")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius"])


def _store():
    return get_store()


def _withhold_answers() -> bool:
    """v2 anti-peeking (Eval Flow Upgrade §1): when on (default), the candidate
    answer TEXT is omitted from the blinded task payload so it isn't even on the
    wire during Stages 1–2 — the evaluator fetches it via ``GET /tasks/{id}/answers``
    only after committing their independent answer. Set ASCLEPIUS_WITHHOLD_ANSWERS=0
    to fall back to v1 (text inline; DOM-withholding only)."""
    return os.getenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1").strip().lower() in ("1", "true", "yes", "on")


def _blind_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Strip server-only fields (generator_model) before sending to an evaluator,
    and — under v2 anti-peeking (default on) — withhold the candidate answer text
    until the independent answer is committed (see :func:`_withhold_answers`)."""
    grounding_mode = task.get("grounding_mode") or DEFAULT_GROUNDING_MODE
    withhold = _withhold_answers()
    answers = []
    for c in (task.get("candidate_answers") or []):
        entry = {"id": c.get("id")}
        if not withhold:
            entry["text"] = c.get("text", "")
        answers.append(entry)
    out = {
        "task_id": task["task_id"],
        "specialty": task.get("specialty"),
        "difficulty": task.get("difficulty"),
        "capture_reasoning": bool(task.get("capture_reasoning")),
        "grounding_mode": grounding_mode,
        # Stage-2 capture mode (Speed Optimization §1): stance (default) | full.
        "independent_mode": task.get("independent_mode") or DEFAULT_INDEPENDENT_MODE,
        # earn-more disclaimer surfaced near the verdict buttons only in required mode (opt §1.2)
        "grounding_disclaimer": GROUNDED_PREMIUM_DISCLAIMER if grounding_mode == "required" else None,
        "prompt": task.get("prompt"),
        "candidate_answers": answers,
        # Tells the client the texts must be fetched at reveal (Stage 2 -> 3).
        "answers_withheld": withhold,
    }
    return out


def _task_answers(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Candidate answer texts for the reveal step — still blinded (never leaks
    generator_model)."""
    return [
        {"id": c.get("id"), "text": c.get("text", "")}
        for c in (task.get("candidate_answers") or [])
    ]


# ─── Meta ─────────────────────────────────────────────────────────────────────
@router.get("/taxonomy")
async def get_taxonomy(_user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    return {
        "taxonomy_version": ASCLEPIUS_TAXONOMY_VERSION,
        "config_version": ASCLEPIUS_CONFIG_VERSION,
        "verdicts": list(VERDICTS),
        "prompt_review_verdicts": list(PROMPT_REVIEW_VERDICTS),
        "confidence_levels": list(CONFIDENCE_LEVELS),
        "why_better_tags": list(WHY_BETTER_TAGS),
        "error_tags": list(ERROR_TAXONOMY),
        "error_severities": list(ERROR_SEVERITIES),
        "task_sources": list(TASK_SOURCES),
        "grounding_modes": list(GROUNDING_MODES),
        "grounding_disclaimer": GROUNDED_PREMIUM_DISCLAIMER,
        "evidence_source_types": list(EVIDENCE_SOURCE_TYPES),
        "reasoning_step_labels": list(REASONING_STEP_LABELS),
        "step_correction_reasons": list(STEP_CORRECTION_REASONS),
        "error_tag_reasons": list(ERROR_TAG_REASONS),
        "independent_modes": list(INDEPENDENT_MODES),
        "portal_versions": list(PORTAL_VERSIONS),
        "value_tiers": list(VALUE_TIERS),
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

    A clinician already signed into the doctor portal enters the evaluator portal
    automatically — no second login. The presented ``tenant_staff`` token is the
    access barrier: it must be valid/unrevoked (only an authenticated, affiliated
    clinician holds one). On first arrival we auto-provision an evaluator account
    keyed to the doctor's email so access "just works"; on later visits we resume
    that same account. The portal is never left unauthenticated — an anonymous
    visitor with no doctor session still gets the login form (PRD §3, §7.1)."""
    # Local import keeps the asclepius package import-graph standalone; the SSO
    # bridge is the one deliberate touch-point into the clinical/tenant auth plane.
    import secrets as _secrets

    from tenant_jwt import decode_tenant_staff_token

    payload = decode_tenant_staff_token(body.token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired doctor session")
    email = (payload.get("sub") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Doctor session is missing an identity")

    store = _store()
    user = store.get_user_by_email(email)
    provisioned = False
    if not user:
        # First SSO arrival for this affiliated clinician — provision an evaluator
        # seat on the fly. The password is a throwaway random value: this account
        # is reached via SSO, not a typed credential.
        user = store.create_user(
            email=email,
            password=_secrets.token_urlsafe(32),
            role="evaluator",
        )
        provisioned = True
    if not user.get("active"):
        raise HTTPException(status_code=403, detail="This evaluator account is disabled.")

    store.log_event(
        entity_type="user",
        entity_id=user["id"],
        event_type="sso_provisioned" if provisioned else "sso_login",
        actor=user["id"],
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
            independent_mode=t.independent_mode or DEFAULT_INDEPENDENT_MODE,
            buyer_request_id=t.buyer_request_id,
            value_tier=t.value_tier,
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
            independent_mode=t.get("independent_mode") or DEFAULT_INDEPENDENT_MODE,
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
                "independent_mode": row.get("independent_mode") or DEFAULT_INDEPENDENT_MODE,
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
    if body.independent_mode not in INDEPENDENT_MODES:
        raise HTTPException(status_code=400, detail="Invalid independent_mode")
    try:
        result = await asc_generation.generate_tasks(
            store,
            specialty=specialty,
            n=body.count,
            difficulty_mix=body.difficulty_mix,
            capture_reasoning=body.capture_reasoning,
            grounding_mode=body.grounding_mode,
            independent_mode=body.independent_mode,
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
    status: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    tasks = _store().list_tasks(specialty=specialty, status=status)
    # admin view keeps generator_model; add submission counts for visibility
    store = _store()
    for t in tasks:
        t["submission_count"] = store.submission_count_for_task(t["task_id"])
    return {"tasks": tasks}


# ─── Queue auto-fill ──────────────────────────────────────────────────────────
# When an evaluator opens an empty queue, run the generation engine on demand so
# prompts + candidate answers appear automatically — no admin step. Guarded so it
# can't stampede the LLM: one in-flight generation at a time, plus a per-specialty
# cooldown so repeated refreshes (or a configured-off LLM) don't burn budget.
_AUTOFILL_LOCK = asyncio.Lock()
_autofill_last_attempt: Dict[str, float] = {}


def _autofill_enabled() -> bool:
    return (os.getenv("ASCLEPIUS_AUTOFILL", "1").strip().lower() in ("1", "true", "yes", "on"))


def _autofill_batch() -> int:
    try:
        return max(1, min(10, int(os.getenv("ASCLEPIUS_AUTOFILL_BATCH", "3"))))
    except ValueError:
        return 3


def _autofill_cooldown_sec() -> float:
    try:
        return max(0.0, float(os.getenv("ASCLEPIUS_AUTOFILL_COOLDOWN_SEC", "30")))
    except ValueError:
        return 30.0


def _autofill_specialty(user: Dict[str, Any]) -> str:
    """The evaluator's specialty if it's enabled, else the v1 default (nephrology)."""
    want = (user.get("specialty") or "").strip().lower()
    if want and asc_specialties.is_enabled(want):
        return want
    return "nephrology"


def _value_aware_next(store: Any, user: Dict[str, Any], specialty: Optional[str]) -> Optional[Dict[str, Any]]:
    """Value-aware routing (Value-per-Minute PRD B3) — V2 ONLY. Serves the eligible
    task with the highest expected value-per-minute for THIS contributor (their
    rolling median speed × each task's expected realized value). Ties break on
    the oldest task, preserving FIFO fairness within an equal-value cohort."""
    candidates = store.eligible_tasks_for_evaluator(
        evaluator_id=user["id"], specialty=specialty
    )
    if not candidates:
        return None
    median_secs = store.evaluator_median_seconds(user["id"])
    # Higher score first; stable sort keeps the original oldest-first order as the
    # tiebreaker (candidates already arrive oldest-first).
    ranked = sorted(
        candidates,
        key=lambda t: asc_value.routing_score(t, median_secs),
        reverse=True,
    )
    return ranked[0]


def _query_next(
    store: Any, user: Dict[str, Any], *, portal_version: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # Value-aware routing is a V2-only enhancement. V1 (and any request that does
    # not explicitly declare the v2 flow) keeps the exact classic oldest-first
    # behavior — this is the "edits only on V2" guarantee, enforced at the gate.
    # Match the LITERAL "v2" only: an absent, empty, v1, or typo'd value must fall
    # to classic (normalize_portal_version would map "" / "v3" to the "v2" default
    # and silently opt them in — exactly what this gate must not do).
    value_aware = portal_version == "v2"

    def _classic(specialty: Optional[str]) -> Optional[Dict[str, Any]]:
        return store.next_task_for_evaluator(evaluator_id=user["id"], specialty=specialty)

    pick = _value_aware_next(store, user, user.get("specialty")) if value_aware else _classic(user.get("specialty"))
    if not pick and not user.get("specialty"):
        # admins/QA (and SSO-provisioned clinicians) with no specialty see any queue
        pick = _value_aware_next(store, user, None) if value_aware else _classic(None)
    return pick


async def _seed_tasks_from_corpus(store: Any, specialty: str, batch: int) -> int:
    """Turn ratified seed-corpus prompts into eval tasks by generating only the
    two candidate answers (Sonnet ``asclepius_candidate_gen``). This deliberately
    bypasses the Opus prompt-synthesis + judge + dedupe pipeline (``generate_tasks``)
    so the queue fills reliably and fast: the prompts are already vetted, we just
    need the A/B answers. Returns the number of tasks created."""
    items = asc_corpus.load_corpus(specialty).get("items") or []
    # Dedup against prompts ALREADY in the queue/DB only — NOT the corpus itself
    # (generation._existing_prompt_hashes also hashes the seeds, which here are
    # exactly what we want to use). Otherwise every seed reads as "already seen".
    existing = {
        asc_generation._prompt_hash(t.get("prompt"))  # noqa: SLF001
        for t in store.list_tasks(specialty=specialty, limit=100000)
    }
    picks: List[Dict[str, Any]] = []
    for it in items:
        prompt = (it.get("prompt") or "").strip()
        if not prompt or asc_generation._prompt_hash(prompt) in existing:  # noqa: SLF001
            continue
        picks.append(it)
        if len(picks) >= batch:
            break
    if not picks:
        return 0
    # Generate the candidate pairs concurrently so first load is ~one LLM call.
    gens = await asyncio.gather(
        *[
            generate_candidates_ex(
                (it.get("prompt") or "").strip(),
                specialty=specialty,
                ai_failure_mode=it.get("ai_failure_mode"),
            )
            for it in picks
        ],
        return_exceptions=True,
    )
    created = 0
    llm_failed = False
    for it, gen in zip(picks, gens):
        if isinstance(gen, Exception):
            llm_failed = True
            continue
        cands = (gen or {}).get("candidates") or []
        if len(cands) < 2:
            llm_failed = True
            continue
        store.insert_task(
            prompt=(it.get("prompt") or "").strip(),
            specialty=specialty,
            difficulty=it.get("difficulty") or "medium",
            capture_reasoning=bool(it.get("capture_reasoning_recommended")),
            source="internal_prompt_bank",
            candidate_answers=cands,
            grounding_mode=DEFAULT_GROUNDING_MODE,
            generation={
                "mode": "autofill_seed",
                "seed_id": it.get("seed_id"),
                "intended_flawed_id": gen.get("intended_flawed_id"),
                "candidate_model": gen.get("model"),
            },
            created_by="system:autofill",
        )
        created += 1
    if created == 0 and llm_failed:
        log.warning(
            "asclepius autofill: candidate generation produced no answers. Check that "
            "ANTHROPIC_API_KEY is set and the 'asclepius_candidate_gen' model "
            "(configured in ai/model_config.py, override via MODEL_ASCLEPIUS_CANDIDATE_GEN) "
            "is reachable."
        )
    return created


async def _autofill_queue(
    store: Any, user: Dict[str, Any], *, portal_version: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    if not _autofill_enabled():
        return None
    specialty = _autofill_specialty(user)
    cooldown = _autofill_cooldown_sec()
    if time.monotonic() - _autofill_last_attempt.get(specialty, 0.0) < cooldown:
        return None
    async with _AUTOFILL_LOCK:
        # Another request may have filled the queue (or just attempted) while we
        # waited on the lock — re-check both before spending LLM budget.
        task = _query_next(store, user, portal_version=portal_version)
        if task:
            return task
        if time.monotonic() - _autofill_last_attempt.get(specialty, 0.0) < cooldown:
            return None
        _autofill_last_attempt[specialty] = time.monotonic()
        try:
            created = await _seed_tasks_from_corpus(store, specialty, _autofill_batch())
            log.info("asclepius autofill: created %d task(s) for %s", created, specialty)
        except asc_specialties.SpecialtyNotEnabled:
            return None
        except asc_corpus.CorpusError as exc:
            log.warning("asclepius autofill: seed corpus error: %s", exc)
            return None
        except Exception:  # never let generation break the evaluator's queue request
            log.exception("asclepius autofill failed")
            return None
    return _query_next(store, user, portal_version=portal_version)


@router.get("/tasks/next")
async def next_task(
    portal_version: Optional[str] = Query(
        None,
        description="Declare the active flow. 'v2' opts into value-aware routing "
        "(serves the highest expected value-per-minute task for you). Absent or "
        "'v1' keeps the classic oldest-first queue unchanged.",
    ),
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    store = _store()
    task = _query_next(store, user, portal_version=portal_version)
    if not task:
        # Empty queue -> auto-generate a fresh batch via the engine, then serve.
        task = await _autofill_queue(store, user, portal_version=portal_version)
    return {"task": _blind_task(task) if task else None}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, _user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    task = _store().get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _blind_task(task)}


@router.post("/tasks/{task_id}/reveal")
async def reveal_task_answers(
    task_id: str, body: IndependentAnswer, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Commit the evaluator's blind independent answer and reveal the candidate
    answers in one step (Eval Flow Upgrade §1, v2 anti-peeking). This is the ONLY
    way to obtain the answer texts under withholding: a non-empty independent
    answer must be recorded server-side FIRST, so the answer was provably written
    before the AI answers were seen. The commit is the authoritative independent
    answer used at packaging. Idempotent — the first commit's answer/timestamp win."""
    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "independent_answer_required",
                "message": "Write your independent answer before revealing the AI answers.",
            },
        )
    # The contributor's portal version drives the capture kind: V1 (classic)
    # ALWAYS commits a full blind ideal answer; V2 respects the task's mode
    # (stance default, 'full' for premium/eval). ``kind`` is stamped
    # server-side, never trusted from the client — a quick stance can't be
    # passed off as a full blind ideal answer (Speed Optimization §1).
    pv = normalize_portal_version(body.portal_version)
    kind = "full" if pv == "v1" else normalize_independent_mode(task.get("independent_mode"))
    store.commit_independent_answer(
        task_id=task_id,
        evaluator_id=user["id"],
        payload={
            "text": text,
            "kind": kind,
            "portal_version": pv,
            "evidence_anchor": body.evidence_anchor.model_dump() if body.evidence_anchor else None,
        },
    )
    store.log_event(
        entity_type="task", entity_id=task_id,
        event_type="independent_answer_committed", actor=user["id"],
    )
    return {"answers": _task_answers(task), "committed": True}


def _require_independent_commit(store: Any, task_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """The v2 anti-peeking gate, shared by every endpoint that describes the
    candidate answers (answer re-fetch, prelabel suggestions): the evaluator
    must have committed their blind independent capture first. One policy, one
    place — a hardening change here covers every answer-describing surface."""
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not store.get_independent_commit(task_id, user["id"]):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "independent_answer_required",
                "message": "Commit your independent answer (POST /tasks/{id}/reveal) before revealing the AI answers.",
            },
        )
    return task


@router.get("/tasks/{task_id}/answers")
async def get_task_answers(task_id: str, user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Re-fetch the revealed candidate answer texts (Eval Flow Upgrade §1, v2 anti-
    peeking) — e.g. on a mid-task refresh resuming into the compare stage. GATED:
    returns text only to an evaluator who has already committed an independent
    answer (POST /tasks/{id}/reveal). Texts are still blinded (no generator_model)."""
    task = _require_independent_commit(_store(), task_id, user)
    return {"answers": _task_answers(task)}


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

    # Stage-1 prompt validation gate (Eval Flow Upgrade §2): a clinician who
    # flagged the prompt as invalid never judged answers. Capture the flag for
    # audit + admin triage, mark the TASK flagged (out of the queue), and produce
    # ZERO records. The doctor advances to the next task. Handled BEFORE verdict
    # validation because a flagged submission carries no verdict.
    review = body.prompt_review
    if review and review.verdict == "flagged":
        # Defensive PHI scan on the flag reason — the flagged path skips
        # validate_submission, but the PRD's "PHI scan on every submission"
        # (§0/§13) still applies. Redact rather than persist a raw identifier.
        note_phi = residual_identifiers(review.note) if review.note else []
        safe_note = "[redacted — possible identifier detected]" if note_phi else review.note
        flag_pv = normalize_portal_version(body.portal_version)
        flagged_payload = body.model_dump()
        flagged_payload["portal_version"] = flag_pv
        if note_phi:
            (flagged_payload.get("prompt_review") or {})["note"] = safe_note
        store.insert_submission(
            submission_id=sid,
            task_id=body.task_id,
            evaluator_id=user["id"],
            verdict=None,
            chosen_id=None,
            rejected_id=None,
            confidence=body.confidence,
            time_spent_sec=body.time_spent_sec,
            payload=flagged_payload,
            annotator=store.annotator_block(user),
            dedupe_hash=None,
            grounded=False,
            grounding_mode=task.get("grounding_mode") or "optional",
            portal_version=flag_pv,
            status=PROMPT_FLAGGED_TASK_STATUS,
        )
        store.mark_task_status(body.task_id, PROMPT_FLAGGED_TASK_STATUS)
        # If a concurrent evaluator already graded this task (max_labels >= 2, or a
        # race at max_labels=1), pull their not-yet-shipped records back to QA so a
        # flagged prompt never silently exports. Route to needs_qa (a human can
        # still decide), never reject — no lost work. Already-exported records
        # cannot be unshipped.
        for sib in store.submissions_for_task(body.task_id):
            if sib["submission_id"] == sid:
                continue
            if sib.get("status") in ("submitted", "auto_validated", "qa_checked", "export_ready"):
                store.update_submission(sib["submission_id"], status="needs_qa", qa_reason="prompt_flagged")
                store.update_records_status_for_submission(sib["submission_id"], "needs_qa")
                store.log_event(
                    entity_type="submission", entity_id=sib["submission_id"],
                    event_type="routed_to_qa", actor=user["id"],
                    payload={"reason": "prompt_flagged", "task_id": body.task_id},
                )
        store.log_event(
            entity_type="task",
            entity_id=body.task_id,
            event_type="prompt_flagged",
            actor=user["id"],
            payload={"submission_id": sid, "note": safe_note, "phi_redacted": bool(note_phi)},
        )
        return {
            "submission_id": sid,
            "status": PROMPT_FLAGGED_TASK_STATUS,
            "issues": [],
            "record_count": 0,
            "critic": None,
            "agreement_score": None,
        }

    if body.verdict not in VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid verdict")
    if body.confidence not in CONFIDENCE_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid confidence")

    payload = body.model_dump()

    # The independent answer that ships is the one COMMITTED before reveal (Eval
    # Flow Upgrade §1), not whatever the post-reveal client submits — so a client
    # can't unlock the answers with a throwaway commit and then pass off an
    # AI-influenced answer as the blind one. Falls back to the submitted value when
    # no commit exists (withholding disabled, or a direct API client).
    _commit = store.get_independent_commit(body.task_id, user["id"])
    if _commit:
        payload["independent_answer"] = _commit["payload"]

    # Portal version (Asclepius V2): the reveal commit's stamped version is
    # authoritative (it drove the capture kind); fall back to the client's
    # declared version when no commit exists (v1 with withholding off, or a
    # direct API client). Stamped onto the row + payload so packaging carries it
    # onto every record.
    portal_version = normalize_portal_version(
        (_commit or {}).get("payload", {}).get("portal_version") or body.portal_version
    )
    payload["portal_version"] = portal_version

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
        portal_version=portal_version,
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

    # If another clinician already flagged this prompt as invalid, a grading that
    # races in afterward must not silently export (Eval Flow Upgrade §2). Route it
    # to QA instead of auto-export — never lose the work, never ship a flagged
    # prompt's records. Re-read the task so a flag committed during processing is
    # seen. (refresh_task_status leaves the prompt_flagged task as-is.)
    _cur = store.get_task(body.task_id) or {}
    if _cur.get("status") == PROMPT_FLAGGED_TASK_STATUS and result.get("status") in ("auto_validated", "export_ready"):
        store.update_submission(sid, status="needs_qa", qa_reason="prompt_flagged")
        store.update_records_status_for_submission(sid, "needs_qa")
        store.log_event(
            entity_type="submission", entity_id=sid, event_type="routed_to_qa",
            actor=user["id"], payload={"reason": "prompt_flagged", "task_id": body.task_id},
        )
        result["status"] = "needs_qa"
        result["issues"] = sorted(set((result.get("issues") or []) + ["prompt_flagged"]))

    store.refresh_task_status(body.task_id)
    return result


@router.post("/reasoning/split")
async def reasoning_split(
    body: ReasoningSplitRequest, _user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Split a chosen/ideal answer into ordered reasoning steps for tap-to-grade
    (Eval Flow Upgrade §4). Returns ``{steps: [str, ...], source}``. Degrades to a
    local heuristic split when no LLM is configured (never errors the doctor)."""
    res = await run_reasoning_split(body.text, prompt=body.prompt, specialty=body.specialty)
    return {"steps": res.get("steps", []), "source": res.get("source")}


@router.post("/reasoning/pregrade")
async def reasoning_pregrade(
    body: ReasoningSplitRequest, _user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Split + pre-grade an answer's reasoning steps (Speed Optimization §2):
    each step arrives with a SUGGESTED ``good``/``bad`` label (+ a one-line
    critique on bad steps) so the doctor verifies instead of authoring. The
    labels are suggestions only — every step still requires an explicit human
    confirm/correct before submit. Degrades to the heuristic splitter with
    ``suggested_label = null`` when no LLM is configured."""
    res = await run_reasoning_pregrade(body.text, prompt=body.prompt, specialty=body.specialty)
    return {
        "steps": res.get("steps", []),
        "source": res.get("source"),
        "skipped": bool(res.get("skipped")),
    }


# ─── Model-assisted pre-labeling (Speed Optimization §2) ─────────────────────
@router.post("/assist/prelabel")
async def assist_prelabel(
    body: PrelabelRequest, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Suggest the weaker answer + error tags + a draft rationale for a task the
    evaluator is grading — VERIFY, don't author. Guardrails:

      * Anti-peeking: gated behind the evaluator's independent-answer commit
        (like ``GET /tasks/{id}/answers``) — the suggestion describes the A/B
        answers, so it must not exist pre-reveal.
      * Never applied server-side: the verdict/tags/rationale stay untouched;
        the client renders the suggestion as a tap-to-accept hint only.
      * Low-confidence suggestions (< ASCLEPIUS_ASSIST_MIN_CONF, default 0.6)
        are HIDDEN — returned as ``skipped`` so the UI never nudges on an
        uncertain call.
      * Degrades to ``skipped=True`` with no LLM key — manual labeling always
        works.
    """
    store = _store()
    # Unconditional (even with withholding off): the suggestion names the weaker
    # answer + error spans, so it must never exist before the blind commit.
    task = _require_independent_commit(store, body.task_id, user)
    res = await run_prelabel(task)
    if res.get("skipped"):
        return {"skipped": True, "reason": res.get("error") or "assist_unavailable"}
    min_conf = assist_min_confidence()
    if (res.get("confidence") or 0.0) < min_conf:
        # Quality guardrail: don't nudge on uncertain calls. The suggestion is
        # withheld entirely (not just de-emphasized).
        store.log_event(
            entity_type="task", entity_id=body.task_id, event_type="prelabel_hidden_low_conf",
            actor=user["id"], payload={"confidence": res.get("confidence"), "min_conf": min_conf},
        )
        return {"skipped": True, "reason": "low_confidence"}
    store.log_event(
        entity_type="task", entity_id=body.task_id, event_type="prelabel_suggested",
        actor=user["id"],
        payload={
            "suggested_weaker": res.get("suggested_weaker"),
            "suggested_error_tags": res.get("suggested_error_tags"),
            "confidence": res.get("confidence"),
        },
    )
    return {
        "skipped": False,
        "suggested_weaker": res.get("suggested_weaker"),
        "suggested_error_tags": res.get("suggested_error_tags") or [],
        "suggested_rationale": res.get("suggested_rationale"),
        "error_spans": res.get("error_spans") or [],
        "confidence": res.get("confidence"),
    }


# ─── Voice dictation (Speed Optimization §4) ──────────────────────────────────
@router.post("/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...), _user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Transcribe a short dictation clip from the in-app mic. Provider-abstracted
    (``ASCLEPIUS_STT_PROVIDER``: ``standard`` = Deepgram/Whisper, ``wispr`` stub).
    Audio is EPHEMERAL — held in memory for this request only, never persisted
    (synthetic prompts, no PHI; TLS in transit). 503 when no provider is
    configured so the mic button can degrade to typing."""
    data = await file.read()
    res = await asc_stt.transcribe(data, mime=file.content_type or "audio/webm")
    if res.get("skipped"):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "stt_unavailable",
                "message": "Dictation is not available — type your note instead "
                           "(or use the Wispr Flow desktop app).",
                "reason": res.get("error"),
            },
        )
    return {"text": res.get("text", ""), "provider": res.get("provider")}


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


@router.post("/qa/approve-all")
async def qa_approve_all(reviewer: Dict[str, Any] = Depends(asc_auth.require_qa)):
    """Approve every submission currently held in QA in one step, moving them all
    to ``export_ready``. Lets a solo admin clear the QA backlog and export
    immediately. Each approval is logged with the reviewer for the audit trail."""
    store = _store()
    pending = store.list_submissions(status="needs_qa")
    approved = 0
    for sub in pending:
        asc_pipeline.apply_qa_decision(
            store, sub, decision="approve", reviewer_id=reviewer["id"],
            notes="bulk approve-all",
        )
        approved += 1
    return {"approved": approved}


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
    if body.portal_version is not None and body.portal_version not in PORTAL_VERSIONS:
        raise HTTPException(status_code=400, detail="Invalid portal_version")
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
            portal_version=body.portal_version,
            note=body.note,
            include_exported=body.include_exported,
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


# ─── Contributors view + tiered export (admin) ────────────────────────────────
# An admin-only view of every credentialed contributor, grouped by organization,
# with a two-tier export: "Export Data" (Tier A, buyer-facing) and "Further
# Credential Summary" (Tier B verification dossier, under NDA). The wall is
# enforced at export by the Tier B leak gate in ``export.build_export``.


def _credential_summaries_root():
    root = asc_export.export_root() / "credential-summaries"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _contributor_blurb(store: Any, id_hashed: str, contributor: Dict[str, Any],
                       cred: Optional[Dict[str, Any]]) -> str:
    if cred and (cred.get("blurb") or "").strip():
        return cred["blurb"].strip()
    ship = (cred or {}).get("ship") or {}
    return asc_credentials.generalized_blurb(
        ship, fallback_specialty=contributor.get("primary_specialty") or contributor.get("specialty")
    )


def _contributor_metrics(store: Any) -> List[Dict[str, Any]]:
    """Per-contributor metrics: directory facts + the throughput/grounded numbers
    from ``contributor_stats`` (keyed by user id)."""
    stats_by_uid = {s.get("evaluator_id"): s for s in store.contributor_stats()}
    rows: List[Dict[str, Any]] = []
    for c in store.contributor_directory():
        st = stats_by_uid.get(c["user_id"]) or {}
        rows.append(
            {
                **c,
                "avg_time_sec": st.get("avg_time_sec"),
                "total_hours": st.get("total_hours"),
                "premium_submissions": st.get("premium_submissions"),
                "premium_hours": st.get("premium_hours"),
                "grounded_submissions": st.get("grounded_submissions"),
                "credential": st.get("credential"),
            }
        )
    return rows


def _organization_metrics(store: Any) -> List[Dict[str, Any]]:
    contribs = _contributor_metrics(store)
    orgs: Dict[str, Dict[str, Any]] = {}
    for c in contribs:
        org = c.get("organization") or "Unaffiliated"
        agg = orgs.setdefault(
            org,
            {
                "organization": org,
                "contributor_count": 0,
                "verified_count": 0,
                "record_count": 0,
                "submission_count": 0,
                "grounded_submissions": 0,
                "total_hours": 0.0,
                "last_labeled_at": None,
            },
        )
        agg["contributor_count"] += 1
        agg["verified_count"] += 1 if c.get("credentials_verified") else 0
        agg["record_count"] += c.get("record_count") or 0
        agg["submission_count"] += c.get("submission_count") or 0
        agg["grounded_submissions"] += c.get("grounded_submissions") or 0
        agg["total_hours"] += c.get("total_hours") or 0.0
        ll = c.get("last_labeled_at")
        if ll and (agg["last_labeled_at"] is None or ll > agg["last_labeled_at"]):
            agg["last_labeled_at"] = ll
    for agg in orgs.values():
        agg["total_hours"] = round(agg["total_hours"], 2)
    return sorted(orgs.values(), key=lambda o: o["organization"].lower())


@router.get("/organizations")
async def list_organizations(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """All contributors grouped by organization (spec §3 — "listed by organization
    name, then I click into it")."""
    return {"organizations": _store().organization_directory()}


@router.get("/contributors")
async def list_contributors(
    organization: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Every contributor (optionally within one organization): internal display
    name, hashed id, primary specialty, # records labeled, verified status."""
    contributors = _store().contributor_directory()
    if organization:
        contributors = [c for c in contributors if (c["organization"] or "Unaffiliated") == organization]
    return {"contributors": contributors, "organization": organization}


@router.get("/contributors/{id_hashed}")
async def get_contributor(
    id_hashed: str,
    include_verify: bool = False,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """A contributor's profile: the generalized blurb + a credential summary. Tier
    B values are masked unless ``include_verify=true`` (admin edit path); the
    audited release path is the Further Credential Summary dossier."""
    store = _store()
    contributor = store.get_contributor(id_hashed)
    if not contributor:
        raise HTTPException(status_code=404, detail="Contributor not found")
    cred = store.get_contributor_credentials(id_hashed, include_verify=include_verify)
    ship = (cred or {}).get("ship") or {}
    verify = (cred or {}).get("verify") or {}
    blurb = _contributor_blurb(store, id_hashed, contributor, cred)
    credentials_block: Dict[str, Any] = {
        "organization": (cred or {}).get("organization") or contributor.get("organization"),
        "role_title": (cred or {}).get("role_title") or contributor.get("role_title"),
        "credentials_verified": bool((cred or {}).get("credentials_verified") or contributor.get("credentials_verified")),
        "ship": ship,
        "verify_encrypted": bool((cred or {}).get("verify_encrypted")),
        "verify_fields_on_file": sorted(verify.keys()) if include_verify else None,
        "has_verify_vault": bool(verify) if include_verify else (cred is not None),
    }
    if include_verify:
        credentials_block["verify"] = verify
    return {
        "contributor": contributor,
        "blurb": blurb,
        "credentials": credentials_block,
        "buttons": ["export_data", "further_credential_summary"],
    }


@router.put("/contributors/{id_hashed}")
async def upsert_contributor(
    id_hashed: str,
    body: ContributorCredentialsIn,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Create/update a contributor's credential profile (Tier A ship + Tier B
    vault). The contributor (user) must already exist."""
    store = _store()
    contributor = store.get_contributor(id_hashed)
    user = None
    # Resolve the owning user id from the directory (id_hashed -> user).
    for c in store.contributor_directory():
        if c["id_hashed"] == id_hashed:
            user = c
            break
    saved = store.upsert_contributor_credentials(
        id_hashed=id_hashed,
        user_id=(user or {}).get("user_id"),
        organization=body.organization,
        role_title=body.role_title,
        blurb=body.blurb,
        credentials_verified=body.credentials_verified,
        ship=body.ship,
        verify=body.verify,
    )
    store.log_event(
        entity_type="contributor", entity_id=id_hashed,
        event_type="credentials_updated", actor=admin["id"],
        payload={"organization": body.organization, "verified": body.credentials_verified},
    )
    return {
        "id_hashed": id_hashed,
        "organization": saved.get("organization"),
        "role_title": saved.get("role_title"),
        "credentials_verified": saved.get("credentials_verified"),
        "verify_encrypted": saved.get("verify_encrypted"),
    }


def _identifying_values(store: Any, id_hashed: str) -> List[str]:
    """All high-specificity identifying values for a contributor to scan exported
    records against — from BOTH the Tier B vault AND the onboarding-collected
    credential fields on the user row (full_name, npi, license). This guarantees a
    physician's real name / NPI / license can never appear in an Export Data batch,
    regardless of which store holds them."""
    values: List[str] = []
    cred = store.get_contributor_credentials(id_hashed, include_verify=True)
    if cred:
        values += asc_credentials.collect_verify_values([cred.get("verify") or {}])
    user = store.get_user_by_id_hashed(id_hashed)
    if user:
        onboarding = {}
        if user.get("full_name"):
            onboarding["full_legal_name"] = user["full_name"]
        if user.get("npi"):
            onboarding["npi"] = user["npi"]
        try:
            ucreds = json.loads(user.get("credentials_json") or "{}")
        except (TypeError, ValueError):
            ucreds = {}
        for k in ("medical_license_number", "license_number", "practice_address", "practice_contact"):
            if ucreds.get(k):
                onboarding[k if k != "license_number" else "medical_license_number"] = ucreds[k]
        values += asc_credentials.collect_verify_values([onboarding])
    return sorted(set(values))


@router.post("/contributors/{id_hashed}/export")
async def export_contributor_data(
    id_hashed: str,
    body: ScopedExportRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Button 1 — "Export Data": all export-ready records labeled by THIS
    contributor, Tier A only. The Tier B leak gate guards the batch."""
    store = _store()
    contributor = store.get_contributor(id_hashed)
    if not contributor:
        raise HTTPException(status_code=404, detail="Contributor not found")
    cred = store.get_contributor_credentials(id_hashed, include_verify=True)
    verify_values = _identifying_values(store, id_hashed)
    blurb = _contributor_blurb(store, id_hashed, contributor, cred)
    scope = {
        "type": "contributor",
        "label": contributor.get("display_name") or id_hashed,
        "id_hashed": id_hashed,
        "blurb": blurb,
    }
    return _build_scoped_export(
        store, admin, body, annotator_id_hashed=id_hashed,
        verify_values=verify_values, scope=scope,
    )


@router.post("/organizations/{organization}/export")
async def export_organization_data(
    organization: str,
    body: ScopedExportRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Export all Tier A data labeled by every contributor in an organization
    (spec §3 — "within the organization name: export all the data that
    organization labelled")."""
    store = _store()
    hashed_ids = store.hashed_ids_for_organization(organization)
    if not hashed_ids:
        raise HTTPException(status_code=404, detail="No contributors found for that organization")
    verify_values: List[str] = []
    for h in hashed_ids:
        verify_values += _identifying_values(store, h)
    scope = {
        "type": "organization",
        "label": organization,
        "contributor_count": len(hashed_ids),
    }
    return _build_scoped_export(
        store, admin, body, annotator_ids=hashed_ids,
        verify_values=verify_values, scope=scope,
    )


def _build_scoped_export(
    store: Any, admin: Dict[str, Any], body: ScopedExportRequest, *,
    annotator_id_hashed: Optional[str] = None,
    annotator_ids: Optional[List[str]] = None,
    verify_values: Optional[List[str]] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        manifest = asc_export.build_export(
            store,
            created_by=admin["id"],
            profile=body.profile,
            note=body.note,
            include_exported=body.include_exported,
            annotator_id_hashed=annotator_id_hashed,
            annotator_ids=annotator_ids,
            verify_values=verify_values,
            scope=scope,
        )
    except asc_export.ExportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except asc_profiles.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return manifest


@router.post("/contributors/{id_hashed}/credential-summary")
async def create_credential_summary(
    id_hashed: str,
    body: CredentialSummaryRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Button 2 — "Further Credential Summary": generate a verification dossier
    (PDF + JSON) containing Tier B + Tier A + verification handles, watermarked
    confidential, with the §9 notice prepended. Requires a click-through
    acknowledgment and is logged for audit (spec §6)."""
    if not body.acknowledged:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "acknowledgment_required",
                "message": "You must acknowledge the Non-Circumvention & Confidentiality "
                           "Notice before generating a credential verification summary.",
            },
        )
    store = _store()
    contributor = store.get_contributor(id_hashed)
    if not contributor:
        raise HTTPException(status_code=404, detail="Contributor not found")
    cred = store.get_contributor_credentials(id_hashed, include_verify=True)
    ship = (cred or {}).get("ship") or {}
    verify = (cred or {}).get("verify") or {}
    blurb = _contributor_blurb(store, id_hashed, contributor, cred)

    summary_id = "cvs-" + uuid.uuid4().hex[:12]
    generated_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    dossier = asc_credentials.build_dossier(
        id_hashed=id_hashed,
        organization=(cred or {}).get("organization") or contributor.get("organization"),
        role_title=(cred or {}).get("role_title") or contributor.get("role_title"),
        blurb=blurb,
        ship=ship,
        verify=verify,
        recipient=body.recipient,
        generated_by=admin.get("email"),
        generated_at=generated_at,
    )
    dossier["summary_id"] = summary_id

    out_dir = _credential_summaries_root() / summary_id
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (out_dir / "summary.json").write_text(json.dumps(dossier, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "summary.pdf").write_bytes(asc_credentials.render_dossier_pdf(dossier))

    # Audit: every generation is logged (timestamp, admin, intended recipient).
    store.log_event(
        entity_type="contributor", entity_id=id_hashed,
        event_type="credential_summary_generated", actor=admin["id"],
        payload={
            "summary_id": summary_id, "recipient": body.recipient,
            "generated_by": admin.get("email"), "generated_at": generated_at,
            "dir_path": str(out_dir),
        },
    )
    return {
        "summary_id": summary_id,
        "id_hashed": id_hashed,
        "recipient": body.recipient,
        "generated_at": generated_at,
        "blurb": blurb,
        "verification_handles": dossier.get("verification_handles"),
        "watermark": CREDENTIAL_SUMMARY_WATERMARK,
        "files": ["summary.json", "summary.pdf"],
        "downloads": {
            "json": f"/contributors/{id_hashed}/credential-summary/{summary_id}/download?format=json",
            "pdf": f"/contributors/{id_hashed}/credential-summary/{summary_id}/download?format=pdf",
        },
    }


@router.get("/contributors/{id_hashed}/credential-summaries")
async def list_credential_summaries(
    id_hashed: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """The audit trail of credential summaries generated for this contributor."""
    events = _store().list_events(entity_type="contributor", entity_id=id_hashed, limit=500)
    summaries = [
        {
            "summary_id": (e.get("payload") or {}).get("summary_id"),
            "recipient": (e.get("payload") or {}).get("recipient"),
            "generated_by": (e.get("payload") or {}).get("generated_by"),
            "generated_at": (e.get("payload") or {}).get("generated_at") or e.get("occurred_at"),
        }
        for e in events
        if e.get("event_type") == "credential_summary_generated"
    ]
    return {"summaries": summaries}


@router.get("/contributors/{id_hashed}/credential-summary/{summary_id}/download")
async def download_credential_summary(
    id_hashed: str,
    summary_id: str,
    format: str = "pdf",
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    fmt = (format or "pdf").lower()
    if fmt not in ("pdf", "json"):
        raise HTTPException(status_code=400, detail="format must be 'pdf' or 'json'")
    out_dir = _credential_summaries_root() / summary_id
    meta_path = out_dir / "summary.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Credential summary not found")
    # Validate the summary belongs to the contributor named in the path, so a
    # mismatched URL can never serve another contributor's dossier.
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    if meta.get("hashed_annotator_id") != id_hashed:
        raise HTTPException(status_code=404, detail="Credential summary not found")
    fname = "summary.pdf" if fmt == "pdf" else "summary.json"
    fpath = out_dir / fname
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Credential summary not found")
    data = fpath.read_bytes()
    media = "application/pdf" if fmt == "pdf" else "application/json"
    download_name = f"credential-summary-{id_hashed}-{summary_id}.{fmt}"
    headers = {"Content-Disposition": f'attachment; filename="{download_name}"'}
    return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)


# ─── Per-organization / per-contributor metrics (admin) ───────────────────────
@router.get("/metrics/organizations")
async def metrics_organizations(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    return {"organizations": _organization_metrics(_store())}


@router.get("/metrics/contributors")
async def metrics_contributors(
    organization: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    rows = _contributor_metrics(_store())
    if organization:
        rows = [r for r in rows if (r.get("organization") or "Unaffiliated") == organization]
    return {"contributors": rows, "organization": organization}


@router.get("/metrics/value-per-time")
async def metrics_value_per_time(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Value-per-clinician-minute — the north-star metric (Value-per-Minute PRD
    A4). Median REALIZED and PROJECTED V/T, split by product version (v1 vs v2),
    difficulty, grounded vs plain, Mode A vs B, and per contributor.

    The team is held to REALIZED V/T ≥ the target; projected (× reuse) is the
    fuller economics but a forecast. Reported next to κ + the assist override
    rate so a rising ratio with falling quality reads as the regression it is."""
    store = _store()
    vpt = store.value_per_time_stats()
    target = value_per_minute_target()
    vpt["target"] = target
    overall = (vpt.get("overall") or {}).get("realized_vpm")
    return {
        "value_per_time": vpt,
        "target_realized_vpm": target,
        "meets_target": (overall is not None and overall >= target),
        # Quality gate context (Part D): a high V/T is only real if κ holds and the
        # clinician is not rubber-stamping the model's suggestions.
        "kappa": asc_agreement.aggregate_kappa(store.list_agreement_observations()),
        "override_rate": store.override_rate_stats(portal_version="v2"),
    }


@router.get("/credential-policy")
async def credential_policy(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """The tiering policy + the §9 notice text, for the UI (ack modal, tier hints)."""
    from asclepius.constants import TIER_A_SHIP_FIELDS, TIER_B_VERIFY_FIELDS

    return {
        "company": _company_name(),
        "tier_a_ship_fields": list(TIER_A_SHIP_FIELDS),
        "tier_b_verify_fields": list(TIER_B_VERIFY_FIELDS),
        "watermark": CREDENTIAL_SUMMARY_WATERMARK,
        "non_circumvention_notice": _non_circumvention_notice(),
        "legal_disclaimer": CREDENTIAL_SUMMARY_LEGAL_DISCLAIMER,
    }


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
    if body.independent_mode not in INDEPENDENT_MODES:
        raise HTTPException(status_code=400, detail="Invalid independent_mode")
    constraints = {
        "specialty": body.specialty,
        "difficulty": body.difficulty,
        "capture_reasoning": body.capture_reasoning,
        "grounding_mode": body.grounding_mode,
        "independent_mode": body.independent_mode,
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
    independent_mode = c.get("independent_mode") or DEFAULT_INDEPENDENT_MODE
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
            independent_mode=t.get("independent_mode") or independent_mode,
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
                independent_mode=independent_mode,
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
    # Value-per-Minute (PRD Part A): a compact V/T summary on the same call the
    # admin Metrics tile already makes, so the north-star ratio sits next to κ.
    # The full breakdown (by difficulty/grounded/mode/contributor) is on
    # GET /metrics/value-per-time.
    vpt = store.value_per_time_stats()
    vpt["target"] = value_per_minute_target()
    return {
        "status_counts": store.status_counts(),
        # V1 (classic) vs V2 (assisted) provenance breakdown (Asclepius V2).
        "portal_version_counts": store.portal_version_counts(),
        "value_per_time": vpt,
        "value_per_time_target": value_per_minute_target(),
        # Rubber-stamp guard: model-assist override rate on the v2 assisted flow.
        "override_rate": store.override_rate_stats(portal_version="v2"),
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
        # Records packaged + QA-cleared but not yet shipped — the "ready to export"
        # backlog the admin can one-click package.
        "exportable_records": len(store.list_records(status="export_ready")),
        # Already-shipped records (re-downloadable) and the grand total — lets the
        # UI explain a 0 backlog: "already exported" vs "no records yet".
        "exported_records": len(store.list_records(status="exported")),
        "total_records": len(store.list_records()),
        # Submissions held in QA review (sampled / flagged). These are NOT yet in
        # the export pool — the admin must approve them first.
        "qa_pending": len(store.list_submissions(status="needs_qa")),
    }


@router.get("/events")
async def events(
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    limit: int = 200,
    _qa: Dict[str, Any] = Depends(asc_auth.require_qa),
):
    return {"events": _store().list_events(entity_type=entity_type, entity_id=entity_id, limit=limit)}
