"""Admin — health systems + portal provisioning (PRD C).

The admin types an organization and a contact email; everything else is derived:
the health system row is created-or-reused by name, a portal username is derived
from the organization name (recognisable, not a random token), a passphrase is
generated and emailed ONCE, and only its hash is stored. ``must_reset=1`` forces
a change on first login.

This router owns the admin-facing health-system surface. The portal-facing door
(login / password / upload) lives in ``routers/asclepius_provider.py``.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from asclepius import auth as asc_auth
from asclepius.store import get_store
from email_utils import is_email_transport_configured, send_html_email

log = logging.getLogger("asclepius.admin")

router = APIRouter(prefix="/api/asclepius/admin", tags=["asclepius-admin"])


def _store():
    return get_store()


# ─── Username derivation ─────────────────────────────────────────────────────
# Generic org-name words that carry no identity. "Mass General Hospital" should
# become "massgeneral", not "massgeneralhospital".
_USERNAME_STOPWORDS = {
    "hospital", "hospitals", "health", "healthcare", "system", "systems",
    "medical", "medicine", "center", "centers", "centre", "centres", "clinic",
    "clinics", "the", "of", "and", "for", "group", "network", "regional",
    "university", "institute", "foundation", "associates", "partners", "care",
}


def derive_hs_username(org_name: str) -> str:
    """A username the recipient can recognise ("Mass General Hospital" ->
    "massgeneral"). Falls back to the full word list when stopwords would strip
    everything (e.g. "University Health System" -> "universityhealthsystem")."""
    words = re.findall(r"[a-z0-9]+", (org_name or "").lower())
    kept = [w for w in words if w not in _USERNAME_STOPWORDS]
    base = "".join(kept or words)[:20]
    return base or "partner"


def unique_hs_username(store: Any, base: str) -> str:
    """Collision-suffix: base, base2 … base9, then a short random suffix."""
    if not store.hs_username_exists(base):
        return base
    for n in range(2, 10):
        cand = f"{base}{n}"
        if not store.hs_username_exists(cand):
            return cand
    while True:
        cand = f"{base}-{secrets.token_hex(2)}"
        if not store.hs_username_exists(cand):
            return cand


# ─── Passphrase generation ───────────────────────────────────────────────────
# Word-based so hospital IT can retype it from an email without transcription
# errors; the trailing hex keeps the space large. Shown once, stored hashed,
# and must_reset=1 forces replacement at first login.
_PASSPHRASE_WORDS = [
    "amber", "aspen", "basil", "birch", "canyon", "cedar", "clover", "coral",
    "delta", "dune", "ember", "fjord", "garnet", "grove", "harbor", "hazel",
    "indigo", "juniper", "kestrel", "lagoon", "linden", "lumen", "maple",
    "meadow", "north", "opal", "orchid", "prairie", "quartz", "raven", "river",
    "saffron", "sierra", "summit", "thistle", "tundra", "umber", "violet",
    "willow", "zephyr",
]


def generate_portal_passphrase() -> str:
    words = [secrets.choice(_PASSPHRASE_WORDS) for _ in range(3)]
    return "-".join(words) + "-" + secrets.token_hex(3)


# ─── Request/response models ─────────────────────────────────────────────────
class HealthSystemProvisionRequest(BaseModel):
    organization: str
    email: EmailStr


# ─── Credentials email ───────────────────────────────────────────────────────
def _portal_url() -> str:
    import os
    base = (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")
    return base + "/provider"


def _build_credentials_email(*, org_name: str, username: str, passphrase: str) -> str:
    org = html.escape(org_name)
    user = html.escape(username)
    pw = html.escape(passphrase)
    url = html.escape(_portal_url())
    return f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#1a1b1a">
      <h2 style="font-weight:normal">Your secure upload access for {org}</h2>
      <p>Hello,</p>
      <p>Archangel Health has set up a secure portal for {org} to send us
         clinical data. Sign in here:</p>
      <p><a href="{url}" style="color:#4ca63c">{url}</a></p>
      <table style="border-collapse:collapse;margin:16px 0">
        <tr><td style="padding:6px 16px 6px 0;color:#6b6d6b">Username</td>
            <td style="padding:6px 0"><code>{user}</code></td></tr>
        <tr><td style="padding:6px 16px 6px 0;color:#6b6d6b">Temporary password</td>
            <td style="padding:6px 0"><code>{pw}</code></td></tr>
      </table>
      <p>This temporary password is shown only in this email — you will be asked
         to choose your own the first time you sign in.</p>
      <p>You can upload a .zip, or individual .json / .csv / .hl7 / .txt files
         and we will package them. If anything does not go through, reply to
         this email and we will take it by secure transfer.</p>
      <p style="color:#6b6d6b;font-size:13px;margin-top:24px">Archangel Health ·
         secure data intake. If you did not expect this email, you can ignore it.</p>
    </div>
    """


