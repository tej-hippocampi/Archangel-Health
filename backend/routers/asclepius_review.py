"""Review portal router (PRD A) — the senior-review surface.

A purpose-built, fast surface for anyone holding the REVIEW capability
(``reviewer`` and ``advisor`` tiers — see ``asclepius.capabilities``):
draw the oldest reviewable submission (blinded — no labeler identity), submit a
per-dimension verdict, and expose the double-label pointer that routes a second
INDEPENDENT labeler for the real-κ slice. Policy lives in ``asclepius.review``;
persistence in the PRD-A sentinel block of ``asclepius.store``. This router
never touches ``routers/asclepius.py`` (START_HERE §3.1).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import review as asc_review
from asclepius import routing as asc_routing
from asclepius.store import get_store

log = logging.getLogger("asclepius.review")

# ─── The Agent P seam (PRD R §4 / context pack §3.1) ──────────────────────────
# A review session is a BILLING primitive, not a review primitive. R decides what
# a reviewer sees; P decides whether they get paid for the time. The ENTIRE
# integration is: call open_session when a reviewer draws their first pair, and
# forward whatever P returns to the client, opaquely.
#
# Forwarded opaquely on purpose. Naming P's fields here would (a) put payment
# vocabulary in a review module, which §4 forbids, and (b) make R the second
# place that has to change when P adds a field. This router therefore never
# reads P's tables, never computes elapsed time, never decides whether a session
# has met its threshold, and never writes a payout row. ``tests/
# test_paired_review.py`` greps this file for those concepts and fails if any of
# them appear here — including in a comment, which is why this one paraphrases.
#
# The import is guarded because R merges LAST: on a tree where P has not landed
# yet the review flow must still work, minus the countdown, rather than 500.
try:  # pragma: no cover - exercised by whichever tree it lands in
    from asclepius import payments as _asc_payments
except Exception:  # ImportError before Agent P merges
    _asc_payments = None


def _open_review_session(store: Any, user_id: str) -> Optional[Dict[str, Any]]:
    """Open (or re-open, idempotently) this reviewer's billable session.

    Failure is non-fatal by design: a physician must never be blocked from
    reviewing because the payments module is absent or unhappy. The absence is
    logged and surfaced to the client as "no session", which the page renders as
    no countdown — visibly missing rather than silently wrong."""
    if _asc_payments is None:
        return None
    try:
        return _asc_payments.open_session(store, user_id=user_id, kind="review")
    except Exception:
        log.exception("asclepius-review: could not open a review session")
        return None

router = APIRouter(tags=["asclepius-review"])

# ─── Preview draws (PRD-1 §4.1) ───────────────────────────────────────────────
# An operator must be able to SEE the reviewer surface without adjudicating a
# real pair. A preview draw therefore:
#
#   * claims nothing — the pair stays at the head of a real reviewer's queue;
#   * opens no billable session — nobody is being paid to sightsee;
#   * carries a draw token whose prefix the submit endpoint REFUSES with a 409.
#
# The refusal is loud on purpose. A silent no-op would leave an operator
# believing they had recorded a judgment, and a real submit would grade a
# physician's work by an admin who was looking around — with ``blinded`` false
# on a row nobody meant to create.
PREVIEW_TOKEN_PREFIX = "preview:"


def _preview_draw_token(task_id: str) -> str:
    """The marker a preview draw hands the client and the submit path refuses.

    Not a credential and deliberately not secret: it is a statement about which
    DRAW produced this payload, and the only thing it is used for is refusing.
    A guessable value cannot widen anything — the worst a forged one achieves is
    the client refusing its own submission.
    """
    return PREVIEW_TOKEN_PREFIX + str(task_id or "")


def _is_preview_operator(user: Dict[str, Any]) -> bool:
    """True when this account reaches review ONLY through the admin override.

    ``capabilities.can`` admits every admin to every surface, so an operator with
    no reviewer tier can already open this queue — and, before §4.1, could
    adjudicate through it. Their TIER is what says whether they are a reviewer;
    the override says they may look. Served to the client so the banner and the
    preview draw follow the same rule the server enforces, rather than the
    frontend re-deriving a tier check it must not own.
    """
    return not (asc_caps.REVIEW in asc_caps.capabilities(user))


def _store():
    return get_store()


# ─── Off-request routing sweep (FIX A A-3.4) ──────────────────────────────────
# Deciding double-labeling is fleet-wide bookkeeping, not part of serving one
# reviewer one case. Throttled so N reviewers drawing concurrently trigger at
# most one sweep per interval, and run in a background task so no draw waits on
# it. PRD A's constraint is a senior physician accepting in under 60 seconds.
_SWEEP_STATE: Dict[str, float] = {"last": 0.0}


def _sweep_interval_sec() -> float:
    try:
        return max(0.0, float(os.getenv("ASCLEPIUS_REVIEW_SWEEP_SEC", "60")))
    except ValueError:
        return 60.0


def _sweep_due() -> bool:
    now = time.monotonic()
    if now - _SWEEP_STATE["last"] < _sweep_interval_sec():
        return False
    _SWEEP_STATE["last"] = now      # claim the slot BEFORE running, not after
    return True


def _run_sweep() -> None:
    try:
        asc_review.sweep_double_label_routing(_store(), limit=100)
    except Exception:
        log.exception("asclepius-review: double-label routing sweep failed")


def require_reviewer(
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
) -> Dict[str, Any]:
    """Admits any tier carrying the REVIEW capability (``reviewer``, ``advisor``)
    — or an admin, so the portal is operable/demoable before the verification
    flow has assigned any tiers. A NULL tier denies: 'not yet assigned' is not a
    grant (PRD A §1.2). The tier set is NOT written here; it lives in
    ``capabilities.py`` (Advisor PRD §2.2) so a new tier is one edit, not a
    codebase-wide literal hunt."""
    if not asc_review.can_review(user):
        raise HTTPException(
            status_code=403, detail="Reviewer or advisor tier required")
    return user


# ─── Page ─────────────────────────────────────────────────────────────────────
@router.get("/asclepius/review")
async def review_portal_page():
    """PRD-1 §2.1: there is no standalone review page any more.

    Review is a VIEW INSIDE the evaluation portal — same shell, same rail, same
    session, same design system — so this route redirects into it rather than
    404ing. The old URL is in physicians' bookmarks and in email we have already
    sent; a dead link is a reviewer who cannot find their work.
    """
    return RedirectResponse(url="/asclepius#review", status_code=307)


# ─── Session / vocabulary ─────────────────────────────────────────────────────
@router.get("/api/asclepius/review/me")
async def review_me(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Who am I, for the review portal boot. Served to ANY authenticated portal
    user (not just reviewers) so the page can render an honest 'not a reviewer'
    state instead of a bare 403."""
    return {
        "user": asc_auth.public_user(user),
        "tier": user.get("tier"),
        "can_review": asc_review.can_review(user),
        "dimensions": asc_review.REVIEW_DIMENSIONS,
        "dimension_states": list(asc_review.DIMENSION_STATES),
        "verdicts": list(asc_review.REVIEW_VERDICTS),
        # PRD R §2.3 — served so the page and the server can never disagree
        # about the paired vocabulary, exactly as with the dimensions.
        "strength_choices": list(asc_review.PAIR_STRENGTH),
        "paired": True,
        # PRD-1 §4.1. True when this account can only reach review through the
        # admin override, so the surface renders read-only with a banner and
        # draws in preview mode. Decided HERE, not in the frontend: "is this a
        # real reviewer or an operator looking around" is a tier question, and
        # the tier table is not the client's to interpret.
        "preview_only": asc_review.can_review(user) and _is_preview_operator(user),
    }


