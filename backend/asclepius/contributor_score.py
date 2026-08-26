"""The contributor score: one explainable number per physician (PRD-SCORE).

The INITIAL rating already exists: ``users.tier_score``, written at
verification from ``credentialing.propose_tier`` (0-100; reviewer at 70+,
labeler at 30+). This module owns what happens AFTER: every QA-graded or
reviewer-graded case folds into a running score, so a physician can watch
their number move with their work and an admin can read the trajectory.

Design rules, in order of importance:

  * **Deterministic and explainable.** No model call anywhere. Every point is
    a component with a name, the breakdown is stored next to the score, and
    recomputing from the same inputs gives the same answer. A physician asking
    "why did my score move?" gets an itemized answer, not a shrug.
  * **Best-effort at the call sites.** The recompute hooks ride on QA
    decisions and review submissions; a scoring failure must never take a
    submit or a decision down with it. Callers wrap in try/except; this
    module raises nothing it can avoid.
  * **Shrinkage toward the prior.** ``score = (K*prior + sum(cases)) / (K+n)``
    with K=5: a first bad case cannot crater a strong profile, a first good
    case cannot mint a reviewer, and with volume the work outweighs the CV,
    which is the whole point of measuring.

Per graded case, clamped to 0..100:

    outcome_base   85 accepted / 70 accepted with edits / 30 rejected
    citation_bonus +1 per evidence anchor, max +5
    reasoning_bonus +0.5 per reasoning step, max +5
    agreement_adj  ±5 from the submission's agreement score (kappa-shaped)
    time_adj       +3 inside the expected-minutes band for the case's
                   difficulty, -5 when rushed (< a quarter of expected)

Expected minutes prefer the task's MEASURED difficulty
(``empirical_difficulty``, the frontier-model failure rate) over the declared
label, because a declared "hard" that every model aces is not hard.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("asclepius.contributor_score")

# Shrinkage weight: how many cases of evidence the prior is worth.
PRIOR_WEIGHT = 5

OUTCOME_BASE = {
    "accepted": 85.0,
    "accepted_with_edits": 70.0,
    "rejected": 30.0,
}

# Declared difficulty -> expected minutes of careful work.
EXPECTED_MINUTES = {"easy": 10.0, "medium": 20.0, "hard": 35.0}

# Score bands, aligned with credentialing's tiering thresholds so the number
# a physician watches and the tier the queue proposes speak one language.
REVIEWER_BAND_MIN = 70
LABELER_BAND_MIN = 30


def band_word(score: Optional[float]) -> str:
    if score is None:
        return "Unrated"
    if score >= REVIEWER_BAND_MIN:
        return "Reviewer band"
    if score >= LABELER_BAND_MIN:
        return "Labeler band"
    return "Building"


def expected_minutes(task: Optional[Dict[str, Any]]) -> float:
    """Expected careful-work minutes for a case. Measured difficulty wins."""
    t = task or {}
    measured = t.get("empirical_difficulty")
    if t.get("difficulty_measured") and measured is not None:
        try:
            return 10.0 + 30.0 * max(0.0, min(1.0, float(measured)))
        except (TypeError, ValueError):
            pass
    return EXPECTED_MINUTES.get((t.get("difficulty") or "").strip().lower(), 20.0)


def _outcome_for(submission: Dict[str, Any], reviews: List[Dict[str, Any]]) -> Optional[str]:
    """The graded outcome of one submission, or None when nobody graded it.

    A reviewer's verdict is the finer instrument, so when one exists it wins;
    QA's approve/reject covers submissions that were sampled straight into the
    admin gate. The WORST review verdict stands: a submission one reviewer
    accepted and another rejected is a disagreement, not an acceptance.
    """
    verdicts = [(r.get("verdict") or "").strip() for r in reviews]
    verdicts = [v for v in verdicts if v]
    if verdicts:
        if "reject" in verdicts:
            return "rejected"
        if "accept_with_edits" in verdicts:
            return "accepted_with_edits"
        if "accept" in verdicts:
            return "accepted"
        return None
    qa = submission.get("qa") or {}
    decision = (qa.get("decision") or "").strip()
    if decision == "approve":
        return "accepted"
    if decision == "reject":
        return "rejected"
    # Pipeline statuses double as outcomes when the qa block is absent.
    status = (submission.get("status") or "").strip()
    if status == "export_ready":
        return "accepted"
    if status == "rejected":
        return "rejected"
    return None


def _count_anchors(payload: Dict[str, Any]) -> int:
    """Evidence anchors anywhere in the submission payload, deduplicated by
    identity of the dict (a rough count is all the bonus needs)."""
    n = 0

    def walk(node: Any) -> None:
        nonlocal n
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("evidence_anchors",) and isinstance(value, list):
                    n += len([a for a in value if a])
                elif key == "evidence_anchor" and value:
                    n += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload or {})
    return n


def case_score(
    submission: Dict[str, Any],
    task: Optional[Dict[str, Any]],
    reviews: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Score one graded submission, or None when it has no graded outcome.

    Returns {score, components} where components itemizes every point.
    """
    outcome = _outcome_for(submission, reviews)
    if outcome is None:
        return None
    payload = submission.get("payload") or {}

    base = OUTCOME_BASE[outcome]
    citations = min(_count_anchors(payload), 5)
    steps = payload.get("reasoning_steps") or []
    reasoning = min(len(steps) if isinstance(steps, list) else 0, 10) / 2.0

    agreement = 0.0
    kappa = submission.get("agreement_score")
    if kappa is not None:
        try:
            agreement = max(-5.0, min(5.0, round(10.0 * (float(kappa) - 0.5), 1)))
        except (TypeError, ValueError):
            agreement = 0.0

    time_adj = 0.0
    expected = expected_minutes(task)
    spent_sec = submission.get("time_spent_sec")
    if spent_sec:
        try:
            ratio = (float(spent_sec) / 60.0) / expected
            if ratio < 0.25:
                time_adj = -5.0  # rushed relative to what the case demands
            elif 0.5 <= ratio <= 2.0:
                time_adj = 3.0   # inside the careful-work band
        except (TypeError, ValueError, ZeroDivisionError):
            time_adj = 0.0

    total = max(0.0, min(100.0, base + citations + reasoning + agreement + time_adj))
    return {
        "score": round(total, 1),
        "components": {
            "outcome": outcome,
            "outcome_base": base,
            "citation_bonus": citations,
            "reasoning_bonus": reasoning,
            "agreement_adj": agreement,
            "time_adj": time_adj,
            "expected_minutes": round(expected, 1),
            "time_spent_sec": spent_sec,
        },
    }


