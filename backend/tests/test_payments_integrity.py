"""PRD-P — an infrastructure event must not cost a physician $100 (audit H3).

Measured on an otherwise perfect 1,200-second session:

    perturbation                          credited  continuous  qualified
    clean                                     1200        1200       True
    39 unparseable server_ts                   600         600      False
    one beat stamped a year earlier            1170         585      False

The backward case is the nastier one. ``gap < 0 -> 0.0`` protects the first edge,
and then the NEXT gap is a year, exceeds MAX_GAP, and breaks the run. An NTP step
or a VM migration mid-session silently converts a qualifying session to $0, and
``_check_clock_skew`` detects exactly that and only logs it.

The fix is a ratchet: **credited and continuous seconds may never fall below what
was already persisted.** That is safe rather than generous — the stored value was
itself computed from a strict SUBSET of the same beats, so the floor can only
preserve a number that was already earned once, never invent one. And when the
floor actually binds, that is an infrastructure fault worth a human look, so it
raises a payout-review event rather than passing quietly.

The asymmetry is the point. A recomputation that goes UP is ordinary — the
reviewer kept working. A recomputation that goes DOWN means the record changed
underneath a number somebody was already shown, and the physician should not be
the one who absorbs that.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402

client = TestClient(A.app)
_T0 = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)
_KEY = "pair-under-test"


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    asc_payments._MONOTONIC_REF.clear()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _reviewer():
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier = 'reviewer', verification_status = 'approved' "
                     "WHERE id = ?", (u["id"],))
    return store.get_user_by_id(u["id"])


class Clock:
    def __init__(self, monkeypatch, start=_T0):
        self.t = start
        monkeypatch.setattr(asc_payments, "_now", lambda: self.t)

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


def _open(h):
    r = client.post("/api/asclepius/sessions", json={"kind": "review"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _beat(h, sid, nonce, seq, expect=200):
    r = client.post(f"/api/asclepius/sessions/{sid}/heartbeat",
                    json={"nonce": nonce, "seq": seq, "active": True,
                          "progress_key": _KEY}, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def _work(h, session, clock, beats):
    sid, nonce, seq = session["session_id"], session["nonce"], 0
    last = None
    for i in range(beats + 1):
        if i:
            clock.advance(15)
        seq += 1
        last = _beat(h, sid, nonce, seq)
        nonce = last["next_nonce"]
    return last, nonce, seq


def _corrupt(sid, *, sql, params=()):
    """Reach into the beat rows the way an infrastructure event would — a clock
    step or a storage fault — rather than through any API."""
    conn = sqlite3.connect(_store().db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ─── The ratchet ──────────────────────────────────────────────────────────────
def test_a_backward_clock_step_cannot_take_back_a_qualified_session(monkeypatch):
    """The audit's NTP-step case, end to end. The session qualified; a clock event
    afterwards must not un-qualify it."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    last, nonce, seq = _work(h, session, clock, 80)
    assert last["credited_seconds"] == 1200 and last["qualified"] is True

    # A beat in the middle of the run is suddenly stamped a year earlier.
    _corrupt(sid, sql="UPDATE session_beats SET server_ts = ? "
                      "WHERE session_id = ? AND seq = 40",
             params=("2025-08-03T14:10:00.000000", sid))

    result = client.post(f"/api/asclepius/sessions/{sid}/close",
                         json={"reason": "closed"}, headers=h).json()
    assert result["qualified"] is True
    assert result["continuous_seconds"] >= 1200
    assert result["payout_cents"] == 10000


def test_unreadable_beats_cannot_reduce_a_running_session(monkeypatch):
    """The other measured case: rows whose timestamps stop parsing. Half the
    session's evidence disappearing must not halve what the reviewer is owed for
    time the server already counted."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    last, nonce, seq = _work(h, session, clock, 80)
    assert last["credited_seconds"] == 1200

    _corrupt(sid, sql="UPDATE session_beats SET server_ts = 'not-a-timestamp' "
                      "WHERE session_id = ? AND seq <= 40", params=(sid,))

    clock.advance(15)
    after = _beat(h, sid, nonce, seq + 1)
    assert after["credited_seconds"] >= 1200
    assert after["continuous_seconds"] >= 1200
    assert after["qualified"] is True


def test_the_ratchet_still_lets_a_session_grow(monkeypatch):
    """A floor that also became a ceiling would freeze every session at its first
    reading. Going UP is the ordinary case and must stay ordinary."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    last, nonce, seq = _work(h, session, clock, 20)
    assert last["credited_seconds"] == 300

    for _ in range(20):
        clock.advance(15)
        seq += 1
        last = _beat(h, session["session_id"], nonce, seq)
        nonce = last["next_nonce"]
    assert last["credited_seconds"] == 600


