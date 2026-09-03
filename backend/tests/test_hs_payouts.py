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


# ─── Accrual: what we have taken, before anyone has priced it ────────────────

def _accept_uploads(hs_id, count):
    """Uploads the pipeline has finished with, which is what `accepted` means to
    a partner. Inserted directly because the accrual line is about the state the
    INGESTION pipeline lands on, not about how the bytes got here."""
    store = _store()
    for _ in range(count):
        up = store.insert_ingest_upload(
            link_id="hs-portal", partner_id=hs_id, filename="bundle.zip",
            sha256=uuid.uuid4().hex * 2, size_bytes=1024, raw_path=None,
            source_ip=None)
        store.set_upload_health_system(up["upload_id"], hs_id)
        store.update_ingest_upload(up["upload_id"], status="ingested")


def test_accepted_uploads_are_visible_before_anything_is_priced():
    """Pricing is manual and can take weeks. Without this line, a partner whose
    data we accepted reads a ledger of zero and cannot tell acceptance from
    loss, which is the support email this whole page exists to prevent."""
    client, hs = _partner()
    _accept_uploads(hs["hs_id"], 3)
    accrual = client.get("/api/asclepius/hs/payouts").json()["accrual"]
    assert accrual["accepted_uploads"] == 3
    assert accrual["ledger_entries"] == 0
    assert accrual["awaiting_pricing"] == 3


def test_the_accrual_line_promises_no_amount():
    """The one thing this line must never do. Nobody has priced these uploads,
    so a figure here would be a number we invented on a page a hospital's
    finance contact reads, and it would be quoted back at us."""
    client, hs = _partner()
    _accept_uploads(hs["hs_id"], 2)
    accrual = client.get("/api/asclepius/hs/payouts").json()["accrual"]
    assert not any(k.endswith("_cents") for k in accrual)
    assert "$" not in accrual["note"]
    # It says who does the pricing, so the reader knows what they are waiting on.
    assert "prices" in accrual["note"] and "not an amount owed" in accrual["note"]


def test_pricing_an_upload_closes_the_gap_it_opened():
    """The line is a GAP, not a running total. An accepted upload that has been
    turned into a ledger entry is no longer awaiting anything, and a line that
    kept counting it would tell a partner they are owed for it twice."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    for ref in ("INV-1", "INV-2"):
        assert admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                          json={"amount_cents": 50000, "external_ref": ref}
                          ).status_code == 200
    accrual = client.get("/api/asclepius/hs/payouts").json()["accrual"]
    assert accrual["accepted_uploads"] == 2 and accrual["ledger_entries"] == 2
    assert accrual["awaiting_pricing"] == 0


def test_more_ledger_entries_than_uploads_never_goes_negative():
    """An operator may price one upload into several rows, or record a payment
    that predates the portal. Neither is an error, and neither may render as
    "-2 uploads accepted and awaiting pricing"."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    for ref in ("INV-A", "INV-B", "INV-C"):
        admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/payouts", headers=headers,
                   json={"amount_cents": 1000, "external_ref": ref})
    accrual = client.get("/api/asclepius/hs/payouts").json()["accrual"]
    assert accrual["awaiting_pricing"] == 0


def test_the_accrual_counts_only_this_organizations_uploads():
    """The same property the ledger holds: the route takes no identifier, so
    neither may anything derived from it. A count that leaked across tenants
    would tell one hospital how busy another one is."""
    a_client, a_hs = _partner("Alpha Accrual")
    b_client, b_hs = _partner("Beta Accrual")
    _accept_uploads(a_hs["hs_id"], 4)
    assert a_client.get("/api/asclepius/hs/payouts").json()["accrual"][
        "accepted_uploads"] == 4
    assert b_client.get("/api/asclepius/hs/payouts").json()["accrual"][
        "accepted_uploads"] == 0


# ─── The line as rendered ────────────────────────────────────────────────────

PORTAL_DIR = Path(__file__).resolve().parents[2] / "frontend" / "provider"


def test_the_portal_renders_the_accrual_line_and_hides_it_when_there_is_no_gap():
    """Source-level, following ``test_hs_signin_split``: there is no jsdom here,
    and what actually breaks is somebody deleting the branch that hides the line.
    "0 uploads accepted and awaiting pricing" is a sentence that only worries the
    reader, so the zero case must stay hidden rather than merely read oddly."""
    js = (PORTAL_DIR / "provider.js").read_text(encoding="utf-8")
    assert "renderAccrual(data.accrual || {})" in js, "the line is never rendered"
    assert "awaiting_pricing" in js
    assert "and awaiting pricing" in js
    assert "host.hidden = true" in js, "the zero case is not hidden"
    # Counts only. The page must not turn a count into a figure of its own.
    assert "formatMoney(accrual" not in js


def test_every_class_the_accrual_line_uses_has_a_rule():
    """A class with no rule renders as unstyled text in the middle of a money
    page, which is exactly where it reads as a bug rather than as a line."""
    css = (PORTAL_DIR / "provider.css").read_text(encoding="utf-8")
    html = (PORTAL_DIR / "index.html").read_text(encoding="utf-8")
    for cls in ("prv-accrual", "prv-accrual-line", "prv-accrual-note"):
        assert f"class=\"{cls}\"" in html, f"{cls} is not on any element"
        assert f".{cls}" in css, f"{cls} has no rule in provider.css"