def prior_for(store, user: Dict[str, Any]) -> Tuple[float, str]:
    """(prior score, where it came from). The stored tier_score when the
    verification queue wrote one; a live proposal otherwise (propose_tier is
    pure, so a pre-verification physician still sees a real number)."""
    stored = user.get("tier_score")
    if stored is not None:
        try:
            return max(0.0, min(100.0, float(stored))), "tier_score"
        except (TypeError, ValueError):
            pass
    try:
        from asclepius import credentialing

        prop = credentialing.propose_tier(user)
        return max(0.0, min(100.0, float(prop.get("score") or 0.0))), "proposal"
    except Exception:
        log.exception("contributor_score: prior proposal failed; defaulting")
        return 50.0, "default"


def compute(store, user_id: str, *, limit: int = 500) -> Optional[Dict[str, Any]]:
    """The full picture for one physician: prior, graded cases, blended score.

    Reads everything from the store, writes nothing. Returns None only when
    the user does not exist.
    """
    user = store.get_user_by_id(user_id)
    if not user:
        return None
    prior, prior_source = prior_for(store, user)

    graded: List[Dict[str, Any]] = []
    for sub in store.list_submissions(evaluator_id=user_id, limit=limit):
        task = store.get_task(sub.get("task_id") or "")
        reviews = store.reviews_for_submission(sub["submission_id"])
        scored = case_score(sub, task, reviews)
        if scored is not None:
            graded.append({"submission_id": sub["submission_id"], **scored})

    n = len(graded)
    total = sum(g["score"] for g in graded)
    blended = (PRIOR_WEIGHT * prior + total) / (PRIOR_WEIGHT + n)
    blended = round(max(0.0, min(100.0, blended)), 1)
    return {
        "user_id": user_id,
        "score": blended,
        "band": band_word(blended),
        "prior": round(prior, 1),
        "prior_source": prior_source,
        "prior_weight": PRIOR_WEIGHT,
        "n_cases": n,
        "cases": graded,
    }


def recompute_and_store(store, user_id: str) -> Optional[Dict[str, Any]]:
    """Recompute from the graded record and persist score + history.

    Idempotent by construction: the score is a pure function of the graded
    record, and the history row is keyed on the newest graded submission so a
    re-run of the same grade replaces its own entry instead of stacking.
    """
    result = compute(store, user_id)
    if result is None:
        return None
    latest = result["cases"][-1] if result["cases"] else None
    prev = store.get_contributor_score(user_id)
    store.upsert_contributor_score(
        user_id=user_id,
        score=result["score"],
        n_cases=result["n_cases"],
        components={
            "prior": result["prior"],
            "prior_source": result["prior_source"],
            "prior_weight": result["prior_weight"],
            "latest_case": latest,
        },
    )
    store.record_contributor_score_history(
        user_id=user_id,
        score=result["score"],
        prev_score=(prev or {}).get("score"),
        case_score=(latest or {}).get("score"),
        submission_id=(latest or {}).get("submission_id"),
        components=(latest or {}).get("components") or {},
    )
    return result


def recompute_for_submission(store, submission_id: str) -> None:
    """The hook the QA and review paths call. Never raises."""
    try:
        sub = store.get_submission(submission_id)
        if not sub:
            return
        recompute_and_store(store, sub.get("evaluator_id") or "")
    except Exception:
        log.exception("contributor_score: recompute failed for %s (grade stands)",
                      submission_id)
