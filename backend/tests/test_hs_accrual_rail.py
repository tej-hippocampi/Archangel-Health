"""The health-system payout rail: accrual, invoicing, settlement.

This rail fell through a mutual deferral between two PRDs, so before it existed
the only path from "we accepted your data" to "here is money" was an operator
typing a number into a box. The tests below are about the three properties that
make a computed ledger better than that box, and each one is a property somebody
will be tempted to relax later:

  * the rate is STAMPED at accrual, so a price change cannot restate history;
  * settlement is idempotent, so a double-submitted form pays once;
  * no bank or tax column ever appears, checked mechanically rather than
    remembered.

The fourth is the honest empty state. Nothing is priced by default, and an
unpriced partner must see a count of what we took rather than three zeroes that
read as "you are owed nothing".
"""
from __future__ import annotations

import ast
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

from asclepius import hs_billing  # noqa: E402

PASSWORD = "harbor-thistle-meadow-41"
ADMIN_BASE = "/api/asclepius/admin/health-systems"
PORTAL_DIR = Path(__file__).resolve().parents[2] / "frontend" / "provider"
BILLING_PY = Path(__file__).resolve().parent.parent / "asclepius" / "hs_billing.py"


def _billing_code() -> str:
    """``hs_billing.py`` with its docstrings and comments removed.

    The prose in that module explains why it stores no bank detail and why it
    stays out of the physician ledger, which means a naive grep for those words
    matches the explanation and passes forever. Stripping the prose is what
    makes the checks below assert the code rather than the promise.
    """
    src = BILLING_PY.read_text(encoding="utf-8")
    lines = src.splitlines()
    prose = set()
    for node in ast.walk(ast.parse(src)):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return "\n".join(ln for i, ln in enumerate(lines, 1)
                     if i not in prose and not ln.lstrip().startswith("#"))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    monkeypatch.setenv("ENV", "test")
    # No price anywhere by default, which is also production's default. A test
    # that leaked one would be testing a figure nobody agreed to.
    monkeypatch.delenv(hs_billing.RATE_ENV, raising=False)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _partner(name=None):
    store = _store()
    uname = "hs" + uuid.uuid4().hex[:10]
    hs = store.create_health_system_unclaimed(name or ("Rail Test " + uname))
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"], password=PASSWORD,
                                must_reset=False, approval_status="approved")
    client = TestClient(A.app, base_url="https://testserver")
    client.post("/api/asclepius/hs/login", json={"username": uname, "password": PASSWORD})
    return client, hs


def _admin_client():
    admin = A.make_user(_store(), role="admin")
    return TestClient(A.app), A.headers_for(admin)


def _accept_uploads(hs_id, count):
    """Uploads the ingestion pipeline has finished with, which is what
    `accepted` means to a partner and what the ledger accrues against."""
    store = _store()
    ids = []
    for _ in range(count):
        up = store.insert_ingest_upload(
            link_id="hs-portal", partner_id=hs_id, filename="bundle.zip",
            sha256=uuid.uuid4().hex * 2, size_bytes=1024, raw_path=None,
            source_ip=None)
        store.set_upload_health_system(up["upload_id"], hs_id)
        store.update_ingest_upload(up["upload_id"], status="ingested")
        ids.append(up["upload_id"])
    return ids


def _set_rate(admin, headers, hs_id, cents):
    return admin.post(f"{ADMIN_BASE}/{hs_id}/data-rate", headers=headers,
                      json={"rate_cents": cents})


# ─── Nothing is priced until somebody prices it ──────────────────────────────

def test_an_unpriced_partner_accrues_nothing_at_all():
    """The conservative default, and the one this whole design turns on. No
    price is baked into the code, so a partner whose terms have not been agreed
    gets no ledger rows, no figures, and nothing that could be read back at us
    as an offer."""
    client, hs = _partner()
    _accept_uploads(hs["hs_id"], 4)
    body = client.get("/api/asclepius/hs/payouts").json()
    assert body["rail"]["priced"] is False
    assert body["rail"]["accrued_cents"] == 0
    assert _store().list_hs_accruals(hs["hs_id"]) == []
    # And the honest line is still the count of what we took.
    assert body["accrual"]["awaiting_pricing"] == 4


