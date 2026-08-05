"""PRD-P — a beat must carry what the reviewer is working on (audit C2).

The finding: ``progress_key`` was accepted, threaded through, written to the
database, and read by nothing. Credit was a function of elapsed wall time with a
beat attached and nothing else, so 28 blind HTTP requests 45 seconds apart were
worth $100.

**Read this before changing anything here.** These tests pin a control that is
DELIBERATELY NOT a fraud detector, because the cost of a false positive is
refusing to pay a physician $100. What they enforce is a *contract*: every beat
names the work it is a beat for, the session records how many distinct pieces of
work it saw, and a session that qualifies on a single key with machine-perfect
timing raises a reviewable event. What they do NOT enforce — on purpose — is a
minimum number of distinct keys above 1, because a reviewer who spends twenty
honest minutes on one genuinely hard case is the exact person this feature
exists to pay.

Closing C2 completely needs something this module cannot do alone: the key has to
be one the SERVER issued, and only PRD-R knows what a review pair is. PRD-P §8 is
explicit that if payments finds itself needing to know what a pair is, the
boundary has slipped. So this is the half that lives here — the contract, the
record, and the signal — and the verification half is a seam addition with R.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import capabilities as caps  # noqa: E402
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
        return self.t


def _open(h):
    r = client.post("/api/asclepius/sessions", json={"kind": "review"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _beat(h, sid, nonce, seq, key="pair-1", active=True, expect=200):
    body = {"nonce": nonce, "seq": seq, "active": active}
    if key is not None:
        body["progress_key"] = key
    r = client.post(f"/api/asclepius/sessions/{sid}/heartbeat", json=body, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def _work(h, session, clock, *, beats, interval=15, keys=("pair-1",)):
    """Beat through the API, rotating through ``keys`` so a multi-case session can
    be expressed as easily as a single-case one."""
    sid, nonce, seq = session["session_id"], session["nonce"], 0
    last = None
    for i in range(beats + 1):
        if i:
            clock.advance(interval)
        seq += 1
        last = _beat(h, sid, nonce, seq, key=keys[i % len(keys)])
        nonce = last["next_nonce"]
    return last


# ─── The contract ─────────────────────────────────────────────────────────────
def test_a_beat_without_a_progress_key_is_refused(monkeypatch):
    """THE hole, at its narrowest: a beat that does not say what it is a beat for
    is not evidence of anything."""
    Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _beat(h, session["session_id"], session["nonce"], 1, key=None, expect=422)


def test_an_empty_or_whitespace_progress_key_is_refused(monkeypatch):
    Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid, nonce = session["session_id"], session["nonce"]
    _beat(h, sid, nonce, 1, key="", expect=422)
    _beat(h, sid, nonce, 1, key="   ", expect=422)
    # Nothing landed, so nothing is credited.
    assert _store().session_beats(sid) == []


def test_a_refused_beat_credits_nothing_and_does_not_advance_the_session(monkeypatch):
    """The audit's 28-request bot, run against the fixed server."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    sid, nonce = session["session_id"], session["nonce"]

    for seq in range(1, 29):
        _beat(h, sid, nonce, seq, key=None, expect=422)
        clock.advance(45)

    row = _store().get_work_session(sid)
    assert row["credited_seconds"] == 0
    assert row["continuous_seconds"] == 0
    assert _store().session_beats(sid) == []
    assert client.get("/api/asclepius/earnings", headers=h).json()["approved_cents"] == 0


