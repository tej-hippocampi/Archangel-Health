"""PRD-P — who may open a billable session (audit C1).

The audit's finding, in one sentence: opening a $100-per-20-minutes session had
no tier, capability or verification gate on it, so any authenticated account
could earn — including a labeler who is not a reviewer at all.

This file is the negative-authz suite the original session tests never had.
Every test in ``test_payments_session.py`` opens as a generic evaluator, which is
exactly why the hole survived a green suite: the tests only ever asked "can the
person who should be able to?" and never "can the person who should not?".

Two gates, deliberately separate, because they fail for different reasons and a
physician deserves to be told which one they hit:

  * REVIEW capability — read through ``capabilities.can`` and never off
    ``users.tier`` directly (context pack §3.3). A labeler doing labeling work is
    not doing review work.
  * verification_status == 'approved' — an account an admin refused is not on the
    payroll. ``auth.get_current_user`` already blocks pending/rejected across the
    whole evaluator surface; the check is repeated at the money boundary because
    a payment gate should not be one refactor of somebody else's dependency away
    from opening.
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


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    asc_payments._MONOTONIC_REF.clear()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _user(*, tier=None, verification=None, role="evaluator"):
    store = _store()
    u = A.make_user(store, role=role, specialty="nephrology")
    sets, params = [], []
    if tier is not None:
        sets.append("tier = ?")
        params.append(tier)
    if verification is not None:
        sets.append("verification_status = ?")
        params.append(verification)
    if sets:
        with store._conn() as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?",
                         (*params, u["id"]))
    return store.get_user_by_id(u["id"])


def _open(user, expect=200):
    r = client.post("/api/asclepius/sessions", json={"kind": "review"},
                    headers=A.headers_for(user))
    assert r.status_code == expect, r.text
    return r


# ─── The gate ─────────────────────────────────────────────────────────────────
def test_a_reviewer_can_open_a_billable_session():
    """The positive case, first — a gate that denies everyone is not a gate."""
    reviewer = _user(tier=caps.REVIEWER, verification="approved")
    assert caps.can(reviewer, caps.REVIEW) is True
    body = _open(reviewer).json()
    assert body["session_id"]
    assert body["min_seconds"] == 1200


def test_an_advisor_can_open_a_billable_session():
    """An advisor holds REVIEW. They earn nothing (compensation.accrues_payment
    decides that, elsewhere) but they are entitled to the surface and to the
    countdown — being unpaid is not the same as being unauthorized."""
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    advisor = store.appoint_advisor(u["id"], agreement_ref="AGR-1",
                                    appointed_by="admin@asclepius.test")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (advisor["id"],))
    _open(store.get_user_by_id(advisor["id"]))


def test_a_labeler_cannot_open_a_billable_session():
    """THE hole. A labeler is a real, approved, working physician who simply does
    not do review work — and could earn $100 per 20 minutes on a surface they
    have no business being on."""
    labeler = _user(tier=caps.LABELER, verification="approved")
    assert caps.can(labeler, caps.REVIEW) is False
    r = _open(labeler, expect=403)
    assert "review" in r.json()["detail"].lower()


def test_an_unassigned_tier_cannot_open_a_billable_session():
    """A brand-new signup. NULL tier is 'not yet decided', which is not a grant —
    the same three-state rule the rest of this codebase holds."""
    _open(_user(tier=None, verification="approved"), expect=403)


def test_a_qa_reviewer_cannot_open_a_billable_session():
    """``qa_reviewer`` is an internal ops role, not a physician tier. It reads as
    'reviewer' to the eye and holds no REVIEW capability, which is exactly the
    kind of near-miss a tier-string check would have got wrong."""
    qa = _user(tier=None, verification="approved", role="qa_reviewer")
    assert caps.can(qa, caps.REVIEW) is False
    _open(qa, expect=403)


def test_a_rejected_physician_cannot_open_a_billable_session():
    """The audit's headline case: an account an admin explicitly refused —
    possibly for a name/NPI mismatch flagged as suspected impersonation — must
    not be on the payroll."""
    rejected = _user(tier=caps.REVIEWER, verification="rejected")
    _open(rejected, expect=403)


def test_a_physician_awaiting_verification_cannot_open_a_billable_session():
    _open(_user(tier=caps.REVIEWER, verification="pending"), expect=403)


def test_the_verification_gate_holds_inside_open_session_itself():
    """``auth.get_current_user`` already refuses pending/rejected across the whole
    evaluator surface, so the route-level cases above pass through it rather than
    through the payments gate. That is defence in depth working — but it means
    those tests do NOT prove the money boundary has its own opinion.

    This one does: it calls ``payments.open_session`` directly, which is the entry
    point PRD-R's review surface uses (context pack §3.1) and which therefore does
    not go through ``get_current_user`` at all. Without a gate HERE, R calling
    open_session for a rejected physician would mint a billable session."""
    rejected = _user(tier=caps.REVIEWER, verification="rejected")
    with pytest.raises(asc_payments.PaymentsDenied):
        asc_payments.open_session(_store(), user_id=rejected["id"], kind="review")

    labeler = _user(tier=caps.LABELER, verification="approved")
    with pytest.raises(asc_payments.PaymentsDenied):
        asc_payments.open_session(_store(), user_id=labeler["id"], kind="review")

    # And a legitimate reviewer still gets a session from the same call.
    reviewer = _user(tier=caps.REVIEWER, verification="approved")
    assert asc_payments.open_session(
        _store(), user_id=reviewer["id"], kind="review")["session_id"]


def test_an_unknown_user_id_is_denied_rather_than_minting_a_session():
    with pytest.raises(asc_payments.PaymentsDenied):
        asc_payments.open_session(_store(), user_id="u-does-not-exist", kind="review")


def test_a_deactivated_reviewer_cannot_open_a_billable_session():
    """Deactivation is how an account is taken off the payroll operationally. It
    must reach the money boundary too."""
    reviewer = _user(tier=caps.REVIEWER, verification="approved")
    with _store()._conn() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (reviewer["id"],))
    with pytest.raises(asc_payments.PaymentsDenied):
        asc_payments.open_session(_store(), user_id=reviewer["id"], kind="review")


def test_the_gate_reads_the_tier_through_capabilities_not_off_the_column():
    """Context pack §3.3: no agent reads ``users.tier`` directly. A literal tier
    comparison is the exact defect ``capabilities.py`` exists to have removed, and
    it fails SILENTLY — 'this user is not a reviewer' is a legitimate answer for a
    labeler, so nothing logs and nobody finds out."""
    source = (Path(__file__).resolve().parents[1] / "asclepius" / "payments.py").read_text(
        encoding="utf-8")
    assert "capabilities.can(" in source or "_caps.can(" in source
    for banned in ('tier") == "reviewer"', "tier'] == 'reviewer'",
                   'get("tier") ==', 'user["tier"]'):
        assert banned not in source, f"payments.py compares users.tier directly: {banned}"


# ─── The exposure the gate closes ─────────────────────────────────────────────
def test_an_ungated_account_cannot_reach_the_twenty_minute_payout():
    """End to end, in the audit's own terms: 28 heartbeats from an account that
    should not have one must produce $0 and no session at all."""
    labeler = _user(tier=caps.LABELER, verification="approved")
    h = A.headers_for(labeler)

    assert client.post("/api/asclepius/sessions", json={"kind": "review"},
                       headers=h).status_code == 403
    # There is nothing to beat on, and nothing was created to beat on.
    assert _store().list_open_work_sessions(user_id=labeler["id"], kind="review") == []
    assert client.get("/api/asclepius/earnings", headers=h).json()["approved_cents"] == 0


def test_a_denied_open_is_logged_so_the_attempt_is_visible():
    """A refusal at the money boundary is worth a record. If a labeler's client is
    calling this, that is a bug in someone's code; if it is not a client, it is
    worth knowing about."""
    labeler = _user(tier=caps.LABELER, verification="approved")
    _open(labeler, expect=403)
    events = _store().list_events(entity_type="work_session")
    denied = [e for e in events if e["event_type"] == "session_open_denied"]
    assert len(denied) == 1
    assert denied[0]["actor"] == labeler["id"]
    assert denied[0]["payload"]["reason"] == "capability"
