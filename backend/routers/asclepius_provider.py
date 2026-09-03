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
import hashlib as _hashlib
import io
import json
import logging
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response as StarletteResponse

from ratelimit import client_ip, global_rate_limiter, rate_limiter

from asclepius import auth as asc_auth
from asclepius import dla as asc_dla
from asclepius import hs_access
from asclepius import hs_billing
from asclepius import hs_provisioning as asc_hs_provisioning
from asclepius import hs_states
from asclepius import ingestion as asc_ingestion
from asclepius import passwords as asc_passwords
from asclepius import portal_accounts as asc_portal_accounts
from asclepius.schemas import (
    DataProviderInviteRequest,
    ProviderPasswordRequest,
)
from asclepius.store import _utcnow_iso as asc_store_utcnow
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
        # A list or scalar body cannot carry a filler key without CHANGING ITS
        # SHAPE, and silently wrapping it in {"data": …} would break any client
        # reading it. No provider route returns one today; if one ever does, the
        # right answer is to give it an envelope deliberately, not to have this
        # function rewrite it behind the author's back. Left unpadded, and the
        # golden header/shape tests will show it.
        return response
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
            except Exception:
                # An UNEXPECTED failure must not escape into Starlette's default
                # 500 — that response is plain text with a different header set,
                # no no-store, no padding and no time budget, so the one shape
                # that skips the whole discipline would be the one nobody wrote
                # deliberately. Logged with the traceback; the partner is told
                # nothing beyond the generic body.
                log.exception("unhandled error on provider route %s", self.path)
                response = _portal_error(500, "Something went wrong on our side. "
                                              "Please try again in a moment.",
                                         code="request_failed")
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
    # The fourth upload door, recording its provenance like the other three: it
    # names the authorizing ROW (this account) and the store joins everything
    # derived from it, server-side. Deliberately NOT a setter — this door is given
    # no way to express what it is handing over, which is what PRD-I §3.3 (and
    # tests/test_purpose_isolation.py) require of provider-reachable code. Before
    # this call the door recorded nothing at all, and the admin side had to guess.
    store.attach_upload_provenance(upload["upload_id"],
                                   provider_id=provider_user["id"])
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
import secrets
import tempfile as _tempfile
import uuid as _uuid

import jwt as _jwt
from fastapi import Response
from pydantic import BaseModel

from asclepius.store import hash_password as _hash_password

_HS_COOKIE = "hs_portal_session"
#: What a portal account waiting on a decision is told when it reaches the one
#: surface it does not have. A module constant so the copy cannot drift between
#: the route dependency and the precondition helper, which guard the same thing
#: from two directions.
_HS_REVIEW_MSG = ("Uploading opens once we have reviewed your account. "
                  "We will email you when it does.")
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


def require_hs_surface(surface: str):
    """Gate a portal route on a surface rather than on a remembered check.

    Mirrors ``asc_auth.require_surface`` on the physician plane, minus its
    ``AUTH_GATE_HEADER``: the header set on provider-facing responses is frozen
    by ``test_link_indistinguishability``, and one more header here would be one
    more differential to reason about. The portal learns its own state from
    ``/hs/me``, which it has always called first, so the header bought nothing.
    """
    def _dep(portal_user: Dict[str, Any] = Depends(require_hs_portal)) -> Dict[str, Any]:
        if not hs_access.can_surface(portal_user, surface):
            raise HTTPException(status_code=403, detail=_HS_REVIEW_MSG)
        return portal_user
    return _dep


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
    hs = portal_user["health_system"]
    return {
        "username": portal_user["username"],
        "organization": hs["name"],
        "email": portal_user.get("email"),
        "full_name": portal_user.get("full_name"),
        "must_reset": bool(portal_user.get("must_reset")),
        # What the rail may show. The client denies on absence, so an older
        # cached payload is not permission.
        "surfaces": sorted(hs_access.surfaces(portal_user)),
        # Partner words, never the raw approval_status. Same rule the upload
        # status map follows: our queue vocabulary is ours.
        "account_state": hs_access.account_state(portal_user),
        # Whether to route them into the intake form on arrival.
        #
        # The second clause is the one that matters. Without it every hospital
        # provisioned before intake existed gets ambushed by a form on its next
        # login, having already told us all of this on a call months ago. They
        # still SEE the tab and can fill it in whenever they like; they are just
        # never made to.
        "intake_needed": bool(
            hs.get("intake_at") is None
            and (portal_user.get("approval_status") or "").strip().lower() == "pending"
        ),
        # How far through onboarding the ORGANIZATION is, and what happens next.
        # A second axis from `surfaces`, which is about this account: see
        # asclepius/hs_states.py. Both are sent because the portal renders by
        # state and gates its rail by surface, and conflating them would make a
        # member of a signed organization look unsigned.
        **hs_states.public_view(hs),
        # Who signed, and when. Named in §0.1.2: a member who opens the portal
        # after somebody else signed must see that rather than a second
        # signature request.
        "agreement": _hs_agreement_summary(_store(), hs),
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
    request_id: str = Form(default=""),
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
):
    """Accept the health system's file(s) — a .zip or bare .json/.csv/.hl7/.txt —
    bundle via the SHARED ``wrap_loose_files`` packer, and hand off to the shared
    ingestion pipeline. The upload is stamped with the health_system_id so it
    appears under that system in the admin. Specialty is NOT collected here — it
    is a property of the data, determined at ingest.

    ``request_id`` is optional and names the data request this answers, when it
    answers one."""
    store = _store()
    hs_id = portal_user["hs_id"]
    # Approval, forced reset, and the production fail-closed checks, in one
    # place shared with the chunked door. This block used to be duplicated here
    # verbatim, which held right up until a gate was added to only one copy.
    _hs_upload_preconditions(store, portal_user)
    # After the gates, not before: a partner who may not upload at all should be
    # told that, not told their request id is stale.
    answers_request = _resolve_upload_request(store, request_id)

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
    if answers_request:
        store.set_upload_request(upload["upload_id"], answers_request)
    # Same two server-side stamps as the chunked door (PRD-I §1.1). The single-
    # request path verifies by construction — the digest is computed over the exact
    # bytes just written — so it is verified at the moment it is stored, and its
    # provenance is joined from the account that sent it.
    store.mark_upload_verified(upload["upload_id"],
                               verified_at=asc_store_utcnow())
    store.attach_upload_provenance(upload["upload_id"],
                                   portal_username=portal_user["username"])
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
        # ─ Integrity, shown to the partner (PRD §6) ─
        # The digest WE computed over the bytes WE stored, not the one the
        # client declared. Handing back their own number would prove nothing;
        # this is the receipt that says the file crossed the wire intact, and it
        # is the same value the chunked handshake verified before the row was
        # created. Empty for an upload that never got far enough to have one.
        "sha256": up.get("sha256") or "",
        # When it stopped being in flight. The same timestamp the status came
        # from, named for what a partner actually wants to know.
        "verified_at": up.get("updated_at") if up.get("sha256") else None,
    }


#: The four partner-facing states, in the order a bundle moves through them.
#: Named here rather than derived from ``_HS_PORTAL_STATUS.values()`` so a
#: summary always carries all four keys, including the ones that are zero: a
#: page that reads ``summary.accepted`` must not have to guard for absence.
_HS_UPLOAD_STATES = ("received", "processing", "accepted", "needs_attention")


