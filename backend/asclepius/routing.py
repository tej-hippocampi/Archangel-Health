"""The order work happens in (PRD R §2).

    open(1)  ──TL#1 submits──►  awaiting_second   [PRIORITY in the TL queue]
                                      │
                                      ▼ TL#2 submits
                               review_ready       [the TR queue draws PAIRS]
                                      │
                                      ▼ TR adjudicates
                                  adjudicated

Derived from counts, never stored as a second source of truth: a task's phase is
a function of (max_labels, submission count, review count). Storing it would
create two truths and one of them would go stale — which is how the first
version of double-labeling became dead code.

This module is PURE. It imports no store, no FastAPI, and — the §4 boundary —
nothing from ``payments``. It answers three questions and nothing else:

  * ``phase``               — where is this task in its lifecycle?
  * ``wants_second_label``  — should the TL queue still serve it?
  * ``ab_order``            — which submission does THIS reviewer see as A?

``review.py`` owns blinding and validation; ``store.py`` owns the SQL; this owns
the state machine, so there is exactly one place to read it.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asclepius import agreement as asc_agreement
from asclepius import trajectory as asc_trajectory

# ─── The four phases ──────────────────────────────────────────────────────────
AWAITING_FIRST = "awaiting_first"
AWAITING_SECOND = "awaiting_second"
REVIEW_READY = "review_ready"
ADJUDICATED = "adjudicated"

PHASES = (AWAITING_FIRST, AWAITING_SECOND, REVIEW_READY, ADJUDICATED)

# The number of independent labels the new flow wants on every case. Kept as a
# named constant rather than a bare 2 sprinkled through the SQL, because the
# whole κ argument rests on it being the SAME number everywhere.
PAIR_LABELS = 2


def target_labels(task: Dict[str, Any]) -> int:
    """How many labels this task is currently PROVISIONED for.

    Read off ``max_labels`` — the stored capacity — not off the policy. The two
    are deliberately different questions: ``wants_second_label`` says what the
    policy would like, this says what the row has actually been lifted to.
    """
    try:
        return max(1, int(task.get("max_labels") or 1))
    except (TypeError, ValueError):
        return 1


def second_label_is_default() -> bool:
    """True when every case is meant to get two labels (PRD R §1.1).

    Reads ``ASCLEPIUS_DOUBLE_LABEL_RATE`` through ``agreement.double_label_rate``
    — the single source of truth for that knob — so shedding load by lowering the
    env var takes effect here, in the queue, and in the sweep at the same instant.
    """
    return asc_agreement.double_label_rate() >= 1.0


def wants_second_label(task: Dict[str, Any]) -> bool:
    """Should this task still be routed to a second INDEPENDENT labeler?

    Three ways to be true, cheapest first — this runs inside the TL draw, so it
    must not issue a query:

      1. it has already been flagged (``max_labels >= 2``) — the stored decision;
      2. double-labeling is the default (the launch setting, rate 1.0);
      3. it is one of the cases ``agreement.should_double_label`` routes
         UNCONDITIONALLY — frontier-hard, real_deid, low first-labeler confidence
         — which stay true even after ops lowers the rate to shed load.

    ``current_rate=1.0`` is passed on purpose in (3): it makes the top-up branch
    (``current_rate < target``) False, so only the unconditional rules can fire.
    The fleet-wide stratified top-up needs counts and belongs in the sweep.
    """
    t = task or {}
    if target_labels(t) >= PAIR_LABELS:
        return True
    # PRD 2 §9.6 — a trajectory point is NOT double-labelled by default, and this
    # short-circuit is load-bearing rather than a preference.
    #
    # ``second_label_is_default()`` is True at the launch rate of 1.0, so without
    # this branch EVERY trajectory point would answer True here, and
    # ``store._prd_r_lift_capacity`` would write ``max_labels = 2`` onto all
    # thirteen of patient-1's points on their first draw. Nobody decided that: it
    # would double a $975 chart walk to $1,950 (a decision point is one completed
    # submission, at the standard per-submission rate) as a side effect of a
    # fleet-wide default, and buy nothing measurable — trajectory points are
    # excluded from the κ pool by construction (§4.2.4), so the second label
    # produces no agreement statistic. What it does produce is a second independent
    # walk of the same chart, which is a different and more expensive product.
    #
    # An admin who wants that says so by passing ``max_labels=2`` at insert, which
    # the first branch above already honours. This only removes the DEFAULT.
    if asc_trajectory.is_trajectory_point(t):
        return False
    if second_label_is_default():
        return True
    return asc_agreement.should_double_label(t, current_rate=1.0)


def effective_capacity(task: Dict[str, Any]) -> int:
    """Label capacity the QUEUE should honour, which is ``max_labels`` lifted to
    2 whenever the policy wants a second label.

    This is what makes the priority rule work without a write on the submit path.
    ``refresh_task_status`` closes a ``max_labels=1`` task the moment its first
    label lands (it is on the hot submit path and PRD R §7 forbids touching it),
    so a case would otherwise sit invisible until some background sweep lifted
    its capacity. Deriving the capacity means the queue is correct immediately
    and the flag write becomes a bookkeeping catch-up rather than a race.
    """
    return max(target_labels(task), PAIR_LABELS if wants_second_label(task) else 1)


def phase(task: Dict[str, Any], n_submissions: int, n_reviews: int) -> str:
    """Where this task is in its lifecycle — DERIVED, never stored (PRD R §2).

    ``n_submissions`` counts verdict-bearing submissions (a row without a verdict
    is a flag or a draft, not a label). ``n_reviews`` counts ``case_reviews`` rows.

    The ordering of the branches is the state machine: a review is terminal, then
    capacity decides whether we are still collecting labels.
    """
    n_submissions = max(0, int(n_submissions or 0))
    n_reviews = max(0, int(n_reviews or 0))
    if n_reviews > 0:
        return ADJUDICATED
    if n_submissions <= 0:
        return AWAITING_FIRST
    if n_submissions < effective_capacity(task):
        return AWAITING_SECOND
    return REVIEW_READY


def is_pair_ready(task: Dict[str, Any], n_submissions: int, n_reviews: int) -> bool:
    """``review_ready`` AND actually a pair. A task that will only ever carry one
    label reaches ``review_ready`` too, but the paired reviewer queue must not
    serve it — there is no comparison to make."""
    return (phase(task, n_submissions, n_reviews) == REVIEW_READY
            and int(n_submissions or 0) >= PAIR_LABELS)


def pair_is_independent(submissions: Sequence[Dict[str, Any]]) -> bool:
    """Two labels from two DIFFERENT physicians.

    κ over two labels by the same person measures their consistency, not
    inter-rater reliability. ``next_task_for_evaluator`` and
    ``next_double_label_for`` both exclude prior submitters in SQL, so this
    should never be False — which is exactly why it is asserted at the point the
    pair is served rather than assumed.
    """
    ids = [s.get("evaluator_id") for s in (submissions or []) if s.get("verdict")]
    return len(ids) >= PAIR_LABELS and len(set(ids)) == len(ids)


# ─── A/B assignment (PRD R §2.2) ──────────────────────────────────────────────
def ab_swapped(task_id: str, reviewer_id: str) -> bool:
    """Does THIS reviewer see the second submission as 'Physician A'?

    Seeded on ``(task_id, reviewer_id)``:

      * stable across reloads — a reviewer who refreshes does not watch the two
        columns swap, which would read as a bug and cost their trust in the pair;
      * uncorrelated with submission order — position no longer leaks who went
        first, so a reviewer cannot form "column A is always the fast one";
      * different for a different reviewer, so two reviewers' A/B assignments
        carry no shared signal.

    A hash rather than ``random`` because it must be re-derivable server-side at
    submit time: the client sends "A"/"B" (a position), and the server maps that
    back to a submission id. The client never learns, and never gets to assert,
    which submission it is grading.
    """
    digest = hashlib.sha256(f"pair-ab:{task_id}:{reviewer_id}".encode("utf-8")).digest()
    return bool(digest[0] & 1)


def ab_pair(
    submissions: Sequence[Dict[str, Any]], *, task_id: str, reviewer_id: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(shown_as_A, shown_as_B)`` for this reviewer, from the canonical pair.

    The canonical order is the caller's (oldest first); the swap is applied here
    so there is one implementation and the draw and the submit can never disagree
    about which column was which.
    """
    pair = list(submissions or [])[:PAIR_LABELS]
    if len(pair) < PAIR_LABELS:
        raise ValueError("ab_pair requires exactly two submissions")
    return (pair[1], pair[0]) if ab_swapped(task_id, reviewer_id) else (pair[0], pair[1])


