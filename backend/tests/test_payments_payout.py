"""PRD-P — the boundary where money actually leaves (audit H2).

``PAID`` existed in the schema, in ``LEDGER_STATES``, and in the doctor's headline
sum — and nothing could ever write it. There was no payout route, no batch, no
disbursement idempotency key. The double-payment guard was airtight *inside* the
system and absent at the one boundary where a double payment costs real money:
pay a physician $4,500 out of band and the ledger could not record it, the doctor
still saw $4,500 approved, and the next admin export re-included every row.

The fix is the boundary, not a smaller headline. Two properties carry it:

  * a payout is a BATCH with an id the caller supplies. That id is the
    idempotency key — replaying the same batch is a no-op, which is what makes a
    retried disbursement job safe.
  * ``approved -> paid`` is the only transition, enforced in SQL as a
    compare-and-set. A ``void`` row cannot be paid, an ``accrued`` row cannot skip
    review, and a row already in a batch cannot be swept into a second one.
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


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin():
    return A.make_user(_store(), role="admin")


def _doctor():
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (u["id"],))
    return store.get_user_by_id(u["id"])


def _earning(user, status, *, ref, cents=7500):
    return _store().insert_earning(
        earning_id=f"e-{ref}", user_id=user["id"], kind="task", ref_id=ref,
        amount_cents=cents, rate_cents=cents, status=status,
        accrued_at="2026-08-01T00:00:00",
        resolved_at="2026-08-02T00:00:00" if status != "accrued" else None)


def _pay(admin_h, *, batch_id, earning_ids=None, user_id=None, expect=200):
    body = {"payout_batch_id": batch_id}
    if earning_ids is not None:
        body["earning_ids"] = earning_ids
    if user_id is not None:
        body["user_id"] = user_id
    r = client.post("/api/asclepius/admin/earnings/mark-paid", json=body, headers=admin_h)
    assert r.status_code == expect, r.text
    return r.json()


def _ledger(user):
    return client.get("/api/asclepius/earnings", headers=A.headers_for(user)).json()


# ─── The transition ───────────────────────────────────────────────────────────
def test_an_approved_earning_can_be_marked_paid():
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")

    out = _pay(admin_h, batch_id="batch-2026-08", earning_ids=["e-s-1"])
    assert out["marked"] == 1
    assert out["payout_batch_id"] == "batch-2026-08"

    row = _store().get_earning(kind="task", ref_id="s-1")
    assert row["status"] == "paid"
    assert row["payout_batch_id"] == "batch-2026-08"
    assert row["resolved_at"]


def test_the_doctor_sees_paid_separately_from_still_owed():
    """The headline may sum approved + paid — both are 'earned and not in doubt'
    from the physician's side — but they must not be indistinguishable. "You have
    been paid $75" and "we owe you $75" are different sentences."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _earning(doctor, "approved", ref="s-2")
    _pay(admin_h, batch_id="batch-1", earning_ids=["e-s-1"])

    ledger = _ledger(doctor)
    assert ledger["approved_cents"] == 15000       # the headline, unchanged
    assert ledger["paid_cents"] == 7500            # ...of which this has landed
    assert ledger["unpaid_cents"] == 7500          # ...and this has not

    words = {r["ref_id"]: r["status_word"] for r in ledger["recent"]}
    assert words["s-1"] == "Paid"
    assert words["s-2"] == "Approved"


# ─── Idempotency, which is the whole point ────────────────────────────────────
def test_replaying_the_same_batch_pays_nobody_twice():
    """A disbursement job that times out and retries is the normal case, not the
    exotic one. The batch id is the idempotency key."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _earning(doctor, "approved", ref="s-2")

    first = _pay(admin_h, batch_id="batch-1", user_id=doctor["id"])
    assert first["marked"] == 2

    for _ in range(4):
        again = _pay(admin_h, batch_id="batch-1", user_id=doctor["id"])
        assert again["marked"] == 0
        assert again["already_in_batch"] == 2

    rows = _store().list_earnings(user_id=doctor["id"])
    assert {r["status"] for r in rows} == {"paid"}
    assert {r["payout_batch_id"] for r in rows} == {"batch-1"}


def test_a_row_already_paid_is_never_swept_into_a_second_batch():
    """The failure this prevents costs real money: a second batch re-including
    last month's rows and paying them again."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _pay(admin_h, batch_id="batch-1", user_id=doctor["id"])

    _earning(doctor, "approved", ref="s-2")
    out = _pay(admin_h, batch_id="batch-2", user_id=doctor["id"])
    assert out["marked"] == 1                       # only the new one

    rows = {r["ref_id"]: r for r in _store().list_earnings(user_id=doctor["id"])}
    assert rows["s-1"]["payout_batch_id"] == "batch-1"
    assert rows["s-2"]["payout_batch_id"] == "batch-2"