# ─── Endpoints ───────────────────────────────────────────────────────────────
@router.post("/health-systems/provision")
async def provision_health_system_portal(
    body: HealthSystemProvisionRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Create-or-reuse the health system, mint a portal account, email credentials.

    Username derives from the organization ("Mass General Hospital" ->
    "massgeneral"), collision-suffixed. A username the recipient can recognise is
    one they can find again in three weeks; a random token is one they lose.

    Password is a generated passphrase, shown ONCE in the email and stored only
    as a hash. must_reset=1 forces a change on first login. Re-provisioning the
    same organization + email rotates that account's password instead of minting
    a second account."""
    name = " ".join((body.organization or "").split())
    if not name:
        raise HTTPException(status_code=400, detail="Organization name is required.")
    if not is_email_transport_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")

    store = _store()
    hs = store.ensure_health_system(name, contact_email=str(body.email))
    passphrase = generate_portal_passphrase()

    existing = [u for u in store.list_hs_portal_users(hs["hs_id"])
                if (u.get("email") or "").lower() == str(body.email).lower() and u.get("active")]
    if existing:
        username = existing[0]["username"]
        store.set_hs_portal_password(username, passphrase, must_reset=True)
        action = "credentials_rotated"
    else:
        username = unique_hs_username(store, derive_hs_username(name))
        store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                    password=passphrase, email=str(body.email))
        action = "portal_user_created"

    ok = await send_html_email(
        str(body.email),
        f"Your Archangel Health secure upload access — {hs['name']}",
        _build_credentials_email(org_name=hs["name"], username=username, passphrase=passphrase),
        importance_headers=True,
    )
    if not ok:
        raise HTTPException(status_code=503,
                            detail="Could not send the credentials email — nothing was sent. Try again.")

    store.log_event(entity_type="health_system", entity_id=hs["hs_id"],
                    event_type=action, actor=admin["id"],
                    payload={"username": username, "email": str(body.email), "org": hs["name"]})
    return {
        "health_system": {"hs_id": hs["hs_id"], "name": hs["name"]},
        "username": username,
        "message": f"Upload access sent to {body.email} — username “{username}”, "
                   "temporary password emailed (shown once, never stored).",
    }


# ─── Health system detail: the pipeline in explicit buckets ──────────────────
# Workflow order (PRD C Phase 2). An upload can appear in more than one bucket
# when its cases had mixed outcomes — that is honest, not a bug: the operator
# needs to know that one file produced both live cases and held ones.
def _bucket_uploads(store: Any, hs_id: str) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "needs_attention": [], "needs_review": [], "ready_to_promote": [], "in_production": [],
    }
    for up in store.list_uploads_for_health_system(hs_id):
        cases = store.list_ingest_cases(upload_id=up["upload_id"])
        held = [c for c in cases if c.get("status") in ("needs_review", "quarantined")]
        clean = [c for c in cases if c.get("status") == "ingested"]
        promoted = [c for c in cases if c.get("status") == "promoted"]
        entry = {
            "upload_id": up["upload_id"],
            "filename": up.get("filename"),
            "received_at": up.get("created_at"),
            "size_bytes": up.get("size_bytes") or 0,
            "upload_status": up.get("status"),
            "case_total": len(cases),
            "case_counts": {"held": len(held), "clean": len(clean), "promoted": len(promoted)},
            "reasons": [],
            "note": up.get("reason"),
        }
        if held:
            # Safety holds must never be buried inside a normal bucket. Surface
            # the actual reasons (review flags + quarantine reasons), deduped.
            reasons: List[str] = []
            for c in held:
                for r in (c.get("review") or []):
                    txt = (r.get("detail") or r.get("reason") or "").strip()
                    if txt and txt not in reasons:
                        reasons.append(txt)
                qr = ((c.get("report") or {}).get("quarantine_reason") or "").strip()
                if qr and qr not in reasons:
                    reasons.append(qr)
            buckets["needs_attention"].append({**entry, "reasons": reasons[:6]})
        if promoted:
            buckets["in_production"].append(entry)
        if clean:
            buckets["ready_to_promote"].append(entry)
        # Uploaded, not yet examined: still moving through the pipeline (or it
        # produced nothing at all — e.g. rejected outright), and none of the
        # terminal buckets above claimed it.
        if not (held or clean or promoted):
            buckets["needs_review"].append(entry)
    return buckets


@router.get("/health-systems/{hs_id}")
async def health_system_detail(
    hs_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """One health system: portal accounts, linked physicians, and every upload
    placed into the four workflow buckets."""
    store = _store()
    hs = store.get_health_system(hs_id)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    uploads = store.list_uploads_for_health_system(hs_id)
    physicians = [u for u in store.list_users() if u.get("health_system_id") == hs_id]
    return {
        "health_system": {
            "hs_id": hs["hs_id"], "name": hs["name"],
            "contact_email": hs.get("contact_email"), "notes": hs.get("notes"),
            "active": bool(hs.get("active", 1)), "created_at": hs.get("created_at"),
        },
        "portal_users": [{"username": u["username"], "email": u.get("email"),
                          "last_login": u.get("last_login"), "active": bool(u.get("active", 1))}
                         for u in store.list_hs_portal_users(hs_id)],
        "physicians_linked": len(physicians),
        "uploads_total": len(uploads),
        "last_activity": uploads[0]["created_at"] if uploads else None,
        "buckets": _bucket_uploads(store, hs_id),
    }


@router.get("/health-systems")
async def list_health_systems(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """One row per health system with the counts the admin section needs."""
    store = _store()
    out: List[Dict[str, Any]] = []
    all_users = store.list_users()
    for hs in store.list_health_systems():
        uploads = store.list_uploads_for_health_system(hs["hs_id"])
        physicians = [u for u in all_users
                      if u.get("health_system_id") == hs["hs_id"]]  # PRD-B column; absent → 0
        last_activity: Optional[str] = uploads[0]["created_at"] if uploads else None
        out.append({
            "hs_id": hs["hs_id"],
            "name": hs["name"],
            "contact_email": hs.get("contact_email"),
            "active": bool(hs.get("active", 1)),
            "created_at": hs.get("created_at"),
            "portal_users": [{"username": u["username"], "email": u.get("email"),
                              "last_login": u.get("last_login"), "active": bool(u.get("active", 1))}
                             for u in store.list_hs_portal_users(hs["hs_id"])],
            "physicians_linked": len(physicians),
            "uploads_count": len(uploads),
            "last_activity": last_activity,
        })
    return {"health_systems": out}
