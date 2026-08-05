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

import asyncio
import io
import json
import logging
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.responses import Response as StarletteResponse

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


# ════════════════════════════════════════════════════════════════════════════
#  Provider-facing response discipline (PRD-I §3)
#
#  THE REQUIREMENT: a health system must not be able to tell whether the data
#  they upload is destined for task creation or for brokering. Not from the URL,
#  the page, an API response, an email, an error message, a header, a timing
#  difference, or a response size.
#
#  THE ACTUAL CONTROL is that ``purpose`` never enters provider-reachable code at
#  all — it is a column read only by admin surfaces, enforced by a static test
#  that greps this file and the provider frontend and requires zero hits. Nothing
#  below can save a design that branches on purpose; a branch that returns the
#  same body still differs in the time it took to decide.
#
#  What this route class does is remove the AMBIENT differentials that survive
#  even correct code, and freeze them so a future edit that reintroduces one
#  fails CI rather than shipping:
#
#    * ETag is content-derived and therefore fingerprints the response — deleted.
#    * Cache-Control: no-store on everything. A private/public differential is a
#      direct leak, and these responses have no business in a cache regardless.
#    * Content-Encoding removed. Compression makes size a function of content and
#      hands back the size channel padding is here to close.
#    * Bodies padded to a fixed 4 KB bucket. FIXED buckets, not random padding —
#      random padding is averaged away by an observer who can repeat the request.
#    * One response shape for every failure, so the error paths (written last,
#      reviewed least, and where this always actually breaks) cannot differ.
#    * A fixed time budget, so the response time carries no information about how
#      much work the answer took. The precedent is username-enumeration defence
#      and the lesson transfers exactly: do the same work regardless of the
#      answer.
# ════════════════════════════════════════════════════════════════════════════

# Padding granularity. Every provider-facing JSON body is grown to the next
# multiple of this, so byte length reveals only a coarse bucket.
_PORTAL_PAD_BUCKET = 4096
_PORTAL_PAD_KEY = "_"

# Response-time budget. Every provider-facing response takes at least this long,
# regardless of how much work produced it.
def _portal_time_budget_sec() -> float:
    try:
        return max(0.0, float(os.getenv("ASCLEPIUS_PORTAL_BUDGET_MS", "120")) / 1000.0)
    except ValueError:
        return 0.120


# Chunk uploads are EXEMPT from the time budget, deliberately. Their duration is
# dominated by the number of bytes the client itself chose to send, is identical
# across purposes because the code path is byte-identical, and a fixed budget per
# part would add minutes of pure latency to a 3 GB upload for no gain. Buying
# nothing at a real cost to the partner is not a security control.
_BUDGET_EXEMPT_SUFFIX = "/parts/{part}"

# The ONE failure body. Every provider-facing failure — bad session, expired,
# revoked, wrong purpose, no such upload — is this shape. Only ``detail`` varies,
# and only ever as a function of what the CLIENT sent.
_PORTAL_GENERIC_DETAIL = "That request could not be completed."


def _portal_error(status: int, detail: Optional[str] = None,
                  code: Optional[str] = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"detail": detail or _PORTAL_GENERIC_DETAIL,
                                 "code": code or "request_failed"})


