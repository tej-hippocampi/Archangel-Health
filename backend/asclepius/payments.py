"""Earnings, accrual, and the billable review session (PRD-P).

This module owns money and nothing else owns money. It imports nothing from
``review.py`` or ``routing.py``: a review session is a BILLING primitive, and the
only thing this module knows about review is the string ``kind='review'``.

─── The cliff, stated plainly ────────────────────────────────────────────────
A qualifying review session is "20 CONTINUOUS minutes or $0". That threshold is a
PRODUCT DECISION, not an engineering one, and the records this module keeps exist
specifically to support it: every session's worked seconds are recorded whether or
not it qualified, a sub-threshold session is NEVER deleted, and every open,
qualify and close is written to the event log. The rule is not softened anywhere
in this file. The mitigations are (a) those records, (b) a server-authoritative
countdown so a reviewer never discovers the threshold by losing $100, and (c) an
explicit warning sentence on the session widget.

─── How time is measured ─────────────────────────────────────────────────────
There is no server-side stopwatch and no in-memory session state. Each accepted
heartbeat is one durable row; credited time is recomputed from those rows every
time it is asked for. That is what makes this restart-safe by construction — a
deploy mid-session loses nothing, because the next beat simply produces a gap.

PRD-P §1.3 states the credit rule two ways that do not agree: the formula
``Σ min(gap, MAX_GAP)`` credits up to 45 s of every gap, while the tests require
that a 30 s gap be "stitched, credited, gap NOT paid" and a 60 s gap break the
run — even though 60 s is inside the 90 s PAUSE_TOLERANCE that the parameter
table says stitches a run back together. The reading below is the only one under
which the parameter table, both tests, and the otherwise-unused
``session_beats.active`` column are simultaneously true:

  MAX_GAP governs SILENCE. PAUSE_TOLERANCE governs a DECLARED pause.

| preceding beat        | gap                    | credit    | run       |
|-----------------------|------------------------|-----------|-----------|
| active=1              | <= MAX_GAP (45 s)      | full gap  | continues |
| active=1              | >  MAX_GAP             | 0         | BREAKS    |
| active=0 (pause beat) | <= PAUSE_TOLERANCE(90) | 0         | continues |
| active=0              | >  PAUSE_TOLERANCE     | 0         | BREAKS    |

That is exactly "a network blip or a closed laptop lid versus leaving": the lid
close sends a pause beat on ``visibilitychange`` and is stitched; three minutes of
silence is not. A gap over MAX_GAP credits ZERO rather than the capped 45 s,
because crediting 45 seconds of provable absence is paying for absence.

Two numbers come out of that, and they are different questions:

  * ``credited_seconds``   — every paid-eligible second across the whole session.
                             THE RECORD (§1.4.1). Kept for every session,
                             including abandoned and sub-threshold ones.
  * ``continuous_seconds`` — the longest single unbroken run. THE QUALIFYING
                             MEASURE, because the rule says CONTINUOUS.

With clean beats the two are identical, which is why every threshold example in
the PRD reads the same under either. They diverge exactly when a reviewer left
and came back — the case the cliff is actually about.

─── Clock authority ──────────────────────────────────────────────────────────
``datetime.now(timezone.utc)`` on the server is the durable record. The client
clock never enters the calculation; a client timestamp is stored as a fraud
signal only.

PRD-P §1.3 asks that ``time.monotonic()` be trusted over the wall clock when the
two disagree by more than 2 s. Implemented here as a DETECTOR, not as an input:
consecutive beats are served by different threadpool threads and must survive a
restart, so there is no monotonic reference that spans a session — and §3 requires
``close_session`` be "a pure function of the beat rows". A payout that cannot be
recomputed from the stored record is the opposite of what §1.4's records are for.
So skew is logged, counted on the session row, and written to the event log, and
the wall clock remains the sole ledger authority.

─── Accrual is DERIVED, not hooked ───────────────────────────────────────────
PRD-P §4 says to hook the submit path. The submit route lives in
``routers/asclepius.py``, which the release's ownership rules place off limits to
this agent, and it has six-plus return points plus an optional background
pipeline. So accrual is reconciled instead: ``reconcile_task_accruals`` reads
``submissions`` (and ``case_reviews`` for the verdict, read-only, exactly as §4
requires — a read is a contract-free dependency, a callback is not) and
materialises the ledger rows that are missing. It is idempotent because
``UNIQUE(kind, ref_id)`` makes it so.

This is better than a hook, not merely permitted instead of one: it back-fills
submissions that predate the feature, cannot be bypassed by a second submit path,
and folds the auto-approve sweep into the same pass. Its one cost is that the
rate is stamped when the sweep first observes the submission rather than at the
instant of submit — bounded by how often anyone opens an Earnings page, and
irrelevant unless a rate changes inside that window.
"""

from __future__ import annotations

import json
import logging
import math
import os
import realm as _realm
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from asclepius import capabilities as _caps
from asclepius import compensation
from asclepius import referrals as _referrals

log = logging.getLogger("asclepius.payments")


class SandboxNoDisbursement(Exception):
    """Sandbox PRD §1.4: money never leaves the sandbox. ``mark_paid`` is the
    one place the ledger records a real disbursement, and in the sandbox realm
    it refuses — the routers map this to 403 ``sandbox_no_disbursement``. The
    ledger still moves accrued→approved normally, so payout LOGIC is testable
    end to end; only the final "money moved" record is denied."""

    code = "sandbox_no_disbursement"
    detail = "This is the sandbox realm: nothing is disbursed here. The ledger " \
             "moved as it would in production; only the payment record is refused."


class PaymentsDenied(Exception):
    """This account may not open a billable session.

    Raised rather than returned, and raised from ``open_session`` rather than
    checked in the router, because the router is NOT the only caller: PRD-R's
    review surface calls ``open_session`` directly (context pack §3.1) and never
    passes through ``auth.get_current_user``. A gate that lives only in the router
    is a gate R walks around without knowing it.

    ``reason`` is a short machine token for the event log; ``detail`` is what the
    physician reads. They are different strings on purpose — 'capability' is for
    us, 'this surface is for reviewers' is for them.
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail

# ─── Kinds ────────────────────────────────────────────────────────────────────
KIND_TASK = "task"
KIND_REVIEW_SESSION = "review_session"
# A one-time bounty when a referred physician's FIRST task is approved. A third
# kind rather than a second ledger, so ``UNIQUE(kind, ref_id)`` — the guard that
# makes every other payout in this module un-double-payable — covers it too, with
# ``ref_id`` being the referral_id.
KIND_REFERRAL = "referral"
# The INVITEE's side of the same settlement: a small first-case bonus, paid to
# the referred physician when the referrer's bounty settles. Same ref_id (the
# referral row), so the same UNIQUE guard covers it.
KIND_REFEREE_BONUS = "referee_first_case"
# A health-system introduction that a founder decided to pay for. ADMIN-ENTRY
# ONLY, and that is the entire design.
#
# There is no accrual path from ``hs_referrals`` to this kind: no sweep, no
# trigger, no rate constant. An institutional deal settles on negotiated terms
# over months, so there is no figure to compute and nothing that could compute
# it, which is exactly why the Referral tab prints no percentage for one (see
# docs/asclepius/REFERRALS.md). ``hs_payouts`` is admin-entry for the same
# reason, and the ledger's UNIQUE(kind, ref_id) still guards the row, with
# ``ref_id`` being the hs_referral_id.
KIND_HS_REFERRAL = "hs_referral"
SESSION_KIND_REVIEW = "review"

# ─── Ledger states ────────────────────────────────────────────────────────────
ACCRUED = "accrued"
APPROVED = "approved"
VOID = "void"
PAID = "paid"
LEDGER_STATES = (ACCRUED, APPROVED, VOID, PAID)

# ─── Session end reasons ──────────────────────────────────────────────────────
END_CLOSED = "closed"
END_EXPIRED = "expired"
END_ABANDONED = "abandoned"
END_REASONS = (END_CLOSED, END_EXPIRED, END_ABANDONED)

# ─── Beat cadence (PRD-P §1.3) ────────────────────────────────────────────────
# 15 s: 5 s is 240 writes a session of SQLite pressure for no benefit; 60 s
# credits a full minute of walking away. The client is told these numbers by the
# server so the cadence is never a client-side constant that drifts.
BEAT_INTERVAL_SECONDS = 15
MAX_GAP_SECONDS = 45          # three missed beats ends the continuous run
PAUSE_TOLERANCE_SECONDS = 90  # one resume inside 90 s stitches the run back

# Below this, beat intervals are too regular to have crossed a network.
# A SIGNAL ONLY — see ``_jitter_ms``. Nothing in this module refuses a payout
# because of it: the cost of a false positive is not paying a physician $100.
MIN_HUMAN_JITTER_MS = 50.0

# The key P's OWN client sends when its caller named no work — `session:<id>`.
#
# This is a convention shared between payments and its client, and it deliberately
# encodes nothing about review: the server recognises the shape without knowing
# what a review pair is. It exists so an unnamed session is legible as OUR
# integration gap rather than misread as a fact about the physician — see
# `_flag_low_confidence`.
SESSION_FALLBACK_PREFIX = "session:" 

# Wall-vs-monotonic disagreement that is worth recording.
CLOCK_SKEW_TOLERANCE_SECONDS = 2.0


# ═══ Configuration ════════════════════════════════════════════════════════════
# Rates live in env so a change is a redeploy, not a migration — and the rate in
# force is STAMPED ON THE LEDGER ROW at accrual time, so changing one of these
# can never restate a past earning.
def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("asclepius.payments: %s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < 0:
        log.warning("asclepius.payments: %s=%d is negative; using %d", name, value, default)
        return default
    return value


def tl_rate_cents() -> int:
    """$75 for one completed submission."""
    return _env_int("ASCLEPIUS_TL_RATE_CENTS", 7500)


def tr_session_cents() -> int:
    """$100 for one qualifying review session."""
    return _env_int("ASCLEPIUS_TR_SESSION_CENTS", 10000)


def tr_min_seconds() -> int:
    """20 minutes of continuous, server-measured time."""
    return _env_int("ASCLEPIUS_TR_MIN_SECONDS", 1200)


def referral_bounty_cents() -> int:
    """$50 to the REFERRER when a physician they referred is verified and
    completes their first accepted case.

    The larger half of a $50/$25 split, with the smaller half going to the
    person being referred (see referee_bonus_cents). The referrer is the scarce
    input: a well-connected physician who will actually spend their reputation
    introducing colleagues is worth more to us than the marginal signup, and the
    payment has to be large enough to be worth the ask.

    The Sep 1 meeting was read as reversing this split. It is ambiguous on the
    point: it says "the people who refer get a free $50" and closes by settling
    the signing bonus at "$25 for completing the case", both of which match the
    number here, against one line in the middle that says the reverse. Two
    readings out of three, and the behavior already live in production, keep the
    larger half with the referrer, so the tie breaks toward not silently
    restating what physicians are already promised. Settle it deliberately
    before moving it.

    A ONE-TIME BOUNTY, not a percentage of the colleague's ongoing work, and the
    reasoning is worth keeping next to the number. A revenue share creates an
    indefinite liability against every future task; it is a compliance question
    the moment anyone asks the recruiting question above; and, the practical
    objection, it is unexplainable on a dashboard. *"$50 when your colleague
    completes their first case"* is a sentence a doctor can hold in their head.
    A trailing percentage is a spreadsheet.

    Moving this rate is safe. Like every other rate here it lives in env and is
    STAMPED ON THE LEDGER ROW at accrual, so a bounty already earned keeps the
    rate it was earned at and only future accruals move.
    """
    return _env_int("ASCLEPIUS_REFERRAL_BOUNTY_CENTS", 5000)


def referee_bonus_cents() -> int:
    """$25 to the REFERRED physician after their first accepted case.

    The activation half of the split: the first accepted case is where a new
    physician either stays or is never seen again, and the meeting settled this
    side explicitly at "$25 for completing the case". Paid only when a
    referrer's bounty settles, which inherits every guard that settlement runs
    (QA-accepted work, no self-referral, verified account).

    Stamped on the ledger row at accrual exactly like the bounty, so raising or
    lowering it later cannot restate a bonus already paid."""
    return _env_int("ASCLEPIUS_REFEREE_BONUS_CENTS", 2500)


def referral_cap_cents() -> int:
    """The most one referrer can earn in bounties, ever. **0 means no cap.**

    This was $5,200 (104 x $50) and advertised as a ceiling on the Referral tab,
    which had it exactly backwards: the ceiling was the first thing a physician
    read about a program whose whole point is that a well-connected doctor can
    introduce us to a hundred colleagues and a health system. Capping the person
    who does that, and telling them so up front, is paying for the referrals we
    were going to get anyway and pricing out the ones we were not.

    The env var stays so a cap can be reimposed without a deploy, and the
    enforcement below stays written and tested for the same reason. When it is
    set, it is still read from the LEDGER at accrual, so a historical rate change
    cannot bend it, and a referral past the cap settles as ineligible rather than
    sitting pending forever.
    """
    return _env_int("ASCLEPIUS_REFERRAL_CAP_CENTS", 0)


def tl_auto_approve_days() -> int:
    """A labeler is never held hostage by a review backlog. If nobody reviews
    their work inside this window, it approves."""
    return _env_int("ASCLEPIUS_TL_AUTO_APPROVE_DAYS", 14)


def tr_min_progress_keys() -> int:
    """Distinct pieces of work a session must have named to qualify (audit C2).

    **Defaults to 1, and that default is the considered position, not a
    placeholder.** A reviewer who spends twenty honest minutes adjudicating a
    single genuinely hard pair names exactly one key, and they are precisely the
    physician this feature exists to pay. Raising this above 1 without evidence
    would refuse them $100 to inconvenience a script that can trivially rotate
    keys anyway.

    It exists as a hook so that when there IS evidence — the counts are now
    recorded on every session row — the bar moves with an env change and a
    redeploy rather than a migration."""
    return _env_int("ASCLEPIUS_TR_MIN_PROGRESS_KEYS", 1)


def progress_key_max_seconds() -> int:
    """Credit ceiling for time spent on ONE progress key.

    Set well clear of ``tr_min_seconds`` so it can never cost an honest reviewer a
    session: at 40 minutes against a 20-minute threshold, a physician wrestling
    with one hard case has already qualified and been paid long before this
    binds. What it bounds is an unattended run that holds a single key for hours."""
    return _env_int("ASCLEPIUS_PROGRESS_KEY_MAX_SECONDS", 2400)


def session_abandon_seconds() -> int:
    """Silence after which an open session is considered abandoned.

    PRD-P names ``abandoned`` as an end reason but no rule produces one. Without
    this, an open session whose close beacon was lost is returned by
    ``open_session`` forever and the reviewer can never start a new billable
    session — idempotency turns into a trap. An abandoned session is finalised
    through the NORMAL close path, so one that already earned its 20 minutes is
    still paid."""
    return _env_int("ASCLEPIUS_SESSION_ABANDON_SECONDS", 900)


def session_max_seconds() -> int:
    """Wall-clock ceiling on one session. A tab left open across a weekend should
    not still be an open billable session on Monday; it expires."""
    return _env_int("ASCLEPIUS_SESSION_MAX_SECONDS", 8 * 3600)


def client_params() -> Dict[str, int]:
    """Everything the browser needs, served BY the server. The client holds no
    copy of the cadence or the threshold — a duplicated constant is a constant
    that will disagree."""
    return {
        "beat_interval_seconds": BEAT_INTERVAL_SECONDS,
        "max_gap_seconds": MAX_GAP_SECONDS,
        "pause_tolerance_seconds": PAUSE_TOLERANCE_SECONDS,
        "min_seconds": tr_min_seconds(),
        "rate_cents": tr_session_cents(),
    }


# ═══ Time ═════════════════════════════════════════════════════════════════════
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    """Microsecond-resolution naive-UTC ISO, for sessions and beats.

    Second resolution would round every one of ~80 gaps in a 20-minute session,
    and those roundings sum: up to a minute-and-a-bit of error against a
    1,200-second threshold. Naive (no offset suffix) to match every other
    timestamp in this database, and fixed-width so lexical ordering is
    chronological ordering."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def _ledger_ts(dt: datetime) -> str:
    """Second-resolution naive-UTC ISO, for ledger rows.

    Deliberately the same shape as ``store._utcnow_iso`` so the auto-approve
    sweep's ``accrued_at < cutoff`` comparison — which SQLite performs as a
    STRING comparison — is chronologically correct across every row this module
    writes. Mixing formats in one column is how a lexical date filter starts
    quietly skipping rows."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO parse → aware UTC. Handles this module's naive stamps, the
    rest of the database's naive stamps, and a ``Z``-suffixed value from anywhere
    else. A naive value is UTC: every writer in this codebase writes UTC."""
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _mint_nonce() -> str:
    return uuid.uuid4().hex


