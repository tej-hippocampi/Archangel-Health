"""Upload gating for portal accounts, checked at every door.

The multipart door and the chunked door used to duplicate their preconditions
instead of sharing them, so a gate added to one was a bypass on the other. That
is exactly the change this feature makes, which is why every test here that
asserts a refusal asserts it three times.
"""
from __future__ import annotations

import base64
import io
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

_KEY = base64.urlsafe_b64encode(b"hs-gating-test-key-32-bytes-pad!").decode()
PASSWORD = "harbor-thistle-meadow-41"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("ENV", "test")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _client() -> TestClient:
    return TestClient(A.app, base_url="https://testserver")


def _account(approval_status, *, active=True):
    """A portal account in a chosen approval state, signed in."""
    store = _store()
    uname = "hs" + uuid.uuid4().hex[:10]
    hs = store.create_health_system_unclaimed("Gate Test " + uname, contact_email="it@test.org")
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"], password=PASSWORD,
                                email="it@test.org", must_reset=False,
                                approval_status=approval_status)
    if not active:
        store.set_hs_portal_active(uname, False)
    client = _client()
    r = client.post("/api/asclepius/hs/login", json={"username": uname, "password": PASSWORD})
    return client, uname, hs, r


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("case.json", '{"patient": "de-identified"}')
    return buf.getvalue()


def _try_all_three_doors(client):
    """Every write door, in the order a client would meet them."""
    return {
        "multipart": client.post(
            "/api/asclepius/hs/uploads",
            files={"files": ("case.zip", _zip_bytes(), "application/zip")}),
        "declare": client.post(
            "/api/asclepius/hs/uploads/sessions",
            json={"filename": "big.zip", "size": 50_000_000, "sha256": "b" * 64}),
        "part": client.put(
            "/api/asclepius/hs/uploads/sessions/whatever/parts/1", content=b"x" * 16),
    }


# ─── The zero-backfill guarantee ─────────────────────────────────────────────

def test_an_account_that_predates_approval_still_reaches_everything():
    """Every health system provisioned before this feature has approval_status
    NULL. If the collapse in hs_access flips, all of them lose upload on deploy
    and the first we hear of it is a support email."""
    client, uname, hs, login = _account(None)
    assert login.status_code == 200
    me = client.get("/api/asclepius/hs/me").json()
    assert me["account_state"] == "active"
    assert "upload" in me["surfaces"]
    # ...and not merely allowed by the rail: the door actually opens.
    r = client.post("/api/asclepius/hs/uploads",
                    files={"files": ("case.zip", _zip_bytes(), "application/zip")})
    assert r.status_code == 200, r.text
    # Nor is it ambushed by an intake form it has no reason to fill in.
    assert me["intake_needed"] is False


# ─── Pending ─────────────────────────────────────────────────────────────────

def test_a_pending_account_is_refused_at_all_three_upload_doors():
    client, uname, hs, _ = _account("pending")
    results = _try_all_three_doors(client)
    for door, r in results.items():
        assert r.status_code == 403, f"{door} let a pending account through ({r.status_code})"
    details = {r.json().get("detail") for r in results.values()}
    assert len(details) == 1, f"the doors refuse differently: {details}"
    assert "review" in next(iter(details)).lower()


def test_a_pending_account_still_gets_the_rest_of_the_portal():
    client, uname, hs, _ = _account("pending")
    me = client.get("/api/asclepius/hs/me").json()
    assert me["account_state"] == "in review"
    assert sorted(me["surfaces"]) == ["account", "intake", "payouts"]
    # An empty product at the moment someone signs up is worse than a locked
    # tile they understand, so these must be reachable.
    assert client.get("/api/asclepius/hs/payouts").status_code == 200
    assert client.get("/api/asclepius/hs/intake").status_code == 200


# ─── Rejected and deactivated ────────────────────────────────────────────────

def test_a_rejected_account_cannot_even_sign_in_and_looks_like_a_bad_password():
    store = _store()
    uname = "hs" + uuid.uuid4().hex[:10]
    hs = store.create_health_system_unclaimed("Rejected " + uname)
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"], password=PASSWORD,
                                must_reset=False, approval_status="pending")
    store.set_hs_approval(uname, "rejected", by="admin@x.com", reason="not who they said")
    store.set_hs_portal_active(uname, False)

    client = _client()
    rejected = client.post("/api/asclepius/hs/login",
                           json={"username": uname, "password": PASSWORD})
    wrong_pw = client.post("/api/asclepius/hs/login",
                           json={"username": uname, "password": "definitely-not-it-x"})
    assert rejected.status_code == wrong_pw.status_code == 401
    assert rejected.json() == wrong_pw.json(), "a rejection is distinguishable from a typo"


