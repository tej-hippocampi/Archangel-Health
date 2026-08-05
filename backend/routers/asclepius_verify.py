"""Admin verification queue (PRD B, Phase 5).

Every physician signup lands here as ``pending``; a human decides. The tier
score is advice rendered next to the row — approval always carries an EXPLICIT
tier in the request body, and rejection always carries a note. Every decision
stamps ``verified_by`` / ``verified_at`` and emits a provenance event.

Own router module by design (00_START_HERE §3.1): ``routers/asclepius.py`` is
never edited; main.py gains exactly one import and one mount line.
"""

from __future__ import annotations

import html as html_mod
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import credentialing
from asclepius.store import get_store
from email_utils import is_email_transport_configured, send_html_email

log = logging.getLogger("asclepius.verify")

router = APIRouter(prefix="/api/asclepius/verify", tags=["asclepius-verify"])

# The tier vocabulary is NOT written here. It is imported from the capability
# layer (Advisor PRD §2.2) so this router and every access gate can never
# disagree about what the ``users.tier`` column may hold — the Seam-1 failure
# mode (B validates one set of strings, A's gate compares against another).
_TIERS = asc_caps.TIERS
_QUEUE_STATUSES = ("pending", "approved", "rejected")


def _store():
    return get_store()


def _portal_base() -> str:
    base = (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")
    return base


def _family_name(user: Dict[str, Any]) -> str:
    creds = credentialing._json_field(user, "credentials_json")
    legal = str(creds.get("fullLegalName") or user.get("full_name") or "").strip()
    return legal.split()[-1] if legal else ""


def _duplicate_npi(store: Any, user: Dict[str, Any]) -> bool:
    npi = credentialing.clean_npi(user.get("npi") or "")
    if not npi:
        return False
    return len(store.find_users_by_npi(npi)) > 1


def _proposal(store: Any, user: Dict[str, Any],
              dupe_counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """B-5.8: when a caller already holds the grouped duplicate counts, use
    them instead of running one ``SELECT ... WHERE npi = ?`` per queue row."""
    if dupe_counts is None:
        dupe = _duplicate_npi(store, user)
    else:
        dupe = dupe_counts.get(credentialing.clean_npi(user.get("npi") or ""), 0) > 1
    return credentialing.propose_tier(user, duplicate_npi=dupe)


def _npi_summary(user: Dict[str, Any]) -> Dict[str, Any]:
    payload = credentialing._json_field(user, "npi_payload_json")
    record = payload.get("record") or {}
    attempt = credentialing._json_field(user, "npi_last_attempt_json")
    return {
        "npi": user.get("npi"),
        "result": payload.get("result"),         # verified|mismatch|not_found|unavailable|None
        "reason": payload.get("reason"),
        "taxonomy": (record.get("taxonomy") or {}).get("desc"),
        "registry_name": " ".join(
            p for p in [record.get("first_name"), record.get("last_name")] if p) or None,
        "credential": record.get("credential"),
        "checked_at": user.get("npi_checked_at"),
        # F6: a failed check no longer overwrites the result, so it is reported
        # alongside it — the admin must be able to see "we tried and could not
        # reach NPPES" without that attempt destroying the answer we hold.
        "last_attempt": attempt.get("reason") or attempt.get("result") or None,
        "last_attempt_at": user.get("npi_last_attempt_at"),
        "recheck_pending": bool(user.get("npi_checked_at") is None and user.get("npi")),
    }


def _queue_row(store: Any, user: Dict[str, Any],
               dupe_counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    prop = _proposal(store, user, dupe_counts)
    cv_parsed = credentialing._json_field(user, "cv_parsed_json")
    return {
        "user_id": user["id"],
        "email": user["email"],
        "full_name": user.get("full_name"),
        "specialty": user.get("specialty"),
        "clinical_role": user.get("clinical_role"),
        "org_name": user.get("org_name"),
        "created_at": user.get("created_at"),
        "verification_status": user.get("verification_status"),
        "email_domain_class": user.get("email_domain_class"),
        "phone": user.get("phone"),
        "linkedin_url": user.get("linkedin_url"),
        "has_cv": bool(user.get("cv_asset_sha")),
        "cv_ok": bool(cv_parsed.get("ok")),
        "npi": _npi_summary(user),
        "score": prop["score"],
        "proposed_tier": prop["proposed_tier"],
        "reasons": prop["reasons"],
        "blockers": prop["blockers"],
        "tier": user.get("tier"),
        "verified_by": user.get("verified_by"),
        "verified_at": user.get("verified_at"),
    }


def _load_user_or_404(user_id: str) -> Dict[str, Any]:
    user = _store().get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="No such user")
    return user


# ─── Queue ────────────────────────────────────────────────────────────────────
@router.get("/queue")
async def verification_queue(
    status: str = "pending",
    limit: int = 100,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The admin queue.

    B-5.8: paginated, and duplicate detection is ONE grouped query rather than
    a full-table scan per row. 'pending' self-limits (an admin works it down),
    but 'approved' only ever grows, so an unpaginated version degraded
    linearly forever.
    """
    if status not in _QUEUE_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {', '.join(_QUEUE_STATUSES)}")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    store = _store()
    all_rows = store.list_verification_queue(status)
    page = all_rows[offset:offset + limit]
    dupe_counts = store.npi_claim_counts()
    rows = [_queue_row(store, u, dupe_counts) for u in page]
    return {
        "status": status,
        "count": len(rows),
        "total": len(all_rows),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < len(all_rows),
        "queue": rows,
    }


@router.get("/queue/{user_id}")
async def verification_dossier(
    user_id: str,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    user = _load_user_or_404(user_id)
    row = _queue_row(store, user)
    # Full dossier extras: raw NPPES payload, parsed CV suggestions, duplicate
    # claimants, credential record. Never the password hash.
    dupes = [
        {"user_id": d["id"], "email": d["email"],
         "verification_status": d.get("verification_status")}
        for d in store.find_users_by_npi((user.get("npi") or "").strip())
        if d["id"] != user["id"]
    ] if (user.get("npi") or "").strip() else []
    row.update({
        "npi_payload": credentialing._json_field(user, "npi_payload_json"),
        "cv_parsed": credentialing._json_field(user, "cv_parsed_json"),
        "cv_asset_sha": user.get("cv_asset_sha"),
        "credentials": credentialing._json_field(user, "credentials_json"),
        "attestations": credentialing._json_field(user, "attestations_json"),
        "duplicate_claims": dupes,
        "verification_notes": user.get("verification_notes"),
        "years_experience": user.get("years_experience"),
        "board_cert": user.get("board_cert"),
    })
    return row


@router.get("/queue/{user_id}/cv")
async def verification_cv(
    user_id: str,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The raw CV file — the admin's ground truth when the parse is empty."""
    user = _load_user_or_404(user_id)
    sha = (user.get("cv_asset_sha") or "").strip()
    if not sha:
        raise HTTPException(status_code=404, detail="No CV on file")
    from asclepius import assets
    try:
        data, _ = assets.load_asset(sha)
    except Exception:
        raise HTTPException(status_code=404, detail="CV blob missing from asset store")
    mime = credentialing.sniff_cv_mime(data) or "application/octet-stream"
    ext = "pdf" if mime == "application/pdf" else "txt"
    return Response(content=data, media_type=mime, headers={
        "Content-Disposition": f'inline; filename="cv-{user_id}.{ext}"',
        # B-5.5: served inline from the app origin to an admin whose bearer
        # token is in localStorage. The sibling image endpoint already sets
        # this; without it the browser may sniff its way to an active type.
        # (store_cv now also sniffs on the way IN, so both ends agree.)
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
    })


# ─── Decisions ────────────────────────────────────────────────────────────────
class ApproveBody(BaseModel):
    tier: Optional[str] = None
    note: Optional[str] = None
    # Required ONLY when tier == 'advisor' (Advisor PRD §2.3). An advisor holds
    # equity, so an advisor with no signed agreement on file is a liability, and
    # a required field is the cheapest possible enforcement. The PRD states that
    # rule on the appointment endpoint; it is enforced here too because this is
    # the second door into the same tier, and a rule enforced at one of two
    # doors is not a rule.
    agreement_ref: Optional[str] = None


class RejectBody(BaseModel):
    note: Optional[str] = None


def _welcome_email_html(user: Dict[str, Any]) -> str:
    name = html_mod.escape((user.get("full_name") or "").strip() or "Doctor")
    url = html_mod.escape(_portal_base() + "/asclepius")
    return (
        f"<div style=\"font-family:Georgia,serif;max-width:560px;margin:0 auto;"
        f"padding:24px;color:#1a2b3c\">"
        f"<h2 style=\"margin:0 0 12px\">You&rsquo;re approved.</h2>"
        f"<p>{name}, our clinical team has verified your credentials and your "
        f"Asclepius account is now open for evaluation work.</p>"
        f"<p>Sign in with your email and your standing access key:</p>"
        f"<p><a href=\"{url}\" style=\"display:inline-block;background:#1a2b3c;"
        f"color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none\">"
        f"Open your workspace &rarr;</a></p>"
        f"<p>Your seat in the <strong>Asclepius Community</strong> is open too — a "
        f"private space for verified physicians only. Find it in your portal's side "
        f"panel: introduce yourself, follow the medical-AI digest, and meet the "
        f"colleagues you'll be working alongside.</p>"
        f"<p style=\"font-size:13px;color:#5a6b7c\">Questions? Just reply to this "
        f"email.</p></div>"
    )


@router.post("/queue/{user_id}/approve")
async def approve_signup(
    user_id: str,
    body: ApproveBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    tier = (body.tier or "").strip().lower()
    if tier not in _TIERS:
        # The admin is the decision; the score is advice. An approval that
        # leans on the proposal implicitly is exactly what this 400 prevents.
        # The list is rendered from _TIERS so the message can never go stale
        # against the values actually accepted — a stale error message is how
        # an operator learns the wrong vocabulary.
        allowed = ", ".join(repr(t) for t in _TIERS)
        raise HTTPException(
            status_code=400,
            detail=f"Approval requires an explicit tier: {allowed}.")
    agreement_ref = (body.agreement_ref or "").strip()
    if tier == asc_caps.ADVISOR and not agreement_ref:
        raise HTTPException(
            status_code=400,
            detail="Appointing an advisor requires agreement_ref — the signed "
                   "advisor agreement on file. An advisor holds equity; an "
                   "advisor with no agreement is a liability, not a shortcut.")
    store = _store()
    user = _load_user_or_404(user_id)
    prop = _proposal(store, user)
    if tier == asc_caps.ADVISOR:
        # The SAME single transactional write the appointment endpoint uses —
        # not "approve, then also set the advisor terms" (audit H1). Done as two
        # statements in this order, a crash in between left the account approved
        # and LIVE with advisory capability, no agreement on file and a NULL
        # compensation model. Two doors, one write.
        updated = store.appoint_advisor(
            user_id, agreement_ref=agreement_ref, appointed_by=admin["email"],
            approve=True,
            note=(body.note or f"Approved as medical advisor · agreement {agreement_ref}"),
        )
    else:
        updated = store.record_verification_decision(
            user_id,
            status="approved",
            decided_by=admin["email"],
            tier=tier,
            tier_score=float(prop["score"]),
            note=(body.note or None),
        )
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="verification_approved",
        actor=admin["email"],
        payload={"tier": tier, "score": prop["score"],
                 "proposed_tier": prop["proposed_tier"],
                 "followed_proposal": prop["proposed_tier"] == tier,
                 "agreement_ref": agreement_ref or None,
                 "note": body.note or None},
    )
    # Advisor PRD §3.2 step 4: a referred physician reaching 'approved' is the
    # end of their referrer's funnel. Best-effort — a referral bookkeeping
    # failure must never undo an approval that already committed.
    try:
        store.advance_referral_for_user(user_id, "approved")
    except Exception:
        log.exception("[referral] could not advance referral to approved (non-fatal)")
    # Community v2: approval is the "verified colleague" moment — post the
    # one-time #introductions welcome. Guarded + idempotent inside; a failure
    # can never fail the approval (mirrors the welcome-email guard below).
    try:
        from community.onboard import welcome_new_member  # noqa: PLC0415
        await welcome_new_member(updated or user)
    except Exception:
        log.exception("[verify] community welcome failed (decision stands)")
    if is_email_transport_configured():
        try:
            await send_html_email(
                user["email"], "You're approved — welcome to Asclepius",
                _welcome_email_html(user), importance_headers=True)
        except Exception:
            log.exception("[verify] welcome email failed (decision stands)")
    return {"ok": True, "user_id": user_id, "tier": tier,
            "verification_status": "approved",
            "verified_by": updated.get("verified_by"),
            "verified_at": updated.get("verified_at")}


@router.post("/queue/{user_id}/reject")
async def reject_signup(
    user_id: str,
    body: RejectBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    note = (body.note or "").strip()
    if not note:
        # A rejection with no reason cannot be audited, appealed, or learned
        # from — and this queue rejects real physicians only deliberately.
        raise HTTPException(status_code=400, detail="Rejection requires a note.")
    store = _store()
    _load_user_or_404(user_id)
    updated = store.record_verification_decision(
        user_id, status="rejected", decided_by=admin["email"], note=note)
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="verification_rejected",
        actor=admin["email"], payload={"note": note},
    )
    try:
        store.advance_referral_for_user(user_id, "declined")
    except Exception:
        log.exception("[referral] could not mark referral declined (non-fatal)")
    return {"ok": True, "user_id": user_id, "verification_status": "rejected",
            "verified_by": updated.get("verified_by"),
            "verified_at": updated.get("verified_at")}


def _recheck_one(store: Any, user: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """One NPI recheck. Synchronous (httpx + sqlite) — callers must reach it
    through ``run_in_threadpool``; it must never run on the event loop.

    ``force`` bypasses the 30-day cache. A human clicking "Recheck" is asking
    the registry again, so serving them a cached answer makes the button a
    no-op. The bulk sweep does NOT force: those rows have no definitive answer
    at all, so another row's cached answer for the same NPI is a legitimate —
    and free — resolution.
    """
    npi = credentialing.clean_npi(user.get("npi") or "")
    if not npi:
        return {"result": "skipped", "reason": "no_npi"}
    try:
        cached = None if force else store.get_cached_npi_fetch(npi)
        result = credentialing.verify_npi(npi, _family_name(user), cached=cached)
    except Exception:
        log.exception("[verify] recheck failed for %s", user.get("id"))
        result = {"result": "unavailable", "reason": "exception", "record": None}
    store.set_npi_result(user["id"], result)
    return result


@router.get("/recheck-pending")
async def recheck_pending_list(
    older_than_minutes: int = 60,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The retry list (PRD §1.2). UNAVAILABLE routes to manual review *and*
    schedules a retry — this is the retry, as a list the admin can see and run
    rather than an invisible background job."""
    rows = _store().users_pending_npi_recheck(older_than_minutes=max(0, older_than_minutes))
    return {
        "count": len(rows),
        "users": [
            {"user_id": r["id"], "email": r["email"], "npi": r.get("npi"),
             "last_attempt_at": r.get("npi_last_attempt_at"),
             "last_attempt": (credentialing._json_field(r, "npi_last_attempt_json")
                              .get("reason")),
             "verification_status": r.get("verification_status")}
            for r in rows
        ],
    }


@router.post("/recheck-pending")
async def recheck_pending_run(
    older_than_minutes: int = 60,
    limit: int = 50,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Bulk-run the retry list. Bounded by ``limit`` and by the
    ``older_than_minutes`` floor so a sweep cannot hot-loop a rate-limiting
    registry. Runs off the event loop — this makes N network calls."""
    store = _store()
    rows = store.users_pending_npi_recheck(
        older_than_minutes=max(0, older_than_minutes), limit=max(1, min(limit, 200)))

    def _sweep() -> Dict[str, int]:
        tally: Dict[str, int] = {}
        for row in rows:
            outcome = _recheck_one(store, row).get("result") or "unknown"
            tally[outcome] = tally.get(outcome, 0) + 1
        return tally

    tally = await run_in_threadpool(_sweep)
    store.log_event(
        entity_type="user", entity_id=None, event_type="npi_recheck_sweep",
        actor=admin["email"], payload={"attempted": len(rows), "outcomes": tally},
    )
    return {"ok": True, "attempted": len(rows), "outcomes": tally}


@router.post("/queue/{user_id}/recheck-npi")
async def recheck_npi(
    user_id: str,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Manual retry — the human path out of UNAVAILABLE.

    Non-destructive: a recheck that cannot reach NPPES records an attempt and
    leaves any existing verified result intact (see ``store.set_npi_result``).
    Runs off the event loop — it makes a network call.
    """
    store = _store()
    user = _load_user_or_404(user_id)
    if not credentialing.clean_npi(user.get("npi") or ""):
        raise HTTPException(status_code=400, detail="This user has no NPI on file.")
    result = await run_in_threadpool(_recheck_one, store, user, force=True)
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="npi_rechecked",
        actor=admin["email"],
        payload={"result": result.get("result"), "reason": result.get("reason")},
    )
    refreshed = store.get_user_by_id(user_id)
    return {"ok": True, "user_id": user_id, "npi": _npi_summary(refreshed),
            "npi_verified": refreshed.get("npi_verified")}