# ═══ The credit calculation — a pure function of the beat rows ════════════════
def work_was_named(keys) -> bool:
    """Did the CALLER name the work, or did P's own client fill in for it?

    A session whose only key is the fallback tells us nothing about the reviewer.
    It tells us that the surface driving the beats did not pass a work identity —
    which is a gap on our side of a seam, and must never be read as a signal about
    the person being paid."""
    real = [k for k in keys if k and not str(k).startswith(SESSION_FALLBACK_PREFIX)]
    return bool(real)


def credit_from_beats(
    beats: List[Dict[str, Any]], *, min_seconds: int,
    max_gap: int = MAX_GAP_SECONDS, pause_tolerance: int = PAUSE_TOLERANCE_SECONDS,
    min_progress_keys: Optional[int] = None, key_max_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Total credited seconds, longest continuous run, and whether it qualifies.

    Pure: same rows in, same numbers out, forever. That is what makes a payout
    reconstructible from the record months later, and what makes
    ``close_session`` safe to call repeatedly.

    ``beats`` must be ordered by ``seq``. Server timestamps are used; a beat whose
    timestamp will not parse is skipped rather than being allowed to poison the
    arithmetic with a zero.

    A gap is attributed to the work named by the beat that OPENED it — during the
    interval between two beats the reviewer was on the earlier beat's case. Time
    on any one key is credited up to ``key_max_seconds``; past that the run
    continues (the reviewer is still beating) but stops being payable.
    """
    if min_progress_keys is None:
        min_progress_keys = tr_min_progress_keys()
    if key_max_seconds is None:
        key_max_seconds = progress_key_max_seconds()

    stamps: List[Tuple[datetime, bool, Optional[str]]] = []
    skipped = 0
    for b in beats:
        ts = _parse(b.get("server_ts"))
        if ts is None:
            log.warning("asclepius.payments: unparseable server_ts on beat %r", b.get("beat_id"))
            skipped += 1
            continue
        key = (b.get("progress_key") or "").strip() or None
        stamps.append((ts, bool(b.get("active")), key))

    credited = 0.0
    run = 0.0
    longest = 0.0
    intervals_ms: List[float] = []
    per_key: Dict[str, float] = {}
    keys_seen = {s[2] for s in stamps if s[2]}

    for i in range(1, len(stamps)):
        prev_ts, prev_active, prev_key = stamps[i - 1]
        gap = (stamps[i][0] - prev_ts).total_seconds()
        # Never negative: seq is the ordering authority, and a clock stepping
        # backwards between two beats must not subtract credited time.
        if gap < 0:
            gap = 0.0
        if prev_active:
            if gap <= max_gap:
                # Only WORKING intervals feed the jitter signal. Folding a
                # five-minute absence in as one enormous interval would inflate
                # the standard deviation and hide the very regularity the signal
                # is looking for.
                intervals_ms.append(gap * 1000.0)
                # A beat that named no work buys no time. Legacy rows written
                # before progress_key was required land here; the route now
                # refuses such a beat outright.
                if prev_key is None:
                    payable = 0.0
                else:
                    spent = per_key.get(prev_key, 0.0)
                    payable = max(0.0, min(gap, float(key_max_seconds) - spent))
                    per_key[prev_key] = spent + payable
                credited += payable
                run += payable
            else:
                longest = max(longest, run)
                run = 0.0
        else:
            # A DECLARED pause. Credits nothing either way; the only question is
            # whether the run survives it.
            if gap > pause_tolerance:
                longest = max(longest, run)
                run = 0.0
    longest = max(longest, run)

    # floor, not round (audit L1). ``round`` made 1199.5 s qualify at a 1200 s
    # threshold, and the threshold is the legally sensitive number in this
    # feature — it should never be reached by a rounding rule.
    credited_i = int(math.floor(credited))
    longest_i = int(math.floor(longest))
    distinct = len(keys_seen)

    return {
        "work_named": work_was_named(keys_seen),
        "credited_seconds": credited_i,
        "continuous_seconds": longest_i,
        "qualified": longest_i >= int(min_seconds) and distinct >= int(min_progress_keys),
        "jitter_ms": _jitter_ms(intervals_ms),
        "beats": len(stamps),
        "skipped_beats": skipped,
        "distinct_progress_keys": distinct,
    }


def _jitter_ms(intervals_ms: List[float]) -> Optional[float]:
    """Standard deviation of working beat intervals, in milliseconds.

    Near-zero jitter is machine-generated — a human on ``setInterval`` still shows
    network jitter. This is recorded and alerted on and NEVER acted upon: the cost
    of a false positive is refusing to pay a doctor. Returns None below three
    intervals, where a standard deviation means nothing.

    We deliberately do NOT build behavioural biometrics — mouse-path curvature,
    keystroke dynamics. That is a false-positive machine aimed at the one outcome
    this feature must not produce. If fraud turns out to be real, buy a solution."""
    if len(intervals_ms) < 3:
        return None
    try:
        return round(statistics.pstdev(intervals_ms), 3)
    except statistics.StatisticsError:
        return None


def _ratchet(
    result: Dict[str, Any], session_row: Dict[str, Any], *, min_seconds: int,
) -> Dict[str, Any]:
    """Never let a recomputation take back seconds that were already persisted.

    Credited time is recomputed from the beat rows on every read, which is what
    makes it restart-safe — and also what makes it vulnerable to the rows changing
    meaning underneath it. An NTP step or a VM migration mid-session restamps a
    beat, the next gap becomes enormous, the run breaks, and a session the server
    already counted to twenty minutes silently becomes worth $0 (audit H3).

    This is a floor, not a fudge. The stored value was itself computed from a
    strict SUBSET of the same beats — every earlier beat is still there — so
    holding at it can only preserve a number that was genuinely earned once. It
    can never invent one, which is why a short session stays short.

    The asymmetry is deliberate. Going UP is ordinary: the reviewer kept working.
    Going DOWN means the record changed under a number somebody was already shown,
    and the physician is not the right person to absorb that.
    """
    stored_credited = int(session_row.get("credited_seconds") or 0)
    stored_continuous = int(session_row.get("continuous_seconds") or 0)
    if (result["credited_seconds"] >= stored_credited
            and result["continuous_seconds"] >= stored_continuous):
        return result

    result = dict(result)
    result["regressed"] = {
        "stored_credited_seconds": stored_credited,
        "stored_continuous_seconds": stored_continuous,
        "recomputed_credited_seconds": result["credited_seconds"],
        "recomputed_continuous_seconds": result["continuous_seconds"],
        "skipped_beats": int(result.get("skipped_beats") or 0),
    }
    result["credited_seconds"] = max(result["credited_seconds"], stored_credited)
    result["continuous_seconds"] = max(result["continuous_seconds"], stored_continuous)
    # Re-decide against the floored number, and against the key policy that was
    # already applied — a session held at its stored value is still held to the
    # same threshold.
    result["qualified"] = (
        result["continuous_seconds"] >= int(min_seconds)
        and int(result.get("distinct_progress_keys") or 0) >= tr_min_progress_keys())
    return result


def _log_regression(store, *, session_id: str, session: Dict[str, Any],
                    result: Dict[str, Any]) -> None:
    """A binding ratchet means the infrastructure did something. Pay, and say so."""
    regressed = result.get("regressed")
    if not regressed:
        return
    log.error(
        "asclepius.payments: recomputed credit for session %s went BACKWARDS "
        "(%ds -> %ds continuous): held at the stored value and flagged for payout "
        "review. This is an infrastructure event, not a reviewer's fault.",
        session_id, regressed["stored_continuous_seconds"],
        regressed["recomputed_continuous_seconds"])
    store.log_event(
        entity_type="work_session", entity_id=session_id,
        event_type="session_credit_regressed", actor=session.get("user_id"),
        payload={**regressed, "action": "held_at_stored_value"},
    )


# ═══ Clock skew detection (signal only) ═══════════════════════════════════════
# Process-local, and honest about it: it can only compare two beats that happened
# to be served by THIS process, which is why it can never be a ledger input.
_MONOTONIC_REF: Dict[str, Tuple[float, datetime]] = {}
_MONOTONIC_REF_MAX = 2048


def _check_clock_skew(session_id: str, wall: datetime) -> Optional[float]:
    """Returns the observed |wall − monotonic| discrepancy in seconds when it
    exceeds tolerance, else None."""
    mono = time.monotonic()
    prev = _MONOTONIC_REF.get(session_id)
    _MONOTONIC_REF[session_id] = (mono, wall)
    if len(_MONOTONIC_REF) > _MONOTONIC_REF_MAX:      # bounded; it is a cache
        _MONOTONIC_REF.pop(next(iter(_MONOTONIC_REF)), None)
    if prev is None:
        return None
    delta = abs((wall - prev[1]).total_seconds() - (mono - prev[0]))
    return delta if delta > CLOCK_SKEW_TOLERANCE_SECONDS else None


def _forget_session_clock(session_id: str) -> None:
    _MONOTONIC_REF.pop(session_id, None)
    _SKEW_LOGGED.discard(session_id)
    _JITTER_LOGGED.discard(session_id)


# Say each per-session signal once (audit L2). Bounded and process-local: losing
# these on a restart costs one extra log line, which is the right way round.
_SKEW_LOGGED: set = set()
_JITTER_LOGGED: set = set()
_LOGGED_MAX = 2048


def _first_time(seen: set, session_id: str) -> bool:
    if session_id in seen:
        return False
    if len(seen) > _LOGGED_MAX:
        seen.clear()
    seen.add(session_id)
    return True


# ═══ Who may open a billable session (audit C1) ═══════════════════════════════
def _authorize_session(store, *, user_id: str, kind: str) -> Dict[str, Any]:
    """Three gates, checked in the order that produces the most useful refusal.

    Before this existed, the entire gate on a $100-per-20-minutes endpoint was
    "are you authenticated?" — so an approved LABELER, an account with no tier
    assigned yet, and the internal ``qa_reviewer`` ops role could all open one and
    earn. None of them were doing review work; two of them are not even
    physicians doing clinical work on that surface.

    The tier is read through ``capabilities.can`` and never off ``users.tier``
    (context pack §3.3). A literal ``tier == "reviewer"`` here would be the exact
    defect ``capabilities.py`` was built to remove, and it fails SILENTLY: "this
    user is not a reviewer" is a legitimate answer for a labeler, so nothing logs
    and the advisor tier quietly loses a surface it is entitled to.

    THE VERIFICATION CHECK BELOW IS NOT REDUNDANT. It used to be: every
    evaluator route went through ``auth.get_current_user``, which refused
    ``pending`` outright, and this was defence in depth.

    A physician awaiting verification now reaches the product (they get the
    practice case and the community while we check their credentials), so
    ``pending`` no longer bounces off the shared dependency. Earnings and
    billable sessions are held shut by the EARNINGS surface on the HTTP routes
    and by THIS CHECK for every caller that does not come through one, which
    includes ``open_session`` called directly.

    Do not delete this as duplicated logic. A payment gate should never be one
    refactor of somebody else's middleware away from opening, and today it is
    the only verification check standing between a provisional account and the
    money ledger.
    """
    user = store.get_user_by_id(user_id or "")
    if user is None:
        raise PaymentsDenied("unknown_user", "No such account.")
    if not user.get("active"):
        raise PaymentsDenied(
            "inactive", "This account is not active.")

    status = user.get("verification_status")
    # NULL passes: a pre-verification-era account is 'never asked', which is a
    # different fact from 'refused'. That tri-state is the same one held
    # everywhere else in this codebase and narrowing it here would lock out every
    # physician who signed up before credential review existed.
    if status in ("pending", "rejected") and user.get("role") != "admin":
        raise PaymentsDenied(
            "verification",
            "Your account is awaiting credential verification."
            if status == "pending"
            else "This account was not approved for the evaluator portal.")

    if kind == SESSION_KIND_REVIEW and not _caps.can(user, _caps.REVIEW):
        raise PaymentsDenied(
            "capability",
            "Review sessions are for physicians with the reviewer tier. "
            "If you believe this is wrong, contact your workspace admin.")
    return user


# ═══ §3.1 — the frozen contract ═══════════════════════════════════════════════
def open_session(store, *, user_id: str, kind: str) -> Dict[str, Any]:
    """kind: 'review'. Returns {session_id, started_at, min_seconds,
    credited_seconds, nonce, qualified}. Idempotent: an open session for this
    user+kind is returned rather than a second one being created.

    Raises ``PaymentsDenied`` when this account may not open one — see
    ``_authorize_session``. The signature is the frozen §3.1 contract, so the
    gate loads the user itself rather than taking one; that is also what makes it
    hold for PRD-R, which calls this function and not the route.

    Any open session that has gone silent past ``session_abandon_seconds`` — or
    that has been open past ``session_max_seconds`` — is finalised first, through
    the normal close path, so a session that already earned its time is still
    paid even though nobody closed it.
    """
    kind = (kind or SESSION_KIND_REVIEW).strip() or SESSION_KIND_REVIEW
    _authorize_session(store, user_id=user_id, kind=kind)
    now = _now()

    fresh = []
    for existing in store.list_open_work_sessions(user_id=user_id, kind=kind):
        stale_reason = _stale_reason(existing, now)
        if stale_reason:
            close_session(store, session_id=existing["session_id"], reason=stale_reason)
        else:
            fresh.append(existing)

    live = None
    if fresh:
        # Belt to the partial unique index's braces. If two open sessions ever
        # coexist, the LIVE one is the one that beat most recently — that is the
        # one a client is actually driving. Settle the others through the normal
        # close path so any time they earned is still paid.
        fresh.sort(key=lambda s: (s.get("last_beat_at") or s.get("started_at") or ""))
        live = fresh[-1]
        for stray in fresh[:-1]:
            close_session(store, session_id=stray["session_id"], reason=END_ABANDONED)

    if live is not None:
        return _session_view(store, live, existing_session=True)

    session_id = _new_id("ws")
    min_seconds = tr_min_seconds()
    rate_cents = tr_session_cents()
    try:
        row = store.insert_work_session(
            session_id=session_id, user_id=user_id, kind=kind, started_at=_ts(now),
            nonce=_mint_nonce(), min_seconds=min_seconds, rate_cents=rate_cents,
        )
    except Exception:
        # Lost the race to a concurrent open (the partial unique index refused
        # the second insert). Return the winner rather than 500-ing on what is,
        # from the reviewer's point of view, a successful open.
        concurrent = store.open_work_session_row(user_id=user_id, kind=kind)
        if concurrent is None:
            raise
        log.info("asclepius.payments: concurrent open_session for %s/%s resolved to %s",
                 user_id, kind, concurrent["session_id"])
        return _session_view(store, concurrent, existing_session=True)

    store.log_event(
        entity_type="work_session", entity_id=session_id, event_type="session_opened",
        actor=user_id, payload={"kind": kind, "min_seconds": min_seconds,
                                "rate_cents": rate_cents},
    )
    return _session_view(store, row, existing_session=False)


def heartbeat(
    store, *, session_id: str, nonce: str, active: bool,
    progress_key: Optional[str] = None, seq: Optional[int] = None,
    client_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Returns {credited_seconds, qualified, next_nonce, ended}. Server clock only.

    The rotating nonce is the anti-fraud control that costs nothing: each response
    carries the nonce the NEXT beat must present, so a naive ``setInterval(fetch)``
    script cannot beat without parsing every response.

    ``seq`` must strictly increase. ``client_ts`` is recorded as a fraud signal and
    never enters any calculation.
    """
    # A beat that does not say what it is a beat FOR is not evidence of anything
    # (audit C2). Before this, credit was a function of elapsed wall time with a
    # request attached, and 28 blind POSTs 45 s apart were worth $100.
    #
    # The key stays OPAQUE here — counted, capped and recorded, never parsed or
    # resolved. The moment payments looks up what a key means it has taken a
    # dependency on PRD-R's schema and the seam in §8 is gone. Verifying that the
    # key names work the server actually issued is R's half of the contract.
    progress_key = (progress_key or "").strip() or None
    if progress_key is None:
        return _beat_error(
            "missing_progress_key",
            "A heartbeat must name the work it is a beat for.")

    session = store.get_work_session(session_id)
    if session is None:
        return _beat_error("not_found", "Session not found.")
    if session.get("ended_at"):
        return {
            "credited_seconds": int(session.get("credited_seconds") or 0),
            "continuous_seconds": int(session.get("continuous_seconds") or 0),
            "qualified": bool(session.get("qualified")),
            "next_nonce": None, "ended": True,
            "end_reason": session.get("end_reason"),
            "min_seconds": int(session.get("min_seconds") or tr_min_seconds()),
            "remaining_seconds": 0,
            "ok": True, "error": None,
        }

    now = _now()
    # A session open past the ceiling is expired here rather than beaten forever.
    started = _parse(session.get("started_at"))
    if started is not None and (now - started).total_seconds() > session_max_seconds():
        closed = close_session(store, session_id=session_id, reason=END_EXPIRED)
        return {**closed, "next_nonce": None, "ended": True, "ok": True, "error": None}

    next_seq = int(seq) if seq is not None else None
    if next_seq is None:
        # ``seq`` used to be optional forever, with the server deriving
        # MAX(seq)+1 — which is precisely the affordance a replayer wants, since
        # it means never having to know or send a sequence at all (audit H1).
        #
        # It stays optional for exactly one beat: a session with no beats behind
        # it has nothing to replay, and letting the server number the first one
        # keeps a fresh open simple. From the second beat on there IS something
        # to replay, and the client must say where it is.
        last = _last_seq(store, session_id)
        if last:
            return _beat_error("missing_seq", _BEAT_ERRORS["missing_seq"])
        next_seq = last + 1

    skew = _check_clock_skew(session_id, now)
    min_seconds = int(session.get("min_seconds") or tr_min_seconds())
    before_continuous = int(session.get("continuous_seconds") or 0)

    def _credit(beat_rows, session_row):
        want = int(session_row.get("min_seconds") or min_seconds)
        result = _ratchet(
            credit_from_beats(beat_rows, min_seconds=want), session_row, min_seconds=want)
        result["clock_skew"] = skew is not None
        return result

    result = store.record_session_beat(
        session_id=session_id, nonce=nonce, seq=next_seq, active=bool(active),
        progress_key=progress_key, client_ts=client_ts, server_ts=_ts(now),
        next_nonce=_mint_nonce(), credit_fn=_credit, beat_id=_new_id("beat"),
    )
    if not result.get("ok"):
        return _beat_error(result.get("error") or "rejected", _BEAT_ERRORS.get(
            result.get("error") or "", "Heartbeat rejected."), session=result.get("session"))

    _log_regression(store, session_id=session_id, session=session, result=result)

    # Both of these are per-SESSION facts, not per-beat ones, so they are said
    # once per session (audit L2). Logged on every beat, modest NTP drift alone
    # produced eighty WARNING lines a session — which is how a real signal gets
    # filtered out by the person who has learned to ignore the noisy one.
    if skew is not None and _first_time(_SKEW_LOGGED, session_id):
        log.warning(
            "asclepius.payments: clock skew %.2fs on session %s: wall clock kept as the "
            "ledger authority (signal only). Further skew on this session is not logged.",
            skew, session_id)
        store.log_event(
            entity_type="work_session", entity_id=session_id, event_type="clock_skew",
            actor=session.get("user_id"),
            payload={"delta_seconds": round(skew, 3),
                     "tolerance_seconds": CLOCK_SKEW_TOLERANCE_SECONDS},
        )

    jitter = result.get("jitter_ms")
    if (jitter is not None and jitter < MIN_HUMAN_JITTER_MS
            and _first_time(_JITTER_LOGGED, session_id)):
        # Logged and alerted, never auto-rejected (PRD-P §3). The actionable
        # artifact is the close-time event, which combines this with the distinct
        # key count; this line is a breadcrumb, so once is enough.
        log.warning(
            "asclepius.payments: beat jitter %.1fms below the %.0fms human floor on "
            "session %s: recorded as a signal, payout unaffected",
            jitter, MIN_HUMAN_JITTER_MS, session_id)

    # The moment the threshold is crossed, once, with the credited seconds.
    if result["qualified"] and before_continuous < min_seconds:
        store.log_event(
            entity_type="work_session", entity_id=session_id, event_type="session_qualified",
            actor=session.get("user_id"),
            payload={"credited_seconds": result["credited_seconds"],
                     "continuous_seconds": result["continuous_seconds"],
                     "min_seconds": min_seconds},
        )

    return {
        "credited_seconds": result["credited_seconds"],
        "continuous_seconds": result["continuous_seconds"],
        "qualified": bool(result["qualified"]),
        "next_nonce": result.get("next_nonce"),
        "seq": result.get("seq"),
        "ended": False,
        "min_seconds": min_seconds,
        "remaining_seconds": max(0, min_seconds - result["continuous_seconds"]),
        "ok": True, "error": None,
    }


def close_session(store, *, session_id: str, reason: str = END_CLOSED) -> Dict[str, Any]:
    """Returns {credited_seconds, qualified, payout_cents}. Pure function of the
    heartbeat rows — safe to call repeatedly.

    Idempotent under repetition AND under concurrency: the recompute, the session
    update and the ledger insert happen inside one ``BEGIN IMMEDIATE``
    transaction, and the ledger insert leans on ``UNIQUE(kind, ref_id)`` rather
    than on having checked first.

    A session that does not qualify still closes, still records its seconds, and
    is never deleted. A contributor who does not accrue payment
    (``compensation.accrues_payment`` false — an equity-holding advisor) closes
    identically and writes no ledger row: their work still counts everywhere
    QUALITY is measured, only money is excluded.
    """
    reason = reason if reason in END_REASONS else END_CLOSED
    session = store.get_work_session(session_id)
    if session is None:
        return {"credited_seconds": 0, "continuous_seconds": 0, "qualified": False,
                "payout_cents": 0, "ended": False, "ok": False, "error": "not_found"}

    user = store.get_user_by_id(session.get("user_id") or "")
    # The predicate exists precisely so an equity-holding advisor cannot silently
    # accrue a cash obligation. Never write your own.
    payable = compensation.accrues_payment(user)
    rate_cents = int(session.get("rate_cents") or tr_session_cents())
    min_seconds = int(session.get("min_seconds") or tr_min_seconds())
    now = _now()

    earning = None
    if payable:
        earning = {
            "earning_id": _new_id("earn"),
            "user_id": session.get("user_id"),
            "kind": KIND_REVIEW_SESSION,
            "ref_id": session_id,
            # The rate is stamped at accrual time and read back from the SESSION
            # row, which stamped it at open. A rate change mid-session cannot
            # restate it.
            "amount_cents": rate_cents,
            "rate_cents": rate_cents,
            # A qualifying review session is approved on arrival: unlike a labeler
            # submission there is no downstream verdict that could reject it. The
            # threshold WAS the review.
            "status": APPROVED,
            "accrued_at": _ledger_ts(now),
            "resolved_at": _ledger_ts(now),
            "note": None,
        }

    def _credit(beat_rows, session_row):
        want = int(session_row.get("min_seconds") or min_seconds)
        return _ratchet(
            credit_from_beats(beat_rows, min_seconds=want), session_row, min_seconds=want)

    result = store.finalize_work_session(
        session_id=session_id, end_reason=reason, ended_at=_ts(now),
        credit_fn=_credit, earning=earning,
    )
    if not result.get("ok"):
        return {"credited_seconds": 0, "continuous_seconds": 0, "qualified": False,
                "payout_cents": 0, "ended": False, "ok": False,
                "error": result.get("error")}

    _forget_session_clock(session_id)
    written = result.get("earning")
    payout = int(written["amount_cents"]) if written else 0

    if not result.get("already_ended"):
        _log_regression(store, session_id=session_id, session=session, result=result)
        _flag_low_confidence(store, session_id=session_id, session=session, result=result,
                             payout_cents=payout)
        store.log_event(
            entity_type="work_session", entity_id=session_id, event_type="session_closed",
            actor=session.get("user_id"),
            payload={
                "reason": reason,
                # Recorded for EVERY session, including the ones that earned
                # nothing. "We have no records" is the worst possible answer to a
                # challenge about the cliff.
                "credited_seconds": result["credited_seconds"],
                "continuous_seconds": result["continuous_seconds"],
                "min_seconds": min_seconds,
                "qualified": bool(result["qualified"]),
                "payable": payable,
                "payout_cents": payout,
                # False means the surface driving the beats named no work, so
                # this session's key count says nothing about the physician.
                "work_named": bool(result.get("work_named")),
            },
        )

    return {
        "credited_seconds": result["credited_seconds"],
        "continuous_seconds": result["continuous_seconds"],
        "qualified": bool(result["qualified"]),
        "payout_cents": payout,
        "min_seconds": min_seconds,
        "ended": True,
        "end_reason": result.get("end_reason") or reason,
        "ok": True, "error": None,
    }


def _flag_low_confidence(
    store, *, session_id: str, session: Dict[str, Any], result: Dict[str, Any],
    payout_cents: int,
) -> None:
    """Raise a reviewable event when a QUALIFYING session looks machine-made.

    Neither signal is worth acting on alone, and that is the whole point of
    combining them. Humans do hold one hard case for twenty minutes, so a single
    progress key proves nothing. A clean network on a fast machine can look
    regular, so low jitter proves nothing. A session that ran the full threshold
    on one piece of work with machine-perfect beat spacing is a different claim,
    and it is one no browser on a real network produces.

    The answer is still to PAY and FLAG, never to refuse. The cost of a false
    positive here is a physician not being paid $100 for work they did — which is
    worse than paying for one session that turns out to be scripted, because the
    second is recoverable and the first is how you lose a doctor. This writes the
    artifact a human needs to make that call afterwards.
    """
    if not result.get("qualified"):
        return
    reasons = []
    # ``single_key`` means "the reviewer stayed on one piece of work". That is a
    # conclusion we are not entitled to draw when the client never named any work
    # — and since P's own client falls back to a session-scoped key whenever its
    # caller passes none, counting it would fire this flag on every session in the
    # fleet and bury the one that deserved a look.
    if result.get("work_named") and int(result.get("distinct_progress_keys") or 0) <= 1:
        reasons.append("single_key")
    jitter = result.get("jitter_ms")
    if jitter is not None and jitter < MIN_HUMAN_JITTER_MS:
        reasons.append("no_jitter")
    if len(reasons) < 2:
        return
    log.error(
        "asclepius.payments: session %s qualified for %d cents with %s, PAID and "
        "flagged for review, not refused", session_id, payout_cents, "+".join(reasons))
    store.log_event(
        entity_type="work_session", entity_id=session_id,
        event_type="session_low_confidence", actor=session.get("user_id"),
        payload={
            "reasons": reasons,
            "distinct_progress_keys": int(result.get("distinct_progress_keys") or 0),
            "jitter_ms": jitter,
            "credited_seconds": result.get("credited_seconds"),
            "continuous_seconds": result.get("continuous_seconds"),
            "resume_count": int(session.get("resume_count") or 0),
            "payout_cents": payout_cents,
            "action": "paid_and_flagged",
        },
    )


# ─── Session helpers ──────────────────────────────────────────────────────────
_BEAT_ERRORS = {
    "not_found": "Session not found.",
    "ended": "This session has already ended.",
    "stale_nonce": "Stale session token, this beat was not accepted.",
    "replayed_seq": "Replayed heartbeat sequence, this beat was not accepted.",
    "missing_seq": "This session already has beats; a heartbeat must carry its sequence number.",
    "missing_progress_key": "A heartbeat must name the work it is a beat for.",
}
# Rejections that mean the CLIENT sent something malformed (422) rather than
# losing a race or replaying (409). The distinction matters to the client: a 409
# means re-open, a 422 means fix your request.
_BEAT_MALFORMED = frozenset({"missing_progress_key", "missing_seq"})


def _beat_error(code: str, message: str, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    session = session or {}
    return {
        "credited_seconds": int(session.get("credited_seconds") or 0),
        "continuous_seconds": int(session.get("continuous_seconds") or 0),
        "qualified": bool(session.get("qualified")),
        "next_nonce": None,
        "ended": bool(session.get("ended_at")),
        "ok": False, "error": code, "message": message,
    }


def _stale_reason(session: Dict[str, Any], now: datetime) -> Optional[str]:
    """Why this open session should be finalised before a new one opens, if at all."""
    started = _parse(session.get("started_at"))
    if started is not None and (now - started).total_seconds() > session_max_seconds():
        return END_EXPIRED
    last = _parse(session.get("last_beat_at")) or started
    if last is None:
        return None
    if (now - last).total_seconds() > session_abandon_seconds():
        return END_ABANDONED
    return None


def _session_view(store, row: Dict[str, Any], *, existing_session: bool) -> Dict[str, Any]:
    min_seconds = int(row.get("min_seconds") or tr_min_seconds())
    continuous = int(row.get("continuous_seconds") or 0)
    view = {
        "session_id": row["session_id"],
        "started_at": row.get("started_at"),
        "min_seconds": min_seconds,
        "credited_seconds": int(row.get("credited_seconds") or 0),
        "continuous_seconds": continuous,
        # A nonce is a BEATING CREDENTIAL, and handing one out has to cost
        # something (audit H1). A brand-new session is the one free issue: the
        # caller demonstrably just created it. A RESUMED open returns state and
        # no credential — otherwise ``open_session``'s idempotence, which exists
        # so a reviewer never accidentally opens two billable sessions, doubles
        # as an unlimited nonce dispenser for a client that never reads a
        # heartbeat response. Resuming has its own endpoint for exactly that
        # reason: rate-limited an order of magnitude harder, and counted.
        "nonce": None if existing_session else row.get("nonce"),
        "qualified": bool(row.get("qualified")) or continuous >= min_seconds,
        "rate_cents": int(row.get("rate_cents") or tr_session_cents()),
        "remaining_seconds": max(0, min_seconds - continuous),
        "resumed": existing_session,
        "params": client_params(),
    }
    if existing_session:
        # Where the sequence got to, so a resuming client knows what to ask for
        # next without being able to beat on the strength of knowing it.
        view["seq"] = _last_seq(store, row["session_id"])
    return view


def _last_seq(store, session_id: str) -> int:
    return max((int(b["seq"]) for b in store.session_beats(session_id)), default=0)


def resume_session(store, *, session_id: str, user_id: str) -> Dict[str, Any]:
    """Hand a fresh beating credential to a client that legitimately lost one.

    A physician who reloads the page mid-session has lost their nonce and their
    sequence and must be able to carry on without losing the time they earned.
    That is the only case this exists for, which is why it is rate-limited far
    below the beat rate and why every call is counted on the session row: one or
    two resumes is a reload, thirty is a script.

    Rotating on resume also means two tabs can never both hold a live credential —
    the same property the per-beat rotation provides, extended to the one other
    place a nonce can come from.
    """
    row = store.get_work_session(session_id)
    if row is None or row.get("user_id") != user_id:
        raise PaymentsDenied("not_found", "Session not found.")
    if row.get("ended_at"):
        # Never hand a live credential to a settled session: that would reopen
        # for beating something that has already been paid or refused.
        raise PaymentsDenied("ended", "This session has already ended.")

    nonce = _mint_nonce()
    if not store.rotate_session_nonce(session_id=session_id, nonce=nonce):
        raise PaymentsDenied("ended", "This session has already ended.")

    fresh = store.get_work_session(session_id) or row
    store.log_event(
        entity_type="work_session", entity_id=session_id, event_type="session_resumed",
        actor=user_id, payload={"resume_count": int(fresh.get("resume_count") or 0)},
    )
    view = _session_view(store, fresh, existing_session=True)
    view["nonce"] = nonce
    return view


def session_state(store, *, session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """The current server-authoritative state of one session, scoped to its owner.
    Returns None when the session does not exist OR belongs to someone else —
    deliberately the same answer, so the endpoint cannot be used to probe for the
    existence of another physician's session."""
    row = store.get_work_session(session_id)
    if row is None or row.get("user_id") != user_id:
        return None
    view = _session_view(store, row, existing_session=True)
    view["ended"] = bool(row.get("ended_at"))
    view["end_reason"] = row.get("end_reason")
    # The nonce is minted in exactly one place — ``open_session`` — and rotated in
    # exactly one place: the heartbeat response. Handing it out on a plain GET
    # would give a second tab (the Earnings page polls this route) everything it
    # needs to start beating, and two clients racing for one session's next beat
    # is the failure the rotating nonce exists to prevent. A client that lost its
    # nonce re-opens; open_session is idempotent precisely so that is cheap.
    view.pop("nonce", None)
    return view


# ═══ TL accrual — derived from submissions, reconciled on read ════════════════
# The verdicts under which a labeler's work is PAID FOR. Stated once, here, in
# the module that owns money.
#
# This is emphatically NOT the expert-acceptance statistic. ``agreement.py`` owns
# that, it is a different number, and it must never be reported as if it were
# this one (context pack §3.2, Seam 3). The set happens to coincide today because
# "a reviewer signed off on this work" and "we owe the labeler $75 for it" are the
# same event — if they ever diverge, this constant changes and the statistic does
# not. That is exactly why the two are named separately rather than shared.
PAYABLE_VERDICTS = frozenset({"accept", "accept_with_edits"})
REJECTING_VERDICTS = frozenset({"reject"})


def _verdict_status(verdicts: Optional[str]) -> Optional[str]:
    """The ledger state a submission's review verdicts imply, or None for "no
    verdict has landed yet". ``verdicts`` is the raw comma-joined list.

    A submission may carry several reviews. Any payable verdict approves it; a
    reject voids it only when NO payable verdict exists.

    PRODUCT RULE, stated here because it is a decision and not a mechanism: a
    paired adjudication's verdict applies to BOTH labels, not just the accepted
    one. "Accept A" says A's answer is right as submitted; it does not say B did
    not do the work. B labelled the same case independently and blind, and that
    second label is the thing that makes an agreement statistic possible at all —
    it is the product, not a runner-up. Paying only the accepted side would pay
    half the labelers on a case, and the second-label queue is the throughput
    rule the whole release depends on. Only "reject both" voids, and it voids
    both, because that is the verdict that says the work is unusable. That asymmetry is
    deliberate and follows §1.2's rule that a doctor must never see a number go
    down without an explanation: a later accept may restore money, a later reject
    never takes back money already approved."""
    seen = {v.strip() for v in (verdicts or "").split(",") if v.strip()}
    if seen & PAYABLE_VERDICTS:
        return APPROVED
    if seen & REJECTING_VERDICTS:
        return VOID
    return None


def _payout_amount(rate_cents: int, multiplier: float) -> int:
    """The payable amount for a case. One definition, in payout.py, so a
    fractional cent resolves the same way everywhere and resolves in the
    physician's favour."""
    from asclepius import payout as _payout  # noqa: PLC0415

    return _payout.amount_for(rate_cents, multiplier)


def _quality_terms(store, submission_id: str, verdicts: Optional[str]) -> Dict[str, Any]:
    """The payout multiplier for one case, from what has already been stamped.

    Reads the case-quality stamp written at grade time rather than recomputing
    it. Two reasons. The stamp carries the coefficient version that produced it,
    so a case graded under older rules keeps its number. And this sweep runs on
    every Earnings page load; recomputing a score here would put a per-row walk
    of a physician's submissions inside their page render.

    Never raises. A quality-lookup problem must not stop a physician accruing:
    the fallback is a multiplier of 1.0, which pays the full posted rate.
    """
    from asclepius import payout as _payout  # noqa: PLC0415 — pure, import-light

    quality = None
    try:
        stamped = store.submission_quality(submission_id)
        if stamped:
            quality = stamped.get("score")
    except Exception:  # noqa: BLE001
        log.warning("payments: could not read case quality for %s; paying full rate",
                    submission_id, exc_info=True)

    seen = {v.strip() for v in (verdicts or "").split(",") if v.strip()}
    # The WORST verdict decides, matching _verdict_status and
    # contributor_score._outcome_for: a case one reviewer accepted and another
    # corrected is a disagreement, not a clean accept.
    if "reject" in seen:
        worst = "reject"
    elif "accept_with_edits" in seen:
        worst = "accept_with_edits"
    elif "accept" in seen:
        worst = "accept"
    else:
        worst = None
    return _payout.quality_multiplier(quality_score=quality, review_verdict=worst)


# ═══ The one rule: approved money ⇔ exportable record ════════════════════════
#
# A physician's submission used to carry THREE statuses that never spoke to each
# other: the ledger (`earnings.status`), the QA pipeline (`submissions.status`)
# and the export gate (`records.status`). Export reads only the third. Payment
# approval only ever wrote the first. So a case could be approved, paid, and
# permanently unshippable — and nothing anywhere said so.
#
# After PRD §3 a record ships iff `records.status ∈ {export_ready, exported}`,
# and the invariant this adds is ONE-DIRECTIONAL:
#
#     approved money  ⟹  exportable record        (what was broken; fixed here)
#     exportable record  ⇏  approved money        (by design — see below)
#
# Four APPROVAL events reach the records table, and the first three come through
# THIS function: admin Approve, reviewer accept, the 14-day auto-approve, and the
# QA tab (which writes both in ``pipeline.apply_qa_decision``, the shape this
# function copies). All four resolve the ledger.
#
# They are not the only way a record becomes exportable, and claiming otherwise
# would be wrong. ``pipeline.process_submission`` puts a clean, unsampled
# submission straight to ``export_ready`` at capture, with no ledger row in
# existence — that predates all of this and PRD §7 protects it explicitly. So
# three code sites in total can write ``export_ready``; a FOURTH is a bug.
# ``test_exactly_three_code_paths_can_make_a_record_exportable`` pins the set,
# and ``test_the_four_paths_all_write_both_tables`` asserts each APPROVAL path
# moves both tables.

#: Where an approval may move a submission FROM. A whitelist, not a blacklist:
#: ``exported`` must never be downgraded (it has already shipped), ``rejected``
#: is Void's business, and the stage-1 side branches (``prompt_flagged``,
#: ``not_hard``, ``case_inconsistent``) are deliberate refusals to package that
#: an approval on the MONEY has no business overturning.
APPROVABLE_SUBMISSION_STATES = ("submitted", "auto_validated", "qa_checked", "needs_qa")

#: Where a void may move a submission FROM — everything the approve path covers,
#: plus ``export_ready``: a case that is queued to ship but has not shipped can
#: still be pulled. ``exported`` cannot (the bytes are with a buyer) and
#: ``rejected`` is already there.
REJECTABLE_SUBMISSION_STATES = APPROVABLE_SUBMISSION_STATES + ("export_ready",)


def apply_ledger_decision_to_records(
    store, *, submission_id: Optional[str], decision: str, reason: str,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Make the export gate agree with the ledger for ONE submission.

    ``decision`` is ``"approve"`` (→ ``export_ready``) or ``"reject"``
    (→ ``rejected``). Returns a small dict describing what happened:
    ``{"moved": bool, "submission_id", "prior_status", "status", "outcome"}``.

    **Never raises, and never returns an error to a caller who is settling
    money.** Three of the four approval paths run inside a payment write, and a
    records table that would not move must not roll back a physician's pay — the
    ledger decision stands either way and the mismatch is visible in the export
    preview's excluded list. Failures are logged loudly instead.

    ``outcome`` distinguishes the cases the caller may want to report:
      * ``moved``        — the submission and its records changed
      * ``not_a_case``   — no submission id (a review session or a bounty)
      * ``missing``      — the submission id does not resolve
      * ``already``      — already in the target state; nothing to do
      * ``terminal``     — a state this decision may not leave (exported/rejected/
                           a stage-1 flag)
      * ``error``        — the write failed; see the log
    """
    target = "export_ready" if decision == "approve" else "rejected"
    allowed = (APPROVABLE_SUBMISSION_STATES if decision == "approve"
               else REJECTABLE_SUBMISSION_STATES)
    result = {"moved": False, "submission_id": submission_id, "prior_status": None,
              "status": None, "outcome": "not_a_case"}
    if not submission_id:
        return result
    try:
        sub = store.get_submission(submission_id)
    except Exception:  # noqa: BLE001 — a read failure must not fail a payment
        log.warning("payments: could not read submission %s while applying a "
                    "ledger decision", submission_id, exc_info=True)
        result["outcome"] = "error"
        return result
    if not sub:
        result["outcome"] = "missing"
        return result
    prior = sub.get("status")
    result["prior_status"] = prior
    result["status"] = prior
    if prior == target:
        result["outcome"] = "already"
        return result
    if prior not in allowed:
        result["outcome"] = "terminal"
        return result
    try:
        store.update_submission(submission_id, status=target, qa_reason=reason)
        store.update_records_status_for_submission(submission_id, target)
        store.log_event(
            entity_type="submission", entity_id=submission_id,
            event_type=("export_ready" if decision == "approve" else "records_rejected"),
            actor=actor,
            payload={"via": reason, "prior_status": prior, "status": target},
        )
    except Exception:  # noqa: BLE001
        log.exception("payments: could not move submission %s to %s (%s)",
                      submission_id, target, reason)
        result["outcome"] = "error"
        return result
    result["moved"] = True
    result["status"] = target
    result["outcome"] = "moved"
    return result


def submission_ref(kind: Optional[str], ref_id: Optional[str]) -> Optional[str]:
    """The submission a ledger row is about, or None.

    Only a TASK earning is one case: ``ref_id`` is the submission id (see
    ``reconcile_task_accruals`` pass 1). A review session spans several cases and
    a referral bounty is not casework at all — neither has a record to ship.
    """
    return ref_id if kind == KIND_TASK else None


#: Why an approval was refused. Machine tokens for the router to map to HTTP and
#: for tests to assert on; the sentences a human reads live in the router.
APPROVE_REFUSALS = ("not_found", "already_paid", "already_approved", "voided",
                    "quality_held", "raced")


def approve_earning(
    store, *, earning_id: str, actor: Optional[str], note: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Admin approval of one case: the ledger AND the export gate, one action.

    This is the policy half of ``POST /admin/earnings/{id}/approve``. It lives
    here, beside ``_verdict_status`` and ``_auto_approve``, because it is the
    THIRD path to the same decision and a second implementation of "what
    approving a case means" is how the three of them drift apart again.

    Returns ``{"ok": True, …}`` or ``{"ok": False, "refusal": <token>}`` — never
    raises for a refusal, because every refusal here is a legitimate state the
    console has to explain rather than an exception.

    ``only_from=[ACCRUED]`` is a compare-and-set, so a double-click cannot
    double-approve and a race with the auto-approve sweep resolves as ``raced``
    rather than as two writes.
    """
    now = now or _now()
    earning = store.get_earning_by_id(earning_id)
    if earning is None:
        return {"ok": False, "refusal": "not_found"}
    status = earning.get("status")
    if status == PAID:
        return {"ok": False, "refusal": "already_paid", "earning": earning}
    if status == APPROVED:
        return {"ok": False, "refusal": "already_approved", "earning": earning}
    if status == VOID:
        return {"ok": False, "refusal": "voided", "earning": earning}
    # A row the payout algorithm HELD is refused, on purpose. The hold is the
    # promise that an automated pay cut never applies without a person deciding
    # it; approving through here would apply the reduced amount while looking
    # like a plain approval. `/release` is where that decision belongs.
    if earning.get("quality_hold"):
        return {"ok": False, "refusal": "quality_held", "earning": earning}

    kind = earning.get("kind")
    ref_id = earning.get("ref_id")
    sub_id = submission_ref(kind, ref_id)
    prior_qa = None
    if sub_id:
        try:
            prior_qa = (store.get_submission(sub_id) or {}).get("status")
        except Exception:  # noqa: BLE001 — a read must not block an approval
            log.warning("payments: could not read submission %s before approval",
                        sub_id, exc_info=True)

    if not store.resolve_earning(
            kind=kind, ref_id=ref_id, status=APPROVED, resolved_at=_ledger_ts(now),
            note=(note or "").strip() or "Admin approved", only_from=[ACCRUED]):
        return {"ok": False, "refusal": "raced", "earning": earning}

    gate = apply_ledger_decision_to_records(
        store, submission_id=sub_id, decision="approve", reason="admin_approved",
        actor=actor)

    store.log_event(
        entity_type="earning", entity_id=earning_id,
        event_type="earning_admin_approved", actor=actor,
        payload={"kind": kind, "ref_id": ref_id, "submission_id": sub_id,
                 "user_id": earning.get("user_id"),
                 "amount_cents": int(earning.get("amount_cents") or 0),
                 "prior_ledger": status, "prior_qa": prior_qa,
                 "records_outcome": gate["outcome"],
                 # Stated in the audit row because it is TRUE: this approval did
                 # not go through QA sampling, and an audit that hides that is
                 # not an audit.
                 "bypassed_qa_sampling": True,
                 "note": (note or "").strip() or None},
    )
    log.warning("asclepius.payments: earning %s approved by %s (records: %s)",
                earning_id, actor or "unknown", gate["outcome"])
    return {
        "ok": True,
        "earning_id": earning_id,
        "user_id": earning.get("user_id"),
        "prior_ledger": status,
        "prior_qa": prior_qa,
        "row": store.get_earning_by_id(earning_id),
        "gate": gate,
    }


def reconcile_task_accruals(
    store, *, user_id: Optional[str] = None, limit: int = 2000,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Materialise and resolve TL task earnings. Idempotent; safe to call on every
    read. Returns a small counter dict for logging and tests.

    Three passes over the same data:
      1. every terminal-submitted, payable submission with no ledger row gets one
      2. review verdicts move ``accrued`` (or ``void``) rows to their decided state
      3. ``accrued`` rows older than ASCLEPIUS_TL_AUTO_APPROVE_DAYS auto-approve

    Reads ``case_reviews`` and never calls into the review module: a read is a
    contract-free dependency, a callback is not.

    ``user_id`` scopes every pass to one physician (audit M1). A doctor opening
    their Earnings page used to run this unfiltered, which meant one user's READ
    wrote ledger rows for the whole company and ran everyone's auto-approve sweep,
    inside their request. Scoped, a page load costs what that physician's own
    backlog costs and nothing more. The unscoped form is still what the admin
    ledger runs, because the fourteen-day promise must not depend on a physician
    remembering to look.
    """
    now = now or _now()
    rate = tl_rate_cents()
    counts = {"accrued": 0, "approved": 0, "voided": 0, "auto_approved": 0}
    # Everyone whose task ledger this pass wrote a row for. Collected as the pass
    # runs rather than re-queried afterwards, so the referral settlement in pass 4
    # costs nothing on the overwhelmingly common case where nothing moved.
    touched: set = set()

    # 1. Work with no ledger row yet. Unpayable authors are filtered in SQL, so
    #    an advisor's submissions never enter this set at all.
    for row in store.unaccrued_submissions(user_id=user_id, limit=limit):
        ref = row["submission_id"]
        implied = _verdict_status(row.get("review_verdicts"))
        # Gap U2. A case whose clinical-validity attestation was found false is
        # not payable, whatever the review verdict says. Folded into `implied`
        # rather than added as a fourth ledger state, so it travels through the
        # SAME void path a rejected case takes: one zero-value row carrying its
        # own explanation, never a silent skip. A skipped submission would be
        # re-examined by every future sweep and would appear on no ledger, which
        # is the one way a physician could lose money without being told.
        if attestation_found_false(row):
            implied = VOID
        # ``accrued_at`` is the moment the WORK happened, not the moment this
        # sweep noticed it — otherwise a backfill would restart every auto-approve
        # clock and a doctor's ledger would be dated by our deploy schedule.
        accrued_at = _ledger_ts(_parse(row.get("created_at")) or now)
        # A rejected case is VOIDED, not reduced: it pays nothing, and running a
        # multiplier over it would both mean nothing and misreport the voided
        # amount. The quality adjustment only ever applies to work we are paying
        # for.
        terms = (None if implied == VOID
                 else _quality_terms(store, ref, row.get("review_verdicts")))
        amount = rate if terms is None else _payout_amount(rate, terms["multiplier"])
        # A row the algorithm wants to pay LESS than the posted rate is a
        # proposal, not a decision. It stays accrued and held until an admin
        # acts, even when the verdict would otherwise approve it: an automated
        # pay cut and a proposed cut a person approves are different objects.
        held = bool(terms and terms["proposed"])
        written = store.insert_earning(
            earning_id=_new_id("earn"), user_id=row["evaluator_id"], kind=KIND_TASK,
            ref_id=ref, amount_cents=amount, rate_cents=rate,
            status=(implied if (implied and not held) else (VOID if implied == VOID else ACCRUED)),
            accrued_at=accrued_at,
            resolved_at=_ledger_ts(now) if (implied and not held) else None,
            note=(VALIDITY_VOID_NOTE if attestation_found_false(row)
                  else (_reject_note(row) if implied == VOID else None)),
        )
        if written is not None and terms is not None:
            store.set_earning_quality(
                written["earning_id"], multiplier=terms["multiplier"],
                reasons=terms["reasons"], version=terms["version"],
                hold=held and written["status"] != VOID,
            )
        if written is not None:
            counts["accrued"] += 1
            if written["status"] == APPROVED:
                touched.add(row["evaluator_id"])
                # The verdict landed before this sweep noticed the submission, so
                # the row is born APPROVED and pass 2 will never look at it again.
                # Same rule as pass 2: approved money, exportable record.
                if apply_ledger_decision_to_records(
                        store, submission_id=ref, decision="approve",
                        reason="reviewer_accepted")["moved"]:
                    counts["records_exportable"] = counts.get("records_exportable", 0) + 1
            store.log_event(
                entity_type="earning", entity_id=written["earning_id"],
                event_type="earning_accrued", actor=row["evaluator_id"],
                payload={"kind": KIND_TASK, "ref_id": ref, "amount_cents": rate,
                         "status": written["status"], "task_id": row.get("task_id")},
            )

    # 2. Rows awaiting a verdict. Terminal states are never re-examined.
    for row in store.unresolved_task_earnings(user_id=user_id, limit=limit):
        ref = row["submission_id"]
        status = row["status"]
        implied = _verdict_status(row.get("review_verdicts"))
        # Gap U2, and this branch is the load-bearing one. A finding usually
        # lands AFTER the row accrued, so voiding here is what actually makes
        # the consequence real.
        #
        # `only_from=[ACCRUED]` is the whole guarantee against restating a
        # settled payment, and it is why this is a `continue` rather than a
        # reassignment of `implied`: falling through would reach the branch that
        # moves VOID back to APPROVED on a later accepting verdict, which would
        # let a reviewer's accept overrule a finding that the case should never
        # have been labelled at all. APPROVED and PAID rows are not in this
        # query's result set at all, so a physician who has already been paid
        # for the case keeps that money -- section 3.5 of the agreement says
        # exactly that, and it says it because clawing back settled pay from a
        # doctor is a thing we are choosing not to do.
        if attestation_found_false(row):
            if status == ACCRUED and store.resolve_earning(
                kind=KIND_TASK, ref_id=ref, status=VOID,
                resolved_at=_ledger_ts(now), note=VALIDITY_VOID_NOTE,
                only_from=[ACCRUED],
            ):
                counts["voided"] += 1
                counts["validity_voided"] = counts.get("validity_voided", 0) + 1
            continue
        # The verdict has landed, so the case now has a graded quality number
        # that it may not have had when the row was written. Recompute the terms
        # and restamp the amount while the row is still ACCRUED (never once it
        # is approved or paid: that is the restatement this design refuses).
        if status == ACCRUED and implied != VOID:
            terms = _quality_terms(store, ref, row.get("review_verdicts"))
            # The row's OWN stamped rate, never the current env rate. The rate
            # in force at accrual is what this row is worth, and recomputing
            # from today's env var would restate an accrual every time the
            # posted rate changed. That guarantee predates this feature and is
            # pinned by test_the_rate_is_read_from_env_and_stamped_on_the_row.
            row_rate = int(row.get("rate_cents") or rate)
            store.set_earning_quality(
                row["earning_id"], multiplier=terms["multiplier"],
                reasons=terms["reasons"], version=terms["version"],
                hold=bool(terms["proposed"]),
                amount_cents=_payout_amount(row_rate, terms["multiplier"]),
            )
            if terms["proposed"]:
                # Held for a human. The verdict does not approve it.
                counts["quality_held"] = counts.get("quality_held", 0) + 1
                continue
        if implied == APPROVED and status in (ACCRUED, VOID):
            if store.resolve_earning(kind=KIND_TASK, ref_id=ref, status=APPROVED,
                                     resolved_at=_ledger_ts(now),
                                     only_from=[ACCRUED, VOID]):
                counts["approved"] += 1
                touched.add(row.get("user_id") or user_id)
                # A reviewer's accept approves the MONEY and, from here on, the
                # EXPORT too. Before this line a reviewer-accepted case was paid
                # and still unshippable, because nothing between the verdict and
                # `records.status` existed (PRD §1.1, §3).
                if apply_ledger_decision_to_records(
                        store, submission_id=ref, decision="approve",
                        reason="reviewer_accepted")["moved"]:
                    counts["records_exportable"] = counts.get("records_exportable", 0) + 1
        elif implied == VOID and status == ACCRUED:
            if store.resolve_earning(kind=KIND_TASK, ref_id=ref, status=VOID,
                                     resolved_at=_ledger_ts(now),
                                     note=_reject_note(row), only_from=[ACCRUED]):
                counts["voided"] += 1

    # 3. The backlog escape hatch.
    counts["auto_approved"] = _auto_approve(store, now=now, user_id=user_id,
                                            touched=touched)

    # 4. The referral bounty. Every physician whose ledger this pass touched may
    #    have been referred by somebody, and their FIRST approved task is what
    #    settles that bet. Derived here rather than hooked into a submit path for
    #    exactly the reason stated at the top of this module: a read is a
    #    contract-free dependency and this pass already has the set of people
    #    whose money just moved.
    #
    #    ``touched`` is the union of everyone this pass wrote for, not only the
    #    ones who moved to APPROVED, because pass 1 can insert a row that is
    #    ALREADY approved (the verdict landed before the sweep noticed the
    #    submission) and that is the common case for a fast reviewer.
    counts["referral_bounties"] = 0
    for referred_id in sorted(touched):
        if accrue_referral_bounty(store, referred_user_id=referred_id, now=now):
            counts["referral_bounties"] += 1
    return counts


# ═══ The referral bounty ══════════════════════════════════════════════════════
def accrue_referral_bounty(
    store, *, referred_user_id: str, now: Optional[datetime] = None,
) -> Optional[str]:
    """Settle the bet, if this physician was referred and has now delivered.

    Called when a task earning for ``referred_user_id`` reaches APPROVED.

    **First APPROVED task, not first submission.** A submission a reviewer later
    rejects must not pay a bounty — otherwise the cheapest way to earn $150 is to
    refer someone who submits one thing and leaves. Approval is the first moment
    the referral has produced anything a buyer would pay for.

    Idempotent by construction rather than by checking first: ``earnings`` carries
    ``UNIQUE(kind, ref_id)`` and ``ref_id`` is the referral_id, so a second call
    is a no-op at the DATABASE level. The one thing that guard does NOT cover is
    two different referral rows for the same invitee, so the winner-picking and
    the duplicate marking happen inside one ``BEGIN IMMEDIATE`` in
    ``store.settle_referral_bounty``.

    Returns the earning id when this call is the one that paid, else None.

    Three guards, all of them necessary and all of them checked HERE rather than
    at invite time, because the world changes between the invitation and the
    money:

      * **The referrer must still be payable.** An advisor holds equity and does
        not accrue cash — including on referrals. ``compensation.accrues_payment``
        is the predicate; never write a second one.
      * **No self-referral.** Checked at invite AND here, because email addresses
        change: a physician who adds a second address to their own account after
        being "referred" by themselves must not be able to collect.
      * **An expired invitation cannot come back to life.** ``bounty_state`` is
        already terminal on those rows, so they are never candidates.
    """
    if not referred_user_id:
        return None
    now = now or _now()
    if not store.has_approved_task_earning(referred_user_id):
        return None

    rows = store.referrals_for_invitee(referred_user_id)
    if not rows:
        return None

    invitee = store.get_user_by_id(referred_user_id)
    invitee_label = _invitee_label(invitee, rows[0])

    # Eligibility is resolved HERE, ahead of the settling transaction, and the
    # answer is passed in as a list of referral ids. The store must not run a
    # second connection's read while it holds the write lock, and deciding this
    # needs the referrers' compensation models — so the policy stays in the
    # module that owns money and the transaction stays a pure write.
    #
    # Cached per referrer: a popular invitee with four referrals costs four rows
    # and not four lookups.
    referrers: Dict[str, Optional[Dict[str, Any]]] = {}
    capped: Dict[str, bool] = {}

    def _eligible(referral: Dict[str, Any]) -> bool:
        rid = referral.get("referrer_id") or ""
        if rid == referred_user_id:
            return False                      # self-referral, the patient version
        if rid not in referrers:
            referrers[rid] = store.get_user_by_id(rid or "")
        if not compensation.accrues_payment(referrers[rid]):
            return False
        # An optional ceiling. There is NO cap by default any more (see
        # referral_cap_cents), so this whole branch is inert unless somebody
        # sets the env var — but it stays written and tested, because "we can
        # cap this without a deploy" is worth more than the four lines it costs.
        #
        # When set: a referrer at the cap stops accruing and the row settles as
        # ineligible rather than pending forever. Read from the ledger so a
        # historical rate change cannot bend it.
        cap = referral_cap_cents()
        if cap <= 0:
            return True
        if rid not in capped:
            capped[rid] = (store.referral_earned_cents(rid) + referral_bounty_cents()
                           > cap)
        return not capped[rid]

    stamp = _ledger_ts(now)
    eligible_ids = []
    for r in rows:
        if r.get("bounty_state") is not None:
            continue
        if _eligible(r):
            eligible_ids.append(r["referral_id"])
        else:
            # Settled explicitly rather than left pending forever. A funnel row
            # that will never resolve is the same failure as an empty page: the
            # referrer keeps waiting for something that is not coming.
            store.set_referral_bounty_state(
                r["referral_id"], store.BOUNTY_INELIGIBLE, resolved_at=stamp)

    minted = _new_id("earn")
    settled = store.settle_referral_bounty(
        invitee_user_id=referred_user_id,
        earning_id=minted,
        amount_cents=referral_bounty_cents(),
        accrued_at=stamp,
        eligible_ids=eligible_ids,
        note=f"Referral · {invitee_label} completed their first case",
    )
    if settled is None:
        return None

    # The settlement is the moment the referral produced value, whichever call
    # got here first: stamp the fact, and pay the INVITEE's side of the same
    # bet. Both are idempotent (first-writer stamp; UNIQUE(kind, ref_id) on the
    # ledger) and both are best-effort — the referrer's bounty must never be
    # unwound by a bookkeeping failure on the invitee's bonus.
    try:
        store.stamp_referral_first_case(settled["referral_id"], at=stamp)
        if compensation.accrues_payment(invitee):
            bonus_minted = _new_id("earn")
            written = store.insert_referee_bonus(
                earning_id=bonus_minted,
                user_id=referred_user_id,
                referral_id=settled["referral_id"],
                amount_cents=referee_bonus_cents(),
                accrued_at=stamp,
                note="First case bonus · you were referred by a colleague",
            )
            if written == bonus_minted:
                store.log_event(
                    entity_type="earning", entity_id=bonus_minted,
                    event_type="referee_bonus_accrued", actor=None,
                    payload={"kind": KIND_REFEREE_BONUS,
                             "referral_id": settled["referral_id"],
                             "user_id": referred_user_id,
                             "amount_cents": referee_bonus_cents()},
                )
    except Exception:
        log.exception("asclepius.payments: referee bonus bookkeeping failed "
                      "(the referrer's bounty stands)")

    earning = store.get_earning(kind=KIND_REFERRAL, ref_id=settled["referral_id"])
    if earning is None:
        return None
    # The ledger row carries the id THIS call minted only when this call's INSERT
    # is the one that won; every later pass reads back somebody else's id and
    # says nothing. Comparing timestamps instead would double-log two calls
    # inside the same second — which is exactly what a retry looks like.
    if earning["earning_id"] != minted:
        return None
    store.log_event(
        entity_type="earning", entity_id=earning["earning_id"],
        event_type="referral_bounty_accrued", actor=None,
        payload={"kind": KIND_REFERRAL, "referral_id": settled["referral_id"],
                 "referrer_id": settled.get("referrer_id"),
                 "referred_user_id": referred_user_id,
                 "amount_cents": int(earning["amount_cents"])},
    )
    return earning["earning_id"]


def _invitee_label(invitee: Optional[Dict[str, Any]], referral: Dict[str, Any]) -> str:
    """How the referred physician is named on the referrer's ledger row.

    Their real name if we have one, else the name the referrer typed, else the
    MASKED address — never the raw one. The ledger note is read by the referrer,
    and a colleague's address is not something they gain a right to because the
    system now knows it.
    """
    name = ((invitee or {}).get("full_name") or "").strip()
    if name:
        return name
    return _referrals.display_name(referral)


def reconcile_referral_bounties(
    store, *, referrer_id: str, now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Bring ONE physician's referral funnel up to date, from their side.

    The accrual above fires when the INVITEE's ledger moves, which is the prompt
    path — but it depends on somebody having loaded the invitee's earnings (or an
    admin having loaded the ledger) since their work was approved. That is a
    scheduling assumption, and the one thing this feature must not do is make the
    referrer wait on it: the whole premise is that a referrer never has to wonder
    whether it worked.

    So the referrer's own page load reconciles their own funnel. Bounded by their
    referral count and capped, so a page load costs what that physician's funnel
    costs and nothing more — the same scoping rule ``reconcile_task_accruals``
    follows (audit M1).
    """
    now = now or _now()
    counts = {"expired": 0, "accrued": 0}
    rows = store.list_referrals_by_referrer(referrer_id)

    # The sweep is a WRITE, and this function runs on every Earnings page load.
    # Reading first means the overwhelmingly common case — a physician with no
    # unclaimed invitations outstanding — costs one indexed SELECT and takes no
    # write lock at all.
    if any(r.get("bounty_state") is None and not r.get("user_id") for r in rows):
        counts["expired"] = _referrals.sweep_expiries(
            store, referrer_id=referrer_id, now=now)

    pending = [r for r in rows if r.get("bounty_state") is None and r.get("user_id")]
    for referral in pending[:_REFERRAL_RECONCILE_CAP]:
        invitee_id = referral["user_id"]
        try:
            # The invitee's own accruals may not be materialised yet — their work
            # is approved on review, but the ledger row that says so is written by
            # whoever next reads their earnings. Scoped to that one physician, so
            # this is their backlog and nobody else's.
            reconcile_task_accruals(store, user_id=invitee_id, now=now)
            if accrue_referral_bounty(store, referred_user_id=invitee_id, now=now):
                counts["accrued"] += 1
        except Exception:
            # One unresolvable referral must never take the Earnings page down.
            log.exception("asclepius.payments: referral bounty reconcile failed for %s",
                          referral.get("referral_id"))
    return counts


#: How many pending referrals one page load will chase. Twenty is already an
#: unusual funnel for a real physician; past that the tail resolves on the next
#: load rather than making one request pay for all of it.
_REFERRAL_RECONCILE_CAP = 20


#: An admin's determination that a physician's clinical-validity attestation was
#: not true (Gap U2). The physician agreement, section 3.5, is the promise this
#: token enforces: the case that attestation covered is not paid.
VALIDITY_FINDING_FALSE = "false"

#: Said to the physician, next to the zero, on the same §1.2 rule the reject
#: note follows: a number that moved is never shown without its explanation.
VALIDITY_VOID_NOTE = (
    "Not paid: on review, the clinical-validity attestation for this case was "
    "found not to hold. Section 3.5 of your contributor agreement covers this, "
    "and you can ask for it to be looked at again.")


def attestation_found_false(row: Dict[str, Any]) -> bool:
    """Whether this submission's validity attestation was found false.

    A PREDICATE RATHER THAN AN INLINE COMPARISON, in the module that owns money,
    because it is read from three places in the sweep and all three have to
    agree. The finding is the ONLY thing consulted: a case nobody has reviewed
    reads NULL and is paid normally, and a case reviewed and upheld reads
    'upheld' and is paid normally. Silence is not an accusation.
    """
    return (row.get("validity_finding") or "") == VALIDITY_FINDING_FALSE


def _reject_note(row: Dict[str, Any]) -> str:
    """The reason a task was not approved, in the doctor's own reviewer's words
    where there are any. §1.2: never show a number that might go down without an
    explanation next to it."""
    note = (row.get("reject_note") or "").strip()
    base = "Not approved on review"
    return f"{base}: {note}" if note else base


def _auto_approve(store, *, now: datetime, user_id: Optional[str] = None,
                  touched: Optional[set] = None) -> int:
    """A labeler is never held hostage by a review backlog: if nobody reviews
    their work within the window, it approves.

    Runs on read rather than on a nightly cron. This deployment has no scheduler,
    and a sweep that only runs when someone is looking at the number is a sweep
    that has always run by the time the number is shown."""
    days = tl_auto_approve_days()
    cutoff = _ledger_ts(now - timedelta(days=days))
    moved = 0
    for row in store.accrued_earnings_before(cutoff, user_id=user_id):
        if store.resolve_earning(
            kind=row["kind"], ref_id=row["ref_id"], status=APPROVED,
            resolved_at=_ledger_ts(now), only_from=[ACCRUED],
            note=f"Auto-approved after {days} days without a review",
        ):
            moved += 1
            # An auto-approval is an approval: a physician whose referrer has been
            # waiting fourteen days for a review that never came still delivered,
            # and the bounty settles on the same event the labeler's money does.
            if touched is not None and row["kind"] == KIND_TASK and row.get("user_id"):
                touched.add(row["user_id"])
            # …and it is an approval for the EXPORT too. Before this, a
            # timer-approved case paid the physician and then silently never
            # shipped, because the 14-day sweep wrote the ledger and nothing else
            # (PRD §1.1).
            apply_ledger_decision_to_records(
                store, submission_id=submission_ref(row["kind"], row.get("ref_id")),
                decision="approve", reason="auto_approved")
            store.log_event(
                entity_type="earning", entity_id=row["earning_id"],
                event_type="earning_auto_approved", actor=None,
                payload={"kind": row["kind"], "ref_id": row["ref_id"], "days": days},
            )
    return moved


# ═══ Disbursement — where money actually leaves ═══════════════════════════════
def mark_paid(
    store, *, payout_batch_id: str, actor_id: Optional[str] = None,
    earning_ids: Optional[List[str]] = None, user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record that a batch of approved earnings has actually been disbursed.

    Before this existed, ``paid`` was a state nothing could write: the ledger's
    double-payment guard was airtight inside the system and absent at the only
    boundary where a double payment costs real money. Paying a physician out of
    band left the ledger unable to record it, the doctor still seeing the money as
    owed, and the next export re-including every row.

    This does NOT move money. It is the ledger's record that money moved, which is
    the half that belongs here — the transfer itself is a treasury operation and
    the batch id is how the two are reconciled afterwards.

    ``payout_batch_id`` is the idempotency key, not a label. Replaying a batch is
    a no-op, so a disbursement job that times out and retries is safe by
    construction rather than by the operator remembering.
    """
    if _realm.is_sandbox():
        raise SandboxNoDisbursement()
    batch = (payout_batch_id or "").strip()
    if not batch:
        raise PaymentsDenied("batch_required", "A payout batch id is required.")
    if not earning_ids and not user_id:
        # A call with no target would mean "pay the entire company", which is
        # never what anyone meant to type.
        raise PaymentsDenied(
            "target_required",
            "Name the earnings to pay, or the physician to pay them to.")

    now = _now()
    result = store.mark_earnings_paid(
        payout_batch_id=batch, paid_at=_ledger_ts(now),
        earning_ids=earning_ids, user_id=user_id)

    for row in result["marked"]:
        store.log_event(
            entity_type="earning", entity_id=row["earning_id"],
            event_type="earning_paid", actor=actor_id,
            payload={"payout_batch_id": batch, "kind": row["kind"],
                     "ref_id": row["ref_id"], "user_id": row["user_id"],
                     "amount_cents": int(row["amount_cents"])},
        )
    total = sum(int(r["amount_cents"]) for r in result["marked"])
    log.warning(
        "asclepius.payments: batch %s marked %d earnings paid (%d cents) by %s",
        batch, len(result["marked"]), total, actor_id or "unknown")
    return {
        "payout_batch_id": batch,
        "marked": len(result["marked"]),
        "amount_cents": total,
        "already_in_batch": result["already_in_batch"],
        "skipped": result["skipped"],
    }


# ═══ The Earnings read model ══════════════════════════════════════════════════
_KIND_LABELS = {KIND_TASK: "Task", KIND_REVIEW_SESSION: "Review session",
                KIND_REFERRAL: "Referral",
                KIND_REFEREE_BONUS: "First case bonus",
                KIND_HS_REFERRAL: "Health system introduction"}
# Words, not tokens — a raw status string never reaches a human.
STATUS_WORDS = {
    ACCRUED: "Pending review",
    APPROVED: "Approved",
    VOID: "Not approved",
    PAID: "Paid",
}


def _line(
    totals: Dict[str, Any], kind: str, label: str, rate_cents: int
) -> Dict[str, Any]:
    """One breakdown row: what has settled, and what is still in review.

    APPROVED and PAID are both "earned and not in doubt" from the doctor's side,
    so they make up the settled half. ACCRUED is submitted work awaiting review —
    real, countable, and not yet money. VOID is excluded from both: it is
    reported on its own row with the reason attached, never folded into a total.
    """

    def _n(status: str) -> int:
        return int(totals.get(status, {}).get(kind, {}).get("n", 0))

    def _c(status: str) -> int:
        return int(totals.get(status, {}).get(kind, {}).get("cents", 0))

    return {
        "kind": kind,
        "label": label,
        "count": _n(APPROVED) + _n(PAID),
        "rate_cents": rate_cents,
        "cents": _c(APPROVED) + _c(PAID),
        "pending_count": _n(ACCRUED),
        "pending_cents": _c(ACCRUED),
    }


def _quality_reasons(row: Any) -> Optional[List[str]]:
    """The itemization stored on a ledger row, or None."""
    try:
        raw = row["quality_reasons_json"] if "quality_reasons_json" in row.keys() else None
    except (TypeError, AttributeError):
        raw = None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return [str(x) for x in parsed] if isinstance(parsed, list) else None


def earnings_summary(store, *, user_id: str, limit: int = 50) -> Dict[str, Any]:
    """Everything the Earnings page shows, for ONE physician.

    Reconciles first, so a labeler who submitted a minute ago sees the row rather
    than an honest-looking zero.
    """
    try:
        # Scoped to THIS physician (audit M1). One doctor's read must not
        # materialize — or auto-approve — anybody else's money.
        reconcile_task_accruals(store, user_id=user_id)
    except Exception:
        # A reconciliation failure must never turn the Earnings page into a 500:
        # the ledger rows that already exist are still the truth, and showing them
        # beats showing an error. Logged loudly because it IS a defect.
        log.exception("asclepius.payments: accrual reconciliation failed; serving the ledger as-is")

    # And their own referral funnel, from their side. A SEPARATE try/except on
    # purpose: a referral that cannot be settled must never stop the reconcile
    # that pays this physician for their OWN work, and the funnel is the half
    # more likely to hit something unexpected (it reaches other people's rows).
    try:
        reconcile_referral_bounties(store, referrer_id=user_id)
    except Exception:
        log.exception("asclepius.payments: referral reconciliation failed; "
                      "serving the funnel as-is")

    user = store.get_user_by_id(user_id)
    totals = store.earnings_totals_for_user(user_id)
    rows = store.earnings_for_user(user_id, limit=limit)
    sessions = {s["session_id"]: s for s in store.work_sessions_for_user(user_id, limit=limit)}
    specialties = store.submission_specialties(
        [r["ref_id"] for r in rows if r["kind"] == KIND_TASK])

    def _cents(status: str) -> int:
        return sum(v["cents"] for v in totals.get(status, {}).values())

    # PAID is money that has actually left the building; it belongs in the
    # headline alongside APPROVED, because from the doctor's side both are
    # "earned and not in doubt". But they are NOT interchangeable — "you have been
    # paid $75" and "we owe you $75" are different sentences — so both halves are
    # served separately and the page can say which is which.
    paid_cents = _cents(PAID)
    unpaid_cents = _cents(APPROVED)
    approved_cents = unpaid_cents + paid_cents

    recent = []
    for r in rows:
        item = {
            "earning_id": r["earning_id"],
            "kind": r["kind"],
            "kind_label": _KIND_LABELS.get(r["kind"], r["kind"]),
            "ref_id": r["ref_id"],
            "amount_cents": int(r["amount_cents"]),
            "rate_cents": int(r["rate_cents"]),
            "status": r["status"],
            "status_word": STATUS_WORDS.get(r["status"], r["status"]),
            "accrued_at": r["accrued_at"],
            "resolved_at": r["resolved_at"],
            "note": r["note"],
            "payout_batch_id": r["payout_batch_id"],
            "detail": None,
            # Why this row is worth what it is worth. A silent deduction is the
            # worst possible version of this feature: a physician watching a
            # number go down with no reason attached learns only that the number
            # can go down. Present on every row, including the ones paid at full
            # rate, so the explanation is not itself a signal that something is
            # wrong.
            "quality_multiplier": r["quality_multiplier"] if "quality_multiplier" in r.keys() else None,
            "quality_reasons": _quality_reasons(r),
            # "We are still deciding" is honest, and it is better than a number
            # that silently changes under them later.
            "awaiting_review": bool(r["quality_hold"]) if "quality_hold" in r.keys() else False,
        }
        if r["kind"] == KIND_REVIEW_SESSION:
            s = sessions.get(r["ref_id"])
            if s:
                item["detail"] = f"{int(round((s.get('credited_seconds') or 0) / 60))} min"
        elif r["kind"] == KIND_TASK:
            spec = specialties.get(r["ref_id"])
            if spec:
                item["detail"] = f"{spec} case"
        recent.append(item)

    bounty_cents = referral_bounty_cents()
    referral_block = _referral_block(store, user=user, bounty_cents=bounty_cents)
    lines = [
        _line(totals, KIND_TASK, "Tasks labeled", tl_rate_cents()),
        _line(totals, KIND_REVIEW_SESSION, "Review sessions", tr_session_cents()),
    ]
    # The referral line only exists once there is a referral to report. A doctor
    # who has never referred anyone should not be shown a third rate they are not
    # participating in — the card below does the asking, and a permanent
    # "Referrals 0 × $150 · $0" row is the growth-loop instinct this feature is
    # supposed to resist.
    #
    # The second clause is the one that keeps the arithmetic honest: a bounty
    # already earned is inside ``approved_cents`` whatever happens to the funnel
    # afterwards, so if the block is missing (a physician who may no longer
    # refer) the line must still appear or the breakdown stops summing to the
    # headline above it — a number that does not add up on a payments page.
    earned_referral_money = bool(totals.get(APPROVED, {}).get(KIND_REFERRAL)
                                 or totals.get(PAID, {}).get(KIND_REFERRAL))
    if (referral_block and referral_block["total"]) or earned_referral_money:
        referral_line = _line(totals, KIND_REFERRAL, "Referrals", bounty_cents)
        # PENDING here is NOT an ``accrued`` ledger row — a bounty has no accrued
        # state, because there is nothing to review. It is a referral in flight:
        # somebody invited, not yet at their first case. That distinction is why
        # this line's pending half is computed from the FUNNEL and not from the
        # ledger totals, and why it has to be carried at all: without it the
        # doctor sees nothing and assumes nothing happened.
        in_flight = referral_block["pending_count"] if referral_block else 0
        referral_line["pending_count"] = in_flight
        referral_line["pending_cents"] = (
            referral_block["pending_cents"] if referral_block else 0)
        referral_line["pending_label"] = _pending_referral_label(in_flight)
        lines.append(referral_line)

    return {
        "currency": "USD",
        "approved_cents": approved_cents,
        "paid_cents": paid_cents,
        "unpaid_cents": unpaid_cents,
        "pending_cents": _cents(ACCRUED),
        "void_cents": _cents(VOID),
        # Each line reports SETTLED work and PENDING work separately.
        #
        # `count` used to be APPROVED+PAID only, while `pending_cents` above
        # counts ACCRUED — and a task accrues the moment it is submitted and only
        # settles after review. So the first thing a new labeler ever saw was
        # "$75 pending" sitting beside "Tasks labeled: 0", which reads as a bug in
        # the thing that pays them. The counts now come from the same states as
        # the money beside them, and the pending half is carried explicitly rather
        # than being left for the reader to reconcile.
        "lines": lines,
        "recent": recent,
        # The referral card's whole payload: the funnel, what it is worth, and
        # whether this physician may refer at all. Absent (None) rather than an
        # empty block for someone who cannot, so the page renders nothing rather
        # than an inert form.
        "referrals": referral_block,
        # Present so the page can say, honestly, "you are not paid per task" to an
        # advisor rather than showing them an unexplained $0.
        "accrues_payment": compensation.accrues_payment(user),
        # The live session, if one is open, so the Earnings page can render the
        # countdown without the reviewer having to be on the review tab. Read-only:
        # the NONCE is deliberately withheld, because the tab that is beating is
        # the review tab and handing a second tab a live nonce would let two
        # clients race for the same session's next beat.
        "open_session": _open_session_view(store, user_id=user_id),
        "params": client_params(),
    }


def _referral_block(
    store, *, user: Optional[Dict[str, Any]], bounty_cents: int,
) -> Optional[Dict[str, Any]]:
    """The referral card's payload, or None for a physician who cannot refer.

    Wrapped because the funnel reaches rows this request did not create, and a
    referral read failing must not turn a doctor's own ledger into a 500.
    """
    if user is None or not _referrals.can_refer(user):
        return None
    try:
        return _referrals.funnel(store, referrer=user, bounty_cents=bounty_cents)
    except Exception:
        log.exception("asclepius.payments: referral funnel failed for %s", user.get("id"))
        return None


def _pending_referral_label(n: int) -> str:
    """"1 invited, awaiting their first case" — the sub-line that IS the design.

    A count with no sentence is a number a physician has to interpret; the whole
    point of this row is that the wait is legible without interpretation.
    """
    if n <= 0:
        return ""
    who = "1 invited" if n == 1 else f"{n} invited"
    return f"{who}, awaiting their first case"


def _open_session_view(store, *, user_id: str) -> Optional[Dict[str, Any]]:
    row = store.open_work_session_row(user_id=user_id, kind=SESSION_KIND_REVIEW)
    if row is None:
        return None
    min_seconds = int(row.get("min_seconds") or tr_min_seconds())
    continuous = int(row.get("continuous_seconds") or 0)
    return {
        "session_id": row["session_id"],
        "started_at": row.get("started_at"),
        "last_beat_at": row.get("last_beat_at"),
        "credited_seconds": int(row.get("credited_seconds") or 0),
        "continuous_seconds": continuous,
        "min_seconds": min_seconds,
        "remaining_seconds": max(0, min_seconds - continuous),
        "qualified": continuous >= min_seconds,
        "rate_cents": int(row.get("rate_cents") or tr_session_cents()),
    }
