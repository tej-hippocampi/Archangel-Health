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
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from onboarding_emails import build_asclepius_invite_email
from ratelimit import rate_limiter

from asclepius import auth as asc_auth
from asclepius import auto_generate as asc_auto_generate
from asclepius import capabilities as asc_caps
from asclepius import ingestion as asc_ingestion
from asclepius import payments as asc_payments
from asclepius import route_notify as asc_route_notify
from asclepius import trajectory as asc_trajectory
from asclepius import store as asc_store_mod
from asclepius import specialties as asc_specialties
from asclepius.constants import ENV_PORTAL_VERSION
from asclepius.store import get_store
from email_utils import is_email_transport_configured, send_html_email

log = logging.getLogger("asclepius.admin")

router = APIRouter(prefix="/api/asclepius/admin", tags=["asclepius-admin"])


def _store():
    return get_store()


# ─── Portal account naming ───────────────────────────────────────────────────
# Username derivation and passphrase generation moved to
# asclepius/portal_accounts.py when self-signup needed the same naming: the
# provider router cannot import this module to reach them. Re-exported here so
# every call site and test that reaches them through this router is unchanged.
from asclepius import dla as asc_dla  # noqa: E402
from asclepius import hs_provisioning as asc_hs_provisioning  # noqa: E402
from asclepius import hs_states  # noqa: E402
from asclepius.portal_accounts import (  # noqa: E402,F401
    derive_hs_username,
    generate_portal_passphrase,
    unique_hs_username,
)


# ─── Request/response models ─────────────────────────────────────────────────
class HealthSystemProvisionRequest(BaseModel):
    organization: str
    email: EmailStr
    # Which of the three buttons was pressed (PRD-I §2.2). Same form, same
    # endpoint, same code path, one different value — and EVERYTHING downstream
    # of the mint is identical, which is what makes them indistinguishable to
    # the recipient.
    #
    # Still REQUIRED even though `storage` is now a real value the form offers.
    # Omitting the field is not the same as choosing to hold the data: one is a
    # caller that forgot, the other is an operator who decided. The gate treats
    # them alike; this endpoint should not have to.
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
_VERSION_TO_PORTAL = {"V3": "v3", "V4": "v4", "V5": "v5"}

#: What each scope IS, in one line, shown beside the dropdown. Operators pick a
#: version and ship it to a buyer; "V4" alone does not say whether that is real
#: data, and the wrong slice is not recoverable once sent.
_VERSION_DESCRIPTIONS = {
    "V3": "synthetic multimodal",
    "V4": "real static",
    "V5": "real longitudinal",
}

#: The agentic tier's label. NOT a portal version and deliberately not in the
#: dropdown above: its records live in ``env_runs``, not ``records``, and it ships
#: through the environments pipeline. Naming it here rather than leaving "V5" to
#: mean two things is the point of the §5 relabel.
_ENV_SCOPE = "ENV"


#: The five scopes of the single Export tab (PRD §2.1). Every one of them
#: resolves through ``_resolve_case_slice`` — preview and bundle alike — so the
#: preview and the bundle can never disagree about what ships.
EXPORT_SCOPES = ("case", "specialty", "version", "physician", "all")

#: Bounds on the Case scope's pasted id list and on the excluded list the
#: preview renders. Both are about the same failure: an admin screen that
#: happily binds ten thousand host parameters into one query, or ships ten
#: thousand rows into one table, works fine on a demo database and falls over on
#: the real one. The COUNT stays exact when the LIST is truncated.
_MAX_CASE_IDS = 200
_MAX_EXCLUDED_ROWS = 200


class CaseBundleRequest(BaseModel):
    scope: Optional[str] = None
    case_id: Optional[str] = None
    case_ids: Optional[List[str]] = None
    specialty: Optional[str] = None
    version: Optional[str] = None
    annotator_id_hashed: Optional[str] = None
    note: Optional[str] = None
    # Optional delivery in the same action (PRD §5): build the bundle and drop it
    # in a buyer's workspace. None = build and download only.
    buyer_email: Optional[str] = None


class ApproveForExportRequest(BaseModel):
    # The submissions the operator saw in the preview's excluded list and chose
    # to approve. Explicit ids rather than "approve everything in the scope":
    # the list they were shown is the list that moves, even if the scope changed
    # in another tab a second ago.
    submission_ids: List[str] = Field(default_factory=list, max_length=500)


def _normalize_scope(scope: Optional[str], *, case_id: Optional[str],
                     case_ids: Optional[List[str]], specialty: Optional[str],
                     version: Optional[str],
                     annotator_id_hashed: Optional[str]) -> str:
    """Which scope a request means, including for the callers that predate the
    field. The old surface sent three combinable filters and no scope name;
    those requests still resolve to the same records they always did."""
    if scope in EXPORT_SCOPES:
        return scope
    if case_id or case_ids:
        return "case"
    if annotator_id_hashed:
        return "physician"
    if specialty:
        return "specialty"
    if version:
        return "version"
    return "all"


def _resolve_case_slice(
    store: Any, *, case_id: Optional[str] = None, specialty: Optional[str] = None,
    version: Optional[str] = None, case_ids: Optional[List[str]] = None,
    annotator_id_hashed: Optional[str] = None, scope: Optional[str] = None,
) -> Dict[str, Any]:
    """The one place a scope turns into concrete records.

    Preview and bundle both call this, so what you saw is what ships. That
    guarantee existed for three combinable filters; PRD §2.1 extends it to all
    five scopes (case · specialty · version · physician · all) without adding a
    second resolver — a second resolver is how a preview and a bundle drift.

    It now also answers the question the old surface could not: **what is NOT
    shipping, and why.** A slice that matched one export-ready case out of five
    used to render as "1 case" with no hint that four submissions on the same
    cases were sitting unapproved. ``excluded`` carries them, with the earning id
    the Approve button needs.
    """
    version = (version or "").upper() or None
    # An EXPLICIT scope is the new radio-button surface (PRD §2.1): one scope,
    # its own selector, the others ignored — so a stale value left in another
    # picker cannot silently narrow a bundle.
    #
    # NO scope at all is the surface that predates it: three COMBINABLE filters
    # (case · specialty · version), where `?case_id=x&specialty=y` means the
    # intersection and legitimately matches nothing. That contract is preserved
    # exactly, because it is a live API that other callers and tests depend on,
    # and quietly turning an intersection into "case only" would widen somebody's
    # export without telling them.
    explicit_scope = scope in EXPORT_SCOPES
    scope = _normalize_scope(scope, case_id=case_id, case_ids=case_ids,
                             specialty=specialty, version=version,
                             annotator_id_hashed=annotator_id_hashed)
    if explicit_scope:
        if scope != "case":
            case_id, case_ids = None, None
        if scope != "specialty":
            specialty = None
        if scope != "version":
            version = None
        if scope != "physician":
            annotator_id_hashed = None

    if version == _ENV_SCOPE:
        # ENV · Clinical RL Environment — rollouts live in env_runs, not the
        # records table, and ship through the environments pipeline.
        #
        # This branch was keyed on "V5" until the §5 relabel, which is why
        # selecting V5 returned zero records with a note about environments while
        # every longitudinal submission sat in the records table unreachable.
        # ENV is deliberately absent from the version dropdown — it is not a
        # version of the single-turn portal — so this is reached only by an
        # explicit API call. Kept as the guard for exactly that.
        runs = store.env_annotation_records()
        return {"scope": scope, "records": [], "submission_ids": [], "task_ids": set(),
                "specialties": set(), "estimated_bytes": 0, "reviews": 0,
                "env_runs": len(runs), "exportable": False,
                "excluded": {"unapproved": [], "unapproved_count": 0,
                             "dropped": 0, "mock": 0},
                "scope_json": {"type": "version", "portal_version": ENV_PORTAL_VERSION},
                "note": f"{len(runs)} annotated ENV trajectories exist. Clinical RL "
                        "Environment data ships through the environments pipeline, "
                        "not this bundle builder."}

    portal_version = _VERSION_TO_PORTAL.get(version) if version else None
    wanted_cases = [c.strip() for c in (case_ids or ([case_id] if case_id else []))
                    if (c or "").strip()]
    evaluator_id = None
    if annotator_id_hashed:
        user = store.get_user_by_id_hashed(annotator_id_hashed)
        evaluator_id = (user or {}).get("id")
        if evaluator_id is None:
            return {"scope": scope, "records": [], "submission_ids": [], "task_ids": set(),
                    "specialties": set(), "estimated_bytes": 0, "reviews": 0,
                    "env_runs": 0, "exportable": False,
                    "excluded": {"unapproved": [], "unapproved_count": 0,
                                 "dropped": 0, "mock": 0},
                    "scope_json": {"type": "physician",
                                   "annotator_id_hashed": annotator_id_hashed},
                    "note": "No contributor matches that id."}

    mock_ids = store.mock_annotator_id_hashes()
    records = (store.list_records(status="export_ready", specialty=specialty or None)
               + store.list_records(status="exported", specialty=specialty or None))
    matched: List[Dict[str, Any]] = []
    mock_excluded = 0
    for r in records:
        payload = r.get("payload") or {}
        if payload.get("annotator_id_hashed") in mock_ids:
            mock_excluded += 1
            continue
        if wanted_cases and (r.get("task_id") or payload.get("task_id")) not in wanted_cases:
            continue
        if portal_version and (payload.get("portal_version") or "v1") != portal_version:
            continue
        if annotator_id_hashed and payload.get("annotator_id_hashed") != annotator_id_hashed:
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

    # ── What is NOT shipping (PRD §2.2) ──────────────────────────────────────
    # The scope's UNIVERSE of cases, not just its exportable part — a case with
    # nothing approved on it must still be able to say so, which is the whole
    # point.
    #
    # Only the Case scope names its universe as a LIST of ids (the ones the
    # operator typed). Every other scope expresses it as the same
    # specialty / version / physician predicate the records query used, and
    # passes `task_ids=None`. That is not a shortcut: materialising "all" as
    # twenty thousand ids and binding them into an `IN (…)` would blow SQLite's
    # host-parameter ceiling and take the export tab down on the biggest slice —
    # the one an operator is most likely to reach for.
    unapproved_total = None
    if scope == "case":
        # Bounded by what a person typed, but bound it anyway: a pasted list is
        # user input reaching a parameterised IN clause.
        universe = wanted_cases[:_MAX_CASE_IDS]
        # The other predicates still apply: in the legacy combinable form a
        # caller may send `?case_id=x&specialty=y`, and an excluded list that
        # ignored the specialty would name submissions the slice never wanted.
        unapproved = store.submissions_not_shipping(
            task_ids=universe, specialty=specialty or None,
            portal_version=portal_version, evaluator_id=evaluator_id,
            limit=_MAX_EXCLUDED_ROWS + 1)
    else:
        # `task_ids=None` EXPLICITLY: the predicate below is the whole scope.
        unapproved = store.submissions_not_shipping(
            task_ids=None, specialty=specialty or None,
            portal_version=portal_version, evaluator_id=evaluator_id,
            limit=_MAX_EXCLUDED_ROWS + 1)
    if len(unapproved) > _MAX_EXCLUDED_ROWS:
        # The COUNT is the number the warning line quotes and must be exact even
        # when the LIST under it is truncated: "4 submissions will not ship" is
        # the sentence an operator acts on.
        unapproved_total = store.count_submissions_not_shipping(
            task_ids=(universe if scope == "case" else None),
            specialty=specialty or None, portal_version=portal_version,
            evaluator_id=evaluator_id)
        unapproved = unapproved[:_MAX_EXCLUDED_ROWS]
    for row in unapproved:
        row["approvable"] = bool(
            row.get("n_records")
            and row.get("status") in asc_payments.APPROVABLE_SUBMISSION_STATES
            and not row.get("quality_hold")
            and row.get("ledger_status") in (None, asc_payments.ACCRUED))
        row["reason"] = _not_shipping_reason(row)

    note = None
    if dropped:
        note = (f"{dropped} matching record{'' if dropped == 1 else 's'} "
                "cannot be mapped to the buyer profile and will not be included.")

    scope_json = {"type": scope}
    if scope == "case":
        scope_json["case_ids"] = wanted_cases
    elif scope == "specialty":
        scope_json["specialty"] = specialty
    elif scope == "version":
        scope_json["portal_version"] = portal_version
    elif scope == "physician":
        # THE HASH ONLY. The physician's name never enters the bundle, the
        # datasheet, `records.jsonl`, or the filename (PRD §2.1).
        scope_json["annotator_id_hashed"] = annotator_id_hashed
    if specialties:
        scope_json.setdefault("specialty", sorted(specialties)[0]
                              if len(specialties) == 1 else None)
    scope_json["case_count"] = len(task_ids)

    return {"scope": scope, "records": emitted, "submission_ids": submission_ids,
            "task_ids": task_ids, "specialties": specialties,
            "estimated_bytes": mapped_bytes,
            "reviews": store.count_case_reviews_for_tasks(sorted(task_ids)),
            "env_runs": 0, "exportable": bool(emitted),
            "excluded": {
                "unapproved": unapproved,
                "unapproved_count": (unapproved_total if unapproved_total is not None
                                     else len(unapproved)),
                "listed": len(unapproved),
                "truncated": unapproved_total is not None,
                "approvable_count": sum(1 for r in unapproved if r["approvable"]),
                "dropped": dropped,
                "mock": mock_excluded,
            },
            "scope_json": {k: v for k, v in scope_json.items() if v is not None},
            "note": note}


def _not_shipping_reason(row: Dict[str, Any]) -> str:
    """Why one submission will not ship, in a sentence an operator can act on.

    Ordered most-actionable first. "Awaiting approval" is the common case and
    the one the Approve button fixes; everything else is a decision somebody
    already made, and saying which one stops an operator from approving their
    way around it.
    """
    if not row.get("n_records"):
        return "No records were packaged for this submission — nothing to ship."
    status = row.get("status")
    if row.get("quality_hold"):
        return ("The payout algorithm proposed a reduced rate and is waiting on a "
                "decision. Release the hold, then approve.")
    if status == "rejected":
        return "Rejected in QA — deliberately not shipped."
    if status in ("prompt_flagged", "not_hard", "case_inconsistent"):
        return f"Flagged at capture ({status}) — captured for audit, never packaged."
    if row.get("ledger_status") == asc_payments.VOID:
        return ("The earning was voided. Voiding is a payment decision, so the "
                "records were left as they were — approve only if the work is good.")
    if status == "needs_qa":
        return "In the QA queue. Approving here skips the QA sample."
    return "Awaiting approval — approved money is what makes a record exportable."


def json_dumps_safe(obj: Any) -> str:
    import json as _j
    try:
        return _j.dumps(obj)
    except (TypeError, ValueError):
        return ""


