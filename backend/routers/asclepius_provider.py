"""Data Provider Portal API (Data Provider Portal PRD §3–§8).

Mounted at ``/api/asclepius`` alongside the main Asclepius router. Three surfaces,
strictly separated by role:

  * ``/admin/data-providers*``  — admin invites / lists / resends / revokes.
  * ``/provider/*``             — the locked-down data-partner portal (upload only).
  * ``/ingestion/*``            — admin inbox: uploads, quarantine review, promote.

Reuses the existing machinery: the SendGrid/SMTP transport + 503 guard, secure
password generation, ``provision_user`` account provisioning, the Asclepius JWT +
role gates, the ``ingestion`` pipeline, and ``insert_task`` (which derives V4 from
the real case). Nothing here is exposed to a ``data_partner`` except ``/provider/*``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from asclepius import auth as asc_auth
from asclepius import cases as asc_cases
from asclepius import ingestion as asc_ingestion
from asclepius.constants import provider_invite_ttl_days, value_real_case_mult
from asclepius.critic import generate_candidates_ex, run_case_judge, run_hardness_judge
from asclepius.schemas import (
    DataProviderInviteRequest,
    PromoteCaseRequest,
    ProviderPasswordRequest,
    QuarantineActionRequest,
)
from asclepius.store import get_store, verify_password
from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import build_data_provider_invite_email
from tenant_utils import generate_secure_password

log = logging.getLogger("asclepius.provider")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius-provider"])


def _store():
    return get_store()


def _email_configured() -> bool:
    return is_email_transport_configured()


def _app_base() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _portal_base() -> str:
    """Where the provider portal lives. ``ASCLEPIUS_PORTAL_URL`` overrides for a
    dedicated host; else the app base (the page is served at ``/provider``)."""
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or _app_base()).strip().rstrip("/")


def _invite_expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=provider_invite_ttl_days())).isoformat()


async def _send_invite(provider: Dict[str, Any], temp_password: str) -> None:
    html_body = build_data_provider_invite_email(
        portal_url=_portal_base(),
        email=provider["email"],
        temporary_password=temp_password,
        org_name=provider.get("org_name") or "",
        specialty=provider.get("specialty") or "",
        note=provider.get("note") or "",
        invite_ttl_days=provider_invite_ttl_days(),
    )
    ok = await send_html_email(
        provider["email"],
        "Send us your clinical data — your Archangel Health upload access",
        html_body,
        importance_headers=True,
    )
    if not ok:
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")


def _public_provider(p: Dict[str, Any], *, store: Any) -> Dict[str, Any]:
    q = store.provider_quality_score(p["provider_id"])
    return {
        "id": p["provider_id"],
        "email": p["email"],
        "org_name": p.get("org_name"),
        "specialty": p.get("specialty"),
        "status": p.get("status"),
        "invited_at": p.get("invited_at"),
        "invite_expires_at": p.get("invite_expires_at"),
        "last_upload_at": p.get("last_upload_at"),
        "uploads": p.get("upload_count"),
        "quality": q,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Admin — Data Providers (Exports tab card, PRD §3)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/admin/data-providers")
async def invite_data_provider(
    body: DataProviderInviteRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Create a ``data_partner`` account + a temporary password, and email the
    provider the portal link + credentials. Idempotent: an existing provider is
    rotated + re-invited (same as Resend). 503 if email isn't configured — the
    same guard onboarding uses (we never create the account without being able to
    tell the provider)."""
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    store = _store()
    pw = generate_secure_password()
    provider = store.provision_data_provider(
        email=body.email, password=pw, org_name=body.org_name,
        specialty=body.specialty, note=body.note, invited_by=admin["id"],
        invite_expires_at=_invite_expiry_iso(),
    )
    await _send_invite(provider, pw)
    store.log_event(
        entity_type="data_provider", entity_id=provider["provider_id"],
        event_type="invite_sent", actor=admin["id"],
        payload={"email": provider["email"], "org": provider.get("org_name")},
    )
    return {
        "provider": _public_provider(provider, store=store),
        "message": f"Invite sent to {provider['email']} — account created, temporary password emailed.",
    }