@router.get("/api/asclepius/review/stats")
async def review_stats(_reviewer: Dict[str, Any] = Depends(require_reviewer)):
    stats = _store().review_queue_stats()
    # PRD R: the phases a TR can actually act on, alongside the submission-level
    # counts the single-submission flow still reports. Merged rather than
    # replaced — the old keys have readers.
    stats.update(_store().review_pair_queue_stats())
    return stats


# ─── Draw a PAIR (PRD R §2.1) ─────────────────────────────────────────────────
@router.get("/api/asclepius/review/pair/next")
async def next_review_pair(
    background: BackgroundTasks = None,
    preview: bool = Query(
        False,
        description="Operator preview (PRD-1 §4.1): serve a pair WITHOUT "
                    "claiming it, open no session, and mark the response so a "
                    "submit against it is refused.",
    ),
    reviewer: Dict[str, Any] = Depends(require_reviewer),
):
    """Draw + claim the oldest ``review_ready`` CASE for this reviewer.

    Returns the case with BOTH labels side by side, blinded, in an A/B order
    seeded on ``(task_id, reviewer_id)`` — stable across reloads so a refresh
    does not swap the columns, and uncorrelated with submission order so position
    never leaks who went first.

    The store query already excludes cases this reviewer labelled or reviewed;
    the claim is compare-and-set on the TASK, so two reviewers drawing
    concurrently can never hold the same pair.

    PREVIEW (§4.1) is a different draw, not a flag on this one: no claim, no
    session, and a draw token the submit path refuses. Restricted to operators —
    a real reviewer asking for a preview would be asking to work for free, and a
    labeler asking for one would be asking to read a pair they were excluded
    from. An operator whose tier does not carry review is FORCED into it, which
    is the whole point: clicking through the reviewer surface must not grade a
    physician's work by accident.
    """
    store = _store()
    forced_preview = _is_preview_operator(reviewer)
    preview = bool(preview) or forced_preview
    if preview and not (reviewer.get("role") == "admin" or forced_preview):
        raise HTTPException(
            status_code=403,
            detail="Preview draws are for operators; draw a real pair instead.")
    # Same off-request discipline as the single-submission draw: the fleet-wide
    # routing sweep is throttled and handed to a background task so no reviewer
    # waits on it. Under the paired flow it is a SECOND path to capacity — the
    # labeler queue lifts capacity synchronously as it serves — so its latency
    # can no longer hide a case from the priority queue.
    if background is not None and not preview and _sweep_due():
        background.add_task(_run_sweep)

    lease = asc_review.review_lease_minutes()
    seen: set = set()
    for _ in range(5):  # claim race: lose the CAS -> draw the next candidate
        task = store.next_review_pair_for(
            reviewer["id"], specialty=reviewer.get("specialty"), lease_minutes=lease)
        if task is None:
            return {"pair": None, "message": "No cases awaiting review."}
        if task["task_id"] in seen:
            break                       # never revisit a candidate within one draw
        seen.add(task["task_id"])

        subs = [s for s in store.submissions_for_task(task["task_id"]) if s.get("verdict")]
        if not asc_routing.pair_is_independent(subs):
            # Two labels by one physician is not a pair. Both queues exclude
            # prior submitters in SQL so this should be unreachable — which is
            # exactly why it is checked where the pair is served rather than
            # assumed. Park it so it stops re-occupying the head of the queue
            # (the A-3.2 lesson), and say so loudly: reaching this line means a
            # SQL independence invariant broke, and κ's whole premise with it.
            log.error(
                "asclepius-review: task %s has two labels from one physician; "
                "parking it rather than serving a pair that is not independent",
                task["task_id"])
            # A PREVIEW WRITES NOTHING (§4.1) — not even a park. An operator
            # looking around must not retire a case two physicians were paid
            # for; the next real draw parks it, which is where that decision
            # belongs.
            if not preview:
                store.mark_task_reviewed(task["task_id"])
                store.log_event(
                    entity_type="task", entity_id=task["task_id"],
                    event_type="review_pair_rejected", actor=reviewer["id"],
                    payload={"reason": "not_independent"},
                )
            continue

        shown_a, shown_b = asc_routing.ab_pair(
            subs, task_id=task["task_id"], reviewer_id=reviewer["id"])
        view = asc_review.blinded_pair_view(task, shown_a, shown_b)
        labelers = [store.get_user_by_id(s.get("evaluator_id") or "") for s in subs]
        # Redact FIRST, then derive blinding from what we are actually about to
        # serve (PRD R §2.2). Measuring a leak and recording blinded=0 satisfies
        # the statistic but not the requirement — the reviewer has already read
        # the name by then, and the adjudication is already biased.
        view, redacted = asc_review.redact_identity(view, labelers)
        blinded = asc_review.pair_is_blinded(
            view, reviewer_role=reviewer.get("role") or "", labelers=labelers)
        if preview:
            # No claim, no event, no session. The pair stays exactly where it was
            # in a real reviewer's queue, and the token below is what the submit
            # path refuses. ``blinded`` still ships so the surface renders
            # honestly — an operator is never blind, and the banner says so.
            return {
                "pair": {**view, "blinded": blinded, "preview": True,
                         "draw_token": _preview_draw_token(task["task_id"])},
                "preview": True,
                "session": None,
            }
        if redacted:
            # Never the strings themselves — that would put the identity into the
            # event log, one hop from where it was removed.
            store.log_event(
                entity_type="task", entity_id=task["task_id"],
                event_type="review_pair_redacted", actor=reviewer["id"],
                payload={"n_redactions": len(redacted)},
            )

        if not store.claim_task_for_review(
            task["task_id"], reviewer_id=reviewer["id"],
            blinded=blinded, lease_minutes=lease,
        ):
            continue  # lost the CAS to a concurrent draw — take the next one
        store.log_event(
            entity_type="task", entity_id=task["task_id"], event_type="review_pair_claimed",
            actor=reviewer["id"], payload={"lease_minutes": lease, "blinded": blinded},
        )
        return {
            "pair": {**view, "blinded": blinded},
            # Opaque passthrough of Agent P's session (PRD R §4). None before P
            # merges, or if P is unhappy — the page renders no countdown rather
            # than a fabricated one.
            "session": _open_review_session(store, reviewer["id"]),
        }
    return {"pair": None, "message": "Queue is contended; try again."}