def test_the_ratchet_does_not_invent_time_for_a_session_that_never_earned_it(monkeypatch):
    """The floor preserves what was persisted, and the persisted value came from
    the same beats. A short session stays short."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    _work(h, session, clock, 40)                      # 600 s, well under

    _corrupt(sid, sql="UPDATE session_beats SET server_ts = 'not-a-timestamp' "
                      "WHERE session_id = ? AND seq <= 20", params=(sid,))

    result = client.post(f"/api/asclepius/sessions/{sid}/close",
                         json={"reason": "closed"}, headers=h).json()
    assert result["credited_seconds"] == 600          # held, not inflated
    assert result["qualified"] is False
    assert result["payout_cents"] == 0


# ─── The record of it ─────────────────────────────────────────────────────────
def test_a_binding_ratchet_raises_a_payout_review_event(monkeypatch):
    """When the floor binds, the stored record and the recomputation disagree —
    which means something happened to the infrastructure. Paying the physician is
    the right call AND a thing a human should know about."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    _work(h, session, clock, 80)

    _corrupt(sid, sql="UPDATE session_beats SET server_ts = 'not-a-timestamp' "
                      "WHERE session_id = ? AND seq <= 40", params=(sid,))
    client.post(f"/api/asclepius/sessions/{sid}/close",
                json={"reason": "closed"}, headers=h)

    events = [e for e in _store().list_events(entity_type="work_session", entity_id=sid)
              if e["event_type"] == "session_credit_regressed"]
    assert events, "a recomputation that went backwards was not recorded"
    payload = events[0]["payload"]
    assert payload["stored_continuous_seconds"] == 1200
    assert payload["recomputed_continuous_seconds"] < 1200
    assert payload["action"] == "held_at_stored_value"


def test_a_clean_session_raises_no_such_event(monkeypatch):
    """A signal that fires on every close is a signal nobody reads."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _work(h, session, clock, 80)
    client.post(f"/api/asclepius/sessions/{session['session_id']}/close",
                json={"reason": "closed"}, headers=h)

    events = [e for e in _store().list_events(
        entity_type="work_session", entity_id=session["session_id"])
        if e["event_type"] == "session_credit_regressed"]
    assert events == []


# ─── L2 — a signal you have learned to ignore is not a signal ────────────────
def test_a_per_session_signal_is_logged_once_per_session(monkeypatch, caplog):
    """Clock skew and low jitter are facts about a SESSION, not about a beat.
    Logged per beat, modest NTP drift alone produced eighty WARNING lines a
    session — which is exactly how the one line that mattered would be missed."""
    import logging

    clock = Clock(monkeypatch)
    asc_payments._SKEW_LOGGED.clear()
    asc_payments._JITTER_LOGGED.clear()
    h = A.headers_for(_reviewer())

    with caplog.at_level(logging.WARNING, logger="asclepius.payments"):
        session = _open(h)
        _work(h, session, clock, 80)

    skew = [r for r in caplog.records if "clock skew" in r.getMessage()]
    jitter = [r for r in caplog.records if "beat jitter" in r.getMessage()]
    assert len(skew) == 1, f"{len(skew)} skew lines for one session"
    assert len(jitter) == 1, f"{len(jitter)} jitter lines for one session"

    # ...and the event log still carries the fact, so throttling the log line
    # never throttles the record.
    events = [e for e in _store().list_events(
        entity_type="work_session", entity_id=session["session_id"])
        if e["event_type"] == "clock_skew"]
    assert len(events) == 1


def test_a_second_session_gets_its_own_line(monkeypatch, caplog):
    """Throttling per session, not globally — the next session's skew is news."""
    import logging

    clock = Clock(monkeypatch)
    asc_payments._SKEW_LOGGED.clear()
    h = A.headers_for(_reviewer())

    with caplog.at_level(logging.WARNING, logger="asclepius.payments"):
        first = _open(h)
        _work(h, first, clock, 4)
        client.post(f"/api/asclepius/sessions/{first['session_id']}/close",
                    json={"reason": "closed"}, headers=h)
        second = _open(h)
        _work(h, second, clock, 4)

    assert len([r for r in caplog.records if "clock skew" in r.getMessage()]) == 2


# ─── L1, while we are in the arithmetic ───────────────────────────────────────
def test_the_threshold_is_reached_by_flooring_never_by_rounding():
    """``int(round(1199.5)) == 1200`` qualified a session 0.5 s short. The
    threshold is the legally sensitive number in this feature and must never be
    reached by a rounding rule."""
    base = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)
    beats = []
    # 80 gaps that sum to 1199.5 s — under the threshold by half a second.
    t = base
    for i in range(81):
        beats.append({"beat_id": f"b{i}", "server_ts": t.replace(tzinfo=None).isoformat(),
                      "active": 1, "progress_key": "pair-1"})
        t = t + timedelta(seconds=14.99375)
    out = asc_payments.credit_from_beats(beats, min_seconds=1200)
    assert out["continuous_seconds"] == 1199
    assert out["qualified"] is False

    # ...and one more beat of ordinary work carries it over honestly.
    beats.append({"beat_id": "bx", "server_ts": (t + timedelta(seconds=1)).replace(
        tzinfo=None).isoformat(), "active": 1, "progress_key": "pair-1"})
    assert asc_payments.credit_from_beats(beats, min_seconds=1200)["qualified"] is True
