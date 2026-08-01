"""PRD C Phase 1 — health systems: schema, provisioning, backfill, admin list.

Covers the admin side of the health-system flow: username derivation +
collision suffixing, create-or-reuse by organization name, the two-field
provision endpoint (password emailed once / stored hashed only / must_reset),
and the boot-time backfill that adopts historical partner uploads.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius.store import verify_password  # noqa: E402
from routers import asclepius_admin as R  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_headers(store):
    admin = A.make_user(store, role="admin")
    return A.headers_for(admin)


@pytest.fixture()
def email_ok(monkeypatch):
    """Pretend email transport is configured and capture what would be sent."""
    sent = []

    async def _fake_send(to, subject, html, **kw):
        sent.append({"to": to, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(R, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(R, "send_html_email", _fake_send)
    return sent


# ─── Username derivation ─────────────────────────────────────────────────────
def test_username_derives_from_org_name():
    assert R.derive_hs_username("Mass General Hospital") == "massgeneral"
    assert R.derive_hs_username("Mercy Health") == "mercy"
    # Stopwords never strip the name to nothing — fall back to all words.
    assert R.derive_hs_username("University Health System") == "universityhealthsyst"
    assert R.derive_hs_username("") == "partner"
    assert R.derive_hs_username("!!!") == "partner"


def test_username_collision_suffixes():
    store = _store()
    hs = store.ensure_health_system("Mass General Hospital")
    store.create_hs_portal_user(username="massgeneral", hs_id=hs["hs_id"], password="x" * 12)
    assert R.unique_hs_username(store, "massgeneral") == "massgeneral2"
    store.create_hs_portal_user(username="massgeneral2", hs_id=hs["hs_id"], password="x" * 12)
    assert R.unique_hs_username(store, "massgeneral") == "massgeneral3"


# ─── Health system create-or-reuse ───────────────────────────────────────────
def test_ensure_health_system_reuses_by_name_case_insensitive():
    store = _store()
    a = store.ensure_health_system("Mass General Hospital", contact_email="a@mgh.org")
    b = store.ensure_health_system("  mass   general HOSPITAL ")
    assert a["hs_id"] == b["hs_id"]
    assert len(store.list_health_systems()) == 1
    # The id is the contract shape hs-<slug>-<6hex>.
    assert a["hs_id"].startswith("hs-")


# ─── Provision endpoint ──────────────────────────────────────────────────────
def test_provision_creates_account_and_emails_once(email_ok):
    store = _store()
    res = client.post(
        "/api/asclepius/admin/health-systems/provision",
        json={"organization": "Mass General Hospital", "email": "data@mgh.harvard.edu"},
        headers=_admin_headers(store),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["username"] == "massgeneral"
    assert body["health_system"]["name"] == "Mass General Hospital"

    # Exactly one email, containing the username and a passphrase…
    assert len(email_ok) == 1
    assert email_ok[0]["to"] == "data@mgh.harvard.edu"
    assert "massgeneral" in email_ok[0]["html"]

    # …and the password exists only as a hash. must_reset forces a change.
    row = store.get_hs_portal_user("massgeneral")
    assert row is not None
    assert row["must_reset"] == 1
    assert row["password_hash"]
    # The emailed passphrase verifies against the stored hash and never appears
    # in the row itself.
    import re as _re
    # the passphrase is the second <code> cell (the first is the username)
    codes = _re.findall(r"<code>([^<]+)</code>", email_ok[0]["html"])
    passphrase = codes[1]
    assert verify_password(passphrase, row["password_hash"])
    assert passphrase not in str(row)


def test_provision_rotates_existing_account_instead_of_minting_second(email_ok):
    store = _store()
    headers = _admin_headers(store)
    r1 = client.post("/api/asclepius/admin/health-systems/provision",
                     json={"organization": "Mercy Health", "email": "it@mercy.org"},
                     headers=headers)
    assert r1.status_code == 200
    first_hash = store.get_hs_portal_user(r1.json()["username"])["password_hash"]

    r2 = client.post("/api/asclepius/admin/health-systems/provision",
                     json={"organization": "Mercy Health", "email": "it@mercy.org"},
                     headers=headers)
    assert r2.status_code == 200
    assert r2.json()["username"] == r1.json()["username"]

    hs_id = r1.json()["health_system"]["hs_id"]
    users = store.list_hs_portal_users(hs_id)
    assert len(users) == 1                      # rotated, not duplicated
    row = store.get_hs_portal_user(r1.json()["username"])
    assert row["password_hash"] != first_hash   # password actually rotated
    assert row["must_reset"] == 1


def test_provision_requires_admin(email_ok):
    store = _store()
    evaluator = A.make_user(store, role="evaluator")
    res = client.post("/api/asclepius/admin/health-systems/provision",
                      json={"organization": "X Health", "email": "a@b.org"},
                      headers=A.headers_for(evaluator))
    assert res.status_code in (401, 403)
    res2 = client.post("/api/asclepius/admin/health-systems/provision",
                       json={"organization": "X Health", "email": "a@b.org"})
    assert res2.status_code in (401, 403)
    assert email_ok == []


def test_provision_503_when_email_unconfigured(monkeypatch):
    store = _store()
    monkeypatch.setattr(R, "is_email_transport_configured", lambda: False)
    res = client.post("/api/asclepius/admin/health-systems/provision",
                      json={"organization": "X Health", "email": "a@b.org"},
                      headers=_admin_headers(store))
    assert res.status_code == 503
    # Nothing half-created without a way to tell the recipient.
    assert store.list_hs_portal_users() == []


# ─── Backfill ────────────────────────────────────────────────────────────────
def test_backfill_adopts_historical_partner_uploads():
    store = _store()
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO data_providers (provider_id, email, org_name, created_at, updated_at) "
            "VALUES ('u-legacy', 'it@stluke.org', 'St Luke Medical Center', '2025-01-01', '2025-01-01')"
        )
        conn.execute(
            "INSERT INTO ingest_uploads (upload_id, link_id, partner_id, status, created_at, updated_at) "
            "VALUES ('upl-legacy', 'account', 'u-legacy', 'ingested', '2025-01-01', '2025-01-01')"
        )
    store._migrate()

    up = store.get_ingest_upload("upl-legacy")
    assert up["health_system_id"], "historical upload was not adopted"
    names = {h["name"] for h in store.list_health_systems()}
    assert "St Luke Medical Center" in names

    # Idempotent: a second boot neither duplicates nor re-stamps.
    before = up["health_system_id"]
    store._migrate()
    assert store.get_ingest_upload("upl-legacy")["health_system_id"] == before
    assert len([h for h in store.list_health_systems()
                if h["name"] == "St Luke Medical Center"]) == 1


# ─── Admin list ──────────────────────────────────────────────────────────────
def test_admin_list_health_systems(email_ok):
    store = _store()
    headers = _admin_headers(store)
    client.post("/api/asclepius/admin/health-systems/provision",
                json={"organization": "Mass General Hospital", "email": "data@mgh.harvard.edu"},
                headers=headers)
    res = client.get("/api/asclepius/admin/health-systems", headers=headers)
    assert res.status_code == 200
    rows = res.json()["health_systems"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Mass General Hospital"
    assert row["portal_users"][0]["username"] == "massgeneral"
    assert row["uploads_count"] == 0
    assert row["physicians_linked"] == 0
