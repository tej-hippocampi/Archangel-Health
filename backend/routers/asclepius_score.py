"""The contributor-score surface (PRD-SCORE).

Two endpoints, one session-scoped and one admin. The physician one takes no
id parameter of any kind: a physician reads their own score and nobody
else's, and that is a property of the route shape rather than of a check
somebody remembered to write.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import contributor_score as cscore
from asclepius.store import get_store

log = logging.getLogger("asclepius.score")

router = APIRouter(tags=["asclepius-score"])


def _store():
    return get_store()


@router.get("/api/asclepius/score")
async def my_score(
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """The signed-in physician's own rating: the stored blended score when
    grading has happened, the initial (tier/proposal) rating before it, and
    the band vocabulary the dashboard renders.

    Gated on the BROWSE surface, deliberately: a PENDING physician must reach
    this — the "your profile is currently in review" state is rendered FROM
    it, and hiding the number during the wait is exactly the silence the
    dashboard exists to remove."""
    store = _store()
    u = store.get_user_by_id(user["id"]) or user
    stored = store.get_contributor_score(u["id"])
    prior, prior_source = cscore.prior_for(store, u)
    if stored:
        score, n_cases, components = stored["score"], stored["n_cases"], stored.get("components")
    else:
        score, n_cases, components = round(prior, 1), 0, None
    return {
        "score": score,
        "band": cscore.band_word(score),
        "n_cases": n_cases,
        "prior": round(prior, 1),
        "prior_source": prior_source,
        "components": components,
        "tier": u.get("tier"),
        "tier_word": asc_caps.tier_word(u.get("tier")),
        "bands": {"reviewer": cscore.REVIEWER_BAND_MIN,
                  "labeler": cscore.LABELER_BAND_MIN},
        # The dashboard's "Your profile is currently in review" state.
        "in_review": (u.get("verification_status") or "") == "pending",
        "verification_status": u.get("verification_status"),
    }


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