def resolve_side(
    side: Optional[str], submissions: Sequence[Dict[str, Any]], *,
    task_id: str, reviewer_id: str,
) -> Optional[str]:
    """Map the reviewer's "A"/"B" back to a submission_id, or None.

    ``None`` in means None out ("reject both" names no side). An unrecognized
    side is None rather than a guess: silently defaulting to A would attribute an
    acceptance to a physician the reviewer never chose.
    """
    key = str(side or "").strip().upper()
    if key not in ("A", "B"):
        return None
    shown_a, shown_b = ab_pair(submissions, task_id=task_id, reviewer_id=reviewer_id)
    return (shown_a if key == "A" else shown_b).get("submission_id")


def canonical_side(
    submission_id: Optional[str], submissions: Sequence[Dict[str, Any]]
) -> Optional[str]:
    """``'A'``/``'B'`` in CANONICAL (oldest-first) terms, or None.

    The inverse of :func:`resolve_side`, and the reason it exists: a position is
    only meaningful next to the frame it was measured in. ``stronger`` used to be
    stored raw — in the REVIEWER's shuffled positions — in the column beside
    ``pair_sub_a``/``pair_sub_b``, which are canonical. Half of those rows named
    the wrong physician to anyone reading the two columns together, which is the
    only sane way to read them.
    """
    if not submission_id:
        return None
    ids = canonical_pair_ids(submissions)
    if submission_id == ids[0]:
        return "A"
    if len(ids) > 1 and submission_id == ids[1]:
        return "B"
    return None


def canonical_pair_ids(submissions: Sequence[Dict[str, Any]]) -> List[Optional[str]]:
    """``[sub_a, sub_b]`` in CANONICAL (oldest-first) order — what gets stored.

    Storage is canonical and the presentation is per-reviewer, so two reviewers
    adjudicating the same pair produce rows that can be compared to each other.
    """
    pair = list(submissions or [])[:PAIR_LABELS]
    return [s.get("submission_id") for s in pair] + [None] * (PAIR_LABELS - len(pair))