# ─── Draw ─────────────────────────────────────────────────────────────────────
@router.get("/api/asclepius/review/next")
async def next_review(
    background: BackgroundTasks = None,
    reviewer: Dict[str, Any] = Depends(require_reviewer),
):
    """Draw + claim the oldest reviewable submission for this reviewer.

    The store query already excludes the reviewer's own submissions and anything
    they reviewed before; the claim is compare-and-set so two reviewers drawing
    concurrently can never hold the same submission. The served payload is the
    blinded whitelist view — no labeler identity, asserted by test (PRD A §4)."""
    store = _store()
    # The double-label routing sweep is OFF the request's critical path
    # (FIX A A-3.4): it is throttled to at most once per interval and handed to
    # a background task, so a reviewer's draw never waits on it and it cannot
    # monopolize the single SQLite writer that labeler submissions also need.
    if background is not None and _sweep_due():
        background.add_task(_run_sweep)

    lease = asc_review.review_lease_minutes()
    seen: set = set()
    for _ in range(5):  # claim race: lose the CAS -> draw the next candidate
        sub = store.next_review_for(
            reviewer["id"],
            specialty=reviewer.get("specialty"),
            lease_minutes=lease,
            predicate=asc_review.needs_review,
            # Declined submissions are marked so they stop re-occupying the scan
            # window on every future draw (A-3.3).
            persist_routing_decision=True,
        )
        # Belt to A-3.2's braces: never revisit a candidate within one draw, so
        # no single row can consume all five attempts.
        if sub is not None and sub["submission_id"] in seen:
            break
        if sub is not None:
            seen.add(sub["submission_id"])
        if sub is None:
            return {"submission": None, "message": "No submissions awaiting review."}
        task = store.get_task(sub["task_id"])
        if task is None:
            # Orphaned submission (its task is gone). Releasing to NULL would
            # make it the OLDEST eligible row again and the next iteration would
            # draw the same orphan — five loops, then a permanently contended
            # queue for every reviewer (FIX A A-3.2). Park it in its own terminal
            # state, which the SQL excludes.
            store.update_submission(sub["submission_id"], review_status="orphaned")
            store.log_event(
                entity_type="submission", entity_id=sub["submission_id"],
                event_type="review_orphaned", actor=reviewer["id"],
                payload={"reason": "task_missing"},
            )
            continue

        # Build the payload FIRST, then derive blinding from the bytes we are
        # actually about to serve, then persist that derivation on the claim.
        # An asserted constant is precisely the defect F2 named.
        view = asc_review.blinded_review_view(task, sub)
        labeler = store.get_user_by_id(sub.get("evaluator_id") or "")
        blinded = asc_review.payload_is_blinded(
            view, reviewer_role=reviewer.get("role") or "", labeler=labeler)

        if not store.claim_submission_for_review(
            sub["submission_id"], reviewer_id=reviewer["id"],
            blinded=blinded, lease_minutes=lease,
        ):
            continue  # lost the CAS to a concurrent draw — take the next one
        store.log_event(
            entity_type="submission",
            entity_id=sub["submission_id"],
            event_type="review_claimed",
            actor=reviewer["id"],
            payload={"lease_minutes": lease, "blinded": blinded},
        )
        # Serve the derived value so the client can show an honest banner; the
        # recorded flag comes from the claim, not from anything the client says.
        return {"submission": {**view, "blinded": blinded}}
    return {"submission": None, "message": "Queue is contended; try again."}