def _hs_upload_summary(views: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The accounting a partner can check their own records against.

    Takes the ALREADY-MAPPED views rather than raw rows, so the counts can never
    disagree with the list rendered beside them. Deriving them from a second
    query would let the two answers drift the first time the status map changes.
    """
    counts = {state: 0 for state in _HS_UPLOAD_STATES}
    accepted_bytes = 0
    for view in views:
        state = view.get("status") or "needs_attention"
        counts[state] = counts.get(state, 0) + 1
        if state == "accepted":
            accepted_bytes += int(view.get("total_bytes") or 0)
    return {**counts, "total": len(views), "accepted_bytes": accepted_bytes}


@portal_router.get("/hs/uploads")
async def hs_uploads(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
):
    """This health system's uploads — date, filename, size, and one of four
    plain-language states: received · processing · accepted · needs_attention.

    Gated on the UPLOAD surface, like every door that writes one. A pending
    account used to be handed a 200 and an empty list here, which reads as "you
    have sent us nothing" when the truth is "you may not use this yet", and it
    was the one upload surface that answered differently from its four siblings.
    """
    store = _store()
    ups = store.list_uploads_for_health_system(portal_user["hs_id"])
    views = [_hs_upload_view(u) for u in ups]
    return {"uploads": views, "summary": _hs_upload_summary(views)}


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
    """What the client declares up front — an EXPLICIT list, so a field the client
    invents is dropped by the model rather than reaching anything."""

    filename: str
    size: int = _Field(gt=0)
    sha256: str
    content_type: Optional[str] = None
    #: The data request this answers, when it answers one. Optional and defaulted
    #: so an old client that does not know about requests declares exactly as
    #: before.
    request_id: str = _Field(default="", max_length=64)


def _upload_session_or_404(store: Any, session_id: str,
                           portal_user: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a session, scoped to the caller's ACCOUNT.

    Per account, not per organization. An organization may hold more than one
    portal account, and what an upload is recorded as is a property of the account
    that sent it — so an account adopting a session another account opened would
    silently change what that upload counts as. Same-organization is not close
    enough when the accounts are the thing being distinguished.

    A session belonging to anyone else is reported as a plain 404 — the same answer
    as one that never existed, so the endpoint cannot be used to probe which
    session ids are real."""
    session = store.get_upload_session(session_id)
    if (not session or session.get("owner_kind") != _UPLOAD_OWNER_KIND
            or session.get("owner_id") != portal_user["hs_id"]
            or (session.get("actor") or "") != portal_user["username"]):
        raise HTTPException(status_code=404, detail="No such upload.")
    return session


def _hs_upload_preconditions(store: Any, portal_user: Dict[str, Any]) -> None:
    """The gates BOTH upload doors apply.

    Same checks, same order, same messages — a partner who is blocked must be
    blocked the same way whichever door they used, or the two doors become a way
    to tell them apart. That was the intent from the start, but the multipart
    door duplicated these inline instead of calling this, so the promise held
    only as long as nobody added a gate. Adding the approval gate is exactly
    that event, so the multipart door now calls this too and the duplicate is
    gone.

    The approval check is ALSO a route dependency on both doors
    (``require_hs_surface(UPLOAD)``). Belt and braces, deliberately: the dependency
    is the structural version, so a third upload door added later has to name a
    surface before it compiles, and this is the version that survives someone
    refactoring the dependency away."""
    if not hs_access.can_surface(portal_user, hs_access.UPLOAD):
        raise HTTPException(status_code=403, detail=_HS_REVIEW_MSG)
    # The organization-level gate (PRD §6). SERVER-SIDE and here rather than in
    # the route dependency, so it is applied by all four doors from one
    # statement: a fifth door that forgets the dependency still cannot get past
    # this, because there is no upload path that does not call this function.
    #
    # The health system row comes off the SESSION, refetched by
    # require_hs_portal on every request, so a state change takes effect on the
    # partner's next call rather than at the end of a 12-hour cookie.
    if not hs_states.can_upload(portal_user.get("health_system")):
        raise HTTPException(status_code=403, detail=_hs_locked_message(
            portal_user.get("health_system")))
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


def _resolve_upload_request(store: Any, raw: Optional[str]) -> Optional[str]:
    """Validate the OPTIONAL data request an upload answers.

    Absence changes nothing and always will: most uploads predate or ignore
    every request, and a partner who just sends us data must not meet a new
    precondition because a broadcast feature shipped.

    An id that is present and wrong is a 400 rather than a silent drop. A
    partner who answered a request and had the tag quietly discarded would
    believe they had responded to something we would have no record of them
    responding to, and the whole value of the tag is that record.
    """
    rid = (raw or "").strip()
    if not rid:
        return None
    row = store.get_hs_data_request(rid)
    if not row:
        raise HTTPException(status_code=400,
                            detail="We do not recognise that data request.")
    if (row.get("status") or "") != "open":
        raise HTTPException(
            status_code=400,
            detail="That data request is closed. You can still send this data "
                   "without it and we will take a look.")
    return rid


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
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
):
    """Declare a bundle and receive its chunk plan.

    Idempotent on the declared bytes: re-declaring the same ``{sha256, size}``
    returns the EXISTING session, so a refresh at 3.2 GB resumes instead of
    restarting. The session id IS the idempotency key."""
    from asclepius import uploads as asc_uploads

    store = _store()
    hs_id = portal_user["hs_id"]
    _hs_upload_preconditions(store, portal_user)
    answers_request = _resolve_upload_request(store, body.request_id)

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
            # The authorizing ACCOUNT, not anything derived from it. What the
            # admin side reads off that account is resolved by the store, server-
            # side; this door is given no way to name it and therefore no way to
            # leak it (PRD-I §3.1).
            portal_username=portal_user["username"])
    except asc_uploads.UploadSessionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    # Parked on the SESSION rather than carried to complete by the client. The
    # upload row is created minutes later, and a value re-declared at that point
    # would be a second chance to name it; this way the tag is fixed at the
    # moment the partner said what they were answering.
    if answers_request:
        store.set_upload_session_request(session["session_id"], answers_request)
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
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
):
    """Which parts are already stored — the resume endpoint. An interrupted 4 GB
    upload continues from here rather than starting over.

    Gated on the UPLOAD surface with the rest of the chunked handshake: an
    account that may not declare or send a part has no business reading the
    progress of one either."""
    from asclepius import uploads as asc_uploads

    store = _store()
    session = _upload_session_or_404(store, session_id, portal_user)
    return asc_uploads.public_session(session)


@portal_router.put("/hs/uploads/sessions/{session_id}/parts/{part}",
                   # Every other upload entry point is limited; this one carries
                   # the bytes, so leaving it open let one authenticated partner
                   # hold unbounded concurrent parts in memory. Generous enough
                   # that a legitimate sequential upload never touches it.
                   dependencies=[Depends(rate_limiter("hs_upload_part", 240, 60))])
async def hs_upload_part(
    session_id: str, part: int, request: Request,
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
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
    # Read the body as a STREAM with a running cap, not via request.body().
    # request.body() buffers the entire body before returning, so the obvious
    # `if len(body) > limit` check happens only after the memory has already been
    # spent — an authenticated partner could push a 10 GB "part" and take the
    # container down before the check it would eventually have failed. Aborting
    # mid-stream costs us the bytes in flight instead.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413,
                            detail=f"A part may not exceed {limit} bytes.")
    buf = bytearray()
    async for piece in request.stream():
        buf.extend(piece)
        if len(buf) > limit:
            raise HTTPException(status_code=413,
                                detail=f"A part may not exceed {limit} bytes.")
    body = bytes(buf)
    try:
        # Off the event loop: hashing, encrypting and fsync-ing 16 MB is tens of
        # milliseconds of CPU and a synchronous disk write, and doing it inline
        # stalls every other request this worker is serving — including the
        # platform health check.
        stored = await run_in_threadpool(
            asc_uploads.store_part, session, part, body,
            request.headers.get("x-chunk-sha256"))
    except asc_uploads.UploadSessionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    state = await run_in_threadpool(asc_uploads.session_state, session)
    return {"part": stored["part"], "size": stored["size"],
            "received_parts": state["received_parts"],
            "missing_parts": state["missing_parts"],
            "bytes_received": state["bytes_received"]}


@portal_router.post("/hs/uploads/sessions/{session_id}/complete")
async def hs_upload_complete(
    session_id: str, request: Request, background: BackgroundTasks,
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
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
        # THE expensive call: decrypt every part, hash the whole file, re-encrypt
        # frame by frame, fsync. Minutes of AES-GCM and disk I/O on a multi-GB
        # bundle. Inline on the event loop it blocks every other request in this
        # worker for the duration — including the platform health check, which
        # would restart the container mid-assembly.
        result = await run_in_threadpool(asc_uploads.complete, store, session)
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
    # Copied off the session the partner declared, not off this request: what an
    # upload answers was decided at declare and cannot be renamed at complete.
    if session.get("request_id"):
        store.set_upload_request(upload["upload_id"], session["request_id"])
    # (sha256, byte_size, verified_at) — the chain-of-custody triple. Stamped only
    # after the digest was recomputed over the assembled bytes and matched.
    store.mark_upload_verified(upload["upload_id"], verified_at=result["verified_at"])
    # Provenance carried forward from the session the server itself stamped at
    # declare — a server-side join, not a value that passed through this door.
    store.attach_upload_provenance(upload["upload_id"], session_id=session_id)
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
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
):
    """Give up on an unfinished upload and release its parts immediately, rather
    than waiting for the reaper.

    Gated on the UPLOAD surface with the rest of the chunked handshake. It is a
    write against an upload, and the account that may not make one may not
    destroy one."""
    from asclepius import uploads as asc_uploads

    store = _store()
    session = _upload_session_or_404(store, session_id, portal_user)
    if session.get("status") == "verified":
        raise HTTPException(status_code=409, detail="This upload has already completed.")
    asc_uploads.abort(store, session)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
#  SELF-SIGNUP
#
#  The second door. PRD-C's door is an operator typing an organization and an
#  email, which is right for a partner we already met and was the only one
#  there was, so a hospital that found us on its own had nowhere to go.
#
#  Nothing durable is created until a code sent to the address comes back. A
#  verified signup lands as ``approval_status='pending'``: signed in, able to
#  tell us about itself and read its ledger, unable to upload until a person
#  has looked at it.
# ════════════════════════════════════════════════════════════════════════════

_HS_SIGNUP_EMAIL_CAP = 3          # per address per 24h
_HS_SIGNUP_CODE_TTL_MIN = 15
_HS_SIGNUP_MAX_ATTEMPTS = 5
#: One body for every outcome of POST /hs/signup. A form that answers
#: differently for a known address is the cheapest account-enumeration oracle
#: there is, and a signup form is the one place people paste addresses to see
#: what happens.
_HS_SIGNUP_OK = {"ok": True, "next": "verify"}


