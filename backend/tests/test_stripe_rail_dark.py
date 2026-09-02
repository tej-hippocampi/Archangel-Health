"""Payments Rail: the rail ships DARK, and dark means nothing changed.

The lock on this build is that ``ASCLEPIUS_STRIPE_ENABLED=0`` leaves every
existing behavior exactly as it was: the same response bodies, the same keys in
the same shapes, no new observable surface, and no dependency loaded. That is
what lets money-moving code land in a deploy that moves no money.

The tests here are the ones that would catch the ways that promise usually
breaks: a response that grew a key, a placeholder that started answering
differently, a route that 404s in production but not in the test environment,
and an import that only works because the SDK happened to be installed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # Explicitly off rather than merely unset: the whole file is about what
    # happens when the flag says no, and inheriting that from an empty
    # environment would make the file pass for the wrong reason.
    monkeypatch.setenv("ASCLEPIUS_STRIPE_ENABLED", "0")
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _doctor():
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (user["id"],))
    return store.get_user_by_id(user["id"])


def _earning(user, status, *, ref, cents=7500):
    return _store().insert_earning(
        earning_id=f"e-{ref}", user_id=user["id"], kind="task", ref_id=ref,
        amount_cents=cents, rate_cents=cents, status=status,
        accrued_at="2026-08-01T00:00:00", resolved_at="2026-08-02T00:00:00")


def test_flag_off_is_byte_identical_current_behavior():
    """WHY: the founder decision was "build it, ship it dark, flip it later".

    Dark has to mean indistinguishable, not merely inert. A client that can tell
    the rail exists can be told the rail exists by a bug, and the interest
    endpoint, the disbursement endpoints and the session payload are the three
    places a physician or an admin would notice first.
    """
    doctor = _doctor()
    admin = A.make_user(_store(), role="admin")
    doc_headers = A.headers_for(doctor)
    admin_headers = A.headers_for(admin)

    # 1. The placeholder interest endpoint, verbatim.
    interest = client.post("/api/asclepius/me/bank-link/interest", headers=doc_headers)
    assert interest.status_code == 200
    assert interest.json() == {"ok": True, "bank_link_status": "coming_soon"}

    # 2. Both NEW endpoints answer with the same body, so no client can tell
    #    them apart from the placeholder that has always been there.
    started = client.post("/api/asclepius/me/bank-link/start", headers=doc_headers)
    assert started.status_code == 200
    assert started.json() == {"ok": True, "bank_link_status": "coming_soon"}
    read = client.get("/api/asclepius/me/bank-link", headers=doc_headers)
    assert read.status_code == 200
    assert read.json() == {"ok": True, "bank_link_status": "coming_soon"}

    # 3. The session payload does not grow a key. Present-and-false would still
    #    be a change; the rail flag is absent while dark.
    me = client.get("/api/asclepius/auth/me", headers=doc_headers)
    assert me.status_code == 200
    assert "bank_link_enabled" not in me.json()

    # 4. mark-paid returns exactly the five keys it returned before the rail.
    _earning(doctor, "approved", ref="dark-1")
    marked = client.post(
        "/api/asclepius/admin/earnings/mark-paid",
        json={"payout_batch_id": "batch-dark-1", "user_id": doctor["id"]},
        headers=admin_headers)
    assert marked.status_code == 200
    assert set(marked.json()) == {"payout_batch_id", "marked", "amount_cents",
                                 "already_in_batch", "skipped"}
    assert marked.json()["marked"] == 1

    # 5. And so does pay, with no per-row transfer report bolted on.
    _earning(doctor, "approved", ref="dark-2")
    paid = client.post(
        "/api/asclepius/admin/earnings/pay",
        json={"payout_batch_id": "batch-dark-2", "user_id": doctor["id"]},
        headers=admin_headers)
    assert paid.status_code == 200
    assert set(paid.json()) == {"ok", "user_id", "payout_batch_id", "marked",
                                "amount_cents", "already_in_batch", "skipped", "totals"}
    assert "transfers" not in paid.json()

    # 6. Nothing reached for the SDK on any of those paths.
    assert sys.modules.get("stripe") is None or "stripe" not in sys.modules


def test_a_physician_with_no_bank_link_is_still_payable_while_dark():
    """WHY: C2's 409 is a NEW refusal, and a new refusal that fires while the
    rail is off would stop today's payouts on the day this merged.

    Nobody has a connected account yet, by construction, so the pay-time bank
    link check must not exist at all until the flag is on.
    """
    doctor = _doctor()
    admin = A.make_user(_store(), role="admin")
    assert not (doctor.get("stripe_account_id") or "")
    _earning(doctor, "approved", ref="dark-3")
    paid = client.post(
        "/api/asclepius/admin/earnings/pay",
        json={"payout_batch_id": "batch-dark-3", "user_id": doctor["id"]},
        headers=A.headers_for(admin))
    assert paid.status_code == 200, paid.text
    assert paid.json()["marked"] == 1


def test_module_imports_without_stripe_package(monkeypatch):
    """WHY: G6. The dependency is pinned but must not be load-bearing while dark.

    A production box that has not yet installed the new requirement, or one where
    the SDK import breaks, must still serve every existing endpoint. Blocking the
    import proves the property rather than relying on this machine happening not
    to have the package installed.
    """
    monkeypatch.setitem(sys.modules, "stripe", None)   # makes `import stripe` raise

    rail = importlib.reload(importlib.import_module("asclepius.stripe_rail"))
    assert rail.enabled() is False
    assert rail.COMING_SOON == "coming_soon"

    # The routers that USE the rail import cleanly too, since the SDK import
    # lives inside a function rather than at module scope.
    importlib.reload(importlib.import_module("routers.asclepius_payments"))

    # And when the flag is on without the package, the failure is loud and says
    # what to do, rather than a silent no-payout state.
    monkeypatch.setenv("ASCLEPIUS_STRIPE_ENABLED", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    with pytest.raises(rail.RailUnavailable) as raised:
        rail.sdk()
    assert "stripe package is not installed" in str(raised.value)


def test_flag_on_without_keys_fails_loudly(monkeypatch):
    """WHY: A2. A rail that is on but unconfigured must not degrade into a rail
    that silently pays nobody, which looks identical to a working one right up
    until a physician asks where their money is."""
    from asclepius import stripe_rail

    monkeypatch.setenv("ASCLEPIUS_STRIPE_ENABLED", "1")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(stripe_rail.RailUnavailable) as raised:
        stripe_rail.sdk()
    assert "STRIPE_SECRET_KEY" in str(raised.value)

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    with pytest.raises(stripe_rail.RailUnavailable) as raised:
        stripe_rail.sdk(need_webhook_secret=True)
    assert "STRIPE_WEBHOOK_SECRET" in str(raised.value)


def test_calling_the_rail_while_dark_raises_rather_than_half_working():
    """WHY: a caller that reached a Stripe call with the flag off has a bug in
    its own gate, and returning a usable client would hide it."""
    from asclepius import stripe_rail

    with pytest.raises(stripe_rail.RailUnavailable):
        stripe_rail.sdk()


def test_webhook_404_when_dark():
    """WHY: D1. A dark rail must not advertise a signature oracle.

    The body matters as much as the code: an attacker probing for a webhook
    endpoint should get the same answer they would get for a path that was never
    routed at all.
    """
    dark = client.post("/api/asclepius/stripe/webhook", content=b"{}",
                       headers={"stripe-signature": "whatever"})
    unrouted = client.post("/api/asclepius/stripe/webhook-that-never-existed",
                           content=b"{}")
    assert dark.status_code == 404
    assert dark.json() == unrouted.json() == {"detail": "Not Found"}


def test_retry_transfer_is_refused_while_dark():
    """WHY: C4's retry is admin-gated, so it says what is wrong rather than
    hiding, but it must still refuse to do anything while the rail is off."""
    doctor = _doctor()
    admin = A.make_user(_store(), role="admin")
    _earning(doctor, "paid", ref="dark-4")
    resp = client.post("/api/asclepius/admin/earnings/e-dark-4/retry-transfer",
                       headers=A.headers_for(admin))
    assert resp.status_code == 409
    assert "off" in resp.json()["detail"]
