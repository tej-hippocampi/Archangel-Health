"""The health-system payouts ledger, portal side and admin side.

Two properties carry the weight. One organization must never see another's
ledger, which is a fact about the ROUTE (it takes no identifier) rather than a
check somebody wrote. And this table must never grow a column that would make it
a place bank details live.
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

PASSWORD = "harbor-thistle-meadow-41"
ADMIN_BASE = "/api/asclepius/admin/health-systems"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    monkeypatch.setenv("ENV", "test")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _partner(name=None):
    """An approved health system with a signed-in portal session."""
    store = _store()
    uname = "hs" + uuid.uuid4().hex[:10]
    hs = store.create_health_system_unclaimed(name or ("Payout Test " + uname))
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"], password=PASSWORD,
                                must_reset=False, approval_status="approved")
    client = TestClient(A.app, base_url="https://testserver")
    client.post("/api/asclepius/hs/login", json={"username": uname, "password": PASSWORD})
    return client, hs


def _admin_client():
    admin = A.make_user(_store(), role="admin")
    return TestClient(A.app), A.headers_for(admin)


# ─── The empty state is the honest state ─────────────────────────────────────

def test_a_partner_with_no_payments_sees_zero_and_an_explanation():
    """Nothing accrues to a health system automatically: there is no schedule and
    no payment rail. The empty state has to say so rather than implying a ledger
    that fills itself, and this is the copy most likely to get softened later."""
    client, _ = _partner()
    r = client.get("/api/asclepius/hs/payouts")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {"total_cents": 0, "paid_cents": 0,
                               "pending_cents": 0, "count": 0}
    assert body["payouts"] == []
    note = body["how_we_pay"].lower()
    assert note, "the empty state explains nothing"
    assert "bank" in note and "tax" in note


# ─── Isolation ───────────────────────────────────────────────────────────────

def test_each_organization_sees_only_its_own_ledger():
    a_client, a_hs = _partner("Alpha Health")
    b_client, b_hs = _partner("Beta Health")
    admin, headers = _admin_client()
    admin.post(f"{ADMIN_BASE}/{a_hs['hs_id']}/payouts", headers=headers,
               json={"amount_cents": 250000, "external_ref": "INV-A",
                     "description": "Alpha Q3 license"})
    admin.post(f"{ADMIN_BASE}/{b_hs['hs_id']}/payouts", headers=headers,
               json={"amount_cents": 990000, "external_ref": "INV-B",
                     "description": "Beta Q3 license"})

    a = a_client.get("/api/asclepius/hs/payouts").json()
    b = b_client.get("/api/asclepius/hs/payouts").json()
    assert a["summary"]["total_cents"] == 250000
    assert b["summary"]["total_cents"] == 990000
    assert [p["description"] for p in a["payouts"]] == ["Alpha Q3 license"]
    assert "Beta" not in a_client.get("/api/asclepius/hs/payouts").text


def test_no_payout_route_on_the_portal_accepts_an_identifier():
    """The isolation above is a property of the route surface, not of a check.
    If a /hs/payouts/{hs_id} ever appears, the check becomes the only thing
    standing between two hospitals' ledgers."""
    paths = [r.path for r in A.app.routes
             if getattr(r, "path", "").startswith("/api/asclepius/hs/payouts")]
    assert paths, "the payouts route disappeared"
    for path in paths:
        assert "{" not in path, f"{path} takes a parameter"


# ─── Recording ───────────────────────────────────────────────────────────────

def test_the_same_reference_cannot_be_recorded_twice():
    """A double-clicked Record button must not pay a hospital twice."""
    _, hs = _partner()
    admin, headers = _admin_client()
    first = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                       json={"amount_cents": 100000, "external_ref": "INV-1"})
    second = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                        json={"amount_cents": 100000, "external_ref": "INV-1"})
    assert first.status_code == 200
    assert second.status_code == 409
    assert _store().hs_payout_summary(hs["hs_id"])["count"] == 1


def test_a_non_positive_amount_is_refused():
    _, hs = _partner()
    admin, headers = _admin_client()
    for amount in (0, -5000):
        r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                       json={"amount_cents": amount, "external_ref": f"INV-{amount}"})
        assert r.status_code == 400


def test_marking_paid_stamps_when_and_void_drops_it_from_the_total():
    client, hs = _partner()
    admin, headers = _admin_client()
    keep = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                      json={"amount_cents": 250000, "external_ref": "INV-KEEP"}).json()
    drop = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                      json={"amount_cents": 100000, "external_ref": "INV-DROP"}).json()

    paid = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts/{keep['payout_id']}/mark-paid",
                      headers=headers, json={"payout_batch_id": "batch-1"}).json()
    # It is paid_at, not status alone, that records money actually left.
    assert paid["paid_at"] and paid["payout_batch_id"] == "batch-1"

    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts/{drop['payout_id']}/void",
               headers=headers, json={"reason": "recorded against the wrong org"})

    body = client.get("/api/asclepius/hs/payouts").json()
    # A cancelled entry is not a negative payment: it leaves the total rather
    # than being netted out of it, so no number a partner reads has an invisible
    # correction folded into it.
    assert body["summary"] == {"total_cents": 250000, "paid_cents": 250000,
                               "pending_cents": 0, "count": 1}
    shown = {p["description"]: p["status"] for p in body["payouts"]}
    assert set(shown.values()) <= {"paid", "cancelled", "recorded"}


def test_voiding_needs_a_reason():
    _, hs = _partner()
    admin, headers = _admin_client()
    row = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                     json={"amount_cents": 1000, "external_ref": "INV-X"}).json()
    r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts/{row['payout_id']}/void",
                   headers=headers, json={"reason": "  "})
    assert r.status_code == 400


# ─── Vocabulary and shape ────────────────────────────────────────────────────

def test_the_portal_never_shows_internal_words_or_our_own_references():
    client, hs = _partner()
    admin, headers = _admin_client()
    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
               json={"amount_cents": 4200, "external_ref": "SECRET-INVOICE-9",
                     "description": "Q3 nephrology license"})
    body = client.get("/api/asclepius/hs/payouts").json()
    row = body["payouts"][0]
    # 'accrued' is our bookkeeping and means nothing to a CFO.
    assert row["status"] == "recorded"
    assert set(row) == {"payout_id", "recorded_at", "description", "period_start",
                        "period_end", "amount_cents", "status", "paid_at"}
    # Our invoice key and who keyed it in are ours.
    assert "SECRET-INVOICE-9" not in client.get("/api/asclepius/hs/payouts").text
    assert "recorded_by" not in row


# ─── The rule that has to outlive the memory of it ───────────────────────────

def test_the_ledger_has_no_column_that_could_hold_a_payment_credential():
    """The disbursement seam says a change that wants to store a bank account or
    a tax id is the signal it belongs behind a payment processor. Mechanical, so
    the rule survives everyone who remembers why it exists."""
    store = _store()
    with store._conn() as conn:
        names = [r["name"] for r in conn.execute("PRAGMA table_info(hs_payouts)")]
    assert names, "hs_payouts does not exist"
    bad = [n for n in names
           if re.search(r"bank|account_number|routing|iban|swift|tax_id|ssn|ein", n, re.I)]
    assert not bad, f"hs_payouts grew a payment-credential column: {bad}"
