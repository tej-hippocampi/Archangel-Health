"""Two-tier review product (PRD A) — routing policy, blinding, and validation.

Senior physicians (any tier carrying the REVIEW capability — ``reviewer`` or
``advisor``; see ``asclepius.capabilities``) grade a labeler's completed
submission instead of doing the case from scratch. This module owns the POLICY
layer:

  * ``can_review``            — the review access gate; delegates to
    ``capabilities.can(user, REVIEW)`` so there is exactly one tier table.
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
import re
from typing import Any, Dict, List, Optional, Tuple

from asclepius import agreement as asc_agreement
from asclepius import capabilities as asc_caps

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
    """Reviewer OR advisor — an EXPLICIT tier carrying the review capability.

    Kept as a named function so the existing call sites do not change; the tier
    set now lives in ``capabilities.py`` (Advisor PRD §2.2). It used to be
    ``tier == "reviewer"`` written right here, and that literal is exactly what
    made adding a third tier dangerous: a hard equality returns False for an
    advisor, which is a legitimate answer for a labeler, so nothing logs and
    nothing 500s — the advisor simply cannot click the button.

    NULL tier means 'not yet assigned', which is not the same as 'no' — but for
    access control both deny. The distinction is preserved so the admin queue can
    tell an unassigned physician from a rejected one (START_HERE §5 rule 4).
    Reads the tier off the user dict only — never via SQL — so this is safe to
    call before the PRD-B users-table migration has ever run."""
    return asc_caps.can(user, asc_caps.REVIEW)


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
    """Decide double-labeling for singly-labeled tasks that carry a first label.
    Returns the number of tasks newly flagged.

    Cost discipline (FIX A A-3.4): this used to issue ~9 statements per
    candidate — ``double_label_flag_rate()`` (two correlated-subquery scans)
    inside the loop, plus ``len(list_agreement_observations(...))``, which
    materialized every observation row just to count it. Measured at 452
    statements for 50 candidates, each on a fresh SQLite connection, on the
    single writer that labeler submissions also need. Now: the rate is read ONCE
    and advanced locally as we flag, and the observation count is a COUNT(*).
    """
    candidates = store.tasks_awaiting_double_label_decision(limit=limit)
    if not candidates:
        return 0
    # Read the fleet-wide counts ONCE and advance the rate locally as we decide.
    # This preserves the greedy top-up (stop once the target share is reached)
    # without re-querying per candidate.
    n_labeled, n_flagged = store.double_label_counts()
    specialty_obs: Dict[str, int] = {}
    decisions: List[Dict[str, Any]] = []
    for t in candidates:
        specialty = t.get("specialty") or "unknown"
        if specialty not in specialty_obs:
            specialty_obs[specialty] = store.agreement_observation_count(specialty=specialty)
        rate = (n_flagged / n_labeled) if n_labeled else 0.0
        # The first labeler's confidence rides on the candidate row, so deciding
        # costs no extra query.
        first_sub = {"confidence": t.get("first_confidence")}
        if needs_second_label(t, first_sub, rate, specialty_n=specialty_obs[specialty]):
            decisions.append({"task_id": t["task_id"], "specialty": specialty,
                              "current_rate": round(rate, 4)})
            n_flagged += 1
    # One batched write for the whole sweep rather than two connections per task.
    return len(store.flag_tasks_for_double_label(decisions))


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


def _blinded_task_view(task: Dict[str, Any]) -> Dict[str, Any]:
    """The case, whitelisted. Candidates ship as ``{id, text}`` only, which
    preserves the existing model-identity blinding."""
    t = task or {}
    view: Dict[str, Any] = {k: t.get(k) for k in _TASK_VIEW_KEYS}
    view["candidate_answers"] = [
        {"id": c.get("id"), "text": c.get("text")}
        for c in (t.get("candidate_answers") or [])
        if isinstance(c, dict)
    ]
    return view


# Metadata keys pruned from ANYWHERE inside a served answer (PRD R §2.2).
#
# The top level is a whitelist and stays one. This is a denylist for what the
# whitelisted values CONTAIN: ``independent_answer`` and ``from_scratch`` are
# nested objects the labeler's client built, and they carry their own
# bookkeeping. ``captured_at`` is the sharpest example — it is the pre-reveal
# commit timestamp, i.e. a direct "who went first" tell, sitting two levels
# under a field the reviewer genuinely needs.
_ORDERING_METADATA_KEYS = frozenset({
    "captured_at", "created_at", "updated_at", "submitted_at",
    "submission_id", "evaluator_id", "portal_version",
})


def _scrub_metadata(obj: Any) -> Any:
    """Recursively drop ordering/identity bookkeeping from a served structure."""
    if isinstance(obj, dict):
        return {k: _scrub_metadata(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.lower() in _ORDERING_METADATA_KEYS)}
    if isinstance(obj, list):
        return [_scrub_metadata(v) for v in obj]
    return obj


def _answer_view(submission: Dict[str, Any], *, scrub: bool = False) -> Dict[str, Any]:
    """One labeler's answer, whitelisted. Shared by the single and paired views so
    a field added to one can never be missing from the other.

    ``scrub`` additionally prunes nested ordering metadata. It is off for the
    single-submission view, which serves ``submitted_at`` at the top level
    anyway and has no second answer to compare against; it is on for the pair,
    where any per-submission timestamp is a first/second tell."""
    s = submission or {}
    payload = s.get("payload") or {}
    view: Dict[str, Any] = {k: payload.get(k) for k in _SUBMISSION_PAYLOAD_VIEW_KEYS
                            if payload.get(k) is not None}
    view["verdict"] = s.get("verdict") or payload.get("verdict")
    return _scrub_metadata(view) if scrub else view


def blinded_review_view(task: Dict[str, Any], submission: Dict[str, Any]) -> Dict[str, Any]:
    """The exact payload a reviewer is served: the case, and the labeler's answer
    as submitted — verdict, corrected answer, step annotations, rubric, citations
    — with zero labeler identity. Candidates ship as {id, text} only, which also
    preserves the existing model-identity blinding."""
    t = task or {}
    s = submission or {}
    payload = s.get("payload") or {}
    task_view: Dict[str, Any] = _blinded_task_view(t)
    answer_view = _answer_view(s)
    return {
        "submission_id": s.get("submission_id"),
        "task_id": s.get("task_id"),
        "portal_version": s.get("portal_version"),
        "confidence": s.get("confidence"),
        "submitted_at": s.get("created_at"),
        "task": task_view,
        "labeler_answer": answer_view,
        # NOTE: no ``blinded`` key here on purpose. Blinding is DERIVED from this
        # object by ``payload_is_blinded`` at draw time and persisted on the
        # claim — an asserted constant is exactly the defect FIX A §F2 named.
    }


# ─── Derived blinding (FIX A Phase 2 / F2) ────────────────────────────────────
# The buyer-facing honesty claim in this whole deliverable is "the reviewer could
# not see who labeled it". It gates ``independent_kappa``'s blinded-only filter,
# so it must be a MEASUREMENT of the payload actually served, never a literal.
_IDENTITY_MARKERS = frozenset({
    "evaluator_id", "annotator", "id_hashed", "email", "user_id",
    "credential", "credentials", "years_experience", "organization",
    "board_cert", "name", "full_name", "npi", "linkedin_url",
})


def _walk_for_keys(obj: Any, markers: frozenset) -> bool:
    """True if any key anywhere in the structure is an identity marker."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in markers:
                return True
            if _walk_for_keys(value, markers):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _walk_for_keys(item, markers):
                return True
    return False


