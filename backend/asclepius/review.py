"""Two-tier review product (PRD A) — routing policy, blinding, and validation.

Senior physicians (``users.tier == 'reviewer'``, assigned by the verification
flow) grade a labeler's completed submission instead of doing the case from
scratch. This module owns the POLICY layer:

  * ``can_review``            — the reviewer access gate (explicit tier only).
  * ``needs_review``          — which submissions enter the review queue.
  * ``needs_second_label``    — which tasks get a second INDEPENDENT labeler so
    the export can report a real Cohen's κ (review ≠ κ — see agreement.py).
  * ``sweep_double_label_routing`` — applies that decision to open tasks.
  * ``blinded_review_view``   — the payload a reviewer sees. Built by WHITELIST,
    never by stripping: a new identity field added upstream stays invisible here
    by default instead of leaking by default.
  * ``validate_review_payload`` — server-side validation of a review submission.

Persistence lives in the PRD-A sentinel block of ``store.py``; HTTP lives in
``routers/asclepius_review.py``. This module is import-light (no FastAPI) so it
stays unit-testable.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

from asclepius import agreement as asc_agreement

# ─── Review vocabulary (PRD A Phase 2) ────────────────────────────────────────
REVIEW_VERDICTS = ("accept", "accept_with_edits", "reject")

# key, short label, plain-language meaning — served to the portal so the UI and
# the server can never disagree about the dimension set.
REVIEW_DIMENSIONS: List[List[str]] = [
    ["clinical_accuracy", "Clinically correct", "the answer is right for this patient"],
    ["reasoning_quality", "Reasoning holds", "the steps actually support the answer"],
    ["completeness", "Nothing decisive missing", "no omission that changes management"],
    ["rubric_quality", "Grader is usable", "the rubric would score a new answer correctly"],
]
DIMENSION_KEYS = tuple(d[0] for d in REVIEW_DIMENSIONS)

# Three states, not two (START_HERE §5 rule 4): "cannot assess" is the honest
# answer when a dimension is outside the reviewer's subspecialty. Forcing a
# binary there manufactures agreement nobody measured, so it persists as its own
# value and is never folded into agree/disagree.
DIMENSION_STATES = ("agree", "disagree", "cannot_assess")


def review_target_rate() -> float:
    """Fraction of submissions routed to expert review. 1.0 at launch — the
    reviewer tier IS the quality story; a partial rollout gives neither the
    metric nor the pitch (PRD A §1.3). Read at call time so tests and ops can
    change it without a process restart."""
    try:
        return float(os.getenv("ASCLEPIUS_REVIEW_RATE", "1.0"))
    except ValueError:
        return 1.0


def double_label_rate() -> float:
    """Target fraction of tasks routed to a second independent labeler. Delegates
    to the agreement module so the κ pipeline and the router can never disagree
    about the target (single source of truth for ``ASCLEPIUS_DOUBLE_LABEL_RATE``)."""
    return asc_agreement.double_label_rate()


def review_lease_minutes() -> int:
    """How long an ``in_review`` claim holds before the submission re-queues.
    Prevents an abandoned draw from vanishing from the worklist forever."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_REVIEW_LEASE_MIN", "45")))
    except ValueError:
        return 45


def can_review(user: Optional[Dict[str, Any]]) -> bool:
    """Reviewer access requires an EXPLICIT reviewer tier.

    NULL tier means 'not yet assigned', which is not the same as 'no' — but for
    access control both deny. The distinction is preserved so the admin queue can
    tell an unassigned physician from a rejected one (START_HERE §5 rule 4).
    Reads the tier off the user dict only — never via SQL — so this is safe to
    call before the PRD-B users-table migration has ever run."""
    return (user or {}).get("tier") == "reviewer"