def test_the_unpriced_note_promises_nothing():
    """This is the copy most likely to get softened into "your payment is being
    calculated", which would be a promise nobody has made."""
    client, hs = _partner()
    _accept_uploads(hs["hs_id"], 1)
    note = client.get("/api/asclepius/hs/payouts").json()["rail"]["note"]
    assert "$" not in note
    assert "not been priced" in note


def test_no_price_is_hardcoded_anywhere_in_the_billing_module():
    """A default rate is a business decision, and one nobody at this company
    made. If a figure appears in this module it got there by accident, and it
    will be the figure a hospital quotes back at us."""
    assert hs_billing._env_rate_cents() == 0
    assert not re.search(r"RATE_CENTS\s*=\s*[1-9]", _billing_code())


# ─── Accrual ─────────────────────────────────────────────────────────────────

def test_a_priced_partner_accrues_one_row_per_accepted_bundle():
    """Computed from what we actually accepted, not typed by an operator. That
    is the difference between a number a partner can reconcile against their own
    records and one they have to take on faith."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 3)
    assert _set_rate(admin, headers, hs["hs_id"], 50000).status_code == 200

    rail = client.get("/api/asclepius/hs/payouts").json()["rail"]
    assert rail["priced"] is True
    assert rail["accrued_cents"] == 150000
    assert rail["count"] == 3


def test_an_upload_still_in_flight_accrues_nothing():
    """Only accepted data is owed for. A bundle we are still parsing might yet
    fail our checks, and accruing against it would show a partner an obligation
    that can evaporate."""
    client, hs = _partner()
    admin, headers = _admin_client()
    store = _store()
    _accept_uploads(hs["hs_id"], 1)
    up = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="wip.zip",
        sha256=uuid.uuid4().hex * 2, size_bytes=10, raw_path=None, source_ip=None)
    store.set_upload_health_system(up["upload_id"], hs["hs_id"])
    store.update_ingest_upload(up["upload_id"], status="parsing")
    _set_rate(admin, headers, hs["hs_id"], 10000)

    assert client.get("/api/asclepius/hs/payouts").json()["rail"]["accrued_cents"] == 10000


def test_reconciling_twice_does_not_accrue_twice():
    """Reconciliation runs on every read of the payouts page, so it has to be
    idempotent by construction rather than by a caller checking first. The
    guard is the ledger's UNIQUE constraint, and this is what proves it holds
    when a partner refreshes."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 25000)

    for _ in range(4):
        client.get("/api/asclepius/hs/payouts")
    assert len(_store().list_hs_accruals(hs["hs_id"])) == 2


def test_the_accrual_line_stops_claiming_data_is_unpriced_once_it_is_priced():
    """Before the rail existed only a hand-entered payout could close that gap.
    A fully accrued partner still being told their data is awaiting pricing is
    the page contradicting itself."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 25000)

    accrual = client.get("/api/asclepius/hs/payouts").json()["accrual"]
    assert accrual["accepted_uploads"] == 2
    assert accrual["awaiting_pricing"] == 0


def test_the_accrual_counts_only_this_organizations_uploads():
    """The ledger route takes no identifier, so neither may anything derived
    from it. A count that leaked across tenants would tell one hospital how much
    another one is being paid."""
    a_client, a_hs = _partner("Alpha Rail")
    b_client, b_hs = _partner("Beta Rail")
    admin, headers = _admin_client()
    _accept_uploads(a_hs["hs_id"], 3)
    _set_rate(admin, headers, a_hs["hs_id"], 10000)
    _set_rate(admin, headers, b_hs["hs_id"], 10000)

    assert a_client.get("/api/asclepius/hs/payouts").json()["rail"]["accrued_cents"] == 30000
    assert b_client.get("/api/asclepius/hs/payouts").json()["rail"]["accrued_cents"] == 0


# ─── The stamped rate ────────────────────────────────────────────────────────

def test_a_price_change_cannot_restate_what_was_already_accrued():
    """THE property. A rate recomputed from current configuration means a
    partner's closed quarter silently changes value months later, and the first
    person to notice is their finance contact, in an email we cannot answer.
    The rate lives on the row."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 50000)
    assert client.get("/api/asclepius/hs/payouts").json()["rail"]["accrued_cents"] == 100000

    # Terms renegotiated upward, and two more bundles arrive under them.
    _set_rate(admin, headers, hs["hs_id"], 90000)
    _accept_uploads(hs["hs_id"], 2)

    rail = client.get("/api/asclepius/hs/payouts").json()["rail"]
    assert rail["accrued_cents"] == 100000 + 180000
    rows = _store().list_hs_accruals(hs["hs_id"])
    assert sorted(r["rate_cents"] for r in rows) == [50000, 50000, 90000, 90000]


