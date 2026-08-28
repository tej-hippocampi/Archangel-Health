"""Longitudinal decision points and the sealed future (Longitudinal Cases PRD).

**The thesis in one line: the next encounter is the answer key.** A chart that
continues past a decision contains what happened after that decision. Truncate the
chart at encounter *k*, ask a physician to commit to an assessment, a plan, and —
the field nobody else sells — *what they expect to see and what would tell them
they are wrong*, then reveal encounter *k+1*. The record grades the prediction. No
human graded it; the chart did.

THE CORRECTNESS RULE THAT GOVERNS ALL OF THIS (PRD §4.1):

    **Truncation is a server responsibility.** The client must never receive data
    it is meant not to show. Everything downstream of the decision point is
    ABSENT FROM THE PAYLOAD — not hidden, not collapsed, not styled away. A
    truncation implemented in CSS is a leak, and a leak here does not merely
    weaken one case: it destroys the RLVR claim for that physician's whole
    trajectory, permanently and unrecoverably. You cannot un-read a future.

This module is PURE. It imports no store, no FastAPI, no ``real_cases``. Like
``routing``, it answers policy questions and nothing else, so there is exactly one
place to read them:

  * ``is_trajectory_point``      — is this task part of an ordered trajectory?
  * ``blocks_out_of_order``      — may THIS evaluator open THIS point yet?
  * ``kappa_exclusion_reason``   — why this observation must not enter the κ pool
  * ``normalize_expected_trajectory`` / ``normalize_self_score`` — the falsifier
    corpus's two wire shapes, validated in one place
  * ``outcome_verification``     — the named metric that replaces κ for these points

``store`` owns the SQL that enforces ``blocks_out_of_order`` at draw time;
``routers/asclepius`` owns the 409 on the direct-open path; both call in here for
the rule itself so the queue and the URL can never disagree about it.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ─── The unit ─────────────────────────────────────────────────────────────────
#: How many labels a trajectory point is provisioned for BY DEFAULT (PRD §9.6).
#:
#: One, deliberately, and this is a pricing decision as much as a routing one.
#: Trajectory points are excluded from the κ pool by construction (§4.2.4), so a
#: second label buys no agreement statistic. What it buys is a second independent
#: walk of the same patient — a different and more expensive product at the
#: standard per-submission rate, 13 points deep on patient-1. Double-walking
#: a trajectory is a deliberate, priced decision an admin makes explicitly by
#: passing ``max_labels=2``; it is never the default and never derived.
TRAJECTORY_MAX_LABELS = 1

#: The κ-pool exclusion token stamped on an agreement observation drawn from a
#: trajectory point. Stored rather than inferred at read time so the exclusion is
#: auditable in the database a buyer's methodologist asks to see.
KAPPA_EXCLUSION_SEQUENTIAL = "trajectory_sequential"

#: Why, in the sentence that goes in front of a buyer. Kept next to the token so
#: the export, the quality report and the admin surface cannot paraphrase it into
#: something weaker.
KAPPA_EXCLUSION_RATIONALE = (
    "Trajectory decision points are excluded from the Cohen's κ pool by "
    "construction. Blinding does not make sequential labels by one physician "
    "independent: a physician who labels encounter k and then k+1 carries their own "
    "model of that patient forward, so aggregating the two measures within-physician "
    "consistency and reports it as between-physician agreement. These points carry "
    "outcome verification against the next encounter instead, which is a stronger "
    "claim than agreement."
)


def new_trajectory_id() -> str:
    """A fresh trajectory id. One per chart-walk, shared by every point in it."""
    return f"traj-{uuid.uuid4().hex[:12]}"


def is_trajectory_point(task: Optional[Dict[str, Any]]) -> bool:
    """True when this task is an ordered point in a trajectory.

    Keyed on ``trajectory_id`` alone. ``sequence_index`` may legitimately be 0 —
    the first point — and ``0`` is falsy, which is precisely the bug this
    one-line function exists to make unwritable anywhere else.
    """
    return bool((task or {}).get("trajectory_id"))


def sequence_index(task: Optional[Dict[str, Any]]) -> Optional[int]:
    """This point's 0-based position, or None for an ordinary V1–V4 task."""
    if not is_trajectory_point(task):
        return None
    raw = (task or {}).get("sequence_index")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        # A trajectory row with an unreadable index is a data defect, not a
        # position. Returning None here would make it look like an ordinary task
        # and let the sequence gate wave it through, so callers get a sentinel
        # that sorts last and blocks — see ``blocks_out_of_order``.
        return None


