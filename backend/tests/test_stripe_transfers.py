"""Payments Rail §C: the transfer that follows the ledger write.

``mark_paid`` was already the boundary where a double payment costs real money,
and it was made safe by a batch id used as an idempotency key plus a guarded
compare-and-set. This file is about the second half of that boundary: money
actually leaving, ordered so that it cannot corrupt the first half.

The ordering is the design. The ledger records our DECISION to pay; Stripe
records the EXECUTION. The transfer is created after the compare-and-set
succeeds, so a Stripe failure cannot un-settle a ledger row, and the one case
that would be genuinely unrecoverable, settling a row for a physician we
provably cannot pay, is refused before the ledger is touched at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from tests import _stripe_fake  # noqa: E402

client = TestClient(A.app)


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


def _admin():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _doctor(*, bank="active", account="acct_linked"):
    """A physician who has finished Connect onboarding, unless a test says not."""
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET verification_status = 'approved', "
            "bank_link_status = ?, stripe_account_id = ? WHERE id = ?",
            (bank, account, user["id"]))
    return store.get_user_by_id(user["id"])


def _earning(user, *, ref, cents=7500, status="approved"):
    return _store().insert_earning(
        earning_id=f"e-{ref}", user_id=user["id"], kind="task", ref_id=ref,
        amount_cents=cents, rate_cents=cents, status=status,
        accrued_at="2026-08-01T00:00:00", resolved_at="2026-08-02T00:00:00")


def _pay(headers, doctor, batch, **extra):
    body = {"payout_batch_id": batch, "user_id": doctor["id"], **extra}
    return client.post("/api/asclepius/admin/earnings/pay", json=body, headers=headers)


def test_transfer_follows_mark_paid_with_row_idempotency_key(stripe_fake):
    """WHY: G2 and G3 together.

    G2: the ledger row is settled first and the transfer is a consequence of it,
    never a precondition. G3: one transfer per ledger row, keyed
    ``earning:{id}``, grouped by the batch, so Stripe's ledger reconciles 1:1
    against ``GET /admin/earnings?payout_batch_id=...`` and a partial failure is
    a list of rows rather than an arithmetic problem.
    """
    doctor = _doctor()
    headers = _admin()
    _earning(doctor, ref="t1", cents=7500)
    _earning(doctor, ref="t2", cents=5000)

    resp = _pay(headers, doctor, "batch-t")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["marked"] == 2

    # The ledger moved first, and it moved completely.
    assert all(row["status"] == "paid"
               for row in _store().list_earnings(payout_batch_id="batch-t"))

    keys = sorted(call["idempotency_key"] for call in stripe_fake.transfer_calls)
    assert keys == ["earning:e-t1", "earning:e-t2"]
    assert {call["transfer_group"] for call in stripe_fake.transfer_calls} == {"batch-t"}
    assert {call["destination"] for call in stripe_fake.transfer_calls} == {"acct_linked"}
    assert {call["currency"] for call in stripe_fake.transfer_calls} == {"usd"}
    assert sorted(call["amount"] for call in stripe_fake.transfer_calls) == [5000, 7500]

    # C3: the response says what happened per row, not what was intended.
    outcomes = {t["earning_id"]: t for t in body["transfers"]}
    assert set(outcomes) == {"e-t1", "e-t2"}
    assert all(t["status"] == "transferred" and t["transfer_id"] for t in outcomes.values())
    # And each attempt is a durable row, which is what makes the queue in G4
    # possible at all.
    assert _store().get_stripe_transfer("e-t1")["status"] == "transferred"


def test_replaying_a_batch_pays_once(stripe_fake):
    """WHY: a disbursement job that times out and retries is the normal case.

    The ledger already made the replay a no-op. This asserts the money half
    matches: the second call moves nothing, because the rows it would transfer
    are already transferred and because the key would make Stripe refuse anyway.
    """
    doctor = _doctor()
    headers = _admin()
    _earning(doctor, ref="r1")

    first = _pay(headers, doctor, "batch-r")
    second = _pay(headers, doctor, "batch-r")
    assert first.status_code == second.status_code == 200
    assert first.json()["marked"] == 1
    assert second.json()["marked"] == 0
    assert stripe_fake.settled_transfer_count == 1


def test_transfer_failure_leaves_ledger_settled_and_queues_row(stripe_fake):
    """WHY: G4. A failed transfer is a queue item, not a rollback.

    Un-settling the row would mean the ledger contradicts an admin's recorded
    decision to pay, and would let the next payout run sweep the row into a
    second batch while the first transfer's fate is still unknown. The honest
    answer is: the decision stands, the execution failed, and the failure is
    visible with a reason somebody can act on.
    """
    doctor = _doctor()
    headers = _admin()
    _earning(doctor, ref="f1")
    _earning(doctor, ref="f2")
    stripe_fake.fail_transfer_for("e-f2", "The destination account cannot receive transfers.")

    resp = _pay(headers, doctor, "batch-f")
    assert resp.status_code == 200, resp.text
    outcomes = {t["earning_id"]: t for t in resp.json()["transfers"]}

    # The ledger says settled for BOTH rows. That is the decision record.
    rows = {r["earning_id"]: r for r in _store().list_earnings(payout_batch_id="batch-f")}
    assert rows["e-f1"]["status"] == "paid" and rows["e-f2"]["status"] == "paid"

    assert outcomes["e-f1"]["status"] == "transferred"
    assert outcomes["e-f2"]["status"] == "failed"
    assert "cannot receive transfers" in outcomes["e-f2"]["failure_reason"]

    queued = _store().get_stripe_transfer("e-f2")
    assert queued["status"] == "failed"
    assert queued["transfer_id"] is None
    assert queued["payout_batch_id"] == "batch-f"
    # One row's failure does not take the other row's money with it.
    assert stripe_fake.settled_transfer_count == 1


def test_retry_is_idempotent_and_gated(stripe_fake):
    """WHY: C4. Two properties, and only one of them is the gate.

    The gate refuses rows where a retry makes no sense: not settled, or already
    transferred. But the reason a double-clicked retry cannot double-pay is the
    idempotency key derived from the ledger row, which holds whatever the gate
    says. Both are asserted, in that order, because a reviewer looking at a
    money path should be able to see which one is load-bearing.
    """
    doctor = _doctor()
    headers = _admin()
    _earning(doctor, ref="q1")
    stripe_fake.fail_transfer_for("e-q1", "Insufficient funds in the platform balance.")
    assert _pay(headers, doctor, "batch-q").status_code == 200
    assert _store().get_stripe_transfer("e-q1")["status"] == "failed"

    # The gate: a row that was never settled has no decision to execute.
    _earning(doctor, ref="q2", status="approved")
    unsettled = client.post("/api/asclepius/admin/earnings/e-q2/retry-transfer",
                            headers=headers)
    assert unsettled.status_code == 409
    assert "not settled" in unsettled.json()["detail"]
    assert client.post("/api/asclepius/admin/earnings/e-nope/retry-transfer",
                       headers=headers).status_code == 404

    # The retry itself, now that the platform balance is fixed.
    stripe_fake.clear_transfer_failures()
    retried = client.post("/api/asclepius/admin/earnings/e-q1/retry-transfer",
                          headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["ok"] is True
    assert _store().get_stripe_transfer("e-q1")["status"] == "transferred"
    assert stripe_fake.settled_transfer_count == 1

    # The double click. Refused by the gate, and the key means it would have
    # been harmless even if it were not.
    again = client.post("/api/asclepius/admin/earnings/e-q1/retry-transfer",
                        headers=headers)
    assert again.status_code == 409
    assert "already transferred" in again.json()["detail"]
    assert stripe_fake.settled_transfer_count == 1


def test_a_retry_is_not_a_second_payment_even_when_the_gate_is_bypassed(stripe_fake):
    """WHY: the gate is a message to an operator; the idempotency key is the
    guarantee. Calling the rail directly twice with the same ledger row proves
    which one actually prevents a double payment."""
    from asclepius import stripe_rail

    first = stripe_rail.create_transfer(
        earning_id="e-key", amount_cents=7500, destination="acct_linked",
        payout_batch_id="batch-key")
    second = stripe_rail.create_transfer(
        earning_id="e-key", amount_cents=7500, destination="acct_linked",
        payout_batch_id="batch-key")
    assert first["transfer_id"] == second["transfer_id"]
    assert stripe_fake.settled_transfer_count == 1


def test_pay_refused_without_active_bank_link(stripe_fake):
    """WHY: C2. Settled-but-unpayable must be impossible to CREATE, not merely
    detectable afterwards.

    There is no later action that makes such a row honest: the ledger says we
    paid, the physician's bank never saw it, and the export re-includes nothing.
    So the refusal happens before ``mark_paid`` runs, and the test asserts the
    ledger is untouched rather than just asserting the status code.
    """
    doctor = _doctor(bank="onboarding", account=None)
    headers = _admin()
    _earning(doctor, ref="n1")

    resp = _pay(headers, doctor, "batch-n")
    assert resp.status_code == 409
    assert "bank account" in resp.json()["detail"]
    assert "Nothing was marked paid" in resp.json()["detail"]

    row = _store().get_earning_by_id("e-n1")
    assert row["status"] == "approved" and row["payout_batch_id"] is None
    assert stripe_fake.transfer_calls == []

    # And mark-paid, the other door onto the same ledger write, refuses too.
    direct = client.post("/api/asclepius/admin/earnings/mark-paid",
                         json={"payout_batch_id": "batch-n2", "earning_ids": ["e-n1"]},
                         headers=headers)
    assert direct.status_code == 409
    assert _store().get_earning_by_id("e-n1")["status"] == "approved"


def test_a_restricted_physician_is_refused_like_an_unlinked_one(stripe_fake):
    """WHY: ``restricted`` means Stripe has decided it will not pay this person.
    Treating it as anything other than unpayable is how a payout run settles rows
    against an account under review."""
    doctor = _doctor(bank="restricted", account="acct_restricted")
    _earning(doctor, ref="x1")
    resp = _pay(_admin(), doctor, "batch-x")
    assert resp.status_code == 409
    assert _store().get_earning_by_id("e-x1")["status"] == "approved"


def test_a_misconfigured_rail_stops_the_payout_before_the_ledger_moves(
        stripe_fake, monkeypatch):
    """WHY: A2. Flag on with a missing key is an operator incident, and a payout
    run that quietly moved no money looks exactly like one that worked."""
    doctor = _doctor()
    _earning(doctor, ref="m1")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    resp = _pay(_admin(), doctor, "batch-m")
    assert resp.status_code == 503
    assert "STRIPE_SECRET_KEY" in resp.json()["detail"]
    assert _store().get_earning_by_id("e-m1")["status"] == "approved"


def test_equity_only_contributors_are_still_refused_before_the_rail_is_consulted(
        stripe_fake):
    """WHY: the existing compensation guard is not superseded by the rail. An
    advisor on equity is not paid per case regardless of whether they have a
    bank account linked, and the older refusal has to win."""
    store = _store()
    doctor = _doctor()
    with store._conn() as conn:
        conn.execute("UPDATE users SET compensation_model = 'equity_only' WHERE id = ?",
                     (doctor["id"],))
    _earning(doctor, ref="eq1")
    resp = _pay(_admin(), doctor, "batch-eq")
    assert resp.status_code == 409
    assert "equity" in resp.json()["detail"]
    assert stripe_fake.transfer_calls == []