def _walk_for_values(obj: Any, needles: List[str]) -> bool:
    """True if any string anywhere in the structure contains one of ``needles``.

    Catches the vector the PRD named but nothing tested: labeler-authored free
    text that names the labeler. ``_SUBMISSION_PAYLOAD_VIEW_KEYS`` deliberately
    serves ``from_scratch``, ``rejected_critique`` and ``reasoning_steps`` — all
    prose the labeler wrote and could have signed."""
    if not needles:
        return False
    if isinstance(obj, str):
        low = obj.lower()
        return any(n in low for n in needles)
    if isinstance(obj, dict):
        return any(_walk_for_values(v, needles) for v in obj.values())
    if isinstance(obj, list):
        return any(_walk_for_values(v, needles) for v in obj)
    return False


def _separator_variants(raw: str) -> List[str]:
    """The same MULTI-TOKEN name written the other conventional ways.

    ``marguerite.okonkwo`` (an email local-part) and ``Marguerite Okonkwo`` (how
    a physician signs a note) are the same identity, and the value scan used to
    match only the first. This closes that gap without loosening precision: only
    multi-token names produce variants, so no BARE surname ever becomes a needle.
    That restraint is deliberate — a single-token rule would redact 'Cushing'
    from a labeler named Cushing writing about Cushing's syndrome, corrupting
    the clinical record it was meant to protect.
    """
    parts = [p for p in re.split(r"[.\-_+\s]+", raw) if len(p) >= 2]
    if len(parts) < 2:
        return []
    return [" ".join(parts), ".".join(parts), "".join(parts),
            " ".join(reversed(parts))]