class HsSignupRequest(BaseModel):
    full_name: str
    email: str
    organization: str
    #: OPTIONAL, and the default is the one the landing dialog uses. Three
    #: fields and a Continue button is the whole of screen one (PRD §2); an
    #: organization that gives us no password is mailed a temporary one and made
    #: to replace it at first sign-in, which is §0.1.1 and the same compromise
    #: the physician onboarding makes for the same SOC 2 reason.
    #:
    #: The portal's own signup screen still sends one, so both doors stay open
    #: and neither is a fork: identical guards, identical staging, identical
    #: account, and one flag deciding which credential the welcome email
    #: carries.
    password: str = ""
    # Same field name as the landing forms', so bots already filling it in keep
    # filling it in.
    company_website: str = ""


class HsSignupVerifyRequest(BaseModel):
    email: str
    code: str


class HsSignupResendRequest(BaseModel):
    email: str


def _hs_portal_url() -> str:
    """Where the portal lives, as something a person can click in an email.

    ALWAYS ABSOLUTE. This used to fall back to the bare path "/provider", which
    is fine in a page and useless in a mail client: the welcome letter's whole
    job is to be the thing they can come back to, and "your portal lives at
    /provider" is a sentence that leads nowhere. The localhost fallback mirrors
    the admin router's ``_portal_url`` -- wrong in production only if BASE_URL
    is unset, which is a deployment fault that a broken link makes visible
    rather than hides.
    """
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("ASCLEPIUS_PORTAL_URL")
            or os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")
    return f"{base}/provider"


@portal_router.post("/hs/signup",
                    dependencies=[Depends(rate_limiter("hs_signup", 5, 600)),
                                  Depends(global_rate_limiter("hs_signup_all", 60, 3600))])
async def hs_signup(body: HsSignupRequest, request: Request):
    """Stage a signup and mail a code. Creates no account.

    The guard stack is the one ``/api/onboarding/self-serve`` already runs, in
    the same order: IP limit, global cap, honeypot, per-address cap, then proof
    of the mailbox. It diverges in one place, deliberately. Onboarding answers
    an over-cap address with a 429 whose text confirms we have seen it before;
    here every outcome returns the same body, because this file's standing rule
    is one code path with one observable, and ``hs_login`` two hundred lines up
    goes to considerable trouble for exactly that property. Losing it on the
    signup route would be an odd place to stop caring.

    The send is awaited, which the authenticated routes below must not do. It is
    correct here: no session exists yet, so response time correlates with
    nothing about an existing partner, and the person genuinely needs to be told
    if the code could not be sent.
    """
    store = _store()
    email = (body.email or "").strip().lower()
    full_name = (body.full_name or "").strip()
    organization = " ".join((body.organization or "").split())

    # Honeypot. Write nothing, send nothing, and return the same shape; the
    # 4 KB pad makes the decoy and the real answer the same size for free.
    if (body.company_website or "").strip():
        return _HS_SIGNUP_OK

    if not email or "@" not in email or not full_name or not organization:
        raise HTTPException(status_code=400,
                            detail="Please fill in your name, work email and organization.")
    chosen_password = body.password or ""
    wants_temp = not chosen_password.strip()
    if not wants_temp:
        try:
            asc_passwords.validate(chosen_password, email=email)
        except asc_passwords.PasswordRejected as exc:
            # The one place a real 400 is right: it is about what THEY typed, so
            # it tells an attacker nothing they did not already supply.
            raise HTTPException(status_code=400, detail=str(exc))

    if store.count_recent_hs_signups_for_email(email, hours=24) >= _HS_SIGNUP_EMAIL_CAP:
        log.info("hs signup: per-email cap reached, dropping silently")
        return _HS_SIGNUP_OK

    code = f"{secrets.randbelow(1000000):06d}"
    try:
        staged = store.create_hs_signup(
            email=email, full_name=full_name, organization=organization,
            # An unusable random string when they chose nothing. The row must
            # never hold a hash anybody could produce a preimage for, because a
            # staged row that is later verified becomes an account.
            password=chosen_password or secrets.token_urlsafe(32),
            code=code, ttl_minutes=_HS_SIGNUP_CODE_TTL_MIN,
            client_ip=client_ip(request), needs_temp_password=wants_temp)
    except Exception:
        log.exception("hs signup: could not stage")
        raise HTTPException(status_code=503, detail="We could not start that just now. Please try again.")

    if not is_email_transport_configured():
        if _is_production():
            store.burn_hs_signup(staged["signup_id"])
            raise HTTPException(status_code=503,
                                detail="We could not send your code just now. Please try again.")
        # Local development has no transport; without this the whole flow is
        # untestable. Mirrors the onboarding OTP's dev bypass.
        log.warning("hs signup: no email transport, code for %s is %s", email, code)
        return _HS_SIGNUP_OK

    from onboarding_emails import build_hs_signup_code_email
    ok = await send_html_email(
        email, "Your Archangel Health confirmation code",
        build_hs_signup_code_email(code=code, organization=organization,
                                   expires_minutes=_HS_SIGNUP_CODE_TTL_MIN))
    if not ok:
        store.burn_hs_signup(staged["signup_id"])
        raise HTTPException(status_code=503,
                            detail="We could not send your code just now. Please try again.")
    return _HS_SIGNUP_OK


@portal_router.post("/hs/signup/resend",
                    dependencies=[Depends(rate_limiter("hs_signup_resend", 3, 600))])
async def hs_signup_resend(body: HsSignupResendRequest):
    """Mail the code again. Always the same body, whether or not anything was
    staged for that address."""
    store = _store()
    email = (body.email or "").strip().lower()
    staged = store.get_live_hs_signup(email) if email else None
    if not staged:
        return _HS_SIGNUP_OK
    # The stored code is hashed, so it cannot be re-sent; issue a new challenge
    # for the same details instead.
    code = f"{secrets.randbelow(1000000):06d}"
    store.create_hs_signup(
        email=email, full_name=staged["full_name"], organization=staged["organization"],
        password=secrets.token_urlsafe(32), code=code,
        ttl_minutes=_HS_SIGNUP_CODE_TTL_MIN, client_ip=staged.get("client_ip"),
        # Carried across, or a resend would silently turn a
        # three-field signup into one that verifies with a password nobody has.
        needs_temp_password=bool(staged.get("needs_temp_password")))
    # ...but the password on the new row is garbage, because we never held the
    # real one in the clear. Carry the ORIGINAL hash across so verifying the new
    # code still creates the account with the password they actually chose.
    fresh = store.get_live_hs_signup(email)
    if fresh:
        store.set_hs_signup_password_hash(fresh["signup_id"], staged["password_hash"])
    if not is_email_transport_configured():
        log.warning("hs signup resend: no transport, code for %s is %s", email, code)
        return _HS_SIGNUP_OK
    from onboarding_emails import build_hs_signup_code_email
    await send_html_email(
        email, "Your Archangel Health confirmation code",
        build_hs_signup_code_email(code=code, organization=staged["organization"],
                                   expires_minutes=_HS_SIGNUP_CODE_TTL_MIN))
    return _HS_SIGNUP_OK


@portal_router.post("/hs/signup/verify",
                    dependencies=[Depends(rate_limiter("hs_signup_verify", 10, 600))])
async def hs_signup_verify(body: HsSignupVerifyRequest, background: BackgroundTasks,
                           response: Response):
    """Trade a correct code for an account and a session.

    Signs them straight in rather than bouncing to the login form: the username
    was derived from their organization name and they have never seen it, so a
    redirect to "enter your username" would strand every single signup.
    """
    store = _store()
    email = (body.email or "").strip().lower()
    code = (body.code or "").strip()
    generic = HTTPException(status_code=400, detail="That code is not right, or it has expired.")

    staged = store.get_live_hs_signup(email) if email else None
    if not staged:
        raise generic
    if not verify_password(code, staged["code_hash"]):
        attempts = store.bump_hs_signup_attempts(staged["signup_id"])
        if attempts >= _HS_SIGNUP_MAX_ATTEMPTS:
            # Burn it rather than leaving a six-digit secret to be ground down.
            store.burn_hs_signup(staged["signup_id"])
        raise generic

    organization = staged["organization"]
    # NEVER ensure_health_system here. See create_health_system_unclaimed: that
    # method is create-or-reuse by name, and on a public route it would hand a
    # stranger an incumbent partner's upload history.
    hs = store.create_health_system_unclaimed(organization, contact_email=email)
    wants_temp = bool(staged.get("needs_temp_password"))
    # ONE minting path for both doors, shared with the operator's own
    # (asclepius/hs_provisioning.py). must_reset follows what they gave us:
    # a credential that travelled through email has to be replaced before it
    # guards anything, and a password they chose ninety seconds ago has nothing
    # to replace -- landing them on the forced-reset screen for it would be
    # asking them to change something they just typed.
    minted = asc_hs_provisioning.provision_account(
        store, hs_id=hs["hs_id"], org_name=organization, email=email,
        full_name=staged["full_name"], signup_source="self_serve",
        approval_status="pending", must_reset=wants_temp)
    username = minted["username"]
    if not wants_temp:
        # Carry the password they actually chose, which we only ever held
        # hashed, over the one provision_account generated.
        store.set_hs_portal_password_hash(username, staged["password_hash"])
    # The organization starts at the beginning of the state machine, not at the
    # end of it. This is the one write that makes the upload door closed by
    # default for everything that arrives through this route; a health system
    # provisioned by an operator keeps its NULL and keeps its door.
    store.set_hs_onboarding_state(hs["hs_id"], hs_states.INTAKE)
    store.consume_hs_signup(staged["signup_id"])
    store.log_event(entity_type="health_system", entity_id=hs["hs_id"],
                    event_type="self_signup_verified", actor=username,
                    payload={"organization": organization})

    collisions = [h["hs_id"] for h in
                  store.health_systems_named_like(organization, exclude_hs_id=hs["hs_id"])]
    background.add_task(_notify_hs_signup, store, staged["full_name"], email,
                        organization, hs["hs_id"], username, collisions,
                        minted["passphrase"] if wants_temp else "")

    fresh = store.get_hs_portal_user(username) or {}
    _set_hs_cookie(response, _hs_token(username, hs["hs_id"],
                                       session_epoch=fresh.get("session_epoch")))
    # Same shape hs_login returns, plus the username they now have to remember.
    return {"ok": True, "username": username, "organization": organization,
            "must_reset": wants_temp}


