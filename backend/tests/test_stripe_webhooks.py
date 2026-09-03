"""Payments Rail §D: webhooks, and the invariant that justifies the whole shape.

A webhook endpoint on a money surface is an unauthenticated write path with a
signature in front of it, so the signature is the first thing tested here. The
second is at-most-once processing, which has to come from a durable table rather
than from hope: Stripe redelivers on any non-2xx, delivers out of order, and
will happily send an event type that did not exist when this code was written.

The last test in this file is the one the rest of the design rests on. We
delegate 1099-NEC filing to Stripe, and that is only defensible because we hold
nothing worth breaching. The file header on the payments router has said so in
prose since before there was a rail; here it becomes an assertion.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from tests import _stripe_fake  # noqa: E402

client = TestClient(A.app)

_BACKEND = Path(__file__).resolve().parent.parent
_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


@pytest.fixture()
def stripe_fake(monkeypatch):
    return _stripe_fake.install(monkeypatch)


def _store():
    from asclepius.store import get_store
    return get_store()


def _post(body: bytes, *, signature=None, secret="whsec_test_rail"):
    return client.post(
        "/api/asclepius/stripe/webhook", content=body,
        headers={"stripe-signature": signature if signature is not None
                 else _stripe_fake.sign(body, secret)})


def _linked_doctor(account_id="acct_hooked", *, status="onboarding"):
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved', "
                     "bank_link_status = ?, stripe_account_id = ? WHERE id = ?",
                     (status, account_id, user["id"]))
    return store.get_user_by_id(user["id"])


def test_bad_signature_rejected(stripe_fake):
    """WHY: an unsigned webhook is an unauthorized write path into the payout
    ledger. Three flavours of wrong, because they are three different bugs.

    The third one matters most: a signature that is perfectly valid for a
    DIFFERENT body is what a replay-with-substitution attack looks like, and a
    verifier that only checks the HMAC exists would pass it.
    """
    body = _stripe_fake.event_body(
        "evt_bad", "account.updated", {"id": "acct_hooked", "payouts_enabled": True})

    assert _post(body, signature="").status_code == 400
    assert _post(body, signature="t=1,v1=deadbeef").status_code == 400
    other = _stripe_fake.event_body("evt_other", "account.updated", {"id": "acct_x"})
    stolen = _stripe_fake.sign(other, "whsec_test_rail")
    assert _post(body, signature=stolen).status_code == 400

    # Nothing was stored, so a rejected body cannot fill the table either.
    assert _store().get_stripe_webhook_event("evt_bad") is None
    # And the handler never ran.
    assert _post(body).status_code == 200


def test_duplicate_event_id_processed_once(stripe_fake):
    """WHY: G5. Stripe redelivers, so at-most-once processing has to come from
    the table rather than from an assumption that it will not.

    The check is deliberately ``processed_at IS NOT NULL`` rather than "have I
    seen this id". A crash between storing the event and handling it must leave
    a work item that the redelivery picks up, not a row that makes the
    redelivery look like a duplicate and silently drop it.
    """
    doctor = _linked_doctor()
    body = _stripe_fake.event_body(
        "evt_dup", "account.updated",
        {"id": "acct_hooked", "payouts_enabled": True, "requirements": {}})

    first = _post(body)
    assert first.status_code == 200 and first.json() == {"ok": True, "received": True}
    stored = _store().get_stripe_webhook_event("evt_dup")
    assert stored["processed_at"] and stored["outcome"] == "status: onboarding -> active"

    second = _post(body)
    assert second.status_code == 200 and second.json() == {"ok": True, "duplicate": True}

    # One event, one status change, one log line. A second pass through the
    # handler would have logged a second transition for a status that never
    # moved twice.
    events = [e for e in _store().list_events(entity_type="user", entity_id=doctor["id"])
              if e["event_type"] == "bank_link_status_changed"]
    assert len(events) == 1


def test_an_event_stored_but_never_processed_is_reprocessed_on_redelivery(stripe_fake):
    """WHY: the crash-mid-handler case, which is the reason the duplicate test
    is written against ``processed_at`` and not against row existence."""
    _linked_doctor()
    store = _store()
    # Exactly the state a crash between the insert and the handler leaves.
    store.record_stripe_webhook_event(
        event_id="evt_crash", event_type="account.updated", payload_json="{}")
    assert store.get_stripe_webhook_event("evt_crash")["processed_at"] is None

    body = _stripe_fake.event_body(
        "evt_crash", "account.updated",
        {"id": "acct_hooked", "payouts_enabled": True, "requirements": {}})
    assert _post(body).json() == {"ok": True, "received": True}
    assert store.get_stripe_webhook_event("evt_crash")["processed_at"]


def test_unknown_event_types_are_stored_and_never_500(stripe_fake):
    """WHY: D4. Stripe adds event types, and it disables an endpoint that keeps
    failing. A 500 on novelty is therefore a slow way to turn the payout rail
    off, and the fix has to be "store it, stamp it, do nothing"."""
    body = _stripe_fake.event_body(
        "evt_novel", "billing.alert.triggered", {"id": "ba_1", "whatever": True})
    resp = _post(body)
    assert resp.status_code == 200
    stored = _store().get_stripe_webhook_event("evt_novel")
    assert stored["type"] == "billing.alert.triggered"
    assert stored["processed_at"] and "unhandled" in stored["outcome"]


def test_an_event_for_an_account_we_do_not_know_is_not_an_error(stripe_fake):
    """WHY: one endpoint receives events for every account on the platform,
    including test-mode accounts and accounts created by another environment
    pointed at the same URL. Unknown is a normal answer."""
    body = _stripe_fake.event_body(
        "evt_stranger", "account.updated",
        {"id": "acct_never_seen", "payouts_enabled": True, "requirements": {}})
    assert _post(body).status_code == 200
    assert "unknown account" in _store().get_stripe_webhook_event("evt_stranger")["outcome"]


def test_transfer_events_stamp_the_attempt_row(stripe_fake):
    """WHY: D3. The ``stripe_transfers`` row is the reconciliation record, and it
    is only worth reading if Stripe's own view of the transfer keeps it current."""
    store = _store()
    store.record_stripe_transfer(earning_id="e-hook", status="transferred",
                                 transfer_id="tr_hook", payout_batch_id="batch-hook")
    body = _stripe_fake.event_body(
        "evt_tr", "transfer.updated",
        {"id": "tr_hook", "reversed": False, "failure_message": None})
    assert _post(body).status_code == 200
    assert store.get_stripe_transfer("e-hook")["status"] == "transferred"


