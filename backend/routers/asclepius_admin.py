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
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr

from onboarding_emails import build_asclepius_invite_email

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import ingestion as asc_ingestion
from asclepius import specialties as asc_specialties
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
    # Which of the two buttons was pressed (PRD-I §2.2). Same form, same endpoint,
    # same code path, one different value — and EVERYTHING downstream of the mint
    # is identical, which is what makes the two indistinguishable to the
    # recipient. Required: a new partner with no purpose is a decision nobody
    # made, and the promotion gate would read it as task_creation.
    purpose: str


class UploadPurposeRequest(BaseModel):
    purpose: str


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


# ─── Metrics: the four questions (PRD C Phase 6) ─────────────────────────────
@router.get("/metrics/questions")
async def metrics_questions(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """SUPPLY · QUALITY · PIPELINE · DEMAND, each with a headline figure and a
    14-day sparkline series. Cohen's κ is NOT in this payload on purpose — the
    client reads it from /stats and renders it beside expert acceptance,
    separately labeled: expert acceptance is not κ, and merging them would
    misreport the number a buyer audits most closely."""
    return _store().metrics_four_questions()


# ─── Export by case (PRD C Phase 4) ──────────────────────────────────────────
# The founder's requirement: export BY CASE, not by physician. Three combinable
# filters — Case ID (exact task id) · Specialty · Version — with a preview of
# exactly what will ship BEFORE the bundle is built. Exporting the wrong slice
# to a buyer is not recoverable; the preview is the safety mechanism, so both
# endpoints resolve the slice through the SAME function.
#
# Bundling reuses the proven build_export machinery (leak gate, schema
# validation, manifest) untouched — export.py belongs to PRD-A's workstream.
# A case with several labeler submissions cuts one bundle per submission (the
# only case-scoped hook build_export exposes today); when PRD-A's
# export_by_case lands, this endpoint is the single seam to switch over.
_VERSION_TO_PORTAL = {"V3": "v3", "V4": "v4"}


class CaseBundleRequest(BaseModel):
    case_id: Optional[str] = None
    specialty: Optional[str] = None
    version: Optional[str] = None
    note: Optional[str] = None


def _resolve_case_slice(store: Any, *, case_id: Optional[str], specialty: Optional[str],
                        version: Optional[str]) -> Dict[str, Any]:
    """The one place the three filters turn into concrete records — preview and
    bundle both call this, so what you saw is what ships."""
    version = (version or "").upper() or None
    if version == "V5":
        # V5 · Clinical RL Environment — trajectories live in env_runs, not the
        # records table, and ship through the environments pipeline.
        runs = store.env_annotation_records()
        return {"records": [], "submission_ids": [], "task_ids": set(),
                "specialties": set(), "estimated_bytes": 0, "reviews": 0,
                "v5_runs": len(runs), "exportable": False,
                "note": f"{len(runs)} annotated V5 trajectories exist. Clinical RL "
                        "Environment data ships through the environments pipeline, "
                        "not this bundle builder."}
    portal_version = _VERSION_TO_PORTAL.get(version) if version else None
    mock_ids = store.mock_annotator_id_hashes()
    records = (store.list_records(status="export_ready", specialty=specialty or None)
               + store.list_records(status="exported", specialty=specialty or None))
    matched = []
    for r in records:
        payload = r.get("payload") or {}
        if payload.get("annotator_id_hashed") in mock_ids:
            continue
        if case_id and (r.get("task_id") or payload.get("task_id")) != case_id:
            continue
        if portal_version and (payload.get("portal_version") or "v1") != portal_version:
            continue
        matched.append(r)

    # Apply the SAME profile mapping the export applies, and count only what
    # survives it (Seam 2). build_export silently drops any record whose type the
    # buyer profile does not map, so counting the candidate set instead made the
    # preview an upper bound — the operator saw "142 cases" and shipped fewer.
    # Deriving both numbers from one mapped set is the only way they cannot drift.
    from asclepius import profiles
    prof = profiles.load_profile("default")
    emitted: List[Dict[str, Any]] = []
    mapped_bytes = 0
    for rec in matched:
        payload = dict(rec.get("payload") or {})
        payload.pop("record_id", None)
        try:
            mapped = profiles.map_record(prof, payload)
        except Exception:      # a mapping failure is not a preview failure
            mapped = None
        if mapped is None:
            continue
        emitted.append(rec)
        mapped_bytes += len(json_dumps_safe(mapped))

    task_ids = {r.get("task_id") or (r.get("payload") or {}).get("task_id")
                for r in emitted} - {None}
    submission_ids: List[str] = []
    for r in emitted:
        sid = r.get("submission_id") or (r.get("payload") or {}).get("submission_id")
        if sid and sid not in submission_ids:
            submission_ids.append(sid)
    specialties = {r.get("specialty") for r in emitted} - {None, ""}
    dropped = len(matched) - len(emitted)
    note = None
    if dropped:
        note = (f"{dropped} matching record{'' if dropped == 1 else 's'} "
                "cannot be mapped to the buyer profile and will not be included.")
    return {"records": emitted, "submission_ids": submission_ids, "task_ids": task_ids,
            "specialties": specialties, "estimated_bytes": mapped_bytes,
            "reviews": store.count_case_reviews_for_tasks(sorted(task_ids)),
            "v5_runs": 0, "exportable": bool(emitted), "note": note}


def json_dumps_safe(obj: Any) -> str:
    import json as _j
    try:
        return _j.dumps(obj)
    except (TypeError, ValueError):
        return ""


@router.get("/export/case-options")
async def export_case_options(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    # Explicit high limit (C-5.3): list_tasks defaults to 500, so a specialty
    # outside the 500 most recent tasks silently never appeared in the filter —
    # the operator could not export a slice they could see existed.
    specialties = sorted({t.get("specialty") for t in store.list_tasks(limit=100000)}
                         - {None, ""})
    return {"specialties": specialties, "versions": ["V3", "V4", "V5"]}


@router.get("/export/case-preview")
async def export_case_preview(
    case_id: Optional[str] = None, specialty: Optional[str] = None,
    version: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    s = _resolve_case_slice(store, case_id=case_id, specialty=specialty, version=version)
    return {
        "cases": len(s["task_ids"]) if not s["v5_runs"] else s["v5_runs"],
        "labeler_submissions": len(s["submission_ids"]),
        "reviews": s["reviews"],
        "specialty_count": len(s["specialties"]),
        "estimated_bytes": s["estimated_bytes"],
        "exportable": s["exportable"],
        "note": s["note"],
    }


@router.post("/export/case-bundle")
async def export_case_bundle(
    body: CaseBundleRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    from asclepius import export as asc_export
    store = _store()
    s = _resolve_case_slice(store, case_id=body.case_id, specialty=body.specialty,
                            version=body.version)
    if not s["exportable"]:
        raise HTTPException(status_code=409, detail=s["note"]
                            or "Nothing matches these filters — adjust and preview again.")
    portal_version = _VERSION_TO_PORTAL.get((body.version or "").upper())
    note = body.note or "Admin export-by-case cut"
    # ONE call to PRD-A's case-centric entry point (Seam 2). This used to loop
    # build_export once per labeler submission, which meant an exact-case cut
    # fragmented into N bundles the operator downloaded one at a time, and none
    # of them carried the case-keyed cases.jsonl — so "export by case, not by
    # physician", the whole point of this surface, was not what shipped.
    export_by_case = getattr(asc_export, "export_by_case", None)
    if export_by_case is None:
        # Ships with PRD-A. Merge order is B → A → C, so this cannot happen in a
        # correctly-ordered deploy — but a legible failure beats an AttributeError.
        raise HTTPException(status_code=503,
                            detail="Case-centric export is unavailable in this build.")
    try:
        res = export_by_case(
            store, created_by=admin["id"], case_id=body.case_id or None,
            specialty=body.specialty or None, portal_version=portal_version,
            include_exported=True, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    store.log_event(entity_type="export", entity_id=res.get("export_id"),
                    event_type="export_by_case", actor=admin["id"],
                    payload={"case_id": body.case_id, "specialty": body.specialty,
                             "version": body.version, "export_id": res.get("export_id")})
    return {
        "exports": [{"export_id": res.get("export_id"), "filename": res.get("filename"),
                     "record_count": res.get("record_count")}],
        "export_id": res.get("export_id"),
        "filename": res.get("filename"),
        "record_count": res.get("record_count") or 0,
        "case_count": res.get("case_count"),
        "bundle_count": 1,
    }


# ─── Storage durability + reconciliation (PRD I-0 §F2/§F4) ───────────────────
# Reconciliation walks every case and task row and stats the whole blob tree, so
# it is far too heavy to run on each page load — and the answer changes only when
# blobs do. The boot run populates this; the page reads it; ``?refresh=1`` forces
# a fresh pass when an operator is actively investigating.
_RECONCILE_CACHE: Dict[str, Any] = {}
_RECONCILE_TTL_SEC = 900


@router.get("/storage/reconcile")
async def storage_reconcile(
    refresh: bool = False,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """What the storage layer actually holds versus what the database believes.

    Read-only. ``missing_blobs`` is an INCIDENT, not a metric: each entry is a case
    that will 404 when a physician opens its image, or ship to a buyer as a
    reference resolving to nothing. ``orphan_blobs`` is disk cost only — reported,
    never deleted, because a wrongly-deleted blob is unrecoverable once the partner
    bundle behind it has aged out of its retention window.

    Also returns the live durability verdict for all three stores, so the operator
    can see *why* blobs went missing rather than only that they did."""
    import time as _time

    from asclepius import assets as asc_assets
    from asclepius.store import _db_storage_durable

    store = _store()
    cached = _RECONCILE_CACHE.get("report")
    fresh_enough = (cached is not None
                    and (_time.monotonic() - _RECONCILE_CACHE.get("at", 0))
                    < _RECONCILE_TTL_SEC)
    if refresh or not fresh_enough:
        # Off the event loop: this is two full table walks plus a stat of the
        # entire blob tree, and doing it inline stalls every other admin request.
        report = await run_in_threadpool(asc_assets.reconcile_assets, store)
        _RECONCILE_CACHE.update({"report": report, "at": _time.monotonic()})
    else:
        report = cached
    # Durability, by contrast, is three cheap syscalls and must always be LIVE —
    # a cached "durable" verdict is exactly the reassurance nobody should get.
    stores = []
    for name, fn in (("database", _db_storage_durable),
                     ("raw ingest", asc_ingestion.ingest_storage_durable),
                     ("asset store", asc_assets.asset_storage_durable)):
        try:
            ok, why = fn()
        except Exception as exc:  # pragma: no cover - a check that cannot run fails
            ok, why = False, f"durability check raised: {exc}"
        stores.append({"store": name, "durable": bool(ok), "detail": why})
    return {
        **report,
        "missing_count": len(report["missing_blobs"]),
        "orphan_count": len(report["orphan_blobs"]),
        "storage": stores,
        "all_durable": all(s["durable"] for s in stores),
        "cached": not (refresh or not fresh_enough),
    }


# ─── Endpoints ───────────────────────────────────────────────────────────────
# include_in_schema=False: /openapi.json is served publicly (it is the Railway
# healthcheck neighbour), so a path segment named "purpose", a request field of
# that name, or a docstring mentioning brokering would disclose the business
# line to any partner who fetched the schema. They still could not tell which
# purpose is THEIRS, but §0 protects the fact that the distinction exists at
# all — a partner who learns we broker goes looking for the buyer. The admin UI
# calls these directly and never reads the schema.
@router.post("/health-systems/provision", include_in_schema=False)
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
    purpose = (body.purpose or "").strip().lower()
    if purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")
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
    # Stamped on the ACCOUNT, which is the row that authorizes an upload on this
    # door — the health-system portal has no link row (it carries the 'hs-portal'
    # sentinel link_id), so the account is where a link's purpose would live. Set
    # BEFORE the email goes out, so a mint that fails to send has still recorded
    # what the admin chose.
    store.set_hs_portal_purpose(username, purpose)

    # From here down, nothing branches. The email, its subject, its body, the
    # response and the timing are byte-identical for both purposes — the value
    # above selected DATA, and nothing about behaviour.
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
                    payload={"username": username, "email": str(body.email),
                             "org": hs["name"], "purpose": purpose})
    return {
        "health_system": {"hs_id": hs["hs_id"], "name": hs["name"]},
        "username": username,
        "purpose": purpose,
        "message": f"Upload access sent to {body.email} — username “{username}”, "
                   "temporary password emailed (shown once, never stored).",
    }


@router.post("/health-systems/{hs_id}/accounts/{username}/purpose",
             include_in_schema=False)
async def set_portal_account_purpose(
    hs_id: str, username: str, body: UploadPurposeRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Resolve or change what a portal account's uploads are for.

    Two jobs. It clears a ``Purpose not set`` work item on an account minted before
    the column existed, and it corrects a mistake. It is NOT retroactive by design:
    uploads already received keep the purpose they were stamped with, because
    rewriting history here is how a brokering case that a physician already
    labelled would silently become promotable."""
    store = _store()
    purpose = (body.purpose or "").strip().lower()
    if purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    accounts = store.list_hs_portal_users(hs_id)
    matching = [u for u in accounts if u["username"].lower() == username.lower()]
    if not matching:
        raise HTTPException(status_code=404,
                            detail="That portal account does not belong to this health system.")
    # Purpose resolves LIVE at completion, which is what makes the upload doors
    # agree — and it also means this change reaches bytes already in flight. So
    # brokering → task creation is allowed only while the account has sent
    # nothing: that is correcting a mis-click, not converting a partner's data
    # into something promotable after the fact.
    if (matching[0].get("purpose") == asc_ingestion.PURPOSE_BROKERING
            and purpose == asc_ingestion.PURPOSE_TASK_CREATION
            and store.hs_portal_account_has_activity(username)):
        raise HTTPException(
            status_code=409,
            detail="This account has already sent data on a brokering mint, so its "
                   "purpose cannot be changed to task creation — that would convert "
                   "data the partner sent us to broker. Send this organization a "
                   "separate task-creation link instead.")
    store.set_hs_portal_purpose(username, purpose)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="portal_purpose_set", actor=admin["id"],
                    payload={"username": username.lower(), "purpose": purpose})
    return {"ok": True, "username": username.lower(), "purpose": purpose,
            "message": f"Future uploads from “{username}” are recorded as "
                       f"{purpose.replace('_', ' ')}. Uploads already received keep "
                       "the purpose they arrived with."}


@router.post("/uploads/{upload_id}/purpose", include_in_schema=False)
async def set_upload_purpose(
    upload_id: str, body: UploadPurposeRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Resolve a single upload's ``Purpose not set`` work item.

    Needed because the legacy magic-link door mints links without a purpose (see
    the note on ``_link_purpose_note``), so an upload can arrive with NULL and the
    admin has to be able to say which it is BEFORE promotion reads NULL as
    task_creation."""
    store = _store()
    purpose = (body.purpose or "").strip().lower()
    if purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    # The invariant is "brokering never becomes a task". A one-call relabel from
    # brokering to task_creation IS that transition, just spelled differently —
    # and it would apply to cases a physician may already have been shown. This
    # endpoint exists to RESOLVE an unset purpose, not to overturn a decided one.
    #
    # The other direction stays open: task_creation → brokering removes a
    # promotion path and never adds one.
    current = upload.get("purpose")
    if current == asc_ingestion.PURPOSE_BROKERING \
            and purpose == asc_ingestion.PURPOSE_TASK_CREATION:
        raise HTTPException(
            status_code=409,
            detail="This upload came in on a brokering link. Its purpose cannot be "
                   "changed to task creation — brokering data never enters the task "
                   "pipeline. If the link itself was minted wrongly, mint a new one "
                   "and ask the partner to re-send.")
    store.set_upload_purpose(upload_id, purpose)
    cases = store.propagate_purpose_to_cases(upload_id)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="purpose_resolved", actor=admin["id"],
                    payload={"purpose": purpose, "cases_updated": cases})
    return {"ok": True, "upload_id": upload_id, "purpose": purpose,
            "cases_updated": cases,
            "message": f"{cases} case{'' if cases == 1 else 's'} recorded as "
                       f"{purpose.replace('_', ' ')}."}


# ─── Physicians (PRD C Phase 3) ──────────────────────────────────────────────
# Roster + profile for the Physicians admin section. Every PRD-B column (tier,
# verification_status, npi, phone, health_system_id, slack_joined, …) is read
# with .get() — before PRD-B merges the column is simply absent and the page
# must still render. Same for PRD-A's case_reviews (store method is defensive).
import json as _json


def _display_name(u: Dict[str, Any]) -> str:
    name = (u.get("full_name") or "").strip()
    if name:
        return name
    email = u.get("email") or ""
    return email.split("@", 1)[0] if "@" in email else (email or u.get("id") or "—")


# tier value -> roster count bucket. Derived from the capability layer's TIERS
# so a new tier cannot be added without a bucket to land in.
_TIER_COUNT_KEYS = {
    asc_caps.LABELER: "labelers",
    asc_caps.REVIEWER: "reviewers",
}


def _physician_users(store: Any) -> List[Dict[str, Any]]:
    """The roster population: real evaluator accounts (physicians). Mock/demo
    contributors and non-physician roles (admin, qa, data_partner, buyer) are
    operator noise here, not supply."""
    return [u for u in store.list_users()
            if u.get("role") == "evaluator" and not u.get("is_mock")]


def _hs_name_map(store: Any) -> Dict[str, str]:
    return {hs["hs_id"]: hs["name"] for hs in store.list_health_systems()}


def _tri_state(v: Any) -> Optional[bool]:
    """SQLite 1/0/NULL → True/False/None. NULL means 'not checked' and must
    survive serialization as null, never collapse to false (§5 rule 4)."""
    if v is None:
        return None
    return bool(v)


@router.get("/physicians")
async def list_physicians(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    hs_names = _hs_name_map(store)
    out: List[Dict[str, Any]] = []
    counts = {"all": 0, "pending": 0, "labelers": 0, "reviewers": 0,
              "unassigned": 0}
    for u in _physician_users(store):
        tier = u.get("tier")
        verification = u.get("verification_status")
        counts["all"] += 1
        if verification == "pending":
            counts["pending"] += 1
        # Counted off the capability layer's tier vocabulary rather than a
        # chain of literals, so a fourth tier lands in its own bucket instead of
        # silently inflating "unassigned" (Advisor PRD §2.2).
        if tier in _TIER_COUNT_KEYS:
            counts[_TIER_COUNT_KEYS[tier]] += 1
        else:
            counts["unassigned"] += 1
        hs_id = u.get("health_system_id")
        out.append({
            "id": u["id"],
            "id_hashed": u.get("id_hashed"),
            "name": _display_name(u),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "specialty": u.get("specialty"),
            "tier": tier,
            "tier_word": asc_caps.tier_word(tier),
            "verification_status": verification,
            "slack_joined": _tri_state(u.get("slack_joined")),
            "compensation_model": u.get("compensation_model"),
            "health_system_id": hs_id,
            "health_system_name": hs_names.get(hs_id) if hs_id else None,
            "active": bool(u.get("active", 1)),
        })
    return {"physicians": out, "counts": counts}


@router.get("/physicians/{user_id}")
async def physician_profile(
    user_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin)
):
    """Everything captured at onboarding: NPI + NPPES payload, CV, LinkedIn,
    board certification, years, score breakdown, Slack, task history, and —
    when they are a reviewer — review history."""
    store = _store()
    u = store.get_user_by_id(user_id)
    if not u or u.get("role") != "evaluator":
        raise HTTPException(status_code=404, detail="Physician not found")
    hs_names = _hs_name_map(store)
    npi_payload = None
    raw_npi = u.get("npi_payload_json")
    if raw_npi:
        try:
            npi_payload = _json.loads(raw_npi)
        except (ValueError, TypeError):
            npi_payload = None
    registry_payload = None
    raw_registry = u.get("registry_payload_json")
    if raw_registry:
        try:
            registry_payload = _json.loads(raw_registry)
        except (ValueError, TypeError):
            registry_payload = None

    def _blob(key):
        raw = u.get(key)
        if not raw:
            return {}
        try:
            return _json.loads(raw) or {}
        except (ValueError, TypeError):
            return {}

    credentials = _blob("credentials_json")
    attestations = _blob("attestations_json")
    flags = _blob("flags_json") if u.get("flags_json") else []
    if not isinstance(flags, list):
        flags = []

    licensure = (u.get("country_of_licensure") or "").upper()
    registry_name = None
    registry_lookup = None
    if licensure and licensure != "US":
        from asclepius.registry import config as registry_config

        cfg = registry_config.for_country(licensure)
        registry_name = cfg.registry_name
        if cfg.lookup_url:
            registry_lookup = cfg.lookup_url.replace(
                "{id}", (u.get("registry_id") or "").strip())

    hs_id = u.get("health_system_id")
    submissions = store.list_submissions(evaluator_id=user_id, limit=200)
    reviews = store.list_case_reviews_for_reviewer(user_id)
    return {
        "physician": {
            "id": u["id"],
            "id_hashed": u.get("id_hashed"),
            "name": _display_name(u),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "specialty": u.get("specialty"),
            "board_cert": u.get("board_cert"),
            "years_experience": u.get("years_experience"),
            "tier": u.get("tier"),
            "tier_score": u.get("tier_score"),
            "tier_assigned_at": u.get("tier_assigned_at"),
            "verification_status": u.get("verification_status"),
            "verification_notes": u.get("verification_notes"),
            "verified_by": u.get("verified_by"),
            "verified_at": u.get("verified_at"),
            "npi": u.get("npi"),
            "npi_verified": _tri_state(u.get("npi_verified")),
            "npi_checked_at": u.get("npi_checked_at"),
            "email_domain_class": u.get("email_domain_class"),
            "linkedin_url": u.get("linkedin_url"),
            "cv_on_file": bool(u.get("cv_asset_sha")),
            "slack_joined": _tri_state(u.get("slack_joined")),
            "slack_checked_at": u.get("slack_checked_at"),
            "health_system_id": hs_id,
            "health_system_name": hs_names.get(hs_id) if hs_id else None,
            "created_at": u.get("created_at"),
            "active": bool(u.get("active", 1)),
            # Where this doctor practises and which registry answers for them.
            "country_of_practice": u.get("country_of_practice"),
            "country_of_licensure": u.get("country_of_licensure"),
            "registry_name": registry_name,
            "registry_id": u.get("registry_id"),
            "registry_verified": _tri_state(u.get("registry_verified")),
            "registry_checked_at": u.get("registry_checked_at"),
            # Where an admin goes to check by hand when there is no API.
            "registry_lookup_url": registry_lookup,
            "flagged": bool(u.get("flagged")),
        },
        "npi_payload": npi_payload,
        "registry_payload": registry_payload,
        # The credentials and attestations blobs. These were captured at
        # signup, returned by the verification queue, and rendered by nothing:
        # licence number, degree, residency, fellowship, practice status and
        # the initials someone signed with were invisible on every admin
        # surface, which made "check their credentials" impossible to actually
        # do from the credentials page.
        "credentials": credentials,
        "attestations": attestations,
        "flags": flags,
        "task_history": [{"task_id": s.get("task_id"),
                          "submission_id": s.get("submission_id"),
                          "status": s.get("status"),
                          "created_at": s.get("created_at")} for s in submissions],
        "review_history": reviews,
    }


# ═══ Signups in flight — the half of the funnel that had no screen ═══════════
#
# The roster above answers "who has an account". Nothing answered "who is
# TRYING to get one", and those two questions are answered by two different
# databases:
#
#   tenant store (team.db)      a physician clicks "Become a contributor" ->
#                               health_systems row (+ asclepius_people for the
#                               clinicians a director invites). Every wizard
#                               step writes here.
#   asclepius store             they press the LAST button (/asclepius/finish)
#   (asclepius.db)              -> users row -> roster + verification queue.
#
# Only the second one had an admin surface. So a physician who requested a link
# on Monday and stalled on the credentials step was, to this console, a person
# who did not exist — while ``/api/onboarding/self-serve`` had already emailed
# the founder "[Onboarding] Physician contributor started" about them. The
# operator was being told about people the operator could not see, could not
# count, and could not chase; the roster read "1 physician" beside an inbox
# holding dozens. An empty screen is not the same claim as "nobody signed up",
# and this endpoint is what stops the console from making the first look like
# the second.
#
# These are NOT approvable and are deliberately kept out of the roster and the
# verification queue: nobody here has submitted a complete credential record, so
# folding them in would mean approving physicians on partial evidence — the one
# thing the verification gate exists to prevent. They are a chase list.

#: Wizard progress, in order. The last one is the painful case: everything was
#: submitted and they simply never pressed the final button, so a single
#: reminder converts them into an approvable signup.
_SIGNUP_STAGES: List[tuple] = [
    ("link_sent", "Link sent — not opened"),
    ("identity", "Entered their name"),
    ("email_verified", "Verified their email"),
    ("institution", "Added practice details"),
    ("credentials", "Submitted credentials"),
    ("attestations", "Signed attestations — never pressed finish"),
]
_SIGNUP_STAGE_WORDS = dict(_SIGNUP_STAGES)
_SIGNUP_STAGE_ORDER = [s for s, _ in _SIGNUP_STAGES]

#: Days without a wizard write before a signup is "stalled". Three, because the
#: link lives for a week or two — flagging on day one would mark every normal
#: signup that got interrupted by a clinic day.
_SIGNUP_STALLED_DAYS = 3


def _team_store(request: Request) -> Any:
    ts = getattr(request.app.state, "team_store", None)
    if ts is None:  # pragma: no cover — the app always sets it at boot
        raise HTTPException(status_code=503, detail="The onboarding store is unavailable.")
    return ts


def _landing_base() -> str:
    """Must match ``routers/onboarding.py``'s ``_landing_base`` exactly.

    The wizard is served by the LANDING app, not the backend — so falling back
    to ``BASE_URL`` here (tempting: it is set everywhere) would mint
    ``https://api.../onboard/<token>``, a link that 404s for the physician while
    looking perfectly correct in the admin's confirmation toast.
    """
    return (os.getenv("LANDING_URL") or "http://localhost:5173").strip().rstrip("/")


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse a stored timestamp to a NAIVE UTC datetime.

    Every writer in the tenant store is ``datetime.utcnow()`` (naive), and the
    comparisons below are against ``utcnow()`` — so one offset-bearing value
    ("...+00:00", from a legacy row, an import, or the day someone modernizes
    ``_utcnow_iso`` to ``datetime.now(timezone.utc)``) raises TypeError on the
    compare, not the parse, and 500s the entire Signups screen. Normalizing here
    is the difference between a wrong-by-hours idle count and a dead page.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _days_since(value: Any) -> Optional[int]:
    dt = _parse_iso(value)
    if dt is None:
        return None
    return max(0, (datetime.utcnow() - dt).days)


def _link_expired(value: Any) -> bool:
    """Expired means the token is DEAD — the physician clicking their emailed
    link now gets a 404. NULL expiry is treated as expired for the same reason
    ``onboarding_token_valid`` does: no expiry means no usable token."""
    dt = _parse_iso(value)
    if dt is None:
        return True
    return dt < datetime.utcnow()


def _signup_stage(*, step: int, email_verified: bool,
                  has_credentials: bool, has_attestations: bool) -> str:
    """Furthest point reached, from what the wizard actually persisted.

    Read newest-first rather than as a step counter: an invited clinician never
    touches ``onboarding_step`` at all (it lives on the health system, not on
    them), so a step-only reading would report every team member as "link sent"
    forever, however much they had filled in.
    """
    if has_attestations:
        return "attestations"
    if has_credentials:
        return "credentials"
    if step >= 3:
        return "institution"
    if email_verified or step >= 2:
        return "email_verified"
    if step >= 1:
        return "identity"
    return "link_sent"


def _signup_row(*, email: str, name: Optional[str], kind: str, hs: Dict[str, Any],
                stage: str, started_at: Any, last_activity: Any,
                expires_at: Any, credentials: Dict[str, Any]) -> Dict[str, Any]:
    idle = _days_since(last_activity or started_at)
    expired = _link_expired(expires_at)
    return {
        # health_system_id + email is the composite key the resend endpoint takes
        # back; there is no single id, because a director is keyed by the health
        # system row and an invited clinician by their address on it.
        "health_system_id": hs.get("id"),
        "email": email,
        "name": (name or "").strip() or None,
        # "director" is the self-serve physician who created the workspace —
        # which, for a solo contributor signing up from the landing page, is
        # every one of them. Not an operator role; see onboarding.py's note.
        "kind": kind,
        # 'general' marks a /join?flavor=general signup (an invited
        # non-clinical signer): the admin should not wait on an NPI for them.
        "signup_flavor": (hs.get("signup_flavor") or "").strip() or None,
        "org_name": (hs.get("name") or "").strip() or None,
        "specialty": (hs.get("specialty") or "").strip() or None,
        "npi": (str(credentials.get("npi") or "").strip() or None),
        "stage": stage,
        "stage_word": _SIGNUP_STAGE_WORDS[stage],
        "stage_index": _SIGNUP_STAGE_ORDER.index(stage) + 1,
        "stage_total": len(_SIGNUP_STAGE_ORDER),
        # The one that converts with a single reminder: everything submitted,
        # final button never pressed.
        "ready_to_finish": stage == "attestations",
        "started_at": started_at,
        "last_activity": last_activity or started_at,
        "days_idle": idle,
        "stalled": bool(idle is not None and idle >= _SIGNUP_STALLED_DAYS and not expired),
        "link_expires_at": expires_at,
        # No token, ever. This is a list an operator reads; the link itself is
        # only minted (fresh) by the resend endpoint, and only into the
        # physician's own inbox.
        "link_expired": expired,
    }


class RoleBody(BaseModel):
    role: str


@router.post("/users/{user_id}/role")
async def set_user_role(
    user_id: str,
    body: RoleBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Grant or revoke the admin role on one account.

    This is how the second founder becomes an admin without touching env
    vars: the env-bootstrapped admin promotes their existing account once
    from the roster. Two roles only; every other role is provisioned by its
    own flow and must not be reachable from a console button. Self-demotion
    is refused so the console cannot lock its last operator out.
    """
    role = (body.role or "").strip().lower()
    if role not in ("admin", "evaluator"):
        raise HTTPException(status_code=422, detail="Role must be admin or evaluator.")
    store = _store()
    target = store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such account.")
    if target["id"] == admin["id"] and role != "admin":
        raise HTTPException(
            status_code=422,
            detail="You cannot demote your own account. Ask the other admin.")
    if target.get("role") not in ("admin", "evaluator"):
        raise HTTPException(
            status_code=422,
            detail="Only physician or admin accounts can move between those roles.")
    updated = store.set_user_role(user_id, role)
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="role_changed",
        actor=admin.get("email"),
        payload={"from": target.get("role"), "to": role})
    return {"ok": True, "user_id": user_id, "role": (updated or {}).get("role")}


@router.get("/signups")
async def list_signups(request: Request,
                       _admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Every physician who started onboarding and does not yet have an account.

    Sorted by most recent activity, because the person who touched the wizard an
    hour ago is the one a nudge still reaches.
    """
    ts = _team_store(request)
    store = _store()
    # Anyone already provisioned belongs on the roster, not here — including a
    # physician who re-onboards through a second link while holding an account.
    provisioned = {(u.get("email") or "").lower().strip()
                   for u in store.list_users() if u.get("email")}

    # One query each, not one per workspace.
    people_by_hs = ts.asclepius_people_by_health_system()
    otp_activity = ts.latest_otp_activity()

    rows: List[Dict[str, Any]] = []
    for hs in ts.list_health_systems_admin():
        if (hs.get("product") or "").strip().lower() != "asclepius":
            continue  # clinical (CareGuide) onboarding is a different funnel
        people = {(p.get("email") or "").lower().strip(): p
                  for p in people_by_hs.get(hs["id"], [])}
        step = int(hs.get("onboarding_step") or 0)

        director_email = (hs.get("director_email") or "").lower().strip()
        if (director_email and not hs.get("onboarding_completed_at")
                and director_email not in provisioned):
            person = people.get(director_email) or {}
            creds = person.get("credentials") or {}
            name = (person.get("full_name") or "").strip() or " ".join(
                p for p in [(hs.get("director_first_name") or "").strip(),
                            (hs.get("director_last_name") or "").strip()] if p)
            rows.append(_signup_row(
                email=director_email, name=name, kind="director", hs=hs,
                stage=_signup_stage(step=step, email_verified=step >= 2,
                                    has_credentials=bool(creds),
                                    has_attestations=bool(person.get("attestations"))),
                started_at=hs.get("created_at"),
                # Newest of the three things this signup can timestamp. The
                # health system row itself carries no updated_at, so the early
                # steps (name, email verification) would otherwise date a
                # physician by the day their LINK was issued — reporting someone
                # who verified their email minutes ago as three weeks idle, and
                # sending the operator to chase a person who is mid-signup.
                last_activity=max(
                    (t for t in (person.get("updated_at"),
                                 otp_activity.get((hs["id"], director_email)),
                                 hs.get("created_at")) if t),
                    default=None),
                expires_at=hs.get("onboarding_token_expires_at"),
                credentials=creds,
            ))

        for email, person in people.items():
            if person.get("is_director") or person.get("onboarding_completed_at"):
                continue
            if not email or email in provisioned:
                continue
            creds = person.get("credentials") or {}
            rows.append(_signup_row(
                email=email, name=person.get("full_name"), kind="invited", hs=hs,
                # An invited clinician's own link carries them from their inbox
                # to the same credential + attestation steps, so their progress
                # is read off THEIR row, never the health system's step counter.
                stage=_signup_stage(step=0,
                                    email_verified=bool(person.get("email_verified_at")),
                                    has_credentials=bool(creds),
                                    has_attestations=bool(person.get("attestations"))),
                started_at=person.get("created_at"),
                last_activity=max(
                    (t for t in (person.get("updated_at"),
                                 otp_activity.get((hs["id"], email)),
                                 person.get("created_at")) if t),
                    default=None),
                expires_at=person.get("member_token_expires_at"),
                credentials=creds,
            ))

    rows.sort(key=lambda r: str(r.get("last_activity") or ""), reverse=True)
    return {
        "signups": rows,
        "counts": {
            "total": len(rows),
            "ready_to_finish": sum(1 for r in rows if r["ready_to_finish"]),
            "stalled": sum(1 for r in rows if r["stalled"]),
            "expired": sum(1 for r in rows if r["link_expired"]),
        },
        # The number the operator actually came for: how many physicians can I
        # approve RIGHT NOW. Carried here so the two halves of the funnel are
        # legible in one read instead of one screen each.
        "awaiting_review": len(store.list_verification_queue("pending")),
        # Drives the resend button's disabled state rather than letting the
        # admin discover the missing transport by pressing it.
        "can_resend": is_email_transport_configured(),
        "stalled_after_days": _SIGNUP_STALLED_DAYS,
    }


class SignupResendRequest(BaseModel):
    health_system_id: str
    email: EmailStr


@router.post("/signups/resend")
async def resend_signup_link(
    body: SignupResendRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Mail this physician their onboarding link again, resuming where they stopped.

    Any new token is minted onto the SAME row, so a doctor who stalled after
    entering their credentials returns to a wizard that still has them. A link
    that is still alive is re-sent unchanged rather than replaced — see below.
    The link goes to their address only, never back in the response body, so
    this cannot be used to read out a live onboarding credential.
    """
    if not is_email_transport_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    ts = _team_store(request)
    email = str(body.email).lower().strip()
    hs = ts.get_health_system_by_id(body.health_system_id)
    if not hs or (hs.get("product") or "").strip().lower() != "asclepius":
        raise HTTPException(status_code=404, detail="That signup no longer exists.")
    store = _store()
    if store.get_user_by_email(email):
        raise HTTPException(
            status_code=409,
            detail="That physician already has an account — they're on the roster.")

    director_email = (hs.get("director_email") or "").lower().strip()
    org_name = (hs.get("name") or "").strip()
    if email == director_email:
        if hs.get("onboarding_completed_at"):
            raise HTTPException(status_code=409, detail="They already finished onboarding.")
        # Rotating unconditionally would kill a link the physician may have OPEN
        # RIGHT NOW: an admin nudging someone who is mid-form turns their next
        # click into "this onboarding link has expired". So a live token is
        # re-sent as-is, and only a dead or missing one is replaced. The stored
        # URL always matches the live token — both writers of
        # onboarding_token_hash set last_generated_invite_url in the same
        # statement — so re-sending it cannot mail a link that no longer works.
        existing_url = (hs.get("last_generated_invite_url") or "").strip()
        if existing_url and ts.onboarding_token_valid(hs):
            invite = {"onboarding_url": existing_url,
                      "expires_at": hs.get("onboarding_token_expires_at")}
            rotated = False
        else:
            try:
                invite = ts.reissue_onboarding_token(hs["id"], invite_base_url=_landing_base())
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            rotated = True
        url = html.escape(invite["onboarding_url"])
        subject = "Your Archangel Health onboarding link"
        body_html = (
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'color:#1a1b1a;line-height:1.6">'
            "<p>Here is your link to finish your Asclepius onboarding — it picks up "
            "exactly where you left off:</p>"
            f'<p><a href="{url}">{url}</a></p>'
            "<p style='color:#8b8d89;font-size:13px'>If you didn&rsquo;t request this, "
            "you can ignore this email.</p></div>"
        )
        expires_at = invite["expires_at"]
    else:
        person = ts.get_asclepius_person(hs["id"], email)
        if not person or person.get("is_director"):
            raise HTTPException(status_code=404, detail="That signup no longer exists.")
        if person.get("onboarding_completed_at"):
            raise HTTPException(status_code=409, detail="They already finished onboarding.")
        # An invited clinician's link CANNOT be re-sent as-is the way a
        # director's can: only the token's hash is stored, never the token, so
        # there is nothing to re-send and a new one must be minted. That is the
        # right security posture, and the cost is real — if they had the old
        # link open, it stops working. Worth knowing before pressing the button
        # on someone who is actively filling the form.
        rotated = True
        token = ts.issue_asclepius_member_token(hs["id"], email)
        person = ts.get_asclepius_person(hs["id"], email) or person
        full_name = (person.get("full_name") or "").strip()
        director_name = " ".join(
            p for p in [(hs.get("director_first_name") or "").strip(),
                        (hs.get("director_last_name") or "").strip()] if p).strip()
        subject = f"You're invited to label data with {org_name or 'your organization'}"
        body_html = build_asclepius_invite_email(
            invitee_first_name=full_name.split(" ", 1)[0] if full_name else "",
            director_full_name=director_name,
            role_label=(person.get("clinical_role") or "").replace("_", " ").title(),
            org_name=org_name,
            specialty=(hs.get("specialty") or "").strip(),
            onboarding_url=f"{_landing_base()}/onboard/m/{token}",
            invitee_email=email,
        )
        expires_at = person.get("member_token_expires_at")

    if not await send_html_email(email, subject, body_html):
        raise HTTPException(status_code=503,
                            detail="Could not send that email — nothing was sent. Try again.")
    store.log_event(entity_type="signup", entity_id=hs["id"],
                    event_type="onboarding_link_resent", actor=admin["id"],
                    payload={"email": email, "org": org_name or None, "rotated": rotated})
    return {"ok": True, "email": email, "expires_at": expires_at, "rotated": rotated,
            "message": f"A fresh onboarding link is on its way to {email}."}


# ─── Health system detail: the pipeline in explicit buckets ──────────────────
# Workflow order (PRD C Phase 2). An upload can appear in more than one bucket
# when its cases had mixed outcomes — that is honest, not a bug: the operator
# needs to know that one file produced both live cases and held ones.
def _purpose_view(purpose: Optional[str]) -> Dict[str, Any]:
    """How a purpose renders on the admin side (PRD-I §2.2, §5).

    Green for task creation because it becomes physician-authored work; muted grey
    for brokering because brokering is a normal business line, not a flag — pink
    would tell an operator it is a problem to be cleaned up. Lime for unset,
    because lime means *needs attention* and an unresolved purpose genuinely is a
    work item rather than a default."""
    if purpose == asc_ingestion.PURPOSE_TASK_CREATION:
        return {"purpose": purpose, "label": "task creation", "accent": "green",
                "resolved": True}
    if purpose == asc_ingestion.PURPOSE_BROKERING:
        return {"purpose": purpose, "label": "brokering", "accent": "grey",
                "resolved": True}
    return {"purpose": None, "label": asc_ingestion.PURPOSE_UNSET_LABEL,
            "accent": "lime", "resolved": False}


def _bucket_uploads(store: Any, hs_id: str) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "needs_attention": [], "rejected": [], "needs_review": [],
        "ready_to_promote": [], "in_production": [],
        # Brokering gets its OWN bucket rather than a badge inside another one
        # (PRD-I §5). It has a different lifecycle — it is never promoted — and
        # mixing it into "ready to promote" is exactly how something gets promoted
        # by accident, which is the failure the whole gate exists to prevent.
        "brokering": [],
    }
    uploads = store.list_uploads_for_health_system(hs_id)
    # One query for every case on the page instead of one per upload (C-5.4):
    # a health system with 500 uploads issued 500 round-trips per page load.
    cases_by_upload: Dict[str, List[Dict[str, Any]]] = {}
    for c in store.list_ingest_cases_for_uploads([u["upload_id"] for u in uploads]):
        cases_by_upload.setdefault(c.get("upload_id"), []).append(c)
    for up in uploads:
        cases = cases_by_upload.get(up["upload_id"], [])
        held = [c for c in cases if c.get("status") in ("needs_review", "quarantined")]
        clean = [c for c in cases if c.get("status") == "ingested"]
        promoted = [c for c in cases if c.get("status") == "promoted"]
        # 'general' means nothing declared a specialty (ingest refuses to guess),
        # so the operator is prompted to set the real one before promotion.
        specialties = sorted({c.get("specialty") for c in cases
                              if c.get("specialty")
                              and not asc_ingestion.specialty_is_undetermined(c.get("specialty"))})
        # Scoped to the cases promotion would actually touch. A quarantined case
        # with no specialty is not a reason to withhold the Promote button from
        # the clean ones beside it, and the promote endpoints do not read it.
        # Same rule as GET /ingestion/uploads, so the two admin surfaces cannot
        # disagree about whether one upload is ready.
        undetermined = [c for c in clean
                        if asc_ingestion.specialty_is_undetermined(c.get("specialty"))]
        entry = {
            "upload_id": up["upload_id"],
            "filename": up.get("filename"),
            "received_at": up.get("created_at"),
            "size_bytes": up.get("size_bytes") or 0,
            # The chain-of-custody triple, shown on every row (PRD-I §5): what we
            # hold, how much of it, and when we proved it. Truncated for the mono
            # chip; the full digest is on the row object for a copy action.
            "sha256": up.get("sha256"),
            "sha256_short": (up.get("sha256") or "")[:12] or None,
            "verified_at": up.get("verified_at"),
            **_purpose_view(up.get("purpose")),
            "upload_status": up.get("status"),
            "case_total": len(cases),
            "case_counts": {"held": len(held), "clean": len(clean), "promoted": len(promoted)},
            "specialties": specialties,
            # Ingest no longer invents a specialty (C-3.2). Undetermined is shown
            # to the operator to set BEFORE promotion, because the promote path
            # still falls back to a literal — a wrong specialty routes the case
            # to the wrong physician pool and mislabels it in the export.
            "specialty_determined": bool(clean) and not undetermined,
            "specialty_undetermined_cases": len(undetermined),
            "reasons": [],
            "note": up.get("reason"),
        }
        # An outright-rejected upload is dead, not pending work. Filing it under
        # "uploaded, not yet examined" is the opposite of what the buckets are
        # for — it inflates the operator's queue with things they cannot action.
        if (up.get("status") or "") in ("rejected", "failed"):
            buckets["rejected"].append({**entry, "note": up.get("reason")
                                        or "We could not read this upload."})
            continue
        # A brokering upload leaves the promotion workflow entirely. It is held,
        # downloadable, and never appears in a bucket that has a Promote button —
        # the gate in the promote endpoints is the enforcement, and this is the
        # affordance, and a design that relies on only one of the two is a design
        # that eventually promotes one by accident.
        if asc_ingestion.PURPOSE_BROKERING == up.get("purpose"):
            buckets["brokering"].append(entry)
            continue
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
                          "last_login": u.get("last_login"),
                          "active": bool(u.get("active", 1)),
                          **_purpose_view(u.get("purpose"))}
                         for u in store.list_hs_portal_users(hs_id)],
        "physicians_linked": len(physicians),
        "uploads_total": len(uploads),
        "last_activity": uploads[0]["created_at"] if uploads else None,
        "buckets": _bucket_uploads(store, hs_id),
        "link_purpose_note": _link_purpose_note(),
    }


# The magic-link door (``POST /admin/upload-links`` in routers/asclepius.py) now
# REQUIRES a purpose — it 400s without one — so no newly minted link can produce
# an unresolved upload. What remains is history: links minted before that gate,
# and uploads that arrived through them, still carry NULL.
#
# This note is what the admin reads to know whether an unresolved row is a bug or
# a leftover, so it has to describe the code as it is. Leaving it saying "the mint
# form carries no purpose" would send an operator looking for a missing field that
# is now mandatory — and, worse, teach them that "Purpose not set" is normal.
def _link_purpose_note() -> Optional[str]:
    return ("Uploads from links minted before purpose became mandatory arrive as "
            "“Purpose not set”. Resolve them on the upload row before promoting. "
            "Newly minted links always carry one.")


class UploadSpecialtyRequest(BaseModel):
    specialty: str


@router.post("/uploads/{upload_id}/specialty")
async def set_upload_specialty(
    upload_id: str,
    body: UploadSpecialtyRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Assign the specialty for an upload's cases (FIX-C C-3.2).

    Ingest no longer guesses. Where nothing declared a specialty the cases carry
    NULL and surface as "not yet determined", and this is how an operator
    resolves it — before promotion, which still falls back to a literal."""
    store = _store()
    specialty = " ".join((body.specialty or "").split()).lower()
    if not specialty:
        raise HTTPException(status_code=400, detail="A specialty is required.")
    # The whole reason this endpoint exists is that a WRONG specialty is worse
    # than a missing one — it routes the case to the wrong physician pool and
    # mislabels it in the export, invisibly, and neither is visible again once
    # the bundle ships. A free-text field accepting "nefrology" would reproduce
    # that failure through the very control built to prevent it, so the value is
    # checked against the enabled registry the picker is populated from.
    if asc_ingestion.specialty_is_undetermined(specialty):
        raise HTTPException(
            status_code=400,
            detail=f"{specialty!r} is the absence of a specialty, not a specialty. "
                   "Choose the real one.")
    if not asc_specialties.is_enabled(specialty):
        enabled = ", ".join(sorted(c["specialty"] for c in asc_specialties.list_specialties()
                                   if c.get("enabled")))
        raise HTTPException(
            status_code=400,
            detail=f"{specialty!r} is not an enabled specialty. Choose one of: {enabled}.")
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    n = store.set_ingest_specialty_for_upload(upload_id, specialty)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="specialty_assigned", actor=admin["id"],
                    payload={"specialty": specialty, "cases": n})
    return {"ok": True, "upload_id": upload_id, "specialty": specialty, "cases_updated": n,
            "message": f"{n} case{'' if n == 1 else 's'} set to {specialty}."}


class DataProviderPurposeRequest(BaseModel):
    purpose: str


# include_in_schema=False for the same reason as the routes above: /openapi.json
# is served publicly, and a path segment named `purpose` discloses that the
# distinction exists at all — a weaker leak than naming which one is a partner's,
# and still the one PRD-I §0 protects against.
@router.post("/data-providers/{provider_id}/purpose", include_in_schema=False)
async def set_data_provider_purpose(
    provider_id: str,
    body: DataProviderPurposeRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Decide what a data provider account's uploads are FOR (PRD-I §2.2).

    The fourth upload door — a provider posting to their own account — now
    records provenance like the other three: it names its account row and the
    store joins the value forward. That join needs something to find, and this is
    where an admin puts it. It lives HERE, not on the provider router, because a
    door that can name the distinction is a door that can leak it (§3.3, enforced
    statically by tests/test_purpose_isolation.py).

    Until it is set, that provider's uploads arrive "Purpose not set" and the
    admin resolves them per-upload on the upload row — an unresolved purpose is a
    work item, never a default invented for them."""
    store = _store()
    purpose = (body.purpose or "").strip().lower()
    if purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")
    if not store.set_data_provider_purpose(provider_id, purpose):
        raise HTTPException(status_code=404, detail="Data provider not found")
    store.log_event(entity_type="data_provider", entity_id=provider_id,
                    event_type="provider_purpose_set", actor=admin["id"],
                    payload={"purpose": purpose})
    view = _purpose_view(purpose)
    return {"ok": True, "provider_id": provider_id, **view,
            "message": f"Uploads from this provider are now recorded as {view['label']}. "
                       "Uploads already received keep the value they arrived with — "
                       "resolve those on the upload row."}


class HsAccessRequest(BaseModel):
    username: Optional[str] = None   # omit to apply to every account on the system
    active: Optional[bool] = None    # deactivate/reactivate endpoint only


@router.post("/health-systems/{hs_id}/unlock")
async def unlock_health_system(
    hs_id: str,
    body: Optional[HsAccessRequest] = None,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Clear brute-force lock state for a health system's portal accounts.

    Without this the only recovery from a lockout was re-provisioning, which
    rotates the passphrase and forces the hospital through another reset — a
    heavy remedy for someone else's five wrong guesses. Locks are now scoped to
    (username, ip), so this is a support tool rather than the sole defence, but
    an operator still needs a way to say "clear it now"."""
    store = _store()
    hs = store.get_health_system(hs_id)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    target = (body.username if body else None)
    usernames = [u["username"] for u in store.list_hs_portal_users(hs_id)]
    if target:
        if target.lower() not in [u.lower() for u in usernames]:
            raise HTTPException(status_code=404,
                                detail="That portal account does not belong to this health system.")
        usernames = [target.lower()]
    cleared = sum(store.clear_hs_login_attempts(u) for u in usernames)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="portal_unlocked", actor=admin["id"],
                    payload={"usernames": usernames, "attempt_rows_cleared": cleared})
    return {"ok": True, "unlocked": usernames, "attempt_rows_cleared": cleared,
            "message": f"Cleared sign-in lock for {len(usernames)} account"
                       f"{'' if len(usernames) == 1 else 's'} at {hs['name']}."}


@router.post("/health-systems/{hs_id}/access")
async def set_health_system_access(
    hs_id: str,
    body: HsAccessRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Deactivate (or restore) portal access. ``username`` scopes it to one
    account; omitting it applies to the whole health system.

    Both ``hs_portal_users.active`` and ``health_systems.active`` existed from
    the first build and nothing ever wrote them — they were columns for a
    revocation path that did not exist. ``require_hs_portal`` already rejects an
    inactive account on every request, so flipping the flag ends live sessions
    too."""
    store = _store()
    hs = store.get_health_system(hs_id)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    if body.active is None:
        raise HTTPException(status_code=400, detail="`active` is required.")
    usernames = [u["username"] for u in store.list_hs_portal_users(hs_id)]
    if body.username:
        if body.username.lower() not in [u.lower() for u in usernames]:
            raise HTTPException(status_code=404,
                                detail="That portal account does not belong to this health system.")
        usernames = [body.username.lower()]
        for u in usernames:
            store.set_hs_portal_active(u, body.active)
    else:
        for u in usernames:
            store.set_hs_portal_active(u, body.active)
        store.set_health_system_active(hs_id, body.active)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="portal_access_enabled" if body.active else "portal_access_revoked",
                    actor=admin["id"], payload={"usernames": usernames})
    verb = "restored" if body.active else "revoked"
    return {"ok": True, "active": body.active, "usernames": usernames,
            "message": f"Portal access {verb} for {len(usernames)} account"
                       f"{'' if len(usernames) == 1 else 's'} at {hs['name']}."}


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
        accounts = store.list_hs_portal_users(hs["hs_id"])
        out.append({
            "hs_id": hs["hs_id"],
            "name": hs["name"],
            "contact_email": hs.get("contact_email"),
            "active": bool(hs.get("active", 1)),
            "created_at": hs.get("created_at"),
            "portal_users": [{"username": u["username"], "email": u.get("email"),
                              "last_login": u.get("last_login"),
                              "active": bool(u.get("active", 1)),
                              **_purpose_view(u.get("purpose"))}
                             for u in accounts],
            # Per ACCOUNT, not collapsed to one value for the organization: a
            # partner may legitimately hold one account of each kind, and a single
            # summary value would have to pick a winner between them.
            "purposes": [_purpose_view(p) for p in store.hs_purposes_for(hs["hs_id"])],
            "purpose_unresolved": sum(
                1 for u in accounts if u.get("purpose") not in asc_ingestion.PURPOSES),
            "brokering_uploads": sum(
                1 for u in uploads
                if u.get("purpose") == asc_ingestion.PURPOSE_BROKERING),
            "physicians_linked": len(physicians),
            "uploads_count": len(uploads),
            "last_activity": last_activity,
        })
    return {"health_systems": out}
