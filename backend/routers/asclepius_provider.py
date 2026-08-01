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

from ratelimit import client_ip, rate_limiter

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
    """Turn the uploaded file(s) into ONE .zip bundle for the shared pipeline.

    Thin alias for ``asclepius.ingestion.wrap_loose_files`` — the single packing
    implementation shared with the magic-link door (Buyer Response PRD §2 A1), so
    the two upload doors can never drift into accepting different files again."""
    return asc_ingestion.wrap_loose_files(files, specialty=specialty)


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
        # Fail closed on non-durable raw storage too (see the 410 incident).
        ok, why = asc_ingestion.ingest_storage_durable()
        if not ok:
            raise HTTPException(status_code=503, detail=f"Ingestion is disabled: {why}")
        # Audit PRD §P2: the DERIVED image blobs must be as durable as the raw upload.
        from asclepius import assets as asc_assets
        ok, why = asc_assets.asset_storage_durable()
        if not ok:
            raise HTTPException(status_code=503, detail=f"Ingestion is disabled: {why}")

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
    # Persist the encrypted bundle to durable storage BEFORE inserting the row,
    # so the row never carries a null/unreachable raw_path (see the 410 incident).
    upload_id = store.new_upload_id()
    try:
        raw_path = asc_ingestion.store_raw(upload_id, data)
    except Exception as exc:  # disk full, permissions, encrypt failure, …
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="upload_store_failed", actor=provider_user["id"],
                        payload={"error": str(exc)})
        raise HTTPException(status_code=503,
                            detail="Could not store the upload securely — please retry in a moment.")
    upload = store.insert_ingest_upload(
        upload_id=upload_id,
        link_id=_ACCOUNT_LINK_ID, partner_id=provider_user["id"],
        filename=(raw_files[0]["filename"] if len(raw_files) == 1 else "bundle.zip")[:120],
        sha256=digest, size_bytes=len(data), raw_path=raw_path,
        source_ip=(request.client.host if request.client else None),
    )
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


# ════════════════════════════════════════════════════════════════════════════
#  Health-system portal (PRD C) — username + password, cookie session.
#
#  The door a hospital IT contact uses: an admin provisions the account from an
#  organization + email (routers/asclepius_admin.py); the contact signs in with
#  the emailed username/passphrase, is forced to reset it, and uploads. Uploads
#  flow through the SAME shared ingestion pipeline and are stamped with the
#  health_system_id, so they land in the Health Systems admin section.
# ════════════════════════════════════════════════════════════════════════════
import time as _time
import uuid as _uuid
from collections import defaultdict as _defaultdict, deque as _deque

import jwt as _jwt
from fastapi import Response
from pydantic import BaseModel

from asclepius.store import hash_password as _hash_password

_HS_COOKIE = "hs_portal_session"
_HS_LINK_ID = "hs-portal"     # link_id sentinel (shared ingest_uploads row needs one)
_HS_LOCK_THRESHOLD = 5
_HS_LOCK_MINUTES = 15
_GENERIC_LOGIN_MSG = "Incorrect username or password."
_LOCKED_LOGIN_MSG = ("Too many failed sign-in attempts. Please wait "
                     f"{_HS_LOCK_MINUTES} minutes and try again.")

# A real hash to verify unknown-username attempts against, so response timing
# does not reveal whether a username exists.
_DUMMY_HASH = _hash_password(os.urandom(16).hex())

# Unknown usernames have no DB row to count failures on; track them in memory so
# the lockout behaves identically whether or not the username exists. (Real
# accounts use the durable DB counter; this map is only the existence shield.)
_UNKNOWN_FAILS: Dict[str, Any] = _defaultdict(_deque)


def _is_production() -> bool:
    return (os.getenv("ENV") or "").strip().lower() == "production"


def _hs_session_ttl_min() -> int:
    try:
        return max(5, int(os.getenv("ASCLEPIUS_HS_SESSION_TTL_MIN", "720")))
    except ValueError:
        return 720


