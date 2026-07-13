"""Data Provider Portal — email + password door (EHR Ingestion PRD §4).

A complement to the magic-link uploader already in ``routers/asclepius.py``: this
is the account-based flow — an admin invites a provider by email, the provider
signs in with the emailed email+password, is forced to reset it, and uploads.
Uploads flow through the SAME ingestion pipeline (``asc_ingestion.process_upload``)
and land in the SAME admin inbox / quarantine / promote-to-V4 surface — there is
no second pipeline. This router only adds the front door.

Three surfaces, strictly role-separated:
  * ``/admin/data-providers*`` — admin invites / lists / resends / revokes.
  * ``/provider/*``            — the locked-down data_partner portal (upload only).
The ingestion inbox + quarantine + promote endpoints already live in
``routers/asclepius.py`` and are reused unchanged.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile

from ratelimit import rate_limiter

from asclepius import auth as asc_auth
from asclepius import ingestion as asc_ingestion
from asclepius.schemas import (
    DataProviderInviteRequest,
    ProviderPasswordRequest,
)
from asclepius.store import get_store, verify_password
from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import build_data_provider_invite_email
from tenant_utils import generate_secure_password

log = logging.getLogger("asclepius.provider")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius-provider"])

# link_id sentinel for account-door uploads (the shared ingest_uploads row needs a
# non-null link_id; there is no upload link in the account flow).
_ACCOUNT_LINK_ID = "account"


def _store():
    return get_store()


def _email_configured() -> bool:
    return is_email_transport_configured()


def _app_base() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _portal_base() -> str:
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or _app_base()).strip().rstrip("/")


def _invite_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("ASCLEPIUS_PROVIDER_INVITE_TTL_DAYS", "14")))
    except ValueError:
        return 14


def _invite_expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=_invite_ttl_days())).isoformat()


async def _send_invite(provider: Dict[str, Any], temp_password: str) -> None:
    html_body = build_data_provider_invite_email(
        portal_url=_portal_base(),
        email=provider["email"],
        temporary_password=temp_password,
        org_name=provider.get("org_name") or "",
        specialty=provider.get("specialty") or "",
        note=provider.get("note") or "",
        invite_ttl_days=_invite_ttl_days(),
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
        "uploads": q["total_uploads"],
        "quality": q,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Admin — Data Providers
# ════════════════════════════════════════════════════════════════════════════
@router.post("/admin/data-providers")
async def invite_data_provider(
    body: DataProviderInviteRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Create a data_partner account + temporary password, and email the provider
    the portal link + credentials. Idempotent (existing provider → rotate + re-
    invite). 503 if email isn't configured — we never create the account without
    being able to tell the provider."""
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
    store.log_event(entity_type="data_provider", entity_id=provider["provider_id"],
                    event_type="invite_sent", actor=admin["id"],
                    payload={"email": provider["email"], "org": provider.get("org_name")})
    return {"provider": _public_provider(provider, store=store),
            "message": f"Invite sent to {provider['email']} — account created, temporary password emailed."}


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
#  Provider portal — the locked-down data_partner surface
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
        "uploads_count": store.provider_quality_score(provider_user["id"])["total_uploads"],
    }