def _notify_hs_signup(store: Any, full_name: str, email: str, organization: str,
                      hs_id: str, username: str, collisions: List[str],
                      temp_password: str = "") -> None:
    """Background because this route is behind the portal time budget on the way
    out, and a SendGrid round trip is several times it.

    Two welcome letters, one per door. A signup that chose its own password gets
    the letter that delivers the USERNAME, because that is the only thing they
    do not already have. A three-field signup gets the §2.3 access letter, which
    carries the mission, the temporary credential, and the line telling them to
    bookmark it -- and it has to go out immediately, because for that door this
    email is the only record of how to get back in.

    ``temp_password`` is a live credential. It exists in this process, in this
    email, and nowhere else: never log it, and never put it in an event payload.
    """
    try:
        import notifications
        from onboarding_emails import (
            build_hs_access_email, build_hs_signup_alert,
            build_hs_signup_welcome_email,
        )
        notifications.notify_founders(
            store, kind="hs_signup",
            subject=f"[Health system] New signup: {organization}",
            body_html=build_hs_signup_alert(
                full_name=full_name, email=email, organization=organization,
                hs_id=hs_id, username=username, name_collisions=collisions),
            dedupe_key=hs_id, coalesce=False)
        if is_email_transport_configured():
            if temp_password:
                subject = "Welcome to Archangel Health: your portal access"
                body = build_hs_access_email(
                    organization=organization, full_name=full_name,
                    username=username, temp_password=temp_password,
                    portal_url=_hs_portal_url())
            else:
                subject = "Your Archangel Health upload portal"
                body = build_hs_signup_welcome_email(
                    organization=organization, username=username,
                    portal_url=_hs_portal_url())
            # The house bridge, which copes whether or not a loop is running.
            # A sync BackgroundTask has none, but that is a property of how
            # FastAPI happens to schedule this today, not one to depend on.
            from asclepius.ingest_notify import _run_coro
            _run_coro(send_html_email(email, subject, body))
    except Exception:
        log.exception("hs signup: notification failed")


# ════════════════════════════════════════════════════════════════════════════
#  INTAKE — what a health system tells us about itself
# ════════════════════════════════════════════════════════════════════════════

#: Server-owned so the copy is auditable in one place, and so the static grep
#: that scans this file covers every word a partner is shown. Check anything
#: added here against that word list before shipping it.
_HS_INTAKE_PROMPTS = [
    {"key": "organization",
     "label": "Which health system are you with, and what is your role there?",
     "placeholder": "St Mary's Health, and I run the data platform team.",
     "required": True},
    {"key": "size_type",
     "label": "Roughly how big is it?",
     "placeholder": "Beds, sites, or annual encounters. A guess is fine.",
     "required": False},
    {"key": "data_held",
     "label": "What clinical data do you hold, and in what systems?",
     "placeholder": "Epic, about 12 years of nephrology and cardiology encounters "
                    "with labs, notes and outcomes.",
     "required": True},
    {"key": "licensable",
     "label": "What would you be open to licensing to us?",
     "placeholder": "Whatever you already have a sense of. If the answer is "
                    "'not sure yet', that is a useful answer.",
     "required": False},
    {"key": "timeline",
     "label": "What timeline are you working on?",
     "placeholder": "Exploring, this quarter, budgeted for next year.",
     "required": False},
]

_HS_INTAKE_MAX_CHARS = 4000


class HsIntakeRequest(BaseModel):
    """An EXPLICIT field list, per the rule the upload declare model states: a
    field the client invents is dropped by the model rather than reaching
    anything downstream."""
    organization: str
    size_type: Optional[str] = None
    data_held: str
    licensable: Optional[str] = None
    timeline: Optional[str] = None


def _hs_intake_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """Named fields only, never a splat of the stored row."""
    answers = row.get("answers") or {}
    return {
        "submitted_at": row.get("submitted_at"),
        "answers": {p["key"]: (answers.get(p["key"]) or "") for p in _HS_INTAKE_PROMPTS},
    }


