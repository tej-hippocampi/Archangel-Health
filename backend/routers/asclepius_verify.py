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
from asclepius import calibration as asc_calibration
from asclepius import capabilities as asc_caps
from asclepius import credentialing
from asclepius import specialties as asc_specialties
from asclepius import tiering as asc_tiering
from asclepius.store import get_store
from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import build_asclepius_approved_email

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
    """The family name to compare against NPPES.

    C-0.2: this used ``legal.split()[-1]``, which takes "MD" as the family name from
    "Jane Doe, MD" and "Jr" from "John Smith Jr" — both normalize to '' and MISMATCH by
    construction. The signup path (``onboarding._run_signup_verification``) already used
    ``family_name_from_legal_name``, so the two paths disagreed about the same physician:
    signup verified them, and then the admin's **Recheck NPI** button — the one pressed to
    HELP a stuck record — overwrote the verification with a name mismatch. One helper, both
    paths, or they drift again.
    """
    creds = credentialing._json_field(user, "credentials_json")
    legal = str(creds.get("fullLegalName") or user.get("full_name") or "").strip()
    return credentialing.family_name_from_legal_name(legal)


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


# ─── PRD-CRED · tiering proposal ─────────────────────────────────────────────
# The legacy ``credentialing.propose_tier`` is left exactly as it is. It feeds the queue's
# existing score column, existing tests depend on it, and §8 forbids backfilling — so the new
# gate-first proposal is served ALONGSIDE it under its own key rather than replacing it in
# place. When the admin surface has been on the new one for a release, the old key can go.


def _measured_quality_map(store: Any, specialty: Optional[str] = None) -> Dict[str, float]:
    """``{user_id: measured_quality_z}`` for everyone with enough completed work.

    Computed ONCE per request and threaded through the queue rows, for the same reason
    ``npi_claim_counts`` exists: a Dawid–Skene fit per row would run fifty EM loops to render
    one page. Degrades to ``{}`` — never raises — when the paired-label structure has not
    produced anything yet.
    """
    try:
        data = store.paired_label_observations(specialty=specialty)
        obs = data.get("observations") or []
        if not obs:
            return {}
        est = asc_tiering.one_coin_dawid_skene(obs, gold=data.get("gold") or {})
        accuracy = est.get("accuracy") or {}
        if not accuracy:
            return {}
        population = list(accuracy.values())
        n_items = est.get("n_items") or {}
        out: Dict[str, float] = {}
        for uid, acc in accuracy.items():
            z = asc_tiering.measured_quality_z(acc, population,
                                               n_items=int(n_items.get(uid) or 0))
            if z is not None:
                out[uid] = z
        return out
    except Exception:
        # Measured quality is an enhancement to a proposal, not the proposal. A failure here
        # must degrade the score, never the page.
        log.exception("[tiering] measured-quality estimation failed (non-fatal)")
        return {}