def test_a_reversal_is_visibility_only(stripe_fake):
    """WHY: G7. A reversal is a Stripe-dashboard treasury operation.

    The void endpoint already 409s on a paid row because money has left, and a
    ledger that mutated itself on a reversal would contradict the attributed,
    human-decided shape of every other money action in this system. So the
    attempt row learns about it and the ledger row does not move.
    """
    store = _store()
    doctor = _linked_doctor(status="active")
    store.insert_earning(
        earning_id="e-rev", user_id=doctor["id"], kind="task", ref_id="rev",
        amount_cents=7500, rate_cents=7500, status="paid",
        accrued_at="2026-08-01T00:00:00", resolved_at="2026-08-02T00:00:00")
    store.record_stripe_transfer(earning_id="e-rev", status="transferred",
                                 transfer_id="tr_rev", payout_batch_id="batch-rev")

    body = _stripe_fake.event_body(
        "evt_rev", "transfer.reversed", {"id": "tr_rev", "reversed": True})
    assert _post(body).status_code == 200

    assert store.get_stripe_transfer("e-rev")["status"] == "reversed"
    assert store.get_earning_by_id("e-rev")["status"] == "paid", (
        "a reversal moved the ledger; only a human may do that")


def test_a_transfer_event_for_a_transfer_we_never_made_changes_nothing(stripe_fake):
    """WHY: the same reason unknown accounts are tolerated, applied to the table
    that decides which payouts look settled."""
    body = _stripe_fake.event_body(
        "evt_ghost", "transfer.created", {"id": "tr_ghost"})
    assert _post(body).status_code == 200
    assert "unknown transfer" in _store().get_stripe_webhook_event("evt_ghost")["outcome"]


# ═════════════════════════════════════════════════════════════════════════════
# E4: the invariant that justifies delegating 1099s
# ═════════════════════════════════════════════════════════════════════════════

#: Things we must never hold. Written as patterns rather than substrings so that
#: ``stripe_account_id`` (fine) is not confused with ``bank_account`` (not fine)
#: and so that a three-letter word like ``tin`` only matches on its own.
#:
#: The separator excludes a space on purpose. This has to catch field names,
#: column names and payload keys, which is where the data would actually live,
#: and the phrase "link your bank account" is copy a physician needs to read on
#: the card. A rule that banned the sentence would be a rule people route around
#: by rewording the button.
_FORBIDDEN = re.compile(
    r"routing[_\-]?number|account[_\-]?number|bank[_\-]?account|"
    r"social[_\-]?security|tax[_\-]?id\b|\bssn\b|\bein\b|\btin\b|\biban\b|"
    r"\bsort[_\-]?code\b",
    re.IGNORECASE)