def test_a_price_change_cannot_restate_a_settled_row_either():
    """The same guarantee, past the point where money actually moved. A settled
    amount that moves is not a ledger, it is a suggestion."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    _set_rate(admin, headers, hs["hs_id"], 40000)
    client.get("/api/asclepius/hs/payouts")
    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
               json={"settlement_ref": "WIRE-1"})

    _set_rate(admin, headers, hs["hs_id"], 5000)
    assert client.get("/api/asclepius/hs/payouts").json()["rail"]["settled_cents"] == 40000


def test_clearing_the_price_stops_new_accrual_without_erasing_the_old():
    """Un-pricing a partner is how an operator pauses a rail mid-negotiation.
    It must not be how the ledger they have already been shown disappears."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    _set_rate(admin, headers, hs["hs_id"], 30000)
    client.get("/api/asclepius/hs/payouts")

    assert _set_rate(admin, headers, hs["hs_id"], None).status_code == 200
    _accept_uploads(hs["hs_id"], 5)
    assert len(_store().list_hs_accruals(hs["hs_id"])) == 1


# ─── Invoicing ───────────────────────────────────────────────────────────────

def test_invoicing_moves_the_open_accruals_and_the_total_follows():
    """Accrued and invoiced are different facts about the same obligation, and
    a partner reconciling against their AP system needs to see which one each
    bundle is in."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 3)
    _set_rate(admin, headers, hs["hs_id"], 20000)
    client.get("/api/asclepius/hs/payouts")

    r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
                   json={"period": "2026-Q3"})
    assert r.status_code == 200, r.text
    assert r.json()["accruals"] == 3
    assert r.json()["invoice"]["amount_cents"] == 60000

    rail = client.get("/api/asclepius/hs/payouts").json()["rail"]
    assert rail["accrued_cents"] == 0 and rail["invoiced_cents"] == 60000
    assert rail["outstanding_cents"] == 60000


def test_the_same_period_cannot_be_invoiced_twice():
    """UNIQUE(hs_id, period) is the double-billing guard. A double-clicked
    Invoice button must not bill a hospital for the same quarter twice."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 20000)
    client.get("/api/asclepius/hs/payouts")

    first = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
                       json={"period": "2026-Q3"})
    second = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
                        json={"period": "2026-Q3"})
    assert first.status_code == 200 and second.status_code == 409
    assert len(_store().list_hs_invoices(hs["hs_id"])) == 1


