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
import hashlib
import io
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
# Pillow decode + re-encode + a synchronous disk write. On the event loop that
# stalls every other request in the process, not just this one.
from starlette.concurrency import run_in_threadpool

from asclepius import agreement as asc_agreement
from asclepius import auth as asc_auth
from asclepius import auto_generate as asc_auto_generate
from asclepius import passwords as asc_passwords
from asclepius import capabilities as asc_caps
from asclepius import cases as asc_cases
from asclepius import citations as asc_citations
from asclepius import corpus as asc_corpus
from asclepius import assets as asc_assets
from asclepius import avatar as asc_avatar
from asclepius import contributor_score as asc_contributor_score
from asclepius import credentials as asc_credentials
from asclepius import export as asc_export
from asclepius import generation as asc_generation
from asclepius import pipeline as asc_pipeline
from asclepius import profiles as asc_profiles
from asclepius import specialties as asc_specialties
from asclepius import store as asc_store
from asclepius import task_notify as asc_task_notify
# Pure policy module (stdlib only) — safe at module scope, no cycle.
from asclepius import route_notify as asc_route_notify
from asclepius import trajectory as asc_trajectory
from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_ENGINE,
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
    FAILURE_MODES,
    GROUNDED_PREMIUM_DISCLAIMER,
    CASE_INCOHERENT_TASK_STATUS,
    GROUNDING_MODES,
    INDEPENDENT_MODES,
    NOT_HARD_TASK_STATUS,
    PORTAL_VERSIONS,
    SINGLE_TURN_PORTAL_VERSIONS,
    DEFAULT_PORTAL_VERSION,
    ENV_PORTAL_VERSION,
    LONGITUDINAL_PORTAL_VERSION,
    PREFERENCE_VARIANTS,
    REAL_CASE_PORTAL_VERSION,
    SYNTHETIC_PORTAL_VERSIONS,
    PROMPT_FLAGGED_TASK_STATUS,
    PROMPT_REVIEW_VERDICTS,
    REASONING_STEP_LABELS,
    RUBRIC_AXES,
    RUBRIC_TIERS,
    RUBRIC_TIER_BANDS,
    STEP_CORRECTION_REASONS,
    ASSISTED_PORTAL_VERSIONS,
    TASK_SOURCES,
    VALUE_TIERS,
    VERDICTS,
    WHY_BETTER_TAGS,
    assist_min_confidence,
    fallback_window,
    independent_capture_kind,
    max_fallback_rate,
    normalize_portal_version,
    target_pool_size,
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
    hard_only_generation,
    v4_open_to_all_specialties,
    v3_multimodal_only,
    relax_multimodal_gates,
    ab_source,
    non_circumvention_notice as _non_circumvention_notice,
    require_measured_difficulty,
    measure_empirical_difficulty_enabled,
    min_empirical_difficulty,
)
from asclepius.schemas import (
    AscPortalHandoffConsumeRequest,
    ChangePasswordRequest,
    BatchFromRequest,
    BuyerIn,
    BuyerRequestIn,
    BuyerRequestStatusUpdate,
    CandidateGenRequest,
    CiteRequest,
    FirstRunUpdate,
    FIRST_RUN_STOPS as _FIRST_RUN_STOPS,
    ForgotPasswordRequest,
    ContributorCredentialsIn,
    CreateUserRequest,
    GenerateRealCasesRequest,
    PromoteCaseRequest,
    ResetPasswordRequest,
    ReviewClearRequest,
    UploadPromoteRequest,
    QuarantineOverrideRequest,
    RealDataApprovalRequest,
    UploadLinkRequest,
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
    TaskIn,
    PasswordChange,
    ProfileUpdate,
    TaskUploadRequest,
    TrajectorySelfScore,
    TutorialStateUpdate,
)
from asclepius.tutorial_case import (
    TUTORIAL_TASK_ID,
    TUTORIAL_VERSION,
    grade_tutorial_submission,
    tutorial_raw_task,
)
from asclepius.v4_cases import V4_DEFAULT_MAX_LABELS
from asclepius.store import get_store, verify_password as _verify_password, _utcnow_iso
from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import (
    build_asclepius_password_changed_email,
    build_asclepius_password_reset_email,
)
from ratelimit import rate_limiter
from asclepius.validation import compute_dedupe_hash, grounding_status, is_grounded, residual_identifiers

log = logging.getLogger("asclepius.router")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius"])


def _store():
    return get_store()


def _ab_fallback_health(store) -> Dict[str, Any]:
    """Two-frontier fallback health for /stats (PRD §A3 Rung 3): the rolling
    legacy-fallback rate, the ceiling, and a RED alert flag when the rate exceeds it
    (a provider is likely down and new pairs are being held as ``needs_baseline``)."""
    ceiling = max_fallback_rate()
    rate = store.ab_fallback_rate(window=fallback_window())
    return {"rate": rate, "ceiling": ceiling, "alert": rate is not None and rate > ceiling}


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
        # Multimodal (Synthetic Multimodal Cases PRD): the case panel renders the
        # PUBLIC case only — ground_truth/hard_hook/reasoning_divergence are the
        # answer key and are stripped here, exactly like generator_model. The
        # rendered case is already in ``prompt`` regardless.
        "modality": task.get("modality") or "text",
        "case": asc_cases.public_case(task.get("case")),
        # Empirical difficulty (Specialty Hyper-Personalization PRD §9): the
        # frontier-model failure rate + whether it was LIVE-measured. Not an answer
        # key — surfaced so admin/QA + the buyer export can see the difficulty.
        "empirical_difficulty": task.get("empirical_difficulty"),
        "difficulty_measured": bool(task.get("difficulty_measured")),
        # Longitudinal trajectory (PRD 2 §3.5): which chart walk this point belongs
        # to and where it sits in it, so the workspace can say "step 3 of 13 on one
        # patient" instead of showing three unexplained cases. Metadata only — this
        # whitelist is what an evaluator receives, and the sequence GATE is enforced
        # in SQL and again on the by-ID path, never from these two fields.
        "trajectory_id": task.get("trajectory_id"),
        "sequence_index": task.get("sequence_index"),
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
async def get_taxonomy(_user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE))):
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
        "rubric_axes": list(RUBRIC_AXES),
        # Tiered rubric (Two-Model PRD WS-B): tiers + their |points| bands so the
        # V3/V4 tier picker labels each band consistently with the backend.
        "rubric_tiers": list(RUBRIC_TIERS),
        "rubric_tier_bands": [{"tier": t, "min": lo, "max": hi} for (t, lo, hi) in RUBRIC_TIER_BANDS],
        # Model-Failure Taxonomy (PRD §D): the controlled failure-mode vocabulary +
        # definitions for the V3/V4 capture chips.
        "failure_modes": [{"id": mid, "label": label, "definition": definition}
                          for (mid, label, definition) in FAILURE_MODES],
        "independent_modes": list(INDEPENDENT_MODES),
        "portal_versions": list(SINGLE_TURN_PORTAL_VERSIONS),
        "value_tiers": list(VALUE_TIERS),
        "preference_variants": list(PREFERENCE_VARIANTS),
        "export_profiles": asc_profiles.list_profiles(),
    }


# ─── Auth ─────────────────────────────────────────────────────────────────────
@router.post("/auth/login", dependencies=[Depends(rate_limiter("asclepius_login", 10, 60))])
async def login(body: LoginRequest):
    store = _store()
    user = asc_auth.authenticate(store, body.email, body.password)
    if not user:
        # Onboarding v2 §2: a physician who submitted an application has an
        # account row with NO password — the v2 wizard has no password step, and
        # credentials are minted and mailed on approval (§4.4, §5). Telling them
        # "invalid email or password" would be false in both halves and would
        # send them to the reset flow, which cannot help: there is nothing to
        # reset. Answer with the state they are actually in.
        #
        # This is not an enumeration oracle worth worrying about, and the reason
        # is structural rather than a judgment call: the only accounts it can
        # distinguish are ones that HAVE NO CREDENTIAL. There is nothing to
        # guess, nothing to brute-force, and nothing an attacker can do with the
        # answer that they could not do by submitting an application to the same
        # address. Every account that has a password still gets the same generic
        # 401 it always did.
        pending = store.get_user_by_email((body.email or "").strip().lower())
        if pending and asc_store.password_is_unset(pending) \
                and (pending.get("verification_status") or "pending") == "pending":
            raise HTTPException(
                status_code=403,
                detail=("Your application is in review — we'll email you within "
                        "24–48 hours."),
                headers={asc_auth.AUTH_GATE_HEADER: "pending"},
            )
        raise HTTPException(status_code=401, detail="Invalid email or password")
    store.log_event(entity_type="user", entity_id=user["id"], event_type="login", actor=user["id"])
    return {"token": asc_auth.create_token(user), "user": asc_auth.public_user(user)}


# ─── Password: forgot, reset, change ─────────────────────────────────────────
# The forgot endpoint answers identically whether or not the address has an
# account. That is not politeness: an endpoint that says "no such user" is an
# account-enumeration oracle, and this plane already takes care not to be one
# (both auth planes return the same generic 401, which is why SignInDialog
# swallows the Asclepius attempt rather than reporting which plane matched).
#
# Uniform BODY is only half of it. The send is queued on BackgroundTasks and
# never awaited inline, so the found and not-found branches do not differ in
# latency either. A timing difference is an oracle just the same.

_FORGOT_ANSWER = {
    "ok": True,
    "message": (
        "If that email has an Asclepius account, we've sent a reset link. "
        "It expires in 60 minutes."
    ),
}


async def _mail_password_reset(email: str, raw_token: str) -> None:
    if not is_email_transport_configured():
        return
    try:
        await send_html_email(
            email,
            "Reset your Archangel Health password",
            build_asclepius_password_reset_email(
                email=email,
                reset_url=asc_passwords.reset_url(raw_token),
                expires_minutes=asc_passwords.RESET_TTL_MINUTES,
            ),
        )
    except Exception:
        log.exception("[asclepius] password reset email failed")


async def _mail_password_changed(email: str) -> None:
    if not is_email_transport_configured():
        return
    try:
        await send_html_email(
            email,
            "Your Archangel Health password was changed",
            build_asclepius_password_changed_email(email=email),
        )
    except Exception:
        log.exception("[asclepius] password-changed notice failed")


@router.post(
    "/auth/password/forgot",
    dependencies=[Depends(rate_limiter("asclepius_pw_forgot", 5, 900))],
)
async def forgot_password(
    body: ForgotPasswordRequest,
    background: BackgroundTasks,
    request: Request,
):
    store = _store()
    email = (body.email or "").strip().lower()
    user = store.get_user_by_email(email) if email else None

    if user and user.get("active"):
        # A rejected account still gets a working link. Refusing here would be
        # the loudest oracle in the set, and the reset grants them nothing that
        # the verification gate does not already refuse.
        if store.count_live_password_resets(user["id"]) < asc_passwords.MAX_LIVE_RESETS:
            raw, hashed = asc_passwords.new_reset_token()
            store.create_password_reset(
                user_id=user["id"],
                token_hash=hashed,
                expires_at=asc_passwords.reset_expires_at(),
                requested_ip=(request.client.host if request.client else None),
            )
            store.log_event(
                entity_type="user",
                entity_id=user["id"],
                event_type="password_reset_requested",
                actor=user["id"],
            )
            background.add_task(_mail_password_reset, user["email"], raw)
    else:
        # Recorded WITHOUT an entity_id, so the provenance log never implies an
        # account exists for an address that has none.
        store.log_event(
            entity_type="user",
            entity_id=None,
            event_type="password_reset_requested_unknown",
            actor=None,
            payload={"domain": email.split("@")[-1] if "@" in email else ""},
        )
    return _FORGOT_ANSWER


@router.post(
    "/auth/password/reset",
    dependencies=[Depends(rate_limiter("asclepius_pw_reset", 10, 900))],
)
async def reset_password(body: ResetPasswordRequest, background: BackgroundTasks):
    store = _store()
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="This reset link is no longer valid. Request a new one.")

    row = store.get_password_reset_by_token_hash(asc_passwords.hash_reset_token(token))
    user = store.get_user_by_id(row["user_id"]) if row else None

    # Validate the password BEFORE consuming, so a rejected password does not
    # burn the single use and strand someone with a dead link.
    try:
        asc_passwords.validate(body.new_password, email=(user or {}).get("email", ""))
    except asc_passwords.PasswordRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Expired, already used, superseded and never-existed all collapse to one
    # sentence: the holder of a bad link learns nothing about which it was.
    if not row or not user or not store.consume_password_reset(row["id"]):
        raise HTTPException(status_code=400, detail="This reset link is no longer valid. Request a new one.")

    store.set_user_password(user["id"], body.new_password)
    store.invalidate_password_resets_for_user(user["id"])
    store.log_event(
        entity_type="user",
        entity_id=user["id"],
        event_type="password_reset_completed",
        actor=user["id"],
    )
    background.add_task(_mail_password_changed, user["email"])

    # Sign them in. They just proved mailbox control and typed the password
    # eight seconds ago; making them retype it is how a recovery flow gets
    # abandoned halfway.
    fresh = store.get_user_by_id(user["id"])
    return {"ok": True, "token": asc_auth.create_token(fresh), "user": asc_auth.public_user(fresh)}


@router.post(
    "/auth/password/change",
    dependencies=[Depends(rate_limiter("asclepius_pw_change", 10, 300))],
)
async def change_password(
    body: ChangePasswordRequest,
    background: BackgroundTasks,
    # get_current_user_optional, not get_current_user: a physician still
    # awaiting verification must always be able to change their own password.
    user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional),
):
    if user is None:
        raise HTTPException(status_code=401, detail="Asclepius authentication required")
    store = _store()
    if not _verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="That is not your current password.")
    try:
        asc_passwords.validate(body.new_password, email=user.get("email", ""))
    except asc_passwords.PasswordRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store.set_user_password(user["id"], body.new_password)
    store.invalidate_password_resets_for_user(user["id"])
    store.log_event(
        entity_type="user",
        entity_id=user["id"],
        event_type="password_changed",
        actor=user["id"],
    )
    background.add_task(_mail_password_changed, user["email"])
    # The write just invalidated the caller's own token, so hand back a fresh one.
    fresh = store.get_user_by_id(user["id"])
    return {"ok": True, "token": asc_auth.create_token(fresh)}


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
            # BUG-6: stamp a stable organization at provisioning (email local-part
            # fallback) so an SSO-provisioned clinician groups immediately in
            # Exports/Metrics instead of sitting in the (unassigned) bucket until
            # the next boot-time backfill.
            organization=(email.split("@", 1)[0] if "@" in email else email),
        )
        # FIX-B F5: new SSO accounts land pending like every other signup —
        # otherwise a clinician arriving through this bridge gets an evaluator
        # seat, never appears in the verification queue, and draws tasks with
        # zero credentialing. NULL still means "pre-verification-era account"
        # for rows that predate the migration (auth.py passes those through);
        # it must not also mean "arrived through a side door yesterday".
        store.set_verification_status(user["id"], "pending")
        user = store.get_user_by_id(user["id"]) or user
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


# ─── Landing → Asclepius portal handoff ──────────────────────────────────────
# Mirrors main.py's doctor-portal handoff (POST /api/auth/portal-handoff /
# .../consume), but scoped to this plane: an Asclepius token is signed with its
# own secret and decoded by asclepius.auth.decode_token, so the landing/tenant
# handoff (which depends on get_staff_context_optional) cannot resolve one.
# Rather than putting the raw JWT in a URL query param (browser history, server
# access logs, Referer headers), the landing SPA trades the token for a
# short-lived, single-use, server-held code here, and the Asclepius frontend
# consumes it on load (see asclepius.js consumeHandoffFromUrl).
_ASC_HANDOFF_TTL_SECONDS = 60
_ASC_HANDOFF_STORE: Dict[str, Dict[str, Any]] = {}


def _cleanup_asc_handoffs(now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    expired = [k for k, v in _ASC_HANDOFF_STORE.items() if v.get("expires_at") and v["expires_at"] <= now]
    for key in expired:
        _ASC_HANDOFF_STORE.pop(key, None)


@router.post("/auth/portal-handoff")
async def create_asclepius_portal_handoff(
    authorization: Optional[str] = Header(None),
    # get_current_user_optional, NOT get_current_user: minting a handoff must
    # not itself enforce the verification gate. /auth/login already hands a
    # pending physician a valid token unconditionally (the gate only bites on
    # subsequent calls) — this endpoint must not be stricter than login itself,
    # or a pending physician signing in correctly from the landing page would
    # be blocked one hop earlier than signing in directly on /asclepius.
    user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional),
):
    if user is None or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Asclepius authentication required")
    _cleanup_asc_handoffs()
    token = authorization.removeprefix("Bearer ").strip()
    code = secrets.token_urlsafe(24)
    _ASC_HANDOFF_STORE[code] = {
        "token": token,
        "expires_at": datetime.utcnow() + timedelta(seconds=_ASC_HANDOFF_TTL_SECONDS),
    }
    return {"handoff_code": code, "expires_in_seconds": _ASC_HANDOFF_TTL_SECONDS}


@router.post("/auth/portal-handoff/consume")
async def consume_asclepius_portal_handoff(body: AscPortalHandoffConsumeRequest):
    _cleanup_asc_handoffs()
    code = (body.handoff_code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="handoff_code is required")
    entry = _ASC_HANDOFF_STORE.pop(code, None)
    if not entry:
        raise HTTPException(status_code=404, detail="Handoff code not found or expired")
    return {"token": entry["token"]}


@router.get("/auth/me")
async def me(user: Dict[str, Any] = Depends(asc_auth.get_current_account)):
    return asc_auth.public_user(user)


@router.get("/me/profile")
async def my_profile(user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE))):
    """Everything the portal shows a physician about their own account.

    Until now the only self-scoped write on this whole API was the tutorial
    state, and there was no way for a doctor to see what we hold about them,
    let alone correct a phone number. The credential fields come back
    ``editable: false`` rather than being withheld: someone should be able to
    read what they submitted and see plainly which parts are settled.
    """
    store = _store()
    row = store.get_user_by_id(user["id"]) or {}
    creds = {}
    try:
        creds = json.loads(row.get("credentials_json") or "{}")
    except (TypeError, ValueError):
        creds = {}

    country = (row.get("country_of_licensure") or "").upper()
    registry_name = None
    if country and country != "US":
        from asclepius.registry import config as registry_config

        registry_name = registry_config.for_country(country).registry_name

    score = None
    try:
        score = store.get_contributor_score(user["id"])
    except Exception:
        score = None

    return {
        "editable": {
            "full_name": row.get("full_name"),
            "phone": row.get("phone"),
            "linkedin_url": row.get("linkedin_url"),
            "specialty_niche": row.get("specialty_niche"),
        },
        # Read-only: checked against a registry, or attested to, or decided by
        # a person. Correcting one of these is a conversation, not a form.
        "credentials": {
            "email": row.get("email"),
            "specialty": row.get("specialty"),
            "board_cert": row.get("board_cert"),
            "years_experience": row.get("years_experience"),
            "organization": row.get("organization") or row.get("org_name"),
            "country_of_practice": row.get("country_of_practice"),
            "country_of_licensure": row.get("country_of_licensure"),
            "registry_name": registry_name,
            "registration_number": row.get("registry_id"),
            "npi": row.get("npi"),
            "qualification": creds.get("qualification") or creds.get("degree"),
            "signed_initials": (json.loads(row.get("attestations_json") or "{}") or {}).get(
                "signedInitials") if row.get("attestations_json") else None,
        },
        "standing": {
            "verification_status": row.get("verification_status"),
            "tier": row.get("tier"),
            "tier_word": asc_caps.tier_word(row.get("tier")),
            "score": (score or {}).get("score"),
            # Derived, not read off the row: contributor_scores has no `band`
            # column, so `(score or {}).get("band")` was None for every
            # physician who ever loaded this page, and the profile rendered a
            # bare number with nothing saying what it meant.
            "band": asc_contributor_score.band_word((score or {}).get("score"))
                    if (score or {}).get("score") is not None else None,
            "referral_code": row.get("referral_code"),
        },
        # Absent until they upload one. The initials and the accent come from
        # the community's own helpers rather than a second implementation: the
        # two letters a physician sees on their own profile and the two their
        # colleagues see beside their messages have to be the same two letters.
        "avatar": _avatar_block(row),
    }


def _avatar_block(row: Dict[str, Any]) -> Dict[str, Any]:
    from community.router import _initials, specialty_accent
    sha = (row.get("avatar_asset_sha") or "").strip()
    return {
        # Cache-busted on the sha, so replacing a picture is visible
        # immediately rather than after an hour of `private, max-age=3600`.
        "url": (f"/api/asclepius/users/{row['id']}/avatar?v={sha[:12]}") if sha else None,
        "initials": _initials(row.get("full_name") or row.get("email") or ""),
        "accent": specialty_accent(row.get("specialty")),
        "updated_at": row.get("avatar_updated_at"),
    }