# ─── Submit ───────────────────────────────────────────────────────────────────
class ReviewSubmitBody(BaseModel):
    verdict: str
    dimensions: Dict[str, str] = {}
    corrections: Optional[Dict[str, Any]] = None
    reviewer_notes: Optional[str] = None
    time_spent_sec: Optional[int] = None


@router.post("/api/asclepius/review/{submission_id}")
async def submit_review(
    submission_id: str,
    body: ReviewSubmitBody,
    reviewer: Dict[str, Any] = Depends(require_reviewer),
):
    store = _store()
    sub = store.get_submission(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    # Belt and braces: the queue can never serve own work (enforced in SQL), but
    # a hand-crafted POST must hit the same wall.
    if sub.get("evaluator_id") == reviewer["id"]:
        raise HTTPException(status_code=403, detail="You cannot review your own submission")
    if store.has_review_by(submission_id, reviewer["id"]):
        raise HTTPException(status_code=409, detail="You already reviewed this submission")

    # Audit R H2. The single-review queue's predicates — one label, never lifted
    # to a pair — were checked at DRAW time and never again, while the labeler
    # queue deliberately kept the same task servable to a second labeler. So a
    # reviewer could be reading a submission at the moment its case became a
    # pair, and their POST would land: two case_reviews rows for one case, and an
    # expert-acceptance rate counting it twice.
    #
    # The claim check below would already 409 this once the pair review retires
    # the submission — but with "your claim is missing or has expired", which is
    # FALSE and offers no way forward. Say what actually happened, and hand their
    # judgment back so the pair review can be finished rather than retyped.
    _task = store.get_task(sub["task_id"]) or {}
    _labels = [s for s in store.submissions_for_task(sub["task_id"]) if s.get("verdict")]
    if len(_labels) >= asc_routing.PAIR_LABELS or asc_routing.target_labels(_task) >= asc_routing.PAIR_LABELS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "became_a_pair",
                "message": "This case became a pair while you were reading it. It is "
                           "reviewed against both labels together — your judgment is "
                           "below, ready to carry over.",
                "task_id": sub["task_id"],
                "your_review": body.model_dump(),
            },
        )

    errors = asc_review.validate_review_payload(body.model_dump())
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    # The claim is the authority on this review (FIX A A-3.1). Without this,
    # any reviewer could POST onto any submission id they can guess — including
    # one another reviewer currently holds, evicting their in-flight work, and
    # including one the routing policy excluded.
    lease = asc_review.review_lease_minutes()
    claim = store.review_claim(submission_id, lease_minutes=lease)
    if claim["status"] != "in_review" or claim["expired"]:
        raise HTTPException(
            status_code=409,
            detail="Draw this submission for review before submitting; your claim "
                   "is missing or has expired.",
        )
    if claim["holder"] != reviewer["id"]:
        raise HTTPException(
            status_code=409, detail="Another reviewer currently holds this submission.")

    # Scan the reviewer's OWN prose for Safe-Harbor identifiers before it is
    # stored (FIX A A-5.3). The review is never rejected over this — a physician
    # should not lose clinical judgment to a scanner — but a flagged review has
    # its free text withheld from the buyer-facing block, and the reviewer is
    # told so they can rewrite it.
    identifier_flags = asc_review.scan_review_free_text(
        body.reviewer_notes, body.corrections)

    review = store.insert_case_review(
        task_id=sub["task_id"],
        submission_id=submission_id,
        reviewer_user_id=reviewer["id"],
        reviewer_id_hashed=reviewer.get("id_hashed") or "",
        verdict=body.verdict,
        dimensions=body.dimensions,
        corrections=body.corrections,
        reviewer_notes=(body.reviewer_notes or "").strip() or None,
        time_spent_sec=body.time_spent_sec,
        # DERIVED at draw time from the payload actually served, and read back
        # here from the claim (FIX A F2). Deliberately NOT recomputed from a
        # payload we are no longer serving — that would reintroduce the same
        # assumption in a new place. Tri-state: None means no draw ever asserted
        # it, which is excluded from κ as unverified rather than treated as blind.
        blinded=claim["blinded"],
        identifier_flags=identifier_flags,
    )
    store.update_submission(submission_id, review_status="reviewed")
    store.log_event(
        entity_type="submission",
        entity_id=submission_id,
        event_type="review_submitted",
        actor=reviewer["id"],
        payload={
            "review_id": review["review_id"],
            "verdict": body.verdict,
            "task_id": sub["task_id"],
        },
    )
    # The verdict just graded the labeler's work: fold it into their
    # contributor score. Best-effort inside; a scoring failure never takes
    # the review down with it.
    from asclepius import contributor_score as _cscore  # noqa: PLC0415
    _cscore.recompute_for_submission(store, submission_id)
    return {
        "review": review,
        "review_status": "reviewed",
        # Surfaced so the reviewer can rewrite the note rather than discovering
        # months later that their correction never shipped.
        "identifier_flags": identifier_flags,
        "corrections_withheld": bool(identifier_flags),
    }


