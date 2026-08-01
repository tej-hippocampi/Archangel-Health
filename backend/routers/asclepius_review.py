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
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
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
async def next_review(reviewer: Dict[str, Any] = Depends(require_reviewer)):
    """Draw + claim the oldest reviewable submission for this reviewer.

    The store query already excludes the reviewer's own submissions and anything
    they reviewed before; the claim is compare-and-set so two reviewers drawing
    concurrently can never hold the same submission. The served payload is the
    blinded whitelist view — no labeler identity, asserted by test (PRD A §4)."""
    store = _store()
    # Lazy, bounded routing sweep: decide double-labeling for tasks whose first
    # label has landed. Never fatal to the draw.
    try:
        asc_review.sweep_double_label_routing(store, limit=100)
    except Exception:
        log.exception("asclepius-review: double-label routing sweep failed")

    lease = asc_review.review_lease_minutes()
    for _ in range(5):  # claim race: lose the CAS -> draw the next candidate
        sub = store.next_review_for(
            reviewer["id"],
            specialty=reviewer.get("specialty"),
            lease_minutes=lease,
            predicate=asc_review.needs_review,
        )
        if sub is None:
            return {"submission": None, "message": "No submissions awaiting review."}
        if not store.claim_submission_for_review(sub["submission_id"], lease_minutes=lease):
            continue
        task = store.get_task(sub["task_id"])
        if task is None:  # orphaned submission — release the claim, keep drawing
            store.update_submission(sub["submission_id"], review_status=None)
            continue
        store.log_event(
            entity_type="submission",
            entity_id=sub["submission_id"],
            event_type="review_claimed",
            actor=reviewer["id"],
            payload={"lease_minutes": lease},
        )
        return {"submission": asc_review.blinded_review_view(task, sub)}
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
        # 1: this portal only ever served the whitelist view, which carries no
        # labeler identity (asserted by test — PRD A §4).
        blinded=True,
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
