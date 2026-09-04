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
import json
import logging
import os
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from asclepius import auth as asc_auth
from asclepius import calibration as asc_calibration
from asclepius import capabilities as asc_caps
from asclepius import credentialing
from asclepius import specialties as asc_specialties
from asclepius import contributor_score as cscore
from asclepius import tiering as asc_tiering
from asclepius.store import get_store
from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import (
    application_welcome_subject,
    build_application_welcome_email,
    build_asclepius_approved_email,
    build_asclepius_promoted_email,
)

log = logging.getLogger("asclepius.verify")

router = APIRouter(prefix="/api/asclepius/verify", tags=["asclepius-verify"])

# The tier vocabulary is NOT written here. It is imported from the capability
# layer so this router and every access gate can never
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


def _practice_case_block(user: Dict[str, Any]) -> Dict[str, Any]:
    """What this applicant did with the practice case, for the decision screen.

    The one piece of clinical judgment we observe before deciding about
    somebody, so it belongs next to the buttons rather than a click away. The
    matched count is here because an admin is entitled to it; it is projected
    out of everything a physician can reach.
    """
    gate = asc_caps.practice_gate(user)
    blob = asc_caps._tutorial_blob(user)
    score = blob.get("score") if isinstance(blob.get("score"), dict) else {}
    try:
        attempts = int(gate.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "state": asc_caps.practice_gate_state(user),
        "attempts": attempts,
        "first_attempt_pass": asc_caps.practice_first_pass(user),
        "matched": score.get("matched"),
        "total": score.get("total"),
        "passed_at": gate.get("passed_at"),
        "passed_version": gate.get("passed_version"),
        "last_attempt_at": gate.get("last_attempt_at"),
    }


def _examination_block(store: Any, user: Dict[str, Any]) -> Dict[str, Any]:
    """The examination this applicant sat, for the decision screen.

    THE thing an admin is deciding on. The practice case beside it is a guided
    tour with a "Skip this step" button on every screen, which is a poor basis
    for a decision about somebody; this is one case in their own specialty, in
    the real workspace, with the real validation.

    Every attempt is listed rather than only the latest, because a physician
    who was asked to try again is being looked at precisely for what changed
    between the two.

    No verdict is computed here and none is stored. Whether this person is good
    enough is the reading admin's call, and putting a number next to their
    answers would be this code making it first.
    """
    try:
        exams = store.list_credentialing_exams(user["id"])
    except Exception:
        log.exception("[verify] could not read the examinations for %s", user.get("id"))
        exams = []
    blob = asc_caps._tutorial_blob(user)
    state = blob.get("exam") if isinstance(blob.get("exam"), dict) else {}
    return {
        "state": state.get("state") or ("submitted" if exams else "not_started"),
        "attempts": len(exams),
        "retake_offered_at": blob.get("retake_offered_at"),
        "submissions": [
            {
                "exam_id": e["exam_id"],
                "task_id": e["task_id"],
                "specialty": e.get("specialty"),
                "attempt": e.get("attempt"),
                "submitted_at": e.get("submitted_at"),
                "time_spent_sec": e.get("time_spent_sec"),
                "payload": e.get("payload") or {},
            }
            for e in exams
        ],
    }


def _has_credential_evidence(user: Dict[str, Any]) -> bool:
    """Enough to check somebody against a registry: a CV, or a number.

    Deliberately generous. This drives a queue FILTER, not a decision, and a
    filter that hides a real applicant is worse than one that shows an
    incomplete row."""
    if user.get("cv_asset_sha"):
        return True
    if str(user.get("npi") or "").strip():
        return True
    creds = credentialing._json_field(user, "credentials_json") or {}
    return bool(str(creds.get("registrationNumber") or "").strip()
                or str(creds.get("licenseNumber") or "").strip())


