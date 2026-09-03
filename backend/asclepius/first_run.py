"""Welcome package v2 §1 — the first-run checklist model, in one place.

The old model had two states and skip was terminal, which had two consequences
the product did not want. A physician could skip "Choose your start" and "Do the
practice case" — the two stops that are the whole point — and once every stop
carried any outcome the walkthrough never returned, so the optional stops were
asked about exactly once and then never again.

The model here is three states over two classes of stop:

    REQUIRED (welcome, start, practice)   null → done.        No skip exists.
    OPTIONAL (community, earnings, manual) null → deferred → done.

``deferred`` means "asked, declined this session". It is deliberately NOT
terminal: it may be rewritten every session, and it is what lets the cadence in
``mode()`` ask twice and then go quiet, instead of asking once and going silent
forever.

This module is pure and dependency-light on purpose — ``store``, ``auth``, the
router and the tests all read the same normalizer, so there is exactly one
answer to "what does this blob mean" rather than four that drift.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from asclepius.schemas import FIRST_RUN_STOPS

#: The three stops a physician must actually do. There is no skip control on
#: any of them, in the UI or on the wire: ``PATCH /me/first-run`` refuses a
#: defer against one with a 400, and ``/tasks/next`` refuses real work while any
#: of them is open. The gate ends here — nothing after ``practice`` blocks work.
REQUIRED_STOPS: Tuple[str, ...] = ("welcome", "start", "practice")

#: The three that are genuinely optional. A physician who never opens any of
#: them keeps full access to every real case; these are offered, twice, and then
#: they become a banner.
OPTIONAL_STOPS: Tuple[str, ...] = ("community", "earnings", "manual")

DONE = "done"
DEFERRED = "deferred"

#: The cadence answers, in the order ``mode()`` decides them.
MODE_WALKTHROUGH = "walkthrough"
MODE_REENTRY = "reentry"
MODE_BANNER = "banner"
MODE_NONE = "none"

#: Logins 2 and 3 get the re-entry page; the 4th onwards gets the banner. The
#: number is the *count of sessions seen*, so "<= 3" is logins two and three —
#: login one is the walkthrough itself.
REENTRY_THROUGH_SESSION = 3

assert set(REQUIRED_STOPS) | set(OPTIONAL_STOPS) == set(FIRST_RUN_STOPS), \
    "the required/optional split must cover exactly the six declared stops"


def is_required(stop: str) -> bool:
    return stop in REQUIRED_STOPS


def normalize_stops(raw: Any) -> Dict[str, str]:
    """The stored ``stops`` map, migrated to the three-state model.

    This is the migration, and it runs on READ rather than as a batch UPDATE.
    That is deliberate: it is idempotent, it cannot half-finish, it needs no
    downtime, and a row that is never read again costs nothing. ``set_first_run``
    writes the normalized shape back, so a row rewrites itself the first time
    anybody touches it.

    Per §1:
      * optional ``skipped`` → ``deferred`` — they were asked and declined, and
        under the new model that is a question we may ask again.
      * required ``skipped`` → ``null`` — today's data has real accounts with the
        practice case skipped, which the product should never have allowed. They
        get asked again, and this time there is no skip control to find.
      * anything unrecognised is treated the same way as ``skipped``, because an
        outcome nobody can name should not silently count as finished work.
    """
    out: Dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for stop in FIRST_RUN_STOPS:
        value = raw.get(stop)
        if not isinstance(value, str):
            continue
        value = value.strip().lower()
        if value == DONE:
            out[stop] = DONE
        elif is_required(stop):
            # skipped, deferred, or junk — all mean "not done", and a required
            # stop that is not done is simply open.
            continue
        else:
            out[stop] = DEFERRED
    return out


def _coerce_sessions_seen(raw: Any) -> int:
    """``sessions_seen``, floored at 1.

    §1 backfills existing rows to 1: every account that has a stored checklist
    has by definition logged in at least once. Zero would put a returning
    physician back at "this is your first login", which is the one reading that
    is definitely wrong.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def normalize(parsed: Any, *, version: int) -> Dict[str, Any]:
    """One stored blob → the canonical v2 shape. Never raises.

    A bad read costs one extra walkthrough; a raise costs a physician who cannot
    open the portal. Every caller in this codebase already made that trade, and
    this keeps making it.
    """
    empty = {
        "version": version,
        "stops": {},
        "sessions_seen": 1,
        "last_session_counted": None,
        "completed_at": None,
        "dismissed_at": None,
    }
    if not isinstance(parsed, dict):
        return empty
    stops = normalize_stops(parsed.get("stops"))
    state = {
        "version": version,
        "stops": stops,
        "sessions_seen": _coerce_sessions_seen(parsed.get("sessions_seen")),
        # §5's idempotency key: the ``jti`` of the token that last incremented
        # ``sessions_seen``. One login mints one token, so a reload — or six
        # parallel /auth/me calls from one page paint — carries the same jti and
        # counts once. Carried verbatim; it is never read as anything but "equal
        # to the current jti, or not".
        "last_session_counted": parsed.get("last_session_counted"),
        # Recomputed rather than carried. The old model set ``completed_at`` once
        # every stop had *any* outcome, so an account that skipped all three
        # optional stops is stored as complete while, under §1, it has three
        # stops still open. Trusting the stored stamp there would silence the
        # re-entry cadence for exactly the physicians it was written for.
        "completed_at": parsed.get("completed_at") if is_complete(stops) else None,
        # Carried, and still honoured. ``dismiss`` is an explicit "stop asking
        # me" a physician clicked on the finish card, and §4.2's "no don't-show-
        # again" governs the NEW screens rather than retracting a choice someone
        # already made. It never opens the required gate — see ``mode()``.
        "dismissed_at": parsed.get("dismissed_at"),
    }
    return state