@router.get("/admin/data-providers")
async def list_data_providers(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    return {"providers": [_public_provider(p, store=store) for p in store.list_data_providers()]}


@router.post("/admin/data-providers/{provider_id}/resend")
async def resend_data_provider_invite(
    provider_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    store = _store()
    existing = store.get_data_provider(provider_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Data provider not found")
    pw = generate_secure_password()
    provider = store.provision_data_provider(
        email=existing["email"], password=pw, org_name=existing.get("org_name"),
        specialty=existing.get("specialty"), note=existing.get("note"),
        invited_by=admin["id"], invite_expires_at=_invite_expiry_iso(),
    )
    await _send_invite(provider, pw)
    store.log_event(entity_type="data_provider", entity_id=provider_id,
                    event_type="invite_resent", actor=admin["id"])
    return {"provider": _public_provider(provider, store=store),
            "message": f"New temporary password emailed to {provider['email']}."}


@router.post("/admin/data-providers/{provider_id}/revoke")
async def revoke_data_provider(
    provider_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    if not store.get_data_provider(provider_id):
        raise HTTPException(status_code=404, detail="Data provider not found")
    provider = store.revoke_data_provider(provider_id)
    store.log_event(entity_type="data_provider", entity_id=provider_id,
                    event_type="access_revoked", actor=admin["id"])
    return {"provider": _public_provider(provider, store=store),
            "message": "Access revoked — the provider can no longer sign in or upload."}


# ════════════════════════════════════════════════════════════════════════════
#  Provider portal — the locked-down data_partner surface (PRD §5)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/provider/me")
async def provider_me(provider_user: Dict[str, Any] = Depends(asc_auth.require_data_partner)):
    store = _store()
    p = store.get_data_provider(provider_user["id"]) or {}
    return {
        "email": provider_user.get("email"),
        "org_name": p.get("org_name"),
        "specialty": p.get("specialty"),
        "status": p.get("status") or "active",
        "must_reset_password": bool(p.get("must_reset_password")),
        "uploads_count": p.get("upload_count") or 0,
    }


@router.post("/provider/password")
async def provider_password(
    body: ProviderPasswordRequest,
    provider_user: Dict[str, Any] = Depends(asc_auth.require_data_partner),
):
    """Forced first-login reset (and normal change). On the FORCED first reset the
    Bearer token is proof of identity (the temp password was consumed at login),
    so ``current_password`` may be blank; a NORMAL change requires it."""
    store = _store()
    p = store.get_data_provider(provider_user["id"]) or {}
    if len((body.new_password or "").strip()) < 12:
        raise HTTPException(status_code=400, detail="New password must be at least 12 characters.")
    if not p.get("must_reset_password"):
        # normal change — verify the current password
        full = store.get_user_by_id(provider_user["id"]) or {}
        if not verify_password(body.current_password or "", full.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
    store.set_user_password(provider_user["id"], body.new_password)
    store.clear_provider_password_reset(provider_user["id"])
    store.log_event(entity_type="data_provider", entity_id=provider_user["id"],
                    event_type="password_reset", actor=provider_user["id"])
    return {"ok": True}


@router.post("/provider/uploads")
async def provider_upload(
    files: List[UploadFile] = File(...),
    provider_user: Dict[str, Any] = Depends(asc_auth.require_data_partner),
):
    """Accept a multipart bundle (files and/or a .zip), enforce the caps, store the
    raw bytes to the encrypted quarantine store, and run the ingestion pipeline.
    Returns the per-file outcome list the portal renders."""
    store = _store()
    p = store.get_data_provider(provider_user["id"]) or {}
    if p.get("must_reset_password"):
        raise HTTPException(status_code=403, detail="Reset your password before uploading.")

    if len(files) > asc_ingestion.max_files():
        raise HTTPException(status_code=400, detail=f"Too many files (max {asc_ingestion.max_files()}).")

    raw_files: List[Dict[str, Any]] = []
    total = 0
    for uf in files:
        content = await uf.read()
        if len(content) > asc_ingestion.max_file_bytes():
            raise HTTPException(status_code=413, detail=f"{uf.filename} exceeds the per-file size limit.")
        total += len(content)
        if total > asc_ingestion.max_total_bytes():
            raise HTTPException(status_code=413, detail="Upload exceeds the total size limit.")
        raw_files.append({"filename": uf.filename or "file", "content": content})

    checksum = asc_ingestion.sha256(b"".join(sorted(asc_ingestion.sha256(f["content"]).encode() for f in raw_files)))
    upload = store.create_upload(
        provider_id=provider_user["id"], provider_email=provider_user.get("email"),
        checksum=checksum, meta={"n_submitted": len(raw_files)},
    )
    # Seal raw bytes at rest (auto-purged after retention).
    for f in raw_files:
        try:
            asc_ingestion.store_raw(upload["upload_id"], f["filename"], f["content"])
        except Exception:
            log.warning("failed to store raw upload file", exc_info=True)
    store.log_event(entity_type="ingest_upload", entity_id=upload["upload_id"],
                    event_type="received", actor=provider_user["id"],
                    payload={"files": len(raw_files), "bytes": total, "checksum": checksum})

    specialty = (p.get("specialty") or "general")
    result = asc_ingestion.process_upload(
        store, upload["upload_id"], raw_files, specialty=specialty,
        actor=provider_user["id"],
    )
    return {
        "upload_id": upload["upload_id"],
        "status": result["status"],
        "files": [{"filename": f["filename"], "detected_type": f["detected_type"],
                   "status": f["status"], "outcome": f.get("outcome")} for f in result["files"]],
    }


@router.get("/provider/uploads")
async def provider_uploads(provider_user: Dict[str, Any] = Depends(asc_auth.require_data_partner)):
    store = _store()
    out = []
    for up in store.list_uploads(provider_id=provider_user["id"]):
        files = store.list_ingest_files(up["upload_id"])
        out.append({
            "upload_id": up["upload_id"],
            "received_at": up["received_at"],
            "status": up["status"],
            "file_count": up["file_count"],
            "total_bytes": up["total_bytes"],
            "reason": up.get("reason"),
            "files": [{"filename": f["filename"], "detected_type": f.get("detected_type"),
                       "status": f["status"], "outcome": f.get("outcome")} for f in files],
        })
    return {"uploads": out}


# ════════════════════════════════════════════════════════════════════════════
#  Admin — Ingestion inbox + quarantine + promote (PRD §6, §8)
# ════════════════════════════════════════════════════════════════════════════
def _upload_detail(store: Any, up: Dict[str, Any]) -> Dict[str, Any]:
    provider = store.get_data_provider(up["provider_id"]) or {}
    return {
        "upload_id": up["upload_id"],
        "provider_id": up["provider_id"],
        "provider_email": up.get("provider_email") or provider.get("email"),
        "received_at": up["received_at"],
        "status": up["status"],
        "file_count": up["file_count"],
        "total_bytes": up["total_bytes"],
        "checksum": up.get("checksum"),
        "reason": up.get("reason"),
        "purged": up.get("purged"),
    }


@router.get("/ingestion/uploads")
async def ingestion_uploads(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    return {"uploads": [_upload_detail(store, u) for u in store.list_uploads()]}


@router.get("/ingestion/uploads/{upload_id}")
async def ingestion_upload_detail(
    upload_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    up = store.get_upload(upload_id)
    if not up:
        raise HTTPException(status_code=404, detail="Upload not found")
    detail = _upload_detail(store, up)
    detail["files"] = store.list_ingest_files(upload_id)
    detail["cases"] = store.list_ingest_cases(upload_id=upload_id)
    detail["quarantine"] = store.list_quarantine(status=None, upload_id=upload_id)
    return detail


@router.post("/ingestion/uploads/{upload_id}/retry")
async def ingestion_retry(
    upload_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Re-run the pipeline for an upload from its sealed raw files (PRD §6). Only
    possible while the raw files are still within the retention window."""
    store = _store()
    up = store.get_upload(upload_id)
    if not up:
        raise HTTPException(status_code=404, detail="Upload not found")
    if up.get("purged"):
        raise HTTPException(status_code=410, detail="Raw files were purged after the retention window; cannot re-run.")
    raw = _load_raw_files(upload_id)
    if not raw:
        raise HTTPException(status_code=410, detail="No raw files available to re-run.")
    result = asc_ingestion.process_upload(store, upload_id, raw,
                                          specialty="general", actor=admin["id"])
    return {"upload_id": upload_id, "status": result["status"], "result": result}


def _load_raw_files(upload_id: str) -> List[Dict[str, Any]]:
    import field_crypto

    up_dir = os.path.join(asc_ingestion._raw_dir(), upload_id)  # noqa: SLF001
    if not os.path.isdir(up_dir):
        return []
    out: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(up_dir)):
        path = os.path.join(up_dir, fn)
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        name = fn
        if fn.endswith(".enc"):
            name = fn[:-4]
            try:
                dec = field_crypto.decrypt_bytes(blob)
                if dec is not None:
                    blob = dec
            except Exception:
                continue
        out.append({"filename": name, "content": blob})
    return out


@router.get("/ingestion/quarantine")
async def ingestion_quarantine(
    status: Optional[str] = "open", _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    store = _store()
    return {"quarantine": store.list_quarantine(status=status)}


@router.post("/ingestion/quarantine/{q_id}/{action}")
async def ingestion_quarantine_action(
    q_id: str, action: str, body: QuarantineActionRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Resolve a quarantine item: ``reject`` · ``remap`` · ``scrub`` · ``override``
    (override is admin-only + logged). Findings stay masked — this never renders a
    suspected identifier in cleartext (PRD §6)."""
    if action not in ("reject", "remap", "scrub", "override"):
        raise HTTPException(status_code=400, detail="Unknown quarantine action")
    store = _store()
    item = store.get_quarantine(q_id)
    if not item:
        raise HTTPException(status_code=404, detail="Quarantine item not found")
    status_map = {"reject": "rejected", "remap": "remapped",
                  "scrub": "scrubbed", "override": "overridden"}
    resolved = store.resolve_quarantine(
        q_id, status=status_map[action], resolution=body.note, resolved_by=admin["id"]
    )
    store.log_event(entity_type="ingest_quarantine", entity_id=q_id,
                    event_type=f"quarantine_{action}", actor=admin["id"],
                    payload={"upload_id": item.get("upload_id"), "note": body.note})
    return {"item": resolved}


@router.post("/ingestion/cases/{ic_id}/promote")
async def promote_ingested_case(
    ic_id: str, body: PromoteCaseRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Promote an ingested real case to a **V4 task** (PRD §8). Renders the case +
    question into the prompt, generates strong + plausibly-flawed candidates
    CONDITIONED on the real case (``intended_flawed_id`` kept server-side), and
    inserts the task — whose ``modality`` auto-derives to multimodal and whose
    ``case_source=real_deid`` makes it a V4-only task via the routing wall.

    The case judge runs the REAL-case variant: for ``real_deid`` we skip
    ``ground_truth_determinable`` (a real case has no synthetic answer key — the
    specialist is the answer key), keeping coherence + multimodal-necessity +
    reasoning-divergence for provenance only (advisory, never a hard drop here)."""
    store = _store()
    ic = store.get_ingest_case(ic_id)
    if not ic:
        raise HTTPException(status_code=404, detail="Ingested case not found")
    if ic.get("status") == "promoted" and ic.get("task_id"):
        raise HTTPException(status_code=409, detail="This case has already been promoted.")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="A clinical question is required to promote the case.")

    case = dict(ic.get("case") or {})
    case["case_source"] = "real_deid"  # provenance is authoritative (the V4 wall)
    case.setdefault("case_id", ic_id)
    specialty = case.get("specialty") or "general"
    prompt = asc_cases.render_case_prompt(case, question)

    cg = await generate_candidates_ex(prompt, specialty=specialty)
    candidates = cg.get("candidates") or []
    if len(candidates) < 2:
        raise HTTPException(
            status_code=502,
            detail="Candidate generation is unavailable (check ANTHROPIC_API_KEY / candidate-gen model).",
        )

    # Advisory judges (real-case variant: ground_truth_determinable is skipped).
    hj = await run_hardness_judge(prompt, candidates)
    cj = await run_case_judge(case)
    case_judge = None
    if not cj.get("skipped"):
        case_judge = {k: v for k, v in cj.items() if k != "ground_truth_determinable"}

    generation: Dict[str, Any] = {
        "candidate_gen_model": cg.get("model"),
        "modality": "multimodal",
        "case_source": "real_deid",
        "case_id": case.get("case_id"),
        "provenance": "data_provider_portal",
        "ingest_case_id": ic_id,
        "upload_id": ic.get("upload_id"),
        # server-side only; stripped from the blinded eval screen
        "intended_flawed_id": cg.get("intended_flawed_id"),
    }
    if not hj.get("skipped"):
        generation["hardness"] = {"score": hj.get("hardness_score"),
                                  "axes": hj.get("hardness_axes") or [],
                                  "judge_model": hj.get("model")}
    if case_judge is not None:
        generation["case_judge"] = case_judge

    task = store.insert_task(
        prompt=prompt,
        specialty=specialty,
        capture_reasoning=bool(body.capture_reasoning),
        source="lab_supplied",
        candidate_answers=candidates,
        case=case,                    # -> modality auto-derives to multimodal
        generation=generation,
        created_by=admin["id"],
    )
    store.update_ingest_case(ic_id, status="promoted", task_id=task["task_id"])
    store.log_event(entity_type="ingest_case", entity_id=ic_id,
                    event_type="promoted_to_v4", actor=admin["id"],
                    payload={"task_id": task["task_id"], "case_source": "real_deid"})
    return {
        "task_id": task["task_id"],
        "portal_version": "v4",
        "case_source": "real_deid",
        "modality": task.get("modality"),
        "value_real_case_mult": value_real_case_mult(),
        "message": "Promoted to the V4 real-cases queue.",
    }