# ─── What may not be paid ─────────────────────────────────────────────────────
def test_only_approved_rows_can_be_paid():
    """``accrued`` has not passed review yet and ``void`` did not pass it. Paying
    either is paying for work nobody signed off on."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "accrued", ref="s-pending")
    _earning(doctor, "void", ref="s-rejected")

    out = _pay(admin_h, batch_id="batch-1",
               earning_ids=["e-s-pending", "e-s-rejected"])
    assert out["marked"] == 0
    assert out["skipped"] == 2

    assert _store().get_earning(kind="task", ref_id="s-pending")["status"] == "accrued"
    assert _store().get_earning(kind="task", ref_id="s-rejected")["status"] == "void"


def test_a_batch_id_is_required_and_must_be_meaningful():
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")

    for bad in ("", "   "):
        r = client.post("/api/asclepius/admin/earnings/mark-paid",
                        json={"payout_batch_id": bad, "user_id": doctor["id"]},
                        headers=admin_h)
        assert r.status_code == 422, r.text
    r = client.post("/api/asclepius/admin/earnings/mark-paid",
                    json={"user_id": doctor["id"]}, headers=admin_h)
    assert r.status_code == 422
    assert _store().get_earning(kind="task", ref_id="s-1")["status"] == "approved"


def test_marking_paid_requires_a_target_rather_than_paying_everyone():
    """A call with neither a user nor an explicit list is almost certainly a
    mistake, and the mistake is 'pay the entire company'. Refuse it."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _pay(admin_h, batch_id="batch-1", expect=422)
    assert _store().get_earning(kind="task", ref_id="s-1")["status"] == "approved"


def test_marking_paid_is_admin_only():
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    r = client.post("/api/asclepius/admin/earnings/mark-paid",
                    json={"payout_batch_id": "b", "user_id": doctor["id"]},
                    headers=A.headers_for(doctor))
    assert r.status_code == 403
    assert _store().get_earning(kind="task", ref_id="s-1")["status"] == "approved"


# ─── Provenance ───────────────────────────────────────────────────────────────
def test_every_payment_is_logged_with_its_batch_and_its_actor():
    admin = _admin()
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _pay(A.headers_for(admin), batch_id="batch-2026-08", user_id=doctor["id"])

    events = [e for e in _store().list_events(entity_type="earning")
              if e["event_type"] == "earning_paid"]
    assert len(events) == 1
    assert events[0]["actor"] == admin["id"]
    assert events[0]["payload"]["payout_batch_id"] == "batch-2026-08"
    assert events[0]["payload"]["amount_cents"] == 7500


def test_the_admin_ledger_can_be_filtered_to_a_batch():
    """Reconciling a disbursement against the ledger is the first thing anyone
    will want to do after running one."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _earning(doctor, "approved", ref="s-2")
    _pay(admin_h, batch_id="batch-1", earning_ids=["e-s-1"])

    payload = client.get("/api/asclepius/admin/earnings?payout_batch_id=batch-1",
                         headers=admin_h).json()
    assert [r["ref_id"] for r in payload["rows"]] == ["s-1"]
    assert payload["rows"][0]["status"] == "paid"


def test_the_reconciliation_sweep_never_touches_a_paid_row():
    """``paid`` is terminal. A late review verdict landing after a disbursement
    must not reopen or void money that has already left the building."""
    admin_h = A.headers_for(_admin())
    doctor = _doctor()
    _earning(doctor, "approved", ref="s-1")
    _pay(admin_h, batch_id="batch-1", user_id=doctor["id"])

    later = datetime.now(timezone.utc) + timedelta(days=365)
    asc_payments.reconcile_task_accruals(_store(), now=later)

    row = _store().get_earning(kind="task", ref_id="s-1")
    assert row["status"] == "paid"
    assert row["payout_batch_id"] == "batch-1"