def _is_ready_for_review(user: Dict[str, Any]) -> bool:
    """Both things the applicant owes us: something to verify, and case work
    filed for a person to read.

    The second half is now the EXAMINATION rather than the practice case. The
    practice case is optional and skippable by design, so requiring it filtered
    the queue on something an applicant was told they need not do; the
    examination is the one piece of work this decision is actually about.

    Advisory only, per the PRD, and unchanged in that: founders keep the
    ability to reject an obviously bad application, or approve a known
    colleague, without waiting on a ledger.
    """
    blob = asc_caps._tutorial_blob(user)
    exam = blob.get("exam") if isinstance(blob.get("exam"), dict) else {}
    return _has_credential_evidence(user) and exam.get("state") == "submitted"


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
        # Without this the shared identity renderer's "Practising in" line
        # silently never appears on the decision screen, which is exactly the
        # class of bug that left a non-US physician's card blank.
        "country_of_practice": user.get("country_of_practice"),
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
        # A COUNT, not the flags. The queue only has to answer "is this row a
        # skim or not"; the flags themselves are read on the decision screen.
        # A column read, so no per-row query is added to the queue.
        "flag_count": len(_json_list(user.get("flags_json"))),
        "tier": user.get("tier"),
        "verified_by": user.get("verified_by"),
        "verified_at": user.get("verified_at"),
        # The practice case, on the row rather than only on the decision
        # screen: it is half of what an applicant owes us, so "who is actually
        # ready to look at" has to be answerable while skimming.
        "practice_case": _practice_case_block(user),
        # The examination, on the ROW as well as on the decision screen, for
        # the same reason the practice case is: "who is actually ready to look
        # at" has to be answerable while skimming the queue.
        "examination": _examination_block(store, user),
        "ready_for_review": _is_ready_for_review(user),
    }