def labeler_identity_needles(labeler: Optional[Dict[str, Any]]) -> List[str]:
    """The specific strings that would identify THIS labeler, for the value scan
    and for redaction.

    Precise, not heuristic: the actual account's email, local-part, name, org and
    hashed id — plus the conventional re-spellings of any multi-token one. A
    generic name-detector would false-positive on clinical prose ('per Osler's
    sign') and silently shrink κ's n.

    What this deliberately does NOT catch, so it is stated rather than implied:
    a bare surname, an initials-only signature, and writing style. Style in
    particular is unfixable by any scanner and is the residual blinding risk two
    answers side by side create (PRD R §2.2).
    """
    raw: List[str] = []
    for key in ("email", "full_name", "id_hashed", "organization", "org_name"):
        val = (labeler or {}).get(key)
        if isinstance(val, str) and len(val.strip()) >= 4:
            raw.append(val.strip().lower())
    email = (labeler or {}).get("email")
    if isinstance(email, str) and "@" in email:
        local = email.split("@", 1)[0].strip().lower()
        if len(local) >= 4:
            raw.append(local)

    out: List[str] = list(raw)
    for value in raw:
        if "@" in value:
            continue           # a full address has one spelling; do not fragment it
        out.extend(v for v in _separator_variants(value) if len(v) >= 6)
    # Longest first so a redaction pass consumes the full name before a shorter
    # variant of it can carve the string up.
    return sorted(set(out), key=len, reverse=True)


def payload_is_blinded(view: Dict[str, Any], *, reviewer_role: str,
                       labeler: Optional[Dict[str, Any]] = None) -> bool:
    """True only when the served payload provably carries no labeler identity.

    Derived, never asserted. An admin is treated as NOT blind regardless of the
    payload: ``require_reviewer`` admits admins, and an admin can de-blind
    through ``GET /submissions/{id}`` (which serves the full row, including
    ``evaluator_id`` and ``annotator``). Recording ``blinded=1`` for a reviewer
    who can look up the author in another tab is the same lie, one hop out.
    """
    if reviewer_role == "admin":
        return False
    if _walk_for_keys(view, _IDENTITY_MARKERS):
        return False
    return not _walk_for_values(view, labeler_identity_needles(labeler))


# ─── Validation (PRD A Phase 2) ───────────────────────────────────────────────
def _has_correction_content(corrections: Any) -> bool:
    if not isinstance(corrections, dict):
        return False
    notes = (corrections.get("notes") or "").strip() if isinstance(
        corrections.get("notes"), str) else ""
    edited = (corrections.get("edited_answer") or "").strip() if isinstance(
        corrections.get("edited_answer"), str) else ""
    return bool(notes or edited)


def scan_review_free_text(
    reviewer_notes: Optional[str], corrections: Optional[Dict[str, Any]]
) -> List[str]:
    """Safe-Harbor identifier kinds found in the reviewer's own prose (FIX A A-5.3).

    ``corrections.notes`` / ``corrections.edited_answer`` and ``reviewer_notes``
    are free text a physician typed about a possibly-real de-identified case, and
    ``review_block`` ships ``corrections`` verbatim to buyers. The export leak
    gate scans KEYS, not values, so a reviewer writing "per Dr. Chen's note, K+
    6.2 on 3/14" would ship that string to a lab. Uses the same scanner the
    ingestion pipeline uses, so there is one definition of an identifier."""
    from asclepius.validation import residual_identifiers

    texts: List[Optional[str]] = [reviewer_notes]
    if isinstance(corrections, dict):
        for key in ("notes", "edited_answer"):
            val = corrections.get(key)
            if isinstance(val, str):
                texts.append(val)
    kinds: List[str] = []
    for t in texts:
        kinds.extend(residual_identifiers(t))
    return sorted(set(kinds))


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


