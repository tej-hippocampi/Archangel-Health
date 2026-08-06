"""PRD-P Phase 2 — the billable review session, proven end to end.

Money gets an INTEGRATION test, not a unit test. Every assertion about a payout
here is reached by issuing HTTP requests as a signed-in physician: open the
session over the API, beat over the API, close over the API, then read the ledger
back over the API. A test that constructs state by calling the store directly
proves the store works, which is not the claim under test.

Beat timing is controlled by moving the module's clock, not by sleeping: a
suite that actually waits 20 minutes never runs, and one that waits 2 seconds is
testing rounding. The clock is the ONLY thing faked — the routes, the store, the
transactions and the arithmetic are all real.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import compensation as asc_comp  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402

client = TestClient(A.app)

_T0 = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    asc_payments._MONOTONIC_REF.clear()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _reviewer(**kw):
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier = 'reviewer', verification_status = 'approved' "
                     "WHERE id = ?", (u["id"],))
    return store.get_user_by_id(u["id"])


def _advisor():
    """A real appointed advisor, minted through the real appointment path — which
    is what sets ``compensation_model='equity_only'``. Faking the column with an
    UPDATE would test the predicate and not the rule."""
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    return store.appoint_advisor(u["id"], agreement_ref="AGR-2026-01",
                                 appointed_by="admin@asclepius.test")


class Clock:
    """A movable server clock. ``payments._now`` is the single place the module
    reads wall time, so replacing it moves every timestamp the feature writes —
    beats, session stamps and ledger stamps alike — with no other seam."""

    def __init__(self, monkeypatch, start=_T0):
        self.t = start
        monkeypatch.setattr(asc_payments, "_now", lambda: self.t)

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)
        return self.t


def _open(headers, kind="review"):
    r = client.post("/api/asclepius/sessions", json={"kind": kind}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# Every beat names the work it is a beat for (audit C2). These tests are about
# the CLOCK, so they hold one key throughout — which is also the shape of a real
# reviewer on one hard case, and the shape that must keep paying.
_KEY = "pair-under-test"


def _beat(headers, session_id, nonce, seq, active=True, expect=200, key=_KEY):
    body = {"nonce": nonce, "seq": seq, "active": active}
    if key is not None:
        body["progress_key"] = key
    r = client.post(
        f"/api/asclepius/sessions/{session_id}/heartbeat", json=body, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def _close(headers, session_id, reason="closed"):
    r = client.post(f"/api/asclepius/sessions/{session_id}/close",
                    json={"reason": reason}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _run(headers, clock, session, *, beats, interval=15, active=True):
    """Beat ``beats`` more times at ``interval`` seconds apart, through the API,
    honouring the rotating nonce. Returns the last response."""
    nonce, seq, last = session["nonce"], 0, None
    # The opening beat at t+0 establishes the first timestamp; credited time is
    # the sum of gaps BETWEEN beats, so N gaps needs N+1 beats.
    seq += 1
    last = _beat(headers, session["session_id"], nonce, seq)
    nonce = last["next_nonce"]
    for _ in range(beats):
        clock.advance(interval)
        seq += 1
        last = _beat(headers, session["session_id"], nonce, seq, active=active)
        nonce = last["next_nonce"]
    return last, nonce, seq


# ─── The threshold ────────────────────────────────────────────────────────────
def test_twenty_clean_minutes_qualifies_and_pays_exactly_once(monkeypatch):
    clock = Clock(monkeypatch)
    user = _reviewer()
    h = A.headers_for(user)

    session = _open(h)
    assert session["min_seconds"] == 1200
    assert session["rate_cents"] == 10000

    # 80 gaps x 15 s = 1200 s.
    last, nonce, seq = _run(h, clock, session, beats=80)
    assert last["credited_seconds"] == 1200
    assert last["qualified"] is True
    assert last["remaining_seconds"] == 0

    result = _close(h, session["session_id"])
    assert result["qualified"] is True
    assert result["payout_cents"] == 10000

    ledger = client.get("/api/asclepius/earnings", headers=h).json()
    rows = [r for r in ledger["recent"] if r["kind"] == "review_session"]
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == 10000
    assert rows[0]["rate_cents"] == 10000
    assert rows[0]["status"] == "approved"
    assert ledger["approved_cents"] == 10000


def test_nineteen_thirty_earns_nothing_and_the_record_survives(monkeypatch):
    """The cliff, and the mitigation that makes it defensible. $0 — and 1,170
    seconds still on the record, because "we have no records" is the worst
    possible answer to a challenge."""
    clock = Clock(monkeypatch)
    user = _reviewer()
    h = A.headers_for(user)

    session = _open(h)
    last, _n, _s = _run(h, clock, session, beats=78)   # 78 x 15 = 1170 s
    assert last["credited_seconds"] == 1170
    assert last["qualified"] is False
    assert last["remaining_seconds"] == 30

    result = _close(h, session["session_id"])
    assert result["qualified"] is False
    assert result["payout_cents"] == 0

    assert client.get("/api/asclepius/earnings", headers=h).json()["approved_cents"] == 0

    row = _store().get_work_session(session["session_id"])
    assert row["credited_seconds"] == 1170          # never deleted, never zeroed
    assert row["continuous_seconds"] == 1170
    assert row["qualified"] == 0                    # decided, not NULL
    assert row["end_reason"] == "closed"


def test_definition_of_done_twenty_one_minutes_pays_nineteen_does_not(monkeypatch):
    """PRD-P §7.2, in its own numbers: a TR who reviews for 21 minutes sees $100
    approved; a TR who leaves at 19 minutes sees $0 — and the session row still
    records 1,140 seconds."""
    clock = Clock(monkeypatch)

    stayed_h = A.headers_for(_reviewer())
    stayed = _open(stayed_h)
    _run(stayed_h, clock, stayed, beats=84)        # 84 × 15 = 1260 s = 21:00
    assert _close(stayed_h, stayed["session_id"])["payout_cents"] == 10000
    assert client.get("/api/asclepius/earnings", headers=stayed_h).json()["approved_cents"] == 10000

    left_h = A.headers_for(_reviewer())
    left = _open(left_h)
    _run(left_h, clock, left, beats=76)            # 76 × 15 = 1140 s = 19:00
    assert _close(left_h, left["session_id"])["payout_cents"] == 0
    assert client.get("/api/asclepius/earnings", headers=left_h).json()["approved_cents"] == 0
    assert _store().get_work_session(left["session_id"])["credited_seconds"] == 1140


# ─── Gaps ─────────────────────────────────────────────────────────────────────
def test_sixty_second_silence_breaks_the_run_and_is_not_paid(monkeypatch):
    """Silence past MAX_GAP (45 s) ends the continuous run. The gap credits
    nothing — crediting the capped 45 s would be paying for provable absence."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    nonce, seq = session["nonce"], 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(20):                       # 20 x 15 = 300 s
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    clock.advance(60)                         # silence, no pause beat
    seq += 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(10):                       # 10 x 15 = 150 s
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    # The gap itself is excluded from credited time...
    assert r["credited_seconds"] == 450
    # ...and the run is broken, so the longest continuous stretch is the first leg.
    assert r["continuous_seconds"] == 300
    assert r["qualified"] is False


