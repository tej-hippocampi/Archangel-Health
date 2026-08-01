"""Review portal router (PRD A) — the senior-reviewer tier's HTTP surface.

A purpose-built, fast surface for reviewers (``users.tier == 'reviewer'``):
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
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from asclepius import auth as asc_auth
from asclepius import review as asc_review
from asclepius.store import get_store

log = logging.getLogger("asclepius.review")

router = APIRouter(tags=["asclepius-review"])

_REVIEW_HTML = os.path.join(
    os.path.dirname(__file__), "..", "..", "frontend", "asclepius", "review.html"
)


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
    """Admits an explicit ``tier == 'reviewer'`` — or an admin, so the portal is
    operable/demoable before the verification flow has assigned any tiers. A NULL
    tier denies: 'not yet assigned' is not 'reviewer' (PRD A §1.2)."""
    if user.get("role") == "admin":
        return user
    if not asc_review.can_review(user):
        raise HTTPException(status_code=403, detail="Reviewer tier required")
    return user


# ─── Page ─────────────────────────────────────────────────────────────────────
@router.get("/asclepius/review", response_class=HTMLResponse)
async def review_portal_page():
    """The review portal shell. Served unauthenticated by design (same pattern
    as ``/asclepius`` and ``/asclepius/v5/annotate``): the page JS gates on the
    Asclepius token and shows its own sign-in. No PHI in the shell."""
    if not os.path.exists(_REVIEW_HTML):
        raise HTTPException(status_code=404, detail="Review portal not built")
    with open(_REVIEW_HTML, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ─── Session / vocabulary ─────────────────────────────────────────────────────
@router.get("/api/asclepius/review/me")
async def review_me(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Who am I, for the review portal boot. Served to ANY authenticated portal
    user (not just reviewers) so the page can render an honest 'not a reviewer'
    state instead of a bare 403."""
    return {
        "user": asc_auth.public_user(user),
        "tier": user.get("tier"),
        "can_review": asc_review.can_review(user) or user.get("role") == "admin",
        "dimensions": asc_review.REVIEW_DIMENSIONS,
        "dimension_states": list(asc_review.DIMENSION_STATES),
        "verdicts": list(asc_review.REVIEW_VERDICTS),
    }


@router.get("/api/asclepius/review/stats")
async def review_stats(_reviewer: Dict[str, Any] = Depends(require_reviewer)):
    return _store().review_queue_stats()


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
    return {"review": review, "review_status": "reviewed"}


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