@portal_router.get("/hs/intake")
async def hs_intake_get(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    store = _store()
    history = store.list_hs_intake(portal_user["hs_id"])
    return {
        "prompts": _HS_INTAKE_PROMPTS,
        "submitted": [_hs_intake_view(r) for r in history[:5]],
        "organization": portal_user["health_system"]["name"],
    }


@portal_router.post("/hs/intake",
                    dependencies=[Depends(rate_limiter("hs_intake", 5, 600))])
async def hs_intake_post(
    body: HsIntakeRequest,
    background: BackgroundTasks,
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    store = _store()
    answers: Dict[str, Any] = {}
    for prompt in _HS_INTAKE_PROMPTS:
        raw = getattr(body, prompt["key"], None) or ""
        value = str(raw).strip()[:_HS_INTAKE_MAX_CHARS]
        if prompt["required"] and not value:
            raise HTTPException(status_code=400,
                                detail="Please answer the two required questions.")
        answers[prompt["key"]] = value
    row = store.record_hs_intake(hs_id=portal_user["hs_id"],
                                 username=portal_user["username"], answers=answers)
    store.log_event(entity_type="health_system", entity_id=portal_user["hs_id"],
                    event_type="intake_submitted", actor=portal_user["username"])
    # Background, not awaited: this route sits behind the portal time budget and
    # a mail round trip is several times it, so awaiting would make response
    # time a function of whether email is configured.
    background.add_task(_notify_hs_intake, store, portal_user, answers)
    return {"ok": True, "submitted_at": row["submitted_at"]}


def _notify_hs_intake(store: Any, portal_user: Dict[str, Any],
                      answers: Dict[str, Any]) -> None:
    try:
        import notifications
        from onboarding_emails import build_hs_intake_alert
        org = portal_user["health_system"]["name"]
        notifications.notify_founders(
            store, kind="hs_intake", subject=f"[Health system] Intake: {org}",
            body_html=build_hs_intake_alert(
                full_name=portal_user.get("full_name") or "",
                email=portal_user.get("email") or "", organization=org,
                answers=answers, hs_id=portal_user["hs_id"]),
            dedupe_key=f"{portal_user['hs_id']}|{answers.get('data_held', '')[:40]}",
            coalesce=False)
    except Exception:
        log.exception("hs intake: notification failed")


# ════════════════════════════════════════════════════════════════════════════
#  PAYOUTS — what we have paid this organization
#
#  Admin-entry only. Nothing accrues from a health system's uploads to money:
#  there is no schedule, no rail, and no Stripe. The empty state says so rather
#  than implying a ledger that fills itself.
# ════════════════════════════════════════════════════════════════════════════

#: Internal status -> the word a partner reads. The same discipline the upload
#: status map applies: 'accrued' is our bookkeeping and means nothing to a CFO.
_HS_PAYOUT_STATUS = {
    "accrued": "recorded",
    "approved": "recorded",
    "paid": "paid",
    "void": "cancelled",
}

#: The empty state, verbatim from §7. It says two true things and no more: that
#: this is where money appears, and that the amounts come from the agreement
#: they signed rather than from anything on this page. A ledger that fills
#: itself is exactly what this must not imply, because nothing accrues
#: automatically and pretending otherwise is a conversation about a number that
#: does not exist.
_HS_PAYOUT_EMPTY = (
    "Compensation for licensed data appears here. Invoicing goes live shortly; "
    "your agreement's Schedule A governs amounts."
)

#: Internal invoice status -> the word a partner reads. A draft is OURS and they
#: should not see it at all, which is why it is filtered rather than renamed.
_HS_INVOICE_STATUS = {"sent": "issued", "paid": "paid"}

_HS_PAYOUT_NOTE = (
    "Every payment we make to your organization appears here, with what it was "
    "for and when it was sent. Payments are arranged with your contract "
    "contact. We do not hold your bank details or tax identifiers, and we are "
    "not going to start."
)

#: The accrual line's own caveat, and the whole reason the line is allowed to
#: exist. Pricing is an operator decision made off this page, so the count says
#: what we have TAKEN and refuses to imply what it is worth. A number of dollars
#: here would be a figure nobody has agreed to, printed on the page a hospital's
#: finance contact reads.
_HS_ACCRUAL_NOTE = (
    "Accepted means the data reached us intact and passed our checks. Our team "
    "prices accepted data before it becomes a payout line, so this is a count "
    "of what we have taken, not an amount owed."
)


def _hs_payout_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """Named fields only. No recorded_by, no external_ref, no batch id: those
    are ours, and a partner reading an internal invoice key learns nothing."""
    return {
        "payout_id": row.get("payout_id"),
        "recorded_at": row.get("recorded_at"),
        "description": row.get("description") or "",
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "amount_cents": int(row.get("amount_cents") or 0),
        "status": _HS_PAYOUT_STATUS.get(row.get("status") or "", "recorded"),
        "paid_at": row.get("paid_at"),
    }


def _hs_invoice_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """Named fields only. No created_by, no stripe id, no internal identifier:
    those are ours, and a partner reading our bookkeeping keys learns nothing."""
    return {
        "period": row.get("period") or "",
        "description": row.get("description") or "",
        "amount_cents": int(row.get("amount_cents") or 0),
        "status": _HS_INVOICE_STATUS.get(row.get("status") or "", "issued"),
        "issued_at": row.get("sent_at"),
        "paid_at": row.get("paid_at"),
    }


@portal_router.get("/hs/payouts")
async def hs_payouts(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.PAYOUTS)),
):
    """The signed-in organization's ledger, and nobody else's.

    This route takes NO identifier, in the path, the query or a body. The
    ``hs_id`` comes from the session and from nowhere else, which is the same
    property the physician earnings route holds: it is a fact about the route
    rather than about a check somebody remembered to write. There is
    deliberately no ``/hs/payouts/{hs_id}``.
    """
    store = _store()
    hs_id = portal_user["hs_id"]
    # Reconciled on read, scoped to THIS organization, exactly as the physician
    # Earnings page reconciles its own ledger. Idempotent by construction (the
    # ledger's UNIQUE constraint, not a check this route performs), scoped so a
    # partner opening their page costs their own backlog and nobody else's, and
    # a no-op entirely while the organization is unpriced. A partner who reads
    # this page ten seconds after an upload is accepted should see it.
    try:
        hs_billing.reconcile_accruals(store, hs_id=hs_id)
    except Exception:
        # A ledger write must never cost a partner the ability to READ their
        # ledger. The next open reconciles what this one missed.
        log.exception("hs payouts: accrual reconciliation failed for %s", hs_id)
    rows = store.list_hs_payouts(hs_id)
    # DRAFTS ARE FILTERED OUT, not renamed. A draft is a number an operator is
    # still deciding about, and showing a hospital's finance contact an amount
    # we have not committed to is a conversation nobody wants to have twice.
    invoices = [_hs_invoice_view(r) for r in store.list_hs_invoices(hs_id)
                if (r.get("status") or "") in _HS_INVOICE_STATUS]
    money = store.hs_payout_summary(hs_id)
    # Accrual visibility, and the gap it closes: a partner whose data we accepted
    # weeks ago sees an empty ledger and reasonably concludes it was lost.
    # Counted off the SAME view the uploads page maps through, so the two pages
    # cannot tell them different numbers.
    upload_summary = _hs_upload_summary(
        [_hs_upload_view(u) for u in store.list_uploads_for_health_system(hs_id)])
    rail = hs_billing.partner_rail(store, hs_id)
    # An accepted upload with a ledger row of EITHER kind is no longer awaiting
    # anything. Before the accrual rail existed only a hand-entered payout could
    # close this gap; counting accruals here is what stops a fully accrued
    # partner from being told their data is still waiting to be priced.
    priced_entries = money["count"] + rail["count"]
    return {
        "currency": "usd",
        "summary": money,
        # What is owed, what is billed, what has cleared. Beside the payout
        # summary rather than merged into it: that block is money we have
        # RECORDED PAYING, this one is money the arithmetic says is DUE, and a
        # page that adds them together would double-count a settled quarter.
        "rail": rail,
        # Deliberately beside the money summary rather than inside it: nothing in
        # here is currency, and a count that lands in a block the page formats as
        # dollars is how "3" becomes "$0.03".
        "accrual": {
            "accepted_uploads": upload_summary["accepted"],
            "ledger_entries": priced_entries,
            # What the line actually renders. Computed server-side so the page
            # has no arithmetic of its own to get wrong, and floored at zero
            # because an operator may price one upload into several ledger rows.
            "awaiting_pricing": max(0, upload_summary["accepted"] - priced_entries),
            "note": _HS_ACCRUAL_NOTE,
        },
        "payouts": [_hs_payout_view(r) for r in rows],
        "invoices": invoices,
        "how_we_pay": _HS_PAYOUT_NOTE,
        "empty_note": _HS_PAYOUT_EMPTY,
    }


# ─── Open data requests ──────────────────────────────────────────────────────
#: The one thing every request has to say, and the reason it says it: a partner
#: who reads a request as exclusive treats a reply as a claim, and the second
#: partner to see it does not bother. Neither is true. Several are asked, we
#: confirm what we take.
_HS_REQUEST_NOTE = (
    "We ask several partner organizations for the same data. More than one may "
    "send cases, nothing is reserved, and our team confirms what we accept "
    "after it arrives. If you have nothing that fits, no reply is needed."
)

_HS_REQUEST_EMPTY = (
    "Nothing open right now. When we need a specific kind of case we will email "
    "everyone on your team and it will appear here."
)


def _hs_request_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """One open request, in partner words. An explicit field list, like every
    other provider serializer in this file: a column added to the table later
    must not ship to a hospital because somebody splatted a row."""
    return {
        "request_id": row["id"],
        "title": row["title"],
        "specialty": row["specialty"],
        "case_count": row["case_count"],
        "due_date": row.get("due_date") or "",
        "details": row.get("details") or "",
        "asked_at": row.get("created_at"),
    }


@portal_router.get("/hs/requests")
async def hs_requests(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.UPLOAD)),
):
    """The data requests this organization can answer right now.

    NO identifier, in the path, the query or a body. The organization comes off
    the session, which is the property ``/hs/payouts`` holds and for the same
    reason. There is deliberately no ``/hs/requests/{hs_id}``.

    Gated on the ORGANIZATION's state as well as the account's surface, because
    a request is an ask to upload and an organization that has not signed the
    agreement may not. A partner in intake, in review, or holding an unsigned
    agreement is told what it is told at every other upload door rather than
    handed a list of things it cannot act on.

    Closed requests are gone from here the moment they close. A request we have
    stopped asking for still sitting on the portal is how a partner spends a
    week assembling cases nobody is waiting for.
    """
    store = _store()
    if not hs_states.can_upload(portal_user.get("health_system")):
        raise HTTPException(status_code=403, detail=_hs_locked_message(
            portal_user.get("health_system")))
    rows = store.list_hs_data_requests(status="open")
    return {
        "requests": [_hs_request_view(r) for r in rows],
        "how_it_works": _HS_REQUEST_NOTE,
        "empty_note": _HS_REQUEST_EMPTY,
    }



# ════════════════════════════════════════════════════════════════════════════
#  ONBOARDING — the application, the team, and the agreement
#
#  Three surfaces that together take an organization from "we just signed up"
#  to "we can upload", with no phone call in the middle. They are ordered by the
#  state machine in asclepius/hs_states.py rather than by a wizard step counter,
#  because a member who joins halfway through has to land somewhere sensible and
#  a step counter has no answer for that.
#
#  Every one of them renders from a SERVER-OWNED question list, for the reason
#  the intake prompts give: the copy a partner reads is auditable in one place,
#  and the static check that scans this file covers every word of it.
# ════════════════════════════════════════════════════════════════════════════