def _unknown_locked(username: str) -> bool:
    bucket = _UNKNOWN_FAILS[username]
    cutoff = _time.time() - _HS_LOCK_MINUTES * 60
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return len(bucket) >= _HS_LOCK_THRESHOLD


def _record_unknown_fail(username: str) -> None:
    # Bound the map: a username-spray attack must not grow process memory
    # forever. Dropping the shield on overflow fails open, but the per-IP rate
    # limiter still throttles, and real accounts keep their durable DB lock.
    if len(_UNKNOWN_FAILS) > 50_000:
        _UNKNOWN_FAILS.clear()
    _UNKNOWN_FAILS[username].append(_time.time())


class HsLoginRequest(BaseModel):
    username: str
    password: str


class HsPasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str


def _hs_token(username: str, hs_id: str) -> str:
    payload = {
        "typ": "hs_portal",
        "sub": username,
        "hs": hs_id,
        "jti": _uuid.uuid4().hex,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=_hs_session_ttl_min()),
    }
    return _jwt.encode(payload, asc_auth.get_asclepius_secret(), algorithm=asc_auth.ALGORITHM)


def _set_hs_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_HS_COOKIE, value=token, max_age=_hs_session_ttl_min() * 60,
        httponly=True, secure=_is_production(), samesite="strict", path="/",
    )


def require_hs_portal(request: Request) -> Dict[str, Any]:
    """Cookie-session dependency for the health-system portal. Returns the
    portal user row (sans password hash) with ``health_system`` attached."""
    expired = HTTPException(status_code=401, detail="Your session has ended. Please sign in again.")
    token = request.cookies.get(_HS_COOKIE) or ""
    if not token:
        raise expired
    try:
        payload = _jwt.decode(token, asc_auth.get_asclepius_secret(),
                              algorithms=[asc_auth.ALGORITHM])
    except _jwt.PyJWTError:
        raise expired
    if payload.get("typ") != "hs_portal":
        raise expired
    store = _store()
    user = store.get_hs_portal_user(str(payload.get("sub") or ""))
    if not user or not user.get("active"):
        raise expired
    hs = store.get_health_system(user["hs_id"])
    if not hs or not hs.get("active"):
        raise expired
    user.pop("password_hash", None)
    user["health_system"] = hs
    return user


@router.post("/hs/login", dependencies=[Depends(rate_limiter("hs_login", 10, 60))])
async def hs_login(body: HsLoginRequest, request: Request, response: Response):
    """Sign in with the emailed username + password. The failure message never
    reveals whether a username exists, and repeated failures lock the account
    (durably for real accounts, in-memory for unknown names so the two are
    indistinguishable)."""
    store = _store()
    username = (body.username or "").strip().lower()
    password = body.password or ""
    ip = client_ip(request)
    generic = HTTPException(status_code=401, detail=_GENERIC_LOGIN_MSG)
    locked_exc = HTTPException(status_code=429, detail=_LOCKED_LOGIN_MSG,
                               headers={"Retry-After": str(_HS_LOCK_MINUTES * 60)})
    if not username or not password:
        raise generic

    user = store.get_hs_portal_user(username)
    if user is None:
        if _unknown_locked(username):
            raise locked_exc
        verify_password(password, _DUMMY_HASH)   # equalize timing
        _record_unknown_fail(username)
        store.log_event(entity_type="hs_portal", entity_id=username,
                        event_type="login_failed", payload={"reason": "unknown_user", "ip": ip})
        raise generic

    if store.hs_portal_user_locked(username):
        store.log_event(entity_type="hs_portal", entity_id=username,
                        event_type="login_rejected_locked", payload={"ip": ip})
        raise locked_exc

    if not verify_password(password, user.get("password_hash") or ""):
        tripped = store.record_hs_login_failure(
            username, lock_threshold=_HS_LOCK_THRESHOLD, lock_minutes=_HS_LOCK_MINUTES)
        store.log_event(entity_type="hs_portal", entity_id=username,
                        event_type="login_failed",
                        payload={"reason": "bad_password", "locked": tripped, "ip": ip})
        raise locked_exc if tripped else generic

    hs = store.get_health_system(user["hs_id"])
    if not user.get("active") or not hs or not hs.get("active"):
        # A revoked account fails with the same generic message — revealing
        # "this account was disabled" tells an attacker the username is real.
        store.log_event(entity_type="hs_portal", entity_id=username,
                        event_type="login_rejected_inactive", payload={"ip": ip})
        raise generic

    store.mark_hs_login_success(username)
    store.log_event(entity_type="hs_portal", entity_id=username,
                    event_type="login_succeeded", actor=username,
                    payload={"hs_id": user["hs_id"], "ip": ip})
    _set_hs_cookie(response, _hs_token(username, user["hs_id"]))
    return {"ok": True, "username": username, "organization": hs["name"],
            "must_reset": bool(user.get("must_reset"))}