def is_complete(stops: Dict[str, str]) -> bool:
    """True only when all six are ``done``. Deferred never completes the set."""
    return all(stops.get(s) == DONE for s in FIRST_RUN_STOPS)


def required_open(stops: Dict[str, str]) -> Tuple[str, ...]:
    """The required stops still owed, in order."""
    return tuple(s for s in REQUIRED_STOPS if stops.get(s) != DONE)


def optional_remaining(stops: Dict[str, str]) -> Tuple[str, ...]:
    """The optional stops not yet ``done`` — deferred ones included.

    Deferred is "not now", not "no". This is the list the re-entry page renders
    and the banner counts down.
    """
    return tuple(s for s in OPTIONAL_STOPS if stops.get(s) != DONE)


def mode(state: Optional[Dict[str, Any]]) -> str:
    """§2's cadence, as one function.

        dismissed                  → none
        required unfinished        → walkthrough   (regardless of session count)
        no optional remaining      → none
        sessions_seen <= 3         → reentry       (logins 2 and 3)
        otherwise                  → banner

    The Python side is the authority the tests pin; ``first_run.js`` carries the
    same lines, because the shell has to decide what to paint before it can ask
    anybody. They are checked against each other in the test suite rather than
    trusted to stay in step.

    ── Why ``dismissed`` is checked FIRST, and not last ──────────────────────

    §2's table starts at "required unfinished", and read literally that would put
    every EXISTING contributor into the welcome letter on the deploy that ships
    this. The store's one-time backfill (``store.py``, the ``first_run_json``
    ALTER branch) stamped every already-approved account with ``dismissed_at``
    and an EMPTY stops map — that is how a physician who has been labeling for
    months was kept out of "Welcome to Archangel Health". Those rows have three
    required stops open and always will, so testing ``required_open`` ahead of
    the stamp would undo that migration and drop the entire existing roster into
    an onboarding they finished long before it was written.

    This costs nothing the gate was protecting. ``dismiss`` is reachable from
    exactly one place in the product — the finish card, after every stop is
    closed — so nobody can dismiss their way past a required stop, and real work
    is gated by ``require_practice_case`` on the server regardless of what this
    function returns. Dismiss silences the ASKING; it has never granted access,
    and it does not grant any here.
    """
    if (state or {}).get("dismissed_at"):
        return MODE_NONE
    stops = normalize_stops((state or {}).get("stops"))
    if required_open(stops):
        return MODE_WALKTHROUGH
    if not optional_remaining(stops):
        return MODE_NONE
    if _coerce_sessions_seen((state or {}).get("sessions_seen")) <= REENTRY_THROUGH_SESSION:
        return MODE_REENTRY
    return MODE_BANNER