def _tiering_proposal(
    store: Any,
    user: Dict[str, Any],
    *,
    case_domain: Optional[str] = None,
    dupe_counts: Optional[Dict[str, int]] = None,
    mq_map: Optional[Dict[str, float]] = None,
    weights: Optional[Dict[str, Dict[str, float]]] = None,
    explore: Optional[bool] = None,
) -> Dict[str, Any]:
    """Compose everything the score needs, then propose.

    ``case_domain`` defaults to the physician's OWN declared specialty. That is not a
    shortcut: at signup there is no case, and the honest question the admin is answering is
    "could this person adjudicate in their own domain?" — ``P(TR | physician, their domain)``.
    Asking it without a domain at all would silently reintroduce ``P(TR | physician)``, which
    is the exact thing finding 1 says does not exist.
    """
    uid = user["id"]
    npi = credentialing.clean_npi(user.get("npi") or "")
    if dupe_counts is None:
        dupe = _duplicate_npi(store, user) if npi else False
    else:
        dupe = dupe_counts.get(npi, 0) > 1
    creds = credentialing._json_field(user, "credentials_json")
    domain = (case_domain or creds.get("primarySpecialty") or user.get("specialty") or "")

    attempt = store.latest_calibration_for_user(uid, asc_tiering.normalize_domain(domain))
    calibration = None
    if attempt and attempt.get("composite") is not None:
        population = store.calibration_population(attempt.get("specialty") or "")
        calibration = {
            "calibration_z": asc_calibration.calibration_z(attempt["composite"], population),
            "tr_gate_passed": attempt.get("tr_gate_passed"),
            "composite": attempt["composite"],
            "attempt_id": attempt.get("attempt_id"),
        }

    n_tasks = store.completed_task_count(uid)
    mq = (mq_map or {}).get(uid)

    if explore is None:
        explore = False
    out = asc_tiering.propose(
        user,
        case_domain=domain or None,
        calibration=calibration,
        measured_quality_z=mq,
        n_tasks=n_tasks,
        leie_status=store.leie_status(npi) if npi else "unknown",
        duplicate_npi=dupe,
        weights=weights or store.get_tiering_weights(),
        explore=bool(explore),
    )
    out["calibration"] = calibration
    out["leie_loaded_at"] = store.leie_loaded_at()
    # AUDIT UI. Both lists are served rather than written in the client:
    #
    #   * tier words, because "Never render a raw tier token to a human" is stated twice in
    #     the context pack and `capabilities.tier_word()` is the single place that knows them.
    #     A client-side map is a second enumeration that drifts.
    #   * available domains, because a hardcoded ['nephrology','cardiology','oncology'] in the
    #     client is wrong the week a specialty is enabled, and wrong SILENTLY — the picker
    #     just lacks an entry and nobody can score against the new domain.
    out["tier_options"] = [{"value": t, "word": asc_caps.tier_word(t)}
                           for t in (asc_tiering.TL, asc_tiering.TR)]
    out["available_domains"] = [
        {"value": c["specialty"], "word": c["specialty"].replace("_", " ").title()}
        for c in asc_specialties.list_specialties() if c.get("enabled")
    ]
    return out