def test_the_key_is_recorded_on_every_beat(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _work(h, session, clock, beats=3, keys=("pair-a",))

    keys = [b["progress_key"] for b in _store().session_beats(session["session_id"])]
    assert keys == ["pair-a"] * 4


# ─── What is counted, and what is NOT enforced ────────────────────────────────
def test_twenty_honest_minutes_on_one_hard_case_is_still_paid(monkeypatch):
    """The false-positive case, asserted first and deliberately.

    A reviewer who spends the whole session adjudicating a single genuinely hard
    pair produces exactly ONE distinct progress key. Any rule that demanded more
    would refuse to pay the physician this feature exists to pay. If a future
    change makes this test fail, the change is wrong."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    last = _work(h, session, clock, beats=80, keys=("pair-only-one",))

    assert last["credited_seconds"] == 1200
    assert last["qualified"] is True

    r = client.post(f"/api/asclepius/sessions/{session['session_id']}/close",
                    json={"reason": "closed"}, headers=h)
    assert r.json()["payout_cents"] == 10000

    row = _store().get_work_session(session["session_id"])
    assert row["distinct_progress_keys"] == 1


def test_distinct_keys_are_counted_across_a_multi_case_session(monkeypatch):
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _work(h, session, clock, beats=80, keys=("pair-a", "pair-b", "pair-c", "pair-d"))

    row = _store().get_work_session(session["session_id"])
    assert row["distinct_progress_keys"] == 4


def test_the_minimum_distinct_key_policy_exists_and_defaults_to_one(monkeypatch):
    """The policy hook, so raising the bar later is an env change rather than a
    migration — and so the decision is made from recorded data rather than from a
    guess. It defaults to 1, which by construction changes nothing."""
    assert asc_payments.tr_min_progress_keys() == 1

    monkeypatch.setenv("ASCLEPIUS_TR_MIN_PROGRESS_KEYS", "3")
    assert asc_payments.tr_min_progress_keys() == 3

    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    last = _work(h, session, clock, beats=80, keys=("pair-only-one",))
    # Time was worked and is recorded in full — the threshold is about what the
    # time is worth, never about pretending it did not happen.
    assert last["credited_seconds"] == 1200
    assert last["qualified"] is False

    r = client.post(f"/api/asclepius/sessions/{session['session_id']}/close",
                    json={"reason": "closed"}, headers=h)
    assert r.json()["payout_cents"] == 0
    assert _store().get_work_session(session["session_id"])["credited_seconds"] == 1200


def test_a_single_key_cannot_accrue_indefinitely(monkeypatch):
    """A reviewer who never moves off one case stops accruing at the per-key
    ceiling. Set well above the 20-minute threshold, so it can never cost an
    honest reviewer their session — it bounds an unattended overnight run, not a
    hard case."""
    assert asc_payments.progress_key_max_seconds() > asc_payments.tr_min_seconds()

    clock = Clock(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_PROGRESS_KEY_MAX_SECONDS", "300")
    h = A.headers_for(_reviewer())
    session = _open(h)
    last = _work(h, session, clock, beats=60, keys=("pair-forever",))   # 900 s of beats

    assert last["credited_seconds"] == 300
    assert last["qualified"] is False


def test_moving_to_a_new_case_resets_the_per_key_ceiling(monkeypatch):
    clock = Clock(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_PROGRESS_KEY_MAX_SECONDS", "300")
    h = A.headers_for(_reviewer())
    session = _open(h)
    last = _work(h, session, clock, beats=60, keys=("pair-a", "pair-b", "pair-c"))

    # Three cases, each inside its own ceiling, so the whole run is credited.
    assert last["credited_seconds"] == 900
    assert _store().get_work_session(session["session_id"])["distinct_progress_keys"] == 3


# ─── The signal ───────────────────────────────────────────────────────────────
def test_a_metronome_session_on_one_key_raises_a_reviewable_event(monkeypatch):
    """Neither signal is worth acting on alone — humans hold one hard case, and a
    clean network can look regular. TOGETHER they describe something no browser
    produces, and the answer is still to pay and flag rather than to refuse."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)
    _work(h, session, clock, beats=80, keys=("pair-only-one",))
    result = client.post(f"/api/asclepius/sessions/{session['session_id']}/close",
                         json={"reason": "closed"}, headers=h).json()

    # Paid. The flag is for a human to look at, not a gate.
    assert result["payout_cents"] == 10000

    events = _store().list_events(entity_type="work_session",
                                  entity_id=session["session_id"])
    flagged = [e for e in events if e["event_type"] == "session_low_confidence"]
    assert len(flagged) == 1
    payload = flagged[0]["payload"]
    assert payload["distinct_progress_keys"] == 1
    assert payload["jitter_ms"] == 0.0
    assert payload["payout_cents"] == 10000
    assert "single_key" in payload["reasons"] and "no_jitter" in payload["reasons"]


def test_an_ordinary_multi_case_session_is_not_flagged(monkeypatch):
    """The negative case, so the signal stays worth reading. A flag that fires on
    every session is a flag nobody looks at."""
    clock = Clock(monkeypatch)
    h = A.headers_for(_reviewer())
    session = _open(h)

    sid, nonce, seq = session["session_id"], session["nonce"], 0
    keys = ("pair-a", "pair-b", "pair-c")
    for i in range(81):
        if i:
            clock.advance(15 + (i % 4))       # human-ish jitter
        seq += 1
        r = _beat(h, sid, nonce, seq, key=keys[i % 3])
        nonce = r["next_nonce"]
    client.post(f"/api/asclepius/sessions/{sid}/close", json={"reason": "closed"}, headers=h)

    events = _store().list_events(entity_type="work_session", entity_id=sid)
    assert [e for e in events if e["event_type"] == "session_low_confidence"] == []


# ─── The boundary this fix must not cross ─────────────────────────────────────
def _payments_code() -> str:
    """payments.py with comments and docstrings stripped.

    The grep below is about what the module DOES. Its docstrings explain, at
    length, which parts of PRD-R's world it deliberately refuses to touch — and a
    naive grep over the raw file reads those explanations as violations, which
    would push a future maintainer toward deleting the reasoning to keep the test
    green. That is the opposite of what this test is for."""
    import io
    import tokenize

    source = (Path(__file__).resolve().parents[1] / "asclepius" / "payments.py").read_text(
        encoding="utf-8")
    out, prev_end, prev_type = [], (1, 0), tokenize.INDENT
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        # A bare string statement is a docstring; a string in an expression is a
        # value the code actually uses and must stay visible to the grep.
        if tok.type == tokenize.STRING and prev_type in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            continue
        out.append(tok.string)
        prev_type = tok.type if tok.type != tokenize.NL else prev_type
    return " ".join(out)


def test_payments_still_does_not_know_what_a_review_pair_is():
    """PRD-P §8. The progress key is an OPAQUE string to this module: it is
    counted, capped and recorded, and never parsed, resolved or validated against
    review state. The moment payments looks up what a key means, it has taken a
    dependency on R's schema and the seam is gone."""
    code = _payments_code()
    for banned in ("case_reviews", "review_pair", "import review",
                   "asclepius.review", "asclepius.routing"):
        assert banned not in code, f"payments.py reaches into review state: {banned}"
