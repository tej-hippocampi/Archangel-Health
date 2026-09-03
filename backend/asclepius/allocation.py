"""Who should do which case. A pure proposal, never a dispatch.

There was no assignment concept at any layer: no table, no column, no endpoint,
no UI. A hundred promoted nephrology cases reached physicians purely by pull
from a specialty-filtered, oldest-first queue announced by one email. Ordering
was ``n_labels DESC, created_at ASC`` and nothing else: no load balancing, no
per-doctor cap, no reservation, no matching on the contributor score or on
domain fit. One fast labeler could take all hundred, and independence only
stopped them taking the SAME case twice.

This module answers "given these cases and these physicians, who should do
what". It is pure, in the same sense ``routing.py``, ``value.py`` and
``payout.py`` are pure: no store, no FastAPI, no clock. Everything it needs is
passed in.

Three rules it is built on.

**It proposes; an admin commits.** The same shape as the tiering tool, which the
counsel memo describes as "the tool proposes; a human decides". A proposal is a
table an operator reads, adjusts and commits, and the admin's override is
recorded rather than discarded.

**It never invents eligibility.** Everything that decides whether a physician
MAY do a case (tier, real-data approval, domain fit, independence) is read from
inputs computed by the modules that own those questions. This module ranks
within the eligible set and does nothing else. A hard gate implemented twice is
a hard gate that will disagree with itself.

**Reviewer supply is a constraint, not an afterthought.** Reserving the strongest
physicians as reviewers starves labeling, which is the throughput the whole
release depends on. The reviewer share is bounded and stated.

What this module does NOT do: it does not make an assignment exclusive, it does
not decide the queue order, and it does not enforce independence. Those live in
``store.labeler_queue_sql`` and are unchanged, because an assignment is a
PRIORITY, not a permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Two labels per case is the pairing rule PRD-R made the default, and the
#: allocator matches it rather than inventing its own number.
LABELS_PER_CASE = 2

#: One reviewer per case: the paired adjudication is a single judgment.
REVIEWERS_PER_CASE = 1

#: Nobody is allocated more than this share of a batch. A ceiling rather than a
#: strict round robin, because a batch of 100 across 10 physicians should not
#: hand exactly 10 to someone who is faster and better at the specialty than the
#: rest, and should also never hand 100 to them.
DEFAULT_MAX_SHARE = 0.35


@dataclass(frozen=True)
class Physician:
    """One candidate, reduced to what allocation is allowed to look at.

    Deliberately narrow. There is no name, no email, no tenure, no age, no
    school, no region. The allocator cannot weigh what it cannot see, which is
    the same argument ``payout.py`` makes on its signature and the same one
    ``tiering.FORBIDDEN_CREDENTIAL_KEYS`` makes at the encoder.
    """

    user_id: str
    #: Whether this physician may LABEL at all (the capability, from tiering).
    can_label: bool = True
    #: Whether they are eligible to REVIEW in this domain. Computed by
    #: ``tiering.tr_eligibility``, never by this module.
    can_review: bool = False
    #: 1.0 subspecialty, 0.5 specialty, 0.0 neither (``tiering.domain_match``).
    domain_match: float = 0.0
    #: The running contributor score, 0..100, or None when unrated.
    contributor_score: Optional[float] = None
    #: Cleared for real de-identified cases (the V4 wall).
    real_data_approved: bool = False
    #: Work already on their plate, so a batch does not land on someone who is
    #: three deep in the last one.
    open_assignments: int = 0
    #: 0..1. How much of the routable profile this physician has filled in
    #: (``profile_depth`` below). The eighth field, and the only one added since
    #: the shape was frozen, so the reasoning is written out rather than left to
    #: the reader:
    #:
    #: The physician profile PRD says a richer profile powers better routing.
    #: That was the ASK made of physicians and nothing delivered on it, which
    #: made the completeness meter a request for information in exchange for
    #: nothing. This field is what makes the promise true.
    #:
    #: It survives the "cannot weigh what it cannot see" rule because of what it
    #: is made of: ``DEPTH_FIELDS`` is an explicit allowlist of clinical
    #: self-descriptions, it shares no member with
    #: ``tiering.FORBIDDEN_CREDENTIAL_KEYS``, and it deliberately excludes every
    #: geographic field even though the profile collects one. See ``DEPTH_FIELDS``.
    profile_depth: float = 0.0


#: What "a richer profile" means for routing, stated as an allowlist rather than
#: as "whatever the completeness meter counts". The meter and this list overlap
#: but are NOT the same list, and the difference is the point.
#:
#: EVERY MEMBER IS A CLINICAL SELF-DESCRIPTION that makes a better case match
#: possible: what you subspecialise in, what you are certified in, what settings
#: you practise in, what languages you can read a chart in, how long you have
#: been doing it, and the niche you would name for yourself.
#:
#: WHAT IS DELIBERATELY ABSENT, and why it is absent rather than merely
#: forgotten:
#:
#:   * ``practice_city``. The completeness meter counts it, and it must not
#:     count here. ``tiering.PINNED_ZERO`` pins ``practice_region`` at exactly
#:     zero forever and ``FORBIDDEN_CREDENTIAL_KEYS`` bars ``practiceZip``,
#:     ``zipCode`` and ``practiceRegion`` from ever becoming a feature. A city is
#:     the same quantity at a finer grain. Letting it raise a physician's
#:     standing for work would route around a guardrail through a proxy, which
#:     is exactly the failure the guardrail exists to prevent, and it would do it
#:     while looking like a completeness bonus.
#:   * ``avatar`` and ``linkedin_url``. Both are counted by the meter, for good
#:     reasons that are about the community and the verified card. Neither tells
#:     anyone which case this physician should get, and a photograph is a
#:     protected-attribute channel with no clinical content at all.
DEPTH_FIELDS: Tuple[str, ...] = (
    "subspecialties",
    "board_certifications",
    "practice_settings",
    "languages",
    "years_in_active_practice",
    "specialty_niche",
)


def profile_depth(fields_present: Sequence[str]) -> float:
    """0..1: the share of ``DEPTH_FIELDS`` this physician has answered.

    A SHARE RATHER THAN A COUNT, so adding a seventh question later cannot
    silently demote everyone who answered the first six.

    Takes the names of the fields that are present rather than the profile
    itself, keeping this module pure and keeping the decision about what counts
    as "answered" with the caller that can see the actual values. Unknown names
    are ignored rather than rejected: a caller passing a field this list does
    not weigh should get no credit for it, not an exception in an allocator.
    """
    present = {str(f) for f in (fields_present or [])}
    hits = sum(1 for f in DEPTH_FIELDS if f in present)
    return hits / float(len(DEPTH_FIELDS))


@dataclass(frozen=True)
class Case:
    task_id: str
    specialty: str = ""
    #: True when the case is real de-identified data, which only cleared
    #: physicians may see.
    real_deid: bool = False
    #: 0..1, or None when unmeasured and undeclared.
    difficulty: Optional[float] = None


@dataclass
class Proposal:
    """What the admin is shown before anything is written."""

    assignments: List[Dict[str, Any]] = field(default_factory=list)
    unassigned: List[Dict[str, Any]] = field(default_factory=list)
    per_physician: Dict[str, Dict[str, int]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _eligible_to_label(p: Physician, case: Case) -> Tuple[bool, str]:
    if not p.can_label:
        return False, "not cleared to label"
    if case.real_deid and not p.real_data_approved:
        return False, "not cleared for real de-identified data"
    if p.domain_match <= 0.0:
        return False, "no domain match for this specialty"
    return True, ""


def _eligible_to_review(p: Physician, case: Case) -> Tuple[bool, str]:
    ok, why = _eligible_to_label(p, case)
    if not ok:
        return False, why
    if not p.can_review:
        return False, "not eligible to review in this domain"
    # The same hard clause tiering's TR eligibility already applies. Restated
    # here only to fail loudly if a caller hands over a reviewer who does not
    # meet it; the decision itself is not made here.
    if p.domain_match < 0.5:
        return False, "domain match below the reviewer threshold"
    return True, ""


def _rank_key(p: Physician, case: Case, load: int) -> Tuple:
    """How a physician is ordered for one case. Higher is better.

    Domain fit first, because a nephrologist on a nephrology case is the whole
    point. Then current load, ASCENDING, so a batch spreads instead of piling
    onto whoever ranks highest. Then the contributor score. Then profile depth.
    Then user_id, so the result is deterministic and a proposal can be diffed
    against a previous one.

    An unrated physician sorts at the middle of the range rather than the
    bottom. Sorting them last would mean nobody new is ever allocated work,
    which is the loop that stops them ever being rated.

    PROFILE DEPTH SITS BELOW THE CONTRIBUTOR SCORE AND ABOVE NOTHING BUT THE
    TIEBREAK, and that position is the whole of the design decision.

    Above ``user_id``: a filled-in profile has to beat an alphabetical accident,
    or the promise the profile page makes is not kept at all.

    Below load: a fuller profile must never be able to concentrate a batch on
    one person. The spread guarantee outranks it.

    Below the contributor score: work quality outranks self-description, always.
    Anything else would let a physician talk their way past a better labeler,
    and self-declared fields are cheap to fill in while a contributor score has
    to be earned.

    Which sounds like it makes the term inert, and it is worth being precise
    about why it does not: EVERY UNRATED PHYSICIAN CARRIES THE SAME 50.0. Among
    the people with no track record -- exactly the population where we have no
    other signal and exactly the population this is meant to help -- the score
    is a constant, and profile depth is the only thing left to sort on. That is
    a real effect on who gets the case, produced without ever outranking
    evidence of how well someone actually works.
    """
    score = 50.0 if p.contributor_score is None else float(p.contributor_score)
    return (-p.domain_match, load, -score, -float(p.profile_depth), p.user_id)


def allocate(
    cases: Sequence[Case],
    physicians: Sequence[Physician],
    *,
    labels_per_case: int = LABELS_PER_CASE,
    reviewers_per_case: int = REVIEWERS_PER_CASE,
    max_share: float = DEFAULT_MAX_SHARE,
) -> Proposal:
    """Propose who labels and who reviews each case.

    Constraints, all of them enforced here and all of them also enforced
    downstream by the queue SQL, because an assignment is a priority rather than
    a permission:

      * a physician never labels a case they are also reviewing, and never holds
        two label slots on one case (that would defeat the independence the
        second blind label exists to provide);
      * only physicians eligible for the case are considered;
      * nobody exceeds ``max_share`` of the batch;
      * reviewers are drawn from the eligible reviewer pool, and reserving them
        never leaves a case without enough labelers.

    Cases nothing can be proposed for come back in ``unassigned`` WITH A REASON.
    A case that quietly vanishes from a proposal is worse than one that arrives
    saying "no cleared nephrologist for this".
    """
    proposal = Proposal()
    if not cases:
        proposal.notes.append("No cases to allocate.")
        return proposal
    if not physicians:
        proposal.notes.append("No physicians available.")
        proposal.unassigned = [
            {"task_id": c.task_id, "reason": "no physicians available"} for c in cases
        ]
        return proposal

    slots_per_case = max(0, int(labels_per_case)) + max(0, int(reviewers_per_case))
    cap = max(1, int(len(cases) * slots_per_case * float(max_share)))
    load: Dict[str, int] = {p.user_id: int(p.open_assignments) for p in physicians}
    counts: Dict[str, Dict[str, int]] = {
        p.user_id: {"label": 0, "review": 0, "total": 0} for p in physicians
    }

    # Hardest first. A hard case has the smallest eligible pool, so allocating it
    # while the pool is still free is what stops it being the one left over.
    ordered = sorted(
        cases, key=lambda c: (-(c.difficulty if c.difficulty is not None else 0.5), c.task_id)
    )

    for case in ordered:
        taken: set = set()
        assigned_here: List[Dict[str, Any]] = []

        def _pick(role: str, eligible_fn) -> Optional[Physician]:
            pool = []
            for p in physicians:
                if p.user_id in taken:
                    continue
                if counts[p.user_id]["total"] >= cap:
                    continue
                ok, _why = eligible_fn(p, case)
                if ok:
                    pool.append(p)
            if not pool:
                return None
            pool.sort(key=lambda p: _rank_key(p, case, load[p.user_id]))
            return pool[0]

        # Reviewers FIRST. The reviewer pool is the scarce one (it is a subset of
        # the labeler pool by construction), so allocating labelers first would
        # routinely consume the only eligible reviewer and leave the case
        # unreviewable.
        for _ in range(max(0, int(reviewers_per_case))):
            pick = _pick("review", _eligible_to_review)
            if pick is None:
                break
            taken.add(pick.user_id)
            load[pick.user_id] += 1
            counts[pick.user_id]["review"] += 1
            counts[pick.user_id]["total"] += 1
            assigned_here.append({"task_id": case.task_id, "user_id": pick.user_id,
                                  "role": "review"})

        for _ in range(max(0, int(labels_per_case))):
            pick = _pick("label", _eligible_to_label)
            if pick is None:
                break
            taken.add(pick.user_id)
            load[pick.user_id] += 1
            counts[pick.user_id]["label"] += 1
            counts[pick.user_id]["total"] += 1
            assigned_here.append({"task_id": case.task_id, "user_id": pick.user_id,
                                  "role": "label"})

        n_labels = sum(1 for a in assigned_here if a["role"] == "label")
        if n_labels == 0:
            # Nothing to review either: a review assignment on a case nobody is
            # labelling is a reviewer waiting on work that will never arrive.
            for a in assigned_here:
                counts[a["user_id"]][a["role"]] -= 1
                counts[a["user_id"]]["total"] -= 1
                load[a["user_id"]] -= 1
            proposal.unassigned.append({
                "task_id": case.task_id,
                "reason": _why_nobody(case, physicians),
            })
            continue
        if n_labels < labels_per_case:
            proposal.notes.append(
                f"{case.task_id}: only {n_labels} of {labels_per_case} labelers "
                "available, so it will not produce an agreement pair.")
        proposal.assignments.extend(assigned_here)

    proposal.per_physician = {
        uid: c for uid, c in counts.items() if c["total"] > 0
    }
    if not proposal.assignments and not proposal.notes:
        proposal.notes.append("Nothing could be allocated.")
    return proposal


def _why_nobody(case: Case, physicians: Sequence[Physician]) -> str:
    """The most common reason nobody could take this case.

    Named rather than counted, because "0 assigned" sends an operator to the
    database and "no physician is cleared for real de-identified data" sends
    them to the one screen that fixes it.
    """
    reasons: Dict[str, int] = {}
    for p in physicians:
        _ok, why = _eligible_to_label(p, case)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
    if not reasons:
        return "every eligible physician is already at their share of this batch"
    return max(reasons.items(), key=lambda kv: kv[1])[0]