def test_an_invoice_a_partner_can_see_is_not_a_draft():
    """A draft is a number an operator is still deciding about and the portal
    filters those out, so an invoice created as a draft is one the partner
    cannot reconcile against. This rail issues, it does not stage."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    _set_rate(admin, headers, hs["hs_id"], 15000)
    client.get("/api/asclepius/hs/payouts")
    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
               json={"period": "2026-09"})

    invoices = client.get("/api/asclepius/hs/payouts").json()["invoices"]
    assert [i["status"] for i in invoices] == ["issued"]
    assert invoices[0]["amount_cents"] == 15000


def test_invoicing_nothing_is_refused_rather_than_billed_for_zero():
    """A zero invoice is a document a hospital's AP team has to process for no
    reason, and it makes the ledger look like it is doing something."""
    _, hs = _partner()
    admin, headers = _admin_client()
    r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
                   json={"period": "2026-Q4"})
    assert r.status_code == 409


# ─── Settlement ──────────────────────────────────────────────────────────────

def test_settlement_records_that_money_moved_and_the_total_follows():
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 30000)
    client.get("/api/asclepius/hs/payouts")
    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
               json={"period": "2026-Q3"})

    r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
                   json={"settlement_ref": "WIRE-88"})
    assert r.status_code == 200
    assert r.json() == {"settlement_ref": "WIRE-88", "settled": 2,
                        "amount_cents": 60000, "already_in_ref": 0, "skipped": 0}

    rail = client.get("/api/asclepius/hs/payouts").json()["rail"]
    assert rail["settled_cents"] == 60000 and rail["outstanding_cents"] == 0


def test_a_double_submitted_settlement_pays_once():
    """The reference is the idempotency key, not a label. An operator who
    double-clicks, or a job that times out and retries, is the NORMAL case, and
    the guard is a compare-and-set rather than a read followed by a hopeful
    write. A retry is the run where `settled` is zero and `already_in_ref` is
    not, which is how an operator can tell the two apart."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 30000)
    client.get("/api/asclepius/hs/payouts")

    first = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
                       json={"settlement_ref": "WIRE-88"}).json()
    second = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
                        json={"settlement_ref": "WIRE-88"}).json()
    assert first["settled"] == 2 and first["amount_cents"] == 60000
    assert second["settled"] == 0 and second["already_in_ref"] == 2
    assert _store().hs_accrual_summary(hs["hs_id"])["settled_cents"] == 60000


def test_a_second_reference_cannot_settle_rows_the_first_one_settled():
    """Two transfers recorded against the same bundle would double the amount
    the ledger says we paid, which is the number that reconciles against a bank
    statement."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    _set_rate(admin, headers, hs["hs_id"], 30000)
    client.get("/api/asclepius/hs/payouts")

    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
               json={"settlement_ref": "WIRE-1"})
    again = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
                       json={"settlement_ref": "WIRE-2"}).json()
    assert again["settled"] == 0
    assert _store().hs_accrual_summary(hs["hs_id"])["settled_cents"] == 30000


def test_settling_an_invoice_in_full_marks_the_invoice_paid():
    """Derived from the lines rather than set separately, so the invoice and
    its accruals cannot disagree about whether it was paid."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 2)
    _set_rate(admin, headers, hs["hs_id"], 20000)
    client.get("/api/asclepius/hs/payouts")
    inv = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/invoice", headers=headers,
                     json={"period": "2026-Q3"}).json()["invoice"]

    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
               json={"settlement_ref": "WIRE-9", "invoice_id": inv["invoice_id"]})
    assert _store().get_hs_invoice(inv["invoice_id"])["status"] == "paid"


def test_a_settlement_without_a_reference_is_refused():
    """The reference is the only thing reconciling our record with the bank's.
    A settlement without one cannot be checked and cannot be replayed safely."""
    _, hs = _partner()
    admin, headers = _admin_client()
    r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
                   json={"settlement_ref": "   "})
    assert r.status_code == 400