def blocks_out_of_order(
    task: Optional[Dict[str, Any]], *, unanswered_earlier: Sequence[Any],
) -> Optional[str]:
    """``None`` when this evaluator may open this point; a reason string when not.

    ``unanswered_earlier`` is every earlier point in the same trajectory that this
    evaluator has NOT yet submitted, resolved by the caller (``store``) in SQL.
    The rule itself lives here so the queue's WHERE clause and the direct-open
    path's 409 are two enforcements of one sentence rather than two sentences.

    Why this is a BLOCKER and not a nicety (PRD §9.1): the labeler queue sorts on
    label count FIRST (``store._PRD_R_PRIORITY_ORDER``), so the moment a second
    physician labels point 5 of a 13-point trajectory, point 5 outranks points 1–4
    for everybody. Physician A, who has answered point 0, is then served encounter
    5 — whose visible state block contains encounters 1–4, the outcomes of the four
    decisions they were about to be asked to predict. That is not a race or an edge
    case; it is the ordinary behaviour of the priority sort as soon as two people
    work the same chart.
    """
    if not is_trajectory_point(task):
        return None
    idx = sequence_index(task)
    if idx is None:
        return (
            "This trajectory point carries no readable sequence index, so its "
            "position in the chart walk cannot be established. Refusing to serve "
            "it rather than risk revealing a future the physician is being asked "
            "to predict."
        )
    pending = list(unanswered_earlier or [])
    if not pending:
        return None
    return (
        f"This is step {idx + 1} of a longitudinal chart walk. "
        f"{len(pending)} earlier decision point(s) in the same chart are still "
        "unanswered by you, and this case's history contains what happened after "
        "them. Answer them in order first."
    )


# ─── §4.2.4 — the κ pool ──────────────────────────────────────────────────────
def kappa_exclusion_reason(task: Optional[Dict[str, Any]]) -> Optional[str]:
    """The exclusion token for an agreement observation on this task, or ``None``.

    Subtle, and it will not announce itself. ``agreement`` requires ``blinded =
    True`` to enter the κ computation — but **blinding is about not seeing the
    other labeler's identity. It says nothing about temporal independence.** A
    physician who labels encounter *k* and then *k+1* is blinded on both. Both
    observations pass the gate and enter κ. What they share is not a co-labeler;
    it is their own model of that patient, formed at *k* and carried into *k+1*.

    Aggregate that and you are reporting within-physician consistency as
    between-physician agreement — on the one number a buyer audits.
    """
    return KAPPA_EXCLUSION_SEQUENTIAL if is_trajectory_point(task) else None


# ─── §3.3 field 3 — the falsifier ─────────────────────────────────────────────
#: The floor is a SHAPE check, not a quality judgment, and it is set at two words
#: for a reason worth stating: clinical shorthand is terse and correct. "GGT
#: climbs", "bilirubin falls", "creatinine plateaus" are all perfectly good,
#: chart-checkable predictions, and a three-word floor would have silently deleted
#: every one of them — discarding a board-certified specialist's falsifier without
#: telling anyone, which is the single worst thing this normalizer could do.
#:
#: What two words does exclude is the degenerate input the floor exists for: a
#: stray "yes", an "ok", a half-typed word left in an unused box.
_MIN_FALSIFIER_WORDS = 2
_MIN_EXPECTATION_WORDS = 2
#: Horizon in days. A prediction with no horizon is not falsifiable — "bilirubin
#: will fall" is true eventually — so the field is asked for every time, though it
#: stays optional.
#:
#: The ceiling is deliberately GENEROUS, at five years, and that is the opposite of
#: the obvious choice. A tight ceiling looks like discipline and is not: clamping
#: rewrites the physician's stated prediction into one they did not make, and then
#: scores them against it. On a 20-year chart "I expect this stable over two years"
#: is a legitimate specialist claim, and turning it into "within 400 days" would be
#: this product putting words in a board-certified clinician's mouth on the one
#: field it sells as their own.
#:
#: Whether a horizon is checkable against the NEXT encounter is a different
#: question, and it is answered where it belongs: the physician marks the
#: expectation ``not_assessable`` when the record does not reach far enough. That is
#: a judgment, not an arithmetic clamp.
#:
#: So the bounds here only reject nonsense — a zero, a negative, a mis-typed
#: 999999 — and everything a clinician could plausibly mean survives intact.
_HORIZON_MIN_DAYS = 1
_HORIZON_MAX_DAYS = 1825