#: The four questions, in the order §3 asks for them. Each carries its own
#: options, and every one of them has an honest "not sure" -- an organization
#: that does not know whether it may license data is telling us something true
#: and useful, and a form that refuses to accept it just teaches people to
#: guess. Nothing here blocks submission.
_HS_APPLICATION_QUESTIONS: List[Dict[str, Any]] = [
    {
        "key": "authority",
        "label": "Does your organization have the authority to license "
                 "de-identified clinical data to a commercial party?",
        "help": "If you are not sure, say so. It is a common answer and it "
                "routes to a conversation rather than to a dead end.",
        "options": [
            {"value": "yes", "label": "Yes"},
            {"value": "no", "label": "No"},
            {"value": "not_sure", "label": "Not sure"},
        ],
    },
    {
        "key": "deid_capability",
        "label": "Can you de-identify and date-shift records before the data "
                 "leaves your environment, or would we need to receive "
                 "identified data under a BAA?",
        "help": "De-identifying on your side is simpler for both of us. If it "
                "is not possible today, a BAA is the path and we will say so.",
        "options": [
            {"value": "in_our_environment",
             "label": "De-identified in our environment"},
            {"value": "needs_baa", "label": "We would need a BAA"},
            {"value": "not_sure", "label": "Not sure"},
        ],
    },
    {
        "key": "export_scope",
        "label": "Do your exports include free-text notes, or only structured "
                 "fields?",
        "help": "Notes are where clinical reasoning lives, so this changes what "
                "we can do with an extract more than anything else here.",
        "options": [
            {"value": "notes_and_structured", "label": "Notes and structured"},
            {"value": "structured_only", "label": "Structured only"},
            {"value": "varies", "label": "Depends by system"},
        ],
    },
    {
        "key": "scale",
        "label": "Roughly how many patients, over how many years, in which "
                 "specialties?",
        "help": "Estimates are fine. Nobody is held to these numbers.",
        # The one composite question, because "scale" is one thought and three
        # separate screens for it reads like an interrogation.
        "fields": [
            {"key": "scale_patients", "label": "Patients", "kind": "select",
             "options": [
                 {"value": "under_10k", "label": "Under 10,000"},
                 {"value": "10k_50k", "label": "10,000 to 50,000"},
                 {"value": "50k_250k", "label": "50,000 to 250,000"},
                 {"value": "250k_1m", "label": "250,000 to 1 million"},
                 {"value": "over_1m", "label": "Over 1 million"},
                 {"value": "not_sure", "label": "Not sure"},
             ]},
            {"key": "scale_years", "label": "Years of history", "kind": "select",
             "options": [
                 {"value": "under_2", "label": "Under 2 years"},
                 {"value": "2_5", "label": "2 to 5 years"},
                 {"value": "5_10", "label": "5 to 10 years"},
                 {"value": "10_20", "label": "10 to 20 years"},
                 {"value": "over_20", "label": "Over 20 years"},
                 {"value": "not_sure", "label": "Not sure"},
             ]},
            {"key": "scale_specialties", "label": "Specialties",
             "kind": "multiselect", "options": None},   # filled from _HS_SPECIALTIES
        ],
    },
]

#: What a hospital can pick from when telling us what it holds. Deliberately
#: WIDER than asclepius/specialties.py's registry: that list is what we can
#: currently build tasks for, and a health system describing its own data should
#: not be asked to guess our roadmap. "Other" carries the rest.
_HS_SPECIALTIES: List[str] = [
    "Cardiology", "Dermatology", "Emergency medicine", "Endocrinology",
    "Gastroenterology", "General surgery", "Hematology", "Hepatology",
    "Infectious disease", "Internal medicine", "Nephrology", "Neurology",
    "Obstetrics and gynecology", "Oncology", "Ophthalmology", "Orthopedics",
    "Otolaryngology", "Pathology", "Pediatrics", "Psychiatry", "Pulmonology",
    "Radiology", "Rheumatology", "Urology", "Other",
]

#: Answer value -> the words an operator reads on the admin card and in the
#: alert email. Kept beside the questions so a new option cannot be added
#: without deciding what it is called in the place a decision gets made.
def _build_answer_labels() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for question in _HS_APPLICATION_QUESTIONS:
        for option in (question.get("options") or []):
            out[f"{question['key']}:{option['value']}"] = option["label"]
        for field in (question.get("fields") or []):
            for option in (field.get("options") or []):
                out[f"{field['key']}:{option['value']}"] = option["label"]
    return out


_HS_ANSWER_LABELS: Dict[str, str] = _build_answer_labels()

#: Cap on how many teammates one organization may add through the portal. Not a
#: licence limit -- it is the blast radius of a compromised portal session, which
#: could otherwise mail our credentials to an arbitrary number of addresses.
_HS_MAX_MEMBERS = 25


def _hs_application_prompts() -> List[Dict[str, Any]]:
    """The question list as the client receives it, with the specialty options
    resolved. Built per call rather than mutated at import, so nothing can hand
    back a list a previous request edited."""
    out: List[Dict[str, Any]] = []
    for q in _HS_APPLICATION_QUESTIONS:
        item = {k: v for k, v in q.items() if k not in ("fields",)}
        if q.get("fields"):
            fields = []
            for f in q["fields"]:
                fld = dict(f)
                if fld.get("kind") == "multiselect" and fld.get("options") is None:
                    fld["options"] = [{"value": s, "label": s} for s in _HS_SPECIALTIES]
                fields.append(fld)
            item["fields"] = fields
        out.append(item)
    return out


def _hs_answer_label(key: str, value: str) -> str:
    return _HS_ANSWER_LABELS.get(f"{key}:{value}", value or "")