#: Every file the payments rail added or touched with money in it.
_RAIL_SOURCES = (
    _BACKEND / "asclepius" / "stripe_rail.py",
    _BACKEND / "asclepius" / "store.py",
    _BACKEND / "asclepius" / "payments.py",
    _BACKEND / "routers" / "asclepius_payments.py",
    _BACKEND / "routers" / "asclepius.py",
    _FRONTEND / "first_run.js",
)


def _strip_prose(source: str, *, js: bool) -> str:
    """Code only. Comments and docstrings are where the rule is EXPLAINED.

    A file that says "there is deliberately no routing_number column here" is
    doing the right thing, and a grep that failed it would teach people to stop
    writing the explanation down.
    """
    if js:
        source = re.sub(r"/\*[\s\S]*?\*/", "", source)
        return re.sub(r"//[^\n]*", "", source)
    source = re.sub(r'"""[\s\S]*?"""', '""', source)
    source = re.sub(r"'''[\s\S]*?'''", "''", source)
    return re.sub(r"#[^\n]*", "", source)


def test_no_bank_or_tax_data_ever_stored():
    """WHY: E4. This is the assertion the entire 1099 decision rests on.

    We tell physicians that Stripe collects their bank details and their tax
    identity and files their 1099-NEC, and we tell ourselves that delegating it
    is safe because there is nothing here to leak. Both statements stop being
    true the first time somebody adds a convenience column, and it will look
    reasonable in review: caching the last four digits to show on a receipt,
    storing a TIN so an admin can answer a question faster.

    Three checks, because there are three ways it breaks: the schema, the code,
    and the wire.
    """
    store = _store()

    # 1. The schema. Not just the new tables: ``users`` is where a bank detail
    #    would most plausibly be parked.
    with store._conn() as conn:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
        for table in tables:
            for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
                assert not _FORBIDDEN.search(col["name"]), (
                    f"{table}.{col['name']} looks like bank or tax data. "
                    "It belongs behind Connect, not in this database.")

    # 2. The code. Comments explaining the rule are stripped first, so writing
    #    the reasoning down stays free.
    for path in _RAIL_SOURCES:
        code = _strip_prose(path.read_text(encoding="utf-8"), js=path.suffix == ".js")
        hit = _FORBIDDEN.search(code)
        assert hit is None, (
            f"{path.name} handles {hit.group(0)!r} in code. Nothing in this "
            "codebase may touch a bank account number or a tax id.")

    # 3. The wire. What the rail actually asks Stripe for, and what it hands
    #    back to a physician, are both checked in the flow tests; here the
    #    account-state serializer is pinned directly, since it is the one
    #    function that reads a Stripe account object and returns part of it.
    from asclepius import stripe_rail

    public = stripe_rail.account_public_state({
        "payouts_enabled": True, "details_submitted": True,
        "external_accounts": {"data": [{"last4": "6789", "routing_number": "110000000"}]},
        "individual": {"id_number_provided": True, "ssn_last_4_provided": True},
        "requirements": {"disabled_reason": None,
                         "currently_due": ["individual.id_number"]},
    })
    assert set(public) == {"payouts_enabled", "details_submitted", "disabled_reason"}
    assert not _FORBIDDEN.search(json.dumps(public))


def test_the_rail_stores_exactly_two_stripe_columns():
    """WHY: G1, as a schema fact rather than a promise.

    ``users`` gains one column and the two new tables carry an account status
    and a transfer's own state. If a future change needs a third fact about a
    physician's Stripe account, that is the moment to ask whether it belongs
    behind Connect instead, and this test is where that question gets asked.
    """
    store = _store()
    with store._conn() as conn:
        conn.row_factory = sqlite3.Row
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        transfer_cols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(stripe_transfers)").fetchall()}
        webhook_cols = {r["name"] for r in
                        conn.execute("PRAGMA table_info(stripe_webhook_events)").fetchall()}

    assert {c for c in user_cols if "stripe" in c} == {"stripe_account_id"}
    assert "bank_link_status" in user_cols
    assert transfer_cols == {"earning_id", "transfer_id", "status", "failure_reason",
                             "payout_batch_id", "created_at", "updated_at"}
    assert webhook_cols == {"event_id", "type", "payload_json", "received_at",
                            "processed_at", "outcome"}