def _pad_json_response(response: StarletteResponse) -> StarletteResponse:
    """Grow a JSON body to the next fixed bucket with an ignored filler field.

    Only JSON is padded; a streamed or binary response has a length that is
    already a function of content the partner supplied, not of anything we chose."""
    body = getattr(response, "body", None)
    ctype = (response.headers.get("content-type") or "").split(";")[0].strip()
    if body is None or ctype != "application/json":
        return response
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return response
    if not isinstance(payload, dict):
        payload = {"data": payload}
    payload.pop(_PORTAL_PAD_KEY, None)
    base = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    # Cost of adding the filler key itself: ,"_":"" → 7 bytes.
    overhead = len(f',"{_PORTAL_PAD_KEY}":""')
    target = ((len(base) + overhead) // _PORTAL_PAD_BUCKET + 1) * _PORTAL_PAD_BUCKET
    payload[_PORTAL_PAD_KEY] = "." * max(0, target - len(base) - overhead)
    out = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    response.body = out
    response.headers["content-length"] = str(len(out))
    return response


def _normalize_portal_headers(response: StarletteResponse) -> None:
    response.headers["cache-control"] = "no-store"
    response.headers["referrer-policy"] = "no-referrer"
    for name in ("etag",              # content-derived → fingerprints the variant
                 "content-encoding"):  # size as a function of content
        if name in response.headers:
            del response.headers[name]


class PortalRoute(APIRoute):
    """Applies the §3.2 checklist to every provider-facing route uniformly.

    Uniformly is the point. Each item below is one line where it is applied, and
    would be N lines and one forgotten endpoint if applied per handler — and the
    forgotten one is always an error path."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, StarletteResponse]]:
        original = super().get_route_handler()
        exempt = self.path.endswith(_BUDGET_EXEMPT_SUFFIX)

        async def portal_handler(request: Request) -> StarletteResponse:
            started = time.perf_counter()
            try:
                response = await original(request)
            except HTTPException as exc:
                # Caught HERE rather than by the app's handler so the failure path
                # gets the same header set, padding and budget as the success path.
                # Error paths diverging from success paths is the single most
                # common way this class of control is defeated.
                detail = exc.detail if isinstance(exc.detail, str) else _PORTAL_GENERIC_DETAIL
                response = _portal_error(exc.status_code, detail)
                for k, v in (exc.headers or {}).items():
                    response.headers[k] = v
            except RequestValidationError:
                response = _portal_error(422, "That request could not be understood.",
                                         code="invalid_request")
            _normalize_portal_headers(response)
            response = _pad_json_response(response)
            if not exempt:
                await asyncio.sleep(
                    max(0.0, _portal_time_budget_sec() - (time.perf_counter() - started)))
            return response

        return portal_handler


# Every provider-reachable route lives on this sub-router, so the discipline is a
# property of the router rather than something each handler has to remember.
# ``include_router`` preserves the route class, and the admin endpoints stay on
# ``router`` — they are allowed (and required) to say the word "brokering".
portal_router = APIRouter(route_class=PortalRoute)

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
@portal_router.get("/provider/me")
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


@portal_router.post("/provider/password",
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


@portal_router.post("/provider/uploads",
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


@portal_router.get("/provider/uploads")
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
import asyncio
import re as _re
import tempfile as _tempfile
import uuid as _uuid

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

# Progressive delay on repeated failures for the same username. Bounded, because
# an unbounded delay is a self-inflicted DoS, and applied on the SAME schedule
# whether or not the username exists — a delay that differed would just move the
# enumeration oracle from the status code into the response time.
_HS_DELAY_CAP_SEC = 2.0


def _is_production() -> bool:
    return (os.getenv("ENV") or "").strip().lower() == "production"


def _hs_session_ttl_min() -> int:
    try:
        return max(5, int(os.getenv("ASCLEPIUS_HS_SESSION_TTL_MIN", "720")))
    except ValueError:
        return 720


def _hs_upload_quota_bytes() -> int:
    """Cumulative upload allowance per health system per rolling window."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_HS_QUOTA_BYTES", str(5 * 1024 * 1024 * 1024))))
    except ValueError:
        return 5 * 1024 * 1024 * 1024


def _hs_quota_window_hours() -> int:
    try:
        return max(1, int(os.getenv("ASCLEPIUS_HS_QUOTA_WINDOW_HOURS", "24")))
    except ValueError:
        return 24


def _progressive_delay_sec(fails: int) -> float:
    if fails <= 1:
        return 0.0
    return min(0.25 * (2 ** (fails - 2)), _HS_DELAY_CAP_SEC)


def _safe_upload_filename(name: Optional[str]) -> str:
    """Reduce an uploaded filename to a safe basename (FIX-C C-2.5).

    The stored name is echoed into a quoted ``Content-Disposition`` on the admin
    download path, so an uploader who controls it controls what the admin's
    browser saves the file as — ``x"; filename="Q3-invoice.pdf`` renames an
    attacker-supplied zip. Non-latin-1 names additionally blew up header
    encoding with a 500. Strip to a conservative character set at INSERT time so
    nothing downstream has to remember to."""
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = _re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return (base or "upload")[:120]


class HsLoginRequest(BaseModel):
    username: str
    password: str


class HsPasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str


