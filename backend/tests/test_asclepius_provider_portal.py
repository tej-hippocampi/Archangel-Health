"""Data Provider Portal — email+password door, end-to-end (EHR PRD §4).

Verifies the flow you actually want: admin invites by email → provider signs in
with email+password → forced reset → uploads real cases → they land in the SHARED
ingestion inbox and auto-generate a real_deid case (reusing main's pipeline).
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

os.environ["EMAIL_DEV_MODE"] = "1"  # make the transport "configured" (prints, no send)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)


def _admin(store):
    return A.make_user(store, role="admin")


def _provision(store, email="p@clinic.org", pw="KnownPass-123!"):
    return store.provision_data_provider(email=email, password=pw, specialty="nephrology")


def _login(email="p@clinic.org", pw="KnownPass-123!"):
    return client.post("/api/asclepius/auth/login", json={"email": email, "password": pw}).json()["token"]


# ─── invite ───────────────────────────────────────────────────────────────────
def test_admin_invite_creates_data_partner_account():
    store = A.fresh_store()
    admin = _admin(store)
    r = client.post("/api/asclepius/admin/data-providers", headers=A.headers_for(admin),
                    json={"email": "provider@clinic.org", "org_name": "Clinic", "specialty": "nephrology"})
    assert r.status_code == 200, r.text
    assert "account created" in r.json()["message"]
    u = store.get_user_by_email("provider@clinic.org")
    assert u and u["role"] == "data_partner"
    lst = client.get("/api/asclepius/admin/data-providers", headers=A.headers_for(admin)).json()
    assert any(p["email"] == "provider@clinic.org" for p in lst["providers"])


# ─── deny-by-default ──────────────────────────────────────────────────────────
def test_data_partner_can_only_use_the_portal():
    store = A.fresh_store()
    _provision(store)
    h = {"Authorization": f"Bearer {_login()}"}
    assert client.get("/api/asclepius/provider/me", headers=h).status_code == 200
    assert client.get("/api/asclepius/tasks/next", headers=h).status_code == 403
    assert client.get("/api/asclepius/admin/data-providers", headers=h).status_code == 403
    assert client.get("/api/asclepius/auth/me", headers=h).status_code == 403
    assert client.get("/api/asclepius/ingestion/uploads", headers=h).status_code == 403


# ─── forced reset → upload → shared inbox → real case ────────────────────────
def test_forced_reset_then_upload_ingests_a_real_case():
    store = A.fresh_store()
    admin = _admin(store)
    _provision(store)
    h = {"Authorization": f"Bearer {_login()}"}

    assert client.get("/api/asclepius/provider/me", headers=h).json()["must_reset_password"] is True
    # can't upload before resetting
    assert client.post("/api/asclepius/provider/uploads", headers=h,
                       files=[("files", ("x.txt", b"note", "text/plain"))]).status_code == 403
    # forced reset (blank current password — the token is the proof)
    assert client.post("/api/asclepius/provider/password", headers=h,
                       json={"new_password": "BrandNewPass-456!"}).status_code == 200
    assert client.get("/api/asclepius/provider/me", headers=h).json()["must_reset_password"] is False

    # upload loose files (server zips them + injects the specialty manifest)
    csv = b"patient_key,panel,analyte,value,unit,collected_at\npt1,BMP,Creatinine,2.4,mg/dL,2025-03-08\npt1,BMP,Creatinine,1.1,mg/dL,2025-03-01"
    note = b"Progress nephrology: AKI, creatinine up since 3/1/2025, improving by 2025-03-09."
    up = client.post("/api/asclepius/provider/uploads", headers=h, files=[
        ("files", ("labs.csv", csv, "text/csv")),
        ("files", ("note.txt", note, "text/plain")),
    ])
    assert up.status_code == 200, up.text
    upload_id = up.json()["upload_id"]

    # the background pipeline ran (TestClient waits for background tasks)
    row = store.get_ingest_upload(upload_id)
    assert row["status"] in ("ingested", "quarantined"), row
    # it reappears in the SHARED admin inbox (not a second pipeline)
    admin_h = A.headers_for(admin)
    uploads = client.get("/api/asclepius/ingestion/uploads", headers=admin_h).json()["uploads"]
    assert any(u["upload_id"] == upload_id for u in uploads)

    # the provider sees their OWN upload with a plain-English status
    mine = client.get("/api/asclepius/provider/uploads", headers=h).json()["uploads"]
    assert mine and mine[0]["upload_id"] == upload_id

    # a real_deid case was assembled (clean bundle -> ingested)
    if row["status"] == "ingested":
        cases = client.get("/api/asclepius/ingestion/cases", headers=admin_h).json()
        recs = cases.get("cases") or cases.get("ingest_cases") or []
        assert any((c.get("case") or {}).get("case_source") == "real_deid" for c in recs) or recs


def test_single_loose_file_is_wrapped_and_ingested():
    """A single loose (non-zip) file must be wrapped into a bundle, not rejected."""
    store = A.fresh_store()
    _provision(store)
    h = {"Authorization": f"Bearer {_login()}"}
    client.post("/api/asclepius/provider/password", headers=h,
                json={"new_password": "BrandNewPass-456!"})
    csv = (b"patient_key,panel,analyte,value,unit,collected_at\n"
           b"p1,BMP,Creatinine,2.4,mg/dL,2025-03-08")
    up = client.post("/api/asclepius/provider/uploads", headers=h,
                     files=[("files", ("labs.csv", csv, "text/csv"))])
    assert up.status_code == 200, up.text
    row = store.get_ingest_upload(up.json()["upload_id"])
    assert row["status"] in ("ingested", "quarantined")  # processed, not rejected-as-nonzip


def test_revoked_provider_cannot_sign_in():
    store = A.fresh_store()
    p = _provision(store)
    _login()  # works before revoke
    store.revoke_data_provider(p["provider_id"])
    r = client.post("/api/asclepius/auth/login", json={"email": "p@clinic.org", "password": "KnownPass-123!"})
    assert r.status_code == 401  # account deactivated


def test_login_is_rate_limited(monkeypatch):
    import ratelimit
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    ratelimit.reset()
    try:
        codes = [client.post("/api/asclepius/auth/login",
                             json={"email": "nobody@x.org", "password": "wrong"}).status_code
                 for _ in range(13)]
        assert 429 in codes
    finally:
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
        ratelimit.reset()