@router.post("/provider/password",
             dependencies=[Depends(rate_limiter("provider_password", 10, 60))])
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
        full = store.get_user_by_id(provider_user["id"]) or {}
        if not verify_password(body.current_password or "", full.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
    store.set_user_password(provider_user["id"], body.new_password)
    store.clear_provider_password_reset(provider_user["id"])
    store.log_event(entity_type="data_provider", entity_id=provider_user["id"],
                    event_type="password_reset", actor=provider_user["id"])
    return {"ok": True}


def _bundle_zip(files: List[Dict[str, Any]], *, specialty: Optional[str]) -> bytes:
    """Turn the uploaded file(s) into ONE .zip bundle for the shared pipeline. A
    single already-zip upload is passed through untouched; loose files are packed
    into a fresh zip (with a manifest.json carrying the provider's specialty when
    the bundle doesn't already include one)."""
    if len(files) == 1 and (files[0]["content"][:2] == b"PK"
                            or files[0]["filename"].lower().endswith(".zip")):
        return files[0]["content"]
    has_manifest = any(os.path.basename(f["filename"]).lower() == "manifest.json" for f in files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(os.path.basename(f["filename"]) or "file", f["content"])
        if not has_manifest and specialty:
            import json as _json
            z.writestr("manifest.json", _json.dumps({"specialty": specialty}))
    return buf.getvalue()


@router.post("/provider/uploads",
             dependencies=[Depends(rate_limiter("provider_upload", 30, 60))])
async def provider_upload(
    request: Request,
    background: BackgroundTasks,
    files: List[UploadFile] = File(...),
    provider_user: Dict[str, Any] = Depends(asc_auth.require_data_partner),
):
    """Accept the provider's file(s), bundle to a zip, and hand off to the SHARED
    ingestion pipeline (unpack/parse/verify run in the BACKGROUND, never in the
    request path — PRD §4). Returns a receipt; the portal polls GET /provider/
    uploads for the real per-file outcome."""
    store = _store()
    p = store.get_data_provider(provider_user["id"]) or {}
    if p.get("must_reset_password"):
        raise HTTPException(status_code=403, detail="Reset your password before uploading.")

    # Fail CLOSED in production: the raw partner bundle is the most sensitive
    # artifact — refuse it if it cannot be encrypted at rest (mirrors the
    # magic-link uploader).
    if (os.getenv("ENV") or "").strip().lower() == "production":
        import field_crypto
        if not field_crypto.is_configured():
            raise HTTPException(status_code=503,
                                detail="Ingestion is disabled: DATA_ENCRYPTION_KEY is not configured.")

    cap = asc_ingestion.max_zip_bytes()
    raw_files: List[Dict[str, Any]] = []
    total = 0
    for uf in files:
        content = await uf.read(cap + 1)
        total += len(content)
        if len(content) > cap or total > cap:
            raise HTTPException(status_code=413, detail="Upload exceeds the size limit.")
        raw_files.append({"filename": uf.filename or "file", "content": content})

    data = _bundle_zip(raw_files, specialty=p.get("specialty"))
    if len(data) > cap:
        raise HTTPException(status_code=413, detail="Bundle exceeds the size limit.")
    digest = asc_ingestion.sha256_hex(data)
    upload = store.insert_ingest_upload(
        link_id=_ACCOUNT_LINK_ID, partner_id=provider_user["id"],
        filename=(raw_files[0]["filename"] if len(raw_files) == 1 else "bundle.zip")[:120],
        sha256=digest, size_bytes=len(data), raw_path=None,
        source_ip=(request.client.host if request.client else None),
    )
    raw_path = asc_ingestion.store_raw(upload["upload_id"], data)
    store.update_ingest_upload(upload["upload_id"], raw_path=raw_path)
    store.log_event(entity_type="ingest_upload", entity_id=upload["upload_id"],
                    event_type="upload_received", actor=provider_user["id"],
                    payload={"partner_id": provider_user["id"], "sha256": digest,
                             "bytes": len(data), "via": "account"})
    background.add_task(asc_ingestion.process_upload, store, upload["upload_id"])
    return {
        "upload_id": upload["upload_id"],
        "status": "received",
        "files": [{"filename": f["filename"], "detected_type": None,
                   "status": "received", "outcome": "queued for processing"} for f in raw_files],
    }


# outcome (main pipeline) → provider-facing per-file status the portal knows.
_OUTCOME_STATUS = {
    "parsed": "parsed", "used": "parsed", "rejected_imaging": "excluded",
}
# upload status (main pipeline) → provider-facing upload status the portal knows.
_UPLOAD_STATUS = {"scanning": "parsing", "rejected": "failed"}


def _provider_file_view(e: Dict[str, Any]) -> Dict[str, Any]:
    outcome = e.get("outcome") or ""
    if outcome.startswith("parse_failed"):
        status, shown = "needs_review", "could not be parsed"   # mask the raw exc
    elif outcome in _OUTCOME_STATUS:
        status, shown = _OUTCOME_STATUS[outcome], outcome
    else:
        status, shown = "needs_review", outcome or "needs review"
    return {"filename": e.get("name"), "detected_type": e.get("kind"),
            "status": status, "outcome": shown}


@router.get("/provider/uploads")
async def provider_uploads(provider_user: Dict[str, Any] = Depends(asc_auth.require_data_partner)):
    """The provider's OWN uploads + status (mapped to plain-English states). Scoped
    to this provider by partner_id — never another provider's data."""
    store = _store()
    out = []
    for up in store.list_ingest_uploads(limit=500):
        if up.get("partner_id") != provider_user["id"]:
            continue
        files = [_provider_file_view(e) for e in (up.get("files") or [])
                 if e.get("kind") != "manifest"]
        out.append({
            "upload_id": up["upload_id"],
            "received_at": up["created_at"],
            "status": _UPLOAD_STATUS.get(up["status"], up["status"]),
            "file_count": len(files) or 1,
            "total_bytes": up.get("size_bytes") or 0,
            "reason": up.get("reason"),
            "files": files,
        })
    return {"uploads": out}