def _hs_token(username: str, hs_id: str, *, session_epoch: Any = 0) -> str:
    payload = {
        "typ": "hs_portal",
        "sub": username,
        "hs": hs_id,
        # Binds the session to the password it was issued against, so a password
        # change (or an admin-forced reset) invalidates every outstanding cookie
        # immediately instead of leaving a leaked one live for the session TTL.
        # A monotonic epoch, not a timestamp: _utcnow_iso() truncates to whole
        # seconds, so a same-second change would not have changed the claim.
        "se": int(session_epoch or 0),
        "jti": _uuid.uuid4().hex,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=_hs_session_ttl_min()),
    }
    return _jwt.encode(payload, asc_auth.get_asclepius_secret(), algorithm=asc_auth.ALGORITHM)


def _set_hs_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_HS_COOKIE, value=token, max_age=_hs_session_ttl_min() * 60,
        # Unconditionally Secure. This used to be gated on ENV == "production",
        # which nothing in the deployment actually sets, so a PHI portal's
        # session cookie shipped over plain HTTP. There is no plausible
        # plain-HTTP deployment of this portal, so the gate bought nothing.
        httponly=True, secure=True, samesite="strict", path="/",
    )


def require_hs_portal(request: Request) -> Dict[str, Any]:
    """Cookie-session dependency for the health-system portal. Returns the
    portal user row (sans password hash) with ``health_system`` attached.

    Four things can end a session: expiry, an explicit sign-out (jti denylist),
    a password change since the token was minted, and deactivation of either the
    portal account or the health system."""
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
    if store.hs_token_revoked(str(payload.get("jti") or "")):
        raise expired
    user = store.get_hs_portal_user(str(payload.get("sub") or ""))
    if not user or not user.get("active"):
        raise expired
    if int(payload.get("se") or 0) != int(user.get("session_epoch") or 0):
        raise expired
    hs = store.get_health_system(user["hs_id"])
    if not hs or not hs.get("active"):
        raise expired
    user.pop("password_hash", None)
    user["health_system"] = hs
    user["_jti"] = str(payload.get("jti") or "")
    user["_exp"] = payload.get("exp")
    return user


@portal_router.post("/hs/login", dependencies=[Depends(rate_limiter("hs_login", 10, 60))])
async def hs_login(body: HsLoginRequest, request: Request, response: Response):
    """Sign in with the emailed username + password.

    ONE code path for existing and non-existing usernames. Every observable —
    status code, message, Retry-After, and the progressive delay — is produced
    by the same statements regardless of whether the account is real, so the
    response cannot be used to enumerate which hospitals are partners. Usernames
    here are derived deterministically from the organization name, which is what
    made the old 5th-attempt 429/401 split a partnership-disclosure leak.

    The hard lock is scoped to (username, ip): five wrong guesses stop THAT
    caller, not the hospital. A username-only lock let anyone who could guess
    `massgeneral` hold Mass General out of its own portal indefinitely.
    """
    store = _store()
    username = (body.username or "").strip().lower()
    password = body.password or ""
    ip = client_ip(request)
    generic = HTTPException(status_code=401, detail=_GENERIC_LOGIN_MSG)
    locked_exc = HTTPException(status_code=429, detail=_LOCKED_LOGIN_MSG,
                               headers={"Retry-After": str(_HS_LOCK_MINUTES * 60)})
    if not username or not password:
        raise generic

    # 1. Lock check — before any lookup, identical for real and fake accounts.
    if store.hs_login_locked(username, ip):
        store.log_event(entity_type="hs_portal", entity_id=username,
                        event_type="login_rejected_locked", payload={"ip": ip})
        raise locked_exc

    # 2. Look up and verify. A missing user still costs one hash verification,
    #    so timing does not substitute for the status code as an oracle.
    user = store.get_hs_portal_user(username)
    hs = store.get_health_system(user["hs_id"]) if user else None
    usable = bool(user) and bool(user.get("active")) and bool(hs) and bool(hs.get("active"))
    expected_hash = (user or {}).get("password_hash") or _DUMMY_HASH
    password_ok = verify_password(password, expected_hash)

    # 3. One failure path. A deactivated account fails exactly like a wrong
    #    password — "this account is disabled" would confirm the username.
    if not password_ok or not usable:
        outcome = store.record_hs_login_failure(
            username, ip, lock_threshold=_HS_LOCK_THRESHOLD, lock_minutes=_HS_LOCK_MINUTES)
        delay = _progressive_delay_sec(store.hs_login_failure_signal(username))
        if delay:
            await asyncio.sleep(delay)
        # The reason is recorded for the operator and NEVER returned — the
        # response body is identical in all three cases.
        if not user:
            reason = "unknown_user"
        elif not password_ok:
            reason = "bad_password"
        else:
            reason = "inactive"
        store.log_event(entity_type="hs_portal", entity_id=username,
                        event_type="login_failed",
                        payload={"locked": outcome["locked"], "ip": ip, "reason": reason})
        raise locked_exc if outcome["locked"] else generic

    store.mark_hs_login_success(username, ip)
    store.log_event(entity_type="hs_portal", entity_id=username,
                    event_type="login_succeeded", actor=username,
                    payload={"hs_id": user["hs_id"], "ip": ip})
    _set_hs_cookie(response, _hs_token(username, user["hs_id"],
                                       session_epoch=user.get("session_epoch")))
    return {"ok": True, "username": username, "organization": hs["name"],
            "must_reset": bool(user.get("must_reset"))}