def test_settled_money_cannot_be_voided_away():
    """Money that cleared is a fact. A ledger that can cancel it retroactively
    is one whose totals stop meaning anything."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    _set_rate(admin, headers, hs["hs_id"], 20000)
    client.get("/api/asclepius/hs/payouts")
    row = _store().list_hs_accruals(hs["hs_id"])[0]
    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/settle", headers=headers,
               json={"settlement_ref": "WIRE-3"})

    r = admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/{row['accrual_id']}/void",
                   headers=headers, json={"reason": "wrong org"})
    assert r.status_code == 409


def test_a_voided_accrual_is_not_written_back_by_the_next_sweep():
    """Voiding is a DECISION not to pay for that bundle. Reconciliation reading
    its absence from the live totals as unnoticed work would undo that decision
    the next time anybody opened the page."""
    client, hs = _partner()
    admin, headers = _admin_client()
    _accept_uploads(hs["hs_id"], 1)
    _set_rate(admin, headers, hs["hs_id"], 20000)
    client.get("/api/asclepius/hs/payouts")
    row = _store().list_hs_accruals(hs["hs_id"])[0]
    admin.post(f"{ADMIN_BASE}/{hs['hs_id']}/accruals/{row['accrual_id']}/void",
               headers=headers, json={"reason": "recorded against the wrong org"})

    for _ in range(3):
        client.get("/api/asclepius/hs/payouts")
    rows = _store().list_hs_accruals(hs["hs_id"])
    assert len(rows) == 1 and rows[0]["status"] == "void"
    assert client.get("/api/asclepius/hs/payouts").json()["rail"]["accrued_cents"] == 0


# ─── Isolation and the rule that must outlive the memory of it ───────────────

def test_no_accrual_route_on_the_portal_accepts_an_identifier():
    """The partner-facing ledger is isolated by a property of the ROUTE, not by
    a check somebody wrote, and everything derived from it inherits that."""
    paths = [r.path for r in A.app.routes
             if getattr(r, "path", "").startswith("/api/asclepius/hs/payouts")]
    assert paths
    for path in paths:
        assert "{" not in path


def test_the_accrual_ledger_has_no_column_that_could_hold_a_payment_credential():
    """We never store an account number, a routing number or a tax identifier,
    for a health system any more than for a physician. Mechanical, following
    test_hs_payouts, so the rule survives everyone who remembers why it exists:
    a change that wants such a column is the signal it belongs behind a payment
    processor instead."""
    store = _store()
    with store._conn() as conn:
        names = [r["name"] for r in conn.execute("PRAGMA table_info(hs_accruals)")]
    assert names, "hs_accruals does not exist"
    bad = [n for n in names
           if re.search(r"bank|account_number|routing|iban|swift|tax_id|ssn|ein", n, re.I)]
    assert not bad, f"hs_accruals grew a payment-credential column: {bad}"


def test_the_billing_module_never_names_a_payment_credential():
    """The table check above is only half of it: a value that reaches this
    module has somewhere to go even if this table has no column for it."""
    code = _billing_code()
    hit = re.search(r"\b(routing_number|account_number|iban|swift_code|"
                    r"tax_id|ssn|ein)\b", code, re.I)
    assert not hit, f"hs_billing handles {hit.group(0) if hit else ''}"


def test_the_billing_module_does_not_reach_into_the_physician_rail():
    """Two counterparties, two ledgers. Every path in asclepius/payments.py
    assumes physician semantics, and a health system reaching them is how the
    wrong party gets paid. Asserted rather than trusted to a docstring, and
    against the code rather than the docstring that makes the promise."""
    code = _billing_code()
    for banned in ("from asclepius import payments", "import payments",
                   "asclepius.payments", "import stripe", "stripe.",
                   "connect_account"):
        assert banned not in code, f"hs_billing reaches into {banned}"


# ─── The rail as rendered ────────────────────────────────────────────────────

def test_the_portal_renders_the_rail_and_hides_it_when_nothing_is_priced():
    """Source-level, following test_hs_payouts: there is no jsdom here, and what
    actually breaks is somebody deleting the branch that hides the block. Three
    zeroes on a money page read as "you are owed nothing", which is a different
    and false statement from "nobody has priced this yet"."""
    js = (PORTAL_DIR / "provider.js").read_text(encoding="utf-8")
    assert "renderRail(data.rail || {})" in js, "the rail is never rendered"
    for field in ("accrued_cents", "invoiced_cents", "settled_cents"):
        assert field in js
    assert "if (!rail.priced)" in js, "the unpriced case is not hidden"


def test_every_class_the_rail_uses_has_a_rule():
    """A class with no rule renders as unstyled text in the middle of a money
    page, which is exactly where it reads as a bug rather than as a line."""
    css = (PORTAL_DIR / "provider.css").read_text(encoding="utf-8")
    html = (PORTAL_DIR / "index.html").read_text(encoding="utf-8")
    for cls in ("prv-rail", "prv-rail-stats", "prv-rail-note"):
        assert f'class="{cls}"' in html, f"{cls} is not on any element"
        assert f".{cls}" in css, f"{cls} has no rule in provider.css"
