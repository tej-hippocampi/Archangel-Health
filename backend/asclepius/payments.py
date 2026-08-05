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

import logging
import os
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from asclepius import compensation

log = logging.getLogger("asclepius.payments")

# ─── Kinds ────────────────────────────────────────────────────────────────────
KIND_TASK = "task"
KIND_REVIEW_SESSION = "review_session"
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


def tl_auto_approve_days() -> int:
    """A labeler is never held hostage by a review backlog. If nobody reviews
    their work inside this window, it approves."""
    return _env_int("ASCLEPIUS_TL_AUTO_APPROVE_DAYS", 14)


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
def credit_from_beats(
    beats: List[Dict[str, Any]], *, min_seconds: int,
    max_gap: int = MAX_GAP_SECONDS, pause_tolerance: int = PAUSE_TOLERANCE_SECONDS,
) -> Dict[str, Any]:
    """Total credited seconds, longest continuous run, and whether it qualifies.

    Pure: same rows in, same numbers out, forever. That is what makes a payout
    reconstructible from the record months later, and what makes
    ``close_session`` safe to call repeatedly.

    ``beats`` must be ordered by ``seq``. Server timestamps are used; a beat whose
    timestamp will not parse is skipped rather than being allowed to poison the
    arithmetic with a zero."""
    stamps: List[Tuple[datetime, bool]] = []
    for b in beats:
        ts = _parse(b.get("server_ts"))
        if ts is None:
            log.warning("asclepius.payments: unparseable server_ts on beat %r", b.get("beat_id"))
            continue
        stamps.append((ts, bool(b.get("active"))))

    credited = 0.0
    run = 0.0
    longest = 0.0
    intervals_ms: List[float] = []

    for i in range(1, len(stamps)):
        prev_ts, prev_active = stamps[i - 1]
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
                credited += gap
                run += gap
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

    return {
        "credited_seconds": int(round(credited)),
        "continuous_seconds": int(round(longest)),
        "qualified": int(round(longest)) >= int(min_seconds),
        "jitter_ms": _jitter_ms(intervals_ms),
        "beats": len(stamps),
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


# ═══ §3.1 — the frozen contract ═══════════════════════════════════════════════
def open_session(store, *, user_id: str, kind: str) -> Dict[str, Any]:
    """kind: 'review'. Returns {session_id, started_at, min_seconds,
    credited_seconds, nonce, qualified}. Idempotent: an open session for this
    user+kind is returned rather than a second one being created.

    Any open session that has gone silent past ``session_abandon_seconds`` — or
    that has been open past ``session_max_seconds`` — is finalised first, through
    the normal close path, so a session that already earned its time is still
    paid even though nobody closed it.
    """
    kind = (kind or SESSION_KIND_REVIEW).strip() or SESSION_KIND_REVIEW
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
        # A client that does not track its own seq gets one derived server-side.
        # Still strictly increasing, so the replay guard holds either way.
        beats = store.session_beats(session_id)
        next_seq = (max((int(b["seq"]) for b in beats), default=0)) + 1

    skew = _check_clock_skew(session_id, now)
    min_seconds = int(session.get("min_seconds") or tr_min_seconds())
    before_continuous = int(session.get("continuous_seconds") or 0)

    def _credit(beat_rows, session_row):
        result = credit_from_beats(
            beat_rows, min_seconds=int(session_row.get("min_seconds") or min_seconds))
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

    if skew is not None:
        log.warning(
            "asclepius.payments: clock skew %.2fs on session %s — wall clock kept as the "
            "ledger authority (signal only)", skew, session_id)
        store.log_event(
            entity_type="work_session", entity_id=session_id, event_type="clock_skew",
            actor=session.get("user_id"),
            payload={"delta_seconds": round(skew, 3),
                     "tolerance_seconds": CLOCK_SKEW_TOLERANCE_SECONDS},
        )

    jitter = result.get("jitter_ms")
    if jitter is not None and jitter < MIN_HUMAN_JITTER_MS:
        # Logged and alerted, never auto-rejected (PRD-P §3).
        log.warning(
            "asclepius.payments: beat jitter %.1fms below the %.0fms human floor on "
            "session %s — recorded as a signal, payout unaffected",
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
        return credit_from_beats(
            beat_rows, min_seconds=int(session_row.get("min_seconds") or min_seconds))

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


# ─── Session helpers ──────────────────────────────────────────────────────────
_BEAT_ERRORS = {
    "not_found": "Session not found.",
    "ended": "This session has already ended.",
    "stale_nonce": "Stale session token — this beat was not accepted.",
    "replayed_seq": "Replayed heartbeat sequence — this beat was not accepted.",
}


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
    return {
        "session_id": row["session_id"],
        "started_at": row.get("started_at"),
        "min_seconds": min_seconds,
        "credited_seconds": int(row.get("credited_seconds") or 0),
        "continuous_seconds": continuous,
        "nonce": row.get("nonce"),
        "qualified": bool(row.get("qualified")) or continuous >= min_seconds,
        "rate_cents": int(row.get("rate_cents") or tr_session_cents()),
        "remaining_seconds": max(0, min_seconds - continuous),
        "resumed": existing_session,
        "params": client_params(),
    }


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
    reject voids it only when NO payable verdict exists. That asymmetry is
    deliberate and follows §1.2's rule that a doctor must never see a number go
    down without an explanation: a later accept may restore money, a later reject
    never takes back money already approved."""
    seen = {v.strip() for v in (verdicts or "").split(",") if v.strip()}
    if seen & PAYABLE_VERDICTS:
        return APPROVED
    if seen & REJECTING_VERDICTS:
        return VOID
    return None


def reconcile_task_accruals(
    store, *, limit: int = 2000, now: Optional[datetime] = None
) -> Dict[str, int]:
    """Materialise and resolve TL task earnings. Idempotent; safe to call on every
    read. Returns a small counter dict for logging and tests.

    Three passes over the same data:
      1. every terminal-submitted, payable submission with no ledger row gets one
      2. review verdicts move ``accrued`` (or ``void``) rows to their decided state
      3. ``accrued`` rows older than ASCLEPIUS_TL_AUTO_APPROVE_DAYS auto-approve

    Reads ``case_reviews`` and never calls into the review module: a read is a
    contract-free dependency, a callback is not.
    """
    now = now or _now()
    rate = tl_rate_cents()
    counts = {"accrued": 0, "approved": 0, "voided": 0, "auto_approved": 0}

    # 1. Work with no ledger row yet. Unpayable authors are filtered in SQL, so
    #    an advisor's submissions never enter this set at all.
    for row in store.unaccrued_submissions(limit=limit):
        ref = row["submission_id"]
        implied = _verdict_status(row.get("review_verdicts"))
        # ``accrued_at`` is the moment the WORK happened, not the moment this
        # sweep noticed it — otherwise a backfill would restart every auto-approve
        # clock and a doctor's ledger would be dated by our deploy schedule.
        accrued_at = _ledger_ts(_parse(row.get("created_at")) or now)
        written = store.insert_earning(
            earning_id=_new_id("earn"), user_id=row["evaluator_id"], kind=KIND_TASK,
            ref_id=ref, amount_cents=rate, rate_cents=rate,
            status=implied or ACCRUED, accrued_at=accrued_at,
            resolved_at=_ledger_ts(now) if implied else None,
            note=_reject_note(row) if implied == VOID else None,
        )
        if written is not None:
            counts["accrued"] += 1
            store.log_event(
                entity_type="earning", entity_id=written["earning_id"],
                event_type="earning_accrued", actor=row["evaluator_id"],
                payload={"kind": KIND_TASK, "ref_id": ref, "amount_cents": rate,
                         "status": written["status"], "task_id": row.get("task_id")},
            )

    # 2. Rows awaiting a verdict. Terminal states are never re-examined.
    for row in store.unresolved_task_earnings(limit=limit):
        ref = row["submission_id"]
        status = row["status"]
        implied = _verdict_status(row.get("review_verdicts"))
        if implied == APPROVED and status in (ACCRUED, VOID):
            if store.resolve_earning(kind=KIND_TASK, ref_id=ref, status=APPROVED,
                                     resolved_at=_ledger_ts(now),
                                     only_from=[ACCRUED, VOID]):
                counts["approved"] += 1
        elif implied == VOID and status == ACCRUED:
            if store.resolve_earning(kind=KIND_TASK, ref_id=ref, status=VOID,
                                     resolved_at=_ledger_ts(now),
                                     note=_reject_note(row), only_from=[ACCRUED]):
                counts["voided"] += 1

    # 3. The backlog escape hatch.
    counts["auto_approved"] = _auto_approve(store, now=now)
    return counts


def _reject_note(row: Dict[str, Any]) -> str:
    """The reason a task was not approved, in the doctor's own reviewer's words
    where there are any. §1.2: never show a number that might go down without an
    explanation next to it."""
    note = (row.get("reject_note") or "").strip()
    base = "Not approved on review"
    return f"{base}: {note}" if note else base


def _auto_approve(store, *, now: datetime) -> int:
    """A labeler is never held hostage by a review backlog: if nobody reviews
    their work within the window, it approves.

    Runs on read rather than on a nightly cron. This deployment has no scheduler,
    and a sweep that only runs when someone is looking at the number is a sweep
    that has always run by the time the number is shown."""
    days = tl_auto_approve_days()
    cutoff = _ledger_ts(now - timedelta(days=days))
    moved = 0
    for row in store.accrued_earnings_before(cutoff):
        if store.resolve_earning(
            kind=row["kind"], ref_id=row["ref_id"], status=APPROVED,
            resolved_at=_ledger_ts(now), only_from=[ACCRUED],
            note=f"Auto-approved after {days} days without a review",
        ):
            moved += 1
            store.log_event(
                entity_type="earning", entity_id=row["earning_id"],
                event_type="earning_auto_approved", actor=None,
                payload={"kind": row["kind"], "ref_id": row["ref_id"], "days": days},
            )
    return moved


# ═══ The Earnings read model ══════════════════════════════════════════════════
_KIND_LABELS = {KIND_TASK: "Task", KIND_REVIEW_SESSION: "Review session"}
# Words, not tokens — a raw status string never reaches a human.
STATUS_WORDS = {
    ACCRUED: "Pending review",
    APPROVED: "Approved",
    VOID: "Not approved",
    PAID: "Paid",
}


def earnings_summary(store, *, user_id: str, limit: int = 50) -> Dict[str, Any]:
    """Everything the Earnings page shows, for ONE physician.

    Reconciles first, so a labeler who submitted a minute ago sees the row rather
    than an honest-looking zero.
    """
    try:
        reconcile_task_accruals(store)
    except Exception:
        # A reconciliation failure must never turn the Earnings page into a 500:
        # the ledger rows that already exist are still the truth, and showing them
        # beats showing an error. Logged loudly because it IS a defect.
        log.exception("asclepius.payments: accrual reconciliation failed; serving the ledger as-is")

    totals = store.earnings_totals_for_user(user_id)
    rows = store.earnings_for_user(user_id, limit=limit)
    sessions = {s["session_id"]: s for s in store.work_sessions_for_user(user_id, limit=limit)}
    specialties = store.submission_specialties(
        [r["ref_id"] for r in rows if r["kind"] == KIND_TASK])

    def _cents(status: str) -> int:
        return sum(v["cents"] for v in totals.get(status, {}).values())

    def _count(status: str, kind: str) -> int:
        return int(totals.get(status, {}).get(kind, {}).get("n", 0))

    # PAID is money that has actually left the building; it belongs in the
    # headline alongside APPROVED, because from the doctor's side both are
    # "earned and not in doubt".
    approved_cents = _cents(APPROVED) + _cents(PAID)

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
            "detail": None,
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

    return {
        "currency": "USD",
        "approved_cents": approved_cents,
        "pending_cents": _cents(ACCRUED),
        "void_cents": _cents(VOID),
        "lines": [
            {
                "kind": KIND_TASK, "label": "Tasks labeled",
                "count": _count(APPROVED, KIND_TASK) + _count(PAID, KIND_TASK),
                "rate_cents": tl_rate_cents(),
                "cents": (totals.get(APPROVED, {}).get(KIND_TASK, {}).get("cents", 0)
                          + totals.get(PAID, {}).get(KIND_TASK, {}).get("cents", 0)),
            },
            {
                "kind": KIND_REVIEW_SESSION, "label": "Review sessions",
                "count": _count(APPROVED, KIND_REVIEW_SESSION) + _count(PAID, KIND_REVIEW_SESSION),
                "rate_cents": tr_session_cents(),
                "cents": (totals.get(APPROVED, {}).get(KIND_REVIEW_SESSION, {}).get("cents", 0)
                          + totals.get(PAID, {}).get(KIND_REVIEW_SESSION, {}).get("cents", 0)),
            },
        ],
        "recent": recent,
        # Present so the page can say, honestly, "you are not paid per task" to an
        # advisor rather than showing them an unexplained $0.
        "accrues_payment": compensation.accrues_payment(store.get_user_by_id(user_id)),
        # The live session, if one is open, so the Earnings page can render the
        # countdown without the reviewer having to be on the review tab. Read-only:
        # the NONCE is deliberately withheld, because the tab that is beating is
        # the review tab and handing a second tab a live nonce would let two
        # clients race for the same session's next beat.
        "open_session": _open_session_view(store, user_id=user_id),
        "params": client_params(),
    }


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
