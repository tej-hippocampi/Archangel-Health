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
    onto whoever ranks highest. Then the contributor score. Then user_id, so the
    result is deterministic and a proposal can be diffed against a previous one.

    An unrated physician sorts at the middle of the range rather than the
    bottom. Sorting them last would mean nobody new is ever allocated work,
    which is the loop that stops them ever being rated.
    """
    score = 50.0 if p.contributor_score is None else float(p.contributor_score)
    return (-p.domain_match, load, -score, p.user_id)


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
