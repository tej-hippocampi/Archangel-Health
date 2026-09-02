"""The contributor-score surface (PRD-SCORE).

One endpoint, and it is admin-only. There used to be a second, session-scoped
one so a physician could read their own rating; it is gone.

The score is INTERNAL. It exists to route work and to inform an approval
decision, and the meeting that set this product's direction was explicit that
a physician never sees it. The portal already stopped rendering it, but the
route stayed reachable by any signed-in session, so the number was one curl
away from the person it is a judgment about. A surface nothing renders is
still a surface; removing the route is what makes the rule true rather than
merely observed.

What the physician sees instead is what they can act on: their verification
status, their tier and what work it opens, and their own case history. Those
live on the profile and dashboard endpoints.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from asclepius import auth as asc_auth
from asclepius import contributor_score as cscore
from asclepius.store import get_store

log = logging.getLogger("asclepius.score")

router = APIRouter(tags=["asclepius-score"])


def _store():
    return get_store()


@router.get("/api/asclepius/admin/scores/{user_id}")
async def admin_score(
    user_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The full picture for one physician: current score, component
    breakdown, and the per-case trajectory."""
    store = _store()
    result = cscore.compute(store, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No such physician.")
    return {
        **result,
        "band": cscore.band_word(result["score"]),
        "history": store.contributor_score_history(user_id),
        "bands": {"reviewer": cscore.REVIEWER_BAND_MIN,
                  "labeler": cscore.LABELER_BAND_MIN},
    }