@router.get("/export/case-options")
async def export_case_options(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Everything the five scope pickers need, in one call.

    ``specialties`` / ``versions`` are what the old two-filter surface returned
    and are unchanged. ``cases`` and ``physicians`` are new (PRD §2.1): the Case
    scope types or pastes an id and wants a typeahead behind it, and the
    Physician scope picks a PERSON BY NAME while the bundle it produces carries
    only ``annotator_id_hashed``. Both halves come from one row so a caller
    cannot cross them.
    """
    store = _store()
    # Explicit high limit (C-5.3): list_tasks defaults to 500, so a specialty
    # outside the 500 most recent tasks silently never appeared in the filter —
    # the operator could not export a slice they could see existed.
    specialties = sorted({t.get("specialty") for t in store.list_tasks(limit=100000)}
                         - {None, ""})
    cases = [
        {"case_id": c["task_id"], "specialty": c.get("specialty"),
         "case_source": c.get("case_source"), "portal_version": c.get("portal_version"),
         "submissions": int(c.get("n_submissions") or 0),
         "shippable": int(c.get("n_shippable") or 0)}
        for c in store.export_case_directory()
    ]
    physicians = [
        {"name": _display_name(p), "annotator_id_hashed": p.get("id_hashed"),
         "specialty": p.get("specialty"), "cases": int(p.get("n_cases") or 0),
         "submissions": int(p.get("n_submissions") or 0)}
        for p in store.export_physician_directory()
        # A mock/sandbox contributor's records are hard-excluded from every
        # bundle, so offering them as a scope offers an empty bundle.
        if not p.get("is_mock") and p.get("id_hashed")
    ]
    return {"specialties": specialties, "versions": ["V3", "V4", "V5"],
            # One line per version, so an operator picking a slice to send a buyer
            # can see what it IS. "V4" alone does not say "real static", and the
            # wrong slice is not recoverable once it has been sent. ENV is not
            # here on purpose: it is not a version of the single-turn portal.
            "version_descriptions": dict(_VERSION_DESCRIPTIONS),
            "scopes": list(EXPORT_SCOPES), "cases": cases, "physicians": physicians}


def _preview_payload(s: Dict[str, Any]) -> Dict[str, Any]:
    """One slice, rendered for the preview. The counts and the excluded list come
    from the SAME resolve call the bundle uses, which is what makes the preview a
    promise rather than an estimate."""
    excluded = s["excluded"]
    return {
        "scope": s.get("scope"),
        "cases": len(s["task_ids"]) if not s["env_runs"] else s["env_runs"],
        "labeler_submissions": len(s["submission_ids"]),
        "reviews": s["reviews"],
        "specialty_count": len(s["specialties"]),
        "estimated_bytes": s["estimated_bytes"],
        "exportable": s["exportable"],
        "note": s["note"],
        # PRD §2.2 — the part that would have answered the question. Yesterday's
        # export would have read: "1 case ships. 1 submission on
        # v4real-v4-neph-001 is awaiting approval and will not ship."
        "excluded": {
            "unapproved_count": excluded["unapproved_count"],
            "approvable_count": excluded.get("approvable_count", 0),
            # How many of that count are actually LISTED below. The count is
            # exact; the list is capped (PRD §2.2 has to stay true on the real
            # database, not only on a demo one).
            "listed": excluded.get("listed", excluded["unapproved_count"]),
            "truncated": bool(excluded.get("truncated")),
            "dropped": excluded["dropped"],
            "mock": excluded["mock"],
            "unapproved": [
                {"submission_id": r["submission_id"], "case_id": r["task_id"],
                 "earning_id": r.get("earning_id"), "status": r.get("status"),
                 "ledger_status": r.get("ledger_status"),
                 "specialty": r.get("specialty"),
                 "portal_version": r.get("portal_version"),
                 "annotator_id_hashed": r.get("annotator_id_hashed"),
                 "approvable": r["approvable"], "reason": r["reason"]}
                for r in excluded["unapproved"]
            ],
        },
        "scope_json": s.get("scope_json"),
    }


@router.get("/export/case-preview")
async def export_case_preview(
    case_id: Optional[str] = None, specialty: Optional[str] = None,
    version: Optional[str] = None, scope: Optional[str] = None,
    case_ids: Optional[str] = Query(None,
        description="comma-separated case ids for the Case scope"),
    annotator_id_hashed: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    s = _resolve_case_slice(
        store, scope=scope, case_id=case_id,
        case_ids=[c for c in (case_ids or "").split(",") if c.strip()] or None,
        specialty=specialty, version=version,
        annotator_id_hashed=annotator_id_hashed)
    return _preview_payload(s)


@router.post(
    "/export/approve",
    dependencies=[Depends(rate_limiter("asclepius_export_approve_all", 30, 600))],
)
async def export_approve_unapproved(
    body: ApproveForExportRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Approve, for export, the submissions the preview said would not ship.

    This is the "[ Approve all 4 ]" button (PRD §2.2). It calls the SAME policy
    function the single-row Approve button calls — ``payments.approve_earning``
    — once per submission, server-side. One round trip instead of N, one rate
    limit instead of N, and exactly one definition of what approving means.

    Two cases it handles that the per-row button never sees:

    * a submission whose ledger row does not exist yet. The accrual sweep
      materialises rows lazily, so a freshly submitted case may have none.
      ``reconcile_task_accruals`` runs first, which is the same call the admin
      ledger makes on every page load.
    * a submission whose author does not accrue payment at all (an advisor on
      the equity-only model). There is no money to approve, and the work is
      still real: the export gate moves on its own. "Approved money ⇔ exportable
      record" holds vacuously — there is no money either way.
    """
    store = _store()
    wanted = [s for s in dict.fromkeys(body.submission_ids or []) if (s or "").strip()]
    if not wanted:
        raise HTTPException(status_code=422,
                            detail="Name the submissions to approve.")
    try:
        asc_payments.reconcile_task_accruals(store)
    except Exception:
        log.exception("export approve-all: reconciliation failed; approving what exists")

    results: List[Dict[str, Any]] = []
    approved = 0
    for sub_id in wanted:
        sub = store.get_submission(sub_id)
        if sub is None:
            results.append({"submission_id": sub_id, "ok": False, "outcome": "missing"})
            continue
        earning = store.get_earning(kind=asc_payments.KIND_TASK, ref_id=sub_id)
        if earning is None:
            # No ledger row and none coming: an equity-only contributor, or a
            # kind that does not accrue. Move the export gate on its own.
            gate = asc_payments.apply_ledger_decision_to_records(
                store, submission_id=sub_id, decision="approve",
                reason="admin_approved_no_ledger", actor=admin["email"])
            store.log_event(
                entity_type="submission", entity_id=sub_id,
                event_type="records_approved_without_ledger", actor=admin["email"],
                payload={"prior_qa": gate["prior_status"], "outcome": gate["outcome"]})
            approved += 1 if gate["moved"] else 0
            results.append({"submission_id": sub_id, "ok": bool(gate["moved"]),
                            "outcome": gate["outcome"], "earning_id": None})
            continue
        res = asc_payments.approve_earning(
            store, earning_id=earning["earning_id"], actor=admin["email"],
            note="Approved from the export preview")
        if res["ok"]:
            approved += 1
            results.append({"submission_id": sub_id, "ok": True,
                            "outcome": res["gate"]["outcome"],
                            "earning_id": earning["earning_id"]})
            continue
        if res["refusal"] in ("already_approved", "already_paid"):
            # THE CONVERGENCE RULE, applied to a row the ledger already decided.
            # This is the population §4 backfills: money settled, records never
            # told. Refusing here would leave the operator staring at a case that
            # says "approved" in the ledger and "will not ship" in the preview,
            # with no button that fixes it.
            gate = asc_payments.apply_ledger_decision_to_records(
                store, submission_id=sub_id, decision="approve",
                reason="ledger_already_approved", actor=admin["email"])
            if gate["moved"]:
                approved += 1
                store.log_event(
                    entity_type="submission", entity_id=sub_id,
                    event_type="records_backfilled_from_ledger", actor=admin["email"],
                    payload={"earning_id": earning["earning_id"],
                             "ledger_status": res["refusal"],
                             "prior_status": gate["prior_status"]})
            results.append({"submission_id": sub_id, "ok": bool(gate["moved"]),
                            "outcome": gate["outcome"],
                            "earning_id": earning["earning_id"]})
            continue
        results.append({"submission_id": sub_id, "ok": False,
                        "outcome": res["refusal"],
                        "earning_id": earning["earning_id"]})
    return {"requested": len(wanted), "approved": approved, "results": results}


@router.get("/export/migration-report")
async def export_migration_report(
    request: Request,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """What the boot migration did, and whether any row was lost (PRD §0, §4).

    Read this instead of SSHing into a container. It reports the run that
    happened when this process started: how many cases had been approved or
    paid but could not ship, how many are now exportable, how many voided
    earnings were deliberately left alone — and the no-data-loss contract check
    taken around the sweep by the sweep itself.

    ``ran`` is False when the process has not finished the boot sweep yet (it
    runs off the event loop) or when this build started before the migration
    existed. Refresh; it does not need a redeploy.
    """
    report = getattr(request.app.state, "asclepius_export_backfill", None)
    if not report:
        return {"ran": False,
                "message": "The boot migration has not reported yet. It runs a "
                           "few seconds after startup — refresh shortly."}
    contract = report.get("contract")
    return {
        "ran": True,
        "cases_stranded": report.get("candidates", 0),
        "cases_now_exportable": report.get("moved", 0),
        "skipped": report.get("skipped", 0),
        "voided_left_untouched": report.get("voided_untouched", 0),
        "error": bool(report.get("error")),
        "no_data_loss": {
            "checked": contract is not None,
            "ok": (contract or {}).get("ok"),
            "problems": (contract or {}).get("problems") or [],
            "row_counts_before": {k: (v or {}).get("count")
                                  for k, v in ((contract or {}).get("before") or {}).items()},
            "row_counts_after": {k: (v or {}).get("count")
                                 for k, v in ((contract or {}).get("after") or {}).items()},
        },
        # Case ids, so an operator can go look at one rather than trust a count.
        "cases": [{"case_id": r.get("case_id"), "submission_id": r.get("submission_id"),
                   "was": r.get("prior_status"), "outcome": r.get("outcome")}
                  for r in (report.get("rows") or [])[:200]],
    }


@router.post("/export/case-bundle")
async def export_case_bundle(
    body: CaseBundleRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    from asclepius import export as asc_export
    store = _store()
    s = _resolve_case_slice(
        store, scope=body.scope, case_id=body.case_id, case_ids=body.case_ids,
        specialty=body.specialty, version=body.version,
        annotator_id_hashed=body.annotator_id_hashed)
    if not s["exportable"]:
        # The 409 now SAYS WHY, when there is a why to say. "Nothing matches"
        # next to four unapproved submissions on the very cases you selected is
        # the silence this PRD exists to remove.
        n = s["excluded"]["unapproved_count"]
        detail = s["note"] or "Nothing matches this scope — adjust and preview again."
        if n:
            detail = (f"Nothing in this scope is approved yet: {n} submission"
                      f"{'' if n == 1 else 's'} on these cases "
                      f"{'is' if n == 1 else 'are'} not approved and will not ship. "
                      "Approve them from the preview, then export.")
        raise HTTPException(status_code=409, detail=detail)
    portal_version = _VERSION_TO_PORTAL.get((body.version or "").upper())
    note = body.note or "Admin export cut"
    scope_json = dict(s["scope_json"])
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
    case_ids = scope_json.get("case_ids") or None
    try:
        res = export_by_case(
            store, created_by=admin["id"],
            case_id=(case_ids[0] if case_ids and len(case_ids) == 1 else None),
            case_ids=(case_ids if case_ids and len(case_ids) > 1 else None),
            specialty=(s["scope"] == "specialty" and body.specialty) or None,
            portal_version=(portal_version if s["scope"] == "version" else None),
            annotator_id_hashed=(scope_json.get("annotator_id_hashed")),
            include_exported=True, note=note,
            # Carried into the datasheet's Composition section (PRD §2.3) so a
            # lab knows how the bundle was cut. Hashes and counts only.
            scope=scope_json)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except asc_export.ExportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    export_id = res.get("export_id")
    # §2.4: the scope is persisted ON the export row, so History can say what a
    # bundle WAS rather than only how big it was. Existing rows have no scope and
    # render as ``legacy``; nothing is re-generated.
    store.set_export_scope(export_id, scope_json)
    store.log_event(entity_type="export", entity_id=export_id,
                    event_type="export_by_case", actor=admin["id"],
                    payload={"scope": scope_json, "export_id": export_id})

    delivery = None
    if (body.buyer_email or "").strip():
        # PRD §5 — "Export + send to ▾". The CRM is gone; the delivery rail is
        # not. Attached to the export that was just built rather than rebuilding
        # a second, differently-filtered one.
        from routers import asclepius_buyer as asc_buyer_router
        delivery = await asc_buyer_router.deliver_existing_export(
            store, export_id=export_id, buyer_email=body.buyer_email.strip(),
            label=_scope_label(scope_json), admin=admin, note=body.note)

    return {
        "exports": [{"export_id": export_id, "filename": res.get("filename"),
                     "record_count": res.get("record_count")}],
        "export_id": export_id,
        "filename": res.get("filename"),
        "record_count": res.get("record_count") or 0,
        "case_count": res.get("case_count"),
        "bundle_count": 1,
        "scope": scope_json,
        "delivery": delivery,
    }


def _scope_label(scope_json: Dict[str, Any]) -> str:
    """A human label for one scope — for a delivery record and the History row.

    Hashes and ids only. A physician-scoped delivery is labelled by hash for the
    same reason the bundle is: the name never leaves this building attached to
    the data (PRD §2.1).
    """
    stype = scope_json.get("type") or "scope"
    if stype == "case":
        ids = scope_json.get("case_ids") or []
        return f"Case · {', '.join(ids[:3])}" + (" +…" if len(ids) > 3 else "")
    if stype == "specialty":
        return f"Specialty · {scope_json.get('specialty')}"
    if stype == "version":
        return f"Version · {scope_json.get('portal_version')}"
    if stype == "physician":
        return f"Physician · {str(scope_json.get('annotator_id_hashed') or '')[:12]}"
    return "All exportable records"


# ─── Storage durability + reconciliation (PRD I-0 §F2/§F4) ───────────────────
# Reconciliation walks every case and task row and stats the whole blob tree, so
# it is far too heavy to run on each page load — and the answer changes only when
# blobs do. The boot run populates this; the page reads it; ``?refresh=1`` forces
# a fresh pass when an operator is actively investigating.
_RECONCILE_CACHE: Dict[str, Any] = {}
_RECONCILE_TTL_SEC = 900


@router.get("/storage/durability")
async def storage_durability(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Will this deployment survive its next redeploy? Three cheap syscalls.

    Split out of ``/storage/reconcile`` deliberately. That endpoint walks every
    case row and stats the whole blob tree, so it is cached for fifteen minutes
    and far too heavy to call on a page load — which meant the ONE question an
    operator needs answered constantly ("is my data on the volume?") was locked
    behind the one query that could not be asked constantly. This is three
    ``stat`` calls and a write probe, never cached, safe to call on every admin
    render.

    ``gate_armed`` is the part that outlives any single answer. With
    ``ENV=production`` the app REFUSES TO BOOT on non-durable storage, so the
    question stops needing to be asked. Without it, storage can silently become
    ephemeral again on any future variable change and nothing will stop the
    container from accepting data it is going to destroy — so an unarmed gate is
    reported as a problem even when today's verdict is green.
    """
    from asclepius import assets as asc_assets
    from asclepius import export as asc_export
    from asclepius.store import _db_storage_durable
    from http_security import is_production

    # ``severity`` separates the two questions an operator is really asking.
    # CRITICAL: this data cannot be recreated — losing it is losing the company's
    # record of work done and money owed. RECOVERABLE: an export bundle is a
    # rendering of records that are themselves permanent, so a lost one is
    # re-cut, not gone. Both are worth a banner; only one is worth panicking
    # about, and a screen that cannot tell them apart teaches an operator to
    # ignore both.
    stores = []
    for name, fn, severity in (
            ("Asclepius database", _db_storage_durable, "critical"),
            ("raw ingest", asc_ingestion.ingest_storage_durable, "critical"),
            ("asset store", asc_assets.asset_storage_durable, "critical"),
            ("export bundles", asc_export.export_storage_durable, "recoverable")):
        try:
            ok, why = fn()
        except Exception as exc:  # a check that cannot run is a failed check
            ok, why = False, f"durability check raised: {exc}"
        stores.append({"store": name, "durable": bool(ok), "detail": why,
                       "severity": severity})

    # The tenant database is the fourth store and is in none of the three checks
    # above — they cover the Asclepius plane only. It holds every onboarding in
    # flight, so leaving it out of the answer is how a green banner sits above a
    # signup funnel being erased on every deploy.
    try:
        from asclepius.constants import path_is_ephemeral, path_under_declared_volume
        from team_store import get_team_store

        team_db = os.getenv("TEAM_DB_PATH") or getattr(get_team_store(), "db_path", "")
        team_dir = os.path.dirname(os.path.abspath(team_db)) or "/"
        under = path_under_declared_volume(team_dir)
        if under is False:
            ok, why = False, (f"{team_db} is not under the volume this platform "
                              "mounted; set TEAM_DB_PATH into it")
        elif path_is_ephemeral(team_dir):
            ok, why = False, f"{team_db} is on ephemeral storage; set TEAM_DB_PATH"
        elif not (os.getenv("TEAM_DB_PATH") or "").strip():
            ok, why = False, (f"TEAM_DB_PATH is not set, so the tenant database "
                              f"lives beside the code at {team_db} and is replaced "
                              "on every redeploy")
        else:
            ok, why = True, f"tenant database durable ({team_db})"
        stores.append({"store": "tenant database", "durable": ok, "detail": why,
                       "severity": "critical"})
    except Exception as exc:  # noqa: BLE001 — never 500 the health banner
        stores.append({"store": "tenant database", "durable": False,
                       "severity": "critical",
                       "detail": f"durability check raised: {exc}"})

    all_durable = all(s["durable"] for s in stores)
    critical_durable = all(s["durable"] for s in stores
                           if s["severity"] == "critical")
    armed = is_production()
    return {
        "all_durable": all_durable,
        # The four stores whose loss is unrecoverable. This is the one the
        # fail-closed boot gate refuses to start over.
        "critical_durable": critical_durable,
        "gate_armed": armed,
        "volume_mount": os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "") or None,
        "stores": stores,
        # What to do, on the same payload as the diagnosis. An operator reading
        # "not durable" at 2am should not also have to find the runbook.
        "remedy": (
            None if (all_durable and armed) else
            ("Attach a volume mounted at /data, point TEAM_DB_PATH, "
             "ASCLEPIUS_DB_PATH, ASCLEPIUS_DATA_DIR and ASCLEPIUS_EXPORT_DIR "
             "into it, redeploy until every store reads durable, and only then "
             "set ENV=production so the app refuses to boot onto ephemeral "
             "storage. See docs/asclepius/IS_MY_DATA_SAFE.md.")),
    }


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
# ─── Health-system referrals (HS-REF) ─────────────────────────────────────────
class HsReferralAdvanceBody(BaseModel):
    status: str


@router.get("/hs-referrals", include_in_schema=False)
async def list_hs_referrals(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Every health-system introduction, newest first, with the referrer named.

    Admin-side, so unlike the physician's own funnel this DOES carry the contact
    details and the enrichment: working the lead is the whole job here, and the
    person doing it is proven to be an administrator by the dependency above.
    """
    store = get_store()
    out: List[Dict[str, Any]] = []
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hs_referrals ORDER BY invited_at DESC LIMIT 500").fetchall()
    for r in rows:
        row = dict(r)
        referrer = store.get_user_by_id(row.get("referrer_id") or "") or {}
        row["referrer_name"] = (referrer.get("full_name") or "").strip()
        row["referrer_email"] = (referrer.get("email") or "").strip()
        # The bearer token never leaves the server, not even for an admin: it
        # would let anyone holding a screenshot read the contact's details off
        # the public prefill route.
        row.pop("landing_token", None)
        out.append(row)
    return {"referrals": out, "total": len(out)}


@router.post("/hs-referrals/{hs_referral_id}/advance", include_in_schema=False)
async def advance_hs_referral(
    hs_referral_id: str,
    body: HsReferralAdvanceBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Move an introduction along the funnel, met, signed.

    The earlier stages stamp themselves (the email sends, the page is opened,
    the form is submitted). These last two cannot: they happen in a meeting and
    in a contract, so a human records them. Forward-only, enforced in the store.
    """
    store = get_store()
    row = store.get_hs_referral(hs_referral_id)
    if not row:
        raise HTTPException(status_code=404, detail="No such introduction.")
    status = (body.status or "").strip()
    if status not in store.HS_REFERRAL_STAGES:
        raise HTTPException(
            status_code=422,
            detail="Unknown stage. One of: " + ", ".join(store.HS_REFERRAL_STAGES))
    store.advance_hs_referral(hs_referral_id, status)
    store.log_event(
        entity_type="user", entity_id=row["referrer_id"],
        event_type="hs_referral_advanced", actor=admin.get("email"),
        payload={"hs_referral_id": hs_referral_id, "status": status})
    return {"ok": True, "referral": store.get_hs_referral(hs_referral_id)}


class HsReferralRewardBody(BaseModel):
    amount_cents: int
    note: str = ""


@router.post("/hs-referrals/{hs_referral_id}/reward", include_in_schema=False)
async def reward_hs_referral(
    hs_referral_id: str,
    body: HsReferralRewardBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Pay a physician for an introduction that closed.

    **The amount is typed by a human, every time.** There is no rate, no
    percentage, and nothing that derives one, because there is nothing to
    derive it from: institutional terms are negotiated one deal at a time. That
    is the same reason the Referral tab prints no figure for this, and it is
    why this endpoint takes ``amount_cents`` rather than computing it.

    Idempotent through ``UNIQUE(kind, ref_id)`` on the ledger: a double-click,
    or two admins working the same deal, cannot pay the introduction twice.
    """
    store = get_store()
    row = store.get_hs_referral(hs_referral_id)
    if not row:
        raise HTTPException(status_code=404, detail="No such introduction.")
    amount = int(body.amount_cents or 0)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Enter an amount above zero.")

    existing = store.get_earning(kind=asc_payments.KIND_HS_REFERRAL, ref_id=hs_referral_id)
    if existing:
        return {"ok": True, "already": True, "earning": existing}

    now = datetime.now(timezone.utc).isoformat()
    earning = store.insert_earning(
        earning_id=f"earn-{uuid.uuid4().hex[:12]}",
        user_id=row["referrer_id"],
        kind=asc_payments.KIND_HS_REFERRAL,
        ref_id=hs_referral_id,
        amount_cents=amount,
        # Not a rate. Stamped equal to the amount so the ledger's shape holds
        # for a one-off with no schedule behind it.
        rate_cents=amount,
        status=asc_payments.APPROVED,
        accrued_at=now,
        resolved_at=now,
        note=(body.note or "").strip() or f"Health-system introduction: {row.get('hs_name')}",
    )
    if earning is None:  # lost the race; the other writer's row is the truth
        return {"ok": True, "already": True,
                "earning": store.get_earning(
                    kind=asc_payments.KIND_HS_REFERRAL, ref_id=hs_referral_id)}

    with store._conn() as conn:
        conn.execute(
            "UPDATE hs_referrals SET reward_state = ?, reward_earning_id = ? "
            "WHERE hs_referral_id = ?",
            ("paid", earning.get("earning_id"), hs_referral_id))
    store.log_event(
        entity_type="user", entity_id=row["referrer_id"],
        event_type="hs_referral_rewarded", actor=admin.get("email"),
        payload={"hs_referral_id": hs_referral_id, "amount_cents": amount})
    return {"ok": True, "already": False, "earning": earning}


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
    # The one place accounts are minted, shared with self-signup and with a
    # partner adding a colleague (asclepius/hs_provisioning.py). Which health
    # system row to use stays HERE, because this door reuses by name and the
    # public one must never.
    minted = asc_hs_provisioning.provision_account(
        store, hs_id=hs["hs_id"], org_name=name, email=str(body.email))
    username = minted["username"]
    passphrase = minted["passphrase"]
    action = minted["action"]
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


class IngestFixturesRequest(BaseModel):
    """Which committed bundles to send through the door. Empty = all of them."""

    bundles: Optional[List[str]] = None


@router.post("/fixtures/ingest-patient-records", include_in_schema=False)
async def ingest_committed_patient_records(
    body: IngestFixturesRequest, background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Send the committed de-identified patient records through the real ingest
    door (Longitudinal E2E PRD §2.1).

    This is the missing FRONT DOOR, and it is deliberately not a shortcut. The
    four charts the V4 static cases were written from had never been uploaded, so
    ``ingest_cases`` for them did not exist and ``generate`` — the only code path
    that creates trajectories — had nothing to run on. The fix is to send them the
    way a hospital sends one: mint an authorizing link, store the encrypted bytes,
    insert the upload row, unpack in the background. **Not** a helper that inserts
    trajectory rows from ``v4_cases.py``, because then the next hospital's upload
    would still not work.

    They land in Box 1 with purpose unset, exactly like a partner's bundle, and an
    admin decides what they are for. Idempotent on the bundle sha256: click it
    twice and the second call reports "already ingested" per bundle.
    """
    from asclepius import patient_fixtures as asc_fixtures

    store = _store()
    # Same fail-closed posture as ``POST /partner/uploads``: in production we
    # refuse to accept a raw chart we cannot encrypt at rest or cannot keep. This
    # door writes the same blobs to the same place, so it must refuse on the same
    # conditions — a second entrance with weaker preconditions is how the unsafe
    # path gets built by accident.
    if (os.getenv("ENV") or "").strip().lower() == "production":
        import field_crypto
        if not field_crypto.is_configured():
            raise HTTPException(
                status_code=503,
                detail="Ingestion is disabled: DATA_ENCRYPTION_KEY is not configured, "
                       "so the bundle cannot be encrypted at rest.")
        ok, why = asc_ingestion.ingest_storage_durable()
        if not ok:
            raise HTTPException(status_code=503, detail=f"Ingestion is disabled: {why}")

    try:
        result = asc_fixtures.ingest_committed_bundles(
            store, actor=admin["id"], bundles=body.bundles,
            on_ingested=background.add_task)
    except Exception as exc:  # pragma: no cover — surfaced, never swallowed
        log.exception("committed-fixture ingest failed")
        raise HTTPException(status_code=500, detail=f"Fixture ingest failed: {exc}")

    store.log_event(entity_type="ingest_upload", entity_id="fixtures",
                    event_type="committed_fixtures_ingested", actor=admin["id"],
                    payload={k: result[k] for k in ("ingested", "skipped", "failed")})
    n_new, n_old, n_bad = result["ingested"], result["skipped"], result["failed"]
    if not result["available"]:
        message = (f"No committed bundles found under {result['root']}. Each bundle is "
                   "a directory of the partner's own files; add one and click again.")
    else:
        message = (f"{n_new} bundle(s) sent through the ingest door"
                   + (f" · {n_old} already ingested" if n_old else "")
                   + (f" · {n_bad} failed" if n_bad else "")
                   + ". Parsing runs in the background; the rows appear in Incoming "
                     "data as their cases land.")
    return {**result, "message": message}


@router.post("/uploads/{upload_id}/purpose", include_in_schema=False)
async def set_upload_purpose(
    upload_id: str, body: UploadPurposeRequest, background: BackgroundTasks,
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
    # §3 — this may have been the last of the three conditions. The trigger is
    # evaluated atomically inside the claim, so calling it here is safe whether or
    # not the mode is set yet.
    auto = asc_auto_generate.maybe_start(store, upload_id, actor=admin["id"],
                                         schedule=background.add_task)
    return {"ok": True, "upload_id": upload_id, "purpose": purpose,
            "cases_updated": cases, "auto_generate": auto,
            "message": f"{cases} case{'' if cases == 1 else 's'} recorded as "
                       f"{purpose.replace('_', ' ')}."
                       + (" Task creation is running now — no click needed."
                          if auto.get("started") else "")}


class UploadTaskModeRequest(BaseModel):
    """How an upload's cases become tasks (PRD ADMIN-TASKS §3.2)."""

    task_mode: Optional[str] = None


@router.post("/uploads/{upload_id}/task-mode", include_in_schema=False)
async def set_upload_task_mode(
    upload_id: str, body: UploadTaskModeRequest, background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Record static vs longitudinal for this upload, before any task is made.

    The choice was previously a boolean in the body of a generate call, which
    meant it existed only for the duration of one request: come back tomorrow to a
    half-finished bundle and nothing on the screen could tell you which kind of
    task the first half became. Storing it on the upload is what makes the §3.2
    row self-describing and lets a resumed batch continue in the same mode.

    Freely changeable while it still means something, and refused once it does
    not: mode is a property of the tasks that come out, so flipping it after the
    first task exists would describe rows that were built the other way. The
    remaining cases can still be promoted — as the mode they were staged under.

    This writes NO task and promotes nothing. It records an intention.
    """
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    # A brokering upload has no task mode, because it will never become tasks.
    # Accepting one here would put a static/longitudinal choice on a row whose
    # every promote path 409s — a control that does nothing, which reads as the
    # product being broken rather than as the rule it actually is.
    if asc_ingestion.is_brokering(upload.get("purpose")):
        raise HTTPException(
            status_code=409,
            detail="This upload came in on a brokering link, so it never becomes "
                   "tasks and has no task mode.")
    mode = (body.task_mode or "").strip().lower() or None
    if mode is not None and mode not in asc_store_mod.TASK_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"task_mode must be one of {', '.join(asc_store_mod.TASK_MODES)}.")
    counts = store.upload_task_counts(upload_id)
    if counts["promoted"] and mode != (upload.get("task_mode") or None):
        raise HTTPException(
            status_code=409,
            detail=f"{counts['promoted']} case(s) from this upload are already "
                   f"tasks, built as "
                   f"{upload.get('task_mode') or 'static'}. Changing the mode now "
                   f"would describe them as something they are not — promote the "
                   f"rest in the same mode, or send a new upload.")
    store.set_upload_task_mode(upload_id, mode)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="task_mode_set", actor=admin["id"],
                    payload={"task_mode": mode})
    auto = asc_auto_generate.maybe_start(store, upload_id, actor=admin["id"],
                                         schedule=background.add_task)
    return {"ok": True, "upload_id": upload_id, "task_mode": mode,
            "auto_generate": auto}


class AutoGenerateRequest(BaseModel):
    """Arm or disarm unattended task creation (Longitudinal E2E PRD §3)."""

    enabled: bool


@router.post("/uploads/{upload_id}/auto-generate", include_in_schema=False)
async def set_upload_auto_generate(
    upload_id: str, body: AutoGenerateRequest, background: BackgroundTasks,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Let this bundle build its tasks without a click, once it is fully described.

    Off by default, and that is the only safe default: a run costs a frontier
    difficulty probe, a candidate generation and two judges PER ENCOUNTER, and a
    25-point chart that started itself unasked is a bill nobody approved. Opting
    IN is a decision; opting out must never be one an admin had to remember.

    Arming does NOT change who can see the output. Points still land
    ``distribution='assigned_only'`` — this removes a click from BUILDING, never
    one from SENDING. Nothing reaches a physician until Task Routing sends it.
    """
    store = _store()
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")
    # Same rule as the task mode: a brokering bundle never becomes tasks, so a
    # control that does nothing would read as the product being broken rather
    # than as the rule it is.
    if asc_ingestion.is_brokering(upload.get("purpose")):
        raise HTTPException(
            status_code=409,
            detail="This upload came in on a brokering link, so it never becomes "
                   "tasks and cannot auto-generate.")
    if body.enabled and asc_auto_generate.has_run(upload):
        # Two different situations reach here, and telling an operator the wrong
        # one wastes their time. A run that FINISHED wrote a report; a run that
        # was claimed and never finished — the process was redeployed mid-flight,
        # which is an ordinary event — did not. The second reads as "nothing
        # happened and the button is refusing me", so it says so and names the
        # recovery instead of implying the work was done.
        #
        # Neither case re-arms. The claim is what stops a 25-encounter chart
        # being billed twice, and a heuristic that guessed "this one looks dead,
        # let it run again" would be exactly the thing that double-bills when it
        # guesses wrong. The manual path in Task creation is unaffected and does
        # the whole job.
        finished = bool((upload.get("auto_generate_report") or {}))
        raise HTTPException(
            status_code=409,
            detail=("This bundle has already had its automatic run. Promote what is "
                    "left from Task creation — re-arming would bill the whole chart "
                    "a second time."
                    if finished else
                    "This bundle's automatic run was started but never recorded an "
                    "outcome — usually a deploy while it was in flight. It will not "
                    "start again, because re-arming could bill the whole chart twice. "
                    "Promote the remaining cases from Task creation; nothing about "
                    "them is blocked."))
    store.set_upload_auto_generate(upload_id, body.enabled)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="auto_generate_set", actor=admin["id"],
                    payload={"enabled": bool(body.enabled)})
    auto = (asc_auto_generate.maybe_start(store, upload_id, actor=admin["id"],
                                          schedule=background.add_task)
            if body.enabled else {"started": False, "reason": "disarmed"})
    return {"ok": True, "upload_id": upload_id, "auto_generate": bool(body.enabled),
            "run": auto}


@router.post("/health-systems/{hs_id}/auto-generate", include_in_schema=False)
async def set_hs_auto_generate_default(
    hs_id: str, body: AutoGenerateRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The per-partner default, applied to their FUTURE uploads (§3).

    Not retroactive, deliberately. An upload already on the screen carries an
    ``auto_generate`` value an admin can see and change; rewriting it from here
    would arm a bundle whose own row says it is not armed.
    """
    store = _store()
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    store.set_health_system_auto_generate_default(hs_id, body.enabled)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="auto_generate_default_set", actor=admin["id"],
                    payload={"enabled": bool(body.enabled)})
    return {"ok": True, "hs_id": hs_id, "auto_generate_default": bool(body.enabled),
            "message": ("New uploads from this health system will build their tasks "
                        "automatically once a purpose and a mode are set. Uploads "
                        "already received are unchanged."
                        if body.enabled else
                        "New uploads from this health system will wait for a click.")}


class UploadDescriptionRequest(BaseModel):
    """Free text: what this bundle is (PRD ADMIN-TASKS §3.1)."""

    description: Optional[str] = None


@router.post("/uploads/{upload_id}/description", include_in_schema=False)
async def set_upload_description(
    upload_id: str, body: UploadDescriptionRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Let an admin write down what an upload IS when the sender did not say.

    Most bundles arrive through integrations that predate the description field,
    so without this the answer to "what am I looking at" would stay unavailable
    for exactly the uploads that already exist — the ones an operator most needs
    to triage."""
    store = _store()
    if not store.get_ingest_upload(upload_id):
        raise HTTPException(status_code=404, detail="Upload not found")
    store.set_upload_description(upload_id, body.description)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="description_set", actor=admin["id"])
    return {"ok": True, "upload_id": upload_id,
            "description": store.get_ingest_upload(upload_id).get("description")}


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


#: Row fields that only a physician account ever carries. Used to tell a real
#: doctor filed under an operator role apart from an actual operator.
_PHYSICIAN_MARKERS = ("specialty", "npi", "board_cert", "verification_status",
                      "tier", "clinical_role", "years_experience")


def _misfiled_physicians(store: Any) -> List[Dict[str, Any]]:
    """Accounts that carry physician credentials but are NOT filed as physicians.

    The roster above is ``role == 'evaluator'``, so an account whose row says
    ``role = 'admin'`` is not merely mislabelled — it is INVISIBLE. It cannot be
    approved, tiered or role-changed from the console, because the roster is how
    an operator reaches an account at all. The verification queue and the tier
    backfill filter the same way, so nothing else surfaces it either.

    That is not hypothetical: the self-serve director onboarding provisioned
    ``role="admin"`` until it was changed to ``"evaluator"``, and every account
    created before that fix still carries it. The code change did not repair the
    rows, and there was no screen on which the damage was visible — a doctor
    reports an empty queue, the roster does not list them, and the only remaining
    move is to read the database.

    Deliberately a SEPARATE list rather than a widened roster: these accounts are
    not supply until somebody decides they are, so they must not silently join
    the counts. They are shown so the decision can be made."""
    out: List[Dict[str, Any]] = []
    for u in store.list_users():
        if u.get("is_mock") or (u.get("role") or "") == "evaluator":
            continue
        if not any(u.get(k) for k in _PHYSICIAN_MARKERS):
            continue
        out.append({
            "id": u["id"],
            "name": _display_name(u),
            "email": u.get("email"),
            "role": u.get("role"),
            "specialty": u.get("specialty"),
            "tier": u.get("tier"),
            "verification_status": u.get("verification_status"),
            "real_data_approved": bool(u.get("real_data_approved")),
            "created_at": u.get("created_at"),
            # Why we think this is a doctor and not an operator — named, so the
            # decision is reviewable rather than a claim the screen makes.
            "physician_markers": [k for k in _PHYSICIAN_MARKERS if u.get(k)],
        })
    return out


#: The two verification states the Physicians console has a tab for. Anything
#: else is on NEITHER, which is the whole point of ``_unfiled_physicians``.
_TABBED_VERIFICATION = ("approved", "pending")


def _unfiled_physicians(store: Any) -> List[Dict[str, Any]]:
    """Physicians the console cannot show, because no tab claims their state.

    The roster tab is ``verification_status == 'approved'`` and the queue tab is
    ``status=pending`` plus mid-wizard signups. An evaluator whose verification
    was never decided — NULL — is therefore in NEITHER, and an account nobody can
    see is an account nobody can approve, tier, or route a real case to. It is
    the same invisibility ``_misfiled_physicians`` was written for, one column
    over: that one catches a doctor filed under an operator ROLE, and missed this
    because the role here is correct and it is the STATUS that has no home.

    It is not hypothetical. An account provisioned directly (the director
    onboarding mails an access key and creates a working evaluator) never enters
    the verification queue, so it logs in, draws synthetic cases and labels them
    perfectly — while being absent from the roster the operator is looking at.
    The physician sees a working product; the admin sees an empty screen; nothing
    errors anywhere.

    ``rejected`` is included deliberately. A decided-and-rejected account is also
    invisible today, and "we decided no" is a thing an operator should be able to
    see and reconsider — the row carries its status so the two cases are never
    confused.
    """
    out: List[Dict[str, Any]] = []
    for u in _physician_users(store):
        if (u.get("verification_status") or None) in _TABBED_VERIFICATION:
            continue
        out.append({
            "id": u["id"],
            "name": _display_name(u),
            "email": u.get("email"),
            "specialty": u.get("specialty"),
            "tier": u.get("tier"),
            "verification_status": u.get("verification_status"),
            "real_data_approved": bool(u.get("real_data_approved")),
            "active": bool(u.get("active", 1)),
            "created_at": u.get("created_at"),
            # Whether they have been WORKING while invisible. An operator
            # reading this card needs to know they are looking at a live
            # contributor, not a dormant row — a doctor who has labelled
            # thirty cases nobody can see is a different problem from an
            # account that was created and never used.
            "submissions_total": (
                store.evaluator_self_stats(u["id"]) or {}).get("submissions_total", 0),
        })
    return out


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
    score_by_user = store.contributor_scores_by_user()
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
            # Advisor is NOT a tier (capabilities.py:12 — the tier is retired and
            # rows carrying it migrate to reviewer on boot). It lives on
            # ``users.advisor_since``, and without it here the roster rendered
            # "Unassigned" over a real medical advisor: quiet, wrong, and only
            # discovered when the person tells you.
            "is_advisor": bool(u.get("advisor_since")),
            "advisor_since": u.get("advisor_since"),
            "verification_status": verification,
            # Real-data approval (EHR PRD §9.5). Gates the ENTIRE V4 real
            # de-identified queue, so a roster that omits it cannot answer the
            # first question an operator asks when the real cases are not being
            # labelled: "is anyone actually cleared to see them?"
            "real_data_approved": bool(u.get("real_data_approved")),
            "slack_joined": _tri_state(u.get("slack_joined")),
            "compensation_model": u.get("compensation_model"),
            "health_system_id": hs_id,
            "health_system_name": hs_names.get(hs_id) if hs_id else None,
            "active": bool(u.get("active", 1)),
            # The running contributor score. Read from the stored row, not
            # recomputed: this is a roster of everyone and ``compute`` is a
            # query per submission. None means nobody has graded them yet, and
            # the roster renders an em dash rather than a zero, because a zero
            # here reads as a physician who does bad work rather than one whose
            # first case is still in the queue.
            "contributor_score": score_by_user.get(u["id"]),
        })
    # Accounts with a doctor's credentials and an operator's role. Not part of
    # ``physicians`` or ``counts`` — they are not supply until someone decides
    # they are — but never again invisible.
    misfiled = _misfiled_physicians(store)
    # Correctly filed as physicians, but in a verification state no tab renders.
    # Separate from ``misfiled`` because the repair is different: those need a
    # role change, these need a verification decision.
    unfiled = _unfiled_physicians(store)
    return {"physicians": out, "counts": counts,
            "misfiled_physicians": misfiled, "misfiled_count": len(misfiled),
            "unfiled_physicians": unfiled, "unfiled_count": len(unfiled)}


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
            # A face, for the person cross-checking this record against an NPPES
            # entry or a registry lookup page. Arguably more use to them than to
            # the physician: an admin is the one being asked "is this the same
            # doctor?" Absent unless they uploaded one.
            "avatar_url": (
                f"/api/asclepius/users/{u['id']}/avatar"
                f"?v={(u.get('avatar_asset_sha') or '')[:12]}"
                if (u.get("avatar_asset_sha") or "").strip() else None
            ),
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
    # Restoring a physician is not finished at the role. The boot tier backfill
    # skips operator-role accounts, so one moved back arrives with a NULL tier —
    # which fails the LABEL capability. They would sit on the roster looking
    # correct and be unable to draw a single case, with nothing on screen asking
    # for the second step. Same rule the migration uses, applied here.
    tier_assigned = (store.backfill_tier_on_role_restore(
        user_id, by=f"role_restore:{admin.get('email') or admin['id']}")
        if role == "evaluator" else None)
    # Real-data access follows APPROVED + LABELING, and both may have just become
    # true. Running the sync here means the repair lands now instead of at the
    # next deploy.
    if role == "evaluator":
        try:
            store.sync_real_data_approval()
        except Exception:  # never fail a role change on the follow-on policy
            log.exception("asclepius: real-data sync after role restore failed")
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="role_changed",
        actor=admin.get("email"),
        payload={"from": target.get("role"), "to": role,
                 "tier_assigned": tier_assigned})
    fresh = store.get_user_by_id(user_id) or {}
    return {"ok": True, "user_id": user_id, "role": (updated or {}).get("role"),
            "tier": fresh.get("tier"), "tier_assigned": tier_assigned,
            "real_data_approved": bool(fresh.get("real_data_approved"))}


class RestorePhysicianBody(BaseModel):
    """Deliberate, itemised repair. Nothing here defaults to on: each field is a
    decision an operator is making on the record, with their identity stamped."""
    model_config = ConfigDict(extra="forbid")
    approve_verification: bool = False
    tier: Optional[str] = None
    note: Optional[str] = None


@router.post("/physicians/restore")
async def restore_physician(
    body: RestorePhysicianBody,
    email: str = Query(..., description="The account to restore, by email."),
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Put one account back together as a labeling physician, in a single call.

    The repair is four writes across three screens — role, tier, verification,
    real-data approval — and an account filed under an operator role is on NONE
    of those screens, so the sequence could not be started from the console at
    all. Every step here is reachable through its own route; what this adds is
    that they can be done in one action on an account the UI cannot reach, and
    that the response says what each one did.

    Nothing is implicit. ``approve_verification`` is a credentialing decision and
    is off by default; when set, it is recorded as a human decision by the calling
    admin (``verified_by``), exactly as the queue would. Real-data approval is
    never set directly — it is left to the same APPROVED + LABELING policy that
    governs everyone else, so this endpoint cannot become a side door around it."""
    store = _store()
    target = store.get_user_by_email((email or "").strip().lower())
    if not target:
        raise HTTPException(status_code=404, detail=f"No account for {email!r}.")
    if target["id"] == admin["id"]:
        raise HTTPException(
            status_code=422,
            detail="You cannot convert your own account to a physician; you would "
                   "lose the admin access you are using. Ask the other admin.")
    if (target.get("role") or "") not in ("admin", "evaluator", "qa_reviewer"):
        raise HTTPException(
            status_code=422,
            detail=f"Role {target.get('role')!r} is provisioned by its own flow and "
                   "is not a physician account.")
    tier = (body.tier or "").strip().lower() or None
    if tier and tier not in asc_caps.TIERS:
        raise HTTPException(status_code=422,
                            detail=f"Tier must be one of {sorted(asc_caps.TIERS)}.")

    approving = bool(body.approve_verification
                     and target.get("verification_status") != "approved")
    # The tier is written ONLY by the approval decision — record_verification_decision
    # touches the tier columns on its approved branch and nowhere else. Naming a
    # tier without an approval to carry it therefore writes nothing, and routing it
    # through a re-stamp of the CURRENT status would also stamp verified_by on an
    # account nobody verified. Both were measured. Refuse instead: a tier on a
    # pending or rejected account grants no access anyway (the verification gate
    # denies them), so it would only report a decision that had not been made.
    if tier and not approving and target.get("verification_status") != "approved":
        raise HTTPException(
            status_code=422,
            detail=("A tier is part of the approval decision and cannot be set on an "
                    f"account whose verification is {target.get('verification_status')!r}. "
                    "Send approve_verification: true to decide both together."))

    before = {k: target.get(k) for k in
              ("role", "tier", "verification_status", "real_data_approved")}
    did: List[str] = []
    if (target.get("role") or "") != "evaluator":
        store.set_user_role(target["id"], "evaluator")
        did.append("role -> evaluator")
    if approving:
        # ``tier`` is written unconditionally on this branch, so passing None would
        # NULL OUT a tier somebody already decided — measured: an existing reviewer
        # came back a labeler, demoted by an operator doing the obvious thing.
        # Carry the current tier forward when the caller did not name one.
        keep = tier or target.get("tier")
        # Credentials BEFORE the decision, for the same reason the console does
        # it and on the same seam: Onboarding v2's wizard has no password step,
        # so approval is when an account becomes usable at all. Repairing a
        # misfiled physician and leaving them unable to sign in is the repair
        # not finishing, and recording the decision is what queues their mail.
        from asclepius.verification_agent import (  # noqa: PLC0415
            _issue_credentials_if_needed,
        )
        issued = await _issue_credentials_if_needed(store, target)
        if issued:
            did.append("temporary password issued")
        store.record_verification_decision(
            user_id=target["id"], status="approved",
            decided_by=admin.get("email") or admin["id"],
            tier=keep, note=body.note)
        did.append("verification -> approved")
        if keep and keep != target.get("tier"):
            did.append(f"tier -> {keep}")
    elif tier and target.get("tier") != tier:
        store.record_verification_decision(
            user_id=target["id"], status="approved",
            decided_by=admin.get("email") or admin["id"], tier=tier, note=body.note)
        did.append(f"tier -> {tier}")
    # The same default the boot migration would have applied had this account
    # been filed as a physician all along. Never overwrites a decided tier.
    if store.backfill_tier_on_role_restore(
            target["id"], by=f"restore:{admin.get('email') or admin['id']}"):
        did.append("tier -> labeler (default backfill)")
    # Derived, never set: APPROVED + LABELING is the policy, and this endpoint
    # must not be a way around it.
    store.sync_real_data_approval()

    after_row = store.get_user_by_id(target["id"]) or {}
    after = {k: after_row.get(k) for k in
             ("role", "tier", "verification_status", "real_data_approved")}
    after["real_data_approved"] = bool(after["real_data_approved"])
    store.log_event(
        entity_type="user", entity_id=target["id"], event_type="physician_restored",
        actor=admin.get("email"), payload={"before": before, "after": after,
                                           "changes": did, "note": body.note})
    return {"ok": True, "user_id": target["id"], "email": target.get("email"),
            "changes": did, "before": before, "after": after,
            # The outcome that was actually being chased. If this is false the
            # response above says which gate is still shut.
            "can_label_real_cases": bool(after_row.get("real_data_approved"))
            and asc_caps.can(after_row, asc_caps.LABEL)}


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
    if purpose == asc_ingestion.PURPOSE_STORAGE:
        # RESOLVED, and the distinction matters. On an ACCOUNT, storage is a
        # deliberate setting — everything this partner sends is held until read,
        # which is the design — so it is not a work item and does not want a
        # resolver beside it. The work item is the per-UPLOAD decision, and it
        # has its own bucket.
        return {"purpose": purpose, "label": "storage", "accent": "lime",
                "resolved": True}
    return {"purpose": None, "label": asc_ingestion.PURPOSE_UNSET_LABEL,
            "accent": "lime", "resolved": False}


def _bucket_uploads(store: Any, hs_id: str) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "needs_attention": [], "rejected": [], "needs_review": [],
        # Received and held, waiting on a person to say what it is for. Its own
        # bucket for the same reason brokering has one: it must never appear
        # next to a Promote button, because it cannot be promoted until the
        # decision is made, and a button that 409s teaches an operator to ignore
        # the workflow.
        "storage": [],
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
            # Whether THIS upload still needs a person to say what it is for.
            # Decided server-side so the UI never re-derives the policy: an
            # unreviewed upload wants the resolver beside it, a brokered one is
            # already decided, and a task-creation one is done.
            "needs_decision": bool(
                asc_ingestion.blocks_promotion(up.get("purpose"))
                and not asc_ingestion.is_brokering(up.get("purpose"))),
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
            # STORAGE REPLACES READY-TO-PROMOTE, and only that bucket. It is the
            # precise substitution: ready_to_promote is exactly the bucket whose
            # action is unavailable until somebody says what this is for, and
            # every other bucket keeps its meaning.
            #
            # Not a `continue` earlier in this loop, which is what the first cut
            # did and what got it wrong: an upload whose cases were promoted
            # under the old rules is HISTORY, and filing it as "awaiting your
            # decision" would ask an operator to decide something that has
            # already happened. Safety holds outrank it too — a flagged upload
            # belongs in needs_attention whatever its destination says.
            if asc_ingestion.is_storage(up.get("purpose")):
                buckets["storage"].append(entry)
            else:
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
                          # Raw here, unlike the portal's own responses. The
                          # queue needs the four states distinguishable; only
                          # the hospital-facing side gets partner words.
                          "approval_status": u.get("approval_status"),
                          "signup_source": u.get("signup_source"),
                          "full_name": u.get("full_name"),
                          "decision_reason": u.get("decision_reason"),
                          **_purpose_view(u.get("purpose"))}
                         for u in store.list_hs_portal_users(hs_id)],
        "physicians_linked": len(physicians),
        "uploads_total": len(uploads),
        "last_activity": uploads[0]["created_at"] if uploads else None,
        "buckets": _bucket_uploads(store, hs_id),
        "link_purpose_note": _link_purpose_note(),
        # What they told us about themselves, newest first, and what we have
        # paid them. Both empty for an organization we provisioned by hand.
        "intake": [{"submitted_at": r["submitted_at"], "answers": r["answers"]}
                   for r in store.list_hs_intake(hs_id)],
        "payouts": store.list_hs_payouts(hs_id),
        "payouts_summary": store.hs_payout_summary(hs_id),
        # ─ Onboarding ─
        "onboarding_state": hs_states.state_of(hs),
        "state_changed_at": hs.get("state_changed_at"),
        # THE FOUR ANSWERS VERBATIM, every submission, newest first. Both the
        # stored value and the words the partner actually saw: an operator
        # deciding whether a BAA is needed should read "We would need a BAA",
        # and a later query should filter on `needs_baa`.
        "applications": [_hs_application_admin_view(r)
                         for r in store.list_hs_applications(hs_id)],
        "agreements": [_hs_agreement_admin_view(r)
                       for r in store.list_signed_agreements(hs_id)],
        "invoices": store.list_hs_invoices(hs_id),
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
    """The one sentence explaining an unresolved destination on this page.

    Rewritten when self-signup started minting accounts with it unset ON
    PURPOSE. The old text said these were links from before the field became
    mandatory and that newly minted ones always carry a destination — which is
    now false for every health system that lets itself in, and told an operator
    the newest partner on the page was a leftover.
    """
    return ("A destination is mandatory when you mint an upload link, so a link "
            "always carries one. An ACCOUNT can still arrive without: a row from "
            "before the field existed, or a health system that signed itself up, "
            "where the choice is made per upload instead of once. Resolve those "
            "on the upload row before promoting.")


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
            # ─ Onboarding, for the state chip and the DLA chip ─
            # Raw state here, unlike the portal's own responses: the queue needs
            # the five states distinguishable, and only the hospital-facing side
            # gets partner words.
            "onboarding_state": hs_states.state_of(hs),
            "state_changed_at": hs.get("state_changed_at"),
            "application": _hs_application_summary(store, hs["hs_id"]),
            "agreement": _hs_agreement_chip(store, hs["hs_id"]),
            # Sandbox PRD §3.4: where the row came from. NULL for every live
            # row; 'production' (+ copied_at, source_hs_id) for a snapshot
            # copy in the sandbox; 'sandbox' for a system onboarded there.
            "origin": hs.get("origin"),
            "copied_at": hs.get("copied_at"),
            "source_hs_id": hs.get("source_hs_id"),
        })
    return {"health_systems": out}


# ═══ Admin Launch PRD §5.1 — invite a physician into Asclepius Community ══════
#
# "Slack" is our own community (store.py: the community IS our Slack). This
# mails a link into Asclepius Community at /community. It does not call
# Slack.com, and nothing here should ever start to.

_COMMUNITY_INVITE_TTL_DAYS = 14


def _community_base() -> str:
    """Where Asclepius Community is served.

    The BACKEND, not the landing app — ``/community`` is a route in ``main.py``.
    Mirrors ``asclepius_verify._portal_base``; deliberately NOT ``_landing_base``,
    which would mint a link that 404s for the physician while looking correct in
    the admin's confirmation."""
    base = (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")
    return base


def _hash_invite_token(raw: str) -> str:
    """Only the hash is stored — the raw token exists once, in the email. Same
    posture as the onboarding member token in ``team_store``."""
    import hashlib  # noqa: PLC0415
    return hashlib.sha256((raw or "").strip().encode("utf-8")).hexdigest()


def _build_community_invite_email(*, full_name: str, join_url: str) -> str:
    first = (full_name or "").strip().split(" ")[0] if full_name else ""
    greeting = f"Hi {html.escape(first)}," if first else "Hi,"
    url = html.escape(join_url)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'color:#1a1b1a;line-height:1.6">'
        f"<p>{greeting}</p>"
        "<p>You're invited into <strong>Asclepius Community</strong> — a private room "
        "for the physicians contributing to Asclepius. Every member is "
        "credential-verified. Discuss cases, shape how tasks get built, and tell us "
        "when something is wrong.</p>"
        f'<p><a href="{url}">{url}</a></p>'
        "<p style='color:#8b8d89;font-size:13px'>This link is personal and expires in "
        f"{_COMMUNITY_INVITE_TTL_DAYS} days.</p>"
        "<p style='color:#8b8d89;font-size:13px'>Colleague discussion only. Do not post "
        "patient-identifiable information.</p></div>"
    )


class CommunityInviteBody(BaseModel):
    user_id: str


@router.post("/community/invite")
async def invite_to_community(
    body: CommunityInviteBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Mail one approved physician their link into Asclepius Community.

    The 409 on an unapproved physician is the point of the endpoint, not a
    formality: this link opens a room of credential-verified peers, and a
    physician we have not verified must never be handed one.

    Idempotent on the physician: already joined returns 200 with
    ``already_joined`` and sends nothing, so a second click is free.
    """
    store = _store()
    user = store.get_user_by_id(body.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such physician.")
    if (user.get("verification_status") or "") != "approved":
        raise HTTPException(
            status_code=409,
            detail="Only an approved physician can be invited. The community is a "
                   "room of credential-verified peers — approve them first.")
    if user.get("slack_joined"):
        # Nothing sent, no token minted. The safe answer to "invite them again"
        # for someone already inside.
        return {"ok": True, "already_joined": True, "user_id": user["id"],
                "email": user.get("email"), "sent": False}

    if not is_email_transport_configured():
        raise HTTPException(status_code=503,
                            detail="Email is not configured (SendGrid or SMTP).")

    raw_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
                  + timedelta(days=_COMMUNITY_INVITE_TTL_DAYS)).isoformat()
    store.create_community_invite(
        user_id=user["id"], email=user["email"],
        token_hash=_hash_invite_token(raw_token),
        expires_at=expires_at, created_by=admin["email"])

    join_url = f"{_community_base()}/community/join/{raw_token}"
    # The SAME mailer as /admin/signups/resend. One email path, so a transport
    # change cannot fix one surface and silently miss the other.
    sent = await send_html_email(
        user["email"], "You're invited to Asclepius Community",
        _build_community_invite_email(
            full_name=(user.get("full_name") or "").strip(), join_url=join_url))
    if not sent:
        raise HTTPException(status_code=503,
                            detail="Could not send that email — nothing was sent. Try again.")
    store.log_event(entity_type="user", entity_id=user["id"],
                    event_type="community_invite_sent", actor=admin["email"],
                    payload={"email": user["email"], "expires_at": expires_at})
    return {"ok": True, "already_joined": False, "user_id": user["id"],
            "email": user["email"], "sent": True, "expires_at": expires_at,
            "invited_at": (store.latest_community_invite_for_user(user["id"]) or {})
            .get("created_at")}


# ═══════════════════════════════════════════════════════════════════════════════
# Assignment (PRD-ASSIGN) — proposing who does which case
# ═══════════════════════════════════════════════════════════════════════════════
class AllocateBody(BaseModel):
    """Which cases to allocate, and to whom.

    ``dry_run`` defaults to TRUE. Same shape as ingest promotion, and for the
    same reason: an admin should be able to iterate on an allocation before a
    physician is told to do anything.
    """

    task_ids: List[str] = Field(default_factory=list)
    labels_per_case: int = Field(2, ge=1, le=5)
    reviewers_per_case: int = Field(1, ge=0, le=3)
    max_share: float = Field(0.35, gt=0.0, le=1.0)
    dry_run: bool = True
    due_at: Optional[str] = None
    # Exclusivity is offered ONLY with an expiry. An exclusive assignment with
    # no timeout is a queue that wedges the moment somebody goes on holiday.
    exclusive_hours: Optional[int] = Field(None, ge=1, le=720)

    # ═══ PRD CASE-BATCHES §2.4 — explicit targeting ═══════════════════════════
    # The allocator picks physicians algorithmically, which is right when the
    # question is "spread this fairly". It has no answer for "send these three to
    # Dr. Faheem", which is the question the Batches screen asks. These three
    # fields are that answer, and they are mutually exclusive by validation rather
    # than by convention.
    #
    # ``user_ids``  — the admin's list IS the allocation; ``allocate()`` is bypassed.
    # ``specialty`` — resolved to approved doctors of that specialty AT SEND TIME,
    #                 so a doctor approved this morning is included tonight.
    # ``to_all``    — no assignments at all: the cases are flipped to
    #                 distribution='open' and enter the ordinary queue. For a
    #                 longitudinal walk this is a deliberate un-sealing, and the UI
    #                 says so before the admin commits.
    user_ids: Optional[List[str]] = None
    specialty: Optional[str] = None
    to_all: bool = False

    # ═══ PRD ADMIN-TASKS §4.3 — per-doctor role ══════════════════════════════
    # {user_id: 'label' | 'review'}. Sparse and optional: a name absent from this
    # map is a LABELER, which is what every explicit send meant before this field
    # existed, so an old client's payload keeps its exact meaning.
    #
    # ``assignments.role`` already carries both values and the review path already
    # reads it — the gap was that the explicit-send builder hardcoded 'label', so
    # the Batches screen could name a reviewer and silently assign them labeling.
    roles: Optional[Dict[str, str]] = None

    @model_validator(mode="after")
    def _roles_are_a_known_vocabulary(self):
        """Refuse an unknown role at the door.

        ``allocate`` and the review path both compare ``role`` against the exact
        strings 'label' and 'review'. A third value would write a row that no
        query matches: not an error, just an assignment that never appears in
        anyone's queue and cannot be explained by looking at the screen."""
        for uid, role in (self.roles or {}).items():
            if role not in ("label", "review"):
                raise ValueError(
                    f"role for {uid!r} must be 'label' or 'review', got {role!r}")
        return self

    @model_validator(mode="after")
    def _one_targeting_mode(self):
        chosen = [n for n, v in (("user_ids", self.user_ids),
                                 ("specialty", self.specialty),
                                 ("to_all", self.to_all)) if v]
        if len(chosen) > 1:
            raise ValueError(
                f"choose ONE targeting mode, got {chosen}. They mean different "
                f"things — a specialty send resolves its roster at send time, an "
                f"explicit list does not, and to_all writes no assignments at all — "
                f"so combining them would silently pick one.")
        return self


def _allocation_inputs(store: Any, task_ids: List[str]):
    """Build the allocator's pure inputs from the store.

    Every eligibility answer is READ from the module that owns that question:
    the capability layer for labeling, ``review.can_review`` for reviewing,
    ``tiering.domain_match`` for domain fit. A hard gate implemented twice is a
    hard gate that will disagree with itself.
    """
    from asclepius import allocation as asc_allocation
    from asclepius import capabilities as _caps
    from asclepius import review as _review
    from asclepius import tiering as _tiering

    cases = []
    for tid in task_ids:
        t = store.get_task(tid)
        if not t:
            continue
        cases.append(asc_allocation.Case(
            task_id=t["task_id"],
            specialty=(t.get("specialty") or ""),
            real_deid=(t.get("case_source") == "real_deid"),
            difficulty=(
                float(t["empirical_difficulty"])
                if t.get("difficulty_measured") and t.get("empirical_difficulty") is not None
                else {"easy": 0.2, "medium": 0.5, "hard": 0.8}.get(
                    (t.get("difficulty") or "").strip().lower())
            ),
        ))

    domains = {c.specialty for c in cases if c.specialty}
    domain = next(iter(domains)) if len(domains) == 1 else None
    scores = store.contributor_scores_by_user()
    loads = store.open_assignment_counts()

    physicians = []
    for u in store.list_users():
        if (u.get("role") or "") != "evaluator" or not u.get("active", 1):
            continue
        if u.get("is_mock"):
            continue
        if (u.get("verification_status") or "approved") not in ("approved",):
            continue
        match, _why = _tiering.domain_match(u, domain) if domain else (0.0, "mixed batch")
        physicians.append(asc_allocation.Physician(
            user_id=u["id"],
            can_label=_caps.can(u, _caps.LABEL),
            can_review=_review.can_review(u),
            domain_match=match,
            contributor_score=scores.get(u["id"]),
            real_data_approved=bool(u.get("real_data_approved")),
            open_assignments=loads.get(u["id"], 0),
        ))
    return cases, physicians, domain


# ═══ PRD CASE-BATCHES §2 — the Batches surface ═══════════════════════════════
@router.get("/batches")
async def admin_batches(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """The three case classes, counted. Level 1 of the Batches screen.

    The classes are not a new taxonomy — they are a grouping over discriminators
    every task row already carries (``trajectory_id``, ``case_source``), so nothing
    here can disagree with what the queue thinks a case is.
    """
    return _store().batch_overview()


@router.get("/batches/{batch}")
async def admin_batch_cases(
    batch: str, trajectory_id: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The case rows inside one batch, with routing status resolved per row."""
    try:
        rows = _store().batch_cases(batch=batch, trajectory_id=trajectory_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"batch": batch, "cases": rows, "count": len(rows)}


class RelaySendBody(BaseModel):
    """Send one whole trajectory as a care-team relay (§8.3)."""

    trajectory_id: str
    user_ids: List[str] = Field(default_factory=list)
    dry_run: bool = True
    #: Fixes the rotation so the mapping an admin was SHOWN is the one committed.
    #: Without it, preview and commit are two independent draws from the same
    #: distribution and the screen is a lie the admin cannot detect.
    seed: Optional[int] = None
    due_at: Optional[str] = None


@router.post("/batches/relay")
async def admin_send_relay(
    body: RelaySendBody, admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """One trajectory, N doctors, one point each, walked in sequence.

    A different product from the solo walk, not a variant of it. The solo walk
    captures one physician's evolving judgment about a patient; this captures how
    clinicians build on each other's reasoning — doctor k reads doctor k−1's
    committed assessment before writing their own, exactly like a real handoff.

    What this commits, atomically enough that a partial state is not servable:
    the rotation as assignment rows, ``walk_mode='relay'`` on every point, and
    ``distribution`` left at ``assigned_only``. Only doctor #0's point is
    serveable on send; everyone else's assignment exists and is held closed by the
    relay gate until the chart reaches them.
    """
    store = _store()
    points = store.trajectory_points(body.trajectory_id)
    if not points:
        raise HTTPException(status_code=404, detail="No such trajectory.")

    # Re-sending would write a second rotation over the first, and a doctor
    # already told "point 4 is yours" would lose it with nobody informed.
    if not body.dry_run and store.trajectory_is_sent(body.trajectory_id):
        raise HTTPException(status_code=409, detail={
            "error": "trajectory_already_sent",
            "message": ("This chart walk has already been sent. Re-sending would "
                        "overwrite the current assignments; revoke them first if "
                        "you mean to re-route it."),
            "trajectory_id": body.trajectory_id})

    people = _resolve_send_targets(
        store, AllocateBody(task_ids=[p["task_id"] for p in points],
                            user_ids=body.user_ids))
    if not people:
        raise HTTPException(status_code=400, detail="user_ids is required for a relay.")

    # ── the two shape rules, refused rather than silently accommodated ───────
    if len(people) > len(points):
        raise HTTPException(status_code=400, detail={
            "error": "too_many_doctors",
            "message": (f"{len(people)} doctors for {len(points)} decision points. "
                        f"A relay gives each doctor at least one point; somebody "
                        f"here would be told they are on a relay and never get a "
                        f"turn."),
            "n_points": len(points), "n_doctors": len(people)})
    if len(people) < 2:
        raise HTTPException(status_code=400, detail={
            "error": "relay_needs_two_doctors",
            "message": ("A relay with one doctor is a solo walk wearing a relay "
                        "label: every handoff would be that physician reading "
                        "their own note back, and the κ annex would claim "
                        "independent raters that do not exist. Send it as a solo "
                        "walk instead."),
            "n_doctors": len(people)})

    rotation = asc_trajectory.relay_rotation(
        len(points), [u["id"] for u in people], seed=body.seed)
    by_id = {u["id"]: u for u in people}
    mapping = [{
        "sequence_index": p.get("sequence_index"),
        "task_id": p["task_id"],
        "user_id": uid,
        "email": (by_id.get(uid) or {}).get("email"),
    } for p, uid in zip(points, rotation)]

    if body.dry_run:
        return {"dry_run": True, "trajectory_id": body.trajectory_id,
                "n_points": len(points), "n_doctors": len(people),
                "seed": body.seed, "mapping": mapping,
                "notes": ["Only the first point is serveable on send; each later "
                          "point unlocks when the one before it is submitted."]}

    committed = []
    for row in mapping:
        committed.append(store.upsert_assignment(
            task_id=row["task_id"], user_id=row["user_id"], role="label",
            assigned_by=admin["email"], due_at=body.due_at)["assignment_id"])
    store.set_walk_mode([p["task_id"] for p in points],
                        asc_trajectory.WALK_MODE_RELAY)
    # distribution stays 'assigned_only': a relay is the opposite of an open queue.
    store.log_event(
        entity_type="assignment", event_type="relay_sent", actor=admin["email"],
        payload={"trajectory_id": body.trajectory_id, "n_points": len(points),
                 "n_doctors": len(people), "seed": body.seed})
    notified = asc_route_notify.notify_relay_send(
        store, mapping=mapping, trajectory_id=body.trajectory_id)
    return {"dry_run": False, "trajectory_id": body.trajectory_id,
            "n_points": len(points), "n_doctors": len(people),
            "mapping": mapping, "committed": committed, "notified": notified}


@router.get("/batches/relay/{trajectory_id}")
async def admin_trajectory_chain(
    trajectory_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The walk as a chain: who has each point, who the chart is waiting on (§8.7).

    Served for SOLO walks too, deliberately. At max_labels=1 a solo walk that its
    physician abandons is unrecoverable by anyone else — nobody can satisfy the
    sequence gate for the remaining points — so it is dead stock, and before this
    it was dead stock invisible as a problem anywhere in admin.
    """
    chain = _store().trajectory_chain(trajectory_id)
    if not chain.get("points"):
        raise HTTPException(status_code=404, detail="No such trajectory.")
    return chain


class ReassignPointBody(BaseModel):
    task_id: str
    user_id: str


@router.post("/batches/relay/{trajectory_id}/reassign")
async def admin_reassign_point(
    trajectory_id: str, body: ReassignPointBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Hand one stuck point to somebody else (§8.7).

    Revokes the current holder's assignment, writes the new one, and tells the new
    doctor. The nudge clock resets by construction: ``nudged_at`` lives on the
    assignment, so the new row starts unnudged and the replacement gets their own
    one reminder rather than inheriting a spent one.

    Recorded in the audit log as a reassignment because the export's provenance
    reads it: a relay walk with a substitution in the middle is a handoff chain a
    buyer should be able to see, not a detail that quietly disappears once the
    chart is complete.
    """
    store = _store()
    task = store.get_task(body.task_id)
    if not task or task.get("trajectory_id") != trajectory_id:
        raise HTTPException(status_code=404, detail="That point is not in this walk.")
    if store.submissions_for_task(body.task_id):
        raise HTTPException(status_code=409, detail={
            "error": "already_answered",
            "message": ("That point has already been answered. Reassigning it would "
                        "take finished work away from the physician who did it.")})

    people = _resolve_send_targets(
        store, AllocateBody(task_ids=[body.task_id], user_ids=[body.user_id]))
    new_doctor = (people or [None])[0]
    if not new_doctor:
        raise HTTPException(status_code=404, detail="Unknown user_id.")

    revoked = []
    for a in store.assignments_for_task(body.task_id):
        if a.get("role") == "label" and a.get("status") in ("offered", "claimed"):
            if a.get("user_id") == body.user_id:
                continue                     # already theirs; nothing to revoke
            store.set_assignment_status(a["assignment_id"], "revoked")
            revoked.append(a["assignment_id"])
    row = store.upsert_assignment(
        task_id=body.task_id, user_id=body.user_id, role="label",
        assigned_by=admin["email"])
    store.log_event(
        entity_type="assignment", event_type="relay_point_reassigned",
        actor=admin["email"],
        payload={"trajectory_id": trajectory_id, "task_id": body.task_id,
                 "sequence_index": task.get("sequence_index"),
                 "to_user_id": body.user_id, "revoked": revoked})
    notified = asc_route_notify.notify_reassigned(
        store, task=task, doctor=new_doctor)
    return {"trajectory_id": trajectory_id, "task_id": body.task_id,
            "assignment_id": row["assignment_id"], "revoked": revoked,
            "notified": notified, "chain": store.trajectory_chain(trajectory_id)}


class ResolveSelectionBody(BaseModel):
    task_ids: List[str] = Field(default_factory=list)


@router.post("/batches/resolve-selection")
async def admin_resolve_selection(
    body: ResolveSelectionBody, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Complete a selection with the earlier chart-walk points it implies.

    This exists so the CLIENT NEVER COMPARES SEQUENCE INDICES. The admin screen
    wants to tell an operator "3 selected, +5 earlier points included" before they
    commit, and the obvious way to get that number is a loop over sequence_index
    in JavaScript — which is precisely what this product does not allow anywhere,
    for a reason that outlives this screen: a client that knows how to order a walk
    is a client someone will later trust to enforce the order, and a test asserts
    the shipped client contains no such comparison.

    So the arithmetic stays server-side and the screen asks for the answer. One
    cheap call on selection change, no ordering logic in the browser, and the same
    function (``missing_trajectory_predecessors``) that ``allocate`` refuses with —
    so the preview count and the commit can never disagree about what is required.
    """
    store = _store()
    chosen = [t for t in (body.task_ids or []) if t]
    gaps = store.missing_trajectory_predecessors(chosen)
    implied = sorted({e["task_id"] for gap in gaps.values() for e in gap})
    return {
        "selected": chosen,
        "implied": implied,
        "task_ids": chosen + [t for t in implied if t not in set(chosen)],
        "n_added": len(implied),
    }


@router.get("/batches/preview/{task_id}")
async def admin_batch_preview(
    task_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """What the doctor will see — built by the doctor's own payload function.

    §2.3. The load-bearing decision is that this calls ``_blind_task``, the exact
    function the serve path calls, rather than assembling its own view of the case.
    A preview that reimplemented the payload would be a SECOND definition of "what
    a physician may see", and the first time the two drifted the admin screen would
    show a future the portal correctly hides — which is not a cosmetic difference.
    A screenshot of encounter 6 pasted into a Slack thread leaks the answer to
    decision 5 exactly as thoroughly as serving it would, and just as permanently.

    For a longitudinal point the truncation is already baked into the stored case
    (``build_encounter_case`` writes the visible window, not the whole chart), so
    reusing the serve payload inherits it for free. A test asserts the preview
    carries no offset past the decision point, because "inherits it for free" is
    the kind of claim that stops being true after an innocent refactor.
    """
    from routers.asclepius import _blind_task

    store = _store()
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    out = {
        "task": _blind_task(task),
        "prompt": task.get("prompt"),
        "eyebrow": "PREVIEW — read-only · exactly what the physician sees at labeling",
        "trajectory_id": task.get("trajectory_id"),
        "sequence_index": task.get("sequence_index"),
        "distribution": task.get("distribution") or "open",
    }
    if task.get("trajectory_id"):
        pts = store.trajectory_points(task["trajectory_id"])
        out["trajectory"] = {
            "n_points": len(pts),
            "position": (task.get("sequence_index") or 0) + 1,
        }
    return out


# ═══ PRD CASE-BATCHES §2.4 — resolving "who" ═════════════════════════════════
def _resolve_send_targets(store: Any, body: "AllocateBody") -> Optional[List[Dict[str, Any]]]:
    """The doctors an explicit send names, or ``None`` to let the allocator decide.

    ``to_all`` never reaches here — see the caller. Sending to everyone writes no
    assignment rows at all: it flips the cases to the open queue, where "everyone
    eligible" is already what the queue means. Manufacturing an assignment per
    doctor would be a second, worse spelling of the same fact, and would put
    hundreds of rows in a table whose purpose is to record who was singled out.
    """
    if not (body.user_ids or body.specialty):
        return None

    if body.user_ids:
        found = {u["id"]: u for u in (store.get_user_by_id(uid) for uid in body.user_ids) if u}
        missing = [uid for uid in body.user_ids if uid not in found]
        if missing:
            raise HTTPException(status_code=404, detail={
                "error": "unknown_user_ids", "user_ids": missing})
        people = [found[uid] for uid in body.user_ids]
    else:
        want = (body.specialty or "").strip().lower()
        people = [u for u in _physician_users(store)
                  if (u.get("specialty") or "").strip().lower() == want]
        if not people:
            raise HTTPException(status_code=400, detail={
                "error": "no_doctors_in_specialty",
                "message": f"No physician accounts are filed under {want!r}.",
                "specialty": want})

    # The V4 wall is not negotiable from here. An admin naming a doctor explicitly
    # is still not permission to show them real patient data — ``real_data_approved``
    # is the gate, and it is checked at the draw too, so an assignment written past
    # it would be an unservable row that looks like a routing bug. Refuse at send.
    blocked = [u for u in people if not u.get("real_data_approved")]
    if blocked:
        raise HTTPException(status_code=400, detail={
            "error": "not_approved_for_real_data",
            "message": ("These accounts are not approved for real de-identified "
                        "cases, so an assignment to them could never be served."),
            "user_ids": [u["id"] for u in blocked],
            "emails": [u.get("email") for u in blocked]})

    # ═══ PRD ADMIN-TASKS §4.3 — the same rule, for the review role ═══════════
    # Naming a labeler as a REVIEWER writes a row the review queue never returns:
    # ``review.can_review`` gates that surface on an explicit reviewer/advisor
    # tier, so the assignment would sit in a queue the doctor cannot open. That is
    # precisely the failure the V4 wall check above exists to prevent, one field
    # over, and it reads to everyone as the product being broken rather than as a
    # tier that was never granted. Refuse at send, naming who and why.
    from asclepius import review as _review

    wrong_tier = [u for u in people
                  if (body.roles or {}).get(u["id"]) == "review"
                  and not _review.can_review(u)]
    if wrong_tier:
        raise HTTPException(status_code=400, detail={
            "error": "not_a_reviewer",
            "message": ("These accounts do not carry the reviewer tier, so a "
                        "review assignment to them could never be served. Grant "
                        "the tier on Physicians, or send them the case to label."),
            "user_ids": [u["id"] for u in wrong_tier],
            "emails": [u.get("email") for u in wrong_tier]})
    return people


def _explicit_proposal(cases: List[Dict[str, Any]], people: List[Dict[str, Any]],
                       body: "AllocateBody") -> Any:
    """The admin's list, in the shape ``allocate()`` returns, so one commit path
    and one response shape serve both modes.

    ``labels_per_case`` still applies and is NOT silently ignored: three doctors at
    ``labels_per_case=2`` means the first two of them to open it get it, which the
    confirm dialog states in those words. Modelling it as an assignment to all
    three is correct — an assignment is a priority, and capacity is enforced at the
    draw by ``routing`` — so nobody is promised work that has already been taken.
    """
    from asclepius import allocation as asc_allocation

    # §4.3 — the admin's per-doctor role, defaulting to 'label' for anyone the map
    # does not name. That default is the compatibility contract: every explicit
    # send that predates this field meant "these people label these cases".
    roles = body.roles or {}
    assignments = [{"task_id": c.task_id, "user_id": u["id"],
                    "role": roles.get(u["id"], "label"),
                    "reason": ("named by admin as "
                               + ("reviewer" if roles.get(u["id"]) == "review"
                                  else "labeler"))}
                   for c in cases for u in people]
    # Same nested shape ``allocate()`` produces ({label, review, total} per user),
    # because the admin screen and the response contract read one of these without
    # knowing which mode produced it.
    per_physician: Dict[str, Dict[str, int]] = {}
    for a in assignments:
        c = per_physician.setdefault(a["user_id"], {"label": 0, "review": 0, "total": 0})
        c[a["role"]] += 1
        c["total"] += 1
    notes = []
    # Counted over LABELERS only. ``labels_per_case`` bounds labeling, and a note
    # that counted a named reviewer toward it would warn about contention that
    # does not exist — reviewers do not race labelers for a case.
    n_labelers = sum(1 for u in people if roles.get(u["id"], "label") == "label")
    if n_labelers > body.labels_per_case:
        notes.append(
            f"{n_labelers} doctors named for {body.labels_per_case} label(s) per "
            f"case: whoever opens a case first takes it, and the rest see it drop "
            f"out of their queue.")
    return asc_allocation.Proposal(
        assignments=assignments, unassigned=[], per_physician=per_physician,
        notes=notes)


@router.post("/assignments/allocate")
async def admin_allocate(
    body: AllocateBody, admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Propose an allocation, and commit it only when asked.

    THE POINT: the allocator proposes and an admin commits. The response is a
    table an operator reads and adjusts, and a case nothing could be proposed
    for comes back in ``unassigned`` WITH A REASON rather than quietly vanishing
    from the list.

    Committing writes ``assignments`` rows, which are a PRIORITY and not a
    permission: an assigned case sorts to the top of its assignee's queue and
    every other physician still sees it exactly where it was. Nothing about who
    MAY draw a case changes here.
    """
    from asclepius import allocation as asc_allocation

    if not body.task_ids:
        raise HTTPException(status_code=400, detail="task_ids is required.")
    store = _store()

    # ═══ PRD CASE-BATCHES §2.2 — the implied-predecessor rule ════════════════
    # Sending point 5 of a chart walk without points 0–4 STRANDS it. The sequence
    # gate refuses to serve 5 to a physician who has not completed the earlier
    # points, so the assignment lands in their queue permanently unservable — and
    # it reads to everyone as the product being broken rather than as a mis-click
    # in admin.
    #
    # The server re-derives the required set rather than trusting the payload's.
    # That is this branch's standing rule about ordering (the client contains no
    # sequence logic, and a test asserts it), and the reason it applies here too is
    # that the Batches screen is a client like any other: a stale tab, a replayed
    # request or a hand-rolled curl would otherwise write assignments that can
    # never be served. Refused BEFORE anything is written, naming the points, so an
    # admin can fix the selection rather than guess.
    gaps = store.missing_trajectory_predecessors(body.task_ids)
    if gaps:
        raise HTTPException(status_code=400, detail={
            "error": "missing_trajectory_predecessors",
            "message": ("A chart walk must be sent from its first unanswered point "
                        "onward. These selections are missing earlier points, which "
                        "the sequence gate would refuse to serve."),
            "missing": {tid: [e["sequence_index"] for e in gap]
                        for tid, gap in gaps.items()},
            "add_task_ids": sorted({e["task_id"] for gap in gaps.values() for e in gap}),
        })

    cases, physicians, domain = _allocation_inputs(store, body.task_ids)
    if not cases:
        raise HTTPException(status_code=404, detail="None of those task ids exist.")

    # ═══ PRD CASE-BATCHES §2.4 — three ways to choose who ════════════════════
    # Three modes, and the third is not a variant of the other two. "No targeting"
    # and "target everyone" are opposite instructions that would otherwise share a
    # branch: the first means "allocator, you pick", the second means "nobody is
    # picked, open the queue". Collapsing them ran the allocator on a send-to-all
    # and wrote the assignment rows it exists to avoid.
    targeted = None
    if body.to_all:
        proposal = asc_allocation.Proposal(
            assignments=[], unassigned=[], per_physician={},
            notes=["Sent to all: these cases enter the open queue and any eligible "
                   "physician may draw them. No assignments are written."])
    else:
        targeted = _resolve_send_targets(store, body)
        if targeted is None:
            # No explicit targeting: the allocator proposes, exactly as before.
            proposal = asc_allocation.allocate(
                cases, physicians,
                labels_per_case=body.labels_per_case,
                reviewers_per_case=body.reviewers_per_case,
                max_share=body.max_share,
            )
        else:
            proposal = _explicit_proposal(cases, targeted, body)

    committed = []
    notified: Dict[str, Any] = {"dms": 0, "channel": False, "errors": []}
    # What SEND does to visibility, which is separate from what it does to
    # priority. Sending to specific people or a specialty makes the cases
    # assigned_only — they are now those doctors' work and nobody else's. Sending
    # to All does the opposite and deliberately: the cases become 'open' and enter
    # the ordinary queue, which for a longitudinal walk is an un-sealing the UI
    # warns about before the admin commits. Computed here, applied below only on a
    # real commit, so ``dry_run`` can report it without doing it.
    flip_to = ("open" if body.to_all else
               ("assigned_only" if targeted is not None else None))
    if not body.dry_run:
        expires_at = None
        if body.exclusive_hours:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=int(body.exclusive_hours))
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for a in proposal.assignments:
            row = store.upsert_assignment(
                task_id=a["task_id"], user_id=a["user_id"], role=a["role"],
                assigned_by=admin["email"], due_at=body.due_at,
                exclusive=bool(body.exclusive_hours), expires_at=expires_at,
            )
            committed.append(row["assignment_id"])
        if flip_to:
            store.set_task_distribution([c.task_id for c in cases], flip_to)
        # ═══ PRD CASE-BATCHES §4 — tell the people it concerns ═══════════════
        # AFTER the assignment rows and the distribution flip, never before and
        # never inside them. The assignment is the truth and the ping is a
        # courtesy: a community outage must not roll back routing the queue is
        # already honouring, or the doctor ends up with neither the work nor the
        # message. ``notify_routed`` swallows its own failures and reports what
        # actually went out, so the audit line below records delivery rather than
        # an intention to deliver.
        notified = asc_route_notify.notify_routed(
            store, assignments=proposal.assignments, to_all=bool(body.to_all),
            due_at=body.due_at, task_ids=[c.task_id for c in cases])
        store.log_event(
            entity_type="assignment", event_type="assignments_committed",
            actor=admin["email"],
            payload={"n": len(committed), "cases": len(cases), "domain": domain,
                     "labels_per_case": body.labels_per_case,
                     "reviewers_per_case": body.reviewers_per_case,
                     "targeting": ("all" if body.to_all else
                                   "specialty" if body.specialty else
                                   "explicit" if body.user_ids else "allocator"),
                     "distribution": flip_to,
                     "notified": notified},
        )

    return {
        "dry_run": bool(body.dry_run),
        "targeting": ("all" if body.to_all else "specialty" if body.specialty
                      else "explicit" if body.user_ids else "allocator"),
        "distribution": flip_to,
        # What actually went out, not what was attempted — so an admin whose
        # community is down sees "0 DMs" on the screen instead of learning about
        # it when a physician says nobody told them.
        "notified": notified,
        "domain": domain,
        "cases": len(cases),
        "physicians_considered": len(physicians),
        "assignments": proposal.assignments,
        "unassigned": proposal.unassigned,
        "per_physician": proposal.per_physician,
        "notes": proposal.notes,
        "committed": committed,
    }


@router.get("/assignments")
async def admin_list_assignments(
    task_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    if task_id:
        return {"assignments": store.assignments_for_task(task_id)}
    if user_id:
        return {"assignments": store.assignments_for_user(user_id)}
    raise HTTPException(status_code=400, detail="task_id or user_id is required.")


@router.post("/assignments/{assignment_id}/revoke")
async def admin_revoke_assignment(
    assignment_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Take an assignment back. The case returns to the ordinary queue, where it
    always was: revoking removes a priority, not an access grant."""
    store = _store()
    if not store.set_assignment_status(assignment_id, "revoked"):
        raise HTTPException(status_code=404, detail="No such assignment.")
    store.log_event(
        entity_type="assignment", entity_id=assignment_id,
        event_type="assignment_revoked", actor=admin["email"],
    )
    return {"assignment_id": assignment_id, "status": "revoked"}


# ═══════════════════════════════════════════════════════════════════════════
#  SELF-SIGNUP REVIEW + HEALTH-SYSTEM PAYOUTS
#
#  The operator half of the portal's second door. Unlike the provider-facing
#  routes, these take hs_id and username in the path: that split is the design.
#  An admin acts ON a named health system; a health system only ever acts as
#  itself, which is why nothing under /hs/ takes an identifier at all.
# ═══════════════════════════════════════════════════════════════════════════


class HsApproveRequest(BaseModel):
    purpose: str


class HsRejectRequest(BaseModel):
    reason: str = ""


class HsPayoutRequest(BaseModel):
    amount_cents: int
    external_ref: str
    description: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None


class HsPayoutPaidRequest(BaseModel):
    payout_batch_id: Optional[str] = None


class HsPayoutVoidRequest(BaseModel):
    reason: str


def _hs_account_for(store: Any, hs_id: str, username: str) -> Dict[str, Any]:
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    matching = [u for u in store.list_hs_portal_users(hs_id)
                if u["username"].lower() == (username or "").lower()]
    if not matching:
        raise HTTPException(status_code=404,
                            detail="That portal account does not belong to this health system.")
    return matching[0]


# NOT "/health-systems/pending". GET /health-systems/{hs_id} is registered
# earlier in this file, and FastAPI matches in registration order, so that path
# resolves "pending" as an hs_id and 404s. A sibling noun avoids depending on
# where in the file somebody adds the next route.
@router.get("/health-system-signups", include_in_schema=False)
async def list_pending_health_systems(
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Self-signups waiting on a decision, oldest first.

    Each row carries what they told us and, when it applies, the name collision.
    That last field is the one an operator must not miss: signup deliberately
    refuses to merge by organization name, so a collision is either a second
    contact at a partner we already have, or somebody who does not work at the
    hospital whose name they typed. Only a person can tell those apart.
    """
    store = _store()
    out = []
    for account in store.list_hs_pending_signups():
        hs_id = account["hs_id"]
        collisions = store.health_systems_named_like(
            account.get("hs_name") or "", exclude_hs_id=hs_id)
        hs = store.get_health_system(hs_id) or {}
        out.append({
            "hs_id": hs_id,
            "organization": account.get("hs_name"),
            "username": account["username"],
            "full_name": account.get("full_name"),
            "email": account.get("email"),
            "created_at": account.get("created_at"),
            "signup_source": account.get("signup_source"),
            # Which decision this row actually needs. An organization carrying
            # an onboarding state is decided at the ORGANIZATION level -- one
            # Approve for everyone on it, then a signature -- while a row whose
            # state is NULL predates the state machine and still takes the
            # per-account decision it was built for. The queue renders whichever
            # applies rather than offering both and letting the operator guess.
            "onboarding_state": hs_states.state_of(hs),
            "org_level": hs.get("onboarding_state") is not None,
            "applications": [_hs_application_admin_view(r)
                             for r in store.list_hs_applications(hs_id)],
            "members": [{"username": u["username"], "email": u.get("email"),
                         "full_name": u.get("full_name")}
                        for u in store.list_hs_portal_users(hs_id)
                        if u.get("active")],
            "intake": [
                {"submitted_at": r["submitted_at"], "answers": r["answers"]}
                for r in store.list_hs_intake(hs_id)
            ],
            "name_collisions": [
                {"hs_id": c["hs_id"], "name": c["name"],
                 "uploads": len(store.list_uploads_for_health_system(c["hs_id"]))}
                for c in collisions
            ],
        })
    return {"pending": out}


@router.post("/health-systems/{hs_id}/accounts/{username}/approve",
             include_in_schema=False)
async def approve_health_system_account(
    hs_id: str, username: str, body: HsApproveRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Open the upload door for a self-signed-up health system.

    Takes a required destination for the same reason ``provision`` does: a
    self-signup arrives with it unset, which the admin view already renders as
    needing attention, and approval is the only moment anyone is looking. Doing
    it here means there is never a live account whose uploads have nowhere
    decided to go.
    """
    store = _store()
    purpose = (body.purpose or "").strip().lower()
    if purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")
    account = _hs_account_for(store, hs_id, username)
    store.set_hs_approval(account["username"], "approved", by=admin["email"])
    store.set_hs_portal_purpose(account["username"], purpose)
    hs = store.get_health_system(hs_id)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="portal_account_approved", actor=admin["email"],
                    payload={"username": account["username"], "purpose": purpose})

    to = (account.get("email") or "").strip()
    if to and is_email_transport_configured():
        from onboarding_emails import build_hs_approved_email
        await send_html_email(
            to, f"Uploading is open for {hs['name']}",
            build_hs_approved_email(organization=hs["name"], portal_url=_portal_url()))
    return {"ok": True, "username": account["username"], "approval_status": "approved"}


@router.post("/health-systems/{hs_id}/accounts/{username}/reject",
             include_in_schema=False)
async def reject_health_system_account(
    hs_id: str, username: str, body: HsRejectRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Turn a self-signup down and deactivate it.

    Sends NO email, deliberately. At this deal size a refusal is a conversation
    somebody has, and an automated "you were rejected" to a hospital CIO is a
    relationship we do not get back. The reason is recorded on the row and in
    the event log so whoever picks up the phone knows what was decided.
    """
    store = _store()
    account = _hs_account_for(store, hs_id, username)
    reason = (body.reason or "").strip() or None
    store.set_hs_approval(account["username"], "rejected", by=admin["email"], reason=reason)
    store.set_hs_portal_active(account["username"], False)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="portal_account_rejected", actor=admin["email"],
                    payload={"username": account["username"], "reason": reason})
    return {"ok": True, "username": account["username"], "approval_status": "rejected"}


@router.get("/health-systems/{hs_id}/payouts", include_in_schema=False)
async def list_health_system_payouts(
    hs_id: str, admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    return {"summary": store.hs_payout_summary(hs_id),
            "payouts": store.list_hs_payouts(hs_id)}


@router.post("/health-systems/{hs_id}/payouts", include_in_schema=False)
async def record_health_system_payout(
    hs_id: str, body: HsPayoutRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Record a payment against a health system.

    ``external_ref`` is yours: an invoice number, a transfer reference, anything
    stable. It is the idempotency key, so a double-clicked Record button records
    once. Nothing here moves money, and nothing here stores a bank detail.
    """
    store = _store()
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    try:
        row = store.record_hs_payout(
            hs_id=hs_id, amount_cents=int(body.amount_cents),
            external_ref=body.external_ref, recorded_by=admin["email"],
            description=body.description, period_start=body.period_start,
            period_end=body.period_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="A payout with that reference is already recorded for this health system.")
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="payout_recorded", actor=admin["email"],
                    payload={"payout_id": row["payout_id"],
                             "amount_cents": row["amount_cents"]})
    return row


@router.post("/health-systems/{hs_id}/payouts/{payout_id}/mark-paid",
             include_in_schema=False)
async def mark_health_system_payout_paid(
    hs_id: str, payout_id: str, body: HsPayoutPaidRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    existing = store.get_hs_payout(payout_id)
    if not existing or existing["hs_id"] != hs_id:
        raise HTTPException(status_code=404, detail="No such payout.")
    if existing["status"] == "void":
        raise HTTPException(status_code=409, detail="That payout was cancelled.")
    row = store.mark_hs_payout_paid(payout_id, batch_id=body.payout_batch_id,
                                    by=admin["email"])
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="payout_marked_paid", actor=admin["email"],
                    payload={"payout_id": payout_id})
    return row


@router.post("/health-systems/{hs_id}/payouts/{payout_id}/void",
             include_in_schema=False)
async def void_health_system_payout(
    hs_id: str, payout_id: str, body: HsPayoutVoidRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    existing = store.get_hs_payout(payout_id)
    if not existing or existing["hs_id"] != hs_id:
        raise HTTPException(status_code=404, detail="No such payout.")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Give a reason for cancelling this payout.")
    row = store.void_hs_payout(payout_id, reason=reason, by=admin["email"])
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="payout_voided", actor=admin["email"],
                    payload={"payout_id": payout_id, "reason": reason})
    return row


# ═══════════════════════════════════════════════════════════════════════════
#  HEALTH-SYSTEM ONBOARDING — the application, the decision, the agreement
#
#  The operator half of the state machine in asclepius/hs_states.py. Two
#  buttons on one card: Approve moves an organization to the signature, Decline
#  closes it with a reason on the row.
#
#  ORGANIZATION-LEVEL, unlike the older
#  /health-systems/{hs_id}/accounts/{username}/approve beside it. That endpoint
#  decides about one LOGIN and still exists for the partner it was built for;
#  this one decides about the ORGANIZATION, because the agreement binds the
#  organization and every member of it has to end up on the same side of that
#  decision.
# ═══════════════════════════════════════════════════════════════════════════

#: The words a partner saw, keyed by the value we stored. Duplicated from the
#: provider router's question list ON PURPOSE: that module is provider-reachable
#: and this one is not, and importing across that boundary to save eight lines
#: would be the first crack in a separation the whole isolation test suite rests
#: on. A test asserts the two stay in step.
_HS_ANSWER_WORDS: Dict[str, Dict[str, str]] = {
    "authority": {"yes": "Yes", "no": "No", "not_sure": "Not sure"},
    "deid_capability": {"in_our_environment": "De-identified in our environment",
                        "needs_baa": "We would need a BAA",
                        "not_sure": "Not sure"},
    "export_scope": {"notes_and_structured": "Notes and structured",
                     "structured_only": "Structured only",
                     "varies": "Depends by system"},
    "scale_patients": {"under_10k": "Under 10,000", "10k_50k": "10,000 to 50,000",
                       "50k_250k": "50,000 to 250,000",
                       "250k_1m": "250,000 to 1 million",
                       "over_1m": "Over 1 million", "not_sure": "Not sure"},
    "scale_years": {"under_2": "Under 2 years", "2_5": "2 to 5 years",
                    "5_10": "5 to 10 years", "10_20": "10 to 20 years",
                    "over_20": "Over 20 years", "not_sure": "Not sure"},
}

#: The label an operator reads on the card, per question, in the PRD's order.
_HS_ANSWER_TITLES: List[Tuple[str, str]] = [
    ("authority", "Authority to license"),
    ("deid_capability", "De-identification"),
    ("export_scope", "Export contents"),
    ("scale_patients", "Patients"),
    ("scale_years", "Years of history"),
]


def _hs_words(key: str, value: Optional[str]) -> str:
    return _HS_ANSWER_WORDS.get(key, {}).get((value or "").strip(), (value or ""))


def _hs_application_admin_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """One submission, as both stored values and the words they were chosen by."""
    answers = []
    for key, title in _HS_ANSWER_TITLES:
        answers.append({"key": key, "title": title,
                        "value": row.get(key) or "",
                        "words": _hs_words(key, row.get(key))})
    return {
        "application_id": row.get("application_id"),
        "submitted_at": row.get("submitted_at"),
        "username": row.get("username"),
        "answers": answers,
        "specialties": list(row.get("scale_specialties") or []),
        # The one answer an operator must not miss: it decides whether a byte
        # may move before a BAA exists.
        "needs_baa": (row.get("deid_capability") or "") == "needs_baa",
        "authority_unclear": (row.get("authority") or "") in ("no", "not_sure"),
    }


def _hs_application_summary(store: Any, hs_id: str) -> Optional[Dict[str, Any]]:
    """Just enough for a row in the list: when, and the two flags worth a chip."""
    row = store.latest_hs_application(hs_id)
    if not row:
        return None
    return {
        "submitted_at": row.get("submitted_at"),
        "needs_baa": (row.get("deid_capability") or "") == "needs_baa",
        "authority_unclear": (row.get("authority") or "") in ("no", "not_sure"),
    }


def _hs_agreement_admin_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """A signature row, whole. The admin side DOES get the network address and
    the client string -- they are the attribution leg of the E-SIGN record and
    the only reader who ever needs them is the one defending it."""
    return {
        "agreement_id": row.get("agreement_id"),
        "doc_version": row.get("doc_version"),
        "doc_sha256": row.get("doc_sha256"),
        "pdf_sha256": row.get("pdf_sha256"),
        "signer_user_id": row.get("signer_user_id"),
        "signer_email": row.get("signer_email"),
        "typed_name": row.get("typed_name"),
        "typed_title": row.get("typed_title"),
        "ip": row.get("ip"),
        "user_agent": row.get("user_agent"),
        "signed_at": row.get("signed_at"),
        "consent_esign": bool(row.get("consent_esign")),
        "authority_affirmed": bool(row.get("authority_affirmed")),
        "download_url": f"/api/asclepius/admin/agreements/{row.get('agreement_id')}/document",
    }


def _hs_agreement_chip(store: Any, hs_id: str) -> Optional[Dict[str, Any]]:
    row = store.latest_signed_agreement(hs_id)
    if not row:
        return None
    return {"doc_version": row.get("doc_version"),
            "signed_by": row.get("typed_name"),
            "signed_at": row.get("signed_at"),
            "agreement_id": row.get("agreement_id")}


class HsOrgApproveRequest(BaseModel):
    #: OPTIONAL, and it is the only field. Leaving it out is the DEFAULT and the
    #: PRD's instruction: a health-system account is minted with this unset so
    #: every upload it sends is resolved deliberately, one at a time, on the
    #: admin's own per-upload control. Supplying it here sets the account
    #: default for an organization whose answer is already settled.
    purpose: Optional[str] = None


class HsOrgDeclineRequest(BaseModel):
    #: REQUIRED. A refusal nobody wrote a reason for is a refusal nobody can
    #: explain when the hospital calls, and somebody always calls.
    reason: str


@router.post("/health-systems/{hs_id}/approve", include_in_schema=False)
async def approve_health_system(
    hs_id: str, body: HsOrgApproveRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Approve the ORGANIZATION and ask it for a signature.

    Three writes, in this order:
      1. every active account on the organization is approved, so the members
         who were provisional become full;
      2. the organization moves to `approved_awaiting_dla`, which is what opens
         the signing surface;
      3. every member is emailed the agreement request.

    Notification to all, signature by one (§0.1.2). The email goes to everybody
    because we cannot tell from here which of them has signing authority, and a
    letter that reaches only the person who happened to sign up is a letter that
    sits unread while the person who could sign never hears about it.
    """
    store = _store()
    hs = store.get_health_system(hs_id)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    current = hs_states.state_of(hs)
    try:
        hs_states.check_transition(current, hs_states.AWAITING_DLA)
    except hs_states.TransitionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    purpose = (body.purpose or "").strip().lower()
    if purpose and purpose not in asc_ingestion.PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of {', '.join(asc_ingestion.PURPOSES)}.")

    accounts = [u for u in store.list_hs_portal_users(hs_id) if u.get("active")]
    for account in accounts:
        # Only rows that were actually waiting. An account provisioned before
        # approval existed carries NULL and already reaches everything; stamping
        # it here would rewrite a decision nobody made.
        if (account.get("approval_status") or "").strip().lower() == "pending":
            store.set_hs_approval(account["username"], "approved", by=admin["email"])
        if purpose:
            store.set_hs_portal_purpose(account["username"], purpose)
    store.set_hs_onboarding_state(hs_id, hs_states.AWAITING_DLA)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="onboarding_approved", actor=admin["email"],
                    payload={"accounts": [a["username"] for a in accounts],
                             "purpose": purpose or None})

    notified = await _mail_dla_request(store, hs, accounts)
    return {"ok": True, "hs_id": hs_id,
            "onboarding_state": hs_states.AWAITING_DLA,
            "accounts_approved": len(accounts), "emailed": notified}


async def _mail_dla_request(store: Any, hs: Dict[str, Any],
                            accounts: List[Dict[str, Any]]) -> int:
    """One letter per member. Awaited rather than backgrounded: this is an admin
    route with no time budget, and the operator clicking Approve needs to know
    whether the thing that unblocks the deal actually went out."""
    if not is_email_transport_configured():
        return 0
    from onboarding_emails import build_hs_dla_request_email

    body = build_hs_dla_request_email(organization=hs["name"],
                                      portal_url=_portal_url())
    sent = 0
    for account in accounts:
        to = (account.get("email") or "").strip()
        if not to:
            continue
        ok = await send_html_email(
            to, "One signature away: your data licensing agreement", body,
            importance_headers=True)
        sent += 1 if ok else 0
    return sent


@router.post("/health-systems/{hs_id}/decline", include_in_schema=False)
async def decline_health_system(
    hs_id: str, body: HsOrgDeclineRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Turn an organization down, with the reason on the row.

    Sends NO email, deliberately, and for the reason the per-account rejection
    already gives: at this deal size a refusal is a conversation somebody has,
    and an automated "you were rejected" to a hospital CIO is a relationship we
    do not get back. The reason is recorded so whoever picks up the phone knows
    what was decided and why.
    """
    store = _store()
    hs = store.get_health_system(hs_id)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    reason = " ".join((body.reason or "").split())
    if not reason:
        raise HTTPException(status_code=400,
                            detail="A reason is required to decline.")
    try:
        hs_states.check_transition(hs_states.state_of(hs), hs_states.DECLINED)
    except hs_states.TransitionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    accounts = [u for u in store.list_hs_portal_users(hs_id) if u.get("active")]
    for account in accounts:
        store.set_hs_approval(account["username"], "rejected",
                              by=admin["email"], reason=reason)
        store.set_hs_portal_active(account["username"], False)
    store.set_hs_onboarding_state(hs_id, hs_states.DECLINED)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="onboarding_declined", actor=admin["email"],
                    payload={"reason": reason,
                             "accounts": [a["username"] for a in accounts]})
    return {"ok": True, "hs_id": hs_id, "onboarding_state": hs_states.DECLINED,
            "accounts_closed": len(accounts)}


@router.get("/agreements/{agreement_id}/document", include_in_schema=False)
async def download_signed_agreement(
    agreement_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The countersigned PDF, by agreement id.

    Reads the blob the signature row points at and re-verifies its hash on the
    way out. A contract that has silently rotted in storage must fail loudly
    here rather than be handed to counsel as if it were intact.
    """
    from fastapi.responses import Response as _RawResponse

    store = _store()
    row = store.get_signed_agreement(agreement_id)
    if not row or not row.get("pdf_sha256"):
        raise HTTPException(status_code=404, detail="No such signed agreement.")
    hs = store.get_health_system(row["hs_id"]) or {"name": "licensor"}
    rebuilt = False
    try:
        from asclepius import assets as asc_assets
        data, _mime = asc_assets.load_asset(str(row["pdf_sha256"]), verify=True)
    except Exception:
        # Same fallback the partner's own download takes, and for the same
        # reason: the row is the record and the document is reproducible from
        # it. The header below says which one this is, because handing counsel a
        # rebuild while letting them believe it is the stored artifact is the
        # one thing worse than the 503 this used to raise.
        log.exception("signed agreement blob is unreadable; rebuilding from the row")
        try:
            data = asc_dla.pdf_from_row(organization=hs.get("name") or "licensor",
                                        row=row)
            rebuilt = True
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"The stored PDF could not be read and could not be "
                       f"rebuilt ({exc}). The signature row is intact; the "
                       "blob is not.")
    filename = asc_dla.pdf_filename(organization=hs.get("name") or "licensor",
                                    version=str(row.get("doc_version") or ""))
    headers = {"content-disposition": f'attachment; filename="{filename}"'}
    if rebuilt:
        headers["x-agreement-source"] = "rebuilt-from-row"
    return _RawResponse(content=data, media_type="application/pdf", headers=headers)


# ─── Invoices (architecture now, Stripe later) ───────────────────────────────
#
# THE DISBURSEMENT SEAM, verbatim from the payments work: this module records
# what is owed and what an operator says has happened. It does not move money.
# No call to Stripe, no webhook, no key read, no balance queried. When the rail
# is wired, it is wired HERE, behind these three endpoints, and every caller
# above them is already written against the shape it will have.

class HsInvoiceRequest(BaseModel):
    period: str
    amount_cents: int
    description: Optional[str] = None


class HsInvoiceStatusRequest(BaseModel):
    status: str


@router.get("/health-systems/{hs_id}/invoices", include_in_schema=False)
async def list_health_system_invoices(
    hs_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    return {"invoices": store.list_hs_invoices(hs_id)}


@router.post("/health-systems/{hs_id}/invoices", include_in_schema=False)
async def create_health_system_invoice(
    hs_id: str, body: HsInvoiceRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Draft one invoice for one period. One per (organization, period).

    A repeat submission of the same period is refused rather than silently
    creating a second invoice -- the double-billing guard, the analogue of
    UNIQUE(hs_id, external_ref) on payouts."""
    store = _store()
    if not store.get_health_system(hs_id):
        raise HTTPException(status_code=404, detail="Health system not found")
    period = " ".join((body.period or "").split())
    if not period:
        raise HTTPException(status_code=400, detail="A period is required.")
    if int(body.amount_cents) <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    row = store.create_hs_invoice(hs_id=hs_id, period=period,
                                  amount_cents=int(body.amount_cents),
                                  created_by=admin["email"],
                                  description=(body.description or None))
    if row is None:
        raise HTTPException(status_code=409,
                            detail=f"An invoice already exists for {period}.")
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="invoice_created", actor=admin["email"],
                    payload={"invoice_id": row["invoice_id"], "period": period,
                             "amount_cents": row["amount_cents"]})
    return {"invoice": row}


@router.post("/health-systems/{hs_id}/invoices/{invoice_id}/status",
             include_in_schema=False)
async def set_health_system_invoice_status(
    hs_id: str, invoice_id: str, body: HsInvoiceStatusRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Move an invoice along. An OPERATOR statement of fact, not a payment.

    'paid' here means a person confirmed the money arrived. Nothing in this
    repository can observe that; when something can, this is the endpoint it
    calls, and the meaning of the column does not change."""
    store = _store()
    row = store.get_hs_invoice(invoice_id)
    if not row or row.get("hs_id") != hs_id:
        raise HTTPException(status_code=404, detail="No such invoice.")
    status = (body.status or "").strip().lower()
    if status not in ("draft", "sent", "paid"):
        raise HTTPException(status_code=400,
                            detail="status must be draft, sent or paid.")
    updated = store.set_hs_invoice_status(invoice_id, status)
    store.log_event(entity_type="health_system", entity_id=hs_id,
                    event_type="invoice_status_set", actor=admin["email"],
                    payload={"invoice_id": invoice_id, "status": status})
    return {"invoice": updated}
