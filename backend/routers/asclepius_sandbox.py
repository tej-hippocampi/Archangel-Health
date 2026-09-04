"""Sandbox PRD §2–§4 — the sandbox router.

``/api/asclepius/sandbox/*`` is, by path, the sandbox realm: the realm
middleware routes every request here to the sandbox stores and rejects a live
token on it with 401 ``realm_mismatch``. Every route below ALSO checks the
realm itself (``require_sandbox_admin``), so a handler can never run against
live stores even if reached some other way — the guard is a 403 before any
store is opened or any file touched (§6.6).

Routes:
  GET  /status                          realm, admin email, counts (the banner reads this)
  POST /seed[?fresh=1]                  §2 — idempotent seed
  GET  /accounts                        §3.2 — roster + credentials (from env, never logged)
  POST /accounts/fresh                  §3.2 — "Seed fresh doctor"
  POST /reset                           §3.2 — drop + reseed; typed confirmation
  GET  /outbox, GET /outbox/{id},
  DELETE /outbox                        §3.3 — everything the sandbox "sent"
  GET  /copy-sources                    §4 — live health systems, read-only
  POST /copy-health-system/{hs_id}      §4 — snapshot copy
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import realm as _realm
from asclepius import auth as asc_auth
from asclepius import sandbox_seed
from asclepius.store import get_store
from ratelimit import rate_limiter

log = logging.getLogger("asclepius.sandbox")

router = APIRouter(prefix="/api/asclepius/sandbox", tags=["asclepius-sandbox"])


def _store():
    return get_store()


def require_sandbox() -> None:
    """The realm must be the sandbox AND the sandbox must be switched on."""
    if not _realm.enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    if not _realm.is_sandbox():
        raise HTTPException(status_code=403, detail={
            "code": "not_sandbox",
            "message": "This is a sandbox-only operation and this request is not in the sandbox realm."})


def require_sandbox_admin(
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
) -> Dict[str, Any]:
    require_sandbox()
    return admin


def _passwords() -> Dict[str, str]:
    admin_pw = _realm.admin_password()
    doctor_pw = _realm.doctor_password()
    if not admin_pw:
        raise HTTPException(status_code=404, detail="Not found.")
    if not doctor_pw:
        raise HTTPException(status_code=503, detail={
            "code": "sandbox_doctor_password_unset",
            "message": f"Set {_realm.DOCTOR_PASSWORD_VAR} to seed the ten physicians."})
    return {"admin": admin_pw, "doctor": doctor_pw}


# ─── Status (the banner reads this; no auth so it renders before login) ──────
@router.get("/status")
async def sandbox_status():
    require_sandbox()
    store = _store()
    # Unauthenticated and polled by every sandbox page: only create the admin
    # when the row is missing (a SELECT), never re-verify the password hash per
    # hit. Rotation is applied at boot and by /seed.
    admin = store.get_user_by_email(sandbox_seed.ADMIN_EMAIL)
    if admin is None:
        sandbox_seed.ensure_sandbox_admin()
        admin = store.get_user_by_email(sandbox_seed.ADMIN_EMAIL)
    return {
        "realm": _realm.current(),
        "enabled": True,
        "admin_email": sandbox_seed.ADMIN_EMAIL,
        "seeded": admin is not None,
        "physicians": sum(1 for s in sandbox_seed.PHYSICIANS if store.get_user_by_email(s["email"])),
        "outbox": store.outbox_count(),
        "doctor_password_set": bool(_realm.doctor_password()),
    }


# ─── §2 Seed ─────────────────────────────────────────────────────────────────
@router.post("/seed", dependencies=[Depends(rate_limiter("sandbox_seed", 10, 60))])
async def sandbox_seed_endpoint(fresh: bool = False,
                                _admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    pw = _passwords()
    return await sandbox_seed.seed(admin_password=pw["admin"], doctor_password=pw["doctor"], fresh=fresh)


# ─── §3.2 Accounts ───────────────────────────────────────────────────────────
@router.get("/accounts")
async def sandbox_accounts(_admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    """The ten doctors + credentials. The passwords come from the two env
    variables and are returned to the sandbox admin's browser only — never
    written to the repo, a log line or the database."""
    store = _store()
    doctor_pw = _realm.doctor_password()
    rows = sandbox_seed.roster(store)
    for r in rows:
        r["password"] = doctor_pw or None
    return {
        "admin": {"email": sandbox_seed.ADMIN_EMAIL, "password": _realm.admin_password()},
        "doctor_password_set": bool(doctor_pw),
        "physicians": rows,
        "links": {
            "physician_onboarding": _landing_base() + "/join?realm=sandbox",
            "org_onboarding": _landing_base() + "/?realm=sandbox&partner=1",
            "sign_in": _landing_base() + "/?realm=sandbox",
            "portal": "/sandbox/asclepius",
            "admin": "/sandbox/admin",
            "provider": "/sandbox/provider",
            "buyer": "/sandbox/buyer",
            "community": "/sandbox/community",
        },
    }


def _landing_base() -> str:
    import os  # noqa: PLC0415
    return (os.getenv("LANDING_URL") or "http://localhost:5173").strip().rstrip("/")


@router.post("/accounts/fresh")
async def sandbox_seed_fresh_doctor(_admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    """One more physician with the walkthrough still ahead of them."""
    pw = _passwords()
    store = _store()
    spec = sandbox_seed.fresh_physician_spec(store)
    user = sandbox_seed.ensure_physician(store, spec, password=pw["doctor"], onboarded=False)
    return {"ok": True, "email": user["email"], "password": pw["doctor"], "name": spec["name"]}


# ─── §3.2 Reset ──────────────────────────────────────────────────────────────
class ResetBody(BaseModel):
    confirm: str = ""
    fresh: bool = False


@router.post("/reset", dependencies=[Depends(rate_limiter("sandbox_reset", 3, 60))])
async def sandbox_reset(body: ResetBody, admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    """Drop the three sandbox DBs and the sandbox asset dir, then reseed.

    Guarded three times before a file is touched: the realm middleware (a
    live token cannot reach this path), ``require_sandbox_admin`` (the realm
    must be the sandbox), and the typed confirmation. ``sandbox_seed.reset``
    then re-checks the realm and validates every path it is about to delete
    is a derived sandbox path (§6.6)."""
    if (body.confirm or "").strip().upper() != sandbox_seed.RESET_CONFIRMATION:
        raise HTTPException(status_code=400, detail={
            "code": "confirmation_required",
            "message": f"Type {sandbox_seed.RESET_CONFIRMATION!r} to reset the sandbox."})
    pw = _passwords()
    log.warning("[sandbox] RESET requested by %s", admin.get("email"))
    return await sandbox_seed.reset(admin_password=pw["admin"], doctor_password=pw["doctor"], fresh=body.fresh)


# ─── §3.3 Outbox ─────────────────────────────────────────────────────────────
@router.get("/outbox")
async def sandbox_outbox(limit: int = 200, _admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    return {"messages": _store().outbox_list(limit=max(1, min(int(limit), 1000)))}


@router.get("/outbox/{outbox_id}")
async def sandbox_outbox_message(outbox_id: int, _admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    row = _store().outbox_get(outbox_id)
    if not row:
        raise HTTPException(status_code=404, detail="No such message.")
    return row


@router.delete("/outbox")
async def sandbox_outbox_clear(_admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    return {"ok": True, "cleared": _store().outbox_clear()}


# ─── §4 Snapshot copy ────────────────────────────────────────────────────────
@router.get("/copy-sources")
async def sandbox_copy_sources(_admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    """The LIVE health systems available to copy, read through the one
    sanctioned read-only live handle, plus the committed fixture provider."""
    from asclepius import sandbox_copy  # noqa: PLC0415

    return {"sources": sandbox_copy.list_sources()}


@router.post("/copy-health-system/{hs_id}",
             dependencies=[Depends(rate_limiter("sandbox_copy", 10, 60))])
async def sandbox_copy_health_system(hs_id: str, admin: Dict[str, Any] = Depends(require_sandbox_admin)):
    from asclepius import sandbox_copy  # noqa: PLC0415

    try:
        return sandbox_copy.copy_health_system(hs_id, actor_id=admin["id"])
    except sandbox_copy.SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