def _stable_fraction(key: str) -> float:
    """Deterministic hash → [0, 1). Rate-based routing keyed on stable ids stays
    re-decidable (same submission → same answer, forever) without storing a
    routing table, and tests never flake on a random draw."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / float(0xFFFFFFFF + 1)


def needs_review(task: Dict[str, Any], submission: Dict[str, Any]) -> bool:
    """Every V4 real_deid case and every frontier-hard case is reviewed; the rest
    top up to the target rate. At launch the rate is 1.0 — the reviewer tier is
    the quality story, and a partial rollout gives us neither the metric nor the
    pitch (PRD A §1.3)."""
    t = task or {}
    case = t.get("case") or {}
    if t.get("case_source") == "real_deid" or case.get("case_source") == "real_deid":
        return True
    if (t.get("declared_difficulty") or case.get("declared_difficulty")) == "frontier-hard":
        return True
    rate = review_target_rate()
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    sid = (submission or {}).get("submission_id") or ""
    return _stable_fraction("review:" + sid) < rate


def needs_second_label(
    task: Dict[str, Any],
    submission: Dict[str, Any],
    current_rate: float,
    *,
    specialty_n: Optional[int] = None,
) -> bool:
    """A slice gets a SECOND INDEPENDENT labeler (not a reviewer) so we can report
    a real Cohen's κ. Stratify toward hard cases: κ computed only on easy cases is
    flatteringly high and tells a buyer nothing (PRD A §1.3).

    Delegates to ``agreement.should_double_label`` (the stratification policy the
    κ pipeline already documents), feeding it the first labeler's confidence from
    the submission so a low-confidence first read always routes."""
    t = dict(task or {})
    conf = str((submission or {}).get("confidence") or "").strip().lower()
    if conf and not t.get("first_annotator_confidence"):
        t["first_annotator_confidence"] = conf
    return asc_agreement.should_double_label(
        t, current_rate=float(current_rate), specialty_n=specialty_n
    )


def sweep_double_label_routing(store: Any, *, limit: int = 100) -> int:
    """Decide double-labeling for open single-label tasks that carry a first
    label. Runs lazily from the review router (bounded, idempotent — flagging is
    a one-way max_labels bump the queue layer already respects). Returns the
    number of tasks newly flagged."""
    candidates = store.tasks_awaiting_double_label_decision(limit=limit)
    if not candidates:
        return 0
    flagged = 0
    specialty_obs: Dict[str, int] = {}
    for t in candidates:
        specialty = t.get("specialty") or "unknown"
        if specialty not in specialty_obs:
            specialty_obs[specialty] = len(
                store.list_agreement_observations(specialty=specialty)
            )
        first_sub = None
        if t.get("first_submission_id"):
            first_sub = store.get_submission(t["first_submission_id"])
        # Rate recomputed per decision: the greedy top-up stops flagging the
        # moment the flagged share reaches the target.
        rate = store.double_label_flag_rate()
        if needs_second_label(t, first_sub or {}, rate, specialty_n=specialty_obs[specialty]):
            if store.flag_task_for_double_label(t["task_id"]):
                flagged += 1
                store.log_event(
                    entity_type="task",
                    entity_id=t["task_id"],
                    event_type="double_label_flagged",
                    payload={"specialty": specialty, "current_rate": round(rate, 4)},
                )
    return flagged


# ─── Blinding (PRD A Phase 2) ─────────────────────────────────────────────────
# The reviewer must not see the labeler's name, credentials, or years of
# experience. Enforced by WHITELIST: only the keys below are ever served. The
# identity-bearing fields (evaluator_id, annotator_json, id_hashed, email, …)
# are absent because they were never copied, not because they were removed.
_TASK_VIEW_KEYS = (
    "task_id", "specialty", "difficulty", "modality", "case_source",
    "prompt", "case", "decisive_action", "grounding_mode",
)
_SUBMISSION_PAYLOAD_VIEW_KEYS = (
    "verdict", "chosen_id", "rejected_id", "chosen_revision", "rejected_critique",
    "from_scratch", "reasoning_steps", "rubric", "independent_answer", "citations",
)


def blinded_review_view(task: Dict[str, Any], submission: Dict[str, Any]) -> Dict[str, Any]:
    """The exact payload a reviewer is served: the case, and the labeler's answer
    as submitted — verdict, corrected answer, step annotations, rubric, citations
    — with zero labeler identity. Candidates ship as {id, text} only, which also
    preserves the existing model-identity blinding."""
    t = task or {}
    s = submission or {}
    payload = s.get("payload") or {}
    task_view: Dict[str, Any] = {k: t.get(k) for k in _TASK_VIEW_KEYS}
    task_view["candidate_answers"] = [
        {"id": c.get("id"), "text": c.get("text")}
        for c in (t.get("candidate_answers") or [])
        if isinstance(c, dict)
    ]
    answer_view: Dict[str, Any] = {k: payload.get(k) for k in _SUBMISSION_PAYLOAD_VIEW_KEYS
                                   if payload.get(k) is not None}
    answer_view["verdict"] = s.get("verdict") or payload.get("verdict")
    return {
        "submission_id": s.get("submission_id"),
        "task_id": s.get("task_id"),
        "portal_version": s.get("portal_version"),
        "confidence": s.get("confidence"),
        "submitted_at": s.get("created_at"),
        "task": task_view,
        "labeler_answer": answer_view,
        # 1 = this payload verifiably carries no labeler identity. If any future
        # change surfaces identity here, this flag must flip to 0 — the Phase 5
        # test enforces the payload, not the flag (PRD A §4).
        "blinded": True,
    }


# ─── Validation (PRD A Phase 2) ───────────────────────────────────────────────
def _has_correction_content(corrections: Any) -> bool:
    if not isinstance(corrections, dict):
        return False
    notes = (corrections.get("notes") or "").strip() if isinstance(
        corrections.get("notes"), str) else ""
    edited = (corrections.get("edited_answer") or "").strip() if isinstance(
        corrections.get("edited_answer"), str) else ""
    return bool(notes or edited)


def validate_review_payload(body: Dict[str, Any]) -> List[str]:
    """All the ways a review submission can be unusable, as a list of reasons
    (empty = valid). Pure so the rules are unit-testable without HTTP."""
    errors: List[str] = []
    b = body or {}

    verdict = b.get("verdict")
    if verdict not in REVIEW_VERDICTS:
        errors.append(
            f"verdict must be one of {list(REVIEW_VERDICTS)}, got {verdict!r}")

    dims = b.get("dimensions")
    if not isinstance(dims, dict):
        errors.append("dimensions must be an object with one state per dimension")
        dims = {}
    for key in DIMENSION_KEYS:
        state = dims.get(key)
        if state not in DIMENSION_STATES:
            # 'cannot_assess' is always available, so every dimension can be
            # answered honestly — a missing one is an incomplete review.
            errors.append(
                f"dimension {key!r} requires one of {list(DIMENSION_STATES)}, got {state!r}")
    for key in dims:
        if key not in DIMENSION_KEYS:
            errors.append(f"unknown review dimension {key!r}")

    # Reject requires a reason: a rejection with no explanation is unusable to
    # the labeler, to the buyer, and to us (PRD A Phase 2).
    notes = b.get("reviewer_notes")
    notes_text = notes.strip() if isinstance(notes, str) else ""
    if verdict == "reject" and not notes_text:
        errors.append("reject requires reviewer_notes explaining the rejection")

    # accept_with_edits without the edits is an accept wearing the wrong label.
    if verdict == "accept_with_edits" and not _has_correction_content(b.get("corrections")):
        errors.append(
            "accept_with_edits requires corrections (notes and/or edited_answer)")

    tss = b.get("time_spent_sec")
    if tss is not None:
        try:
            if int(tss) < 0:
                errors.append("time_spent_sec must be >= 0")
        except (TypeError, ValueError):
            errors.append("time_spent_sec must be an integer")

    return errors