_WS_RE = re.compile(r"\s+")


def _clean(text: Any) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def normalize_expected_trajectory(raw: Any) -> Optional[Dict[str, Any]]:
    """The physician's sealed prediction → the stored shape, or ``None``.

    ``{expectations: [{expectation, horizon_days}], falsifiers: [str], note}``

    Returns ``None`` for anything that is not a usable prediction — an empty
    block, a block whose expectations are all blank. **``None`` is the honest
    answer and it is never an error**: the commitment field is optional, and a
    physician who cannot name a falsifier for this decision must be able to say
    so. A fabricated falsifier is worth less than none, because it will be scored
    against a real chart and the score will be meaningless.

    The one thing this function will not do is invent structure. A prediction with
    expectations but no falsifier stores exactly that, with ``falsifiable = False``,
    so the falsifier corpus (§7) can be filtered to the points that actually carry
    one rather than silently diluted by the ones that do not.
    """
    if not isinstance(raw, dict):
        return None

    expectations: List[Dict[str, Any]] = []
    for item in raw.get("expectations") or []:
        if isinstance(item, str):
            item = {"expectation": item}
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("expectation"))
        if len(text.split()) < _MIN_EXPECTATION_WORDS:
            continue
        horizon: Optional[int]
        try:
            horizon = int(item.get("horizon_days"))
        except (TypeError, ValueError):
            horizon = None
        if horizon is not None:
            horizon = max(_HORIZON_MIN_DAYS, min(_HORIZON_MAX_DAYS, horizon))
        expectations.append({"expectation": text, "horizon_days": horizon})

    falsifiers: List[str] = []
    for item in raw.get("falsifiers") or []:
        text = _clean(item.get("falsifier") if isinstance(item, dict) else item)
        if len(text.split()) >= _MIN_FALSIFIER_WORDS:
            falsifiers.append(text)

    if not expectations:
        return None
    return {
        "expectations": expectations,
        "falsifiers": falsifiers,
        "note": _clean(raw.get("note")) or None,
        # Stated, not inferred downstream. This is the flag §7 prices on.
        "falsifiable": bool(falsifiers),
    }


#: What the physician says happened to each expectation once the next encounter is
#: revealed. ``not_assessable`` is a first-class state, exactly as ``cannot_assess``
#: is in the reviewer's dimensions: the next encounter frequently does not contain
#: the observation the prediction was about, and forcing a binary there
#: manufactures a verification nobody made.
SELF_SCORE_STATES = ("held", "did_not_hold", "not_assessable")


def normalize_self_score(raw: Any, *, n_expectations: int) -> Optional[Dict[str, Any]]:
    """The physician's own grading of their sealed prediction → the stored shape.

    ``{marks: [{index, state, note}], falsifier_fired, n_held, n_did_not_hold,
    n_not_assessable, verified}``

    ``n_expectations`` bounds the indices: a mark pointing past the prediction it
    grades is dropped, because a self-score that does not line up with the
    commitment it scores is not evidence of anything.

    ``verified`` — the RLVR signal — is True only when at least one expectation was
    actually assessable against the revealed encounter. A trajectory point where
    every mark is ``not_assessable`` produced NO outcome verification, and saying
    otherwise would put an unearned claim on the one axis this whole product is
    sold on.
    """
    if not isinstance(raw, dict):
        return None
    marks: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw.get("marks") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= max(0, int(n_expectations or 0)) or idx in seen:
            continue
        state = str(item.get("state") or "").strip().lower()
        if state not in SELF_SCORE_STATES:
            continue
        seen.add(idx)
        marks.append({"index": idx, "state": state, "note": _clean(item.get("note")) or None})
    if not marks:
        return None
    tally = {s: sum(1 for m in marks if m["state"] == s) for s in SELF_SCORE_STATES}
    return {
        "marks": sorted(marks, key=lambda m: m["index"]),
        # Did the physician's OWN stated falsifier fire? This is the §3.3 claim —
        # "the physician's own stated falsifier fired, and the chart proves it" —
        # and it is recorded as the physician's assertion about the revealed
        # encounter, never derived from text matching.
        "falsifier_fired": bool(raw.get("falsifier_fired")),
        "n_held": tally["held"],
        "n_did_not_hold": tally["did_not_hold"],
        "n_not_assessable": tally["not_assessable"],
        "verified": (tally["held"] + tally["did_not_hold"]) > 0,
        "note": _clean(raw.get("note")) or None,
    }