@router.post("/hs/logout")
async def hs_logout(response: Response):
    response.delete_cookie(key=_HS_COOKIE, path="/")
    return {"ok": True}


@router.get("/hs/me")
async def hs_me(portal_user: Dict[str, Any] = Depends(require_hs_portal)):
    return {
        "username": portal_user["username"],
        "organization": portal_user["health_system"]["name"],
        "email": portal_user.get("email"),
        "must_reset": bool(portal_user.get("must_reset")),
    }


@router.post("/hs/password", dependencies=[Depends(rate_limiter("hs_password", 10, 60))])
async def hs_password(
    body: HsPasswordRequest,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Forced first-login reset (and normal change). On the FORCED reset the
    session cookie is proof of identity (the temp passphrase was consumed at
    login); a NORMAL change requires the current password."""
    store = _store()
    if len((body.new_password or "").strip()) < 12:
        raise HTTPException(status_code=400, detail="New password must be at least 12 characters.")
    if not portal_user.get("must_reset"):
        full = store.get_hs_portal_user(portal_user["username"]) or {}
        if not verify_password(body.current_password or "", full.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
    store.set_hs_portal_password(portal_user["username"], body.new_password, must_reset=False)
    store.log_event(entity_type="hs_portal", entity_id=portal_user["username"],
                    event_type="password_reset", actor=portal_user["username"])
    return {"ok": True}


@router.post("/hs/uploads", dependencies=[Depends(rate_limiter("hs_upload", 30, 60))])
async def hs_upload(
    request: Request,
    background: BackgroundTasks,
    files: List[UploadFile] = File(...),
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Accept the health system's file(s) — a .zip or bare .json/.csv/.hl7/.txt —
    bundle via the SHARED ``wrap_loose_files`` packer, and hand off to the shared
    ingestion pipeline. The upload is stamped with the health_system_id so it
    appears under that system in the admin. Specialty is NOT collected here — it
    is a property of the data, determined at ingest."""
    store = _store()
    hs_id = portal_user["hs_id"]
    if portal_user.get("must_reset"):
        raise HTTPException(status_code=403, detail="Reset your password before uploading.")

    # Fail CLOSED in production (mirrors the other two doors): refuse the most
    # sensitive artifact if it cannot be encrypted and durably stored at rest.
    if _is_production():
        import field_crypto
        if not field_crypto.is_configured():
            raise HTTPException(status_code=503,
                                detail="Uploads are temporarily disabled. Please try again later.")
        ok, _why = asc_ingestion.ingest_storage_durable()
        if not ok:
            raise HTTPException(status_code=503,
                                detail="Uploads are temporarily disabled. Please try again later.")
        from asclepius import assets as asc_assets
        ok, _why = asc_assets.asset_storage_durable()
        if not ok:
            raise HTTPException(status_code=503,
                                detail="Uploads are temporarily disabled. Please try again later.")

    cap = asc_ingestion.max_zip_bytes()
    raw_files: List[Dict[str, Any]] = []
    total = 0
    for uf in files:
        content = await uf.read(cap + 1)
        total += len(content)
        if len(content) > cap or total > cap:
            raise HTTPException(status_code=413,
                                detail="This upload is too large. Please split it into smaller "
                                       "batches and send them one at a time.")
        raw_files.append({"filename": uf.filename or "file", "content": content})

    data = asc_ingestion.wrap_loose_files(raw_files, specialty=None)
    if len(data) > cap:
        raise HTTPException(status_code=413,
                            detail="This upload is too large. Please split it into smaller "
                                   "batches and send them one at a time.")
    digest = asc_ingestion.sha256_hex(data)
    upload_id = store.new_upload_id()
    try:
        raw_path = asc_ingestion.store_raw(upload_id, data)
    except Exception as exc:
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="upload_store_failed", actor=portal_user["username"],
                        payload={"error": str(exc), "hs_id": hs_id})
        raise HTTPException(status_code=503,
                            detail="We could not store the upload securely — please retry in a moment.")
    upload = store.insert_ingest_upload(
        upload_id=upload_id, link_id=_HS_LINK_ID, partner_id=hs_id,
        filename=(raw_files[0]["filename"] if len(raw_files) == 1 else "bundle.zip")[:120],
        sha256=digest, size_bytes=len(data), raw_path=raw_path,
        source_ip=(request.client.host if request.client else None),
    )
    store.set_upload_health_system(upload["upload_id"], hs_id)
    store.log_event(entity_type="ingest_upload", entity_id=upload["upload_id"],
                    event_type="upload_received", actor=portal_user["username"],
                    payload={"health_system_id": hs_id, "sha256": digest,
                             "bytes": len(data), "via": "hs_portal"})
    background.add_task(asc_ingestion.process_upload, store, upload["upload_id"])
    return {"upload_id": upload["upload_id"], "status": "received",
            "file_count": len(raw_files), "total_bytes": len(data)}