def _queue_row(store: Any, user: Dict[str, Any],
               dupe_counts: Optional[Dict[str, int]] = None,
               mq_map: Optional[Dict[str, float]] = None,
               weights: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
    prop = _proposal(store, user, dupe_counts)
    cv_parsed = credentialing._json_field(user, "cv_parsed_json")
    try:
        tiering_prop = _tiering_proposal(store, user, dupe_counts=dupe_counts,
                                         mq_map=mq_map, weights=weights)
    except Exception:
        # A row that cannot be scored still has to render — an admin working a queue must
        # never lose a physician because one feature encoder threw.
        log.exception("[tiering] proposal failed for %s (row still rendered)", user.get("id"))
        tiering_prop = {"error": "tiering proposal unavailable for this row"}
    return {
        "tiering": tiering_prop,
        # AUDIT UI: the queue's own legacy badge rendered `proposes labeler` too. Every tier
        # token that reaches this payload now travels with its word, from the one place that
        # knows them (capabilities.TIER_WORDS) — the client never maps a token itself.
        "proposed_tier_word": (asc_caps.tier_word(prop["proposed_tier"])
                               if prop["proposed_tier"] else None),
        "tier_word": asc_caps.tier_word(user.get("tier")) if user.get("tier") else None,
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
    # Both computed once for the whole page, not once per row (see _measured_quality_map).
    mq_map = _measured_quality_map(store)
    weights = store.get_tiering_weights()
    rows = [_queue_row(store, u, dupe_counts, mq_map, weights) for u in page]
    return {
        "status": status,
        # Approval offers all three tiers (advisor included, since the queue must not be a
        # dead end for someone already agreed as an advisor). Served as words, never tokens.
        "tier_words": {t: asc_caps.tier_word(t) for t in _TIERS},
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
    case_domain: Optional[str] = None,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    user = _load_user_or_404(user_id)
    row = _queue_row(store, user)
    if case_domain:
        # Re-score for the domain the admin asked about. The dossier's default is the
        # physician's own specialty; this is how an admin checks "would I want them
        # adjudicating a cardiology case?" without leaving the row.
        row["tiering"] = _tiering_proposal(store, user, case_domain=case_domain)
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
        "tier_words": {t: asc_caps.tier_word(t) for t in _TIERS},
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
    # PRD C §5.3 — **the override IS the training signal.** Recorded BEFORE the approval
    # commits, and recorded on agreement as well as disagreement: a model that only ever sees
    # its mistakes learns that it is always wrong. Non-fatal by construction — a learning-loop
    # bookkeeping failure must never cost a physician their approval.
    try:
        tprop = _tiering_proposal(store, user)
        store.record_tiering_decision(
            user_id=user_id,
            case_domain=tprop.get("case_domain"),
            features=tprop.get("features") or {},
            proposed_tier=tprop.get("proposed_tier"),
            admin_tier=tier,
            was_exploration=bool(tprop.get("was_exploration")),
            outcome_source="admin",
            score=tprop.get("score"),
            decided_by=admin["email"],
        )
        # §6 fairness monitor. The decided tier AND the feature vector are COPIED ONTO the
        # demographics row here, so the monitor later needs no join at all. Note what is NOT
        # happening: nothing reads demographics off `user`. They are not on the users row and
        # never will be — every feature path loads a physician with `SELECT * FROM users`, so
        # a column there is a column the model can reach. This call writes a tier and a
        # feature vector next to a pseudonym, in that direction only.
        store.stamp_fairness_tier(user_id, tier, features=tprop.get("features"))
    except Exception:
        log.exception("[tiering] could not record the decision (approval stands)")
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
                user["email"], "You're approved for Asclepius",
                build_asclepius_approved_email(
                    full_name=(user.get('full_name') or '').strip(),
                    workspace_url=_portal_base() + '/asclepius',
                ), importance_headers=True)
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


# ═══════════════════════════════════════════════════════════════════════════════
# PRD-CRED · tiering, the learning loop, calibration, and the fairness monitor
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/tiering/{user_id}")
async def tiering_for_user(
    user_id: str,
    case_domain: Optional[str] = None,
    explore: bool = False,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """``P(TR | physician, case domain)`` for one physician.

    ``case_domain`` is the whole point of the endpoint: the same physician is a different
    answer on a nephrology case and a cardiology one, and an admin deciding a tier needs to be
    able to ask both. Omitted, it defaults to the physician's own declared specialty.
    """
    store = _store()
    user = _load_user_or_404(user_id)
    return _tiering_proposal(store, user, case_domain=case_domain, explore=explore)


class TierOverrideBody(BaseModel):
    tier: str
    case_domain: Optional[str] = None
    note: Optional[str] = None


@router.post("/tiering/{user_id}/decide")
async def tiering_decide(
    user_id: str,
    body: TierOverrideBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Record an explicit tier decision as a training observation, without re-approving.

    Separate from ``/approve`` on purpose. Approval is a lifecycle event that happens once;
    a tier judgment is something an admin may make repeatedly and per-domain, and every one of
    those is a label. Collapsing them would throw away most of the training signal — the
    override IS the training signal (§5.3).
    """
    tier = (body.tier or "").strip().lower()
    if tier not in _TIERS:
        raise HTTPException(status_code=400,
                            detail=f"tier must be one of {', '.join(repr(t) for t in _TIERS)}")
    store = _store()
    user = _load_user_or_404(user_id)
    prop = _tiering_proposal(store, user, case_domain=body.case_domain)
    decision = store.record_tiering_decision(
        user_id=user_id,
        case_domain=prop.get("case_domain"),
        features=prop.get("features") or {},
        proposed_tier=prop.get("proposed_tier"),
        admin_tier=tier,
        was_exploration=bool(prop.get("was_exploration")),
        outcome_source="admin",
        score=prop.get("score"),
        decided_by=admin["email"],
    )
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="tiering_decision",
        actor=admin["email"],
        payload={"admin_tier": tier, "proposed_tier": prop.get("proposed_tier"),
                 "was_flip": decision.get("was_flip"),
                 "case_domain": prop.get("case_domain"), "note": body.note},
    )
    return {"ok": True, "decision": decision, "proposal": prop}


@router.get("/tiering-weights")
async def tiering_weights(admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """The current posterior, next to the prior it started from.

    Shown side by side deliberately: the useful question about a learned model is never "what
    is the weight" but "how far has it moved from the rule, and did the data earn that".
    """
    store = _store()
    current = store.get_tiering_weights()
    priors = asc_tiering.default_weights()
    return {
        "weights": [
            {
                "feature": name,
                "m": round(row["m"], 5),
                "q": round(row["q"], 5),
                "pinned": bool(row.get("pinned")),
                "prior_m": priors.get(name, {}).get("m"),
                "prior_q": priors.get(name, {}).get("q"),
                "drift": round(row["m"] - priors.get(name, {}).get("m", 0.0), 5),
                # 1/sqrt(q) is the posterior sd — the number that should be shrinking.
                "sd": round(1.0 / (max(row["q"], 1e-9) ** 0.5), 5),
            }
            for name, row in sorted(current.items())
        ],
        "pending_decisions": len(store.pending_tiering_decisions(limit=1000)),
        "thresholds": {"tr": asc_tiering.TR_THRESHOLD, "tl": asc_tiering.TL_THRESHOLD},
        "max_delta_per_batch": asc_tiering.MAX_DELTA_M,
    }


@router.post("/tiering-weights/apply")
async def tiering_weights_apply(
    limit: int = 500,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Fold the un-applied decisions into the weights, exactly once.

    Runs off the event loop: a few Newton steps over a 17-dimensional system plus a sqlite
    transaction is short but not free, and it is a synchronous function.
    """
    store = _store()
    result = await run_in_threadpool(
        asc_tiering.apply_decision_batch, store, limit=limit, actor=admin["email"])
    store.log_event(
        entity_type="user", entity_id=None, event_type="tiering_weights_updated",
        actor=admin["email"],
        payload={"applied": result.get("applied"), "marked": result.get("marked"),
                 "deltas": {k: v for k, v in (result.get("deltas") or {}).items()
                            if abs(v) > 1e-9}},
    )
    return {"ok": True, **{k: v for k, v in result.items() if k != "weights"}}


@router.get("/fairness")
async def fairness_monitor(
    since: Optional[str] = None,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """TR selection rate by voluntary self-reported demographics, four-fifths comparison.

    Reads ``fairness_observations`` and nothing else. There is no join to ``users`` here and
    there cannot be one: the table is keyed by an HMAC pseudonym and carries the decided tier
    copied in at decision time. Collecting demographics separately is exactly what makes this
    monitor possible without making the model able to see them.
    """
    return _store().fairness_selection_rates(since=since)


class DemographicsBody(BaseModel):
    demographics: Dict[str, Any]


@router.post("/demographics/{user_id}")
async def record_demographics(
    user_id: str,
    body: DemographicsBody,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Voluntary self-report, written straight to the fairness table.

    A physician may only submit their own; an admin may submit on behalf of one who answered
    on paper. Never inferred, never required, and never written to the users row.
    """
    if user.get("role") != "admin" and user.get("id") != user_id:
        raise HTTPException(status_code=403, detail="You can only submit your own.")
    store = _store()
    _load_user_or_404(user_id)
    obs_id = store.record_fairness_observation(
        user_id=user_id, demographics=body.demographics or {})
    return {"ok": True, "recorded": obs_id is not None}


# ─── Calibration exam (PRD C §4) ─────────────────────────────────────────────
@router.get("/calibration/exam")
async def calibration_exam(
    specialty: Optional[str] = None,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Start an attempt. The response NEVER carries a key — see ``calibration.blind_item``."""
    store = _store()
    creds = credentialing._json_field(user, "credentials_json")
    spec = asc_tiering.normalize_domain(
        specialty or creds.get("primarySpecialty") or user.get("specialty") or "")
    if not spec:
        raise HTTPException(
            status_code=400,
            detail="No recognised specialty on file — the exam is drawn from your declared "
                   "specialty's task distribution.")
    # An attempt already in flight is RESUMED, never replaced. Minting a new one on every
    # GET would let a candidate reroll the item sample by refreshing until an easy draw came
    # up, and would silently spend an attempt on a dropped connection.
    open_attempt = store.open_calibration_attempt(user["id"], spec)
    if open_attempt:
        return {"attempt_id": open_attempt["attempt_id"], "specialty": spec,
                "items": [asc_calibration.blind_item(it) for it
                          in store.get_calibration_items(open_attempt["item_ids"])],
                "size": len(open_attempt["item_ids"]), "resumed": True}

    try:
        asc_calibration.check_retake_allowed(store, user_id=user["id"], specialty=spec)
    except asc_calibration.RetakeNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        exam = asc_calibration.build_exam(store, specialty=spec)
    except asc_calibration.ExamNotAvailable as exc:
        # 503, not 404. The exam is not missing; it is not ready. A physician must not be told
        # they failed a gate that was never opened.
        raise HTTPException(status_code=503, detail=str(exc))
    attempt = store.start_calibration_attempt(
        user_id=user["id"], specialty=spec, item_ids=exam["item_ids"])
    return {"attempt_id": attempt["attempt_id"], "specialty": spec,
            "items": exam["items"], "size": exam["size"], "resumed": False}


class CalibrationSubmitBody(BaseModel):
    responses: Dict[str, Dict[str, Any]]


@router.post("/calibration/{attempt_id}/submit")
async def calibration_submit(
    attempt_id: str,
    body: CalibrationSubmitBody,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    store = _store()
    attempt = store.get_calibration_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="No such attempt")
    if attempt["user_id"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not your attempt")
    if attempt.get("submitted_at"):
        raise HTTPException(status_code=409, detail="This attempt was already submitted.")
    # Raw first, graded second, and in that order on purpose: if grading throws, the raw
    # responses are already durable and the attempt can be rescored rather than re-sat.
    store.record_calibration_responses(attempt_id, body.responses or {})
    items = store.get_calibration_items(attempt["item_ids"])
    result = asc_calibration.grade(items, body.responses or {})
    store.record_calibration_score(attempt_id, result)
    store.log_event(
        entity_type="user", entity_id=user["id"], event_type="calibration_submitted",
        actor=user.get("email"),
        payload={"attempt_id": attempt_id, "composite": result.get("composite"),
                 "tr_gate_passed": result.get("tr_gate_passed")},
    )
    return {"ok": True, "attempt_id": attempt_id, **result}


@router.post("/calibration/{attempt_id}/rescore")
async def calibration_rescore(
    attempt_id: str,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Re-grade an old attempt against today's keys, from the stored raw responses —
    so an item can be re-keyed without re-testing everyone who already sat it."""
    try:
        return {"ok": True, **await run_in_threadpool(
            asc_calibration.rescore_attempt, _store(), attempt_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="No such attempt")


class LeieLoadBody(BaseModel):
    csv_text: Optional[str] = None
    source_note: Optional[str] = None


@router.post("/leie/load")
async def leie_load(
    body: LeieLoadBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Load an OIG LEIE monthly CSV snapshot (gate A5).

    Takes the CSV body rather than fetching a URL: the download is ~100 MB and lives on a
    government host with its own availability, and an import path that reaches the public
    internet on an admin click is an outage waiting to be attributed to us. The founder
    downloads the monthly file; this ingests it atomically.
    """
    text = body.csv_text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="csv_text is required.")
    rows = asc_tiering.parse_leie_csv(text)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No rows with a usable 10-digit NPI. Expected the OIG LEIE CSV with an "
                   "'NPI' column; rows without an NPI are intentionally not name-matched.")
    store = _store()
    n = await run_in_threadpool(
        store.replace_leie_exclusions, rows, source_note=body.source_note)
    store.log_event(
        entity_type="user", entity_id=None, event_type="leie_snapshot_loaded",
        actor=admin["email"], payload={"rows": n, "source_note": body.source_note},
    )
    return {"ok": True, "rows": n, "loaded_at": store.leie_loaded_at()}


@router.get("/readiness")
async def tr_readiness(admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Can ANY physician become a TR today, and if not, what exactly is missing?

    AUDIT M1. There are **two** independent day-one blockers and they have two different
    owners, but only one of them — the empty calibration item bank — was written down. The
    other is the OIG LEIE snapshot: with `leie_meta` empty, gate A5 answers `unknown` for
    every physician, `propose()` short-circuits on an undetermined gate, and everyone lands in
    the admin band no matter how well they score.

    Both fail loudly in isolation and neither is visible as a *launch* condition, so a plan
    that resolves the item bank alone ships zero TRs and looks like a modelling problem. This
    endpoint is the thing that says otherwise in one call.
    """
    store = _store()
    blockers: List[Dict[str, Any]] = []

    leie_loaded = store.leie_loaded_at()
    blockers.append({
        "id": "leie_snapshot",
        "blocking": not leie_loaded,
        "title": "OIG LEIE exclusion snapshot loaded",
        "detail": (f"Loaded {leie_loaded}." if leie_loaded else
                   "Never loaded. Hard gate A5 answers 'unknown' for EVERY physician, so no "
                   "one can be proposed as a reviewer regardless of their score."),
        "action": ("Re-load monthly to keep it current." if leie_loaded else
                   "Download the monthly LEIE CSV from oig.hhs.gov and POST it to "
                   "/api/asclepius/verify/leie/load."),
        "owner": "operations",
    })

    per_specialty = {}
    for cfg in asc_specialties.list_specialties():
        if not cfg.get("enabled"):
            continue
        name = cfg["specialty"]
        admissible = [it for it in store.list_calibration_items(specialty=name)
                      if asc_calibration.admissible(it.get("key") or {})[0]]
        per_specialty[name] = {"admissible_items": len(admissible),
                               "needed": asc_calibration.EXAM_MIN_ITEMS,
                               "ready": len(admissible) >= asc_calibration.EXAM_MIN_ITEMS}
    ready_specialties = [s for s, v in per_specialty.items() if v["ready"]]
    blockers.append({
        "id": "calibration_item_bank",
        "blocking": not ready_specialties,
        "title": "Calibration exam item bank keyed",
        "detail": (f"Ready in: {', '.join(ready_specialties)}." if ready_specialties else
                   f"No specialty has {asc_calibration.EXAM_MIN_ITEMS} admissible keyed "
                   "items. The exam returns 503 and no one can clear the reviewer gate."),
        "action": (f"Key {asc_calibration.EXAM_MIN_ITEMS} items per specialty with a "
                   f"reference panel of {asc_calibration.PANEL_MIN} independent expert "
                   "judgments each. An item whose panel did not converge is not admissible."),
        "owner": "clinical",
        "per_specialty": per_specialty,
    })

    outstanding = [b for b in blockers if b["blocking"]]
    return {
        # Deliberately the composite answer first: the failure this endpoint exists to prevent
        # is someone resolving one blocker, seeing it turn green, and assuming they are done.
        "tr_possible": not outstanding,
        "blockers": blockers,
        "outstanding": [b["id"] for b in outstanding],
        "note": ("Both blockers must clear before ANY physician can be proposed as a "
                 "reviewer. They are separate operational loops with separate owners — see "
                 "docs/PRD_C_LAUNCH_CHECKLIST.md."),
    }


@router.get("/leie/status")
async def leie_status(admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    store = _store()
    loaded = store.leie_loaded_at()
    return {
        "loaded_at": loaded,
        # Stated plainly because the failure mode is silent: with no snapshot, gate A5 answers
        # UNKNOWN for every physician and every one of them lands in the admin band. That is
        # correct behaviour, and it looks exactly like the model being indecisive.
        "gate_a5": "unknown_for_everyone" if not loaded else "active",
    }