# ─── §3.4 signal 3 — the metric that is NOT κ ─────────────────────────────────
def outcome_verification(points: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate outcome verification over trajectory points — its OWN metric.

    ``points`` is ``[{expected_trajectory, self_score}, ...]``, one per submitted
    decision point.

    Reported under its own name for the same reason ``agreement.review_acceptance``
    is kept separately named from κ: it measures a different thing, over a
    different pool, and a number filed under a κ label is a claim nobody measured.

    Everything here is a count of what physicians asserted against a revealed
    chart. There is no model in it and no imputation: a point with no self-score
    contributes to ``n_points`` and to nothing else, so the denominators say how
    much of the corpus is actually verified rather than hiding the gap.
    """
    rows = [p for p in (points or []) if isinstance(p, dict)]
    scored = [p for p in rows if (p.get("self_score") or {}).get("verified")]
    with_falsifier = [p for p in rows
                      if (p.get("expected_trajectory") or {}).get("falsifiable")]
    held = sum(int((p.get("self_score") or {}).get("n_held") or 0) for p in scored)
    missed = sum(int((p.get("self_score") or {}).get("n_did_not_hold") or 0) for p in scored)
    unassessable = sum(int((p.get("self_score") or {}).get("n_not_assessable") or 0)
                       for p in rows if p.get("self_score"))
    fired = sum(1 for p in scored if (p.get("self_score") or {}).get("falsifier_fired"))
    total_marks = held + missed
    return {
        "metric": "outcome_verification",
        "not_kappa": KAPPA_EXCLUSION_RATIONALE,
        "n_points": len(rows),
        "n_points_verified": len(scored),
        "n_points_with_falsifier": len(with_falsifier),
        "n_expectations_held": held,
        "n_expectations_did_not_hold": missed,
        "n_expectations_not_assessable": unassessable,
        "n_falsifiers_fired": fired,
        # Anticipation accuracy over ASSESSABLE expectations only. None rather
        # than 0.0 when nothing was assessable — a rate with an empty denominator
        # is not a zero, and printing one would read as "the physicians were
        # always wrong".
        "anticipation_rate": (round(held / total_marks, 4) if total_marks else None),
    }


# ─── §6 — what this data cannot support ───────────────────────────────────────
#: Ships in the data dictionary and sits here so the module that produces the
#: signal also states its limits. Every line is a claim a buyer's methodologist
#: will test.
LIMITATIONS = (
    ("not_a_controlled_experiment",
     "What happened next reflects the treatment actually given, not the physician's "
     "plan. When a physician proposes something different, the outcome does not test "
     "their plan — it tests the one that was followed. Score anticipation of the "
     "OBSERVED trajectory only; never counterfactual outcomes."),
    ("confounding_by_indication",
     "Sicker patients get more aggressive treatment, so a model trained naively on "
     "chart trajectories learns the treatment pattern rather than the reasoning. The "
     "scored object is the stated reasoning and expectation, not the plan's "
     "similarity to what was done."),
    ("uneven_density",
     "Yield per chart is not predictable — one 5-year chart yields 13 decision "
     "points, a 20-year chart yields 2. Price by decision point, never by chart."),
    ("survivorship",
     "These charts continue because the patient continued. Encounters ending in "
     "death or transfer are absent by construction, and that is exactly where the "
     "interesting failures live."),
    ("findings_policy_varies_within_a_trajectory",
     "study_findings_policy is computed per truncation: a window with no imaging is "
     "'visible', a later window carrying a study asset is 'hidden'. The same patient "
     "therefore presents under two policies within one session, by design — findings "
     "visibility reflects what that window actually contains."),
)


def limitations_block() -> List[Dict[str, str]]:
    """``LIMITATIONS`` as the list of dicts the export annex ships."""
    return [{"limitation": k, "detail": v} for k, v in LIMITATIONS]
