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
    specialties = sorted({t.get("specialty") for t in store.list_tasks()} - {None, ""})
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
    counts = {"all": 0, "pending": 0, "labelers": 0, "reviewers": 0, "unassigned": 0}
    for u in _physician_users(store):
        tier = u.get("tier")
        verification = u.get("verification_status")
        counts["all"] += 1
        if verification == "pending":
            counts["pending"] += 1
        if tier == "labeler":
            counts["labelers"] += 1
        elif tier == "reviewer":
            counts["reviewers"] += 1
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
            "verification_status": verification,
            "slack_joined": _tri_state(u.get("slack_joined")),
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
        },
        "npi_payload": npi_payload,
        "task_history": [{"task_id": s.get("task_id"),
                          "submission_id": s.get("submission_id"),
                          "status": s.get("status"),
                          "created_at": s.get("created_at")} for s in submissions],
        "review_history": reviews,
    }


# ─── Health system detail: the pipeline in explicit buckets ──────────────────
# Workflow order (PRD C Phase 2). An upload can appear in more than one bucket
# when its cases had mixed outcomes — that is honest, not a bug: the operator
# needs to know that one file produced both live cases and held ones.
def _bucket_uploads(store: Any, hs_id: str) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "needs_attention": [], "rejected": [], "needs_review": [],
        "ready_to_promote": [], "in_production": [],
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
                              if c.get("specialty") and c.get("specialty") != "general"})
        undetermined = [c for c in cases if (c.get("specialty") or "general") == "general"]
        entry = {
            "upload_id": up["upload_id"],
            "filename": up.get("filename"),
            "received_at": up.get("created_at"),
            "size_bytes": up.get("size_bytes") or 0,
            "upload_status": up.get("status"),
            "case_total": len(cases),
            "case_counts": {"held": len(held), "clean": len(clean), "promoted": len(promoted)},
            "specialties": specialties,
            # Ingest no longer invents a specialty (C-3.2). Undetermined is shown
            # to the operator to set BEFORE promotion, because the promote path
            # still falls back to a literal — a wrong specialty routes the case
            # to the wrong physician pool and mislabels it in the export.
            "specialty_determined": bool(cases) and not undetermined,
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
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    n = store.set_ingest_specialty_for_upload(upload_id, specialty)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="specialty_assigned", actor=admin["id"],
                    payload={"specialty": specialty, "cases": n})
    return {"ok": True, "upload_id": upload_id, "specialty": specialty, "cases_updated": n,
            "message": f"{n} case{'' if n == 1 else 's'} set to {specialty}."}


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