# ═══ PRD R Phase 2 — the paired review ════════════════════════════════════════
# Review stops being about a SUBMISSION and becomes about a CASE WITH TWO
# INDEPENDENT LABELS. That shape is the only one from which Cohen's κ (the two
# labels against each other) and an expert acceptance rate (the reviewer's
# verdict) can both be computed honestly.

# 'A' and 'B' are POSITIONS in what this reviewer was shown, not submission ids.
# The server owns the position→submission map (``routing.ab_pair``), so a client
# can never assert which physician's work it is grading.
PAIR_STRENGTH = ("A", "B", "equivalent")

# The stored verdict vocabulary is UNCHANGED on purpose (PRD R §2.4 / §7).
# ``agreement.review_acceptance`` is the single definition of expert acceptance
# and counts exactly these three tokens; a fourth ('accept_a') would fall into
# ``n_unclassified`` and silently deflate the headline rate to zero while
# appearing nowhere. "Accept A" / "Accept B" are therefore ONE verdict
# (``accept``) plus a side, and "Reject both" is ``reject`` with no side.
PAIR_VERDICTS = REVIEW_VERDICTS


def blinded_pair_view(
    task: Dict[str, Any], shown_a: Dict[str, Any], shown_b: Dict[str, Any]
) -> Dict[str, Any]:
    """The exact payload a reviewer is served for a PAIR (PRD R §2.2).

    Built by whitelist, like the single view, and then deliberately narrower:

      * no ``submission_id`` — the client submits against the TASK and names a
        position, so it never needs one, and an id ordering is a first/second
        tell;
      * no ``submitted_at`` — the most direct "who went first" leak there is;
      * no ``portal_version`` — structural metadata with no clinical value to the
        reviewer, and one of the few fields that can differ between two labelers.

    ``confidence`` survives because it is the labeler's own read on their answer
    and a reviewer adjudicating a disagreement needs it.

    What this CANNOT fix, and does not pretend to: prose style. Two answers side
    by side make writing style a far stronger signal than one answer alone. The
    structural leaks are closed here; the value scan below closes the explicit
    ones; style is a residual risk that is stated rather than hidden.
    """
    return {
        "task_id": (task or {}).get("task_id"),
        "task": _blinded_task_view(task),
        "answers": [
            {"label": "A", "confidence": (shown_a or {}).get("confidence"),
             "answer": _answer_view(shown_a, scrub=True)},
            {"label": "B", "confidence": (shown_b or {}).get("confidence"),
             "answer": _answer_view(shown_b, scrub=True)},
        ],
    }


# The marker left where a labeler signed their own prose. Visible on purpose: a
# reviewer seeing it knows something was removed and can weigh the gap, which is
# more honest than a seamless deletion that reads as the physician's own words.
REDACTION_MARKER = "[identifying detail removed]"


def redact_identity(
    view: Dict[str, Any], labelers: Optional[List[Optional[Dict[str, Any]]]] = None
) -> Tuple[Dict[str, Any], List[str]]:
    """Strip each labeler's own identifying strings out of the served payload.

    Returns ``(redacted_view, needles_that_were_hit)``.

    PRD R §2.2 asks for a test that seeds a labeler's name into their free text
    and asserts it is NOT SERVED. Measuring the leak and recording
    ``blinded = 0`` — which is what the single-submission flow does — satisfies
    the statistics but not the sentence: the reviewer still read the name, and
    the adjudication is already biased by the time κ excludes the observation.

    So the pair view redacts and then measures the result. Redaction is
    preferred over dropping the case because the clinical content is the work
    two physicians were paid for; losing a case to a signature would be an
    expensive way to enforce a formatting rule.

    Needles are precise (this account's email, local-part, name, org, hashed id
    — see ``labeler_identity_needles``), never a general name detector, so
    clinical prose is not shredded on a false positive.
    """
    needles = []
    for labeler in (labelers or []):
        needles.extend(labeler_identity_needles(labeler))
    needles = sorted(set(n for n in needles if n), key=len, reverse=True)
    if not needles:
        return view, []

    hit: List[str] = []

    def _walk(obj: Any) -> Any:
        if isinstance(obj, str):
            out = obj
            low = out.lower()
            for needle in needles:
                idx = low.find(needle)
                while idx != -1:
                    hit.append(needle)
                    out = out[:idx] + REDACTION_MARKER + out[idx + len(needle):]
                    low = out.lower()
                    idx = low.find(needle)
            return out
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        return obj

    return _walk(view), sorted(set(hit))