# ─── Approval opens the door, in the same session ────────────────────────────

def test_approving_opens_upload_without_signing_them_out():
    client, uname, hs, _ = _account("pending")
    assert client.post("/api/asclepius/hs/uploads",
                       files={"files": ("c.zip", _zip_bytes(), "application/zip")}
                       ).status_code == 403

    admin = A.make_user(_store(), role="admin")
    r = TestClient(A.app).post(
        f"/api/asclepius/admin/health-systems/{hs['hs_id']}/accounts/{uname}/approve",
        json={"purpose": "task_creation"}, headers=A.headers_for(admin))
    assert r.status_code == 200, r.text

    # Same cookie, no re-login: approval must not cost them their session.
    me = client.get("/api/asclepius/hs/me").json()
    assert me["account_state"] == "active"
    assert "upload" in me["surfaces"]
    assert client.post("/api/asclepius/hs/uploads",
                       files={"files": ("c.zip", _zip_bytes(), "application/zip")}
                       ).status_code == 200


def test_approval_requires_a_destination():
    """A self-signup arrives with none set, and approval is the only moment
    anyone is looking at the account."""
    client, uname, hs, _ = _account("pending")
    admin = A.make_user(_store(), role="admin")
    r = TestClient(A.app).post(
        f"/api/asclepius/admin/health-systems/{hs['hs_id']}/accounts/{uname}/approve",
        json={"purpose": "whatever-i-like"}, headers=A.headers_for(admin))
    assert r.status_code == 400


def test_rejecting_deactivates_and_records_who_and_why():
    client, uname, hs, _ = _account("pending")
    admin = A.make_user(_store(), role="admin")
    r = TestClient(A.app).post(
        f"/api/asclepius/admin/health-systems/{hs['hs_id']}/accounts/{uname}/reject",
        json={"reason": "could not confirm they work there"}, headers=A.headers_for(admin))
    assert r.status_code == 200
    row = _store().get_hs_portal_user(uname)
    assert row["approval_status"] == "rejected"
    assert row["active"] == 0
    # Three columns, because a decision that cannot be attributed cannot be appealed.
    assert row["decision_reason"] == "could not confirm they work there"
    assert row["approved_by"] == admin["email"]
    assert row["approved_at"]


def test_the_review_queue_flags_a_name_collision():
    """Signup refuses to merge by organization name, so a duplicate is either a
    second contact at a real partner or somebody typing a hospital's name who
    does not work there. The operator has to be shown which."""
    store = _store()
    incumbent = store.ensure_health_system("Sinai Health", contact_email="it@sinai.org")
    uname = "sinai" + uuid.uuid4().hex[:6]
    dupe = store.create_health_system_unclaimed("Sinai Health")
    store.create_hs_portal_user(username=uname, hs_id=dupe["hs_id"], password=PASSWORD,
                                must_reset=False, approval_status="pending",
                                signup_source="self_serve")

    admin = A.make_user(store, role="admin")
    r = TestClient(A.app).get("/api/asclepius/admin/health-system-signups",
                              headers=A.headers_for(admin))
    assert r.status_code == 200
    row = next(p for p in r.json()["pending"] if p["username"] == uname)
    assert [c["hs_id"] for c in row["name_collisions"]] == [incumbent["hs_id"]]


# ─── The gate is structural, not remembered ──────────────────────────────────

def test_every_hs_write_door_names_the_upload_surface():
    """A fifth upload door added later must have to name a surface before it
    compiles, rather than relying on somebody remembering the check."""
    import inspect

    from routers import asclepius_provider as P

    doors = [P.hs_upload, P.hs_upload_declare, P.hs_upload_part, P.hs_upload_complete]
    for fn in doors:
        dep = inspect.signature(fn).parameters["portal_user"].default
        # Depends(require_hs_surface(UPLOAD)) closes over the surface name.
        closure = getattr(dep.dependency, "__closure__", None) or ()
        surfaces = [c.cell_contents for c in closure if isinstance(c.cell_contents, str)]
        assert "upload" in surfaces, f"{fn.__name__} is not gated on the upload surface"