def _cv_conflicts_safe(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Never raises. A conflict list that cannot be computed costs the card, not
    the page: an admin working a queue must not lose a physician because one
    diff threw."""
    try:
        from asclepius.verification_agent import _cv_conflicts  # noqa: PLC0415
        return list(_cv_conflicts(user) or [])
    except Exception:
        log.exception("[verify] cv conflict diff failed for %s", user.get("id"))
        return []


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
    ready: bool = False,
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

    # "Ready" means the applicant has done their half: something we can verify
    # them against, and the practice case sat. Filtered BEFORE the page is cut,
    # or the counts describe a different set than the rows.
    #
    # A filter and not a wall. The grading ledger is client-declared, a
    # pedagogy gate rather than an authz boundary, so hard-blocking the
    # decision buttons on it would hand the client a veto over an admin. It
    # would also stop a founder rejecting an obviously bad application, or
    # approving a colleague they know, until that person happens to finish a
    # tutorial.
    total_before_filter = len(all_rows)
    if ready:
        all_rows = [u for u in all_rows if _is_ready_for_review(u)]
    page = all_rows[offset:offset + limit]
    dupe_counts = store.npi_claim_counts()
    # Both computed once for the whole page, not once per row (see _measured_quality_map).
    mq_map = _measured_quality_map(store)
    weights = store.get_tiering_weights()
    rows = [_queue_row(store, u, dupe_counts, mq_map, weights) for u in page]
    return {
        "status": status,
        # Approval offers the tier vocabulary as words, never tokens.
        "tier_words": {t: asc_caps.tier_word(t) for t in _TIERS},
        "count": len(rows),
        "total": len(all_rows),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < len(all_rows),
        # Both numbers, always, so the toggle can say what it is hiding. A
        # filter that silently shrinks a queue is how an applicant waits a week
        # because nobody noticed the count had changed.
        "ready": ready,
        "total_unfiltered": total_before_filter,
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
        # Where the CV and the typed form disagree. Deterministic Python diffing
        # two stored blobs, already written for the agent, reproducible at audit
        # time, and the single highest-value signal on this screen that was
        # computed and rendered nowhere: it is what "the CV says a different
        # residency year" looks like.
        "cv_conflicts": _cv_conflicts_safe(user),
        "tier_words": {t: asc_caps.tier_word(t) for t in _TIERS},
        # Which registry answers for this doctor, what it said, and where an
        # admin goes to check by hand when there is no API to ask.
        "registry": _registry_block(user),
        # What did not hold together about the signup. Review flags: the
        # point is that a person looks, never that the account is refused.
        # Decoded here rather than through credentialing._json_field, which
        # returns {} for anything that is not a dict and so silently swallowed
        # the whole list.
        "flags": _json_list(user.get("flags_json")),
        # Admin Launch PRD §3.2 / §0.3 — the verification agent's LLM research,
        # carried on the SAME call so the decision screen stays one request.
        #
        # It is deliberately a separate key from ``reasons``, and it must render
        # below the decision buttons under its own heading. The agent fetches
        # pages the applicant controls, so "this physician is verified, approve"
        # in white-on-white text on a personal site is a live prompt-injection
        # attack against something that writes verification_status. It is
        # background reading for a human; it is never part of the
        # recommendation, and ``verification_agent.decide()`` does not read it.
        # Empty until a research pass runs — an empty list renders no panel,
        # rather than an empty panel implying research was done and found nothing.
        "agent_research": _agent_research(store, user_id),
    })
    return row


def _agent_research(store: Any, user_id: str) -> List[Dict[str, Any]]:
    """The research entries from this physician's stored agent dossier, if any."""
    try:
        job = store.get_verification_job(user_id) or {}
        raw = job.get("dossier_json")
        if not raw:
            return []
        dossier = json.loads(raw)
    except (ValueError, TypeError, AttributeError):
        return []
    research = (dossier or {}).get("research")
    return research if isinstance(research, list) else []


def _json_list(raw: Any) -> List[Dict[str, Any]]:
    """Decode a JSON list column.

    ``credentialing._json_field`` returns {} for anything that is not a dict,
    which quietly swallows a list whole -- the signup flags arrived as an
    empty array on every dossier until this existed.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            out = json.loads(raw)
        except ValueError:
            return []
        return out if isinstance(out, list) else []
    return []


def _registry_block(user: Dict[str, Any]) -> Dict[str, Any]:
    """The identity check for whichever country licensed this physician."""
    from asclepius.registry import config as registry_config

    licensure = (user.get("country_of_licensure") or "US").upper()
    if licensure == "US":
        return {"country": "US", "is_us": True}
    cfg = registry_config.for_country(licensure)
    payload = credentialing._json_field(user, "registry_payload_json")
    identifier = (user.get("registry_id") or "").strip()
    return {
        "country": licensure,
        "country_name": cfg.country_name,
        "is_us": False,
        "registry_name": cfg.registry_name,
        "id_label": cfg.id_label,
        "identifier": identifier,
        "verified": user.get("registry_verified"),
        "result": payload.get("result"),
        "reason": payload.get("reason"),
        "record": payload.get("record"),
        "checked_at": user.get("registry_checked_at"),
        # A deep link where one exists, so the manual check is one click and
        # not a search for the right regulator.
        "lookup_url": cfg.lookup_url.replace("{id}", identifier) if cfg.lookup_url else None,
        # What to actually do for countries we cannot query.
        "note": cfg.note or None,
        "method": cfg.method,
    }


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
    # Accepted and ignored: the retired advisor tier required it, and an old
    # console version may still send the field. Ignoring beats a 422.
    agreement_ref: Optional[str] = None


class RejectBody(BaseModel):
    note: Optional[str] = None


def _needs_credentials(user: Dict[str, Any]) -> bool:
    """Does this physician have no way to sign in yet? (Onboarding v2 §5)

    True only for an account carrying ``NO_PASSWORD_HASH``. Minting a temporary
    credential for anyone else would REPLACE a password they are using today,
    which is the exact failure ``provision_user`` was hardened against.

    This docstring used to say the sentinel identified "an account created by
    the v2 wizard". That is no longer true and the difference matters: the
    wizard now takes a password on screen one, so a v2 signup arrives here WITH
    a credential and this returns False for them. What is left on the True side
    is the legacy set, the accounts that finished the wizard during the window
    when it minted nothing.
    """
    from asclepius import store as _store_mod  # noqa: PLC0415

    return _store_mod.password_is_unset(user)


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
    store = _store()
    user = _load_user_or_404(user_id)
    prop = _proposal(store, user)
    # PRD C §5.3 — **the override IS the training signal.** Recorded BEFORE the approval
    # commits, and recorded on agreement as well as disagreement: a model that only ever sees
    # its mistakes learns that it is always wrong. Non-fatal by construction — a learning-loop
    # bookkeeping failure must never cost a physician their approval.
    try:
        tprop = _tiering_proposal(store, user)
        # Admin Launch PRD §3.3 has the console POST /tiering/{id}/decide FIRST and
        # then /approve, so that agreement with the recommendation is recorded as a
        # training observation and not only disagreement. That makes this call a
        # potential DUPLICATE of an observation written moments ago: same
        # physician, same features, same tier. Folding both would double-count one
        # admin click into the likelihood and advance the pending-decision counter
        # by two per approval — silently, since nothing about a doubled batch looks
        # wrong from the outside.
        #
        # So: record here only when this judgment is not already queued. An API
        # client calling /approve on its own still produces its observation, which
        # is what keeps the learning loop honest for callers that never touch
        # /decide.
        if not store.has_unapplied_tiering_decision(user_id, tier):
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
                 "note": body.note or None},
    )
    # Approving a physician for labeling IS the decision that clears them for the
    # real de-identified cases — the two were separate flags, and the second one
    # had no UI, so the real queue stayed locked behind an approval nobody could
    # give. Best-effort and idempotent: a sync failure must never undo an approval
    # that already committed, and the startup backfill runs the same policy.
    try:
        store.sync_real_data_approval()
    except Exception:
        log.exception("[verify] real-data approval sync failed (decision stands)")
    # A referred physician reaching 'approved' is the
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
    # ── Onboarding v2 §5: the ONE added side-effect ──────────────────────────
    # Approval is where credentials come into existence. Before v2 a physician
    # chose a password during signup, so approval only had to say "you're in";
    # now the wizard has no password step and this is the moment the account
    # becomes usable at all.
    #
    # The password is TEMPORARY and rotated at first sign-in (§0.1 decision 1).
    # The ask was a permanent password in the email, and the doctor's experience
    # here is identical to that ask — credentials in the email, sign in from the
    # website, works first time. What differs is what is left behind: a permanent
    # plaintext credential sits in an inbox forever, survives an inbox breach,
    # and is the wrong answer to the security-posture question a hospital partner
    # and a SOC 2 auditor both ask. One extra screen buys all of that.
    #
    # Nothing about it can fail the approval, which has already committed above.
    temp_password: Optional[str] = None
    needs_credentials = _needs_credentials(user)
    if needs_credentials:
        try:
            # token_urlsafe(9) — 12 characters, ~72 bits. Long enough that it
            # cannot be guessed in the hours it is alive, short enough to retype
            # from a phone, which is where most of these emails are opened.
            temp_password = secrets.token_urlsafe(9)
            await run_in_threadpool(store.set_temp_password, user_id, temp_password)
            store.log_event(
                entity_type="user", entity_id=user_id,
                event_type="temp_password_issued", actor=admin["email"],
                # The password itself is NEVER in the payload. This row exists so
                # an auditor can see that a credential was minted and by whom,
                # which is the opposite of a place to write the credential down.
                payload={"reason": "approval"},
            )
        except Exception:
            log.exception("[verify] could not mint a temporary password "
                          "(approval stands; the physician has no credential yet)")
            temp_password = None

    welcome_sent = False
    if is_email_transport_configured():
        try:
            if temp_password:
                # §4.4 — the welcome. Carries the credentials, the mission block,
                # and the founders' intro link.
                welcome_sent = bool(await send_html_email(
                    user["email"],
                    application_welcome_subject((user.get("full_name") or "").strip()),
                    build_application_welcome_email(
                        full_name=(user.get("full_name") or "").strip(),
                        email=user["email"],
                        temp_password=temp_password,
                        sign_in_url=_portal_base() + "/asclepius",
                    ), importance_headers=True))
            elif not needs_credentials:
                # An account that already HAS a password. Once that meant an
                # invited member or a pre-v2 signup; since the wizard started
                # taking a password on screen one it means almost every
                # physician, which is what makes this branch load bearing.
                #
                # It used to fall through to the plain queued notice, and the
                # result was that choosing your own password silently cost you
                # the welcome: no mission block, no sign-in button, no founders'
                # Calendly, because of an implementation detail about where the
                # password came from. So the welcome is sent here too, with the
                # credentials card swapped for one line pointing at the password
                # they already have.
                welcome_sent = bool(await send_html_email(
                    user["email"],
                    application_welcome_subject((user.get("full_name") or "").strip()),
                    build_application_welcome_email(
                        full_name=(user.get("full_name") or "").strip(),
                        email=user["email"],
                        sign_in_url=_portal_base() + "/asclepius",
                    ), importance_headers=True))
                if welcome_sent:
                    # The hook on record_verification_decision has already queued
                    # the plain notice, because it cannot see that this handler
                    # is about to send the richer one. Void it. Two "you're
                    # approved" emails for one approval is the visible failure
                    # here, and this is the same mechanism the reject-then-
                    # approve race already uses.
                    try:
                        import notifications  # noqa: PLC0415
                        store.void_pending_admin_notification(
                            notifications._person_key(
                                "physician_approved",
                                f"approved:{user_id}",
                                user["email"]))
                    except Exception:
                        log.exception(
                            "[verify] could not void the queued approval notice; "
                            "this physician may receive two")
            # The remaining case — credentials were NEEDED and the mint failed —
            # sends nothing on purpose. "You're approved, open your workspace"
            # pointing at a door this physician has no key to is worse than
            # silence, and the response below tells the admin so.
        except Exception:
            log.exception("[verify] welcome email failed (decision stands)")
    # An approval whose welcome never left is an approval the physician does not
    # know about, and for a v2 application it is also an account they cannot sign
    # in to. The admin who clicked approve is the only person positioned to
    # notice, so say it here rather than only in a log they will not read.
    return {"ok": True, "user_id": user_id, "tier": tier,
            "verification_status": "approved",
            "verified_by": updated.get("verified_by"),
            "verified_at": updated.get("verified_at"),
            "credentials_issued": bool(temp_password),
            "welcome_email_sent": welcome_sent,
            "warning": (
                None if welcome_sent or not is_email_transport_configured()
                else "The approval is recorded, but the welcome email did not send. "
                     "The physician has not been told, and has no sign-in details."
            )}


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

    # A REJECTION OFFERS ANOTHER GO.
    #
    # The founders were explicit: a physician we turn down is not finished with.
    # They are asked to do the case work again, keeping every credential and CV
    # answer they already gave us, because none of that is what we are asking
    # them to redo.
    #
    # Two writes, in this order, and the order matters: the stamp is what makes
    # `access_level` return PROVISIONAL for a rejected row, so it has to be on
    # the account before the status lands or a concurrent sign-in in between
    # would be refused.
    current = store.get_tutorial_state(user_id) or {}
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    store.set_tutorial_state(user_id, {
        **current,
        "retake_offered_at": now,
        # Both pieces of case work reopen. `resources_seen_at` is cleared so
        # the demo and the practice case are offered again, which is the point:
        # somebody being asked to re-sit should be shown the help first, not
        # dropped straight back into the examination that went badly.
        "resources_seen_at": None,
        "exam": {"state": "retake", "attempt": int(
            ((current.get("exam") or {}).get("attempt") or 0)) + 1},
    })

    updated = store.record_verification_decision(
        user_id, status="rejected", decided_by=admin["email"], note=note)
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="verification_rejected",
        actor=admin["email"], payload={"note": note, "retake_offered": True},
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
            detail="No recognised specialty on file: the exam is drawn from your declared "
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
                 "reviewer. They are separate operational loops with separate owners, see "
                 "docs/PRD_C_LAUNCH_CHECKLIST.md."),
    }


# ─── Promotion ───────────────────────────────────────────────────────────────
# Until now the only writers of users.tier were approval-time and the restore
# backfill, so a physician's role was decided once, from credentials, before
# anybody had seen a single case they filed. The contributor score moved with
# their work and changed nothing. That is the wrong way round: a work record is
# better evidence about a reviewer than a CV is, and it was the evidence we were
# throwing away.
#
# An admin still decides. This endpoint surfaces candidates and records the
# decision; it does not promote anybody on a threshold. A score crossing 70 is a
# reason to look, not a reason to act, and automating it would make the number a
# target the moment somebody noticed it existed.

class RetierBody(BaseModel):
    tier: str
    note: str


@router.get("/retier-candidates")
async def retier_candidates(admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Approved labelers whose filed work reads like a reviewer's.

    Two conditions, both necessary. The score is the judgment; the case count is
    what makes the score mean anything, since a blended score over three cases
    is mostly the credential prior wearing a number.
    """
    store = _store()
    out: List[Dict[str, Any]] = []
    for u in store.list_verification_queue("approved"):
        if (u.get("tier") or "") != asc_caps.LABELER:
            continue
        stored = store.get_contributor_score(u["id"])
        if not stored:
            continue
        score, n_cases = stored.get("score"), int(stored.get("n_cases") or 0)
        if score is None or score < cscore.REVIEWER_BAND_MIN:
            continue
        if n_cases < asc_tiering.MEASURED_QUALITY_MIN_TASKS:
            continue
        out.append({
            "user_id": u["id"],
            "email": u["email"],
            "full_name": u.get("full_name"),
            "specialty": u.get("specialty"),
            "tier": u.get("tier"),
            "tier_word": asc_caps.tier_word(u.get("tier")),
            "n_cases": n_cases,
            # The score IS shown here, because this is an admin surface and the
            # figure is the whole reason the row is on the list. It never
            # crosses to anything a physician can reach.
            "score": score,
            "band": cscore.band_word(score),
        })
    out.sort(key=lambda r: (r["score"] or 0), reverse=True)
    return {
        "candidates": out,
        "criteria": {
            "min_score": cscore.REVIEWER_BAND_MIN,
            "min_cases": asc_tiering.MEASURED_QUALITY_MIN_TASKS,
        },
        "note": ("Candidates, not decisions. Promotion is a judgment an admin "
                 "makes; nothing here promotes anyone."),
    }


@router.post("/retier/{user_id}")
async def retier_physician(
    user_id: str, body: RetierBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Move an already-approved physician between labeler and reviewer.

    Deliberately not part of the approval endpoint. Approval decides whether
    somebody works here at all and carries a first tier as one of its outputs;
    this is the smaller, later thing, and keeping them separate means a
    promotion cannot accidentally re-run the side effects of an approval
    (credentials minted, welcome mail, community post).

    A note is required for the same reason a rejection requires one: in six
    months the only person who can explain a role change is whoever wrote it
    down at the time.
    """
    store = _store()
    user = _load_user_or_404(user_id)
    tier = (body.tier or "").strip().lower()
    note = " ".join((body.note or "").split())

    if tier not in _TIERS:
        raise HTTPException(status_code=400,
                            detail=f"tier must be one of {', '.join(_TIERS)}")
    if not note:
        raise HTTPException(status_code=400,
                            detail="Say why. A tier change with no reason cannot be reviewed later.")
    if (user.get("verification_status") or "") != "approved":
        raise HTTPException(
            status_code=422,
            detail=("This account has not been approved. A tier is part of the approval "
                    "decision; use approve for an undecided application."))

    previous = user.get("tier")
    if previous == tier:
        return {"ok": True, "unchanged": True, "user_id": user_id, "tier": tier,
                "tier_word": asc_caps.tier_word(tier)}

    # Promotion to reviewer re-checks the hard gates. The contributor score is
    # evidence about judgment; it is not evidence that somebody holds a licence,
    # and a work record can never buy past an exclusion or a failed identity
    # check. Demotion is never blocked: nothing about the gates should stop us
    # narrowing what an account can do.
    if tier == asc_caps.REVIEWER:
        gates = asc_tiering.hard_gates(user, leie_status=store.leie_status(user.get("npi")))
        if gates.get("failed"):
            raise HTTPException(
                status_code=422,
                detail=("Blocked by credential gates: "
                        + ", ".join(gates["failed"])
                        + ". A work record cannot substitute for these."))

    if not store.set_user_tier(user_id, tier):
        raise HTTPException(status_code=409, detail="The tier did not change. Reload and try again.")

    stored = store.get_contributor_score(user_id) or {}
    store.log_event(
        entity_type="user", entity_id=user_id, event_type="tier_changed",
        actor=admin["email"],
        payload={
            "from": previous, "to": tier, "note": note,
            # The evidence AS IT WAS at the moment of the decision. Recomputing
            # it later would answer a different question than the one the admin
            # actually acted on.
            "score_at_decision": stored.get("score"),
            "n_cases_at_decision": stored.get("n_cases"),
        },
    )

    # Promotion tells them. Demotion does not: those reasons are specific to the
    # work and belong in a conversation, not in a template that reports a drop
    # in standing and offers nobody to ask about it.
    emailed = False
    promoted = (previous == asc_caps.LABELER and tier == asc_caps.REVIEWER)
    if promoted and is_email_transport_configured():
        try:
            emailed = bool(await send_html_email(
                user["email"],
                "You're now a reviewer on Asclepius",
                build_asclepius_promoted_email(
                    full_name=(user.get("full_name") or "").strip(),
                    workspace_url=_portal_base() + "/asclepius",
                    tier_word=asc_caps.tier_word(tier),
                )))
        except Exception:
            log.exception("[verify] promotion email failed (the tier change stands)")

    return {"ok": True, "user_id": user_id, "from": previous, "tier": tier,
            "tier_word": asc_caps.tier_word(tier), "promoted": promoted,
            "email_sent": emailed}


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
