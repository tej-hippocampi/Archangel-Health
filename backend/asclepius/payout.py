"""What a case is worth, given how it was graded. Pure, bounded, itemized.

Pay was flat. ``reconcile_task_accruals`` wrote ``amount_cents=rate,
rate_cents=rate`` for every payable submission: no quality term, no difficulty
term, no specialty term. Quality acted only as a binary gate, where any payable
verdict paid full rate and only "reject both" paid nothing.

This module computes a MULTIPLIER on that rate, and nothing else. It is pure in
the same sense ``routing.py`` and ``value.py`` are pure: no store, no FastAPI,
no clock. That is deliberate and it is not only about testability. This is
algorithmic management of contractor compensation, it is a stronger version of
the question the tiering work already went to outside counsel on under NYC Local
Law 144, and a rule that decides what a physician is paid should be readable by
someone who is not going to read the application.

Four commitments, in order of importance.

**It takes no physician attribute as input.** Only facts about the CASE and how
it was graded. Not who labelled it, not their tier, not their history, not their
score across other cases. The same discipline as ``tiering.FORBIDDEN_CREDENTIAL_KEYS``
and the ``PINNED_ZERO`` weights, and ``test_payout.py`` asserts it adversarially.

**It proposes; a human decides.** Any multiplier below 1.0 is a proposal that an
admin approves before it becomes money. The algorithm never applies a pay cut on
its own. This is the same shape as the tiering tool, which the counsel memo
describes as "the tool proposes; a human decides", and it is the single most
important line in this file.

**It is bounded on both sides.** A floor, because the physician did the work and
a near-zero payout for delivered work is a wage claim rather than an incentive.
And a ceiling above 1.0, because a hard case done excellently paying MORE is the
incentive actually wanted here, and upside is a far safer instrument than pure
downside.

**Every adjustment is itemized.** A silent deduction is the worst possible
version of this feature. The reasons ride with the number, into the ledger and
onto the physician's Earnings page, in the same signed convention
``credentialing`` uses.

Not in this module: whether money moves, when it moves, and whether it is
clawed back. The no-clawback rule (a later accept may restore money, a later
reject never takes back money already approved) lives in ``payments.py`` where
the ledger state machine lives, and is unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

#: Bump when any coefficient below changes. STAMPED onto the earning, so a
#: tuned weight never restates a row that has already been approved or paid.
PAYOUT_VERSION = "2026-08-28.1"


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    """Whether quality actually moves money. OFF by default.

    Ships off for the same reason the morning routine and the news digest ship
    off: this changes what every physician is paid, and that should be a
    decision somebody makes on a particular day, not a side effect of a deploy.
    With it off, ``quality_multiplier`` returns 1.0 for everything and the
    ledger behaves exactly as it did before this module existed, so the whole
    mechanism can be merged, read, and reviewed by counsel before it is armed.

    The metric itself keeps being computed and stamped either way, so when it is
    turned on there is already a history to look at rather than a cold start.
    """
    raw = (os.getenv("ASCLEPIUS_PAYOUT_QUALITY_ENABLED", "0") or "0").strip()
    return raw not in ("", "0", "false", "False")


def floor_multiplier() -> float:
    """Never pay less than this share of the rate for delivered work.

    0.60 by default. A physician who read the chart, wrote the answer and
    submitted it has done the work; a near-zero payout for it is a wage claim,
    not an incentive. The floor is the reason this mechanism is a quality
    ADJUSTMENT and not a quality gate.
    """
    return _f("ASCLEPIUS_PAYOUT_FLOOR", 0.60)


def ceiling_multiplier() -> float:
    """The most a case can pay. 1.25 by default.

    Above 1.0 on purpose. Paying more for a hard case done excellently is the
    incentive being asked for, and upside is a far safer instrument than pure
    downside: it moves behaviour without ever taking money off a physician who
    delivered.
    """
    return _f("ASCLEPIUS_PAYOUT_CEILING", 1.25)


#: Quality-band adjustments. The bands are the ones ``contributor_score``
#: already speaks in, so the number a physician watches and the number that
#: moves their pay cannot drift apart.
#:
#: Read as: a case graded 85+ pays more; 70-85 is the expected standard and
#: pays the rate; below that the work needed rework and pays less, down to the
#: floor.
QUALITY_BANDS: Tuple[Tuple[float, float, str], ...] = (
    (85.0, 0.15, "graded 85 or above"),
    (70.0, 0.0, "graded in the expected band"),
    (55.0, -0.10, "graded below the expected band"),
    (0.0, -0.25, "graded well below the expected band"),
)

#: What the reviewer's verdict itself says about the work, separate from the
#: numeric grade. "Accept with edits" means a reviewer had to correct it before
#: it could ship, which is the "if a reviewer finds many errors" case.
VERDICT_ADJ: Dict[str, float] = {
    "accept": 0.0,
    "accept_with_edits": -0.10,
    # A rejected case is VOIDED by payments.py rather than reduced. It never
    # reaches this module with a multiplier to compute, and a value here would
    # be a second opinion about a decision already made.
    "reject": 0.0,
}


def _band(quality: float) -> Tuple[float, str]:
    for threshold, adj, label in QUALITY_BANDS:
        if quality >= threshold:
            return adj, label
    return 0.0, "ungraded"


def _signed(value: float) -> str:
    v = round(float(value) * 100.0)
    if v > 0:
        return f"+{v}%"
    if v < 0:
        return f"{v}%"
    return "±0%"


def quality_multiplier(
    *,
    quality_score: Optional[float],
    review_verdict: Optional[str] = None,
) -> Dict[str, Any]:
    """The multiplier on the base rate for one case, and why.

    ``quality_score`` is the STAMPED per-case number from
    ``contributor_score.case_score``, or None when the case has not been graded.
    ``review_verdict`` is the worst verdict any reviewer gave it.

    Returns ``{multiplier, reasons, band, adjustments, version, proposed}``.
    ``proposed`` is True when the multiplier is below 1.0, which is the flag
    that stops the ledger approving it without a human.

    An ungraded case returns 1.0 and proposes nothing. "We have not looked at it
    yet" is not a finding about the work, and paying less for it would charge a
    physician for our own review backlog.
    """
    reasons: List[str] = []
    adjustments: Dict[str, float] = {}

    if not enabled():
        return {
            "multiplier": 1.0,
            "reasons": ["full rate: quality-adjusted pay is not switched on"],
            "band": "disabled",
            "adjustments": {},
            "version": PAYOUT_VERSION,
            "proposed": False,
        }

    if quality_score is None:
        return {
            "multiplier": 1.0,
            "reasons": ["full rate: the case has not been graded"],
            "band": "ungraded",
            "adjustments": {},
            "version": PAYOUT_VERSION,
            "proposed": False,
        }

    quality = max(0.0, min(100.0, float(quality_score)))
    band_adj, band_label = _band(quality)
    if band_adj:
        adjustments["quality"] = band_adj
        reasons.append(f"{_signed(band_adj)} {band_label} ({quality:g})")
    else:
        reasons.append(f"±0% {band_label} ({quality:g})")

    verdict = (review_verdict or "").strip()
    verdict_adj = VERDICT_ADJ.get(verdict, 0.0)
    if verdict_adj:
        adjustments["verdict"] = verdict_adj
        reasons.append(f"{_signed(verdict_adj)} a reviewer corrected it before it could ship")

    raw = 1.0 + sum(adjustments.values())
    lo, hi = floor_multiplier(), ceiling_multiplier()
    multiplier = max(lo, min(hi, raw))
    if multiplier != raw:
        edge = "floor" if multiplier == lo else "ceiling"
        reasons.append(f"held at the {edge} ({multiplier:g}x)")

    return {
        "multiplier": round(multiplier, 4),
        "reasons": reasons,
        "band": band_label,
        "adjustments": adjustments,
        "version": PAYOUT_VERSION,
        # Below the rate means somebody is being paid less than the posted
        # amount. That is a decision a person makes, not one this function
        # makes. payments.py holds the row instead of approving it.
        "proposed": multiplier < 1.0,
    }


#: THE PAY HALF OF "A RICHER PROFILE MEANS BETTER ROUTING AND HIGHER PAY", AND
#: IT IS OFF. Zero means no effect, and zero is the shipped value.
#:
#: The physician profile PRD promises both halves. The routing half is real and
#: shipped (``allocation.profile_depth`` orders candidates by it). The pay half
#: is not, because it cannot be built without choosing a number, and choosing
#: what a fully-filled profile is worth in dollars is a founder's decision about
#: what the company pays for, not an engineer's decision about a coefficient.
#:
#: The specific question that needs answering, because it is not obvious: paying
#: for profile depth pays for TYPING, not for clinical work. A physician who
#: fills in six fields has told us how to route them better, which is worth
#: something; they have not labelled a better case. If the answer is that it
#: should pay, the number belongs here and the version below has to be bumped
#: with it.
#:
#: DELIBERATELY NOT WIRED INTO THE ACCRUAL PATH. A multiplier that is inert only
#: because a constant is zero is still a multiplication inside the sweep that
#: decides what a doctor is owed, and this codebase does not put unexercised
#: arithmetic there. When the number is decided, the call site goes into
#: ``payments._quality_terms`` next to the quality multiplier, with the same
#: stamping discipline: the coefficient version is recorded on the row, so a
#: later change never restates work already approved.
PROFILE_DEPTH_PAY_BONUS_MAX = 0.0


def profile_depth_multiplier(depth: float) -> float:
    """What profile depth is worth as a pay multiplier. 1.0 today, always.

    Exists so the decision has a named home and a test that proves it is inert,
    rather than living as a sentence in a PRD that a future reader has to guess
    the status of.

    Returns EXACTLY 1.0 while ``PROFILE_DEPTH_PAY_BONUS_MAX`` is zero, by
    construction and not by rounding: the caller multiplies a rate by this, and
    ``1.0000000000000002`` against a rate is how a physician gets paid a cent
    more than the posted rate for reasons nobody can explain.
    """
    if PROFILE_DEPTH_PAY_BONUS_MAX == 0.0:
        return 1.0
    d = min(1.0, max(0.0, float(depth or 0.0)))
    return 1.0 + d * PROFILE_DEPTH_PAY_BONUS_MAX


def amount_for(rate_cents: int, multiplier: float) -> int:
    """The payable amount, in cents. Rounded half-up, never below zero.

    Kept here rather than inline at the call site so there is one definition of
    how a fractional cent resolves, and it resolves in the physician's favour.
    """
    if rate_cents <= 0:
        return 0
    cents = int(rate_cents * float(multiplier) + 0.5)
    return max(0, cents)
