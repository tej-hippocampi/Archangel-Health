"""PRD-P — the rotating nonce actually has to rotate (audit H1).

The claim the design rested on was that "a naive ``setInterval(fetch)`` cannot
beat without parsing every response." It could. ``open_session`` is idempotent and
returned the **live** nonce, so a bot that never read a heartbeat response could
re-POST ``/sessions`` before each beat and collect a working token every time. The
read-only session route withheld the nonce behind a five-line comment explaining
why — and the open route handed it out on the next line.

Two changes, and they are one idea: **the nonce is a beating credential, and
getting one has to cost something.**

  * ``open_session`` returns a nonce only when it CREATED the session. A resumed
    open returns state without a credential.
  * Resuming is its own endpoint, rate-limited an order of magnitude harder than
    beating and counted on the session row, because one or two resumes is a page
    reload and thirty is a script.

Plus the small hole beside it: ``seq`` was optional, and a client that omitted it
had the server derive ``MAX(seq)+1`` — which is exactly what a replayer wants. It
is now required as soon as the session has any beats, which is the first moment
there is anything to replay.
"""

from __future__ import annotations

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


def _resume(h, sid, expect=200):
    r = client.post(f"/api/asclepius/sessions/{sid}/resume", json={}, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def _beat(h, sid, nonce, seq, expect=200, key=_KEY):
    body = {"nonce": nonce, "active": True, "progress_key": key}
    if seq is not None:
        body["seq"] = seq
    r = client.post(f"/api/asclepius/sessions/{sid}/heartbeat", json=body, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


# ─── The credential ───────────────────────────────────────────────────────────
def test_a_new_open_returns_a_nonce():
    """The positive case: a client that just created a session can beat."""
    h = A.headers_for(_reviewer())
    session = _open(h)
    assert session["resumed"] is False
    assert session["nonce"]


def test_a_resumed_open_returns_no_nonce(monkeypatch):
    """THE hole. Re-opening was a free, unlimited nonce dispenser for a client
    that never read a heartbeat response."""
    Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    first = _open(h)
    again = _open(h)

    assert again["session_id"] == first["session_id"]
    assert again["resumed"] is True
    assert again.get("nonce") is None


def test_reopening_before_every_beat_no_longer_earns(monkeypatch):
    """The audit's bot, run against the fixed server: never read a heartbeat
    response, re-POST /sessions before each beat, collect $100.

    It now collects nothing. Creating a session issues exactly one credential, and
    the only way to get the next one is out of the heartbeat response the bot
    refuses to read — so it can never string two credited beats together, and
    credited time is the sum of gaps BETWEEN beats."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    _beat(h, sid, session["nonce"], 1)           # the one beat it gets for free
    for seq in range(2, 20):                     # inside the abandon window
        clock.advance(45)
        reopened = _open(h)
        assert reopened["session_id"] == sid     # same session, and...
        assert reopened.get("nonce") is None     # ...nothing to beat with
        _beat(h, sid, "guessed-nonce", seq, expect=409)

    assert _store().get_work_session(sid)["credited_seconds"] == 0

    # Letting the session lapse and taking a fresh one does not help either: each
    # new session is a new credential and a new clock starting at zero.
    for _ in range(4):
        clock.advance(asc_payments.session_abandon_seconds() + 60)
        fresh = _open(h)
        assert fresh["nonce"] and fresh["credited_seconds"] == 0
        _beat(h, fresh["session_id"], fresh["nonce"], 1)

    assert client.get("/api/asclepius/earnings", headers=h).json()["approved_cents"] == 0


# ─── Resuming, for the clients that legitimately need to ──────────────────────
def test_a_reloaded_page_can_resume_and_keep_beating(monkeypatch):
    """The legitimate case this must not break: a physician reloads mid-session.
    The client has lost its nonce and its sequence, and must be able to carry on
    without losing the time already earned."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid, nonce, seq = session["session_id"], session["nonce"], 0

    for i in range(21):                           # 20 gaps x 15 s = 300 s
        if i:
            clock.advance(15)
        seq += 1
        r = _beat(h, sid, nonce, seq)
        nonce = r["next_nonce"]
    assert r["credited_seconds"] == 300

    # The reload: the tab is gone and with it the nonce and the sequence.
    resumed = _resume(h, sid)
    assert resumed["nonce"] and resumed["nonce"] != nonce
    assert resumed["seq"] == seq                  # where the sequence got to
    assert resumed["continuous_seconds"] == 300   # nothing was lost

    # ...and the reload itself is ordinary elapsed time, credited like any gap.
    clock.advance(15)
    after = _beat(h, sid, resumed["nonce"], resumed["seq"] + 1)
    assert after["credited_seconds"] == 315


def test_the_old_nonce_is_dead_after_a_resume(monkeypatch):
    """Resuming rotates. Two tabs cannot both hold a beating credential — which is
    the whole reason the nonce rotates on every beat in the first place."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    r = _beat(h, sid, session["nonce"], 1)
    clock.advance(15)

    resumed = _resume(h, sid)
    _beat(h, sid, r["next_nonce"], 2, expect=409)          # the pre-resume nonce
    _beat(h, sid, resumed["nonce"], 2)                     # the current one


def test_every_resume_is_counted_on_the_session(monkeypatch):
    """One or two resumes is a page reload. Thirty is a script, and the number is
    on the row where a payout review will look for it."""
    Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    sid = _open(h)["session_id"]
    for _ in range(3):
        _resume(h, sid)
    assert _store().get_work_session(sid)["resume_count"] == 3


def test_a_resume_credits_no_time(monkeypatch):
    """Resuming is not beating. It hands over a credential and nothing else."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    r = _beat(h, sid, session["nonce"], 1)
    clock.advance(15)
    _beat(h, sid, r["next_nonce"], 2)

    before = _store().get_work_session(sid)["credited_seconds"]
    clock.advance(600)
    for _ in range(5):
        _resume(h, sid)
    assert _store().get_work_session(sid)["credited_seconds"] == before


def test_resuming_is_scoped_to_the_owner_and_to_an_open_session(monkeypatch):
    clock = Clock(monkeypatch)
    owner_h = A.headers_for(_reviewer())
    other_h = A.headers_for(_reviewer())
    session = _open(owner_h)
    sid = session["session_id"]

    _resume(other_h, sid, expect=404)
    _resume(other_h, "ws-nope", expect=404)

    client.post(f"/api/asclepius/sessions/{sid}/close",
                json={"reason": "closed"}, headers=owner_h)
    # A closed session cannot be handed a live nonce — that would reopen it for
    # beating after it had already settled.
    _resume(owner_h, sid, expect=409)


def test_resume_is_rate_limited_far_harder_than_beating():
    """Beating is legitimate high-frequency traffic; resuming is not. The audit's
    bot needed roughly 1.3 opens a minute, which has to sit well outside the
    budget for a credential hand-out."""
    from routers import asclepius_payments as router_mod

    limits = router_mod.RESUME_RATE_LIMIT
    beats = router_mod.HEARTBEAT_RATE_LIMIT
    per_minute_resume = limits[0] / (limits[1] / 60.0)
    per_minute_beat = beats[0] / (beats[1] / 60.0)
    assert per_minute_resume < 5, "a resume budget this generous is a nonce dispenser"
    assert per_minute_beat > per_minute_resume * 10


# ─── The optional-seq hole beside it ──────────────────────────────────────────
def test_the_first_beat_may_omit_seq_but_no_later_one_may(monkeypatch):
    """A client with no beats behind it has nothing to replay, so the server
    numbering its first beat is safe and keeps a fresh open simple. From the
    second beat on there IS something to replay, and the sequence stops being
    optional."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]

    first = _beat(h, sid, session["nonce"], None)      # allowed: no beats yet
    assert first["seq"] == 1
    clock.advance(15)

    _beat(h, sid, first["next_nonce"], None, expect=422)
    # ...and with the sequence, the same beat is fine.
    second = _beat(h, sid, first["next_nonce"], 2)
    assert second["credited_seconds"] == 15


def test_a_seq_that_does_not_advance_is_still_refused(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    r = _beat(h, sid, session["nonce"], 1)
    clock.advance(15)
    r2 = _beat(h, sid, r["next_nonce"], 2)
    clock.advance(15)
    _beat(h, sid, r2["next_nonce"], 2, expect=409)
    _beat(h, sid, r2["next_nonce"], 1, expect=409)


# ─── The claim, restated honestly ─────────────────────────────────────────────
def test_the_only_source_of_a_beating_credential_is_a_response_body(monkeypatch):
    """Three routes can return a nonce, and each costs something: creating a
    session (once), beating (which requires already holding one), and resuming
    (rate-limited and counted). A client that never parses a response body cannot
    obtain one at all."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid = session["session_id"]
    r = _beat(h, sid, session["nonce"], 1)
    clock.advance(15)

    assert _open(h).get("nonce") is None
    assert "nonce" not in client.get(f"/api/asclepius/sessions/{sid}", headers=h).json()
    assert r["next_nonce"]
    assert _resume(h, sid)["nonce"]
