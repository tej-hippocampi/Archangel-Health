"""PRD-3 — auth hardening: token revocation / logout + opt-in TOTP MFA."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TEAM_DB_PATH"] = os.path.join(tempfile.gettempdir(), f"authhard_{uuid.uuid4().hex}.db")
os.environ["ADMIN_USERNAME"] = "testadmin"
os.environ["ADMIN_PASSWORD"] = "testadminpass"

import pyotp  # noqa: E402

import auth as auth_module  # noqa: E402
# Keep the landing user store out of the real backend/auth_users.json.
auth_module.USERS_FILE = Path(os.environ["TEAM_DB_PATH"] + ".users.json")

import token_revocation  # noqa: E402
from main import app  # noqa: E402
from tenant_jwt import create_tenant_staff_token, decode_tenant_staff_token  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _register_and_login(client, email=None, password="pw12345678"):
    email = email or f"u_{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/auth/register", json={"email": email, "password": password, "name": "U"})
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return email, r.json()


# ─── Token revocation / logout ───────────────────────────────────────────────

def test_landing_logout_revokes_token(client):
    _, data = _register_and_login(client)
    token = data["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=h).status_code == 200
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    # The same token is now rejected.
    assert client.get("/api/auth/me", headers=h).status_code == 401


def test_tenant_token_revocation(client):
    token = create_tenant_staff_token(
        email="rn@hs.com", name="RN", role="rn_coordinator",
        health_system_id="hs1", tenant_slug="t", health_system_code="C",
    )
    assert decode_tenant_staff_token(token) is not None
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert decode_tenant_staff_token(token) is None


def test_admin_logout_revokes_token(client):
    login = client.post("/admin/auth/login",
                        json={"username": "testadmin", "password": "testadminpass"})
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/admin/stats", headers=h).status_code == 200
    assert client.post("/admin/auth/logout", headers=h).status_code == 200
    assert client.get("/admin/stats", headers=h).status_code == 401


def test_tokens_carry_jti(client):
    from jose import jwt
    _, data = _register_and_login(client)
    payload = jwt.decode(data["access_token"], auth_module.AUTH_SECRET,
                         algorithms=[auth_module.ALGORITHM])
    assert payload.get("jti")


# ─── MFA (TOTP) enroll → enforce → login → disable ───────────────────────────

def test_mfa_full_roundtrip(client):
    email, data = _register_and_login(client)
    h = {"Authorization": f"Bearer {data['access_token']}"}

    # Enroll → confirm with a real TOTP code.
    enroll = client.post("/api/auth/mfa/enroll", headers=h)
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    assert enroll.json()["otpauth_uri"].startswith("otpauth://totp/")

    code = pyotp.TOTP(secret).now()
    verify = client.post("/api/auth/mfa/verify", json={"code": code}, headers=h)
    assert verify.status_code == 200 and verify.json()["mfa_enabled"] is True

    # Now login returns an MFA challenge, not a token.
    login = client.post("/api/auth/login", json={"email": email, "password": "pw12345678"})
    assert login.status_code == 200
    body = login.json()
    assert body.get("mfa_required") is True
    assert "access_token" not in body
    mfa_token = body["mfa_token"]

    # Wrong code is rejected.
    bad = client.post("/api/auth/mfa/login", json={"mfa_token": mfa_token, "code": "000000"})
    assert bad.status_code == 401

    # Correct code completes login.
    good = client.post("/api/auth/mfa/login",
                       json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()})
    assert good.status_code == 200, good.text
    assert good.json().get("access_token")

    # Disable MFA → login returns a token directly again.
    h2 = {"Authorization": f"Bearer {good.json()['access_token']}"}
    dis = client.post("/api/auth/mfa/disable",
                      json={"code": pyotp.TOTP(secret).now()}, headers=h2)
    assert dis.status_code == 200 and dis.json()["mfa_enabled"] is False
    relogin = client.post("/api/auth/login", json={"email": email, "password": "pw12345678"})
    assert relogin.json().get("access_token")


def test_require_staff_mfa_blocks_unenrolled(client, monkeypatch):
    email, _ = _register_and_login(client)
    monkeypatch.setenv("REQUIRE_STAFF_MFA", "1")
    r = client.post("/api/auth/login", json={"email": email, "password": "pw12345678"})
    assert r.status_code == 403
    assert "MFA" in r.json()["detail"]


def test_mfa_status_endpoint(client):
    _, data = _register_and_login(client)
    h = {"Authorization": f"Bearer {data['access_token']}"}
    assert client.get("/api/auth/mfa/status", headers=h).json() == {"mfa_enabled": False}
