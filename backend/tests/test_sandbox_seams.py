"""Sandbox PRD §1.4 (and §5's SANDBOX stamps) — the four realm-aware seams.

| Seam                         | Live      | Sandbox                                  |
|------------------------------|-----------|------------------------------------------|
| ``send_html_email``          | SendGrid  | ``sandbox_outbox`` row; never sends      |
| ``mark_paid`` (disbursement) | real      | 403 ``sandbox_no_disbursement``          |
| buyer delivery               | real      | 403 ``sandbox_no_delivery``; bundle +    |
|                              |           | datasheet stamped SANDBOX                |
| community loops              | run       | run, per realm (``realm.active_realms``) |
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

import email_utils  # noqa: E402
import realm  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import export as asc_export  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402

client = TestClient(A.app)


@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "sandbox-admin-secret")
    yield


@pytest.fixture
def sandbox_admin(sandbox_on):
    with realm.scoped("sandbox"):
        store = A.fresh_store()
        admin = A.make_user(store, role="admin")
        token = asc_auth.create_token(admin)
    return {"store": store, "admin": admin, "headers": {"Authorization": "Bearer " + token}}


# ─── Email → outbox ──────────────────────────────────────────────────────────
def test_sandbox_email_lands_in_outbox_and_never_sends(sandbox_on, monkeypatch):
    with realm.scoped("sandbox"):
        store = A.fresh_store()
        sent = {"n": 0}

        def _boom(*a, **k):  # any transport call is a failure of the seam
            sent["n"] += 1
            raise AssertionError("sandbox reached a real transport")

        monkeypatch.setattr(email_utils, "_normalize_sendgrid_api_key", _boom)
        html = ('<p>Your code is <b>482913</b>.</p>'
                '<p><a href="https://archangelhealth.ai/join?token=abc.def">Finish signing up</a></p>')
        ok, reason = asyncio.run(email_utils.send_html_email_with_reason(
            "sb-labeler-1@archangelhealth.ai", "Verify your email", html,
            attachments=[("dla.pdf", "application/pdf", b"%PDF-1.4 fake")]))
        assert (ok, reason) == (True, "sandbox_outbox")
        assert sent["n"] == 0
        rows = store.outbox_list()
        assert len(rows) == 1
        row = rows[0]
        assert row["to_email"] == "sb-labeler-1@archangelhealth.ai"
        assert row["subject"] == "Verify your email"
        assert row["codes"] == ["482913"]
        assert row["links"] == ["https://archangelhealth.ai/join?token=abc.def"]
        full = store.outbox_get(row["id"])
        assert "482913" in full["html"]
        assert full["attachments"] == [{"name": "dla.pdf", "mime": "application/pdf", "bytes": 13}]
        # The transport is always "configured" in the sandbox — onboarding
        # endpoints must not 503 there.
        assert email_utils.is_email_transport_configured() is True
        assert email_utils.active_email_vendor() == "sandbox"
        assert email_utils.email_phi_allowed() is True


def test_live_email_path_is_untouched():
    """Outside the sandbox the dev-mode short-circuit (the suite default) still
    answers — and writes nothing to any outbox."""
    store = A.fresh_store()
    ok, reason = asyncio.run(email_utils.send_html_email_with_reason("x@example.com", "s", "<p>111222</p>"))
    assert (ok, reason) == (True, "dev_mode")
    assert store.outbox_count() == 0


def test_code_and_link_extraction():
    codes, links = email_utils.extract_codes_and_links(
        '<p>Code: 123456 (again 123456) and AB12-CD34.</p>'
        '<a href="https://a.example/x?y=1">x</a> see https://b.example/reset. '
        'Not a code: 12345, 123456789, order-123456-x')
    assert codes == ["123456", "AB12-CD34"]
    assert links == ["https://a.example/x?y=1", "https://b.example/reset"]


# ─── Disbursement ────────────────────────────────────────────────────────────
def test_mark_paid_is_refused_in_sandbox_and_the_ledger_still_moves(sandbox_admin):
    store = sandbox_admin["store"]
    with realm.scoped("sandbox"):
        with pytest.raises(asc_payments.SandboxNoDisbursement):
            asc_payments.mark_paid(store, payout_batch_id="batch-1", actor_id="admin")
    r = client.post("/api/asclepius/admin/earnings/mark-paid", headers=sandbox_admin["headers"],
                    json={"payout_batch_id": "batch-1"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "sandbox_no_disbursement"
    # accrued → approved is NOT a disbursement and keeps working: the sweep
    # that auto-approves aged accruals runs in the sandbox without raising.
    from datetime import datetime
    with realm.scoped("sandbox"):
        moved = asc_payments._auto_approve(store, now=datetime.utcnow())
        assert isinstance(moved, int)


def test_mark_paid_in_live_hits_the_domain_rules_not_the_sandbox_guard():
    """In the live realm the sandbox guard never fires: the first thing a
    caller meets is the ledger's own validation."""
    store = A.fresh_store()
    with pytest.raises(asc_payments.PaymentsDenied):
        asc_payments.mark_paid(store, payout_batch_id="batch-live", actor_id="admin")


