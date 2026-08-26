"""The generalized referral program: referee bonus, cap, links, enterprise note.

test_referral_payout.py owns the referrer bounty's core guards (one bounty per
invitee ever, approval as the trigger, duplicates marked, no oracle). This file
owns what the program added on top of that spine:

  * the INVITEE's $25 first-case bonus, settled in the same pass and covered by
    the same ``UNIQUE(kind, ref_id)`` guard;
  * the $5,200 lifetime cap, read from the ledger;
  * link attribution (/join?ref=CODE) with its refusals and the same-IP flag;
  * the enterprise note endpoint;
  * the funnel's display contract (payout_structure, /join invite URL).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402
from asclepius import referrals as asc_referrals  # noqa: E402
from asclepius.store import get_store  # noqa: E402

client = TestClient(A.app)

BOUNTY = asc_payments.referral_bounty_cents()
REFEREE = asc_payments.referee_bonus_cents()
CAP = asc_payments.referral_cap_cents()


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    import ratelimit
    ratelimit.reset()
    yield


def _store():
    return get_store()


def _physician(**kw):
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (u["id"],))
    return store.get_user_by_id(u["id"])


def _refer(referrer, email, name=None):
    r = client.post("/api/asclepius/referrals",
                    json={"email": email, "name": name},
                    headers=A.headers_for(referrer))
    assert r.status_code == 200, r.text
    return r.json()


def _signup(email, *, full_name=None):
    store = _store()
    user = store.provision_user(email=email, password="pw-12345678",
                                role="evaluator", full_name=full_name)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved', "
                     "tier = 'labeler' WHERE id = ?", (user["id"],))
    store.claim_referral_for_signup(email=email, user_id=user["id"])
    return store.get_user_by_id(user["id"])


def _approved_task(user, ref):
    return _store().insert_earning(
        earning_id=f"earn-{ref}", user_id=user["id"], kind=asc_payments.KIND_TASK,
        ref_id=ref, amount_cents=7500, rate_cents=7500,
        status=asc_payments.APPROVED, accrued_at="2026-08-01T00:00:00",
        resolved_at="2026-08-02T00:00:00")


def _earnings_of(user, kind):
    return [e for e in _store().earnings_for_user(user["id"]) if e["kind"] == kind]


def _email():
    return f"invitee-{A.uniq(10)}@example.com"


def _settle_full_referral(referrer):
    """One referral driven to settlement; returns the invitee."""
    email = _email()
    _refer(referrer, email)
    invitee = _signup(email, full_name="Dr Settled")
    _approved_task(invitee, f"sub-{A.uniq(8)}")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    return invitee


# ═══ The referee's side of the settlement ════════════════════════════════════
def test_the_invitee_gets_a_first_case_bonus_when_the_bounty_settles():
    referrer = _physician()
    invitee = _settle_full_referral(referrer)

    assert [e["amount_cents"] for e in _earnings_of(referrer, asc_payments.KIND_REFERRAL)] == [BOUNTY]
    bonus = _earnings_of(invitee, asc_payments.KIND_REFEREE_BONUS)
    assert [e["amount_cents"] for e in bonus] == [REFEREE]
    assert bonus[0]["status"] == asc_payments.APPROVED


def test_the_referee_bonus_never_pays_twice():
    referrer = _physician()
    invitee = _settle_full_referral(referrer)
    # Settlement runs again, as reconciliation will, forever.
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    asc_payments.reconcile_referral_bounties(_store(), referrer_id=referrer["id"])
    assert len(_earnings_of(invitee, asc_payments.KIND_REFEREE_BONUS)) == 1


def test_no_settlement_means_no_referee_bonus():
    """A self-referral settles nothing, so neither side is paid."""
    referrer = _physician()
    store = _store()
    # The patient version: the referrer's own second address, attached later.
    email = _email()
    _refer(referrer, email)
    invitee = _signup(email)
    with store._conn() as conn:
        conn.execute("UPDATE referrals SET referrer_id = ? WHERE user_id = ?",
                     (invitee["id"], invitee["id"]))
    _approved_task(invitee, "sub-self")
    asc_payments.accrue_referral_bounty(store, referred_user_id=invitee["id"])
    assert _earnings_of(invitee, asc_payments.KIND_REFEREE_BONUS) == []


def test_first_case_at_is_stamped_on_the_settled_row():
    referrer = _physician()
    invitee = _settle_full_referral(referrer)
    rows = _store().referrals_for_invitee(invitee["id"])
    assert rows and rows[0]["first_case_at"]


# ═══ The cap ═════════════════════════════════════════════════════════════════
def test_a_referrer_at_the_cap_accrues_nothing_more(monkeypatch):
    """Cap read from the LEDGER: with lifetime bounties at the ceiling, the
    next referral settles as ineligible rather than paying or pending."""
    monkeypatch.setenv("ASCLEPIUS_REFERRAL_CAP_CENTS", str(BOUNTY * 2))
    referrer = _physician()
    _settle_full_referral(referrer)
    _settle_full_referral(referrer)
    over = _settle_full_referral(referrer)

    assert len(_earnings_of(referrer, asc_payments.KIND_REFERRAL)) == 2
    row = _store().referrals_for_invitee(over["id"])[0]
    assert row["bounty_state"] == "ineligible"


def test_the_funnel_reports_the_cap_and_the_structure():
    referrer = _physician()
    r = client.get("/api/asclepius/referrals", headers=A.headers_for(referrer))
    assert r.status_code == 200
    body = r.json()
    ps = body["payout_structure"]
    assert ps["referrer_bounty_cents"] == BOUNTY
    assert ps["referee_bonus_cents"] == REFEREE
    assert ps["cap_cents"] == CAP == 520_000
    assert body["capped"] is False
    assert "/join?ref=" in (body["invite_url"] or "")


# ═══ Link attribution ════════════════════════════════════════════════════════
def test_a_link_signup_attributes_to_the_code_holder():
    referrer = _physician()
    code = _store().ensure_referral_code(referrer["id"])
    email = _email()
    row = asc_referrals.attach_link_signup(
        _store(), referral_code=code, email=email, ip="10.0.0.1")
    assert row and row["referrer_id"] == referrer["id"]
    assert row["source"] == "link"
    # The normal provisioning claim then binds it to the account.
    invitee = _signup(email)
    assert _store().referrals_for_invitee(invitee["id"])


def test_an_unknown_code_and_a_self_referral_attach_nothing():
    referrer = _physician()
    code = _store().ensure_referral_code(referrer["id"])
    assert asc_referrals.attach_link_signup(
        _store(), referral_code="NOPE1234", email=_email()) is None
    assert asc_referrals.attach_link_signup(
        _store(), referral_code=code, email=referrer["email"]) is None


def test_a_repeat_link_signup_keeps_the_existing_row():
    referrer = _physician()
    code = _store().ensure_referral_code(referrer["id"])
    email = _email()
    first = asc_referrals.attach_link_signup(_store(), referral_code=code, email=email)
    second = asc_referrals.attach_link_signup(_store(), referral_code=code, email=email)
    assert first["referral_id"] == second["referral_id"]


def test_a_second_signup_from_the_same_ip_is_flagged_not_blocked():
    referrer = _physician()
    code = _store().ensure_referral_code(referrer["id"])
    a = asc_referrals.attach_link_signup(
        _store(), referral_code=code, email=_email(), ip="10.9.9.9")
    b = asc_referrals.attach_link_signup(
        _store(), referral_code=code, email=_email(), ip="10.9.9.9")
    assert a["fraud_flag"] is None
    assert _store().get_referral(b["referral_id"])["fraud_flag"] == "same_ip"


# ═══ The enterprise note ═════════════════════════════════════════════════════
def test_an_enterprise_note_reaches_the_founder_inbox(capsys):
    referrer = _physician()
    r = client.post("/api/asclepius/referrals/enterprise-note",
                    json={"note": "Our health system is exploring a data partnership."},
                    headers=A.headers_for(referrer))
    assert r.status_code == 200, r.text
    out = capsys.readouterr().out
    assert "aryaabhatia@berkeley.edu" in out
    assert "data partnership" in out


def test_an_empty_note_is_refused():
    referrer = _physician()
    r = client.post("/api/asclepius/referrals/enterprise-note",
                    json={"note": "   "}, headers=A.headers_for(referrer))
    assert r.status_code == 422


def test_a_physician_awaiting_verification_can_send_the_enterprise_note():
    """The doctor who can open an institutional door is often the one who just
    joined through it. Waiting for the credential check to pass before letting
    them say so loses the introduction, and the note goes to a founder who
    reads it either way."""
    pending = A.make_user(_store(), role="evaluator", specialty="nephrology")
    with _store()._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending' WHERE id = ?",
                     (pending["id"],))
    r = client.post("/api/asclepius/referrals/enterprise-note",
                    json={"note": "hello"},
                    headers=A.headers_for(_store().get_user_by_id(pending["id"])))
    assert r.status_code == 200
