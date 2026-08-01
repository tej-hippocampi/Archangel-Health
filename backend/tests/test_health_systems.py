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


# ─── Detail: pipeline buckets (Phase 2) ──────────────────────────────────────
def _mk_upload(store, hs_id: str, filename: str) -> str:
    up = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs_id, filename=filename,
        sha256="0" * 64, size_bytes=1234, raw_path=None, source_ip=None)
    store.set_upload_health_system(up["upload_id"], hs_id)
    return up["upload_id"]


def test_detail_buckets_follow_workflow_order():
    store = _store()
    headers = _admin_headers(store)
    hs = store.ensure_health_system("Bucket Health")
    hs_id = hs["hs_id"]

    up_clean = _mk_upload(store, hs_id, "clean.zip")
    store.insert_ingest_case(upload_id=up_clean, patient_key="p1", specialty="nephrology",
                             case={"x": 1}, status="ingested", report=None)

    up_held = _mk_upload(store, hs_id, "held.zip")
    ic = store.insert_ingest_case(upload_id=up_held, patient_key="p2", specialty="nephrology",
                                  case={"x": 2}, status="ingested", report=None)
    store.hold_ingest_case_for_review(ic["ingest_case_id"], "phi_unverified",
                                      "unverified burned-in PHI", severity="blocking")

    up_live = _mk_upload(store, hs_id, "live.zip")
    store.insert_ingest_case(upload_id=up_live, patient_key="p3", specialty="nephrology",
                             case={"x": 3}, status="promoted", report=None)

    up_fresh = _mk_upload(store, hs_id, "fresh.zip")   # no cases yet — not examined

    res = client.get(f"/api/asclepius/admin/health-systems/{hs_id}", headers=headers)
    assert res.status_code == 200, res.text
    b = res.json()["buckets"]
    ids = {k: {e["upload_id"] for e in v} for k, v in b.items()}

    assert up_clean in ids["ready_to_promote"]
    assert up_held in ids["needs_attention"]
    assert up_live in ids["in_production"]
    assert up_fresh in ids["needs_review"]
    # A safety hold is never buried in a normal bucket's entry list silently —
    # the reason travels with it.
    held_entry = next(e for e in b["needs_attention"] if e["upload_id"] == up_held)
    assert any("PHI" in r for r in held_entry["reasons"])
    # The fresh upload appears ONLY in needs_review.
    assert up_fresh not in ids["needs_attention"] | ids["ready_to_promote"] | ids["in_production"]


def test_detail_404_for_unknown_health_system():
    store = _store()
    res = client.get("/api/asclepius/admin/health-systems/hs-nope-000000",
                     headers=_admin_headers(store))
    assert res.status_code == 404


# ─── Physicians (Phase 3) ────────────────────────────────────────────────────
def test_physicians_roster_renders_before_prd_b_merges():
    """PRD-B owns the tier/verification columns. Before B merges they simply do
    not exist — the roster must render every physician as Unassigned / not
    checked instead of crashing."""
    store = _store()
    headers = _admin_headers(store)
    A.make_user(store, role="evaluator", specialty="nephrology")
    A.make_user(store, role="evaluator")
    A.make_user(store, role="qa_reviewer")          # not a physician
    store.ensure_mock_user(email="mock@asclepius.example.com", password="pw-12345678")

    res = client.get("/api/asclepius/admin/physicians", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["all"] == 2               # admin, qa, mock excluded
    assert body["counts"]["unassigned"] == 2
    assert body["counts"]["pending"] == 0
    for p in body["physicians"]:
        assert p["tier"] is None                    # renders as "Unassigned"
        assert p["verification_status"] is None     # renders as "Not checked"
        assert p["slack_joined"] is None            # tri-state survives as null


def _add_prd_b_columns(store):
    with store._conn() as conn:
        for ddl in ("ALTER TABLE users ADD COLUMN tier TEXT",
                    "ALTER TABLE users ADD COLUMN verification_status TEXT",
                    "ALTER TABLE users ADD COLUMN phone TEXT",
                    "ALTER TABLE users ADD COLUMN slack_joined INTEGER",
                    "ALTER TABLE users ADD COLUMN health_system_id TEXT"):
            try:
                conn.execute(ddl)
            except Exception:
                pass  # PRD-B merged and created it already — even better


def test_physicians_roster_counts_with_prd_b_columns():
    store = _store()
    headers = _admin_headers(store)
    _add_prd_b_columns(store)
    hs = store.ensure_health_system("Roster Health")
    u1 = A.make_user(store, role="evaluator")
    u2 = A.make_user(store, role="evaluator")
    u3 = A.make_user(store, role="evaluator")
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier='labeler', verification_status='approved', "
                     "slack_joined=1, health_system_id=? WHERE id=?", (hs["hs_id"], u1["id"]))
        conn.execute("UPDATE users SET tier='reviewer', verification_status='pending' "
                     "WHERE id=?", (u2["id"],))
        conn.execute("UPDATE users SET verification_status='pending' WHERE id=?", (u3["id"],))

    body = client.get("/api/asclepius/admin/physicians", headers=headers).json()
    c = body["counts"]
    assert (c["all"], c["pending"], c["labelers"], c["reviewers"], c["unassigned"]) == (3, 2, 1, 1, 1)
    by_id = {p["id"]: p for p in body["physicians"]}
    assert by_id[u1["id"]]["health_system_name"] == "Roster Health"
    assert by_id[u1["id"]]["slack_joined"] is True
    assert by_id[u2["id"]]["health_system_name"] is None
    assert by_id[u3["id"]]["tier"] is None          # pending but unassigned — distinct facts


def test_physician_profile_histories_are_defensive():
    store = _store()
    headers = _admin_headers(store)
    doc = A.make_user(store, role="evaluator", specialty="nephrology")

    res = client.get(f"/api/asclepius/admin/physicians/{doc['id']}", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["physician"]["name"]
    assert body["review_history"] == []             # case_reviews absent pre-PRD-A → [], not 500
    assert body["task_history"] == []
    assert body["npi_payload"] is None

    # Once PRD-A's table exists, the same call returns the reviewer's rows.
    with store._conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS case_reviews (
            review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            submission_id TEXT NOT NULL, reviewer_user_id TEXT NOT NULL,
            reviewer_id_hashed TEXT NOT NULL, verdict TEXT NOT NULL,
            dimension_json TEXT, corrections_json TEXT, reviewer_notes TEXT,
            time_spent_sec INTEGER, blinded INTEGER, created_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO case_reviews (review_id, task_id, submission_id, "
                     "reviewer_user_id, reviewer_id_hashed, verdict, created_at) "
                     "VALUES ('rev-1', 't-1', 's-1', ?, 'h1', 'accept', '2026-01-01')",
                     (doc["id"],))
    body2 = client.get(f"/api/asclepius/admin/physicians/{doc['id']}", headers=headers).json()
    assert len(body2["review_history"]) == 1
    assert body2["review_history"][0]["verdict"] == "accept"


def test_physician_profile_404_for_non_physician():
    store = _store()
    headers = _admin_headers(store)
    admin = A.make_user(store, role="admin")
    assert client.get(f"/api/asclepius/admin/physicians/{admin['id']}",
                      headers=headers).status_code == 404
    assert client.get("/api/asclepius/admin/physicians/u-nope",
                      headers=headers).status_code == 404


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