# ─── Buyer delivery ──────────────────────────────────────────────────────────
def test_buyer_delivery_is_refused_in_sandbox(sandbox_admin):
    r = client.post("/api/asclepius/admin/buyer-deliveries", headers=sandbox_admin["headers"],
                    json={"buyer_email": "buyer@example.com", "organizations": ["Org"],
                          "buyer_name": "B", "buyer_org": "Lab"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "sandbox_no_delivery"


def test_bundle_filename_and_datasheet_are_stamped_in_sandbox():
    assert asc_export.bundle_filename("exp-1") == "exp-1.zip"
    with realm.scoped("sandbox"):
        assert asc_export.bundle_filename("exp-1") == "SANDBOX-not-a-deliverable-exp-1.zip"
        md = asc_export._datasheet_md(export_id="exp-1", profile_name="p",
                                      counts={"total": 0, "by_type": {}, "by_specialty": {}},
                                      records=[], contributors=[])
        assert md.startswith("> **SANDBOX — not a deliverable.**")
    md_live = asc_export._datasheet_md(export_id="exp-1", profile_name="p",
                                       counts={"total": 0, "by_type": {}, "by_specialty": {}},
                                       records=[], contributors=[])
    assert "SANDBOX" not in md_live


def test_dla_pdf_header_is_stamped_in_sandbox():
    from asclepius import dla as asc_dla
    sig = {"typed_name": "Dr Test", "typed_title": "CMO", "signed_at": "2026-09-03T10:00:00Z",
           "signer_email": "t@example.com"}
    live_pdf = asc_dla.render_pdf(organization="Org", version=asc_dla.CURRENT_VERSION, signature=sig)
    assert b"SANDBOX" not in live_pdf
    with realm.scoped("sandbox"):
        sb_pdf = asc_dla.render_pdf(organization="Org", version=asc_dla.CURRENT_VERSION, signature=sig)
    assert b"SANDBOX" in sb_pdf
    assert b"test signature, not a real agreement" in sb_pdf


# ─── Loops iterate realms ────────────────────────────────────────────────────
def test_active_realms_follows_the_switch(monkeypatch):
    monkeypatch.delenv(realm.ADMIN_PASSWORD_VAR, raising=False)
    assert realm.active_realms() == ("live",)
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "x")
    assert realm.active_realms() == ("live", "sandbox")


def test_every_realm_iterating_loop_names_active_realms():
    """The loops the PRD lists (§1.3) each iterate ``realm.active_realms()``
    and enter ``realm.scoped``; the clinical loops in main deliberately do not
    and say why."""
    backend = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("asclepius/verification_agent.py", "community/notify.py", "community/digest.py",
                "community/events.py", "community/morning.py"):
        text = (backend / rel).read_text(encoding="utf-8")
        assert "_realm.active_realms()" in text, rel
        assert "_realm.scoped(" in text, rel
    main_text = (backend / "main.py").read_text(encoding="utf-8")
    assert main_text.count("_realm.active_realms()") >= 2   # assignment sweep + task-notify drain
    assert "run in the LIVE realm only" in main_text