def _hs_application_view(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Named fields only, never a splat of the stored row."""
    if not row:
        return None
    return {
        "submitted_at": row.get("submitted_at"),
        "authority": row.get("authority") or "",
        "deid_capability": row.get("deid_capability") or "",
        "export_scope": row.get("export_scope") or "",
        "scale_patients": row.get("scale_patients") or "",
        "scale_years": row.get("scale_years") or "",
        "scale_specialties": list(row.get("scale_specialties") or []),
    }


def _hs_locked_message(hs: Optional[Dict[str, Any]]) -> str:
    """What a partner is told when the upload door is shut.

    Names what has to happen next rather than saying no. A 403 that reads
    "not allowed" produces a phone call; one that reads "sign the agreement and
    this opens" produces a signature.
    """
    view = hs_states.public_view(hs)
    return view["next_step"]


def _hs_agreement_summary(store: Any, hs: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Who signed for this organization, if anyone. Shown to EVERY member.

    §0.1.2: the agreement binds the organization on one authorized signature, so
    the fifth person to open the portal has to see "signed by Dana Reyes on 14
    March" rather than a second signature request. Returns None when nothing has
    been signed, which is also the honest answer for an organization
    provisioned before this existed.

    Deliberately omits the network address and the client string. Those are on
    the row because a court may want them; a colleague reading the portal has no
    business knowing which IP the CIO signed from.
    """
    if not hs:
        return None
    row = store.latest_signed_agreement(hs.get("hs_id") or "")
    if not row:
        return None
    return {
        "doc_version": row.get("doc_version"),
        "signed_at": row.get("signed_at"),
        "signed_by": row.get("typed_name") or "",
        "signed_by_title": row.get("typed_title") or "",
        "doc_sha256": row.get("doc_sha256") or "",
    }


# ─── The application ─────────────────────────────────────────────────────────
class HsApplicationRequest(BaseModel):
    """An EXPLICIT field list, per the rule the upload declare model states: a
    field the client invents is dropped by the model rather than reaching
    anything downstream."""

    authority: str
    deid_capability: str
    export_scope: str
    scale_patients: str
    scale_years: str
    scale_specialties: List[str] = []


@portal_router.get("/hs/application")
async def hs_application_get(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    """The questions, and what this organization last answered."""
    store = _store()
    hs = portal_user["health_system"]
    return {
        "prompts": _hs_application_prompts(),
        "organization": hs["name"],
        "submitted": _hs_application_view(store.latest_hs_application(hs["hs_id"])),
        **hs_states.public_view(hs),
    }


@portal_router.post("/hs/application",
                    dependencies=[Depends(rate_limiter("hs_application", 10, 600))])
async def hs_application_post(
    body: HsApplicationRequest,
    background: BackgroundTasks,
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    """Record the four answers and move the organization to `submitted`.

    Every value is checked against the server's own option list. Not because a
    bad value would be dangerous -- these are strings in a form -- but because
    an unvalidated one lands in the column an operator reads to decide whether a
    BAA is required, and a column that can hold anything is a column nobody can
    filter on.
    """
    store = _store()
    hs = portal_user["health_system"]
    answers: Dict[str, str] = {}
    for key in ("authority", "deid_capability", "export_scope",
                "scale_patients", "scale_years"):
        value = (getattr(body, key, "") or "").strip()
        if f"{key}:{value}" not in _HS_ANSWER_LABELS:
            raise HTTPException(
                status_code=400,
                detail="Please answer every question before submitting.")
        answers[key] = value
    allowed = set(_HS_SPECIALTIES)
    specialties = []
    for raw in (body.scale_specialties or [])[:len(_HS_SPECIALTIES)]:
        value = str(raw).strip()
        if value in allowed and value not in specialties:
            specialties.append(value)

    row = store.record_hs_application(
        hs_id=hs["hs_id"], username=portal_user["username"],
        authority=answers["authority"], deid_capability=answers["deid_capability"],
        export_scope=answers["export_scope"], scale_patients=answers["scale_patients"],
        scale_years=answers["scale_years"], scale_specialties=specialties)
    store.log_event(entity_type="health_system", entity_id=hs["hs_id"],
                    event_type="application_submitted", actor=portal_user["username"])
    # Background, not awaited: this route sits behind the portal time budget and
    # a mail round trip is several times it, so awaiting would make response
    # time a function of whether email is configured.
    background.add_task(_notify_hs_application, store, portal_user, row)
    fresh = store.get_health_system(hs["hs_id"])
    return {"ok": True, "submitted_at": row["submitted_at"],
            **hs_states.public_view(fresh)}


def _notify_hs_application(store: Any, portal_user: Dict[str, Any],
                           row: Dict[str, Any]) -> None:
    try:
        import notifications
        from onboarding_emails import build_hs_application_alert
        org = portal_user["health_system"]["name"]
        specialties = ", ".join(row.get("scale_specialties") or []) or "not specified"
        answers = [
            ("Authority to license",
             _hs_answer_label("authority", row.get("authority") or "")),
            ("De-identification",
             _hs_answer_label("deid_capability", row.get("deid_capability") or "")),
            ("Export contents",
             _hs_answer_label("export_scope", row.get("export_scope") or "")),
            ("Scale",
             f"{_hs_answer_label('scale_patients', row.get('scale_patients') or '')} "
             f"patients, {_hs_answer_label('scale_years', row.get('scale_years') or '')}"
             f", {specialties}"),
        ]
        members = [u.get("email") or u.get("username")
                   for u in store.list_hs_portal_users(portal_user["hs_id"])]
        notifications.notify_founders(
            store, kind="hs_application",
            subject=f"[Health system] Application: {org}",
            body_html=build_hs_application_alert(
                organization=org, hs_id=portal_user["hs_id"],
                full_name=portal_user.get("full_name") or "",
                email=portal_user.get("email") or "",
                answers=answers, members=members),
            dedupe_key=f"{portal_user['hs_id']}|{row.get('submitted_at')}",
            coalesce=False)
    except Exception:
        log.exception("hs application: notification failed")


# ─── The team ────────────────────────────────────────────────────────────────
class HsMemberRequest(BaseModel):
    emails: List[str] = []


def _hs_member_view(row: Dict[str, Any], *, me: str) -> Dict[str, Any]:
    """Named fields only. No approval status, no invited_by chain, and above all
    no password state: a colleague's account is not this caller's business
    beyond knowing they have one."""
    return {
        "username": row.get("username"),
        "email": row.get("email") or "",
        "full_name": row.get("full_name") or "",
        # Named for the column it comes from rather than for what the page calls
        # it. The indistinguishability sweep allowlists the fields that may
        # legitimately differ between two partners by NAME, and a synonym for a
        # clock reading is a field that looks like it carries a signal when it
        # does not -- which would make that test flaky rather than make this
        # response safer.
        "created_at": row.get("created_at"),
        "is_you": (row.get("username") or "") == me,
    }


@portal_router.get("/hs/members")
async def hs_members_get(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    store = _store()
    rows = [u for u in store.list_hs_portal_users(portal_user["hs_id"]) if u.get("active")]
    return {"members": [_hs_member_view(r, me=portal_user["username"]) for r in rows],
            "max_members": _HS_MAX_MEMBERS}


@portal_router.post("/hs/members",
                    dependencies=[Depends(rate_limiter("hs_members", 10, 600))])
async def hs_members_post(
    body: HsMemberRequest,
    background: BackgroundTasks,
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    """Add teammates. Each gets their own account and their own credentials.

    THE ORGANIZATION IS FIXED BY THE SESSION. This route takes addresses and
    nothing else -- no hs_id, no organization name -- so there is no version of
    it that adds somebody to a health system the caller is not already in. That
    is the same property /hs/payouts holds and for the same reason.

    Adding an address that already has an active account on this organization
    ROTATES nothing and sends nothing: re-adding a colleague by accident must
    not invalidate the password they are already using.
    """
    store = _store()
    hs = portal_user["health_system"]
    existing = [u for u in store.list_hs_portal_users(hs["hs_id"]) if u.get("active")]
    have = {(u.get("email") or "").strip().lower() for u in existing}

    wanted: List[str] = []
    for raw in (body.emails or [])[:_HS_MAX_MEMBERS]:
        addr = str(raw or "").strip().lower()
        if not addr or "@" not in addr or addr in have or addr in wanted:
            continue
        wanted.append(addr)
    if not wanted:
        return {"ok": True, "added": [],
                "members": [_hs_member_view(u, me=portal_user["username"])
                            for u in existing]}
    if len(existing) + len(wanted) > _HS_MAX_MEMBERS:
        raise HTTPException(
            status_code=400,
            detail=f"An organization can hold {_HS_MAX_MEMBERS} portal accounts. "
                   "Reply to any email from us and we will raise it.")

    added: List[Dict[str, Any]] = []
    for addr in wanted:
        minted = asc_hs_provisioning.provision_account(
            store, hs_id=hs["hs_id"], org_name=hs["name"], email=addr,
            signup_source="member_invite", invited_by=portal_user["username"],
            # The same decision their colleague's account got. A member is not a
            # lesser account: they can answer the questions and they can sign.
            approval_status=(portal_user.get("approval_status") or None))
        store.log_event(entity_type="health_system", entity_id=hs["hs_id"],
                        event_type="member_added", actor=portal_user["username"],
                        payload={"username": minted["username"], "email": addr})
        added.append({"email": addr, "username": minted["username"],
                      "passphrase": minted["passphrase"]})

    inviter = (portal_user.get("full_name") or "").strip() or "A colleague"
    # Read here, on the row this request already holds, rather than inside the
    # background task: the task runs after the response and a refetch there would
    # be a second read of a row that could have moved under it, which would mail
    # a member the wrong story about their own organization.
    background.add_task(_notify_hs_members_added, hs["name"], inviter, added,
                        hs_states.state_of(hs) == hs_states.AWAITING_DLA)
    fresh = [u for u in store.list_hs_portal_users(hs["hs_id"]) if u.get("active")]
    return {
        # The passphrases are NOT echoed. They go to the address they belong to
        # and nowhere else -- a colleague who can read another colleague's
        # credential out of a JSON response is a credential that is not theirs.
        "ok": True,
        "added": [a["email"] for a in added],
        "members": [_hs_member_view(u, me=portal_user["username"]) for u in fresh],
    }


def _notify_hs_members_added(organization: str, inviter: str,
                             added: List[Dict[str, Any]],
                             awaiting_dla: bool = False) -> None:
    """One letter per new member, each carrying only its own credential.

    ``awaiting_dla`` is passed in rather than looked up. This runs after the
    response has gone out, so there is no request row to read and no session to
    read it from; the caller resolved the organization's state while it still
    had both.
    """
    if not is_email_transport_configured():
        return
    try:
        from asclepius.ingest_notify import _run_coro
        from onboarding_emails import build_hs_member_added_email
        for member in added:
            _run_coro(send_html_email(
                member["email"],
                f"{inviter} added you to {organization}'s Archangel Health workspace",
                build_hs_member_added_email(
                    organization=organization, added_by=inviter,
                    username=member["username"], temp_password=member["passphrase"],
                    portal_url=_hs_portal_url(), awaiting_dla=awaiting_dla)))
    except Exception:
        log.exception("hs members: invite email failed")


# ─── The agreement ───────────────────────────────────────────────────────────
class HsAgreementSignRequest(BaseModel):
    """The four things a clickwrap has to capture, named separately.

    One "I agree" boolean would be cheaper and would collapse two legally
    distinct affirmations into one: authority to bind the organization, and
    consent to transact electronically. Courts test them separately, so the
    record keeps them separate.
    """

    typed_name: str
    typed_title: str
    authority_affirmed: bool = False
    consent_esign: bool = False
    #: The hash the client was shown. Echoed back so a signature can only be
    #: taken against a document the signer actually saw -- see the mismatch
    #: branch below.
    doc_sha256: str = ""


@portal_router.get("/hs/agreement")
async def hs_agreement_get(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    """The agreement, in full, as text.

    FULL TEXT, not a link to a PDF. §5.1: a clickwrap that requires downloading
    something to read it is a clickwrap whose "I have read this" is provably
    false, and the reasonable-notice line in the case law is exactly there.

    Served to every state, not only to the one that can sign. An organization
    still filling in the application is entitled to read what it would be
    signing before it answers anything, and a member who arrives after the
    signature is entitled to read what their organization agreed to.
    """
    store = _store()
    hs = portal_user["health_system"]
    try:
        text, sha = asc_dla.signable(organization=hs["name"])
    except asc_dla.AgreementError:
        log.exception("agreement source is unreadable")
        raise HTTPException(status_code=503,
                            detail="The agreement could not be loaded just now. "
                                   "Please try again in a moment.")
    signed = _hs_agreement_summary(store, hs)
    return {
        "doc_version": asc_dla.CURRENT_VERSION,
        "doc_sha256": sha,
        "text": text,
        "organization": hs["name"],
        # Whether THIS session may sign right now. False once somebody has, and
        # false before approval, and the reason is in `next_step` either way.
        "can_sign": bool(hs_states.can_sign(hs) and not signed),
        "signed": signed,
        "signer_name_prefill": portal_user.get("full_name") or "",
        **hs_states.public_view(hs),
    }


@portal_router.post("/hs/agreement/sign",
                    dependencies=[Depends(rate_limiter("hs_agreement_sign", 5, 600))])
async def hs_agreement_sign(
    request: Request,
    background: BackgroundTasks,
    body: HsAgreementSignRequest,
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    """Take one signature, render the record, and open the upload door.

    The order of operations is the whole of it, and it is deliberate:

      1. verify the state, the affirmations, and that the text has not changed
         underneath the signer;
      2. render the PDF and store it, so a row can never point at a document
         that does not exist;
      3. INSERT the signature row -- append-only, enforced by trigger;
      4. move the organization to `active`;
      5. mail the copies, in the background.

    A failure at step 2 leaves nothing signed. A failure at step 5 leaves an
    agreement that is signed and an email that did not arrive, which is the
    right way round: the signature is the fact, the email is a copy of it.
    """
    store = _store()
    hs = portal_user["health_system"]

    # ALREADY-SIGNED IS CHECKED FIRST, and the order is the point. Signing moves
    # the organization to `active`, so a second signer fails the state check too
    # -- and would be told "uploading is open", which is true and answers a
    # question they did not ask. The colleague who just clicked Sign needs to
    # know that somebody beat them to it.
    if store.latest_signed_agreement(hs["hs_id"]):
        # §0.1.2 binds the organization on one signature, and two rows for one
        # agreement is a question somebody has to answer later.
        raise HTTPException(
            status_code=409,
            detail="Your organization's agreement is already signed. "
                   "Reload the page to see who signed it and when.")
    if not hs_states.can_sign(hs):
        raise HTTPException(status_code=409, detail=_hs_locked_message(hs))
    if not body.authority_affirmed or not body.consent_esign:
        raise HTTPException(
            status_code=400,
            detail="Both confirmations are required before you can sign.")
    typed_name = " ".join((body.typed_name or "").split())[:120]
    typed_title = " ".join((body.typed_title or "").split())[:120]
    if not typed_name or not typed_title:
        raise HTTPException(status_code=400,
                            detail="Type your full name and your title to sign.")

    try:
        text, sha = asc_dla.signable(organization=hs["name"])
    except asc_dla.AgreementError:
        log.exception("agreement source is unreadable at signature")
        raise HTTPException(status_code=503,
                            detail="The agreement could not be loaded just now. "
                                   "Please try again in a moment.")
    if (body.doc_sha256 or "").strip() and body.doc_sha256.strip() != sha:
        # The document on their screen is not the document we would record. That
        # is either a deploy landing mid-read or a tampered client, and both
        # produce the same wrong outcome: a signature against text nobody agreed
        # to. Refuse and make them re-read.
        raise HTTPException(
            status_code=409,
            detail="The agreement was updated while this page was open. "
                   "Please reload and read it again before signing.")

    signed_at = asc_dla.utcnow_iso()
    signature = {
        "typed_name": typed_name, "typed_title": typed_title,
        "signed_at": signed_at, "signer_user_id": portal_user["username"],
        "signer_email": portal_user.get("email") or "",
        "ip": client_ip(request),
        "user_agent": (request.headers.get("user-agent") or "")[:400],
        "doc_version": asc_dla.CURRENT_VERSION, "doc_sha256": sha,
    }
    try:
        pdf = asc_dla.render_pdf(organization=hs["name"],
                                 version=asc_dla.CURRENT_VERSION,
                                 signature=signature)
        pdf_sha = _hashlib.sha256(pdf).hexdigest()
        from asclepius import assets as asc_assets
        asc_assets._write_blob(pdf_sha, pdf)
    except Exception:
        log.exception("agreement pdf could not be produced or stored")
        raise HTTPException(
            status_code=503,
            detail="We could not file your signed copy just now, so nothing was "
                   "signed. Please try again in a moment.")

    row = store.record_signed_agreement(
        hs_id=hs["hs_id"], doc_version=asc_dla.CURRENT_VERSION, doc_sha256=sha,
        pdf_sha256=pdf_sha, signer_user_id=portal_user["username"],
        signer_email=portal_user.get("email") or "", typed_name=typed_name,
        typed_title=typed_title, consent_esign=True, authority_affirmed=True,
        ip=signature["ip"], user_agent=signature["user_agent"])
    store.set_hs_onboarding_state(hs["hs_id"], hs_states.ACTIVE)
    store.log_event(entity_type="health_system", entity_id=hs["hs_id"],
                    event_type="agreement_signed", actor=portal_user["username"],
                    payload={"agreement_id": row["agreement_id"],
                             "doc_version": asc_dla.CURRENT_VERSION,
                             "doc_sha256": sha, "pdf_sha256": pdf_sha})

    background.add_task(_notify_agreement_signed, store, hs["hs_id"], hs["name"],
                        dict(row), pdf)
    fresh = store.get_health_system(hs["hs_id"])
    return {"ok": True, "signed": _hs_agreement_summary(store, fresh),
            **hs_states.public_view(fresh)}


def _notify_agreement_signed(store: Any, hs_id: str, organization: str,
                             row: Dict[str, Any], pdf: bytes) -> None:
    """Three letters: the countersigned copy to the signer and to us, and the
    door-is-open note to everyone on the account."""
    try:
        from asclepius.ingest_notify import _run_coro
        from onboarding_emails import (
            build_hs_agreement_receipt_email, build_hs_uploads_open_email,
        )
        import notifications
        filename = asc_dla.pdf_filename(organization=organization,
                                        version=str(row.get("doc_version") or ""))
        receipt = build_hs_agreement_receipt_email(
            organization=organization, doc_version=str(row.get("doc_version") or ""),
            signer_name=str(row.get("typed_name") or ""),
            signer_title=str(row.get("typed_title") or ""),
            signed_at=str(row.get("signed_at") or ""),
            doc_sha256=str(row.get("doc_sha256") or ""))
        attachment = [(filename, "application/pdf", pdf)]
        if is_email_transport_configured():
            signer_to = (row.get("signer_email") or "").strip()
            if signer_to:
                _run_coro(send_html_email(
                    signer_to, f"Signed: your data licensing agreement, {organization}",
                    receipt, attachments=attachment))
            opened = build_hs_uploads_open_email(
                organization=organization, portal_url=_hs_portal_url(),
                signer_name=str(row.get("typed_name") or ""),
                signed_at=str(row.get("signed_at") or ""))
            for member in store.list_hs_portal_users(hs_id):
                addr = (member.get("email") or "").strip()
                if addr and member.get("active"):
                    _run_coro(send_html_email(
                        addr, f"Uploads are open for {organization}", opened))
        # Our own copy goes through the founder alerts, which carry their own
        # addressing and their own dedupe. No attachment: it is one click away
        # in the admin card and mailing a contract to a distribution list is a
        # habit worth not starting.
        notifications.notify_founders(
            store, kind="hs_agreement_signed",
            subject=f"[Health system] Agreement signed: {organization}",
            body_html=receipt, dedupe_key=str(row.get("agreement_id") or hs_id),
            coalesce=False)
    except Exception:
        log.exception("agreement: notification failed")


@portal_router.get("/hs/agreement/document")
async def hs_agreement_document(
    portal_user: Dict[str, Any] = Depends(require_hs_surface(hs_access.INTAKE)),
):
    """The signed PDF, for the organization that signed it.

    Scoped to the SESSION's organization and takes no identifier, so there is no
    version of this route that serves one partner's contract to another. The
    E-SIGN retention requirement is what this exists for: the copy was emailed,
    inboxes lose things, and a contract you cannot get back is a contract you
    cannot rely on.
    """
    from fastapi.responses import Response as _RawResponse

    store = _store()
    hs = portal_user["health_system"]
    row = store.latest_signed_agreement(hs["hs_id"])
    if not row or not row.get("pdf_sha256"):
        raise HTTPException(status_code=404, detail="Nothing has been signed yet.")
    try:
        from asclepius import assets as asc_assets
        data, _mime = asc_assets.load_asset(str(row["pdf_sha256"]), verify=True)
    except Exception:
        # The blob is gone or corrupt. The ROW is the record and the document is
        # reproducible from it, so rebuild rather than telling a partner their
        # own contract is unavailable. Logged loudly because a missing blob is
        # an asset-store incident even when the reader never notices.
        log.exception("agreement pdf missing from the asset store; rebuilding")
        try:
            data = asc_dla.pdf_from_row(organization=hs["name"], row=row)
        except Exception:
            log.exception("agreement pdf could not be rebuilt either")
            raise HTTPException(status_code=503,
                                detail="Your signed copy could not be fetched "
                                       "just now. Please try again in a moment.")
    filename = asc_dla.pdf_filename(organization=hs["name"],
                                    version=str(row.get("doc_version") or ""))
    return _RawResponse(
        content=data, media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="{filename}"'})


# Mount the provider-facing surface. Everything above this line that a health
# system can reach carries the §3 response discipline; the admin endpoints on
# ``router`` deliberately do not.
router.include_router(portal_router)