def test_thirty_second_declared_pause_stitches_the_run_but_is_not_paid(monkeypatch):
    """A closed laptop lid, declared with an ``active=false`` beat on
    visibilitychange: inside PAUSE_TOLERANCE the run survives, and the paused
    seconds are credited to nobody."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    nonce, seq = session["nonce"], 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(20):                       # 300 s of work
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    seq += 1                                  # the pause marker, same instant
    r = _beat(h, sid, nonce, seq, active=False); nonce = r["next_nonce"]
    clock.advance(30)                         # away for 30 s
    seq += 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(20):                       # another 300 s of work
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    assert r["credited_seconds"] == 600       # the 30 s gap is NOT paid
    assert r["continuous_seconds"] == 600     # ...and the run was NOT broken


def test_a_pause_longer_than_tolerance_breaks_the_run(monkeypatch):
    """Declaring a pause does not buy unlimited time — past PAUSE_TOLERANCE the
    reviewer left, and the run ends."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    nonce, seq = session["nonce"], 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(20):
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    seq += 1
    r = _beat(h, sid, nonce, seq, active=False); nonce = r["next_nonce"]
    clock.advance(120)                        # 120 s > 90 s tolerance
    seq += 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(5):
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    assert r["credited_seconds"] == 375       # 300 + 75, gap unpaid
    assert r["continuous_seconds"] == 300     # run broken at the long pause