# Internal upload status → the four plain-language portal states. Anything not
# explicitly Received/Processing/Accepted is "Needs attention" — internal
# vocabulary like `quarantined` must never reach a hospital IT contact.
_HS_PORTAL_STATUS = {
    "received": "received",
    "scanning": "processing",
    "ingested": "accepted",
}
_HS_NEEDS_ATTENTION_DETAIL = (
    "Our team is taking a closer look at this upload. Nothing more is needed "
    "from you right now — we will reach out if anything else is required."
)


def _hs_upload_view(up: Dict[str, Any]) -> Dict[str, Any]:
    status = _HS_PORTAL_STATUS.get(up.get("status") or "", "needs_attention")
    detail = ""
    if status == "needs_attention":
        # A rejected/failed upload gets the actionable copy; everything else the
        # reassuring generic. Raw pipeline reasons ("bad magic bytes") never pass.
        if (up.get("status") or "") in ("rejected", "failed"):
            detail = asc_ingestion.UNREADABLE_UPLOAD_MESSAGE
        else:
            detail = _HS_NEEDS_ATTENTION_DETAIL
    files = [e.get("name") for e in (up.get("files") or [])
             if e.get("kind") != "manifest" and e.get("name")]
    return {
        "upload_id": up["upload_id"],
        "received_at": up.get("created_at"),
        "filename": up.get("filename"),
        "file_count": len(files) or 1,
        "total_bytes": up.get("size_bytes") or 0,
        "status": status,
        "detail": detail,
    }


@router.get("/hs/uploads")
async def hs_uploads(portal_user: Dict[str, Any] = Depends(require_hs_portal)):
    """This health system's uploads — date, filename, size, and one of four
    plain-language states: received · processing · accepted · needs_attention."""
    store = _store()
    ups = store.list_uploads_for_health_system(portal_user["hs_id"])
    return {"uploads": [_hs_upload_view(u) for u in ups]}