def pair_is_blinded(
    view: Dict[str, Any], *, reviewer_role: str,
    labelers: Optional[List[Optional[Dict[str, Any]]]] = None,
) -> bool:
    """True only when the served PAIR provably carries no labeler identity.

    Same derivation as ``payload_is_blinded`` — keys anywhere in the structure,
    then values against each labeler's own identifying strings — but over BOTH
    payloads at once and against BOTH labelers' needles. A name that identifies
    physician A is just as much a leak when it appears in physician B's critique.
    """
    if reviewer_role == "admin":
        return False
    if _walk_for_keys(view, _IDENTITY_MARKERS):
        return False
    needles: List[str] = []
    for labeler in (labelers or []):
        needles.extend(labeler_identity_needles(labeler))
    return not _walk_for_values(view, needles)


def validate_pair_review_payload(body: Dict[str, Any]) -> List[str]:
    """All the ways a PAIRED adjudication can be unusable (empty = valid).

    Pure, so the rules are unit-testable without HTTP, and reused verbatim by the
    route — the reason an unexplained rejection cannot reach the database is that
    there is one rule, not a client-side one and a server-side one."""
    errors: List[str] = []
    b = body or {}

    verdict = b.get("verdict")
    if verdict not in PAIR_VERDICTS:
        errors.append(f"verdict must be one of {list(PAIR_VERDICTS)}, got {verdict!r}")

    # "Which is stronger?" is REQUIRED — it is the comparison the old flow could
    # not ask, and the reason the pair exists. 'equivalent' is a real answer.
    stronger = b.get("stronger")
    if stronger not in PAIR_STRENGTH:
        errors.append(f"stronger must be one of {list(PAIR_STRENGTH)}, got {stronger!r}")

    # An acceptance has to name whose work is accepted, or the row cannot be
    # attributed to a physician and 'expert acceptance' means nothing per-labeler.
    side = b.get("accepted_side")
    if verdict == "accept" and side not in ("A", "B"):
        errors.append("accept must name the accepted side ('A' or 'B')")
    if verdict == "reject" and side is not None:
        errors.append("reject both accepts no side")
    if verdict == "accept_with_edits" and side is not None and side not in ("A", "B"):
        errors.append("accepted_side must be 'A', 'B', or absent")

    dims = b.get("dimensions")
    if not isinstance(dims, dict):
        errors.append("dimensions must be an object with one state per dimension")
        dims = {}
    for key in DIMENSION_KEYS:
        if dims.get(key) not in DIMENSION_STATES:
            errors.append(
                f"dimension {key!r} requires one of {list(DIMENSION_STATES)}, "
                f"got {dims.get(key)!r}")
    for key in dims:
        if key not in DIMENSION_KEYS:
            errors.append(f"unknown review dimension {key!r}")

    # PRD R §2.3, same rule and same reason as the single flow: an unexplained
    # rejection — or an "accept with edits" carrying no edits — is unusable to
    # the labeler, to the buyer, and to us.
    if verdict in ("accept_with_edits", "reject") and not _has_correction_content(
            b.get("corrections")):
        errors.append(
            f"{verdict} requires corrections (notes and/or edited_answer)")

    tss = b.get("time_spent_sec")
    if tss is not None:
        try:
            if int(tss) < 0:
                errors.append("time_spent_sec must be >= 0")
        except (TypeError, ValueError):
            errors.append("time_spent_sec must be an integer")

    return errors