# ─── Submit a PAIRED adjudication (PRD R §2.3) ────────────────────────────────
class PairReviewSubmitBody(BaseModel):
    verdict: str
    stronger: str
    accepted_side: Optional[str] = None
    dimensions: Dict[str, str] = {}
    corrections: Optional[Dict[str, Any]] = None
    reviewer_notes: Optional[str] = None
    time_spent_sec: Optional[int] = None
    # PRD-1 §3 — the reasoning-step forks and which side the reviewer judged
    # correct at each. ``judged`` is a POSITION in what this reviewer was shown
    # and is canonicalized below, exactly like ``stronger``. Absent is valid; an
    # array is admitted only when both labels carried reasoning steps.
    step_divergence: Optional[List[Dict[str, Any]]] = None
    # PRD-1 §4.1 — echoed back from the draw. A preview token is REFUSED here
    # with a 409, never quietly dropped.
    draw_token: Optional[str] = None


@router.post("/api/asclepius/review/pair/{task_id}")
async def submit_pair_review(
    task_id: str,
    body: PairReviewSubmitBody,
    reviewer: Dict[str, Any] = Depends(require_reviewer),
):
    """The reviewer's adjudication of a pair: which is stronger, and is either
    right.

    ``accepted_side`` is a POSITION ('A' or 'B') in what this reviewer was shown.
    The server re-derives the same seeded A/B map it served and resolves the
    position to a submission id, so the client never learns — and never gets to
    assert — whose work it accepted.
    """
    store = _store()
    # PRD-1 §4.1, FIRST, before anything is read or written. A preview draw is
    # refused loudly — 409, not a silent no-op — so an operator who clicked
    # through the reviewer surface cannot come away believing they recorded a
    # judgment, and a physician's work is never graded by someone sightseeing.
    #
    # Belt AND braces: a preview never claimed the task, so the claim check below
    # would refuse it too. That refusal says "your claim has expired", which is
    # true but useless. This one says what actually happened.
    if (body.draw_token or "").startswith(PREVIEW_TOKEN_PREFIX):
        raise HTTPException(
            status_code=409,
            detail="This pair was drawn in preview. Nothing submitted from a "
                   "preview is recorded — draw a real pair to adjudicate.")
    if _is_preview_operator(reviewer):
        raise HTTPException(
            status_code=409,
            detail="This account previews the reviewer surface and cannot record "
                   "an adjudication. Nothing you submit here is recorded.")

    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    subs = [s for s in store.submissions_for_task(task_id) if s.get("verdict")]
    if len(subs) < asc_routing.PAIR_LABELS:
        raise HTTPException(
            status_code=409, detail="This case does not yet carry two independent labels")
    if len(subs) > asc_routing.PAIR_LABELS:
        # Audit R H3: a paired review is defined over exactly two labels. Serving
        # two of three silently drops a physician's work and then retires it.
        raise HTTPException(
            status_code=409,
            detail=f"This case carries {len(subs)} labels; a paired review "
                   f"adjudicates exactly two.")
    # One adjudication per case, from either queue (Audit R H2). The draw already
    # excludes it; a hand-crafted POST must hit the same wall, or the acceptance
    # rate counts one case twice.
    if store.reviews_for_task(task_id):
        raise HTTPException(status_code=409, detail="This case has already been adjudicated")
    # Belt and braces: both queues exclude own work in SQL, but a hand-crafted
    # POST must hit the same wall.
    if any(s.get("evaluator_id") == reviewer["id"] for s in subs):
        raise HTTPException(status_code=403, detail="You cannot review your own submission")
    if store.has_task_review_by(task_id, reviewer["id"]):
        raise HTTPException(status_code=409, detail="You already reviewed this case")

    # PRD R §2.3: 400, server-side, on an unexplained rejection or an
    # "accept with edits" with no edits. Same rule as the single flow, and for
    # the same reason — it is unusable to the labeler, the buyer, and us.
    # §3: whether ``step_divergence`` is a real measurement or a fabrication is a
    # fact about THE PAIR, so the two traces are read here and handed to the pure
    # validator. Both sides must have carried reasoning steps, or the array is
    # rejected rather than dropped.
    _steps = [asc_review.submission_reasoning_steps(s) for s in subs]
    pair_steps = {
        "both": all(bool(st) for st in _steps),
        "n_steps": max((len(st) for st in _steps), default=0),
    }
    errors = asc_review.validate_pair_review_payload(
        body.model_dump(), pair_steps=pair_steps)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # The claim is the authority (FIX A A-3.1, moved to the task): without it any
    # reviewer could POST onto a guessed task id, evicting in-flight work.
    lease = asc_review.review_lease_minutes()
    claim = store.task_review_claim(task_id, lease_minutes=lease)
    if claim["status"] != "in_review" or claim["expired"]:
        raise HTTPException(
            status_code=409,
            detail="Draw this case for review before submitting; your claim is "
                   "missing or has expired.",
        )
    if claim["holder"] != reviewer["id"]:
        raise HTTPException(
            status_code=409, detail="Another reviewer currently holds this case.")

    identifier_flags = asc_review.scan_review_free_text(
        body.reviewer_notes, body.corrections)

    pair_a, pair_b = asc_routing.canonical_pair_ids(subs)
    accepted = asc_routing.resolve_side(
        body.accepted_side, subs, task_id=task_id, reviewer_id=reviewer["id"])
    # Audit R H1: ``stronger`` arrives as a position in what THIS reviewer was
    # shown. Resolve it to a submission, then re-express it in the canonical
    # frame so it means the same thing as the pair columns it is stored beside.
    # 'equivalent' names no side and stays as-is.
    stronger_sub = asc_routing.resolve_side(
        body.stronger, subs, task_id=task_id, reviewer_id=reviewer["id"])
    stronger = asc_routing.canonical_side(stronger_sub, subs) or body.stronger

    # §3, same frame problem as H1: every ``judged`` side arrives as a position in
    # what THIS reviewer was shown. Re-expressed in canonical terms — and stored
    # alongside the submission id, the form no reader can misinterpret — so two
    # reviewers' forks on one case mean the same thing.
    step_divergence = asc_review.canonicalize_step_divergence(
        body.step_divergence,
        resolve=lambda side: asc_routing.resolve_side(
            side, subs, task_id=task_id, reviewer_id=reviewer["id"]),
        canonical=lambda sid: asc_routing.canonical_side(sid, subs),
    )

    review = store.insert_pair_review(
        task_id=task_id,
        reviewer_user_id=reviewer["id"],
        reviewer_id_hashed=reviewer.get("id_hashed") or "",
        verdict=body.verdict,
        stronger=stronger,
        stronger_submission_id=stronger_sub,
        pair_sub_a=pair_a,
        pair_sub_b=pair_b,
        accepted_submission_id=accepted,
        dimensions=body.dimensions,
        corrections=body.corrections,
        reviewer_notes=(body.reviewer_notes or "").strip() or None,
        time_spent_sec=body.time_spent_sec,
        # Read back from the claim, DERIVED at draw time from the payload
        # actually served. Deliberately not recomputed from a payload we are no
        # longer serving — that would reintroduce the assumption in a new place.
        blinded=claim["blinded"],
        identifier_flags=identifier_flags,
        # NULL when either label carried no reasoning steps (nothing was
        # compared); '[]' when both did and they agreed at every step. The two
        # are different findings and are stored as different values.
        step_divergence=step_divergence,
    )
    # The task status and the retirement of the two labels commit INSIDE
    # ``insert_pair_review`` (Audit R M2) — they used to be three more writes
    # here, on three more connections, so a crash between them left a review row
    # counting in the acceptance rate beside a task still `in_review`.
    store.log_event(
        entity_type="task", entity_id=task_id, event_type="review_pair_submitted",
        actor=reviewer["id"],
        payload={"review_id": review["review_id"], "verdict": body.verdict,
                 "stronger": body.stronger},
    )
    # The adjudication just graded both labelers' work: fold it into their
    # contributor scores. Best-effort inside; never takes the review down.
    from asclepius import contributor_score as _cscore  # noqa: PLC0415
    for sid in (pair_a, pair_b):
        if sid:
            _cscore.recompute_for_submission(store, sid)
    return {
        "review": review,
        "review_status": "reviewed",
        "identifier_flags": identifier_flags,
        "corrections_withheld": bool(identifier_flags),
    }


# ─── Double-label pointer (the real-κ slice) ──────────────────────────────────
@router.get("/api/asclepius/review/double-label/next")
async def next_double_label(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """The next task flagged for a second INDEPENDENT label that THIS user may
    take. Any evaluator can serve as the second labeler — it is labeling work,
    not review work. The store query excludes the first labeler and anyone who
    already submitted (independence is enforced in SQL, PRD A §1.3), and the V4
    wall holds: real cases only for real-data-approved users. The labeling
    itself happens in the main evaluator portal; this returns a pointer."""
    store = _store()
    task = store.next_double_label_for(
        user["id"],
        specialty=user.get("specialty"),
        allow_real=bool(user.get("real_data_approved")),
    )
    if task is None:
        return {"task": None}
    return {
        "task": {
            "task_id": task["task_id"],
            "specialty": task.get("specialty"),
            "difficulty": task.get("difficulty"),
            "modality": task.get("modality"),
            "case_source": task.get("case_source"),
        },
        "portal_url": "/asclepius",
    }