@portal_router.post("/hs/logout")
async def hs_logout(request: Request, response: Response):
    """Sign out for real. The cookie is cleared AND the token's jti is denylisted
    until it would have expired — on a shared hospital workstation a cookie the
    browser already handed out has to stop working, not just stop being sent."""
    response.delete_cookie(key=_HS_COOKIE, path="/")
    token = request.cookies.get(_HS_COOKIE) or ""
    if token:
        try:
            payload = _jwt.decode(token, asc_auth.get_asclepius_secret(),
                                  algorithms=[asc_auth.ALGORITHM])
        except _jwt.PyJWTError:
            return {"ok": True}
        if payload.get("typ") == "hs_portal":
            exp = payload.get("exp")
            expires_at = (datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
                          if exp else datetime.now(timezone.utc).isoformat())
            _store().revoke_hs_token(str(payload.get("jti") or ""), expires_at)
            _store().log_event(entity_type="hs_portal", entity_id=str(payload.get("sub") or ""),
                               event_type="logout", actor=str(payload.get("sub") or ""))
    return {"ok": True}


@portal_router.get("/hs/me")
async def hs_me(portal_user: Dict[str, Any] = Depends(require_hs_portal)):
    return {
        "username": portal_user["username"],
        "organization": portal_user["health_system"]["name"],
        "email": portal_user.get("email"),
        "must_reset": bool(portal_user.get("must_reset")),
    }