@router.patch("/me/profile")
async def update_my_profile(
    body: ProfileUpdate,
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """Correct the handful of fields that are the physician's own to correct."""
    store = _store()
    store.update_own_profile(
        user["id"],
        full_name=body.full_name,
        phone=body.phone,
        linkedin_url=body.linkedin_url,
        specialty_niche=body.specialty_niche,
    )
    store.log_event(
        entity_type="user", entity_id=user["id"], event_type="profile_self_updated",
        actor=user.get("email"),
        payload={"fields": [k for k, v in body.model_dump().items() if v is not None]},
    )
    return await my_profile(user=store.get_user_by_id(user["id"]) or user)


@router.post("/me/password")
async def change_my_password(
    body: PasswordChange,
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """Change a password from inside the product.

    The only route to this was the signed-out "forgot password" flow, which
    means a doctor who simply wanted a new password had to pretend they had
    lost the old one and go and find an email. The current password is
    required: a session left open on a ward computer should not be enough to
    take the account.
    """
    store = _store()
    row = store.get_user_by_id(user["id"]) or {}
    if not _verify_password(body.current_password, row.get("password_hash") or ""):
        raise HTTPException(status_code=403, detail="That is not your current password.")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Use at least 8 characters.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="That is the password you already have.")
    store.set_user_password(user["id"], body.new_password)
    store.log_event(
        entity_type="user", entity_id=user["id"], event_type="password_self_changed",
        actor=user.get("email"), payload={},
    )
    return {"ok": True}


# ─── Profile picture ──────────────────────────────────────────────────────────
async def _read_avatar_upload(file: UploadFile, request: Request) -> bytes:
    """Read with a RUNNING cap, the same shape as onboarding's ``_read_capped``.

    ``await file.read()`` buffers the whole body before anything can check its
    size, so an arbitrarily large upload is resident in memory before it can be
    refused -- cheap memory pressure against a single-worker process. Reject on
    a declared Content-Length first, then stream and abort the moment the cap is
    passed rather than trusting the declaration.
    """
    cap = asc_avatar.avatar_max_bytes()
    mb = cap // (1024 * 1024)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + 8192:
        raise HTTPException(status_code=413,
                            detail=f"That image is too large. Keep it under {mb} MB.")
    chunks, total = [], 0
    while True:
        chunk = await file.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(status_code=413,
                                detail=f"That image is too large. Keep it under {mb} MB.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/me/avatar",
    dependencies=[Depends(rate_limiter("asclepius_avatar", 10, 600))],
)
async def upload_my_avatar(
    request: Request,
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """A physician's own headshot.

    On BROWSE like the rest of ``/me/*``, so somebody still awaiting
    verification can set one -- they are in the community from the day they
    sign up, and appearing there as two grey letters for a week is a poor
    welcome.
    """
    data = await _read_avatar_upload(file, request)
    try:
        # The declared Content-Type is not passed in. It is attacker-controlled
        # and the stored blob is served inline from this origin; the bytes
        # decide. See asclepius/avatar.py.
        sha, mime = await run_in_threadpool(asc_avatar.store, data)
    except asc_avatar.AvatarRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("[avatar] could not store the upload")
        raise HTTPException(
            status_code=503,
            detail="We could not save that just now. Try again in a moment.",
        ) from exc
    store = _store()
    store.set_user_avatar(user["id"], sha256=sha, mime=mime,
                          at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z")
    store.log_event(entity_type="user", entity_id=user["id"],
                    event_type="avatar_set", actor=user.get("email"),
                    payload={"bytes": len(data)})
    return {"ok": True, "avatar": _avatar_block(store.get_user_by_id(user["id"]) or {})}


@router.delete("/me/avatar")
async def delete_my_avatar(
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    store = _store()
    store.set_user_avatar(user["id"], sha256=None, mime=None, at="")
    store.log_event(entity_type="user", entity_id=user["id"],
                    event_type="avatar_cleared", actor=user.get("email"), payload={})
    return {"ok": True, "avatar": _avatar_block(store.get_user_by_id(user["id"]) or {})}


@router.get("/users/{user_id}/avatar")
async def get_user_avatar(
    user_id: str,
    viewer: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """Serve a physician's picture to any signed-in member of the product.

    Not self-scoped: the point of a profile picture is that colleagues see it
    beside a message and an admin sees it while checking a registry entry. It
    is gated on being signed in at all, which is the same bar the display name
    it sits next to already clears.
    """
    store = _store()
    row = store.get_user_by_id(user_id) or {}
    sha = (row.get("avatar_asset_sha") or "").strip()
    if not sha:
        raise HTTPException(status_code=404, detail="No picture on file.")
    try:
        data, _mime = asc_assets.load_asset(sha)
    except asc_assets.AssetError:
        # The blob is gone -- almost certainly an ephemeral asset store wiped by
        # a redeploy. A 404 lets the client fall back to initials, which is the
        # right outcome: cosmetic, recoverable, and not worth a 500.
        raise HTTPException(status_code=404, detail="No picture on file.")
    return Response(
        content=data,
        media_type=row.get("avatar_mime") or "image/png",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/me/tutorial")
async def update_my_tutorial(
    body: TutorialStateUpdate, user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.TUTORIAL))
):
    """One transition on the caller's own first-run tutorial state.

    Server-authoritative: the "the tutorial never re-triggers once finished"
    invariant lives in THESE rules, not in client flags. ``start`` after
    completed/skipped is a deliberate no-op (replay is a client-side affair and
    must never clear completion); ``reset`` is allowed self-service because the
    tutorial writes no real data.
    """
    store = _store()
    current = store.get_tutorial_state(user["id"])
    status = current.get("status") or "not_started"
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    action = body.action

    if action == "start":
        if status in ("completed", "skipped"):
            return asc_auth.public_user(store.get_user_by_id(user["id"]))
        if status == "not_started":
            current = {
                "status": "in_progress",
                "step": body.step,
                "version": body.version or TUTORIAL_VERSION,
                "started_at": now,
                "completed_at": None,
                "skipped_at": None,
            }
        # already in_progress: keep position, refresh nothing
    elif action == "advance":
        if status in ("completed", "skipped"):
            return asc_auth.public_user(store.get_user_by_id(user["id"]))
        if status == "not_started":
            current = {"status": "in_progress", "version": body.version or TUTORIAL_VERSION,
                       "started_at": now, "completed_at": None, "skipped_at": None}
        current["step"] = body.step
    elif action == "skip":
        if status != "completed" and not current.get("skipped_at"):
            current["status"] = "skipped"
            current["skipped_at"] = now
            if not current.get("version"):
                current["version"] = body.version or TUTORIAL_VERSION
    elif action == "complete":
        if not current.get("completed_at"):
            current["status"] = "completed"
            current["completed_at"] = now
            current["skipped_at"] = None
            if not current.get("version"):
                current["version"] = body.version or TUTORIAL_VERSION
            current.pop("step", None)
    elif action == "reset":
        current = {"status": "not_started", "version": None}

    store.set_tutorial_state(user["id"], current)
    store.log_event(
        entity_type="user", entity_id=user["id"],
        event_type="tutorial_" + action, actor=user["id"],
        payload={"step": body.step} if body.step else None,
    )
    # Onboarding v2 §6 stop 3: the practice case IS a walkthrough stop, and it is
    # checked off HERE — from the tutorial's own transition — rather than by a
    # second tracker the client would have to remember to call. The PRD is
    # explicit about this ("hook the existing tutorial-complete server event; do
    # not add a parallel tracker"), and the reason is that two writers of one
    # fact drift: a doctor who finishes the practice case from the help menu, or
    # on another device, or after a reload, must still find the box ticked.
    #
    # A skip closes the stop too. §6 says a skip never nags again, and leaving
    # the box open after a deliberate skip is exactly nagging.
    if action in ("complete", "skip"):
        _close_first_run_stop(store, user["id"], "practice",
                              "done" if action == "complete" else "skipped")
    return asc_auth.public_user(store.get_user_by_id(user["id"]))


# ─── First-login walkthrough (Onboarding v2 §6) ──────────────────────────────
def _close_first_run_stop(store: Any, user_id: str, stop: str, outcome: str) -> None:
    """Mark one stop closed, and the whole checklist complete once all six are.

    Idempotent and monotonic: a stop that is already closed keeps its FIRST
    outcome, so replaying the practice case after skipping it does not rewrite
    history, and a stop can never reopen. That is what makes "skip for now never
    nags again" true rather than aspirational.
    """
    state = store.get_first_run(user_id)
    stops = dict(state.get("stops") or {})
    if stops.get(stop):
        return
    stops[stop] = outcome
    state["stops"] = stops
    if not state.get("completed_at") and all(stops.get(s) for s in _FIRST_RUN_STOPS):
        state["completed_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    store.set_first_run(user_id, state)


@router.patch("/me/first-run")
async def update_my_first_run(
    body: FirstRunUpdate,
    # get_current_account, not require_full_access: the walkthrough is the FIRST
    # thing a newly approved physician sees, and on some deployments they reach
    # it while still provisional. A checklist that 403s is a checklist nobody
    # can finish.
    user: Dict[str, Any] = Depends(asc_auth.get_current_account),
):
    """One transition on the caller's own first-login walkthrough.

    Server-authoritative and server-STORED (§6): doctors switch devices, and a
    checklist in localStorage restarts on the phone. The rules that make a stop
    permanent live here, not in client flags.
    """
    store = _store()
    if body.action in ("done", "skip"):
        if not body.stop:
            raise HTTPException(status_code=400, detail="Which stop?")
        _close_first_run_stop(store, user["id"], body.stop,
                              "done" if body.action == "done" else "skipped")
    elif body.action == "dismiss":
        state = store.get_first_run(user["id"])
        if not state.get("dismissed_at"):
            state["dismissed_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            store.set_first_run(user["id"], state)
    elif body.action == "reset":
        # Self-service, for the same reason the tutorial's reset is: the
        # walkthrough writes no real data, so replaying it costs nothing and
        # refusing would just mean asking an admin for something harmless.
        store.set_first_run(user["id"], {
            "version": store.FIRST_RUN_VERSION, "stops": {},
            "completed_at": None, "dismissed_at": None,
        })
    store.log_event(
        entity_type="user", entity_id=user["id"],
        event_type="first_run_" + body.action, actor=user["id"],
        payload={"stop": body.stop} if body.stop else None,
    )
    return asc_auth.public_user(store.get_user_by_id(user["id"]))


@router.post("/me/bank-link/interest")
async def register_bank_link_interest(
    user: Dict[str, Any] = Depends(asc_auth.get_current_account),
):
    """§6 stop 5 — "we'll DM you the moment banking goes live".

    The card in the walkthrough is disabled and clearly labelled; this is the
    only thing behind it, and it stores a status rather than pretending to link
    anything. The Stripe work lands on the payments track and reads this column
    to find who has been waiting.
    """
    store = _store()
    if not (user.get("bank_link_status") or "").strip():
        store.set_bank_link_status(user["id"], "coming_soon")
        store.log_event(entity_type="user", entity_id=user["id"],
                        event_type="bank_link_interest", actor=user["id"])
    return {"ok": True, "bank_link_status": "coming_soon"}


# ─── Tutorial — Calibration Case 1 ───────────────────────────────────────────
# The practice case is a fully VIRTUAL task: assembled in memory from
# ``tutorial_case.py``, never inserted into ``tasks``, its submission never
# entering the pipeline. Isolation from the queue, records, exports, stats,
# agreement, and value metrics is therefore structural (those all read the DB),
# not a filter that someone can forget. Only ``events`` rows are written.
@router.get("/tutorial/task")
async def get_tutorial_task(user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.TUTORIAL))):
    """The practice case, blinded EXACTLY like a real task (same
    ``_blind_task`` path: ground_truth stripped, answer texts withheld)."""
    return {"task": _blind_task(tutorial_raw_task())}


@router.post("/tutorial/reveal")
async def tutorial_reveal(
    body: IndependentAnswer, user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.TUTORIAL))
):
    """Mirror of ``POST /tasks/{id}/reveal`` minus persistence: the same
    non-empty-instinct gate (the tutorial teaches the real rule), but no
    ``independent_commits`` row — the practice case leaves no data behind."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "independent_answer_required",
                "message": "Write your independent answer before revealing the AI answers.",
            },
        )
    store = _store()
    store.log_event(
        entity_type="user", entity_id=user["id"],
        event_type="tutorial_reveal", actor=user["id"],
    )
    return {"answers": _task_answers(tutorial_raw_task()), "committed": True}


@router.post("/tutorial/submit")
async def tutorial_submit(
    body: SubmissionIn, user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.TUTORIAL))
):
    """Grade the practice submission against the answer key and stamp the
    caller's tutorial state completed. Never touches the real submit pipeline —
    no ``submissions`` row, no ``records``, no QA routing."""
    if body.task_id != TUTORIAL_TASK_ID:
        raise HTTPException(status_code=400, detail="Not the tutorial task.")
    payload = body.model_dump()
    result = grade_tutorial_submission(payload)
    store = _store()
    current = store.get_tutorial_state(user["id"])
    already_done = bool(current.get("completed_at"))
    if not already_done:
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        current.update({
            "status": "completed",
            "completed_at": now,
            "skipped_at": None,
            "score": {"matched": result["matched"], "total": result["total"]},
        })
        if not current.get("version"):
            current["version"] = TUTORIAL_VERSION
        current.pop("step", None)
        store.set_tutorial_state(user["id"], current)
    store.log_event(
        entity_type="user", entity_id=user["id"],
        event_type="tutorial_submitted", actor=user["id"],
        payload={"matched": result["matched"], "total": result["total"],
                 "replay": already_done},
    )
    return {"result": result,
            "user": asc_auth.public_user(store.get_user_by_id(user["id"]))}


# ─── Users (admin) ────────────────────────────────────────────────────────────
@router.post("/users")
async def create_user(
    body: CreateUserRequest, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    if body.role not in ("evaluator", "admin", "qa_reviewer"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if body.tier is not None and body.tier not in asc_caps.TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"tier must be one of {', '.join(asc_caps.TIERS)}, or omitted.")
    if store.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    user = store.create_user(
        email=body.email,
        password=body.password,
        role=body.role,
        specialty=body.specialty,
        board_cert=body.board_cert,
        years_experience=body.years_experience,
        tier=body.tier,
    )
    store.log_event(entity_type="user", entity_id=user["id"], event_type="user_created", actor=_admin["id"])
    return asc_auth.public_user(user)


@router.get("/users")
async def list_users(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    return {"users": [asc_auth.public_user(u) for u in _store().list_users()]}


@router.post("/users/{user_id}/real-data-approval")
async def set_real_data_approval(
    user_id: str, body: RealDataApprovalRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Grant/revoke V4 real-case access for a contributor (EHR PRD §9.5) — the
    BAA/training attestation lives outside the system; this flag records the
    admin's decision. Serving enforces it on every /tasks/next."""
    store = _store()
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    store.set_real_data_approved(user_id, bool(body.approved))
    store.log_event(
        entity_type="user", entity_id=user_id,
        event_type=("real_data_approved" if body.approved else "real_data_revoked"),
        actor=admin["id"],
    )
    return asc_auth.public_user(store.get_user_by_id(user_id))


# ─── Tasks ────────────────────────────────────────────────────────────────────
def _insert_tasks_from_upload_requests(
    store: Any, tasks: List[TaskIn], *, created_by: str,
) -> List[Dict[str, Any]]:
    """Shared insert loop for the structured (Pydantic) upload path. Returns the
    created task dicts (with ``task_id``/``specialty``) for notify + response."""
    created: List[Dict[str, Any]] = []
    for t in tasks:
        if not (t.prompt or "").strip():
            continue
        # Multimodal (Synthetic Multimodal Cases PRD §5): when a structured case is
        # supplied, the STORED prompt is the rendered case (question + labs table +
        # notes + meds/vitals) so packaging/export/buyers work unchanged; the full
        # case (incl. internal ground_truth) is persisted server-side.
        case_dict = t.case.model_dump() if t.case else None
        prompt_to_store = asc_cases.render_case_prompt(case_dict, t.prompt) if case_dict else t.prompt
        task = store.insert_task(
            task_id=t.task_id,
            prompt=prompt_to_store,
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
            modality=t.modality,
            case=case_dict,
            created_by=created_by,
        )
        created.append(task)
    return created


def _insert_tasks_from_dicts(
    store: Any, tasks: List[Dict[str, Any]], *, created_by: str,
) -> List[Dict[str, Any]]:
    """Shared insert loop for the file-upload (dict) path. Returns the created
    task dicts (with ``task_id``/``specialty``) for notify + response."""
    created: List[Dict[str, Any]] = []
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
            created_by=created_by,
        )
        created.append(task)
    return created


async def _notify_new_tasks(
    store: Any, background_tasks: Optional[BackgroundTasks], created: List[Dict[str, Any]],
    *, admin_id: str,
) -> None:
    """Enqueue the outbox rows synchronously (fast), then drain in the
    background so the admin's request never blocks on ~1000 emails. Also
    posts the in-app announcements (general room plus each affected specialty
    room) — cheap enough to do inline.

    Awaited rather than bridged through a worker-thread loop: the announcement
    ends in a WebSocket broadcast, and the hub's sockets belong to this loop.

    ``background_tasks`` is optional. The outbox is durable and a periodic loop
    drains it (``main._asclepius_task_notify_loop``), so a caller with no
    request-scoped handle still gets its mail sent; passing one only makes the
    send prompt instead of waiting for the next tick.
    """
    if not created:
        return
    batch_id = uuid.uuid4().hex
    asc_task_notify.enqueue_for_batch(store, batch_id=batch_id, created_tasks=created)
    if background_tasks is not None:
        background_tasks.add_task(asc_task_notify.drain_outbox, store)
    await asc_task_notify.post_community_announcement(
        store, admin_user_id=admin_id, created_tasks=created
    )


def _notifiable(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reduce inserted task rows to what notification needs.

    Accepts the full task dicts the insert paths return and keeps only
    ``task_id`` and ``specialty``, dropping anything without both. Written so
    the five previously-silent insert paths can each hand over whatever shape
    they already have.
    """
    out: List[Dict[str, Any]] = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        task_id = t.get("task_id")
        specialty = (t.get("specialty") or "").strip()
        if task_id and specialty:
            out.append({"task_id": task_id, "specialty": specialty})
    return out


@router.post("/tasks")
async def upload_tasks(
    body: TaskUploadRequest, background_tasks: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    created = _insert_tasks_from_upload_requests(store, body.tasks, created_by=admin["id"])
    store.log_event(
        entity_type="task", event_type="tasks_uploaded", actor=admin["id"], payload={"count": len(created)}
    )
    await _notify_new_tasks(store, background_tasks, created, admin_id=admin["id"])
    return {"created": [t["task_id"] for t in created], "count": len(created)}


@router.post("/tasks/upload-file")
async def upload_tasks_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...), admin: Dict[str, Any] = Depends(asc_auth.require_admin),
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
    created = _insert_tasks_from_dicts(store, tasks, created_by=admin["id"])
    store.log_event(
        entity_type="task", event_type="tasks_uploaded_file", actor=admin["id"],
        payload={"count": len(created), "filename": file.filename},
    )
    await _notify_new_tasks(store, background_tasks, created, admin_id=admin["id"])
    return {"created": [t["task_id"] for t in created], "count": len(created)}


@router.post("/admin/task-notifications/drain")
async def drain_task_notifications(
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Manual re-drain safety net (also handy for local QA): sends every
    still-``pending`` outbox row. A crashed BackgroundTasks drain leaves rows
    ``pending`` rather than losing them, so this recovers the tail."""
    store = _store()
    sent, failed = asc_task_notify.drain_outbox(store)
    return {"sent": sent, "failed": failed}


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
    body: CandidateGenRequest, background_tasks: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
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
    await _notify_new_tasks(
        store, background_tasks, _notifiable([task]), admin_id=admin["id"]
    )
    return {"task_id": task["task_id"]}


# ─── Seedmaker auto-generation (Mode A, PRD §7, §10) ──────────────────────────
# Declared BEFORE ``/generation/{specialty}`` on purpose: FastAPI matches routes in
# definition order, so a literal path declared after a parameterised one at the same
# depth is shadowed by it (this route returned 422 'missing body' until it moved).
@router.post("/generation/load-v4-real-cases")
async def load_v4_real_cases(
    specialty: Optional[str] = Query(
        None, description="Load only this specialty's V4 cases; omit for all."),
    max_labels: int = Query(
        V4_DEFAULT_MAX_LABELS, ge=1, le=10,
        description="Independent labels per case. Default 3 (one labeller + two "
                    "for Cohen's kappa). This is what we PAY for — it is not how "
                    "many physicians can SEE the case."),
    open_to_all_specialties: Optional[bool] = Query(
        None, description="Show these cases to every approved physician, "
                          "bypassing specialty routing. VISIBILITY only — it "
                          "does not change max_labels or what we pay. Omit to "
                          "follow ASCLEPIUS_V4_OPEN_TO_ALL (default on), which is "
                          "what the boot seeder uses; pass it explicitly to "
                          "override for this call."),
    background_tasks: BackgroundTasks = None,  # noqa: RUF013 — FastAPI injects it
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Load the three V4 REAL de-identified cases as ``partner_ehr`` tasks
    (V4 Cases & Promotion PRD §3).

    The real-data sibling of ``/generation/{specialty}/load-gold``: no LLM needed
    (each case ships with its authored A/B preference pair), idempotent on a
    stable ``v4real-<case_id>`` task id.

    ``holds`` in the response names any case the content gate is holding OUT of
    the queue and why. A non-empty ``holds`` is the honest answer to "did all
    three ship?" — read it rather than the ``loaded`` count."""
    from asclepius.v4_cases import load_v4_cases

    store = _store()
    sel = specialty.strip().lower() if specialty else None
    if sel and not asc_specialties.is_enabled(sel):
        raise HTTPException(
            status_code=400,
            detail=f"Specialty {sel!r} is not enabled in this release "
                   f"({sorted(s for s in asc_specialties.SPECIALTY_REGISTRY if asc_specialties.is_enabled(s))}).")
    # Omitted => follow the deployment's configured fan-out, so this route and the
    # boot seeder never disagree about who can see the real cases.
    open_all = (v4_open_to_all_specialties() if open_to_all_specialties is None
                else bool(open_to_all_specialties))
    try:
        # An explicit admin action, so it reconciles the cases already in the queue
        # rather than only affecting ones it creates — otherwise re-running this
        # with the checkbox flipped would appear to do nothing.
        res = load_v4_cases(store, specialty=sel, max_labels=max_labels,
                            open_to_all_specialties=open_all,
                            reconcile_visibility=True)
    except Exception as exc:  # a broken entry must return a clear error, not a bare 500
        raise HTTPException(status_code=500, detail=f"Could not load V4 real cases: {exc}")
    res["max_labels"] = max_labels
    res["open_to_all_specialties"] = open_all
    store.log_event(
        entity_type="generation_job", entity_id="v4_real_seed:" + (sel or "all"),
        event_type="v4_real_load", actor=admin["id"],
        payload={"loaded": res.get("loaded", 0), "skipped": res.get("skipped", 0),
                 "held": sorted((res.get("holds") or {}).keys()),
                 "max_labels": max_labels,
                 "revisited": res.get("revisited", 0),
                 "open_to_all_specialties": open_all},
    )
    # ``task_ids`` accumulates only cases created on THIS call (the loader
    # appends alongside its ``loaded`` counter), so a re-run that loads nothing
    # announces nothing. The boot seeder calls load_v4_cases directly rather
    # than through this route, so starting the process never mails anyone.
    if res.get("task_ids"):
        rows = [store.get_task(tid) for tid in res["task_ids"]]
        await _notify_new_tasks(
            store, background_tasks, _notifiable([r for r in rows if r]),
            admin_id=admin["id"],
        )
    return res


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
            multimodal=body.multimodal,
            created_by=admin["id"],
        )
    except asc_specialties.SpecialtyNotEnabled as exc:
        raise HTTPException(status_code=400, detail={"error": "specialty_not_enabled", "message": str(exc)})
    except asc_generation.GenerationDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except asc_corpus.CorpusError as exc:
        raise HTTPException(status_code=500, detail=f"Seed corpus error: {exc}")
    return result


@router.post("/generation/{specialty}/topup")
async def topup_generation(
    specialty: str,
    target: Optional[int] = Query(None, ge=0, le=500,
        description="Desired open V3 pool size; defaults to ASCLEPIUS_TARGET_POOL_SIZE."),
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Continuous supply (PRD §B4): generate NOVEL V3 multimodal cases until the OPEN
    (unlabeled) pool for this specialty reaches ``target`` (default
    ``ASCLEPIUS_TARGET_POOL_SIZE``). A no-op when the pool is already at/above target.
    Respects the same novelty + hardness + structure gates as ``/generation`` and
    reports the drop-reason counts so a shortfall ('why only 3?') is answerable."""
    store = _store()
    tgt = target if target is not None else target_pool_size()
    before = store.open_multimodal_count(specialty)
    deficit = max(0, tgt - before)
    if deficit == 0:
        return {"specialty": specialty, "target": tgt, "pool_before": before, "pool_after": before,
                "requested": 0, "accepted": 0, "dropped": {}, "topped_up": False}
    try:
        result = await asc_generation.generate_tasks(
            store, specialty=specialty, n=deficit, multimodal=True, created_by=admin["id"],
        )
    except asc_specialties.SpecialtyNotEnabled as exc:
        raise HTTPException(status_code=400, detail={"error": "specialty_not_enabled", "message": str(exc)})
    except asc_generation.GenerationDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except asc_corpus.CorpusError as exc:
        raise HTTPException(status_code=500, detail=f"Seed corpus error: {exc}")
    after = store.open_multimodal_count(specialty)
    store.log_event(entity_type="generation_job", entity_id=result.get("job_id") or ("topup:" + specialty),
                    event_type="generation_topup", actor=admin["id"],
                    payload={"target": tgt, "pool_before": before, "pool_after": after,
                             "accepted": result.get("accepted"), "dropped": result.get("dropped")})
    return {"specialty": specialty, "target": tgt, "pool_before": before, "pool_after": after,
            "requested": deficit, "accepted": result.get("accepted", 0),
            "dropped": result.get("dropped", {}), "shortfall": max(0, tgt - after),
            "topped_up": after > before}


@router.post("/generation/{specialty}/load-gold")
async def load_gold_specialty_cases(
    specialty: str,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Load the ratified GOLD multimodal cases for a specialty (Two-Model PRD
    Workstream C — the "load gold" half of the load-vs-generate split).

    Unlike ``POST /generation/{specialty}`` (which synthesizes NOVEL cases via the
    LLM and needs a key), this inserts the hand-authored, clinician-ratified seed
    cases — real labs + EHR + an authored A/B pair — with NO LLM required. Idempotent
    (stable ``gold-<case_id>`` task ids are skipped if already present)."""
    from asclepius.gold_cases import load_gold_cases

    store = _store()
    try:
        res = load_gold_cases(store, specialty=specialty)
    except Exception as exc:  # a broken gold entry must return a clear error, not a bare 500
        raise HTTPException(status_code=500, detail=f"Could not load gold cases: {exc}")
    res["multimodal_in_queue"] = len([
        t for t in store.list_tasks(specialty=specialty, limit=1000)
        if t.get("modality") == "multimodal"
    ])
    store.log_event(
        entity_type="generation_job", entity_id="gold_seed:" + specialty,
        event_type="gold_load", actor=admin["id"],
        payload={"loaded": res.get("loaded", 0), "skipped": res.get("skipped", 0)},
    )
    return res


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
async def list_specialties(_user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE))):
    return {"specialties": asc_specialties.list_specialties()}


# ─── V4 image assets (V4 Image Embedding PRD §3–§4) ──────────────────────────
@router.post("/assets/ingest")
async def ingest_image_asset(
    file: UploadFile = File(...),
    task_id: str = Form(...),
    modality: str = Form(...),
    label: str = Form(""),
    findings: str = Form(""),
    page: int = Form(1),
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Ingest a PNG/JPEG/PDF and attach it as a StudyAsset to a V4 (real de-identified)
    case's study (PRD §3). Enforces PNG/JPEG/PDF only (415), size/dim caps, PDF→raster,
    metadata strip, content-addressed store + dedupe. The image BYTES go to the asset
    store; only the reference is written to the case. Images NEVER enter V1/V2/V3."""
    from asclepius import assets as asc_assets
    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    case = task.get("case") or {}
    # V4 WALL (PRD §3.5 / §11): an image can only attach to a real de-identified case.
    if case.get("case_source") != "real_deid":
        raise HTTPException(status_code=400, detail="image_only_on_real_deid_v4_case")
    data = await file.read()
    try:
        asset = asc_assets.process_upload(data, file.content_type or "", page=page)
    except asc_assets.UnsupportedMediaType as exc:
        raise HTTPException(status_code=415, detail={"error": "unsupported_media_type", "message": str(exc)})
    except asc_assets.ImageTooLarge as exc:
        raise HTTPException(status_code=413, detail={"error": "image_too_large", "message": str(exc)})
    except asc_assets.AssetError as exc:
        raise HTTPException(status_code=422, detail={"error": "asset_error", "message": str(exc)})
    modality = (modality or "other").strip().lower()
    studies = list(case.get("studies") or [])
    # Attach to the first study of this modality that has no asset yet, else append a
    # new study. ``findings`` stays required (the reasoning anchor, PRD §2).
    target = next((s for s in studies if isinstance(s, dict)
                   and str(s.get("modality", "")).lower() == modality and not s.get("asset")), None)
    if target is None:
        target = {"modality": modality, "label": label or modality.upper(),
                  "findings": findings or "(structured findings pending)", "measurements": [], "asset": None}
        studies.append(target)
    if findings:
        target["findings"] = findings
    if label:
        target["label"] = label
    target["asset"] = asset
    case["studies"] = studies
    # Validate the updated case still clears the (extended) multimodal gate.
    try:
        asc_cases.assert_multimodal_content(case)
    except asc_cases.MultimodalContentError as exc:
        raise HTTPException(status_code=422, detail={"error": "case_content", "message": str(exc)})
    store.update_task_case(task_id, case)
    # Index the asset so serving resolves it in one indexed lookup (never a task scan)
    # and the V4 wall can be enforced on the serve path.
    store.insert_asset_ref(asset_id=asset["asset_id"], sha256=asset["sha256"],
                           mime=asset["mime"], task_id=task_id, case_source="real_deid")
    # Return the reference only — never the bytes, store path, or partner id.
    return {"asset_id": asset["asset_id"], "sha256": asset["sha256"], "mime": asset["mime"],
            "modality": modality, "task_id": task_id,
            "case_type": asc_cases.case_type_signature(case)}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str, user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Stream a cleaned image asset by id (PRD §4). Authenticated (evaluator/admin);
    the served bytes carry no provider/model, no partner identity, and no residual
    metadata (stripped at ingest). The store path is never exposed."""
    from asclepius import assets as asc_assets
    store = _store()
    ref = asc_assets.find_asset_by_id(store, asset_id)
    if not ref:
        raise HTTPException(status_code=404, detail="asset_not_found")
    # V4 real-data wall (PRD §4 / EHR PRD §9.5): every image asset lives on a
    # real_deid case, so a non-real-data-approved evaluator must NOT fetch it by id —
    # the wall never depends on the asset_id being unguessable.
    _require_real_data_access({"case_source": ref.get("case_source") or "real_deid"}, user)
    try:
        data, mime = asc_assets.load_asset(ref)
    except asc_assets.AssetError:
        raise HTTPException(status_code=404, detail="asset_unavailable")
    return Response(content=data, media_type=mime, headers={
        "Cache-Control": "private, max-age=3600",
        "Content-Length": str(len(data)),
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/tasks")
async def list_tasks(
    specialty: Optional[str] = None,
    status: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    tasks = _store().list_tasks(specialty=specialty, status=status)
    # admin view keeps generator_model; add submission counts for visibility
    store = _store()
    from asclepius.baselines import _provider_of  # local import mirrors the other baseline uses

    for t in tasks:
        t["submission_count"] = store.submission_count_for_task(t["task_id"])
        # §4.2 two-frontier diagnostic (ADMIN-ONLY — this endpoint requires admin;
        # the evaluator payload is built by _blind_task's allowlist and never
        # carries any of this). Per baseline candidate: provider + model id +
        # the prompt_hash stamped on its stored run, plus a match flag so an
        # admin can verify both answers came from byte-identical input.
        base = [c for c in (t.get("candidate_answers") or []) if c.get("source") == "baseline"]
        if base:
            runs = store.list_baseline_runs(task_id=t["task_id"])
            hash_by_model = {r.get("model"): r.get("prompt_hash") for r in runs if r.get("prompt_hash")}
            cand_meta = [
                {
                    "id": c.get("id"),
                    "provider": c.get("provider") or _provider_of(c.get("baseline_model")),
                    "model": c.get("baseline_model"),
                    "prompt_hash": hash_by_model.get(c.get("baseline_model")),
                }
                for c in base
            ]
            hashes = {m["prompt_hash"] for m in cand_meta if m["prompt_hash"]}
            providers = {m["provider"] for m in cand_meta if m["provider"]}
            t["ab_meta"] = {
                "candidates": cand_meta,
                "prompt_hash_match": len(hashes) <= 1,
                "two_providers": len(providers) >= 2,
            }
        # Two-frontier health flags (§4.2): a task held because no pair could be
        # assembled (e.g. OPENAI_API_KEY unset) must be VISIBLE in admin, never a
        # silent two-Anthropic swap.
        gen = t.get("generation") or {}
        if gen.get("needs_baseline"):
            t["needs_baseline"] = True
        if gen.get("ab_source"):
            t["ab_source"] = gen.get("ab_source")
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


def _autofill_multimodal() -> bool:
    """Whether the empty-queue autofill may generate MULTIMODAL cases (Multimodal
    Debug PRD P0.2). Default OFF: multimodal generation runs the full case-gen +
    case-judge pipeline (slower, more LLM budget) and an operator should opt in.
    When ON, autofill tries a multimodal batch FIRST and still falls back to text
    corpus seeding if it yields nothing — the queue never starves either way."""
    return (os.getenv("ASCLEPIUS_AUTOFILL_MULTIMODAL", "0").strip().lower()
            in ("1", "true", "yes", "on"))


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


def _value_aware_next(
    store: Any, user: Dict[str, Any], specialty: Optional[str], *, hard_only: bool = False,
    real_only: bool = False, trajectory_only: bool = False, multimodal_only: bool = False,
    require_measured_difficulty: bool = False, min_empirical_difficulty: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """Value-aware routing (Value-per-Minute PRD B3) — assisted flows. Serves the
    eligible task with the highest expected value-per-minute for THIS contributor
    (their rolling median speed × each task's expected realized value). Ties break
    on the oldest task, preserving FIFO fairness within an equal-value cohort.
    ``hard_only`` (WS2) restricts the candidate set to hard cases (the V3 queue);
    ``real_only`` is the V4 wall (EHR PRD §9.5); ``trajectory_only`` is the
    longitudinal wall (Longitudinal E2E PRD §5.1); ``multimodal_only`` restricts V3
    to structured cases (the multimodal-by-default queue). The empirical-difficulty
    gate (PRD §9) restricts to live-measured-above-floor cases when required."""
    candidates = store.eligible_tasks_for_evaluator(
        evaluator_id=user["id"], specialty=specialty, hard_only=hard_only,
        real_only=real_only, trajectory_only=trajectory_only, multimodal_only=multimodal_only,
        require_measured_difficulty=require_measured_difficulty,
        min_empirical_difficulty=min_empirical_difficulty,
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
    store: Any, user: Dict[str, Any], *, portal_version: Optional[str] = None,
    fallback: bool = True, specialty: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    # Value-aware routing is an ASSISTED-flow enhancement (V2 + V3). V1 (and any
    # request that does not explicitly declare an assisted flow) keeps the exact
    # classic oldest-first behavior — the "V1 untouched" guarantee, enforced at
    # the gate. Match the LITERAL declared version only: an absent, empty, or
    # typo'd value must fall to classic (normalize_portal_version would map
    # "" / "v9" to the default and silently opt them in — what this must not do).
    value_aware = portal_version in ASSISTED_PORTAL_VERSIONS
    # V3 is the hard-case queue (Seamless PRD WS2): it serves ONLY difficulty=hard.
    # Gated on hard_only_generation() so the two settings can't silently disagree:
    # if an operator sets ASCLEPIUS_HARD_ONLY=0 (disabling the hardness gate, so
    # nothing gets stamped 'hard'), V3 stops filtering to hard and serves the
    # available queue instead of showing an empty V3 to every clinician.
    hard_only = portal_version == "v3" and hard_only_generation()
    # The V4 wall (EHR PRD §9.5): v4 serves ONLY real (case_source='real_deid')
    # tasks; every other version EXCLUDES them — a real patient case can never be
    # served into a v1/v2/v3 session, even by accident. V4 additionally requires
    # the contributor to be real-data approved (BAA/training) — an unapproved
    # evaluator asking for v4 gets an empty queue, never a real case.
    #
    # V5 (longitudinal) is real data too, so the SAME approval gate applies to it
    # unchanged — a chart walk is more sensitive than a static real case, not less.
    # The two flags then partition the real pool inside ``labeler_queue_sql``: V4
    # gets the static cases, V5 gets the trajectory points, neither gets the other.
    real_only = portal_version == REAL_CASE_PORTAL_VERSION
    trajectory_only = portal_version == LONGITUDINAL_PORTAL_VERSION
    if (real_only or trajectory_only) and not user.get("real_data_approved"):
        return None
    # V3 multimodal-by-default (ASCLEPIUS_V3_MULTIMODAL_ONLY, default on): the
    # seamless queue PREFERS structured cases (labs + EHR notes) — so whenever a
    # multimodal case is available it is served ahead of a bare text prompt. It is a
    # preference, NOT a hard filter: if no multimodal case is available (e.g. none
    # have been generated yet), V3 falls back to the normal hard queue rather than
    # showing the clinician an empty "queue cleared" screen. Set the env to 0 to
    # disable the preference entirely.
    multimodal_pref = portal_version == "v3" and v3_multimodal_only()
    # Empirical-difficulty serving gate (Specialty Hyper-Personalization PRD §9):
    # when required, serve only cases whose frontier-failure rate was live-measured
    # above the floor. Default OFF so authored/declared seeds still serve in dev.
    require_measured = require_measured_difficulty()
    ed_floor = min_empirical_difficulty()

    def _classic(specialty: Optional[str], mm_only: bool) -> Optional[Dict[str, Any]]:
        return store.next_task_for_evaluator(
            evaluator_id=user["id"], specialty=specialty, hard_only=hard_only,
            real_only=real_only, trajectory_only=trajectory_only, multimodal_only=mm_only,
            require_measured_difficulty=require_measured, min_empirical_difficulty=ed_floor,
        )

    def _pick(specialty: Optional[str], mm_only: bool) -> Optional[Dict[str, Any]]:
        return (_value_aware_next(store, user, specialty, hard_only=hard_only,
                                  real_only=real_only, trajectory_only=trajectory_only,
                                  multimodal_only=mm_only,
                                  require_measured_difficulty=require_measured,
                                  min_empirical_difficulty=ed_floor)
                if value_aware else _classic(specialty, mm_only))

    def _pick_pref(specialty: Optional[str]) -> Optional[Dict[str, Any]]:
        # Prefer a multimodal case. With ``fallback`` (the default) a text/hard task
        # is served when no structured case exists yet, so V3 is never empty. With
        # ``fallback=False`` the caller wants a multimodal case ONLY (returns None if
        # none exists) — that's how ``next_task`` decides it must trigger generation
        # instead of serving the text seed forever (the seed would otherwise satisfy
        # the queue and generation would never fire).
        if multimodal_pref:
            got = _pick(specialty, True)
            if got or not fallback:
                return got
        return _pick(specialty, False)

    # Specialty selection (Specialty Hyper-Personalization PRD §1): the V3 picker
    # sends ``?specialty=`` to drive task fetch. An explicit, ENABLED specialty
    # overrides the evaluator's default specialty; anything unknown/disabled falls
    # back to the evaluator's own specialty (never silently serves the wrong one).
    chosen = None
    if specialty and asc_specialties.is_enabled(specialty):
        chosen = specialty.strip().lower()
    serve_specialty = chosen or user.get("specialty")
    pick = _pick_pref(serve_specialty)
    if not pick and not serve_specialty:
        # admins/QA (and SSO-provisioned clinicians) with no specialty see any queue
        pick = _pick_pref(None)
    return pick


async def _seed_tasks_from_corpus(store: Any, specialty: str, batch: int, *, hard_only: bool = False) -> int:
    """Turn ratified seed-corpus prompts into eval tasks by generating only the
    two candidate answers (Sonnet ``asclepius_candidate_gen``). This deliberately
    bypasses the Opus prompt-synthesis + judge + dedupe pipeline (``generate_tasks``)
    so the queue fills reliably and fast: the prompts are already vetted, we just
    need the A/B answers. Returns the number of tasks created.

    ``hard_only`` (the V3 hard-case queue, Seamless PRD WS2): only seed items
    already marked ``difficulty=hard`` are used — we never fabricate hardness by
    promoting a medium seed, and we never create tasks V3 would filter out (which
    would waste candidate-gen calls and leave the queue perpetually empty)."""
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
        if hard_only and (it.get("difficulty") or "medium") != "hard":
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
    store: Any, user: Dict[str, Any], *, portal_version: Optional[str] = None,
    specialty: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not _autofill_enabled():
        return None
    # V4 (real cases) can NEVER be autofilled — real patient data cannot be
    # fabricated. An empty V4 queue stays empty until a partner bundle ingests.
    if portal_version == REAL_CASE_PORTAL_VERSION:
        return None
    # The V3 picker's chosen specialty (PRD §1) drives WHICH specialty is generated,
    # overriding the evaluator's default when it names an enabled specialty.
    specialty_override = specialty
    specialty = (specialty_override if (specialty_override and asc_specialties.is_enabled(specialty_override))
                 else _autofill_specialty(user))
    cooldown = _autofill_cooldown_sec()
    # V3 multimodal-by-default: a text/hard seed in the queue must NOT count as
    # "already filled" — otherwise the seed short-circuits the re-check below and a
    # structured multimodal case is never generated (exactly the V3-shows-text bug).
    # For the multimodal preference we only treat the queue as filled if a MULTIMODAL
    # case is already present (fallback=False); every other flow keeps the old
    # any-task check.
    mm_pref = portal_version == "v3" and v3_multimodal_only()
    if time.monotonic() - _autofill_last_attempt.get(specialty, 0.0) < cooldown:
        return None
    async with _AUTOFILL_LOCK:
        # Another request may have filled the queue (or just attempted) while we
        # waited on the lock — re-check before spending LLM budget. Under the
        # multimodal preference this looks for a multimodal case specifically.
        task = _query_next(store, user, portal_version=portal_version,
                           fallback=not mm_pref, specialty=specialty_override)
        if task:
            return task
        if time.monotonic() - _autofill_last_attempt.get(specialty, 0.0) < cooldown:
            return None
        _autofill_last_attempt[specialty] = time.monotonic()
        # V3 multimodal-by-default: PREFER generating structured multimodal cases,
        # but fall back to text seeding if generation is unavailable (no LLM key) so
        # the V3 clinician is never left with an empty "queue cleared" screen. The
        # serving side (_query_next) mirrors this — multimodal is a preference, not a
        # hard filter. (``mm_pref`` computed above, before the cooldown gate.)
        try:
            created = 0
            # Multimodal autofill (Multimodal Debug PRD P0.2): a full multimodal
            # batch FIRST (case-gen → case-judge → hardness, all the normal gates).
            if mm_pref or _autofill_multimodal():
                try:
                    mm = await asc_generation.generate_tasks(
                        store, specialty=specialty, n=_autofill_batch(),
                        multimodal=True, created_by="system:autofill",
                    )
                    created = int(mm.get("accepted") or 0)
                    if created:
                        log.info(
                            "asclepius autofill: created %d MULTIMODAL case(s) for %s (dropped: %s)",
                            created, specialty, mm.get("dropped") or {},
                        )
                    elif mm.get("dropped"):
                        log.warning(
                            "asclepius autofill: multimodal batch yielded 0 for %s "
                            "(dropped: %s); falling back to text seeding so V3 is not empty",
                            specialty, mm.get("dropped"),
                        )
                except asc_generation.GenerationDisabled:
                    # No LLM. Fall through to text seeding below so the V3 clinician
                    # still gets a case (a bare hard prompt) rather than an empty
                    # "queue cleared" screen. The actionable warning tells the
                    # operator that structured multimodal cases need an LLM key.
                    if mm_pref:
                        log.warning(
                            "asclepius autofill: V3 prefers multimodal cases but "
                            "generation is unavailable (no LLM key). Falling back to the "
                            "text hard queue; set ANTHROPIC_API_KEY to serve structured "
                            "multimodal cases."
                        )
                except Exception:
                    log.exception("asclepius autofill: multimodal generation failed")
            # Fallback when live generation produced nothing (no LLM key, or every
            # candidate dropped). For V3 nephrology we seed the ratified GOLD
            # multimodal cases — real labs + EHR notes + an authored A/B pair, and NO
            # LLM required — so the seamless queue shows genuine structured cases even
            # with generation unavailable. Every other flow keeps the text corpus seed.
            if created == 0 and mm_pref:
                # Every specialty with an authored gold set (nephrology, cardiology,
                # oncology) seeds real structured cases with NO LLM required (PRD §7).
                from asclepius.gold_cases import load_gold_cases
                res = load_gold_cases(store, specialty=specialty)
                created = res["loaded"]
                log.info(
                    "asclepius autofill: seeded %d GOLD multimodal case(s) for %s (%d already present)",
                    created, specialty, res["skipped"],
                )
            if created == 0:
                created = await _seed_tasks_from_corpus(
                    store, specialty, _autofill_batch(),
                    hard_only=(portal_version == "v3" and hard_only_generation()),
                )
                log.info("asclepius autofill: created %d task(s) for %s", created, specialty)
        except asc_specialties.SpecialtyNotEnabled:
            return None
        except asc_corpus.CorpusError as exc:
            log.warning("asclepius autofill: seed corpus error: %s", exc)
            return None
        except Exception:  # never let generation break the evaluator's queue request
            log.exception("asclepius autofill failed")
            return None
    return _query_next(store, user, portal_version=portal_version, specialty=specialty_override)


def _ensure_gold_cases(store: Any, user: Dict[str, Any], specialty: Optional[str] = None) -> int:
    """Load the ratified GOLD multimodal cases so V3 always has real structured cases
    to serve — independent of the ASCLEPIUS_AUTOFILL flag AND the LLM (these are
    static, pre-authored, ready-to-serve tasks with an A/B pair). Loads the gold set
    for the SERVED specialty (the picker's choice, else the evaluator's specialty,
    else all enabled specialties for an admin/QA with none). Idempotent (already-
    present cases are skipped). Returns the number newly loaded (Specialty
    Hyper-Personalization PRD §1/§7)."""
    sp = (specialty or user.get("specialty") or "").strip().lower()
    # Memoize per STORE instance so the idempotent seed runs once per specialty per
    # process — NOT on every /tasks/next poll (load_gold_cases does per-case SELECTs).
    # Attached to the store so a test's fresh_store() (a new instance) re-seeds cleanly.
    ensured = getattr(store, "_gold_ensured", None)
    if ensured is None:
        ensured = set()
        try:
            setattr(store, "_gold_ensured", ensured)
        except Exception:  # pragma: no cover
            pass
    try:
        from asclepius.gold_cases import load_gold_cases
        targets = [sp] if sp else [c["specialty"] for c in asc_specialties.list_specialties() if c.get("enabled")]
        targets = [t for t in targets if t and t not in ensured]
        if not targets:
            return 0
        loaded = 0
        for t in targets:
            loaded += int(load_gold_cases(store, specialty=t).get("loaded", 0))
            ensured.add(t)  # mark only after a successful load
        return loaded
    except Exception:  # never let seeding break the queue request
        log.exception("asclepius: gold-case seeding failed")
        return 0


def _ensure_v4_real_cases(store: Any, user: Dict[str, Any],
                          specialty: Optional[str] = None) -> int:
    """Load the three V4 REAL de-identified cases so the V4 queue always has real
    structured cases to serve (V4 Cases & Promotion PRD §3/§7.8).

    The V4 sibling of ``_ensure_gold_cases``, and deliberately separate from it:
    gold cases are ``case_source='synthetic'`` and must never be fed to the
    real-case wall, while these are the actual partner charts and belong nowhere
    else. Same properties — no LLM (each ships with its authored A/B pair),
    idempotent on a stable ``v4real-<case_id>`` task id, memoized per store
    instance so it runs once per specialty per process rather than on every
    ``/tasks/next`` poll.

    Loads with ``max_labels = 3`` (V4 PRD §4): one labeller plus two independent
    for Cohen's kappa. VISIBILITY to every approved physician in the specialty is
    the specialty routing already doing its job — it is not, and must not become,
    a function of ``max_labels``.

    A case the content gate HOLDS (see ``v4_cases.V4_HOLDS``) is not loaded and is
    logged by name. Returns the number newly loaded.

    Loads EVERY case, not just the one matching the specialty being drawn. This
    used to filter by the drawn specialty, which meant a nephrologist's draw
    created only the nephrology case and left the hepatology one uncreated — so
    which real cases existed depended on who had logged in, and an admin looking
    at the queue saw an arbitrary subset. There are three cases; loading all of
    them is one cheap idempotent call, and "what is in the queue" should not be a
    function of who drew first.

    The startup hook in ``main.py`` normally gets here first, which makes this a
    backstop rather than the primary path: it still matters for a store created
    after boot (a test's fresh store, a re-pointed DB)."""
    ensured = getattr(store, "_v4_real_ensured", None)
    if ensured:
        return 0
    try:
        from asclepius.constants import v4_open_to_all_specialties
        from asclepius.v4_cases import load_v4_cases
        # Creates what is missing; never rewrites the visibility of what is already
        # there. This runs on a physician's queue request, and a draw must not
        # change who else can see the corpus (reconcile_visibility stays off).
        res = load_v4_cases(
            store, open_to_all_specialties=v4_open_to_all_specialties())
        for cid, reason in (res.get("holds") or {}).items():
            # Named, not swallowed: a case silently absent from the queue is the
            # exact failure this PRD exists to remove.
            log.warning("V4 real case %s is held out of the queue: %s", cid, reason)
        try:
            setattr(store, "_v4_real_ensured", True)  # only after a successful load
        except Exception:  # pragma: no cover
            pass
        return int(res.get("loaded", 0))
    except Exception:  # never let seeding break the queue request
        log.exception("asclepius: V4 real-case seeding failed")
        return 0


# ─── The LABEL capability, enforced ───────────────────────────────────────────
#
# ``capabilities.LABEL`` was defined and never checked. Drawing a task and
# submitting one gated on authentication alone, so the capability table decided
# nothing: a NULL-tier account could draw and submit, and setting somebody's tier
# restricted them from nothing. A policy table nobody reads is worse than no
# policy table, because it reads like a control.
#
# Same gate shape as the advisor router's ``_require`` — admins pass, as
# everywhere else. Accounts that predate tiering are backfilled to ``labeler`` in
# the store migration, so this locks nobody out of work they are already doing.
def require_label(
    # BOTH axes, because they answer different questions. require_surface says
    # this account may touch real patient data at all; asc_caps.can says the
    # tier it was assigned includes labelling. Neither implies the other, and
    # relying on the coincidence that a provisional user's tier is NULL would be
    # relying on a coincidence.
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.REAL_WORK)),
) -> Dict[str, Any]:
    if not asc_caps.can(user, asc_caps.LABEL):
        raise HTTPException(
            status_code=403,
            detail="Your account is not yet assigned a contributor tier, so it "
                   "cannot draw or submit tasks. An admin assigns one when your "
                   "credentials are approved.")
    return user


@router.get("/tasks/next")
async def next_task(
    portal_version: Optional[str] = Query(
        None,
        description="Declare the active flow. 'v2' opts into value-aware routing "
        "(serves the highest expected value-per-minute task for you). Absent or "
        "'v1' keeps the classic oldest-first queue unchanged.",
    ),
    specialty: Optional[str] = Query(
        None,
        description="The V3 specialty picker's choice (nephrology|cardiology|"
        "oncology). Drives which specialty's cases are served/generated; an unknown "
        "or disabled value falls back to the evaluator's own specialty.",
    ),
    user: Dict[str, Any] = Depends(require_label),
):
    store = _store()
    # Normalize the picker's specialty to an enabled one, else ignore it (never
    # serve the wrong specialty on a typo).
    sel_specialty = specialty.strip().lower() if specialty else None
    if sel_specialty and not asc_specialties.is_enabled(sel_specialty):
        sel_specialty = None
    # When the V3 picker names a specialty, guarantee its authored GOLD cases are
    # present (idempotent, no LLM) so the picker always serves that specialty's real
    # cases regardless of the multimodal-preference/autofill settings (PRD §1/§7).
    # Never for V4 — the real-case wall must not be fed synthetic gold cases.
    if sel_specialty and portal_version != REAL_CASE_PORTAL_VERSION:
        _ensure_gold_cases(store, user, sel_specialty)
    # V3 multimodal-by-default: PREFER a structured case. Critically, a text/hard
    # seed already in the queue must NOT stop us from generating a multimodal case —
    # otherwise the seed is served forever and generation never fires (the V3-shows-
    # text bug). So: (1) serve a multimodal case if one exists; (2) else trigger
    # autofill to GENERATE one; (3) only if generation yields nothing fall back to
    # the text queue, so V3 is never empty.
    mm_pref = portal_version == "v3" and v3_multimodal_only()
    if mm_pref:
        task = _query_next(store, user, portal_version=portal_version, fallback=False, specialty=sel_specialty)
        if not task:
            task = await _autofill_queue(store, user, portal_version=portal_version, specialty=sel_specialty)
        if not task:
            # GUARANTEED structured cases: load the ratified GOLD cases for the served
            # specialty and serve one. This does NOT depend on ASCLEPIUS_AUTOFILL or
            # the LLM — the gold cases are static, pre-authored, ready-to-serve tasks —
            # so V3 shows a real case even when autofill is off and no API key is set.
            _ensure_gold_cases(store, user, sel_specialty)
            task = _query_next(store, user, portal_version=portal_version, fallback=False, specialty=sel_specialty)
        if not task:
            task = _query_next(store, user, portal_version=portal_version, fallback=True, specialty=sel_specialty)
    else:
        task = _query_next(store, user, portal_version=portal_version, specialty=sel_specialty)
        if not task and portal_version == REAL_CASE_PORTAL_VERSION and user.get("real_data_approved"):
            # V4 is the mirror of the gold fallback above, not an exception to it:
            # the real-case wall must not be fed synthetic gold cases, and it must
            # not be left empty either. The V4 real cases ARE real charts, so this
            # is the one seed that belongs here.
            #
            # ONLY on an empty queue, deliberately. Seeding unconditionally would
            # inject three tasks into the priority sort ahead of work an admin
            # actually promoted from a partner bundle, which is not the seed's job
            # — its job is that a V4 physician never opens an empty queue.
            if _ensure_v4_real_cases(store, user, sel_specialty):
                task = _query_next(store, user, portal_version=portal_version,
                                   specialty=sel_specialty)
        if not task and portal_version == REAL_CASE_PORTAL_VERSION:
            # ═══ V4 → V3 continuation ═══
            # There are a finite number of real charts. A physician who finishes
            # them used to hit "queue cleared" and stop, which is the wrong end
            # state for someone sitting down to work: the synthetic multimodal
            # queue is the same task shape and is not empty.
            #
            # The served case decides the stamp, NOT the picker. A synthetic case
            # labelled under a v4 claim is a 400 at ``_derive_portal_version`` (a
            # real/synthetic mislabel is refused, never normalised), so the
            # response below returns ``served_portal_version`` and the client
            # stamps from that. The record then says v3 because the work WAS v3 —
            # which is the whole point of the derivation wall, not a way around it.
            task = _query_next(store, user, portal_version="v3", specialty=sel_specialty)
            if not task:
                task = await _autofill_queue(store, user, portal_version="v3",
                                             specialty=sel_specialty)
        if not task:
            # Empty queue -> auto-generate a fresh batch via the engine, then serve.
            task = await _autofill_queue(store, user, portal_version=portal_version, specialty=sel_specialty)
    if not task:
        return {"task": None, "served_portal_version": None, "continued_from": None}
    # Derived from the TASK, on the same rule the submit path enforces, so the
    # client cannot be handed a version its own submission would be rejected for.
    served = _derive_portal_version(task, None)
    return {
        "task": _blind_task(task),
        "served_portal_version": served,
        # Set only when we moved the physician off the flow they picked, so the UI
        # can say so. Silently switching a paid labeller who chose "real patient
        # data" onto synthetic cases would be the product lying by omission.
        "continued_from": (portal_version
                           if (portal_version and served != portal_version) else None),
    }


@router.get("/tasks/available")
async def available_tasks(
    portal_version: Optional[str] = Query(None, description="Active flow (v1/v2/v3/v4), same as /tasks/next."),
    specialty: Optional[str] = Query(None, description="Specialty picker choice; falls back to the evaluator's own."),
    limit: int = Query(50, ge=1, le=200),
    # Same gate as /tasks/next, deliberately. This endpoint's whole contract is
    # "the tasks this evaluator can pick RIGHT NOW", so gating the draw but not
    # the list would have the dashboard advertise cases the very next click
    # refuses — the product knowing something and not saying it, which is the
    # class of defect this round exists to remove.
    user: Dict[str, Any] = Depends(require_label),
):
    """The tasks THIS evaluator can pick right now — the dashboard list.

    Reuses the exact eligibility the router uses to serve the next case
    (``store.eligible_tasks_for_evaluator`` already drops tasks the evaluator has
    labeled or that are at capacity), with the same hard/real filters, so the
    dashboard count matches what /tasks/next would hand out. Multimodal is only a
    serving PREFERENCE (V3 falls back to text), so it is NOT a hard filter here:
    the list shows everything the evaluator may work on. Cards are lightweight
    metadata only — the case content still loads through the existing task flow."""
    store = _store()
    sel = specialty.strip().lower() if specialty else None
    if sel and not asc_specialties.is_enabled(sel):
        sel = None
    serve_specialty = sel or (user.get("specialty") or None)
    hard_only = portal_version == "v3" and hard_only_generation()
    real_only = portal_version == REAL_CASE_PORTAL_VERSION
    trajectory_only = portal_version == LONGITUDINAL_PORTAL_VERSION
    if (real_only or trajectory_only) and not user.get("real_data_approved"):
        return {"tasks": [], "count": 0, "longitudinal_available": 0,
                "served_portal_version": None, "continued_from": None}
    rows = store.eligible_tasks_for_evaluator(
        evaluator_id=user["id"], specialty=serve_specialty, hard_only=hard_only,
        real_only=real_only, trajectory_only=trajectory_only, multimodal_only=False,
        require_measured_difficulty=require_measured_difficulty(),
        min_empirical_difficulty=min_empirical_difficulty(),
        limit=limit,
    )
    # The list and the draw must agree about what exists: /tasks/next seeds the V4
    # real cases when the queue is empty, so a dashboard that said "0 available"
    # and then handed out a case on the next click would be the product knowing
    # something and not saying it. Same condition, same seed, same re-query.
    # Seeding is a V4-only affair: the seed loads the three authored STATIC cases,
    # and there is no equivalent for V5. A chart walk exists because an admin
    # generated one from an uploaded chart and then routed it — there is nothing
    # to fall back on, and an empty V5 queue is the correct answer, not a gap.
    if real_only and not rows and _ensure_v4_real_cases(store, user, serve_specialty):
        rows = store.eligible_tasks_for_evaluator(
            evaluator_id=user["id"], specialty=serve_specialty, hard_only=hard_only,
            real_only=real_only, multimodal_only=False,
            require_measured_difficulty=require_measured_difficulty(),
            min_empirical_difficulty=min_empirical_difficulty(),
            limit=limit,
        )
    # ═══ V4 → V3 continuation, mirrored from /tasks/next ═══
    # /tasks/next continues a physician who has finished every real chart onto the
    # synthetic multimodal queue. Without the same step here the dashboard reads
    # "no cases available" and the very next click hands one out — the same
    # list/draw disagreement the seed above exists to prevent, in the other
    # direction. Real cases still come FIRST: this runs only once V4 is exhausted.
    # Echo only a version this endpoint actually reasons about. An unknown or
    # absent ``portal_version`` means the classic oldest-first queue answered, and
    # naming it something it is not would be the same class of lie this field
    # exists to remove.
    served = portal_version if portal_version in SINGLE_TURN_PORTAL_VERSIONS else None
    continued_from = None
    # V5 deliberately does NOT continue onto the synthetic queue. Continuation
    # exists so a physician who has cleared the real backlog is not left staring at
    # an empty screen — but a longitudinal walk is ASSIGNED work: an empty V5 queue
    # means "nothing has been routed to you", and quietly handing over a synthetic
    # case would answer a question the physician did not ask.
    if real_only and not rows:
        served = "v3"
        continued_from = REAL_CASE_PORTAL_VERSION
        rows = store.eligible_tasks_for_evaluator(
            evaluator_id=user["id"], specialty=serve_specialty,
            hard_only=hard_only_generation(), real_only=False, multimodal_only=False,
            require_measured_difficulty=require_measured_difficulty(),
            min_empirical_difficulty=min_empirical_difficulty(),
            limit=limit,
        )
    tasks = [
        {
            "task_id": t.get("task_id"),
            "specialty": t.get("specialty"),
            "difficulty": t.get("difficulty"),
            "modality": t.get("modality"),
            "case_source": t.get("case_source"),
            "created_at": t.get("created_at"),
            # PRD 2 §3.5 — a trajectory card says which step it is, so a physician
            # sees "step 3 of 13 on one patient" rather than three unexplained
            # cards. Metadata only: the sequence GATE is enforced in SQL and again
            # on the by-ID path; this is what the card reads, never what decides.
            "trajectory_id": t.get("trajectory_id"),
            "sequence_index": t.get("sequence_index"),
        }
        for t in rows
    ]
    # ``served_portal_version`` is the queue these counts describe, which is not
    # always the one that was asked for. The dashboard names it on screen so a
    # physician who chose real patient data is told when they are being shown
    # synthetic work, rather than left to notice it inside a case.
    # PRD §5.1 Group B — how many longitudinal points are ROUTED to this physician
    # right now, whichever version they are currently looking at.
    #
    # The V5 tab renders only when this is non-zero, because V5 is assigned work:
    # a walk reaches a physician exactly one way, an admin pressing Send, so a tab
    # that appeared for everyone would be empty for almost everyone and would read
    # as the product being broken. Counted through the SAME eligibility the queue
    # uses — not a bare ``trajectory_id IS NOT NULL`` scan — so the number and the
    # queue cannot disagree: it already accounts for the distribution gate, the
    # sequence seal, capacity and independence.
    longitudinal_available = 0
    if user.get("real_data_approved"):
        longitudinal_available = len(store.eligible_tasks_for_evaluator(
            evaluator_id=user["id"], specialty=serve_specialty,
            trajectory_only=True, multimodal_only=False,
            require_measured_difficulty=require_measured_difficulty(),
            min_empirical_difficulty=min_empirical_difficulty(),
            limit=200,
        ))
    return {"tasks": tasks, "count": len(tasks),
            "longitudinal_available": longitudinal_available,
            "served_portal_version": served if tasks else None,
            "continued_from": continued_from if tasks else None}


@router.get("/admin/real-case-access")
async def real_case_access_report(
    email: Optional[str] = Query(None, description="Check one physician by email; omit for a summary."),
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Why can (or cannot) this physician see the V4 real cases?

    Every gate between an approved doctor and a real chart, evaluated for a named
    account, with the reason spelled out. This exists because the question kept
    being answered by reading source: a physician reports an empty queue, and
    ``real_data_approved`` is one flag, LABEL is another, specialty routing is a
    third, and per-case capacity is a fourth — four different reasons for one
    identical empty screen. An operator should be able to ask the product."""
    from asclepius.capabilities import LABEL, can as _can
    from asclepius.constants import v4_open_to_all_specialties as _open_all_cfg
    from asclepius.v4_cases import V4_REAL_CASES, v4_task_id

    store = _store()
    seeded = []
    for entry in V4_REAL_CASES:
        t = store.get_task(v4_task_id(entry["case_id"]))
        if t:
            seeded.append({"case_id": entry["case_id"], "specialty": t.get("specialty"),
                           "open_to_all_specialties": bool(t.get("open_to_all_specialties")),
                           "max_labels": t.get("max_labels")})
    # What the RUNNING process did at boot, not what the code would do now: an
    # operator asking "did my deploy take?" needs the answer from this container.
    try:
        import main as _main_mod
        boot = dict(getattr(_main_mod, "_V4_BOOT_SUMMARY", {}) or {})
        build = _main_mod._running_commit()
        build["started_at"] = getattr(_main_mod, "_BOOT_AT", None)
    except Exception:  # a test app, or main imported under another name
        boot, build = {}, {}
    report: Dict[str, Any] = {
        "build": build,
        "v4_seeding_at_boot": boot,
        "cases_in_queue": seeded,
        "open_to_all_specialties_setting": _open_all_cfg(),
        "specialties_with_a_real_case": sorted({c["specialty"] for c in seeded}),
    }
    if not email:
        return report

    u = store.get_user_by_email(email.strip().lower())
    if not u:
        raise HTTPException(status_code=404, detail=f"No account for {email!r}.")

    blockers: List[str] = []
    if not u.get("active"):
        blockers.append("The account is deactivated.")
    notes: List[str] = []
    is_admin = (u.get("role") or "") == "admin"
    if is_admin:
        # NOT a real-data blocker, and saying so would be wrong: admins hold LABEL,
        # so an admin who is also verified DOES get the auto-grant and can draw real
        # cases. What the role actually costs them is the portal itself — the nav
        # shows the admin console, and the roster files them outside "Approved &
        # Labeling" — so a physician working from this account looks and is filed
        # like an operator. Reported as context, not as the cause.
        notes.append(
            "The role is 'admin', not 'evaluator'. That does not block real cases "
            "on its own, but it puts the admin console in their nav and keeps them "
            "off the Approved & Labeling roster. Set it to 'evaluator' from the "
            "Physicians roster.")
    env_admin = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip().lower()
    pinned = bool(env_admin and env_admin == (u.get("email") or "").strip().lower())
    if pinned:
        # The trap this endpoint exists to make visible: the console's own role
        # button appears to work and the next deploy silently reverts it. Whether
        # that is still true depends on the guard in ensure_admin_from_env, so
        # answer for the state this deployment is ACTUALLY in rather than warning
        # about something that can no longer happen.
        protected = (not is_admin) and store.count_active_admins(excluding=u["id"]) > 0
        if protected:
            notes.append(
                "ASCLEPIUS_ADMIN_EMAIL names this account. The boot-time admin "
                "bootstrap now refuses to convert a physician account while another "
                "admin exists, so the role will survive the next deploy — but "
                "repoint that variable at a separate operations account so it stops "
                "depending on that guard.")
        else:
            blockers.append(
                "ASCLEPIUS_ADMIN_EMAIL names this account, so every boot forces it "
                "back to role='admin'"
                + ("" if is_admin else " (it is the only active admin, so the "
                                       "bootstrap cannot stand down without locking "
                                       "the console out)")
                + ". Changing the role in the console will be undone by the next "
                  "deploy. Point that variable at a separate operations account "
                  "first.")
    if u.get("verification_status") != "approved":
        blockers.append(
            f"Verification status is {u.get('verification_status')!r}, not 'approved'. "
            "Real-data approval follows labeling approval, so nothing is granted until "
            "this clears.")
    if not _can(u, LABEL):
        blockers.append(
            f"Tier is {u.get('tier')!r}, which does not carry the LABEL capability. "
            "Drawing any case requires it.")
    if not u.get("real_data_approved"):
        blockers.append(
            "real_data_approved is off. It is granted automatically to approved "
            "labelers at boot and on the approval route, UNLESS an admin set it "
            f"explicitly (source={u.get('real_data_approval_source')!r}) — a human "
            "decision is never overridden by the sync.")
    spec = (u.get("specialty") or "").strip().lower()
    visible = [c for c in seeded
               if c["open_to_all_specialties"] or c["specialty"] == spec]
    if not visible:
        blockers.append(
            f"No real case is routed to {spec or 'their (unset) specialty'}. Real "
            f"cases exist for {sorted({c['specialty'] for c in seeded})}. Set "
            "ASCLEPIUS_V4_OPEN_TO_ALL=1 (the default) so every approved physician "
            "sees all of them, or promote a chart in their specialty.")

    # What the queue ACTUALLY returns for them — the gates above are the reasons,
    # this is the outcome, and if they ever disagree the outcome is the truth.
    #
    # The real-data wall lives in the ROUTER, not in the store query: passing
    # real_only=True filters to real tasks, it does not ask whether this person may
    # see one. Reproducing the router's own check here is the difference between a
    # report and a lie — without it this endpoint would cheerfully tell an operator
    # that an unapproved physician can see real cases that /tasks/next refuses them.
    if u.get("real_data_approved") and u.get("active") and _can(u, LABEL):
        eligible = store.eligible_tasks_for_evaluator(
            evaluator_id=u["id"], specialty=spec or None, real_only=True, limit=50)
    else:
        eligible = []
    report["physician"] = {
        "email": u.get("email"), "specialty": u.get("specialty"),
        "role": u.get("role"),
        "pinned_admin_by_env": pinned,
        "tier": u.get("tier"), "verification_status": u.get("verification_status"),
        "active": bool(u.get("active")),
        "real_data_approved": bool(u.get("real_data_approved")),
        "real_data_approval_source": u.get("real_data_approval_source"),
    }
    report["notes"] = notes
    report["real_cases_they_can_draw"] = [t.get("task_id") for t in eligible]
    report["can_see_real_cases"] = bool(eligible)
    report["blockers"] = blockers
    if eligible and blockers:
        # Belt and braces: a gate list that says "blocked" over a non-empty queue
        # is a bug in this endpoint, and saying so beats quietly contradicting
        # ourselves on the screen an operator is trusting.
        report["note"] = ("The queue is not empty despite the blockers listed — "
                          "treat the queue as authoritative and report this.")
    elif not eligible and not blockers:
        report["note"] = ("No gate is blocking them and the queue is still empty: "
                          "every real case they can see is already at capacity or "
                          "already labeled by them.")
    return report


@router.get("/me/stats")
async def my_stats(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """This evaluator's own real numbers for the dashboard tracking widget:
    total cases completed, how many in the last 7 days, and their last
    submission timestamp. Every ``submissions`` row is already a completed
    case (drafts live client-side, never written here), so no status filter
    is needed."""
    store = _store()
    return store.evaluator_self_stats(user["id"])


def _require_real_data_access(task: Dict[str, Any], user: Dict[str, Any]) -> None:
    """The V4 wall on DIRECT task access (EHR PRD §9.5): a real (case_source=
    'real_deid') task is visible to admins/QA and to real_data_approved
    evaluators only. /tasks/next already filters; this closes the by-ID paths
    (fetch, reveal, answers, prelabel, submit) so the wall never depends on task
    IDs being unguessable."""
    if task.get("case_source") != "real_deid":
        return
    if user.get("role") in ("admin", "qa_reviewer"):
        return
    if not user.get("real_data_approved"):
        raise HTTPException(
            status_code=403,
            detail="This is a real-patient (V4) case; it requires real-data approval.",
        )


def _require_trajectory_sequence(store: Any, task: Dict[str, Any], user: Dict[str, Any]) -> None:
    """The sealed future on DIRECT task access (Longitudinal Cases PRD §9.1).

    A trajectory point is openable by this evaluator only once they have submitted
    every earlier point in the same chart walk. The labeler queue enforces the same
    rule in SQL (``store._PRD_2_SEQUENCE_GATE``); **a queue-only fix is not a fix**
    — the physician has the task id in the URL, the dashboard opens cases by id,
    and a second tab is a second draw. So this closes the by-ID paths (fetch,
    reveal, answers, prelabel, submit), exactly as ``_require_real_data_access``
    closes them for the V4 wall.

    **409, not 403.** This is not an authorization failure and must not read as
    one: the physician is fully entitled to this case, just not yet. A 403 would
    tell them their account is the problem and send them to support; a 409 with
    the sentence below tells them to finish the earlier steps.

    Admins and QA reviewers are NOT exempt. The exemption on the V4 wall exists
    because an admin can legitimately inspect real data; there is no equivalent
    argument here, because the harm is not disclosure — it is that reading forward
    destroys the physician's own prediction, and an admin who opens point 7 to
    check something has destroyed nothing except their own ability to label it. If
    an admin needs to see a trajectory whole, the admin trajectory view serves it
    without recording a submission.
    """
    if not asc_trajectory.is_trajectory_point(task):
        return
    idx = asc_trajectory.sequence_index(task)
    # §8.2 — the two modes ask different questions, and this path must ask the
    # SAME one the queue's WHERE clause asks or the URL and the draw disagree:
    # a point the queue offers would 409 when opened, or worse, one the queue
    # withholds would open by id. Solo asks "which earlier points has THIS
    # physician not done"; relay asks "how far has the CHART got" plus "is it
    # this physician's turn", because the previous point was somebody else's by
    # design.
    relay = asc_trajectory.is_relay(task)
    is_assignee = None
    if idx is None:
        pending = []
    elif relay:
        pending = store.unanswered_earlier_points_any(
            trajectory_id=task["trajectory_id"], sequence_index=idx)
        is_assignee = store.holds_label_assignment(
            task_id=task["task_id"], user_id=user["id"])
    else:
        pending = store.unanswered_earlier_points(
            trajectory_id=task["trajectory_id"], sequence_index=idx,
            evaluator_id=user["id"])
    reason = asc_trajectory.blocks_out_of_order(
        task, unanswered_earlier=pending, is_assignee=is_assignee)
    if reason is None:
        return
    progress = store.evaluator_trajectory_progress(
        trajectory_id=task["trajectory_id"], evaluator_id=user["id"])
    raise HTTPException(
        status_code=409,
        detail={
            "error": "trajectory_out_of_order",
            "message": reason,
            "trajectory_id": task["trajectory_id"],
            "sequence_index": idx,
            # The next point they MAY open, so the client can offer a way forward
            # rather than a dead end.
            "next_task_id": progress.get("next_task_id"),
            "n_points": progress.get("n_points"),
            "n_answered": progress.get("n_answered"),
        },
    )


def _attach_relay_handoff(store: Any, task: Dict[str, Any], out: Dict[str, Any]) -> None:
    """The predecessor's commitment, above the clinical question (§8.4).

    Doctor k on a relay reads doctor k−1's committed assessment before writing
    their own, exactly as a care-team handoff works. That is the product: how
    clinicians build on each other's reasoning.

    WHAT IS NOT IN IT, and why it is built from named columns rather than a row
    dump: the predecessor's REVEAL OUTCOME and their SELF-SCORE. Those are what
    this physician is being asked to predict. Handing them over turns the relay
    into reading comprehension and destroys the verifiable claim for this point —
    the same unrecoverable loss the sequence gate exists to prevent, arriving
    through a different door. ``store.relay_handoff`` selects the commit columns
    by name so a future column cannot ride along, and a test asserts the served
    payload carries no reveal or self-score key.

    Point 0 has no handoff. Solo walks have none either: the predecessor there is
    the same physician, who does not need to be handed their own note.
    """
    if not asc_trajectory.is_relay(task):
        return
    idx = asc_trajectory.sequence_index(task)
    if idx is None or idx <= 0:
        return
    handoff = store.relay_handoff(
        trajectory_id=task["trajectory_id"], sequence_index=idx)
    if not handoff:
        return
    # The author is named by POSITION, not by identity. Who a physician is is not
    # this physician's business (the whole platform is blinded between labelers),
    # and "the physician before you on this chart" is the entire clinically
    # relevant fact.
    expected = handoff.get("expected_trajectory") or {}
    out["relay_handoff"] = {
        "from_sequence_index": handoff["from_sequence_index"],
        "from_label": f"the physician at decision {int(handoff['from_sequence_index']) + 1}",
        "assessment": handoff.get("assessment") or "",
        "expectations": [e.get("expectation") for e in (expected.get("expectations") or [])
                         if isinstance(e, dict) and e.get("expectation")],
        "falsifiers": list(expected.get("falsifiers") or []),
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_real_data_access(task, user)
    _require_trajectory_sequence(store, task, user)
    # The flow this task is actually graded in, derived from the TASK on the same
    # rule the submit path enforces. Opening a case from the dashboard list skips
    # /tasks/next, so without this the client stamped the draft from whatever
    # version the picker held — and a v4 picker on a synthetic card produced a
    # draft whose own submission is a 400 at ``_derive_portal_version``. The
    # server owns the answer here, exactly as it does at /tasks/next.
    out = {"task": _blind_task(task),
           "served_portal_version": _derive_portal_version(task, None)}
    _attach_relay_handoff(store, task, out)
    return out


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
    _require_real_data_access(task, user)
    _require_trajectory_sequence(store, task, user)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "independent_answer_required",
                "message": "Write your independent answer before revealing the AI answers.",
            },
        )
    # The contributor's portal version drives the capture kind (single source of
    # truth in constants): V1 always commits a full blind ideal answer; V3
    # commits the ~10s instinct one-liner (unless the admin marked the task
    # 'full'); V2 respects the task's mode (stance default). ``kind`` is stamped
    # server-side, never trusted from the client — a lightweight capture can't be
    # passed off as a full blind ideal answer.
    pv = _derive_portal_version(task, body.portal_version)
    kind = independent_capture_kind(pv, task.get("independent_mode"))
    store.commit_independent_answer(
        task_id=task_id,
        evaluator_id=user["id"],
        payload={
            "text": text,
            "kind": kind,
            "portal_version": pv,
            "evidence_anchor": body.evidence_anchor.model_dump() if body.evidence_anchor else None,
            # Multi-anchor (BUG-3b): persist the full citation list on the committed
            # answer too, else packaging (which reads the AUTHORITATIVE commit, not
            # the post-reveal submission) would silently drop the extra citations.
            "evidence_anchors": [a.model_dump() for a in (body.evidence_anchors or [])],
        },
    )
    store.log_event(
        entity_type="task", entity_id=task_id,
        event_type="independent_answer_committed", actor=user["id"],
    )
    return {"answers": _task_answers(task), "committed": True}


def _derive_portal_version(task: Dict[str, Any], claimed: Optional[str]) -> str:
    """The derivation wall (EHR PRD §9.5; Longitudinal E2E PRD §5.1): the portal
    version stamped onto a commit/submission is DERIVED from the task, never
    trusted from the client, at every stamping point (reveal, submit, stage-1
    flags).

    Three walls, one function, because they are one rule about what a task IS:

      * trajectory point (``trajectory_id IS NOT NULL``) → always ``v5``. These are
        real de-identified data AND sequential, and the sequence is what makes them
        a different product: single-labelled, κ-excluded by construction, sealed in
        order, sold as a chart walk. Stamping one ``v4`` would file a walk's points
        as unrelated static cases in the buyer's bundle and lose the reassembly key.
      * real static task (``case_source='real_deid'`` with no trajectory) → always
        ``v4``, exactly as before.
      * synthetic/text task → the claimed version, with an explicit ``v4`` or
        ``v5`` claim a 400 (a synthetic task is neither).

    ``env`` never appears here at all. An env rollout is not a submission — it
    lives in ``env_runs`` — so a claim of it on this path is a client bug, and it
    is REJECTED rather than normalized: quietly stamping it ``v3`` would attribute
    agentic work to V3 and corrupt the buyer's provenance.
    """
    is_trajectory = asc_trajectory.is_trajectory_point(task)
    is_real = (task.get("case_source") == "real_deid")
    if is_trajectory:
        # Claiming ANY other single-turn version on a trajectory point is a
        # mislabel attempt, and it is refused for both directions of harm: v1–v3
        # would put real patient data in a synthetic bundle, and v4 would break the
        # walk apart. The wall is on the task's shape, so no client can talk its
        # way past it.
        if claimed is not None and claimed != LONGITUDINAL_PORTAL_VERSION:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This is a longitudinal (V5) decision point: portal_version "
                    f"{claimed!r} is not valid for it. A point of a chart walk is "
                    "graded only in the V5 flow."
                ),
            )
        return LONGITUDINAL_PORTAL_VERSION
    if is_real:
        if claimed in SYNTHETIC_PORTAL_VERSIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This is a real-case (V4) task: portal_version "
                    f"{claimed!r} is not valid for it. Real de-identified cases are "
                    "graded only in the V4 flow."
                ),
            )
        # A v5 claim on a real STATIC case is the mirror of the v4 claim below, and
        # refused for the same reason: V5 means "a point in an ordered walk", and a
        # case with no trajectory has no walk to be a point of.
        if claimed == LONGITUDINAL_PORTAL_VERSION:
            raise HTTPException(
                status_code=400,
                detail=("portal_version 'v5' is reserved for longitudinal decision "
                        "points; this real case is not part of a trajectory."),
            )
        return REAL_CASE_PORTAL_VERSION
    if claimed == REAL_CASE_PORTAL_VERSION:
        raise HTTPException(
            status_code=400,
            detail="portal_version 'v4' is reserved for real-case tasks; this task is synthetic.",
        )
    if claimed == LONGITUDINAL_PORTAL_VERSION:
        raise HTTPException(
            status_code=400,
            detail="portal_version 'v5' is reserved for longitudinal decision points; "
                   "this task is synthetic and carries no trajectory.",
        )
    if claimed == ENV_PORTAL_VERSION:
        raise HTTPException(
            status_code=400,
            detail=("portal_version 'env' is the agentic environments tier; it is not a "
                    "single-turn evaluation flow and never stamps a submission. "
                    "Use /api/asclepius/environments/*."),
        )
    pv = normalize_portal_version(claimed)
    # v5 is single-turn now, so the tuple alone would let a synthetic task through
    # as v5 if ``claimed`` somehow reached here — it cannot (the explicit claim is a
    # 400 above and normalize only ever returns the claim or the default), and the
    # guard stays anyway because this line is the last thing between a client string
    # and a buyer-facing provenance field.
    if pv in (REAL_CASE_PORTAL_VERSION, LONGITUDINAL_PORTAL_VERSION):
        return DEFAULT_PORTAL_VERSION
    return pv if pv in SINGLE_TURN_PORTAL_VERSIONS else DEFAULT_PORTAL_VERSION


def _require_independent_commit(store: Any, task_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """The v2 anti-peeking gate, shared by every endpoint that describes the
    candidate answers (answer re-fetch, prelabel suggestions): the evaluator
    must have committed their blind independent capture first. One policy, one
    place — a hardening change here covers every answer-describing surface."""
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_real_data_access(task, user)  # V4 wall on answer-describing surfaces
    _require_trajectory_sequence(store, task, user)  # PRD 2 §9.1 sealed future
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


# ─── Longitudinal trajectories (PRD 2 Phases 4 + 5) ───────────────────────────
def _trajectory_submission(store: Any, task: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """This evaluator's submission on this decision point, or a 409.

    **The seal.** The outcome is revealed only after the action is committed, and a
    committed action means a stored submission — not a draft, not an independent
    answer, not a client assertion that it is ready. If the physician could see the
    future before committing, the task collapses into narration: the seal is what
    converts an opinion into a prediction, and a prediction is the only thing an
    outcome can verify (§3.2).
    """
    for sub in store.submissions_for_task(task["task_id"]):
        if sub.get("evaluator_id") == user["id"]:
            return sub
    raise HTTPException(
        status_code=409,
        detail={
            "error": "commitment_required",
            "message": "Submit your assessment, plan and expected trajectory before "
                       "the next encounter is revealed. Seeing what happened first "
                       "would make this a summary rather than a prediction.",
        },
    )


def _outcome_point(store: Any, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The next point in this walk — the encounter that verifies this decision.

    The point with the SMALLEST sequence index greater than this one, not
    ``idx + 1``. A walk can have a hole in it: generation is per-point isolated so
    one encounter failing its case judge cannot fail the batch, and an admin can
    retire a point later. Matching ``idx + 1`` exactly would make the point BEFORE
    a hole report "this is the last decision point in this chart" — false, and
    false in the direction that silently drops a verifiable point from the corpus.

    The wider window that results is still a truthful outcome, and
    ``days_after_decision`` states the gap it actually covers.
    """
    idx = asc_trajectory.sequence_index(task)
    if idx is None:
        return None
    later = [p for p in store.trajectory_points(task.get("trajectory_id"))
             if isinstance(p.get("sequence_index"), int) and p["sequence_index"] > idx]
    return later[0] if later else None


@router.get("/tasks/{task_id}/trajectory-outcome")
async def trajectory_outcome(
    task_id: str, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Reveal encounter *k+1* — the answer key the chart itself wrote (§4 Phase 4).

    Returns what the record added AFTER this decision point, dated from the moment
    the physician committed ("day +12"), together with the expectations they sealed,
    so they can mark which held. Their own falsifier is the rubric; no reviewer
    grades this and no model does.

    WHAT THIS REVEALS, EXACTLY. The window runs from just after this decision point
    up to and including the NEXT decision point in the walk. That is the presenting
    data of encounter *k+1* — the GGT back at 983 — and not its resolution, which
    belongs to the point after. Said plainly here rather than described as "the next
    encounter", because a physician marking an expectation ``not_assessable`` needs
    to know whether the observation is genuinely absent from the record or merely
    beyond this window.

    Also honest about what it is NOT: what happened next reflects the treatment
    actually given, not the plan this physician proposed. Where they proposed
    something different, this does not test their plan — it tests the one that was
    followed (§6). The self-score therefore asks about ANTICIPATION of the observed
    course, never about counterfactual outcomes.
    """
    from asclepius import real_cases

    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_real_data_access(task, user)
    if not asc_trajectory.is_trajectory_point(task):
        raise HTTPException(
            status_code=404,
            detail="This case is not part of a longitudinal trajectory, so there "
                   "is no next encounter to reveal.")
    # NOTE: the sequence gate is deliberately NOT applied here. Reaching this point
    # requires a stored submission on THIS task (below), and the gate already made
    # that submission impossible out of order. Re-applying it would only add a way
    # for the reveal to refuse a physician their own committed work.
    submission = _trajectory_submission(store, task, user)

    outcome_task = _outcome_point(store, task)
    if outcome_task is None:
        return {
            "task_id": task_id,
            "trajectory_id": task.get("trajectory_id"),
            "sequence_index": task.get("sequence_index"),
            "outcome": None,
            # The terminal point of a walk. Named, not silently empty: a walk of N
            # points yields N−1 verifiable ones, and a physician who reaches the
            # end should be told that rather than left looking at a blank panel.
            "reason": "This is the last decision point in this chart. There is no "
                      "later encounter in the record to check it against.",
            "expected_trajectory": submission.get("expected_trajectory"),
            "self_score": submission.get("trajectory_self_score"),
        }

    decision_offset = ((task.get("generation") or {}).get("index_event_offset"))
    outcome_offset = ((outcome_task.get("generation") or {}).get("index_event_offset"))
    try:
        delta = real_cases.outcome_delta(
            asc_cases.public_case(outcome_task.get("case")),
            outcome_index_offset=outcome_offset,
            decision_index_offset=decision_offset,
        )
    except real_cases.RealCaseError as exc:
        # FAIL CLOSED and say so. The alternative — serving the outcome case whole
        # — would show the physician chart state they had already read as if it
        # were new, and could reach back BEFORE their own decision point.
        raise HTTPException(status_code=409, detail={
            "error": "outcome_not_reconstructible", "message": str(exc)})

    store.log_event(
        entity_type="task", entity_id=task_id, event_type="trajectory_outcome_revealed",
        actor=user["id"],
        payload={"trajectory_id": task.get("trajectory_id"),
                 "sequence_index": task.get("sequence_index"),
                 "outcome_task_id": outcome_task["task_id"],
                 "days_after_decision": delta.get("days_after_decision"),
                 "n_events": delta.get("n_events")},
    )
    return {
        "task_id": task_id,
        "trajectory_id": task.get("trajectory_id"),
        "sequence_index": task.get("sequence_index"),
        "outcome": delta,
        "outcome_task_id": outcome_task["task_id"],
        # The physician's own sealed prediction, handed back so the client scores
        # against what was actually stored rather than against a local draft that
        # may have been edited after the commit.
        "expected_trajectory": submission.get("expected_trajectory"),
        "self_score": submission.get("trajectory_self_score"),
        "submission_id": submission.get("submission_id"),
        # §6, in front of the physician at the moment they grade, not only in the
        # data dictionary a buyer reads.
        "limitations": asc_trajectory.limitations_block(),
    }


@router.post("/tasks/{task_id}/trajectory-self-score")
async def trajectory_self_score(
    task_id: str, body: TrajectorySelfScore,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Record which of the physician's own expectations held (§4 Phase 4).

    This is the third signal — the one that is automatic and free of human grading,
    and therefore the one that scales past the constraint the whole business runs
    into: subspecialist hours. It is written from the physician's own submission
    and graded against their own stated falsifier.

    Marks are bounded by the prediction they grade: a mark pointing past the
    committed expectations is dropped, because a self-score that does not line up
    with the commitment it scores is not evidence of anything. A physician with no
    stored prediction cannot self-score at all — there would be nothing to grade.
    """
    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_real_data_access(task, user)
    submission = _trajectory_submission(store, task, user)
    expected = submission.get("expected_trajectory") or {}
    n_expected = len(expected.get("expectations") or [])
    if not n_expected:
        raise HTTPException(status_code=409, detail={
            "error": "no_prediction_to_score",
            "message": "You did not record an expected trajectory on this case, so "
                       "there is nothing here to check against the record."})
    score = asc_trajectory.normalize_self_score(body.model_dump(), n_expectations=n_expected)
    if score is None:
        raise HTTPException(status_code=400, detail={
            "error": "no_usable_marks",
            "message": "Mark at least one expectation as held, did not hold, or not "
                       "assessable from this encounter."})
    store.set_submission_trajectory_self_score(submission["submission_id"], score)
    store.log_event(
        entity_type="submission", entity_id=submission["submission_id"],
        event_type="trajectory_self_scored", actor=user["id"],
        payload={"task_id": task_id, "trajectory_id": task.get("trajectory_id"),
                 "sequence_index": task.get("sequence_index"),
                 "n_held": score["n_held"], "n_did_not_hold": score["n_did_not_hold"],
                 "n_not_assessable": score["n_not_assessable"],
                 "falsifier_fired": score["falsifier_fired"]},
    )
    progress = store.evaluator_trajectory_progress(
        trajectory_id=task["trajectory_id"], evaluator_id=user["id"])
    return {"self_score": score, "progress": progress}


@router.get("/trajectories/{trajectory_id}")
async def get_trajectory(
    trajectory_id: str, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """This evaluator's walk through one chart (§4 Phase 5 — trajectory mode).

    The session view: how many decision points this chart carries, how far this
    physician has come, and which point they may open next. Per-evaluator on
    purpose — two physicians walking the same chart are two independent
    trajectories that happen to share a case set, and a shared "7 of 13" would be a
    lie to both of them.

    Returns METADATA ONLY. No case content, no prompts, no outcomes: the sequence
    gate exists precisely so a physician cannot read ahead, and a session view that
    rendered every point's chart would be that leak wearing a progress bar. Each
    point is opened through the ordinary by-ID path, which re-checks the gate.
    """
    store = _store()
    points = store.trajectory_points(trajectory_id)
    if not points:
        raise HTTPException(status_code=404, detail="Trajectory not found")
    # The V4 wall applies to the walk exactly as it applies to each case in it.
    _require_real_data_access(points[0], user)
    progress = store.evaluator_trajectory_progress(
        trajectory_id=trajectory_id, evaluator_id=user["id"])
    answered = set(progress.get("answered_task_ids") or ())
    return {
        "trajectory_id": trajectory_id,
        "specialty": points[0].get("specialty"),
        "points": [
            {
                "task_id": p["task_id"],
                "sequence_index": p.get("sequence_index"),
                "difficulty": p.get("difficulty"),
                "answered": p["task_id"] in answered,
                # Openable RIGHT NOW by this evaluator: answered points stay
                # readable, the next unanswered one is open, everything past it is
                # sealed. Derived from the same rule the gate enforces so the UI
                # cannot advertise a card the next click refuses.
                "openable": (p["task_id"] in answered
                             or p["task_id"] == progress.get("next_task_id")),
                # A walk of N points yields N−1 verifiable ones: the terminal point
                # has no later encounter to be checked against.
                "outcome_verifiable": p.get("sequence_index") is not None
                and p.get("sequence_index") < len(points) - 1,
            }
            for p in points
        ],
        "progress": progress,
        "limitations": asc_trajectory.limitations_block(),
        "kappa_exclusion": asc_trajectory.KAPPA_EXCLUSION_RATIONALE,
    }


# ─── Submissions ──────────────────────────────────────────────────────────────
@router.post("/submissions")
async def submit(
    body: SubmissionIn,
    background: BackgroundTasks,
    async_pipeline: bool = Query(
        False,
        description="Real submit progress (BUG-5): when true, return 202 + "
        "submission_id immediately and run the pipeline as a background job; poll "
        "GET /submissions/{id}/status for backend-stamped phases. Default false "
        "keeps the synchronous 200 + result behavior.",
    ),
    user: Dict[str, Any] = Depends(require_label),
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
    _require_real_data_access(task, user)  # V4 wall on the submit path
    # PRD 2 §9.1 on the submit path too. Not belt-and-braces: a client that
    # obtained the case some other way (a stale tab, a hand-written POST) must not
    # be able to bank a label on an out-of-order point, because that submission is
    # what the NEXT point's gate reads to decide the walk has advanced.
    _require_trajectory_sequence(store, task, user)

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
        safe_note = "[redacted: possible identifier detected]" if note_phi else review.note
        flag_pv = _derive_portal_version(task, body.portal_version)
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

    # Stage-1 "not actually hard" flag (Seamless PRD WS2): the prompt is clinically
    # valid but not a genuinely hard case. Route it out of the hard-case queue and
    # feed the signal back to recalibrate the hardness judge/corpus (human-in-the-
    # loop hardness curation). No records; the doctor advances.
    if review and review.verdict == "not_hard":
        # PHI scan on the reason (this path skips validate_submission, but the
        # "PHI scan on every submission" rule still applies) — redact, don't persist.
        nh_note_phi = residual_identifiers(review.note) if review.note else []
        nh_safe_note = "[redacted: possible identifier detected]" if nh_note_phi else review.note
        nh_pv = _derive_portal_version(task, body.portal_version)
        nh_payload = body.model_dump()
        nh_payload["portal_version"] = nh_pv
        if nh_note_phi:
            (nh_payload.get("prompt_review") or {})["note"] = nh_safe_note
        store.insert_submission(
            submission_id=sid, task_id=body.task_id, evaluator_id=user["id"],
            verdict=None, chosen_id=None, rejected_id=None, confidence=body.confidence,
            time_spent_sec=body.time_spent_sec, payload=nh_payload,
            annotator=store.annotator_block(user), dedupe_hash=None, grounded=False,
            grounding_mode=task.get("grounding_mode") or "optional",
            portal_version=nh_pv, status=NOT_HARD_TASK_STATUS,
        )
        store.mark_task_status(body.task_id, NOT_HARD_TASK_STATUS)
        store.log_event(
            entity_type="task", entity_id=body.task_id, event_type="prompt_not_hard",
            actor=user["id"],
            payload={"submission_id": sid, "hardness": (task.get("generation") or {}).get("hardness"),
                     "note": nh_safe_note, "phi_redacted": bool(nh_note_phi)},
        )
        return {
            "submission_id": sid, "status": NOT_HARD_TASK_STATUS,
            "issues": [], "record_count": 0, "critic": None, "agreement_score": None,
        }

    # Stage-1 "case internally inconsistent" flag (Multimodal PRD §5): the human
    # counterpart to the case-judge coherence gate. Route the case out and feed the
    # signal back to recalibrate case generation. No records; the doctor advances.
    if review and review.verdict == "case_incoherent":
        ci_note_phi = residual_identifiers(review.note) if review.note else []
        ci_safe_note = "[redacted: possible identifier detected]" if ci_note_phi else review.note
        ci_pv = _derive_portal_version(task, body.portal_version)
        ci_payload = body.model_dump()
        ci_payload["portal_version"] = ci_pv
        if ci_note_phi:
            (ci_payload.get("prompt_review") or {})["note"] = ci_safe_note
        store.insert_submission(
            submission_id=sid, task_id=body.task_id, evaluator_id=user["id"],
            verdict=None, chosen_id=None, rejected_id=None, confidence=body.confidence,
            time_spent_sec=body.time_spent_sec, payload=ci_payload,
            annotator=store.annotator_block(user), dedupe_hash=None, grounded=False,
            grounding_mode=task.get("grounding_mode") or "optional",
            portal_version=ci_pv, status=CASE_INCOHERENT_TASK_STATUS,
        )
        store.mark_task_status(body.task_id, CASE_INCOHERENT_TASK_STATUS)
        store.log_event(
            entity_type="task", entity_id=body.task_id, event_type="prompt_case_incoherent",
            actor=user["id"],
            payload={"submission_id": sid, "case_id": (task.get("case") or {}).get("case_id"),
                     "case_judge": (task.get("generation") or {}).get("case_judge"),
                     "note": ci_safe_note, "phi_redacted": bool(ci_note_phi)},
        )
        return {
            "submission_id": sid, "status": CASE_INCOHERENT_TASK_STATUS,
            "issues": [], "record_count": 0, "critic": None, "agreement_score": None,
        }

    if body.verdict not in VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid verdict")
    if body.confidence not in CONFIDENCE_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid confidence")

    payload = body.model_dump()

    # §13 (Eval UX Overhaul): derive step_error_tag (and, for note-only corrected
    # steps, the correction_reason + label) from the physician's free-text
    # step_note — BEFORE validation, so a V3/V4 note-only correction never routes
    # to QA as missing_correction_reason. No-op when no step carries a note.
    from asclepius.packaging import apply_step_notes
    apply_step_notes(payload.get("reasoning_steps"))
    apply_step_notes((payload.get("from_scratch") or {}).get("reasoning_steps"))

    # The sealed prediction (PRD 2 §3.3 field 3 / §4.2.3), normalized BEFORE the
    # row is written so ``payload_json`` and the ``expected_trajectory_json``
    # column carry the same object. Normalizing after the insert would leave the
    # packaged record showing the client's raw block and the corpus query showing
    # the cleaned one — two versions of the physician's own prediction, which is
    # the last field in this product that should have two versions.
    #
    # Captured on EVERY task, not only trajectory points: a physician who names
    # what they expect to see on an ordinary V4 case has written the same
    # falsifiable object, and discarding it because the case is not part of a walk
    # would throw away the exact artifact §7 prices. ``normalize_expected_trajectory``
    # returns None for anything that is not a usable prediction, and None is stored
    # as None — never an empty shell that would inflate the corpus count.
    _expected = asc_trajectory.normalize_expected_trajectory(payload.get("expected_trajectory"))
    payload["expected_trajectory"] = _expected

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
    # onto every record. The V4 derivation wall (EHR PRD §9.5) applies HERE too:
    # a real-case task derives v4 (a synthetic-version claim on it is a 400),
    # and a synthetic task can never stamp v4 — even via a stale commit.
    portal_version = _derive_portal_version(
        task, (_commit or {}).get("payload", {}).get("portal_version") or body.portal_version
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

    # Critical-negative gate (Two-Model PRD Workstream B): on V3/V4, a captured
    # rubric MUST name at least one CRITICAL NEGATIVE — the one thing a correct
    # answer must never do. Scoped to portal_version ∈ {v3,v4} (NOT isAssisted(),
    # which also matches v2) so V1/V2 stay byte-for-byte unchanged.
    #
    # GUARDRAIL: ``portal_version`` DEFAULTS to v3 when omitted, so gating on the
    # DERIVED value alone would newly 400 a legacy/direct API client that omits the
    # field and posts a rubric — a wire-contract regression. So we gate only when v3/v4
    # is UNAMBIGUOUS: either the client EXPLICITLY claimed v3/v4, or the task is a real
    # de-identified case (which can only ever be v4). An omitted-version submit on a
    # synthetic task is never gated. And we test the NORMALIZED rubric so empty-text /
    # zero-point rows (which package to nothing) don't trip the gate.
    claimed_pv = (_commit or {}).get("payload", {}).get("portal_version") or body.portal_version
    v34_unambiguous = claimed_pv in ("v3", "v4") or task.get("case_source") == "real_deid"
    from asclepius.rubric import has_critical_negative, normalize_rubric
    _rubric_has_crit_neg = v34_unambiguous and bool(normalize_rubric(payload.get("rubric"))) \
        and has_critical_negative(payload.get("rubric"))
    if v34_unambiguous and normalize_rubric(payload.get("rubric")):
        if not has_critical_negative(payload.get("rubric")):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "critical_negative_required",
                    "message": "Your scoring rubric must include at least one CRITICAL negative "
                               "criterion (points −8 to −10): the single thing a correct answer "
                               "must never do. Mark your most serious 'never' criterion as critical.",
                },
            )

    # Model-Failure Taxonomy gate (PRD §D-2): on V3/V4, when the rubric names a critical
    # negative AND this is a real-model A/B pair (grade-real-models), require ≥1 physician
    # failure tag on the REJECTED answer — the taxonomy is only valuable if labels are
    # physician-verified. Reuses the critical-negative machinery; only fires for an A/B
    # verdict on a baseline pair (never over-gates the from-scratch / flagged paths).
    if _rubric_has_crit_neg and body.verdict in ("A_better", "B_better"):
        _is_baseline_pair = any((c.get("source") == "baseline")
                                for c in (task.get("candidate_answers") or []))
        _failure_tags = ((payload.get("rejected_critique") or {}).get("failure_tags")) or []
        if _is_baseline_pair and not _failure_tags:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "failure_tag_required",
                    "message": "Tag at least one failure mode on the rejected answer (how it failed) "
                               "so the model-failure taxonomy is physician-verified, for example anchoring, "
                               "premature closure, unsafe recommendation.",
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

    # Decisive action (Audit §13): if the clinician named the verifiable outcome —
    # the test/action the correct answer depends on — persist it onto the task so
    # every packaged record from it ships an environment-verifiable outcome. Written
    # ONLY from the physician's own submission, never by an admin or a model.
    if _expected:
        # The column, alongside the payload. The payload is what packaging reads;
        # the column is what the falsifier corpus and the outcome-verification
        # metric query, and it is indexed-adjacent to the task's trajectory
        # columns. Both carry the SAME normalized object — normalization happened
        # before the row was written, precisely so the two cannot disagree.
        store.set_submission_expected_trajectory(sid, _expected)
        store.log_event(
            entity_type="submission", entity_id=sid,
            event_type="expected_trajectory_committed", actor=user["id"],
            payload={"task_id": body.task_id,
                     "trajectory_id": task.get("trajectory_id"),
                     "sequence_index": task.get("sequence_index"),
                     "n_expectations": len(_expected["expectations"]),
                     "falsifiable": _expected["falsifiable"]},
        )

    da = payload.get("decisive_action") or {}
    da_action = str(da.get("action") or "").strip()
    if da_action and len(da_action.split()) >= 2:
        store.set_task_decisive_action(body.task_id, {
            "action": da_action,
            "tool_name": (da.get("tool_name") or "").strip() or None,
            "must_precede_final_answer": bool(da.get("must_precede_final_answer", True)),
            "rationale": (da.get("rationale") or "").strip(),
            "physician_authored": True,
            "author_id_hashed": user["id_hashed"],
            "authored_at": _utcnow_iso(),
        })

    # Real submit progress (BUG-5): run the genuinely-slow multi-stage pipeline
    # (validate → package → LLM consistency → LLM grounding → agreement → store)
    # as a BACKGROUND job when the client opts in, returning 202 + submission_id
    # immediately so the UI can poll GET /submissions/{id}/status for real,
    # backend-stamped phases. The default path stays synchronous (200 + result)
    # so existing API clients are unchanged.
    if async_pipeline:
        store.set_submission_progress(sid, phase="queued", pct=5, detail="Queued for processing")
        background.add_task(_finalize_submission, store, body.task_id, sid, user["id"])
        return JSONResponse(
            status_code=202,
            content={"submission_id": sid, "status": "processing", "accepted": True},
        )

    result = await _finalize_submission(store, body.task_id, sid, user["id"])
    return result


async def _finalize_submission(
    store: Any, task_id: str, sid: str, actor_id: str
) -> Dict[str, Any]:
    """Run the full submission pipeline + post-processing for a captured submission
    row. Shared by the synchronous submit path and the background job (BUG-5), so
    the two can never drift. Re-reads the task/submission fresh (safe for a
    background run that starts after the HTTP response)."""
    task = store.get_task(task_id)
    submission = store.get_submission(sid)
    if not task or not submission:
        # Stamp a TERMINAL progress even on this narrow "row vanished" case so an
        # async poller never hangs waiting for a submission that can't finish.
        try:
            store.set_submission_progress(sid, phase="error", pct=100, detail="Submission or task missing")
        except Exception:
            pass
        return {"submission_id": sid, "status": "error", "issues": ["submission_or_task_missing"],
                "record_count": 0}

    try:
        result = await asc_pipeline.process_submission(store, task, submission)

        # If another clinician already flagged this prompt as invalid, a grading that
        # races in afterward must not silently export (Eval Flow Upgrade §2). Route it
        # to QA instead of auto-export — never lose the work, never ship a flagged
        # prompt's records. Re-read the task so a flag committed during processing is
        # seen. (refresh_task_status leaves the prompt_flagged task as-is.)
        _cur = store.get_task(task_id) or {}
        if _cur.get("status") == PROMPT_FLAGGED_TASK_STATUS and result.get("status") in ("auto_validated", "export_ready"):
            store.update_submission(sid, status="needs_qa", qa_reason="prompt_flagged")
            store.update_records_status_for_submission(sid, "needs_qa")
            store.set_submission_progress(sid, phase="needs_qa", pct=100, detail="Routed to QA review")
            store.log_event(
                entity_type="submission", entity_id=sid, event_type="routed_to_qa",
                actor=actor_id, payload={"reason": "prompt_flagged", "task_id": task_id},
            )
            result["status"] = "needs_qa"
            result["issues"] = sorted(set((result.get("issues") or []) + ["prompt_flagged"]))

        # Frontier-model failure capture (FEAT-1): if this task's A/B pair was real
        # frontier answers, persist the per-model failure record (which model was
        # rejected + the expert correction). No-op for a normal generated pair.
        from asclepius import baselines as asc_baselines
        asc_baselines.record_model_failure(store, task_id, sid)

        store.refresh_task_status(task_id)
        # A completed case was the one notable thing on this product that logged
        # nothing at all: the flagged paths above each record why they were
        # flagged, and an ordinary good submission passed through silently. This
        # is the moment the pipeline has settled and the status is real, which is
        # also what makes it the right hook for the founder alert.
        try:
            store.log_event(
                entity_type="submission", entity_id=sid,
                event_type="submission_completed", actor=actor_id,
                payload={"task_id": task_id, "status": result.get("status"),
                         "record_count": result.get("record_count")},
            )
        except Exception:
            log.exception("asclepius: could not log submission completion for %s", sid)
        # §8.6 — the relay unlock ping. Fired HERE because this is the moment the
        # relay gate starts letting the next point through: the message and the
        # availability are one event, rather than a sweep noticing an hour later
        # and a physician finding work that has been sitting there. Best-effort by
        # the same rule as every other ping — ``notify_relay_unlock`` swallows its
        # own failures, and a community outage must never cost somebody a
        # submission that has already been accepted and packaged.
        #
        # AFTER the completion log, not before: both are post-pipeline hooks at the
        # same seam and they are independent, but the audit line is the one that
        # must exist even if the other throws its way out of the try above.
        asc_route_notify.notify_relay_unlock(store, task=store.get_task(task_id))
        return result
    except Exception:
        # BUG-5 review (3b): the pipeline runs as a BACKGROUND job in the async
        # path, so an unexpected exception here would die silently and strand the
        # submission at a NON-terminal status ('submitted'/'auto_validated') — the
        # poller would hang forever. Route to QA (no lost submissions — the core
        # invariant) and stamp a TERMINAL phase so the poller always resolves. The
        # synchronous path returns this same needs_qa result instead of a 500,
        # which is strictly better: the work is captured, not lost.
        log.exception("asclepius: submission pipeline failed for %s", sid)
        try:
            store.update_submission(sid, status="needs_qa", qa_reason="pipeline_error")
            store.update_records_status_for_submission(sid, "needs_qa")
            store.set_submission_progress(sid, phase="needs_qa", pct=100, detail="Routed to QA (pipeline error)")
            store.log_event(
                entity_type="submission", entity_id=sid, event_type="routed_to_qa",
                actor=actor_id, payload={"reason": "pipeline_error", "task_id": task_id},
            )
            store.refresh_task_status(task_id)
        except Exception:
            log.exception("asclepius: could not stamp terminal state for %s", sid)
        return {
            "submission_id": sid, "status": "needs_qa", "issues": ["pipeline_error"],
            "record_count": len(store.records_for_submission(sid)),
            "critic": None, "agreement_score": None,
        }


# Terminal submission statuses — the poller stops once the row reaches one of
# these. NOTE: ``auto_validated`` is deliberately EXCLUDED — it is a TRANSIENT
# status the pipeline sets before running the LLM consistency + grounding checks
# (seconds of work in production). Treating it as terminal would let the client
# stop polling mid-pipeline and report the wrong final status (BUG-5 review).
_SUBMISSION_DONE_STATUSES = (
    "export_ready", "exported", "needs_qa", "rejected",
    "prompt_flagged", "not_hard", "case_incoherent",
)


@router.get("/submissions/{submission_id}/status")
async def submission_status(
    submission_id: str, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Real submit progress (BUG-5): the phase + pct the backend STAMPED as each
    pipeline stage actually started — never an invented percentage. Pollable by
    the submitting evaluator (or admin/QA). Returns ``done=true`` once the row
    reaches a terminal status, with the final status + issues so the client can
    take the existing success / needs-QA path."""
    store = _store()
    sub = store.get_submission(submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    # Only the owning evaluator (or admin/QA) may poll a submission's progress.
    if sub.get("evaluator_id") != user["id"] and user.get("role") not in ("admin", "qa_reviewer"):
        raise HTTPException(status_code=403, detail="Not your submission")
    status = sub.get("status")
    progress = sub.get("progress") or {}
    done = status in _SUBMISSION_DONE_STATUSES
    # A phase not yet stamped (row just inserted) reads as "queued"; a terminal
    # status without a 100% stamp still reports done so the client never hangs.
    phase = progress.get("phase") or ("complete" if done else "queued")
    pct = progress.get("pct")
    if pct is None:
        pct = 100 if done else 5
    issues = (sub.get("qa_reason") or "").split(",") if sub.get("qa_reason") else []
    return {
        "submission_id": submission_id,
        "status": status,
        "phase": phase,
        "pct": pct,
        "detail": progress.get("detail"),
        "done": done,
        "issues": [i for i in issues if i],
        "record_count": len(store.records_for_submission(submission_id)),
        "critic": sub.get("critic"),
        "agreement_score": sub.get("agreement_score"),
    }


# ─── Frontier-model failure capture (FEAT-1) ──────────────────────────────────
@router.post("/tasks/{task_id}/baselines")
async def run_task_baselines(
    task_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Answer this task's rendered case COLD with each configured frontier model
    (ASCLEPIUS_BASELINE_MODELS) and store the verbatim responses (FEAT-1). The
    on-policy artifact that proves the case is hard."""
    from asclepius import baselines as asc_baselines

    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    runs = await asc_baselines.run_baselines(store, task)
    store.log_event(entity_type="task", entity_id=task_id, event_type="baselines_run",
                    actor=admin["id"], payload={"models": [r.get("model") for r in runs]})
    # Never leak raw baseline text into logs; the response is admin-only anyway.
    return {"task_id": task_id, "runs": runs}


@router.get("/tasks/{task_id}/baselines")
async def list_task_baselines(
    task_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    return {"task_id": task_id, "runs": _store().list_baseline_runs(task_id=task_id)}


@router.post("/tasks/{task_id}/grade-real-models")
async def grade_real_models(
    task_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """"Grade the real models" mode (FEAT-1 / PRD §A): swap the task's A/B pair to two
    real frontier answers (blinded, source='baseline', truly-random slots) via the
    assembly ladder (§A3 + §A7):

      * two-frontier (one OpenAI + one Anthropic) is the strong default;
      * on a genuine single-provider failure it reverts to the OLD Anthropic-only
        method so annotation continues (tagged ``legacy_fallback``, counted);
      * a sustained high fallback rate is treated as an incident → ``503 needs_baseline``
        + admin alert, never mostly-legacy data shipped silently;
      * V4 real cases stay Anthropic-only unless ``ASCLEPIUS_TWO_FRONTIER_V4`` is on (§A7);
      * NEVER a gold stand-in for a failed model answer.

    On submission a per-model failure record is computed."""
    from asclepius import baselines as asc_baselines

    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    candidates, meta = await asc_baselines.assemble_ab_pair(store, task)

    if len(candidates) < 2:
        # No pair could be assembled. Mark needs_baseline (never a gold stand-in) and
        # 503 with an actionable message. If the fallback-rate guard tripped, this is an
        # INCIDENT (sustained provider failure), surfaced as an alert on /stats.
        store.set_task_candidates(
            task_id, task.get("candidate_answers") or [],
            generation_patch={"needs_baseline": True, "ab_source": None,
                              "fallback_reason": meta.get("fallback_reason"),
                              "fallback_alert": bool(meta.get("alert"))},
        )
        store.log_event(entity_type="task", entity_id=task_id, event_type="grade_real_models_shortfall",
                        actor=admin["id"], payload={k: meta.get(k) for k in
                                                    ("fallback_reason", "alert", "fallback_rate")})
        if meta.get("alert"):
            detail = (f"Two-frontier fallback rate {meta.get('fallback_rate')} exceeds the ceiling. "
                      "A provider looks down. New pairs are held (needs_baseline) so mostly-legacy "
                      "data is not shipped. Fix OPENAI_API_KEY / the provider, then retry.")
        else:
            detail = (f"Could not assemble an A/B pair ({meta.get('fallback_reason')}). Task marked "
                      "needs_baseline. Check OPENAI_API_KEY / ANTHROPIC_API_KEY and "
                      "ASCLEPIUS_BASELINE_MODELS (must be one id per provider).")
        raise HTTPException(status_code=503, detail=detail)

    updated = store.set_task_candidates(
        task_id, candidates,
        generation_patch={"mode": "grade_real_models",
                          # The REAL, honest source of this pair (§A3): two_frontier |
                          # legacy_fallback | anthropic_only_v4. Never relabelled.
                          "ab_source": meta.get("ab_source"),
                          "fallback_reason": meta.get("fallback_reason"),
                          "needs_baseline": False,
                          "baseline_models": [c.get("baseline_model") for c in candidates],
                          "baseline_providers": [c.get("provider") for c in candidates],
                          # Two-frontier pairs have NO intended_flawed_id — nothing is
                          # pre-labeled flawed; the specialist decides which is wrong.
                          "intended_flawed_id": None},
    )
    store.log_event(entity_type="task", entity_id=task_id, event_type="grade_real_models",
                    actor=admin["id"], payload={"ab_source": meta.get("ab_source"),
                                                "fallback_reason": meta.get("fallback_reason"),
                                                "models": [c.get("baseline_model") for c in candidates],
                                                "providers": [c.get("provider") for c in candidates]})
    return {"task_id": task_id, "modality": updated.get("modality"),
            "candidate_count": len(candidates), "ab_source": meta.get("ab_source")}


@router.get("/baselines/model-failures")
async def model_failures(
    model: Optional[str] = None,
    error_tag: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The Model-Failure artifact (FEAT-1): "cases where model X failed, with the
    expert correction", filterable by model + error tag. This is what you put in
    front of a lab."""
    store = _store()
    summary = store.model_failure_summary()
    # Provider-keyed headline rollup ("OpenAI <id> failed N; Anthropic <id> failed K")
    # — the lab-facing artifact framing.
    by_provider: Dict[str, Any] = {}
    for s in summary:
        prov = s.get("provider") or "unknown"
        bucket = by_provider.setdefault(prov, {"failures": 0, "models": {}})
        bucket["failures"] += int(s.get("failures") or 0)
        bucket["models"][s.get("model")] = int(s.get("failures") or 0)
    return {
        "failures": store.list_model_failures(model=model, error_tag=error_tag),
        "summary": summary,
        "by_provider": by_provider,
    }


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


# ─── Rubric capture (FEAT-2) ──────────────────────────────────────────────────
@router.post("/rubric/suggest")
async def rubric_suggest(
    body: SubmissionIn, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Auto-seed proposed rubric criteria from the doctor's already-captured tags
    (FEAT-2). The client sends the in-progress draft (error tags + reasons,
    why-better tags, graded/corrected steps); we return pre-filled, editable
    ``{text, points, axis, source}`` chips. NOTHING is applied — the doctor
    confirms/edits/deletes before the rubric ships (same anti-rubber-stamp rule as
    everywhere else). Post-reveal + on the doctor's own tags, so no anti-peeking
    gate; the answer key stays server-side."""
    store = _store()
    task = store.get_task(body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_real_data_access(task, user)
    from asclepius.rubric import propose_rubric

    criteria = propose_rubric(task, body.model_dump())
    return {"criteria": criteria, "axes": list(RUBRIC_AXES)}


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


# ─── Auto-suggested citations (Seamless PRD WS3) ──────────────────────────────
@router.post("/assist/cite")
async def assist_cite(
    body: CiteRequest, user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """Auto-suggest the 1–3 most relevant library citations for a rationale or
    reasoning step so grounding a record is a one-click *confirm* (WS3 — the
    biggest ratio lever: more grounded/premium records at less time).

    Guardrails: the doctor MUST confirm — nothing is auto-attached; the suggestion
    is a starting point. Retrieval is deterministic (always works); an optional
    LLM rerank refines ordering. Degrades to ``skipped=True`` when the specialty
    has no citation library (the doctor types a citation as before). No
    anti-peeking gate: citing happens post-reveal on the doctor's OWN text."""
    text = (body.text or "").strip()
    if not text:
        return {"skipped": False, "suggestions": [], "source": "empty_text"}
    res = await asc_citations.suggest_citations_ranked(
        text, specialty=(body.specialty or "nephrology"), k=max(1, min(int(body.k or 3), 5))
    )
    if res.get("skipped"):
        return {"skipped": True, "suggestions": [], "reason": "no_citation_library"}
    if res.get("suggestions"):
        _store().log_event(
            entity_type="user", entity_id=user["id"], event_type="citation_suggested",
            actor=user["id"],
            payload={"n": len(res["suggestions"]), "source": res.get("source"),
                     "top": (res["suggestions"][0] or {}).get("identifier")},
        )
    return {"skipped": False, "suggestions": res.get("suggestions") or [], "source": res.get("source")}


@router.post("/citations/search")
async def citations_search(
    body: CiteRequest, _user: Dict[str, Any] = Depends(asc_auth.get_current_user)
):
    """The explicit "Search the library" box (BUG-3c escape hatch): the doctor
    typed a query on purpose, so this is more permissive than the auto-suggest —
    any token overlap matches, ranked by relevance. A blank query returns the
    library head so the box is never a dead end. ``skipped`` when the specialty
    has no library. Never gated on the independent commit (the doctor is grounding
    their OWN text, post-reveal), like ``/assist/cite``."""
    specialty = (body.specialty or "nephrology")
    if asc_citations.load_library(specialty) is None:
        return {"skipped": True, "suggestions": [], "reason": "no_citation_library"}
    results = asc_citations.search_library(
        body.text or "", specialty=specialty, k=max(1, min(int(body.k or 10), 25))
    )
    return {"skipped": False, "suggestions": results, "source": "search"}


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
                "message": "Dictation is not available, so type your note instead "
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
def _contributor_identity(store: Any, sub: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the labelling contributor's real NAME, ORGANIZATION, and EMAIL for
    an admin/QA view. This is admin-only display data — it is NEVER copied onto a
    record or into an export (the export leak gate forbids full_name/org_name/etc.
    by field-name and by value). Sourced live from the ``users`` row via the
    submission's evaluator id (fallback: the annotator's hashed id)."""
    ident = {"name": None, "organization": None, "email": None}
    user = None
    if sub.get("evaluator_id"):
        user = store.get_user_by_id(sub["evaluator_id"])
    if not user:
        idh = (sub.get("annotator") or {}).get("id_hashed")
        if idh:
            user = store.get_user_by_id_hashed(idh)
    if user:
        ident["name"] = (user.get("full_name") or "").strip() or None
        ident["organization"] = (
            (user.get("organization") or user.get("org_name") or "").strip() or None
        )
        ident["email"] = user.get("email")
    return ident


@router.get("/qa/queue")
async def qa_queue(_qa: Dict[str, Any] = Depends(asc_auth.require_qa)):
    store = _store()
    subs = store.list_submissions(status="needs_qa")
    for s in subs:
        # Admin/QA-only identity block (name/org/email). Not persisted, not exported.
        s["contributor"] = _contributor_identity(store, s)
    return {"submissions": subs}


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
    # contributor_score's docstring has always claimed "the recompute hooks ride
    # on QA decisions and review submissions". Only the review router ever
    # called it, so a QA-only-graded submission never moved the stored score,
    # and nothing tested it. Best-effort by contract: the module swallows its
    # own failures, and a scoring problem must not undo a recorded decision.
    asc_contributor_score.recompute_for_submission(store, submission_id)
    return {"submission_id": submission_id, "status": new_status}


# ─── Export ─────────────────────────────────────────────────────────────────--
@router.post("/exports")
async def create_export(
    body: ExportRequest, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    # SINGLE_TURN only: this builds a V1–V5 preference/ideal-answer bundle. V5
    # (longitudinal) belongs here — a chart-walk point is a single-turn record with
    # a trajectory annex, not a different artifact. What does NOT belong here is
    # ``env``: an agentic rollout lives in ``env_runs`` with its own exporter, and
    # accepting it would silently produce an empty single-turn bundle labelled as
    # the agentic tier.
    if body.portal_version is not None and body.portal_version not in SINGLE_TURN_PORTAL_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail=("Invalid portal_version. ENV (agentic trajectories) exports via "
                    "/api/asclepius/environments/export?mode=raw|graded|expert."
                    if body.portal_version == ENV_PORTAL_VERSION else "Invalid portal_version"),
        )
    if body.modality is not None and body.modality not in asc_cases.MODALITIES:
        raise HTTPException(status_code=400, detail="Invalid modality")
    if body.case_source is not None and body.case_source not in asc_cases.CASE_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid case_source")
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
            modality=body.modality,
            case_source=body.case_source,
            include_answer_key=body.include_answer_key,
            include_mock=body.include_mock,
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
    from asclepius.constants import UNASSIGNED_ORG
    contribs = _contributor_metrics(store)
    orgs: Dict[str, Dict[str, Any]] = {}
    for c in contribs:
        # Never drop the ungrouped (BUG-6): a contributor with no resolvable org
        # is collected in the (unassigned) bucket so no labeled record is invisible.
        org = c.get("organization") or UNASSIGNED_ORG
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
    from asclepius.constants import UNASSIGNED_ORG
    contributors = _store().contributor_directory()
    if organization:
        contributors = [c for c in contributors if (c["organization"] or UNASSIGNED_ORG) == organization]
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
    # Admin-only identity: surface the contributor's real NAME + email alongside
    # the hashed id. This is display truth for the admin console — it never ships
    # in an export (the leak gate forbids full_name/email by field-name and value).
    user = store.get_user_by_id_hashed(id_hashed) or {}
    contributor = dict(contributor)
    contributor["full_name"] = (user.get("full_name") or "").strip() or None
    contributor["email"] = user.get("email") or contributor.get("email")
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


@router.get("/contributors/{id_hashed}/submissions")
async def list_contributor_submissions(
    id_hashed: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Every task this contributor completed, newest first — with the completion
    time (rendered Pacific client-side) and the product version they labelled
    with. Powers the per-task / per-day / per-week export controls."""
    store = _store()
    contributor = store.get_contributor(id_hashed)
    if not contributor:
        raise HTTPException(status_code=404, detail="Contributor not found")
    subs = store.list_submissions(evaluator_id=contributor["user_id"], limit=1000)
    out: List[Dict[str, Any]] = []
    for s in subs:
        task = store.get_task(s.get("task_id")) or {}
        prompt = (task.get("prompt") or "").strip().replace("\n", " ")
        out.append({
            "submission_id": s.get("submission_id"),
            "task_id": s.get("task_id"),
            "created_at": s.get("created_at"),
            "portal_version": s.get("portal_version"),
            "status": s.get("status"),
            "verdict": s.get("verdict"),
            "specialty": task.get("specialty"),
            "prompt_preview": (prompt[:120] + "…") if len(prompt) > 120 else prompt,
        })
    return {"submissions": out, "id_hashed": id_hashed}


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
    # Community v2: the vault flag is the OTHER path to "verified colleague" —
    # fire the same one-time community welcome as queue approval. Guarded +
    # idempotent inside; never fails the credential write.
    if body.credentials_verified and (user or {}).get("user_id"):
        try:
            from community.onboard import welcome_new_member  # noqa: PLC0415
            full_user = store.get_user_by_id(user["user_id"])
            if full_user:
                await welcome_new_member(full_user)
        except Exception:
            log.exception("[contributors] community welcome failed (credential write stands)")
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
            since=getattr(body, "since", None),
            until=getattr(body, "until", None),
            submission_id=getattr(body, "submission_id", None),
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
    from asclepius.constants import UNASSIGNED_ORG
    rows = _contributor_metrics(_store())
    if organization:
        rows = [r for r in rows if (r.get("organization") or UNASSIGNED_ORG) == organization]
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
    request_id: str, body: BatchFromRequest, background_tasks: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
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
    created_rows: List[Dict[str, Any]] = []

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
        created_rows.append(task)

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
            gen_ids = gen_summary.get("created") or []
            created.extend(gen_ids)
            # generate_tasks was invoked with one specialty, so every id it
            # returns carries it; no per-row lookup needed.
            created_rows.extend({"task_id": tid, "specialty": specialty} for tid in gen_ids)
        except asc_specialties.SpecialtyNotEnabled as exc:
            raise HTTPException(status_code=400, detail={"error": "specialty_not_enabled", "message": str(exc)})
        except asc_generation.GenerationDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    store.update_buyer_request_status(request_id, "in_progress")
    store.log_event(
        entity_type="buyer_request", entity_id=request_id, event_type="batch_created",
        actor=admin["id"], payload={"count": len(created)},
    )
    await _notify_new_tasks(
        store, background_tasks, _notifiable(created_rows), admin_id=admin["id"]
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
@router.get("/debug/mm-generate")
async def debug_mm_generate(
    n: int = Query(1, ge=1, le=3),
    specialty: Optional[str] = Query(None),
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Diagnostic: run the REAL multimodal generation pipeline once and report what
    happened — the accepted count and the exact per-reason DROP breakdown — so an
    operator can see precisely why V3 is (or isn't) getting structured cases instead
    of guessing. Synthetic data only (no PHI), so it is available to any signed-in
    evaluator. Accepted cases are inserted into the queue (this doubles as a manual
    'force a case' button)."""
    store = _store()
    sp = (specialty or user.get("specialty") or "nephrology").strip().lower()
    out: Dict[str, Any] = {
        "specialty": sp,
        "config": {
            "autofill_enabled": _autofill_enabled(),
            "multimodal_preferred": v3_multimodal_only(),
            "gates_relaxed": relax_multimodal_gates(),
        },
    }
    # LLM connectivity probe — the #1 reason generation is "disabled" is that the
    # ANTHROPIC_API_KEY seen by THIS backend process is missing/empty/invalid (Railway
    # variables are per-service and require a redeploy to take effect). Report enough
    # to tell missing vs malformed vs invalid vs network — WITHOUT leaking the key.
    _key = os.getenv("ANTHROPIC_API_KEY") or ""
    out["llm"] = {
        "key_present": bool(_key),
        "key_length": len(_key),
        "key_prefix_ok": _key.startswith("sk-ant-"),
    }
    try:
        from ai.llm_client import call_llm as _call_llm, first_text as _first_text
        resp, _ = await _call_llm(
            role="asclepius_case_gen",
            system="Reply with exactly the word OK.",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5, purpose="debug_llm_ping",
        )
        out["llm"]["ping"] = "ok: " + (_first_text(resp) or "")[:20]
    except Exception as exc:  # the REAL reason: AuthenticationError / connection / missing key
        out["llm"]["ping"] = f"FAILED: {type(exc).__name__}: {str(exc)[:240]}"
    try:
        res = await asc_generation.generate_tasks(
            store, specialty=sp, n=n, multimodal=True,
            created_by=f"debug:{user.get('email')}",
        )
        out["result"] = {
            "accepted": res.get("accepted"),
            "dropped": dict(res.get("dropped") or {}),
            "shortfall": res.get("shortfall"),
        }
        mm = [t for t in store.list_tasks(specialty=sp, limit=25)
              if t.get("modality") == "multimodal"]
        out["multimodal_in_queue"] = len(mm)
        if mm:
            c = mm[-1].get("case") or {}
            out["sample_case"] = {
                "task_id": mm[-1]["task_id"],
                "lab_panels": len(c.get("lab_panels") or []),
                "notes": len(c.get("notes") or []),
                "problems": len(c.get("problem_list") or []),
                "medications": len(c.get("medications") or []),
            }
    except asc_generation.GenerationDisabled as exc:
        out["error"] = f"generation_disabled: {exc} (is ANTHROPIC_API_KEY set?)"
    except Exception as exc:  # surface the failure instead of a 500 the operator can't read
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


@router.get("/debug/load-gold-cases")
async def debug_load_gold_cases(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Load the 10 ratified GOLD nephrology multimodal cases into the queue as
    ready-to-serve V3 tasks (real labs + EHR + an authored A/B pair, NO LLM needed).
    Idempotent. Any signed-in evaluator can call it (synthetic data only) so V3 can be
    populated on demand — independent of the ANTHROPIC_API_KEY state."""
    from asclepius.gold_cases import load_gold_cases

    res = load_gold_cases(_store())
    res["multimodal_in_queue"] = len([
        t for t in _store().list_tasks(specialty="nephrology", limit=50)
        if t.get("modality") == "multimodal"
    ])
    return res


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
        # Open queue by modality (Multimodal Debug PRD P3.11): "multimodal in
        # queue: N" so the operator always knows structured cases exist.
        "open_modality_counts": store.open_modality_counts(),
        "value_per_time": vpt,
        "value_per_time_target": value_per_minute_target(),
        # Rubber-stamp guard: model-assist override rate on the assisted flow.
        "override_rate": store.override_rate_stats(portal_version="v2"),
        # Position-bias QC (Seamless PRD WS6): observed A-is-stronger rate (~0.5).
        "ab_balance": store.ab_balance_stats(),
        # Two-frontier slot balance (A3): OpenAI-in-slot-A rate over built pairs (~0.5).
        "ab_slot_balance": store.ab_slot_balance(),
        # Two-frontier fallback health (PRD §A3 Rung 3): rolling legacy_fallback rate +
        # a RED alert when it exceeds the ceiling (a provider is likely down and new
        # pairs are being held). ``rate`` is None on a cold start (no pairing history).
        "ab_fallback": _ab_fallback_health(store),
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Real EHR ingestion (EHR Ingestion PRD §4, §5, §8, §9)
#  Partner secure upload → verify → parse → normalize → quarantine/ingest →
#  promote to a V4 task. Partner endpoints are TOKEN-auth (no app account);
#  everything else is admin.
# ═══════════════════════════════════════════════════════════════════════════════
from asclepius import deid_verify as asc_deid_verify  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from asclepius import ingest_notify as asc_ingest_notify  # noqa: E402


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _validate_upload_token(store: Any, token: Optional[str]) -> Dict[str, Any]:
    """Resolve + validate a partner upload token. 410 for expired/used/revoked
    (the PRD's contract), 401 for unknown. The RAW token is never stored —
    only its SHA-256."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing upload token")
    link = store.get_upload_link_by_token_hash(_token_hash(token))
    if not link:
        raise HTTPException(status_code=401, detail="Invalid upload token")
    if link.get("revoked"):
        raise HTTPException(status_code=410, detail="This upload link was revoked")
    try:
        expired = datetime.utcnow().isoformat() > str(link.get("expires_at"))
    except Exception:
        expired = True
    if expired:
        raise HTTPException(status_code=410, detail="This upload link has expired")
    if link.get("one_time") and int(link.get("used_count") or 0) > 0:
        raise HTTPException(status_code=410, detail="This upload link was already used")
    return link


@router.post("/admin/upload-links")
async def mint_upload_link(
    body: UploadLinkRequest, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Mint a single-purpose, expiring partner upload link (PRD §4). The raw
    token is returned ONCE here and never stored (SHA-256 at rest)."""
    store = _store()
    # PRD-I §2.1/§2.2 — the SAME two buttons the health-system form has. Without
    # this the column existed with no writer and no reader, so every link-door
    # upload landed NULL, resolved to task_creation in the gate, and promoted:
    # the requirement the PRD wrote first was met for neither door it names.
    #
    # Nothing below branches on the value. Same token alphabet and length, same
    # URL template, same expiry, same response shape — the recipient cannot tell
    # which button was pressed.
    purpose = (body.purpose or "").strip().lower()
    if purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=max(1, min(720, body.expires_hours)))).isoformat()
    contact_email = (body.contact_email or "").strip() or None
    if contact_email and not asc_ingest_notify.looks_like_email(contact_email):
        raise HTTPException(status_code=400, detail="Contact email is not a valid address.")
    link = store.create_upload_link(
        token_hash=_token_hash(token),
        partner_id=body.partner_id.strip(),
        partner_label=(body.partner_label or "").strip() or None,
        specialty=body.specialty,
        expires_at=expires_at,
        one_time=body.one_time,
        max_bytes=min(body.max_bytes or asc_ingestion.max_zip_bytes(), asc_ingestion.max_zip_bytes()),
        created_by=admin["id"],
        contact_email=contact_email,
        purpose=purpose,
    )
    store.log_event(entity_type="ingest_link", entity_id=link["link_id"],
                    event_type="upload_link_minted", actor=admin["id"],
                    payload={"partner_id": body.partner_id, "expires_at": expires_at,
                             "one_time": body.one_time, "purpose": purpose})
    base = (os.getenv("BASE_URL") or "").rstrip("/")
    return {**{k: v for k, v in link.items() if k != "token_hash"},
            "token": token,
            "upload_url": f"{base}/partner/upload?t={token}"}


@router.get("/admin/upload-links")
async def list_upload_links(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    links = [{k: v for k, v in l.items() if k != "token_hash"} for l in _store().list_upload_links()]
    return {"links": links}


@router.post("/admin/upload-links/{link_id}/revoke")
async def revoke_upload_link(
    link_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    if not store.get_upload_link(link_id):
        raise HTTPException(status_code=404, detail="Link not found")
    store.revoke_upload_link(link_id)
    store.log_event(entity_type="ingest_link", entity_id=link_id,
                    event_type="upload_link_revoked", actor=admin["id"])
    return {"revoked": True}


@router.post("/partner/uploads")
async def partner_upload(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    t: Optional[str] = Query(None, description="upload link token"),
    # PRD ADMIN-TASKS §3.1 — what the sender says this data IS, in their words.
    # OPTIONAL with an empty default, which is the whole compatibility story: every
    # partner integration that posts only `file` keeps working byte-for-byte, and a
    # bundle that arrives without one renders "no description given" rather than a
    # blank line pretending to be one. Never branched on — it is a sentence for a
    # human, not a routing key.
    description: str = Form(""),
):
    """The partner's one capability (PRD §4): POST a .zip through their token.
    Caps + magic-byte check + SHA-256 + encrypted quarantine write happen inline;
    unpack/parse/verify run in the background (never in the request path)."""
    store = _store()
    # FAIL CLOSED in production (security review): the raw partner bundle is the
    # most sensitive artifact in the pipeline — we refuse to accept it at all if
    # it cannot be encrypted at rest (DATA_ENCRYPTION_KEY unset).
    if (os.getenv("ENV") or "").strip().lower() == "production":
        import field_crypto
        if not field_crypto.is_configured():
            raise HTTPException(
                status_code=503,
                detail="Ingestion is disabled: DATA_ENCRYPTION_KEY is not configured, "
                       "so the upload cannot be encrypted at rest.",
            )
        # FAIL CLOSED on non-durable storage too: never accept a bundle we cannot
        # keep. A raw blob on ephemeral disk is the "download failed (410)" incident.
        ok, why = asc_ingestion.ingest_storage_durable()
        if not ok:
            raise HTTPException(status_code=503, detail=f"Ingestion is disabled: {why}")
        # Audit PRD §P2: the DERIVED image blobs must be as durable as the raw upload.
        # Losing a blob leaves a case with neither the image nor its withheld caption.
        from asclepius import assets as asc_assets
        ok, why = asc_assets.asset_storage_durable()
        if not ok:
            raise HTTPException(status_code=503, detail=f"Ingestion is disabled: {why}")
    link = _validate_upload_token(store, t)
    cap = int(link.get("max_bytes") or asc_ingestion.max_zip_bytes())
    # Read at most cap+1 bytes so a valid-token holder cannot OOM the process with a
    # multi-GB POST before the cap is enforced (matches the account door's bounded
    # read). Reject as soon as the cap is exceeded, before buffering the whole body.
    raw = await file.read(cap + 1)
    if len(raw) > cap:
        raise HTTPException(status_code=413, detail="Upload exceeds the link's size cap")
    # Buyer Response PRD §2 A1: accept a bare partner file (.json / .csv / .hl7 /
    # .txt) through the magic link, not only a pre-zipped bundle. Both upload doors
    # now wrap loose files with the SAME implementation, so the exact file we mail a
    # partner lands identically regardless of which URL they used. A rejection with
    # a message about "zip magic bytes" meant nothing to a hospital IT team.
    data = asc_ingestion.wrap_loose_files(
        [{"filename": file.filename or "file", "content": raw}],
        specialty=(link.get("specialty") or None),
    )
    if len(data) > cap:
        raise HTTPException(status_code=413, detail="Upload exceeds the link's size cap")
    # Import-link hardening (Audit §9.1): reject a STRUCTURALLY-unreadable upload
    # synchronously with actionable copy — a hospital IT team must be told what to DO,
    # never that the "zip magic bytes" were wrong. Only a corrupt archive fails here;
    # a bare .json/.csv/.hl7/.txt was wrapped above and unpacks fine, and a readable
    # bundle with no gradable content is handled (and explained) by the pipeline.
    try:
        asc_ingestion.unpack_bundle(data)
    except asc_ingestion.BundleRejected:
        store.log_event(entity_type="ingest_link", entity_id=link["link_id"],
                        event_type="upload_unreadable",
                        payload={"filename": (file.filename or "")[:120]})
        raise HTTPException(status_code=400, detail=asc_ingestion.UNREADABLE_UPLOAD_MESSAGE)
    digest = asc_ingestion.sha256_hex(data)
    # ── Order matters for data safety (see the 410 incident) ──────────────────
    # 1. Persist the encrypted bytes to DURABLE storage FIRST, under a fresh id.
    #    If the write fails we have NOT consumed the link and NOT created a row,
    #    so the partner's one-time link stays valid and they can simply retry.
    upload_id = store.new_upload_id()
    try:
        raw_path = asc_ingestion.store_raw(upload_id, data)
    except Exception as exc:  # disk full, permissions, encrypt failure, …
        store.log_event(entity_type="ingest_link", entity_id=link["link_id"],
                        event_type="upload_store_failed", payload={"error": str(exc)})
        raise HTTPException(
            status_code=503,
            detail="Could not store the upload securely. Your link is still valid, "
                   "please retry in a moment.",
        )
    # 2. ATOMIC one-time claim, AFTER the bytes are safe (closes the TOCTOU race
    #    where two concurrent uploads both pass a used_count==0 read). If we lose
    #    the claim (already used / revoked), delete the orphan blob and 410.
    if not store.consume_upload_link(link["link_id"], one_time=bool(link.get("one_time"))):
        asc_ingestion.delete_raw(raw_path)
        raise HTTPException(status_code=410, detail="This upload link was already used")
    # 3. Insert the row already carrying raw_path — it is never null, so the file
    #    on disk is always reachable by download/retry/recovery.
    upload = store.insert_ingest_upload(
        upload_id=upload_id,
        link_id=link["link_id"], partner_id=link["partner_id"],
        filename=(file.filename or "bundle.zip")[:120], sha256=digest,
        size_bytes=len(data), raw_path=raw_path,
        source_ip=(request.client.host if request.client else None),
    )
    # Provenance from the authorizing LINK row, joined server-side (PRD-I §2.1).
    # This door had no such call at all, which is why its purpose column was dead.
    store.attach_upload_provenance(upload["upload_id"], link_id=link["link_id"])
    # §3.1 — written AFTER provenance so a failure here cannot cost us the row. A
    # missing description is a cosmetic loss; a missing upload is the 410 incident.
    if (description or "").strip():
        store.set_upload_description(upload["upload_id"], description)
    store.log_event(entity_type="ingest_upload", entity_id=upload["upload_id"],
                    event_type="upload_received",
                    payload={"partner_id": link["partner_id"], "sha256": digest,
                             "bytes": len(data),
                             "source_ip": request.client.host if request.client else None})
    background.add_task(asc_ingestion.process_upload, store, upload["upload_id"])
    return {"upload_id": upload["upload_id"], "sha256": digest, "status": "received"}


@router.get("/partner/uploads/{upload_id}")
async def partner_upload_status(upload_id: str, t: Optional[str] = Query(None)):
    """The partner polls their OWN upload's status through the same token —
    accepted / quarantined + a human-readable reason. No other data is exposed."""
    store = _store()
    link = _validate_upload_token_lenient(store, t)
    upload = store.get_ingest_upload(upload_id)
    if not upload or upload.get("link_id") != link["link_id"]:
        raise HTTPException(status_code=404, detail="Upload not found")
    # Content summary (Audit §9.2): once terminal, tell the partner WHAT came through
    # — patient cases, lab results, images, notes — aggregate counts only, no PHI, so
    # the status page reads "Received. 1 patient case · 33 lab results · 5 images · 8
    # notes." rather than a bare status token.
    summary = None
    if upload["status"] in ("ingested", "needs_review", "quarantined"):
        cases = store.list_ingest_cases(upload_id=upload_id)
        landed = [c for c in cases if c.get("status") in ("ingested", "needs_review")]
        labs = images = notes = 0
        for c in landed:
            case = c.get("case") or {}
            for p in case.get("lab_panels") or []:
                labs += len(p.get("results") or []) or 1
            images += sum(1 for s in (case.get("studies") or []) if (s or {}).get("asset"))
            notes += len(case.get("notes") or [])
        summary = {"cases": len(landed), "lab_results": labs, "images": images, "notes": notes}
    return {"upload_id": upload_id, "status": upload["status"],
            "reason": upload.get("reason"),
            "filename": upload.get("filename"), "sha256": upload.get("sha256"),
            "summary": summary}


def _validate_upload_token_lenient(store: Any, token: Optional[str]) -> Dict[str, Any]:
    """Status polling stays available after a one-time link is used (the partner
    needs to see the outcome of the upload they just made) — but never after
    revocation or expiry."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing upload token")
    link = store.get_upload_link_by_token_hash(_token_hash(token))
    if not link:
        raise HTTPException(status_code=401, detail="Invalid upload token")
    if link.get("revoked"):
        raise HTTPException(status_code=410, detail="This upload link was revoked")
    try:
        if datetime.utcnow().isoformat() > str(link.get("expires_at")):
            raise HTTPException(status_code=410, detail="This upload link has expired")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=410, detail="This upload link has expired")
    return link


# ─── Admin: ingestion review ──────────────────────────────────────────────────
def _partner_label_for_upload(store: Any, upload: Dict[str, Any]) -> Optional[str]:
    """The human-readable partner label ('Gray Scrubs Lab'). Magic-link uploads
    carry it on the link row; account-door uploads carry it as the provider's
    org_name. Falls back to the raw partner_id."""
    link_id = upload.get("link_id")
    if link_id and link_id != "account":
        link = store.get_upload_link(link_id)
        if link and (link.get("partner_label") or "").strip():
            return link["partner_label"].strip()
    prov = store.get_data_provider(upload.get("partner_id") or "")
    if prov and (prov.get("org_name") or "").strip():
        return prov["org_name"].strip()
    return None


def _contact_email_for_upload(store: Any, upload: Dict[str, Any]) -> Optional[str]:
    """The sender's contact email (magic-link ``contact_email`` or the account
    provider's email) — drives the 'Notify sender' action. None if unknown."""
    email, _name = asc_ingest_notify._recipient_for(store, upload)
    return email


def _promote_block(
    upload: Dict[str, Any],
    ingested: List[Dict[str, Any]],
    promotable: List[Dict[str, Any]],
    undetermined: List[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Why this upload cannot be promoted, or None when it can.

    The server already knows every reason — it enforces all of them — but it only
    said so at the moment of refusal, by which point the admin had already clicked
    a button that looked live. Deciding it HERE, in the same place the gate lives,
    is what lets the admin surfaces render a disabled control with the real reason
    instead of a clickable button that 409s. Same order as the promote endpoints,
    so the reason shown is the reason that would fire.
    """
    def _blocked(value) -> Dict[str, str]:
        """The reason THIS value is barred, so the disabled button says the thing
        that lifts it. Brokering never lifts; storage lifts the moment somebody
        reads the file and sets a destination on this row."""
        return {
            "reason": "brokering" if asc_ingestion.is_brokering(value) else "storage",
            "message": asc_ingestion.promotion_block_reason(value),
        }

    # When cases exist, the EFFECTIVE per-case purpose decides, exactly as the
    # promote endpoints decide it (COALESCE(case.purpose, upload.purpose) — see
    # `promotable` at the call site). Testing the upload row first would have
    # reported "brokering" for an upload whose cases were individually resolved
    # to task creation, disabling a button the server would happily have honored.
    # The upload row is consulted only when there are no cases to speak for it.
    if not ingested:
        if asc_ingestion.blocks_promotion(upload.get("purpose")):
            return _blocked(upload.get("purpose"))
        return {
            "reason": "no_cases",
            "message": "No cases in this upload have finished ingesting. Clear any "
                       "review holds in Partner uploads above.",
        }
    if not promotable:
        # Every ingested case is barred. Report the upload's own value, which is
        # what the operator would resolve, rather than an arbitrary case's.
        return _blocked(upload.get("purpose"))
    if undetermined:
        return {
            "reason": "specialty",
            "message": "Specialty not set — choose one to promote. Promoting "
                       "without it would label these cases with a default that "
                       "routes them to the wrong physician pool.",
        }
    return None


@router.get("/ingestion/uploads")
async def list_ingestion_uploads(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Paginated over FULL history (newest first). Returns the page plus the
    grand total so the admin UI can page through every upload ever received.

    ``status`` (Audit PRD §21.5) narrows to one state — ``needs_review`` surfaces
    the admin review queue. ``counts`` always carries the per-status totals so the
    filter chips render real numbers regardless of the active filter."""
    store = _store()
    total = store.count_ingest_uploads(status=status)
    counts = {
        "all": store.count_ingest_uploads(),
        "ingested": store.count_ingest_uploads(status="ingested"),
        "needs_review": store.count_ingest_uploads(status="needs_review"),
        "quarantined": store.count_ingest_uploads(status="quarantined"),
        "rejected": store.count_ingest_uploads(status="rejected"),
    }
    uploads = store.list_ingest_uploads(limit=limit, offset=offset, status=status)
    for u in uploads:
        u["partner_label"] = _partner_label_for_upload(store, u)
        # How many ingested cases are ready to promote from THIS upload file —
        # drives the upload-scoped promote UI.
        cases = store.list_ingest_cases(upload_id=u["upload_id"])
        u["ingested_case_count"] = sum(1 for c in cases if c.get("status") == "ingested")
        u["case_count"] = len(cases)
        # Whether promotion is even POSSIBLE for this upload, on the same row that
        # renders the Promote button. Ingest refuses to guess a specialty, so a
        # hospital-portal upload lands on the neutral 'general' and both promote
        # endpoints 409 on it — and the admin had no way to see that coming and no
        # control to fix it. Mirrors _bucket_uploads in routers/asclepius_admin.py;
        # one query per upload was already being issued above, so this is free.
        # COALESCE(case.purpose, upload.purpose) — the same effective-purpose rule
        # the promote endpoints apply, computed from rows already in hand rather
        # than with a second query per upload.
        ingested = [c for c in cases if c.get("status") == "ingested"]
        promotable = [c for c in ingested
                      if not asc_ingestion.blocks_promotion(
                          c.get("purpose") or u.get("purpose"))]
        undetermined = [c for c in promotable
                        if asc_ingestion.specialty_is_undetermined(c.get("specialty"))]
        u["specialties"] = sorted({c.get("specialty") for c in cases
                                   if c.get("specialty")
                                   and not asc_ingestion.specialty_is_undetermined(c.get("specialty"))})
        u["specialty_determined"] = bool(promotable) and not undetermined
        u["specialty_undetermined_cases"] = len(undetermined)
        u["promote_block"] = _promote_block(u, ingested, promotable, undetermined)
        # Notification affordances for the row (never expose the raw path).
        u["contact_email"] = _contact_email_for_upload(store, u)
        u["failure_notified"] = bool(u.get("failure_notified_at"))
        # ═══ PRD ADMIN-TASKS §3 — the staging fields ═════════════════════════
        # Box 1 asks "what is this and where does it go", Box 2 asks "how much of
        # it is already tasks". Both are answered from rows this loop already
        # holds plus ONE grouped count per upload, rather than by a second
        # endpoint the two boxes would have to keep in sync with this one.
        # NB the name: ``counts`` is the per-STATUS upload tally built above and
        # returned at the top level. Reusing that name here shadowed it, so the
        # response's status chips became whichever upload happened to be last in
        # the page — a filtered request then reported the counts of one upload's
        # cases as the totals for the whole pipeline.
        case_counts = store.upload_task_counts(u["upload_id"])
        u["case_counts"] = case_counts
        u["tasks_created"] = case_counts["promoted"]
        # The three states §3 renders, over the THREE-value purpose vocabulary.
        #
        # 'undecided' is ``is_storage``, not ``purpose IS NULL``. Storage is the
        # default and it explicitly includes NULL — "received, stored, and used
        # for nothing until a person says what it is for" — so testing falsiness
        # would file an upload deliberately marked 'storage' as task creation and
        # offer to build tasks out of it. Box 1 IS the storage bucket.
        _purpose = u.get("purpose")
        if asc_ingestion.is_brokering(_purpose):
            u["staging"] = "brokering"
        elif asc_ingestion.is_storage(_purpose):
            # An upload whose cases already became tasks is HISTORY, not a
            # decision. Those rows predate the storage default, when NULL
            # resolved to task_creation and promoted; asking an operator to
            # decide what they are for asks them to decide something that has
            # already happened. They belong in the done fold, not Box 1.
            u["staging"] = "task_creation" if case_counts["promoted"] else "undecided"
        else:
            u["staging"] = "task_creation"
        # Whether every eligible case has become a task — the §3.2 "done" fold.
        u["task_creation_complete"] = bool(
            case_counts["promoted"] and not case_counts["ingested"])
        # ═══ PRD LONGITUDINAL-E2E §3 — auto-generate, for the row ════════════
        # Three separate facts, because they answer three different questions and
        # collapsing them into one chip is how "why did nothing happen" becomes
        # unanswerable on the screen that has to answer it:
        #   armed    — the flag is on;
        #   will_run — the flag is on AND the trigger is fully satisfied, so this
        #              bundle builds itself the moment anything else changes;
        #   has_run  — its one run is spent (it never fires twice).
        u["auto_generate"] = bool(int(u.get("auto_generate") or 0))
        u["auto_generate_will_run"] = asc_auto_generate.is_armed(u)
        u["auto_generate_has_run"] = asc_auto_generate.has_run(u)
        # A COUNT with the detail behind it, never a modal (§3): 22 points built
        # out of 25 is a result, and a modal would present it as an error.
        u["auto_generate_failures"] = asc_auto_generate.failure_summary(u)
        u.pop("auto_generate_report", None)   # the summary above is what the row reads
        u.pop("raw_path", None)  # server-side path is not admin-relevant
    return {"uploads": uploads, "total": total, "limit": limit, "offset": offset,
            "counts": counts, "status": status}


def _upload_past_raw_retention(upload: Dict[str, Any]) -> bool:
    """True if the upload is old enough that its raw blob would have been purged
    by the retention sweep. Used to word a missing-blob 410 honestly: past the
    window it's expected; inside it, the blob was lost to non-durable storage."""
    created = upload.get("created_at")
    if not created:
        return True  # unknown age — assume the benign (retention) explanation
    try:
        ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    age_days = (datetime.utcnow() - ts).total_seconds() / 86400
    return age_days >= asc_ingestion.raw_retention_days()


@router.get("/ingestion/uploads/{upload_id}/download")
async def download_ingestion_upload(
    upload_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Admin download of the ORIGINAL partner-uploaded bundle (decrypted at rest).
    Available until the raw blob is purged (ASCLEPIUS_RAW_RETENTION_DAYS)."""
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    raw_path = upload.get("raw_path")
    if not raw_path or not os.path.exists(raw_path):
        # Distinguish an EXPECTED retention purge from unexpected blob loss: if the
        # upload is still inside the retention window the blob should be here, so a
        # missing file points at a storage problem (e.g. raw dir on ephemeral disk),
        # not the retention policy. Give the admin the honest reason either way.
        purged_by_retention = _upload_past_raw_retention(upload)
        detail = (
            "The raw upload has been purged (retention window elapsed). "
            "Only the derived cases remain."
            if purged_by_retention else
            "The original upload is no longer available on disk even though it is "
            "still within the retention window. The raw blob was lost (its storage "
            "did not persist). Only the derived cases remain; ask the partner to "
            "re-upload if the original bundle is needed."
        )
        raise HTTPException(status_code=410, detail=detail)
    try:
        data = asc_ingestion.load_raw(raw_path)
    except Exception as exc:  # pragma: no cover - decrypt failure is exceptional
        raise HTTPException(status_code=500, detail=f"Could not read the upload: {exc}")
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="upload_downloaded", actor=_admin["id"])
    fname = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_"
                    for c in (upload.get("filename") or f"{upload_id}.zip")) or "upload.zip"
    headers = {"Content-Disposition": f"attachment; filename=\"{fname}\"; filename*=UTF-8''{fname}"}
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers=headers)


@router.post("/ingestion/uploads/{upload_id}/notify-sender")
async def notify_upload_sender(
    upload_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Manually email the partner that their upload didn't come through (no PHI,
    'nothing was leaked / no breach, please re-send'). Complements the automatic
    notification on rejected/lost uploads; use it for anything you want to flag."""
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    outcome = "lost" if not (upload.get("raw_path") and os.path.exists(upload["raw_path"])) \
        else (upload.get("status") or "failed")
    sent, detail = asc_ingest_notify.notify_upload_failed(
        store, upload, outcome=outcome, manual=True, actor=admin["id"])
    if not sent:
        # No recipient on file is a 400 the admin can act on; a transport failure
        # is a 502 (their config/vendor), so the UI can word it correctly.
        code = 400 if "contact email" in detail else 502
        raise HTTPException(status_code=code, detail=detail)
    return {"sent": True, "detail": detail}


@router.get("/ingestion/uploads/{upload_id}")
async def get_ingestion_upload(
    upload_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    upload = dict(upload)
    upload.pop("raw_path", None)  # server-side path is not admin-relevant
    upload["cases"] = store.list_ingest_cases(upload_id=upload_id)
    return upload


@router.post("/ingestion/reconcile")
async def run_ingestion_reconcile(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Run terminal-state reconciliation on demand (Audit §9.3): re-bind unbound sealed
    keys and hold cases with missing/corrupt asset blobs. Also runs at startup and can
    be scheduled nightly. Returns the counts the admin ingestion card surfaces."""
    store = _store()
    counts = asc_ingestion.reconcile_ingested_cases(store)
    return {"reconcile": counts}


@router.get("/ingestion/uploads/{upload_id}/review")
async def get_upload_review(
    upload_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """The admin review queue for one upload (Audit PRD §21.5): every case that
    raised a review reason, with its reasons split blocking-first so the UI renders
    the PHI hold above the advisory note. A case with no reasons is omitted."""
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    out = []
    for c in store.list_ingest_cases(upload_id=upload_id):
        reasons = c.get("review") or []
        if not reasons:
            continue
        blocking = [r for r in reasons if r.get("severity") == "blocking"]
        advisory = [r for r in reasons if r.get("severity") != "blocking"]
        out.append({
            "ingest_case_id": c["ingest_case_id"],
            "status": c.get("status"),
            "review_status": c.get("review_status"),
            "reviewed_by_hashed": c.get("reviewed_by_hashed"),
            "reviewed_at": c.get("reviewed_at"),
            # Blocking reasons first, always (§21.7) — a certifier must see the PHI
            # hold before the advisory note.
            "reasons": blocking + advisory,
            "blocking_count": len(blocking),
            "studies": (c.get("case") or {}).get("studies") or [],
        })
    return {"upload_id": upload_id, "cases": out}


def _review_actor_hashed(admin: Dict[str, Any]) -> str:
    """Hash the clearing admin's id the same way the store hashes user ids
    (sha256[:16]) so a cleared flag is attributable without storing the raw id."""
    return hashlib.sha256(str(admin.get("id") or "").encode("utf-8")).hexdigest()[:16]


@router.post("/ingestion/cases/{ingest_case_id}/review/clear")
async def clear_case_review(
    ingest_case_id: str, body: ReviewClearRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Clear a case's blocking review reasons (Audit PRD §21.5). A note is
    mandatory (the schema enforces presence; we reject whitespace-only here) and the
    action stamps ``reviewed_by_hashed`` + ``reviewed_at``. Clearing all blocking
    reasons flips the case back to ``ingested`` so it re-enters the annotation queue;
    advisory reasons are retained on the record but never held the case."""
    if not (body.note or "").strip():
        raise HTTPException(status_code=400, detail="A review note is required to clear a case.")
    store = _store()
    ic = store.get_ingest_case(ingest_case_id)
    if not ic or ic.get("status") != "needs_review":
        raise HTTPException(status_code=404, detail="Case is not awaiting review")
    reasons = list(ic.get("review") or [])
    cleared_at = _utcnow_iso()
    actor = _review_actor_hashed(admin)
    # Drop the blocking reasons (the human has affirmatively resolved them); keep
    # advisory reasons as a record. If any advisory remains, the case is still
    # 'ingested' (advisory never held it) with review_status 'cleared'.
    remaining = [r for r in reasons if r.get("severity") != "blocking"]
    store.update_ingest_case(
        ingest_case_id, status="ingested", review_status="cleared",
        review_json=remaining, reviewed_by_hashed=actor, reviewed_at=cleared_at)
    store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                    event_type="review_cleared", actor=admin["id"],
                    payload={"note": body.note, "reason": body.reason,
                             "cleared_blocking": [r.get("reason") for r in reasons
                                                  if r.get("severity") == "blocking"]})
    return {"status": "ingested", "review_status": "cleared",
            "reviewed_by_hashed": actor, "reviewed_at": cleared_at}


@router.post("/ingestion/cases/{ingest_case_id}/review/reject")
async def reject_case_review(
    ingest_case_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Reject a case awaiting review (Audit PRD §21.5) — the reviewer found the flag
    is real (e.g. the image does carry burned-in PHI). The case is quarantined, never
    served for annotation, and the action is attributable."""
    store = _store()
    ic = store.get_ingest_case(ingest_case_id)
    if not ic or ic.get("status") != "needs_review":
        raise HTTPException(status_code=404, detail="Case is not awaiting review")
    actor = _review_actor_hashed(admin)
    store.update_ingest_case(
        ingest_case_id, status="quarantined", review_status="rejected",
        reviewed_by_hashed=actor, reviewed_at=_utcnow_iso())
    store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                    event_type="review_rejected", actor=admin["id"])
    return {"status": "quarantined", "review_status": "rejected"}


@router.post("/ingestion/uploads/{upload_id}/retry")
async def retry_ingestion_upload(
    upload_id: str, background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Re-run the pipeline (e.g. after loosening a knob or fixing an adapter).
    Only the raw blob is reused; prior case rows for the upload stay for audit."""
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    if not upload.get("raw_path") or not os.path.exists(upload["raw_path"]):
        raise HTTPException(status_code=410, detail="Raw upload already purged (retention window)")
    store.update_ingest_upload(upload_id, status="received", reason=None)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="upload_retry", actor=admin["id"])
    background.add_task(asc_ingestion.process_upload, store, upload_id)
    return {"upload_id": upload_id, "status": "received"}


@router.get("/ingestion/quarantine")
async def list_quarantine(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Quarantined cases with MASKED findings (a suspected identifier is never
    rendered in cleartext — PRD §8)."""
    cases = _store().list_ingest_cases(status="quarantined")
    for c in cases:
        c.pop("case", None)  # the case body is not needed to triage; keep the payload light
    return {"cases": cases}


@router.post("/ingestion/quarantine/{ingest_case_id}/reject")
async def quarantine_reject(
    ingest_case_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    ic = store.get_ingest_case(ingest_case_id)
    if not ic or ic["status"] != "quarantined":
        raise HTTPException(status_code=404, detail="Quarantined case not found")
    store.update_ingest_case(ingest_case_id, status="rejected")
    store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                    event_type="quarantine_rejected", actor=admin["id"])
    return {"status": "rejected"}


@router.post("/ingestion/quarantine/{ingest_case_id}/scrub")
async def quarantine_scrub(
    ingest_case_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Targeted scrub (PRD §8): redact EXACTLY the flagged spans and re-run the
    verification + hard guard. An explicit, logged human action — never automatic."""
    store = _store()
    ic = store.get_ingest_case(ingest_case_id)
    if not ic or ic["status"] != "quarantined":
        raise HTTPException(status_code=404, detail="Quarantined case not found")
    findings = ((ic.get("report") or {}).get("verification") or {}).get("findings") or []
    scrubbed = asc_deid_verify.apply_targeted_scrub(ic.get("case") or {}, findings)
    # Review finding: a TIMELINE-unresolved quarantine (ambiguous date tokens the
    # normalizer refused to guess) has no verifier findings to scrub — it must
    # NOT flip to ingested while those tokens are still in the text. Only a
    # better manifest index_event (re-upload/retry) or rejection resolves it.
    from asclepius.timeline import datelike_leftovers
    leftovers = datelike_leftovers(scrubbed)
    if leftovers:
        report0 = dict(ic.get("report") or {})
        report0["unresolved_after_scrub"] = leftovers[:10]
        store.update_ingest_case(ingest_case_id, report_json=report0)
        return {"status": "quarantined",
                "reason": "unresolved date-like tokens remain (" + ", ".join(leftovers[:3]) +
                          "); scrub cannot fix an ambiguous timeline. Re-upload with a "
                          "manifest index_event, or reject."}
    verification = asc_deid_verify.verify_deid(scrubbed)
    report = dict(ic.get("report") or {})
    report["verification"] = verification
    report["scrubbed_spans"] = len(findings)
    if verification["status"] == "flagged":
        store.update_ingest_case(ingest_case_id, case_json=scrubbed, report_json=report)
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="quarantine_scrub_insufficient", actor=admin["id"],
                        payload={"remaining": len(verification["findings"])})
        return {"status": "quarantined",
                "remaining_findings": len(verification["findings"])}
    try:
        from asclepius import case_formats as _cf
        from asclepius.cases import ClinicalCase as _CC
        safe = _cf.deidentify(scrubbed)
        case = _CC(**{**safe, "case_source": "real_deid",
                      # Never a literal specialty (PRD-I §4.2): an override that
                      # silently stamps nephrology on a stroke chart is the same
                      # invisible mislabel the promote guards exist to prevent.
                      "specialty": safe.get("specialty") or ic.get("specialty")
                                   or "general"}).model_dump()
    except Exception as exc:
        store.update_ingest_case(ingest_case_id, case_json=scrubbed, report_json=report)
        return {"status": "quarantined", "reason": f"hard guard still rejects: {exc}"}
    report["quarantine_reason"] = None
    store.update_ingest_case(ingest_case_id, status="ingested", case_json=case, report_json=report)
    store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                    event_type="quarantine_scrubbed_ingested", actor=admin["id"],
                    payload={"scrubbed_spans": len(findings)})
    return {"status": "ingested"}


@router.post("/ingestion/quarantine/{ingest_case_id}/override")
async def quarantine_override(
    ingest_case_id: str, body: QuarantineOverrideRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Documented admin override of VERIFIER findings (e.g. a false positive on a
    lab value shaped like a phone number). The ``deidentify()`` HARD guard still
    runs and cannot be overridden — if it rejects, the override fails."""
    store = _store()
    ic = store.get_ingest_case(ingest_case_id)
    if not ic or ic["status"] != "quarantined":
        raise HTTPException(status_code=404, detail="Quarantined case not found")
    reason = (body.reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(status_code=400, detail="An override requires a documented reason (≥10 chars)")
    try:
        from asclepius import case_formats as _cf
        from asclepius.cases import ClinicalCase as _CC
        safe = _cf.deidentify(ic.get("case") or {})
        case = _CC(**{**safe, "case_source": "real_deid",
                      # Never a literal specialty (PRD-I §4.2): an override that
                      # silently stamps nephrology on a stroke chart is the same
                      # invisible mislabel the promote guards exist to prevent.
                      "specialty": safe.get("specialty") or ic.get("specialty")
                                   or "general"}).model_dump()
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=f"The hard de-identification guard rejects this case ({exc}); it cannot be overridden.",
        )
    store.update_ingest_case(ingest_case_id, status="ingested", case_json=case,
                             override_reason=reason)
    store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                    event_type="quarantine_overridden", actor=admin["id"],
                    payload={"reason": reason})
    return {"status": "ingested", "override_reason": reason}


@router.get("/ingestion/cases")
async def list_ingestion_cases(
    status: Optional[str] = None,
    upload_id: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """``upload_id`` scopes the list to one partner file — what the admin's
    per-upload "Preview cases" control needs, so it does not pull every ingest
    case in the system to find the two it is about to plan."""
    return {"cases": _store().list_ingest_cases(status=status, upload_id=upload_id)}


def _default_clinical_question(ic: Dict[str, Any]) -> str:
    """A CASE-SPECIFIC question derived from this chart, for the batch path where
    the admin supplied none (Real-Case Generation PRD §3.3).

    This replaces a two-key per-specialty dictionary plus a generic fallback. A
    default question that says the same thing about every nephrology chart in a
    partner file produces N identical prompts wrapped around N different cases,
    and a physician answering the third one has stopped reading the question.
    Deterministic — no model call — because this runs inside a batch promote that
    already makes two per case."""
    from asclepius import real_cases

    case = ic.get("case") or {}
    return real_cases._fallback_question(
        case, ic.get("specialty") or case.get("specialty"))


async def _convert_and_gate(store: Any, ic: Dict[str, Any], question: str) -> Dict[str, Any]:
    """Run the FULL real-case → V4 conversion + automated tests on ONE ingested
    case WITHOUT committing: render the case prompt, generate candidates
    conditioned on the real case, and run the hardness + real-variant case-judge
    gate. Returns a structured result the caller can preview or commit. Never
    inserts a task or mutates the case — that is the caller's decision."""
    case = ic.get("case") or {}
    # No literal fallback (PRD-I §4.2, Real-Case Generation PRD §3.3). A wrong
    # specialty routes the case to the wrong physician pool and mislabels it in
    # the export, invisibly; "general" is the absence of a specialty and every
    # caller of this function already refuses to promote on it.
    specialty = ic.get("specialty") or case.get("specialty") or "general"
    prompt = asc_cases.render_case_prompt(case, question)
    out: Dict[str, Any] = {
        "ingest_case_id": ic.get("ingest_case_id"),
        "patient_key": ic.get("patient_key"),
        "specialty": specialty, "question": question, "prompt": prompt,
        "case": case, "candidates": [], "generation": None, "judges": {},
        "ok": False, "failures": [], "error": None, "http_status": 200,
    }
    cg = await generate_candidates_ex(prompt, specialty=specialty)
    candidates = cg.get("candidates") or []
    if len(candidates) < 2:
        out["error"] = "Candidate generation unavailable (no LLM key configured?)."
        out["http_status"] = 503
        return out
    from asclepius import real_cases
    from asclepius.critic import run_case_judge, run_hardness_judge
    from asclepius.empirical_difficulty import measure_empirical_difficulty

    # Difficulty is MEASURED here too (Real-Case Generation PRD §3.6). This path
    # used to stamp the literal "hard" on every promoted real case and leave
    # ``empirical_difficulty`` NULL — a claim we could not defend to a buyer, and
    # the reason ``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY=1`` emptied the V4 queue.
    # Structure alone still cannot confer 'hard'; with no live frontier
    # measurement the band is capped at medium and ``measured`` stays False.
    #
    # Gated on the SAME flag as synthetic generation. Live measurement spends real
    # frontier tokens per case, so it is opt-in; calling it unconditionally here
    # billed 2 models × k attempts of answers AND judge calls on every promote an
    # admin made, which nobody asked for and which no other path does.
    if measure_empirical_difficulty_enabled():
        empirical = await measure_empirical_difficulty(case, question)
    else:
        empirical = {
            "value": None, "measured": False, "both_axes": True,
            "note": "live empirical measurement disabled "
                    "(ASCLEPIUS_MEASURE_EMPIRICAL_DIFFICULTY off); difficulty is the "
                    "structural prior only (PRD §9, Real-Case Generation PRD §3.6)",
        }
    difficulty = real_cases.score_difficulty(
        case,
        model_failure_rate=(empirical.get("value") if empirical.get("measured") else None),
    )
    out["difficulty"] = difficulty
    generation: Dict[str, Any] = {
        "mode": "real_case_promote", "ingest_case_id": ic.get("ingest_case_id"),
        "upload_id": ic.get("upload_id"), "case_source": "real_deid",
        "modality": "multimodal", "candidate_gen_model": cg.get("model"),
        "intended_flawed_id": cg.get("intended_flawed_id"),
        "question": question,
        "case_type": asc_cases.case_type_signature(case),
        "empirical_difficulty": {**empirical, "declared": difficulty["score"],
                                 "structural_axes": difficulty["axes"],
                                 "band": difficulty["band"]},
    }
    hj = await run_hardness_judge(prompt, candidates)
    if not hj.get("skipped"):
        generation["hardness"] = {"score": hj.get("hardness_score"),
                                  "axes": hj.get("hardness_axes") or []}
        out["judges"]["hardness"] = generation["hardness"]
    cj = await run_case_judge(case, case_source="real_deid")
    if cj.get("skipped"):
        # FAIL CLOSED for real data: a V4 task must never enter the queue ungated.
        out["error"] = "Case judge unavailable. The real-case gate requires it; try again."
        out["http_status"] = 503
        return out
    generation["case_judge"] = {k: cj.get(k) for k in (
        "coherence", "multimodal_necessity", "reasoning_divergence_potential")}
    out["judges"]["case_judge"] = generation["case_judge"]
    from asclepius.constants import (
        case_coherence_min, case_divergence_min, case_mm_necessity_min,
    )
    failures: List[str] = []
    if (cj.get("coherence") or 0.0) < case_coherence_min():
        failures.append(f"coherence {cj.get('coherence')} < {case_coherence_min()}")
    if (cj.get("multimodal_necessity") or 0.0) < case_mm_necessity_min():
        failures.append(f"multimodal_necessity {cj.get('multimodal_necessity')} < {case_mm_necessity_min()}")
    if (cj.get("reasoning_divergence_potential") or 0.0) < case_divergence_min():
        failures.append(
            f"reasoning_divergence_potential {cj.get('reasoning_divergence_potential')} < {case_divergence_min()}")
    out["candidates"] = candidates
    out["generation"] = generation
    out["failures"] = failures
    out["ok"] = not failures
    if failures:
        out["http_status"] = 422
    return out


def _commit_promoted_task(
    store: Any, ic: Dict[str, Any], conv: Dict[str, Any], admin: Dict[str, Any], *,
    max_labels: int, grounding_mode: Optional[str], independent_mode: Optional[str],
    open_to_all_specialties: bool = False,
) -> Dict[str, Any]:
    """Insert the gated conversion as a partner_ehr V4 task + mark the case promoted."""
    task = store.insert_task(
        prompt=conv["prompt"], specialty=conv["specialty"],
        # The band from ``_convert_and_gate``'s composite, not the literal "hard"
        # this used to stamp on every real case regardless of the chart.
        difficulty=(conv.get("difficulty") or {}).get("band") or "medium",
        capture_reasoning=True, source="partner_ehr",
        candidate_answers=conv["candidates"], max_labels=max(1, int(max_labels or 1)),
        grounding_mode=grounding_mode or DEFAULT_GROUNDING_MODE,
        independent_mode=independent_mode or DEFAULT_INDEPENDENT_MODE,
        case=conv["case"], generation=conv["generation"], created_by=admin["id"],
        # Launch-week fan-out (V4 PRD §4): VISIBILITY only, never max_labels.
        open_to_all_specialties=bool(open_to_all_specialties),
    )
    store.update_ingest_case(ic["ingest_case_id"], status="promoted", task_id=task["task_id"])
    store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                    event_type="case_promoted", actor=admin["id"],
                    payload={"task_id": task["task_id"]})
    return task


@router.post("/ingestion/cases/{ingest_case_id}/promote")
async def promote_ingest_case(
    ingest_case_id: str, body: PromoteCaseRequest,
    background_tasks: BackgroundTasks,
    dry_run: bool = False,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Ingested case → gradable V4 task (PRD §9): attach the clinical question,
    render the case prompt, generate candidates CONDITIONED ON THE REAL CASE,
    gate (hardness + real-variant case judge — no ground-truth dimension: the
    specialist is the answer key), insert as a partner_ehr task. Needs an LLM.

    ``?dry_run=true`` (or ``{"dry_run": true}``) runs every gate and returns the
    sample WITHOUT committing anything — see the block below. The query parameter
    exists because this is a thing you reach for from a terminal mid-debug, and
    it only ever turns the dry run ON: a query string cannot force a commit that
    the body asked to be a dry run."""
    store = _store()
    # OR, never override. Either place asking for a dry run gets one.
    body = body.model_copy(update={"dry_run": bool(body.dry_run or dry_run)})
    ic = store.get_ingest_case(ingest_case_id)
    if not ic:
        raise HTTPException(status_code=404, detail="Ingested case not found")
    # ═══ PRD-I §4.1 — brokering data can never become a task ═══
    # The functional half of the confidentiality requirement, and the easy half to
    # miss because nothing visibly breaks without it. A promoted brokering case
    # gets labelled by a physician and ships inside a training bundle sold to a
    # lab — data the partner sent us to broker, resold as annotation work.
    #
    # Checked BEFORE the status check so the reason an admin sees is the real one:
    # "this is brokering data", not "this case is already promoted".
    #
    # This message is ADMIN-FACING and reaches no provider — it is raised only from
    # endpoints behind require_admin.
    # The case's own purpose OR its upload's — the copy onto the case is
    # best-effort by design (it must never strand an upload), so reading only the
    # case column would let a swallowed copy failure present as NULL and resolve
    # to task_creation. Fail-open on the one check whose job is to fail closed.
    _purpose = store.ingest_case_effective_purpose(ingest_case_id)
    if asc_ingestion.blocks_promotion(_purpose):
        store.log_event(
            entity_type="ingest_case", entity_id=ingest_case_id,
            event_type=("promote_refused_brokering"
                        if asc_ingestion.is_brokering(_purpose)
                        else "promote_refused_unreviewed"),
            actor=admin["id"], payload={"purpose": _purpose})
        raise HTTPException(
            status_code=409, detail=asc_ingestion.promotion_block_reason(_purpose))
    if ic["status"] != "ingested":
        raise HTTPException(status_code=409, detail=f"Case is {ic['status']!r}, not 'ingested'")
    # PRD-I §4.2: a WRONG specialty is worse than a missing one. It routes the case
    # to the wrong physician pool and mislabels it in the export, invisibly, and
    # neither is visible again once the bundle ships. `_convert_and_gate` falls
    # back to a hardcoded literal for a case with no specialty at all, so refuse
    # here rather than let that literal be applied — the admin sets it on the
    # upload (POST /admin/uploads/{id}/specialty) and promotes again.
    # ``general`` is what ingest writes when nothing declared a specialty — a real
    # value in the column, so the earlier emptiness test could never fire and this
    # guard was unreachable. It is the absence of a specialty, not a specialty.
    if asc_ingestion.specialty_is_undetermined(ic.get("specialty")):
        raise HTTPException(
            status_code=409,
            detail="Specialty not determined for this case. Set the specialty on the "
                   "upload before promoting — promoting now would label it with a "
                   "default that routes it to the wrong physician pool.")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="A clinical question is required to promote")
    conv = await _convert_and_gate(store, ic, question)

    # ═══ V4 PRD §5.1 — dry run: inspect without committing ═══
    #
    # Everything above this line already ran: the brokering refusal, the status
    # check, the specialty gate, the full conversion, the candidate generation and
    # both judges. What a dry run skips is ONLY ``_commit_promoted_task`` — no
    # task, no status change to 'promoted', no ``case_promoted`` event. It is
    # idempotent and repeatable, so an admin can iterate on the clinical question
    # until the case clears the floors instead of discovering the band after a
    # physician has been paid to look at it.
    #
    # It returns the SAMPLE even when the gate FAILED, which is the whole point:
    # "this scored 0.4 on divergence" is the information you need to write a
    # sharper question, and a 422 that carries no scores tells you nothing. The
    # caller reads ``tests_passed`` / ``failures``.
    #
    # A conversion ERROR (no LLM key, case judge unavailable) is still an error —
    # a dry run that silently reported "no candidates" as a result would be a
    # worse lie than the 503.
    if body.dry_run:
        if conv["error"]:
            raise HTTPException(status_code=conv["http_status"], detail=conv["error"])
        # Logged, but as an inspection: a dry run is a thing an admin DID, and the
        # audit trail should show that the case was examined before it was
        # promoted (or after it was gated). This writes to the event log only —
        # the ingest case itself is untouched.
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="promote_dry_run", actor=admin["id"],
                        payload={"tests_passed": conv["ok"],
                                 "failures": conv["failures"]})
        return {
            "dry_run": True,
            "committed": False,
            "task_id": None,
            "ingest_case_status": ic["status"],
            "would_promote": bool(conv["ok"]),
            "max_labels": max(1, int(body.max_labels or 1)),
            "open_to_all_specialties": bool(body.open_to_all_specialties),
            "sample": _sample_case_view(conv),
        }

    if conv["error"]:
        raise HTTPException(status_code=conv["http_status"], detail=conv["error"])
    if not conv["ok"]:
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="promote_gated", actor=admin["id"],
                        payload={"failures": conv["failures"]})
        raise HTTPException(status_code=422, detail={
            "error": "case_judge_gate", "failures": conv["failures"],
            "hint": "Try a sharper clinical question, or reject the case. Floors are env-tunable."})
    task = _commit_promoted_task(
        store, ic, conv, admin, max_labels=body.max_labels,
        grounding_mode=body.grounding_mode, independent_mode=body.independent_mode,
        open_to_all_specialties=body.open_to_all_specialties)
    # A real de-identified chart is the highest-value case we produce, and until
    # now promoting one told nobody: no email, no room post, no broadcast.
    await _notify_new_tasks(
        store, background_tasks, _notifiable([task]), admin_id=admin["id"]
    )
    return {"task_id": task["task_id"], "case_source": task.get("case_source"),
            "modality": task.get("modality"),
            "open_to_all_specialties": bool(task.get("open_to_all_specialties"))}


def _sample_case_view(conv: Dict[str, Any]) -> Dict[str, Any]:
    """The reviewable sample for the admin: the public (PHI-stripped) case with
    its labs / notes / EHR records, the rendered prompt, the generated candidate
    answers, and the automated-test (judge) scores."""
    public = asc_cases.public_case(conv.get("case") or {}) or {}
    return {
        "ingest_case_id": conv.get("ingest_case_id"),
        "patient_key": conv.get("patient_key"),
        "specialty": conv.get("specialty"),
        "question": conv.get("question"),
        "prompt": conv.get("prompt"),
        "case": public,
        "candidates": conv.get("candidates") or [],
        "judges": conv.get("judges") or {},
        # The band the task will actually carry, and whether it was measured. An
        # admin approving a sample must see the claim they are approving.
        "difficulty": conv.get("difficulty") or {},
        "tests_passed": bool(conv.get("ok")),
        "failures": conv.get("failures") or [],
        "error": conv.get("error"),
    }


@router.post("/ingestion/uploads/{upload_id}/prepare")
async def prepare_upload_promotion(
    upload_id: str, body: UploadPromoteRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Step 1 of the upload-scoped promote: run the conversion + automated tests
    on ONE sample case from this partner file and return it for review (labs,
    notes, EHR records, candidates, test scores) WITHOUT committing anything. The
    admin reviews, then calls /promote-all to extend case creation to the rest."""
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    # PRD-I §4.1, extended to the PREVIEW step. This endpoint cannot create a task
    # — both promote endpoints are gated, so the invariant holds without it — but
    # it renders the clinical case and sends it to a third-party inference provider
    # to generate candidate answers. Doing that with brokering data is the activity
    # the rule exists to prevent, whether or not a task comes out the other end.
    _purposes = store.ingest_case_purposes_for_upload(upload_id)
    ingested = [c for c in store.list_ingest_cases(upload_id=upload_id)
                if c.get("status") == "ingested"
                and not asc_ingestion.blocks_promotion(
                    _purposes.get(c.get("ingest_case_id"), c.get("purpose")))]
    if not ingested:
        raise HTTPException(status_code=409,
                            detail="No ingested cases awaiting promotion in this upload.")
    ic = ingested[0]
    if asc_ingestion.specialty_is_undetermined(ic.get("specialty")):
        raise HTTPException(
            status_code=409,
            detail="Specialty not determined for this case. Set the specialty on the "
                   "upload before promoting.")
    question = ((body.question or "").strip()
                or _default_clinical_question(ic))
    conv = await _convert_and_gate(store, ic, question)
    if conv["error"]:
        raise HTTPException(status_code=conv["http_status"], detail=conv["error"])
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="promote_sample_prepared", actor=admin["id"],
                    payload={"ingest_case_id": ic.get("ingest_case_id"),
                             "tests_passed": conv["ok"]})
    return {
        "upload_id": upload_id,
        "partner_label": _partner_label_for_upload(store, upload),
        "filename": upload.get("filename"),
        "ingested_count": len(ingested),
        "sample": _sample_case_view(conv),
    }


@router.post("/ingestion/uploads/{upload_id}/promote-all")
async def promote_upload_all(
    upload_id: str, body: UploadPromoteRequest,
    background_tasks: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Step 2 of the upload-scoped promote: after the admin approved the sample,
    extend case creation to EVERY remaining ingested case in this partner file.
    Each case runs the same conversion + gate; passing cases become V4 tasks,
    gated/failed cases stay 'ingested' with the reason recorded."""
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    candidates = [c for c in store.list_ingest_cases(upload_id=upload_id)
                  if c.get("status") == "ingested"]
    # PRD-I §4.1: brokering cases are FILTERED OUT of the batch rather than failing
    # it. An admin promoting a mixed upload should get their task-creation cases
    # promoted and a count of what was skipped — failing the whole batch would push
    # them toward promoting case-by-case, which is the workflow where a brokering
    # case eventually slips through.
    _purposes = store.ingest_case_purposes_for_upload(upload_id)

    def _case_purpose(c: Dict[str, Any]) -> Optional[str]:
        return _purposes.get(c.get("ingest_case_id"), c.get("purpose"))

    def _barred(c: Dict[str, Any]) -> bool:
        return asc_ingestion.blocks_promotion(_case_purpose(c))

    ingested = [c for c in candidates if not _barred(c)]
    skipped_brokering = [c for c in candidates if _barred(c)]
    if skipped_brokering:
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="promote_skipped_brokering", actor=admin["id"],
                        payload={"skipped": len(skipped_brokering)})
    if not ingested:
        if not skipped_brokering:
            detail = "No ingested cases awaiting promotion in this upload."
        else:
            # Name which bar it was. "Held for brokering" and "nobody has read
            # this yet" call for completely different next actions, and a batch
            # that reported the wrong one would send the operator looking for a
            # decision they had already made.
            detail = (f"All {len(skipped_brokering)} case(s) in this upload are "
                      "held. "
                      + asc_ingestion.promotion_block_reason(
                          _case_purpose(skipped_brokering[0])))
        raise HTTPException(status_code=409, detail=detail)
    promoted: List[Dict[str, Any]] = []
    promoted_tasks: List[Dict[str, Any]] = []
    gated: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for ic in ingested:
        # Same guard as the single-case endpoint: never let the hardcoded
        # specialty fallback label a case (PRD-I §4.2). One unlabelled case does
        # not fail the batch — it is reported alongside the gated ones.
        if asc_ingestion.specialty_is_undetermined(ic.get("specialty")):
            gated.append({"ingest_case_id": ic.get("ingest_case_id"),
                          "failures": ["specialty not determined — set it on the "
                                       "upload before promoting"]})
            continue
        question = ((body.question or "").strip()
                    or _default_clinical_question(ic))
        try:
            conv = await _convert_and_gate(store, ic, question)
        except Exception as exc:  # pragma: no cover - defensive per-case isolation
            failed.append({"ingest_case_id": ic.get("ingest_case_id"), "error": str(exc)})
            continue
        if conv["error"]:
            failed.append({"ingest_case_id": ic.get("ingest_case_id"), "error": conv["error"]})
            continue
        if not conv["ok"]:
            store.log_event(entity_type="ingest_case", entity_id=ic.get("ingest_case_id"),
                            event_type="promote_gated", actor=admin["id"],
                            payload={"failures": conv["failures"]})
            gated.append({"ingest_case_id": ic.get("ingest_case_id"), "failures": conv["failures"]})
            continue
        task = _commit_promoted_task(
            store, ic, conv, admin, max_labels=body.max_labels,
            open_to_all_specialties=body.open_to_all_specialties,
            grounding_mode=body.grounding_mode, independent_mode=body.independent_mode)
        promoted.append({"ingest_case_id": ic.get("ingest_case_id"), "task_id": task["task_id"]})
        promoted_tasks.append(task)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="promote_all", actor=admin["id"],
                    payload={"promoted": len(promoted), "gated": len(gated),
                             "failed": len(failed),
                             "skipped_brokering": len(skipped_brokering)})
    # One notification for the whole batch, counted per specialty, rather than
    # one per case: promoting a 100-case partner file must not send a physician
    # 100 emails.
    await _notify_new_tasks(
        store, background_tasks, _notifiable(promoted_tasks), admin_id=admin["id"]
    )
    return {"upload_id": upload_id, "promoted": len(promoted), "gated": len(gated),
            "failed": len(failed), "skipped_brokering": len(skipped_brokering),
            "task_ids": [p["task_id"] for p in promoted],
            "details": {"promoted": promoted, "gated": gated, "failed": failed,
                        "skipped_brokering": [
                            {"ingest_case_id": c.get("ingest_case_id"),
                             "reason": "came in on a brokering link"}
                            for c in skipped_brokering]}}


# ═══════════════════════════════════════════════════════════════════════════════
# Real-case GENERATION (Real-Case Generation PRD §5) — one chart → many V4 tasks
# ═══════════════════════════════════════════════════════════════════════════════
def _proposal_view(p: Dict[str, Any]) -> Dict[str, Any]:
    """The admin-facing shape of one proposed case. Carries the PUBLIC case only:
    a proposal's ``case`` holds the internal answer key (the chart's own subsequent
    course), and this response is rendered in a browser."""
    out = {
        "encounter_index": p.get("encounter_index"),
        "encounter_span": p.get("encounter_span"),
        "n_events": p.get("n_events"),
        "index_event_offset": p.get("index_event_offset"),
        "index_rationale": p.get("index_rationale"),
        "generatable": bool(p.get("generatable")),
        "blockers": p.get("blockers") or [],
        "question": p.get("question"),
        # The proposed question is MODEL OUTPUT until a physician accepts it, and
        # the console's colour semantics turn on exactly that distinction — the UI
        # renders anything sourced 'model' in orange, physician-authored in green.
        "question_source": p.get("question_source"),
        "specialty": p.get("specialty"),
        "specialty_confidence": p.get("specialty_confidence"),
        "taxonomy_bucket": p.get("taxonomy_bucket"),
        "subtopic": p.get("subtopic"),
        "case_type": p.get("case_type"),
        "difficulty": p.get("difficulty"),
        "curation": p.get("curation"),
        # Longitudinal Cases PRD §2 — why this encounter is or is not a decision
        # point, with the measurements. Surfaced on EVERY proposal, including the
        # ones that fail: an admin who sees "3 of 17" needs to read which threshold
        # each skipped encounter missed, or the gate is unarguable.
        "density": p.get("density"),
        "qualifies_as_decision_point": bool(p.get("qualifies_as_decision_point")),
        "outcome_verifiable": bool(p.get("outcome_verifiable")),
    }
    case = p.get("case")
    if case is not None:
        public = asc_cases.public_case(case) or {}
        out["case"] = public
        out["prompt"] = asc_cases.render_case_prompt(case, p.get("question") or "")
        out["content"] = {
            "lab_panels": len(public.get("lab_panels") or []),
            "notes": len(public.get("notes") or []),
            "medications": len(public.get("medications") or []),
            "problem_list": len(public.get("problem_list") or []),
            "studies": len(public.get("studies") or []),
        }
    return out


async def _generate_one_real_case(
    store: Any, ic: Dict[str, Any], p: Dict[str, Any], admin: Dict[str, Any], *,
    max_labels: int, grounding_mode: Optional[str], independent_mode: Optional[str],
    open_to_all_specialties: bool = False,
    # Longitudinal trajectory (PRD 2 §4.2.2). All-or-nothing, passed explicitly by
    # the trajectory batch and by nothing else; ``store.insert_task`` refuses half
    # an identity. An ordinary batch passes neither and produces ordinary tasks,
    # byte-for-byte as before.
    trajectory_id: Optional[str] = None,
    sequence_index: Optional[int] = None,
    # PRD CASE-BATCHES §1 — passed straight through to ``insert_task``. None means
    # "inherit the column default", which is 'open' and is what every ordinary V4
    # batch wants; the trajectory batch passes 'assigned_only'.
    distribution: Optional[str] = None,
) -> Dict[str, Any]:
    """One proposed case → a gated, fully-tagged V4 task. Returns a result dict;
    never raises for a per-case failure, so one bad encounter cannot fail a batch.

    Ordering is load-bearing and mirrors the synthetic pipeline:
      measure difficulty → derive the failure mode → generate candidates KEYED to
      it → hardness → case judge (fail closed) → floors → content + leakage
      assertions → insert.

    Difficulty is measured BEFORE candidates because the failure mode is derived
    from what the frontier models actually got wrong — a trap the models did not
    fall for is not the trap, and keying the flawed answer to an invented one is
    what makes an A/B pair two guesses instead of a preference pair.
    """
    from asclepius import real_cases
    from asclepius.constants import (
        case_coherence_min, case_divergence_min, case_mm_necessity_min,
    )
    from asclepius.critic import run_case_judge, run_hardness_judge
    from asclepius.empirical_difficulty import measure_empirical_difficulty

    case = p["case"]
    specialty = p.get("specialty")
    # A task with no question is a case with nothing asked of it. The plan may
    # legitimately arrive without one (``derive_questions=false`` on a live run, or
    # a model call that failed), so fall back to the deterministic case-specific
    # question rather than inserting a prompt that asks nothing.
    question = p.get("question") or ""
    if not question.strip():
        question = real_cases._fallback_question(case, specialty)
        p["question_source"] = "deterministic"
    result: Dict[str, Any] = {"encounter_index": p.get("encounter_index"),
                              "task_id": None, "failures": [], "error": None}
    if not specialty or not asc_specialties.is_enabled(specialty):
        result["error"] = "specialty not served"
        return result

    # ── content + leakage assertions, on the REAL path this time ──────────────
    # All three exist and none ran here before.
    #
    # ``assert_temporal_split`` is the primary guarantee on a real chart: the
    # visible window is a total temporal split, so an item after day 0 means the
    # split failed. ``assert_no_answer_leakage`` is the secondary check, and it is
    # given the key it was designed for — what was NEWLY ESTABLISHED after the
    # decision point plus the treatment then started. Handing it the chart's whole
    # subsequent state instead fires on every chronic problem the visible chart
    # legitimately names ("ascites" carried on the list for a year is context, not
    # the answer), which would reject four cases in five for no safety gain.
    held_out = p.get("held_out") or {}
    sealed_key = {
        "established": held_out.get("newly_established_problems") or [],
        "treatment": held_out.get("newly_started_drugs") or [],
    }
    try:
        asc_cases.assert_multimodal_content(case)
        real_cases.assert_temporal_split(case)
        asc_ingestion.assert_no_answer_leakage(case, {"answer_key": sealed_key})
    except Exception as exc:
        result["error"] = f"content/leakage gate: {exc}"
        return result

    # ── §3.6 difficulty, measured ────────────────────────────────────────────
    empirical = await measure_empirical_difficulty(case, question)
    failure_rate = empirical.get("value") if empirical.get("measured") else None
    difficulty = real_cases.score_difficulty(
        case, encounters_spanned=p.get("encounters_spanned") or 1,
        bucket_id=p.get("taxonomy_bucket"), model_failure_rate=failure_rate)

    # ── §3.7 the trap, then candidates keyed to it ───────────────────────────
    failure_mode = real_cases.derive_ai_failure_mode(
        case, difficulty, empirical.get("failure_reasons") or [])
    prompt = asc_cases.render_case_prompt(case, question)
    cg = await generate_candidates_ex(prompt, specialty=specialty,
                                      ai_failure_mode=failure_mode)
    candidates = cg.get("candidates") or []
    if len(candidates) < 2:
        # Report what actually went wrong. "no LLM key configured?" was a guess,
        # and it was the wrong guess for a truncated or unparsable response — the
        # two failures a live key produces (asclepius.critic supplies ``reason``).
        result["error"] = ("Candidate generation produced no usable pair: "
                           + (cg.get("reason") or "no reason reported "
                              "(is an LLM key configured?)"))
        return result

    # ── the gates that must stay ─────────────────────────────────────────────
    hj = await run_hardness_judge(prompt, candidates)
    hardness = (None if hj.get("skipped")
                else {"score": hj.get("hardness_score"), "axes": hj.get("hardness_axes") or []})
    cj = await run_case_judge(case, case_source="real_deid")
    if cj.get("skipped"):
        result["error"] = "Case judge unavailable. The real-case gate requires it; try again."
        return result
    case_judge = {k: cj.get(k) for k in (
        "coherence", "multimodal_necessity", "reasoning_divergence_potential")}
    failures: List[str] = []
    for key, floor in (("coherence", case_coherence_min()),
                       ("multimodal_necessity", case_mm_necessity_min()),
                       ("reasoning_divergence_potential", case_divergence_min())):
        if (cj.get(key) or 0.0) < floor:
            failures.append(f"{key} {cj.get(key)} < {floor}")
    if failures:
        result["failures"] = failures
        result["judges"] = {"case_judge": case_judge, "hardness": hardness}
        return result

    # ── §4 the tag contract — a generated V4 case is tagged like a V3 one ────
    generation = {
        "mode": "real_case_generated",
        "engine": ASCLEPIUS_ENGINE,
        "case_source": "real_deid",
        "modality": "multimodal",
        "case_type": asc_cases.case_type_signature(case),
        "taxonomy_bucket": p.get("taxonomy_bucket"),
        "subtopic": p.get("subtopic"),
        "ai_failure_mode": failure_mode,
        "question": question,
        "question_source": p.get("question_source"),
        "intended_flawed_id": cg.get("intended_flawed_id"),
        "candidate_gen_model": cg.get("model"),
        "empirical_difficulty": {
            **empirical,
            "declared": difficulty["score"],
            "structural_axes": difficulty["axes"],
            "band": difficulty["band"],
            "note": (f"k={empirical.get('k')} × {empirical.get('n_models')} frontier "
                     "model(s) vs the sealed held-out outcome; band composited with "
                     "the structural prior (Real-Case Generation PRD §3.6)"),
        },
        "hardness": hardness,
        "case_judge": case_judge,
        "config_version": ASCLEPIUS_CONFIG_VERSION,
        "generated_at": _utcnow_iso(),
        # real-case provenance. NOT the index DATE: the resolved calendar anchor is
        # a re-identification key back into the partner's shifted calendar, and
        # ``timeline`` destroys it on purpose. The relative offset carries the same
        # provenance with none of the exposure.
        "ingest_case_id": ic.get("ingest_case_id"),
        "upload_id": ic.get("upload_id"),
        "encounter_index": p.get("encounter_index"),
        "index_event_offset": p.get("index_event_offset"),
        "index_rationale": p.get("index_rationale"),
        "decision_offset_days": 0,
        "specialty_confidence": p.get("specialty_confidence"),
    }
    if trajectory_id is not None:
        # The walk, echoed on the generation block for the admin and the buyer; the
        # COLUMNS are what the sequence gate and the export read.
        #
        # Deliberately absent: trajectory length and "is this point verifiable".
        # Both are functions of which points actually exist, and a generation run
        # can produce fewer than it planned (a per-encounter gate failure, an admin
        # deleting a point later). Stamping either here would freeze a number that
        # the next event makes wrong, on a buyer-facing field. Both are derived at
        # read time from ``store.trajectory_points``, which cannot go stale.
        generation.update({
            "mode": "real_case_trajectory",
            "trajectory_id": trajectory_id,
            "sequence_index": sequence_index,
            "density": p.get("density"),
        })
    task = store.insert_task(
        prompt=prompt, specialty=specialty,
        # MEASURED, not hardcoded. This is the line the PRD is about.
        difficulty=difficulty["band"],
        capture_reasoning=True, source="partner_ehr",
        candidate_answers=candidates, max_labels=max(1, int(max_labels or 1)),
        grounding_mode=grounding_mode or DEFAULT_GROUNDING_MODE,
        independent_mode=independent_mode or DEFAULT_INDEPENDENT_MODE,
        case=case, generation=generation, created_by=admin["id"],
        # Launch-week fan-out (V4 PRD §4): VISIBILITY only, never max_labels.
        open_to_all_specialties=bool(open_to_all_specialties),
        trajectory_id=trajectory_id, sequence_index=sequence_index,
        distribution=distribution,
    )
    store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                    event_type="real_case_generated", actor=admin["id"],
                    payload={"task_id": task["task_id"],
                             "encounter_index": p.get("encounter_index"),
                             "difficulty": difficulty["band"],
                             "measured": difficulty["measured"],
                             "taxonomy_bucket": p.get("taxonomy_bucket")})
    result["task_id"] = task["task_id"]
    result["specialty"] = task.get("specialty")
    result["difficulty"] = difficulty
    result["judges"] = {"case_judge": case_judge, "hardness": hardness}
    return result


@router.post("/ingestion/cases/{ingest_case_id}/generate")
async def generate_real_cases(
    ingest_case_id: str, body: GenerateRealCasesRequest,
    background_tasks: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """One ingested real chart → many tagged V4 tasks (Real-Case Generation PRD §5).

    ``dry_run`` (the default) returns the FULL plan — every proposed case with its
    index event, question, tags and difficulty band — and writes nothing.

    A live run is EXPENSIVE and synchronous: each case costs a k-sample frontier
    difficulty probe plus a candidate generation, a hardness judge and a case
    judge. Generating a whole chart in one request is deliberate (the admin asked
    for the batch), but ``encounter_indices`` exists so the per-case button costs
    one case, and ``max_cases`` bounds the batch.
    """
    from asclepius import real_cases

    store = _store()
    ic = store.get_ingest_case(ingest_case_id)
    if not ic:
        raise HTTPException(status_code=404, detail="Ingested case not found")
    # PRD-I §4.1 — brokering data can never become a task. Checked BEFORE anything
    # else, and before the dry run too: the plan renders the chart and sends it to
    # a third-party model to author questions, which is the activity the rule
    # exists to prevent whether or not a task comes out the other end.
    _purpose = store.ingest_case_effective_purpose(ingest_case_id)
    if asc_ingestion.blocks_promotion(_purpose):
        store.log_event(
            entity_type="ingest_case", entity_id=ingest_case_id,
            event_type=("generate_refused_brokering"
                        if asc_ingestion.is_brokering(_purpose)
                        else "generate_refused_unreviewed"),
            actor=admin["id"], payload={"purpose": _purpose})
        raise HTTPException(
            status_code=409, detail=asc_ingestion.promotion_block_reason(_purpose))
    if ic["status"] not in ("ingested", "promoted"):
        raise HTTPException(status_code=409,
                            detail=f"Case is {ic['status']!r}, not 'ingested'")

    hint = (body.specialty or "").strip().lower() or None
    if hint and not asc_specialties.is_enabled(hint):
        raise HTTPException(
            status_code=400,
            detail=f"Specialty {hint!r} is not enabled in this release "
                   f"({sorted(s for s in asc_specialties.SPECIALTY_REGISTRY if asc_specialties.is_enabled(s))}).")
    if hint is None and not asc_ingestion.specialty_is_undetermined(ic.get("specialty")):
        # The upload's declared specialty is an admin decision already made.
        hint = str(ic["specialty"]).strip().lower()
        if not asc_specialties.is_enabled(hint):
            hint = None

    try:
        plan = await real_cases.plan_cases(
            ic.get("case") or {}, max_cases=body.max_cases,
            min_gap_days=max(1, int(body.min_gap_days or 7)),
            specialty_hint=hint, derive_questions=body.derive_questions,
            # On a live per-case generate, author ONLY the question we are about to
            # use. A dry run authors all of them, which is the point of the preview.
            question_indices=(None if body.dry_run else body.encounter_indices))
    except real_cases.RealCaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    wanted = set(body.encounter_indices or [])
    selected = [p for p in plan["proposals"]
                if p.get("generatable") and (not wanted or p["encounter_index"] in wanted)]

    # ── Longitudinal trajectory mode (PRD 2 §4, Phase 5) ─────────────────────
    # Same generation pipeline, three differences, each of them deliberate:
    #
    #   1. Only encounters clearing the §2 DENSITY GATE become points. A repeat lab
    #      draw is not a decision, and the gate is the product (§2.1).
    #   2. The points are ORDERED and share a trajectory_id, which is what makes
    #      the sequence gate (§9.1) and the outcome reveal (Phase 4) work at all.
    #   3. ``max_labels`` is forced to 1 (§9.6) — see the note at the loop.
    #
    # Order is by ``encounter_index``, which ``plan_cases`` already returns
    # oldest-first, and it is re-sorted here rather than trusted: the sequence index
    # IS the chronology, and a walk assembled in the wrong order would hand a
    # physician the outcomes of decisions they have not made.
    trajectory_mode = bool(body.trajectory)
    trajectory_id = None
    if trajectory_mode:
        if body.apply_density_gate:
            selected = [p for p in selected if p.get("qualifies_as_decision_point")]
        selected = sorted(selected, key=lambda p: p["encounter_index"])
        trajectory_id = asc_trajectory.new_trajectory_id()

    response: Dict[str, Any] = {
        "ingest_case_id": ingest_case_id,
        "upload_id": ic.get("upload_id"),
        "patient_key": ic.get("patient_key"),
        "encounters": plan["encounters"],
        "generatable": plan["generatable"],
        "selected": len(selected),
        "specialty_hint": hint,
        "dry_run": bool(body.dry_run),
        "proposals": [_proposal_view(p) for p in plan["proposals"]],
        # §2 — the two numbers a chart walk is priced on. Returned on every run,
        # trajectory or not, because they are what an admin needs to decide whether
        # this chart is worth walking.
        "decision_points": plan.get("decision_points"),
        "verifiable_decision_points": plan.get("verifiable_decision_points"),
        "density_gate": plan.get("density_gate"),
        "trajectory": trajectory_mode,
    }
    if body.dry_run:
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="real_case_plan_previewed", actor=admin["id"],
                        payload={"encounters": plan["encounters"],
                                 "generatable": plan["generatable"]})
        return response

    if not selected:
        raise HTTPException(
            status_code=422,
            detail={"error": "nothing_generatable",
                    "blockers": {p["encounter_index"]: p.get("blockers") or []
                                 for p in plan["proposals"]}})

    generated, gated, failed = [], [], []
    # The sequence index advances ONLY on a point that actually became a task, so a
    # walk is dense 0…n−1 even when an encounter fails its case judge. (The reveal
    # tolerates a hole anyway — see ``_outcome_point`` — but a dense walk is what
    # the physician's "step 3 of 13" should count, and a gap in it would read as a
    # missing case rather than as a rejected one.)
    seq = 0
    for p in selected:
        try:
            r = await _generate_one_real_case(
                store, ic, p, admin,
                # PRD 2 §9.6 — trajectory points are SINGLE-LABELLED. They are
                # excluded from the κ pool by construction (§4.2.4), so a second
                # label buys no agreement statistic; it buys a second independent
                # walk of the same chart, which is a different and more expensive
                # product at $75 a point. Forced here rather than defaulted, so an
                # admin cannot double the bill on a 13-point chart by leaving a
                # batch-level ``max_labels`` at 2 without noticing.
                max_labels=(asc_trajectory.TRAJECTORY_MAX_LABELS if trajectory_mode
                            else body.max_labels),
                grounding_mode=body.grounding_mode,
                independent_mode=body.independent_mode,
                open_to_all_specialties=body.open_to_all_specialties,
                trajectory_id=trajectory_id if trajectory_mode else None,
                sequence_index=seq if trajectory_mode else None,
                # PRD CASE-BATCHES §1 — a promoted trajectory point is NOT released
                # to the open queue. Without this, promoting a walk puts all 13
                # points into every approved doctor's queue the moment the rows
                # land, and the first physician to hit "Start new case" is handed
                # decision point 0 of a chart nobody chose to send them.
                #
                # 'assigned_only' with zero assignments is INVISIBLE to doctors, and
                # that is the correct resting state for a promoted walk, not a bug:
                # admin sees it in Batches with an "unrouted" chip and decides who
                # walks it. Exactly one path makes a longitudinal case visible to a
                # physician, and it is an admin pressing Send.
                distribution=("assigned_only" if trajectory_mode else None))
        except Exception as exc:  # pragma: no cover - per-case isolation
            log.warning("real-case generation failed for %s encounter %s: %s",
                        ingest_case_id, p.get("encounter_index"), exc)
            failed.append({"encounter_index": p.get("encounter_index"), "error": str(exc)})
            continue
        if r.get("task_id"):
            generated.append(r)
            seq += 1
        elif r.get("failures"):
            gated.append(r)
        else:
            failed.append(r)

    if generated:
        # The chart is promoted once, on the first task it produced. The ingest case
        # keeps pointing at that task so the existing V4 wall (an unreviewed case
        # must not be served) still resolves through it.
        store.update_ingest_case(ingest_case_id, status="promoted",
                                 task_id=generated[0]["task_id"])
    store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                    event_type="real_cases_generated", actor=admin["id"],
                    payload={"generated": len(generated), "gated": len(gated),
                             "failed": len(failed)})
    # One chart can produce many tasks; they announce as one batch.
    await _notify_new_tasks(
        store, background_tasks, _notifiable(generated), admin_id=admin["id"]
    )
    response.update({
        "generated": len(generated), "gated": len(gated), "failed": len(failed),
        "task_ids": [g["task_id"] for g in generated],
        "details": {"generated": generated, "gated": gated, "failed": failed},
    })
    if trajectory_mode and generated:
        n = len(generated)
        response["trajectory_id"] = trajectory_id
        response["trajectory_points"] = n
        # A walk of N points yields N−1 verifiable ones: the terminal point has no
        # later encounter in the record to be checked against. Stated in the
        # response because it is the number this artifact is SOLD on (§7), and
        # because an admin reading "13 points" should not have to infer that 12 of
        # them carry outcome verification.
        response["trajectory_verifiable_points"] = max(0, n - 1)
        # The cost, before anyone asks. A trajectory is not a discount on physician
        # time; it is N tasks that happen to share a chart (§9.3).
        from asclepius import payments as asc_payments
        response["estimated_cost_usd"] = round(n * asc_payments.tl_rate_cents() / 100.0, 2)
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="real_case_trajectory_generated", actor=admin["id"],
                        payload={"trajectory_id": trajectory_id, "points": n,
                                 "verifiable_points": max(0, n - 1),
                                 "max_labels": asc_trajectory.TRAJECTORY_MAX_LABELS})
    return response