# ─── Idempotence ──────────────────────────────────────────────────────────────
def test_close_five_times_yields_exactly_one_earnings_row(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _run(h, clock, session, beats=84)         # comfortably over the threshold

    results = [_close(h, session["session_id"]) for _ in range(5)]
    assert all(r["qualified"] for r in results)
    assert all(r["credited_seconds"] == results[0]["credited_seconds"] for r in results)

    rows = [r for r in client.get("/api/asclepius/earnings", headers=h).json()["recent"]
            if r["kind"] == "review_session"]
    assert len(rows) == 1


def test_close_is_idempotent_under_concurrency(monkeypatch):
    """The application check is an optimization; UNIQUE(kind, ref_id) is the
    guarantee. Eight threads closing the same session at once must produce one
    ledger row and one answer."""
    import threading

    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _run(h, clock, session, beats=84)
    sid = session["session_id"]

    results, errors = [], []
    barrier = threading.Barrier(8)

    def _worker():
        try:
            barrier.wait(timeout=10)
            results.append(asc_payments.close_session(_store(), session_id=sid, reason="closed"))
        except Exception as exc:                       # pragma: no cover - a failure IS the finding
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 8
    assert all(r["qualified"] for r in results)
    # Exactly one of the eight can have written the row; all eight must agree on
    # the number, and the ledger must hold one row.
    assert len({r["credited_seconds"] for r in results}) == 1
    rows = [r for r in client.get("/api/asclepius/earnings", headers=h).json()["recent"]
            if r["kind"] == "review_session"]
    assert len(rows) == 1


def test_open_session_is_idempotent(monkeypatch):
    Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    first = _open(h)
    second = _open(h)
    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True


# ─── Anti-replay ──────────────────────────────────────────────────────────────
# ``seq`` used to be optional for the whole life of a session, with the server
# deriving MAX(seq)+1 on every beat that omitted it. Audit H1 removed that: it is
# the exact affordance a replayer wants, and it is now allowed only for the first
# beat of a session, when there is nothing yet to replay. The rule and its
# recovery path (POST /sessions/{id}/resume) are covered in
# ``test_payments_nonce.py``; the test that lived here asserted the old policy.

def test_a_replayed_seq_is_rejected(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    r1 = _beat(h, sid, session["nonce"], 1)
    clock.advance(15)
    r2 = _beat(h, sid, r1["next_nonce"], 2)
    clock.advance(15)
    # Correct nonce, but a sequence number already used.
    _beat(h, sid, r2["next_nonce"], 2, expect=409)


def test_a_stale_nonce_is_rejected(monkeypatch):
    """The rotating nonce is the whole anti-fraud control: a naive
    setInterval(fetch) that never parses the response cannot beat."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    opening = session["nonce"]
    _beat(h, sid, opening, 1)
    clock.advance(15)
    # Re-presenting the nonce that was already spent.
    _beat(h, sid, opening, 2, expect=409)


def test_a_rejected_beat_credits_no_time(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    r = _beat(h, sid, session["nonce"], 1)
    clock.advance(15)
    _beat(h, sid, "not-the-nonce", 2, expect=409)
    clock.advance(600)
    _beat(h, sid, "not-the-nonce", 3, expect=409)

    assert _store().get_work_session(sid)["credited_seconds"] == 0
    assert len(_store().session_beats(sid)) == 1


# ─── The advisor rule ─────────────────────────────────────────────────────────
def test_an_advisor_accrues_nothing(monkeypatch):
    """An equity-holding advisor works a full qualifying session: the session
    closes, the seconds are recorded, and no money exists."""
    clock = Clock(monkeypatch)
    advisor = _advisor()
    assert advisor["compensation_model"] == asc_comp.EQUITY_ONLY
    assert asc_comp.accrues_payment(advisor) is False
    h = A.headers_for(advisor)

    session = _open(h)
    last, _n, _s = _run(h, clock, session, beats=84)
    assert last["qualified"] is True          # the TIME qualified...

    result = _close(h, session["session_id"])
    assert result["qualified"] is True
    assert result["payout_cents"] == 0        # ...and the money did not follow

    ledger = client.get("/api/asclepius/earnings", headers=h).json()
    assert ledger["recent"] == []
    assert ledger["approved_cents"] == 0
    assert ledger["accrues_payment"] is False

    # The record still exists in full — quality is measured everywhere, only
    # money is excluded.
    row = _store().get_work_session(session["session_id"])
    assert row["credited_seconds"] >= 1200
    assert row["qualified"] == 1


# ─── Restart safety ───────────────────────────────────────────────────────────
def test_killing_the_process_mid_session_loses_no_credited_time(monkeypatch):
    """Each beat is an independent durable row and there is no in-memory session
    state, so a redeploy mid-session is indistinguishable from a quiet moment."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    nonce, seq = session["nonce"], 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(40):                        # 600 s
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    assert r["credited_seconds"] == 600

    # Simulate the restart: drop every scrap of process-local state the module
    # holds and rebuild the store against the same database file.
    asc_payments._MONOTONIC_REF.clear()
    from asclepius import store as asc_store
    asc_store.reset_store_for_tests(db_path=_store().db_path)

    clock.advance(15); seq += 1
    r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]
    for _ in range(39):                        # another 585 s
        clock.advance(15); seq += 1
        r = _beat(h, sid, nonce, seq); nonce = r["next_nonce"]

    assert r["credited_seconds"] == 1200
    assert r["qualified"] is True
    assert _close(h, sid)["payout_cents"] == 10000


# ─── Ownership ────────────────────────────────────────────────────────────────
def test_a_session_belongs_to_exactly_one_physician(monkeypatch):
    """No endpoint on this router takes a user_id, and a session id is not a
    capability. Another physician gets the same 404 as a nonexistent session, so
    the route cannot be used to enumerate anyone's sessions."""
    clock = Clock(monkeypatch)
    owner_h = A.headers_for(_reviewer())
    other_h = A.headers_for(_reviewer())
    session = _open(owner_h)
    sid = session["session_id"]

    assert client.post(f"/api/asclepius/sessions/{sid}/heartbeat",
                       json={"nonce": session["nonce"], "seq": 1, "active": True,
                             "progress_key": _KEY},
                       headers=other_h).status_code == 404
    assert client.post(f"/api/asclepius/sessions/{sid}/close",
                       json={"reason": "closed"}, headers=other_h).status_code == 404
    assert client.get(f"/api/asclepius/sessions/{sid}", headers=other_h).status_code == 404
    assert client.get("/api/asclepius/sessions/ws-does-not-exist",
                      headers=other_h).status_code == 404


def test_the_read_only_session_route_never_hands_out_a_nonce(monkeypatch):
    """The nonce is minted in one place and rotated in one place. Serving it on a
    GET would let a second tab — the Earnings page polls this route — start
    beating on a session the review tab holds."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _run(h, clock, session, beats=4)

    state = client.get(f"/api/asclepius/sessions/{session['session_id']}", headers=h)
    assert state.status_code == 200
    payload = state.json()
    assert "nonce" not in payload
    assert payload["continuous_seconds"] == 60
    assert payload["ended"] is False

    # The earnings payload's open-session snapshot withholds it too.
    live = client.get("/api/asclepius/earnings", headers=h).json()["open_session"]
    assert live is not None and "nonce" not in live
    assert live["session_id"] == session["session_id"]


def test_an_advisors_open_session_still_appears_on_their_earnings_page(monkeypatch):
    """The countdown is about TIME, which an advisor works like anyone else. Only
    the money is excluded."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_advisor())
    session = _open(h)
    _run(h, clock, session, beats=4)

    payload = client.get("/api/asclepius/earnings", headers=h).json()
    assert payload["accrues_payment"] is False
    assert payload["open_session"]["continuous_seconds"] == 60


def test_earnings_requires_a_session():
    assert client.get("/api/asclepius/earnings").status_code == 401
    assert client.post("/api/asclepius/sessions", json={"kind": "review"}).status_code == 401


# ─── Abandonment ──────────────────────────────────────────────────────────────
def test_an_abandoned_session_is_settled_not_stranded(monkeypatch):
    """The close beacon is ~91% reliable, so a session WILL sometimes be left
    open. It must not block the next one — and if it already earned its time, it
    must still be paid."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    first = _open(h)
    _run(h, clock, first, beats=84)           # qualified, then the beacon is lost

    clock.advance(asc_payments.session_abandon_seconds() + 60)
    second = _open(h)

    assert second["session_id"] != first["session_id"]
    settled = _store().get_work_session(first["session_id"])
    assert settled["end_reason"] == "abandoned"
    assert settled["qualified"] == 1
    rows = [r for r in client.get("/api/asclepius/earnings", headers=h).json()["recent"]
            if r["kind"] == "review_session"]
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == 10000


def test_a_second_open_session_can_never_exist(monkeypatch):
    """``open_session`` is specified as idempotent, and the partial unique index
    makes it a database guarantee rather than a hopeful application check."""
    import sqlite3

    Clock(monkeypatch)
    user = _reviewer()
    first = _open(A.headers_for(user))

    conn = sqlite3.connect(_store().db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO work_sessions (session_id, user_id, kind, started_at, "
                "credited_seconds, nonce, min_seconds, rate_cents, continuous_seconds, "
                "clock_skew_beats) VALUES ('ws-sneaky', ?, 'review', ?, 0, 'n', 1200, 10000, 0, 0)",
                (user["id"], "2026-08-03T15:00:00"))
            conn.commit()
    finally:
        conn.close()
    assert _open(A.headers_for(user))["session_id"] == first["session_id"]


def test_if_two_open_sessions_ever_coexist_the_live_one_wins(monkeypatch):
    """Defence in depth behind the unique index. Whichever session beat most
    recently is the one a client is actually driving; the stragglers are settled
    through the normal close path, so time they earned is still paid."""
    clock = Clock(monkeypatch)
    store = _store()
    user = _reviewer()
    h = A.headers_for(user)

    stale = _open(h)
    _run(h, clock, stale, beats=84)                # qualified, then orphaned

    # Force the state the index exists to prevent, to prove the resolver copes.
    with store._conn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_ws_one_open_per_kind")
        conn.execute(
            "INSERT INTO work_sessions (session_id, user_id, kind, started_at, "
            "last_beat_at, credited_seconds, nonce, min_seconds, rate_cents, "
            "continuous_seconds, clock_skew_beats) "
            "VALUES ('ws-newer', ?, 'review', ?, ?, 0, 'n-new', 1200, 10000, 0, 0)",
            (user["id"], asc_payments._ts(clock.t), asc_payments._ts(clock.t)))

    resumed = _open(h)
    assert resumed["session_id"] == "ws-newer"      # the one that beat most recently
    settled = store.get_work_session(stale["session_id"])
    assert settled["end_reason"] == "abandoned"
    assert settled["qualified"] == 1
    # Its 20 minutes were still paid, even though nobody closed it.
    rows = [r for r in client.get("/api/asclepius/earnings", headers=h).json()["recent"]
            if r["kind"] == "review_session"]
    assert len(rows) == 1 and rows[0]["amount_cents"] == 10000


def test_a_sub_threshold_abandoned_session_is_recorded_and_unpaid(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    first = _open(h)
    _run(h, clock, first, beats=20)           # 300 s

    clock.advance(asc_payments.session_abandon_seconds() + 60)
    _open(h)

    settled = _store().get_work_session(first["session_id"])
    assert settled["end_reason"] == "abandoned"
    assert settled["credited_seconds"] == 300   # the record survives
    assert settled["qualified"] == 0
    assert client.get("/api/asclepius/earnings", headers=h).json()["approved_cents"] == 0


# ─── Rates are stamped, never recomputed ──────────────────────────────────────
def test_a_rate_change_mid_session_cannot_restate_it(monkeypatch):
    """Rates live in env so a change is a redeploy. The rate in force is stamped
    on the session at open and on the ledger row at accrual, so raising the rate
    tomorrow never restates what was earned today."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    assert session["rate_cents"] == 10000

    monkeypatch.setenv("ASCLEPIUS_TR_SESSION_CENTS", "25000")
    _run(h, clock, session, beats=84)
    result = _close(h, session["session_id"])

    assert result["payout_cents"] == 10000      # the rate at OPEN, not at close
    rows = [r for r in client.get("/api/asclepius/earnings", headers=h).json()["recent"]
            if r["kind"] == "review_session"]
    assert rows[0]["rate_cents"] == 10000


# ─── Signals, never gates ─────────────────────────────────────────────────────
def test_perfectly_regular_beats_are_flagged_but_still_paid(monkeypatch):
    """Zero jitter is machine-generated and is recorded as such. It is never a
    reason to refuse: the cost of a false positive is not paying a physician
    $100."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _run(h, clock, session, beats=84)           # metronome-exact, jitter == 0

    row = _store().get_work_session(session["session_id"])
    assert row["jitter_ms"] == 0.0
    assert row["jitter_ms"] < asc_payments.MIN_HUMAN_JITTER_MS
    assert _close(h, session["session_id"])["payout_cents"] == 10000


def test_the_client_clock_never_enters_the_calculation(monkeypatch):
    """A client timestamp is stored as a fraud signal. A client claiming to have
    worked for a year earns the seconds the SERVER saw and not one more."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    nonce, seq = session["nonce"], 1
    r = client.post(f"/api/asclepius/sessions/{sid}/heartbeat",
                    json={"nonce": nonce, "seq": seq, "active": True,
                          "progress_key": _KEY,
                          "client_ts": "2020-01-01T00:00:00Z"}, headers=h)
    assert r.status_code == 200
    nonce = r.json()["next_nonce"]
    clock.advance(15)
    r = client.post(f"/api/asclepius/sessions/{sid}/heartbeat",
                    json={"nonce": nonce, "seq": 2, "active": True,
                          "progress_key": _KEY,
                          "client_ts": "2099-01-01T00:00:00Z"}, headers=h)
    assert r.status_code == 200
    assert r.json()["credited_seconds"] == 15
    assert r.json()["qualified"] is False

    beats = _store().session_beats(sid)
    assert beats[0]["client_ts"] == "2020-01-01T00:00:00Z"   # recorded...
    assert beats[1]["client_ts"] == "2099-01-01T00:00:00Z"   # ...and ignored


# ─── Events ───────────────────────────────────────────────────────────────────
def test_open_qualify_and_close_are_all_logged(monkeypatch):
    """PRD-P §1.4.3. The records that make the cliff defensible only exist if
    they are written."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _run(h, clock, session, beats=84)
    _close(h, session["session_id"])

    events = _store().list_events(entity_type="work_session",
                                  entity_id=session["session_id"])
    kinds = {e["event_type"] for e in events}
    assert {"session_opened", "session_qualified", "session_closed"} <= kinds

    closed = next(e for e in events if e["event_type"] == "session_closed")
    payload = closed["payload"]
    assert payload["credited_seconds"] >= 1200
    assert payload["qualified"] is True
    assert payload["payout_cents"] == 10000


def test_an_unqualified_close_is_logged_with_its_seconds(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _run(h, clock, session, beats=10)          # 150 s
    _close(h, session["session_id"])

    closed = next(e for e in _store().list_events(
        entity_type="work_session", entity_id=session["session_id"])
        if e["event_type"] == "session_closed")
    assert closed["payload"]["credited_seconds"] == 150
    assert closed["payload"]["qualified"] is False
    assert closed["payload"]["payout_cents"] == 0