@portal_router.post("/hs/password", dependencies=[Depends(rate_limiter("hs_password", 10, 60))])
async def hs_password(
    body: HsPasswordRequest,
    response: Response,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Forced first-login reset (and normal change). On the FORCED reset the
    session cookie is proof of identity (the temp passphrase was consumed at
    login); a NORMAL change requires the current password.

    Changing the password invalidates every session issued against the old one
    (the ``se`` epoch claim), so this re-issues the caller's own cookie — otherwise
    the user would be signed out by their own password change."""
    store = _store()
    if len((body.new_password or "").strip()) < 12:
        raise HTTPException(status_code=400, detail="New password must be at least 12 characters.")
    if not portal_user.get("must_reset"):
        full = store.get_hs_portal_user(portal_user["username"]) or {}
        if not verify_password(body.current_password or "", full.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
    store.set_hs_portal_password(portal_user["username"], body.new_password, must_reset=False)
    fresh = store.get_hs_portal_user(portal_user["username"]) or {}
    _set_hs_cookie(response, _hs_token(portal_user["username"], portal_user["hs_id"],
                                       session_epoch=fresh.get("session_epoch")))
    store.log_event(entity_type="hs_portal", entity_id=portal_user["username"],
                    event_type="password_reset", actor=portal_user["username"])
    return {"ok": True}


@portal_router.post("/hs/uploads", dependencies=[Depends(rate_limiter("hs_upload", 30, 60))])
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

    # Cumulative quota per health system (FIX-C C-2.6). The per-request cap and
    # the per-IP limiter together still allow unbounded total volume from one
    # account; this is the only ceiling on how much a single partner can push.
    quota = _hs_upload_quota_bytes()
    window_start = (datetime.now(timezone.utc)
                    - timedelta(hours=_hs_quota_window_hours())).isoformat()
    used = store.hs_upload_bytes_since(hs_id, window_start)
    if used >= quota:
        raise HTTPException(
            status_code=429,
            detail="You have reached the upload limit for today. Please continue "
                   "tomorrow, or reply to your welcome email and we will arrange "
                   "a secure bulk transfer.",
            headers={"Retry-After": str(_hs_quota_window_hours() * 3600)})

    cap = asc_ingestion.max_zip_bytes()
    remaining = min(cap, max(0, quota - used))
    raw_files: List[Dict[str, Any]] = []
    total = 0
    for uf in files:
        # Read in chunks through a spooled file rather than one read() of
        # cap+1 bytes: the old path materialized the whole upload in RAM, then
        # wrap_loose_files made a second copy and store_raw an encrypted third.
        spool = _tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        size = 0
        while True:
            chunk = await uf.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            total += len(chunk)
            if size > remaining or total > remaining:
                spool.close()
                raise HTTPException(
                    status_code=413,
                    detail="This upload is too large. Please split it into smaller "
                           "batches and send them one at a time.")
            spool.write(chunk)
        spool.seek(0)
        raw_files.append({"filename": _safe_upload_filename(uf.filename),
                          "content": spool.read()})
        spool.close()

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
    # Same two server-side stamps as the chunked door (PRD-I §1.1, §2.1). The
    # single-request path verifies by construction — the digest is computed over
    # the exact bytes just written — so it is verified at the same moment it is
    # stored, and the purpose is joined from the portal account row, never sent.
    store.mark_upload_verified(upload["upload_id"],
                               verified_at=datetime.now(timezone.utc).isoformat())
    store.set_upload_purpose(upload["upload_id"], store.hs_purpose_for(hs_id))
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
    "parsing": "processing",
    "ingested": "accepted",
}

# A healthy upload must never read as a problem. ``parsing`` was missing from the
# map above and is the pipeline's most common in-flight state, so mid-parse
# uploads told the hospital "Needs attention — our team is taking a closer look"
# — support traffic from exactly the external users we cannot support in real
# time. Asserting the whole non-terminal set is mapped means the next status the
# pipeline introduces fails here, loudly, instead of silently reintroducing it.
_UNMAPPED_INFLIGHT = [
    s for s in asc_ingestion._NON_TERMINAL_UPLOAD_STATUSES
    if _HS_PORTAL_STATUS.get(s) not in ("received", "processing")
]
if _UNMAPPED_INFLIGHT:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "hs portal status map is missing in-flight pipeline statuses "
        f"{_UNMAPPED_INFLIGHT!r}; an unmapped status renders to a hospital as "
        "'Needs attention'. Map them to 'processing'."
    )
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


@portal_router.get("/hs/uploads")
async def hs_uploads(portal_user: Dict[str, Any] = Depends(require_hs_portal)):
    """This health system's uploads — date, filename, size, and one of four
    plain-language states: received · processing · accepted · needs_attention."""
    store = _store()
    ups = store.list_uploads_for_health_system(portal_user["hs_id"])
    return {"uploads": [_hs_upload_view(u) for u in ups]}


# ════════════════════════════════════════════════════════════════════════════
#  Chunked, resumable upload (PRD-I §1)
#
#  The multipart POST above still serves small bundles, and stays: it is one
#  request, it works, and nothing is gained by making a hospital send a 4 MB CSV
#  in three phases. It is bounded by the platform's five-minute body timeout,
#  which is why everything larger comes through here instead.
#
#  Note on paths: PRD-I §1.1 writes the declare step as ``POST /hs/uploads``,
#  which is the multipart endpoint's own path and method. Sessions live one
#  segment deeper so both doors can coexist.
# ════════════════════════════════════════════════════════════════════════════
from pydantic import Field as _Field  # noqa: E402

_UPLOAD_OWNER_KIND = "health_system"


class HsUploadDeclareRequest(BaseModel):
    """What the client declares up front.

    There is no ``purpose`` field, and there is no code path by which one could be
    honoured: the purpose is resolved server-side from the portal account row
    (PRD-I §2.1). A client that invents the field is simply ignored."""

    filename: str
    size: int = _Field(gt=0)
    sha256: str
    content_type: Optional[str] = None


def _upload_session_or_404(store: Any, session_id: str,
                           portal_user: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a session, scoped to the caller's health system.

    A session belonging to another hospital is reported as a plain 404 — the same
    answer as one that never existed, so the endpoint cannot be used to probe
    which session ids are real."""
    session = store.get_upload_session(session_id)
    if (not session or session.get("owner_kind") != _UPLOAD_OWNER_KIND
            or session.get("owner_id") != portal_user["hs_id"]):
        raise HTTPException(status_code=404, detail="No such upload.")
    return session


def _hs_upload_preconditions(store: Any, portal_user: Dict[str, Any]) -> None:
    """The gates the multipart door already applies, applied identically here.

    Same checks, same order, same messages — a partner who is blocked must be
    blocked the same way whichever door they used, or the two doors become a way
    to tell them apart."""
    if portal_user.get("must_reset"):
        raise HTTPException(status_code=403, detail="Reset your password before uploading.")
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


def _hs_quota_remaining(store: Any, hs_id: str) -> int:
    """Bytes this health system may still send in the current window, counting
    bytes already committed to OPEN sessions. Counting only completed uploads
    would let a partner declare unlimited concurrent multi-GB sessions and hit the
    quota at complete — after the disk had already been spent."""
    quota = _hs_upload_quota_bytes()
    window_start = (datetime.now(timezone.utc)
                    - timedelta(hours=_hs_quota_window_hours())).isoformat()
    used = store.hs_upload_bytes_since(hs_id, window_start)
    used += store.hs_uploads_bytes_in_open_sessions(hs_id)
    return max(0, quota - used)


@portal_router.post("/hs/uploads/sessions",
                    dependencies=[Depends(rate_limiter("hs_upload", 30, 60))])
async def hs_upload_declare(
    body: HsUploadDeclareRequest,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Declare a bundle and receive its chunk plan.

    Idempotent on the declared bytes: re-declaring the same ``{sha256, size}``
    returns the EXISTING session, so a refresh at 3.2 GB resumes instead of
    restarting. The session id IS the idempotency key."""
    from asclepius import uploads as asc_uploads

    store = _store()
    hs_id = portal_user["hs_id"]
    _hs_upload_preconditions(store, portal_user)

    remaining = _hs_quota_remaining(store, hs_id)
    if remaining <= 0 or body.size > remaining:
        raise HTTPException(
            status_code=429,
            detail="You have reached the upload limit for today. Please continue "
                   "tomorrow, or reply to your welcome email and we will arrange "
                   "a secure bulk transfer.",
            headers={"Retry-After": str(_hs_quota_window_hours() * 3600)})

    # Opportunistic reap of abandoned sessions — no cron needed at pod scale, and
    # it runs on the one event that proves someone is using the feature.
    asc_uploads.reap_stale_sessions(store)
    try:
        session, created = asc_uploads.declare(
            store, owner_kind=_UPLOAD_OWNER_KIND, owner_id=hs_id,
            actor=portal_user["username"], filename=body.filename, size=body.size,
            sha256=body.sha256, content_type=body.content_type,
            # SERVER-SIDE, from the portal account row. Never from the request.
            purpose=store.hs_purpose_for(hs_id))
    except asc_uploads.UploadSessionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    if created:
        store.log_event(entity_type="ingest_upload_session",
                        entity_id=session["session_id"],
                        event_type="upload_session_opened", actor=portal_user["username"],
                        payload={"health_system_id": hs_id, "bytes": body.size,
                                 "parts": session["part_count"]})
    return asc_uploads.public_session(session)


@portal_router.get("/hs/uploads/sessions/{session_id}")
async def hs_upload_session_state(
    session_id: str,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Which parts are already stored — the resume endpoint. An interrupted 4 GB
    upload continues from here rather than starting over."""
    from asclepius import uploads as asc_uploads

    store = _store()
    session = _upload_session_or_404(store, session_id, portal_user)
    return asc_uploads.public_session(session)


@portal_router.put("/hs/uploads/sessions/{session_id}/parts/{part}")
async def hs_upload_part(
    session_id: str, part: int, request: Request,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """One chunk, verified against its own sha256.

    Its own request, so the platform's five-minute body limit applies per chunk
    and is irrelevant at this size. The body is read whole because a part is
    bounded by the server-chosen chunk size — the ceiling is a configuration
    constant, not something the client can grow."""
    from asclepius import uploads as asc_uploads

    store = _store()
    session = _upload_session_or_404(store, session_id, portal_user)
    _hs_upload_preconditions(store, portal_user)
    limit = int(session["chunk_size"])
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(status_code=413,
                            detail=f"A part may not exceed {limit} bytes.")
    try:
        stored = asc_uploads.store_part(
            session, part, body, request.headers.get("x-chunk-sha256"))
    except asc_uploads.UploadSessionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    state = asc_uploads.session_state(session)
    return {"part": stored["part"], "size": stored["size"],
            "received_parts": state["received_parts"],
            "missing_parts": state["missing_parts"],
            "bytes_received": state["bytes_received"]}


@portal_router.post("/hs/uploads/sessions/{session_id}/complete")
async def hs_upload_complete(
    session_id: str, request: Request, background: BackgroundTasks,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Assemble, verify the whole-file digest, and only then create the upload.

    Order matters and is the invariant: the blob is written and verified BEFORE
    the ``ingest_uploads`` row exists, so there is never a row pointing at bytes
    we have not proven. A digest mismatch creates nothing at all."""
    from asclepius import uploads as asc_uploads

    store = _store()
    session = _upload_session_or_404(store, session_id, portal_user)
    _hs_upload_preconditions(store, portal_user)
    hs_id = portal_user["hs_id"]
    try:
        result = asc_uploads.complete(store, session)
    except asc_uploads.UploadIntegrityError as exc:
        store.log_event(entity_type="ingest_upload_session", entity_id=session_id,
                        event_type="upload_session_integrity_failed",
                        actor=portal_user["username"],
                        payload={"health_system_id": hs_id, "code": exc.code})
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except asc_uploads.UploadSessionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    if result.get("already"):
        return {"upload_id": result["upload_id"], "status": "received",
                "sha256": result["sha256"], "total_bytes": result["byte_size"]}

    upload = store.insert_ingest_upload(
        upload_id=result["upload_id"], link_id=_HS_LINK_ID, partner_id=hs_id,
        filename=session.get("filename") or "bundle.zip",
        sha256=result["sha256"], size_bytes=result["byte_size"],
        raw_path=result["raw_path"],
        source_ip=(request.client.host if request.client else None))
    store.set_upload_health_system(upload["upload_id"], hs_id)
    # (sha256, byte_size, verified_at) — the chain-of-custody triple. Stamped only
    # after the digest was recomputed over the assembled bytes and matched.
    store.mark_upload_verified(upload["upload_id"], verified_at=result["verified_at"])
    # Purpose is copied from the authorizing row here, server-side, by joining the
    # session that the server itself stamped at declare (PRD-I §2.1).
    store.set_upload_purpose(upload["upload_id"], session.get("purpose"))
    asc_uploads.finalize(store, session, result)
    store.log_event(entity_type="ingest_upload", entity_id=upload["upload_id"],
                    event_type="upload_received", actor=portal_user["username"],
                    payload={"health_system_id": hs_id, "sha256": result["sha256"],
                             "bytes": result["byte_size"], "via": "hs_portal_chunked",
                             "parts": session["part_count"]})
    background.add_task(asc_ingestion.process_upload, store, upload["upload_id"])
    return {"upload_id": upload["upload_id"], "status": "received",
            "sha256": result["sha256"], "total_bytes": result["byte_size"]}


@portal_router.delete("/hs/uploads/sessions/{session_id}")
async def hs_upload_abort(
    session_id: str,
    portal_user: Dict[str, Any] = Depends(require_hs_portal),
):
    """Give up on an unfinished upload and release its parts immediately, rather
    than waiting for the reaper."""
    from asclepius import uploads as asc_uploads

    store = _store()
    session = _upload_session_or_404(store, session_id, portal_user)
    if session.get("status") == "verified":
        raise HTTPException(status_code=409, detail="This upload has already completed.")
    asc_uploads.abort(store, session)
    return {"ok": True}


# Mount the provider-facing surface. Everything above this line that a health
# system can reach carries the §3 response discipline; the admin endpoints on
# ``router`` deliberately do not.
router.include_router(portal_router)
