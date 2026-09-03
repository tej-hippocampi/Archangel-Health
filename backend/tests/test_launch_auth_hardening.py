"""Launch-night auth hardening: token-type confusion + the production sandbox.

Two findings, both confirmed against the source before these tests were written.
Each test here FAILS on the pre-fix code.

1. MFA was optional. ``create_mfa_pending_token`` mints a pre-auth token after
   the password step and BEFORE TOTP; ``_decode_token`` returned ``sub`` without
   ever reading ``typ``, so that token was accepted anywhere a full session token
   was. Anyone who could finish step one skipped the second factor.

2. ``ensure_mock_contributor`` provisioned a login-capable evaluator account on
   EVERY boot, production included, guarded by a password published in this repo.
   The V4 real-case gate was already correct, but the account itself still
   authenticated on the live portal.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import jwt
import pyotp
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same isolation as tests/test_auth_hardening.py: keep the landing user store and
# the revocation table out of the real backend files.
os.environ["TEAM_DB_PATH"] = os.path.join(tempfile.gettempdir(), f"launchauth_{uuid.uuid4().hex}.db")
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testadminpass")

from tests import _asclepius as A  # noqa: E402
import auth as auth_module  # noqa: E402

auth_module.USERS_FILE = Path(os.environ["TEAM_DB_PATH"] + ".users.json")

from asclepius import auth as asc_auth  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(A.app) as c:
        yield c


def _register_verified(client, password="pw12345678"):
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/auth/register", json={"email": email, "password": password, "name": "U"})
    # These tests exercise token type, not the email-verification gate.
    auth_module.mark_email_verified(email)
    return email, password


def _enroll_mfa(client, token) -> str:
    """Turn on TOTP for the signed-in user; returns the shared secret."""
    h = {"Authorization": f"Bearer {token}"}
    secret = client.post("/api/auth/mfa/enroll", headers=h).json()["secret"]
    r = client.post("/api/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()}, headers=h)
    assert r.status_code == 200, r.text
    return secret


# ─── Finding 1: the MFA pre-auth token is not a session token ─────────────────

def test_mfa_pending_token_is_refused_by_the_session_decode():
    """The narrowest statement of the bypass: the pre-auth token must not resolve
    to a subject through the decode every authenticated route depends on."""
    pending = auth_module.create_mfa_pending_token("doctor@example.com")
    # It is a valid, unexpired, correctly signed token for its own purpose...
    assert auth_module.decode_mfa_pending_token(pending) == "doctor@example.com"
    # ...and it is not a session.
    assert auth_module._decode_token(pending) is None  # noqa: SLF001


def test_the_second_factor_is_not_optional_end_to_end(client):
    """The bypass as an attacker would run it: complete the password step, then
    present the challenge token as a Bearer token instead of answering it."""
    email, password = _register_verified(client)
    first = client.post("/api/auth/login", json={"email": email, "password": password})
    _enroll_mfa(client, first.json()["access_token"])

    # Password step only. The response is a challenge, not a session.
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.json() == {"mfa_required": True, "mfa_token": r.json()["mfa_token"]}
    mfa_token = r.json()["mfa_token"]

    h = {"Authorization": f"Bearer {mfa_token}"}
    assert client.get("/api/auth/me", headers=h).status_code == 401, (
        "the pre-auth token authenticated a session; the second factor is skippable"
    )


def test_the_real_mfa_flow_still_completes(client):
    """The fix must not break the one caller that is SUPPOSED to take this token."""
    email, password = _register_verified(client)
    first = client.post("/api/auth/login", json={"email": email, "password": password})
    secret = _enroll_mfa(client, first.json()["access_token"])

    mfa_token = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    ).json()["mfa_token"]
    r = client.post("/api/auth/mfa/login",
                    json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=h).status_code == 200


def test_session_tokens_are_stamped_positively(client):
    """Positive typing, not a blacklist of the one known-bad value: a token type
    added tomorrow carries its own `typ` and is refused by default."""
    email, password = _register_verified(client)
    token = client.post("/api/auth/login",
                        json={"email": email, "password": password}).json()["access_token"]
    claims = jwt.decode(token, auth_module.AUTH_SECRET, algorithms=[auth_module.ALGORITHM])
    assert claims["typ"] == auth_module.TOKEN_TYPE_SESSION
    assert auth_module._decode_token(token) == email  # noqa: SLF001

    invented = jwt.encode(
        {"sub": email, "typ": "some_future_token", "jti": uuid.uuid4().hex,
         "exp": datetime.utcnow() + timedelta(minutes=30)},
        auth_module.AUTH_SECRET, algorithm=auth_module.ALGORITHM,
    )
    assert auth_module._decode_token(invented) is None  # noqa: SLF001


def test_already_issued_untyped_sessions_still_work(client):
    """Back-compat, deliberately narrow. Staff signed in before this deploy hold
    7-day tokens with no `typ`; invalidating them all mid-launch is its own
    outage. They carry the `jti` every session token has had since 2026-06-06,
    and that is what the grace window is keyed on."""
    email, _ = _register_verified(client)
    legacy = jwt.encode(
        {"sub": email, "jti": uuid.uuid4().hex,
         "exp": datetime.utcnow() + timedelta(days=3)},
        auth_module.AUTH_SECRET, algorithm=auth_module.ALGORITHM,
    )
    assert auth_module._decode_token(legacy) == email  # noqa: SLF001
    h = {"Authorization": f"Bearer {legacy}"}
    assert client.get("/api/auth/me", headers=h).status_code == 200

    # Untyped AND jti-less is not a session this module ever minted.
    forged = jwt.encode(
        {"sub": email, "exp": datetime.utcnow() + timedelta(days=3)},
        auth_module.AUTH_SECRET, algorithm=auth_module.ALGORITHM,
    )
    assert auth_module._decode_token(forged) is None  # noqa: SLF001


def test_a_tenant_token_is_not_a_landing_session():
    """Same secret, different plane. Positive typing closes this too."""
    from tenant_jwt import create_tenant_staff_token

    tok = create_tenant_staff_token(email="x@example.com", name="X", role="surgeon",
                                    health_system_id="hs1", tenant_slug="t",
                                    health_system_code="HS1CODE")
    assert auth_module._decode_token(tok) is None  # noqa: SLF001


# ─── Finding 2: no default-password sandbox account on production ─────────────

@pytest.fixture()
def prod(monkeypatch):
    """A production boot with no operator-set sandbox password."""
    A.fresh_store()
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("ASCLEPIUS_MOCK_PASSWORD", raising=False)
    from asclepius.store import get_store
    return get_store()


def test_production_boot_provisions_no_default_password_account(prod):
    """A fresh production deployment must not end up with a login-capable
    physician-shaped account behind a password published in this repo."""
    cfg = asc_auth.mock_credentials()
    assert asc_auth.ensure_mock_contributor(prod) is None
    assert prod.get_user_by_email(cfg["email"]) is None, "the account was created anyway"
    assert asc_auth.authenticate(prod, cfg["email"], cfg["password"]) is None


def test_production_boot_disarms_an_account_a_previous_boot_created(prod):
    """The half-fix trap. ``ensure_mock_user`` reset this password on every prior
    boot, so a production deployment that has run before already has the default
    credential on disk. Merely declining to create it would leave that login
    live, which is the whole finding."""
    cfg = asc_auth.mock_credentials()
    # Simulate the pre-fix boot that put the published password on the row.
    prod.ensure_mock_user(email=cfg["email"], password=cfg["password"],
                          specialty=cfg["specialty"], board_cert=cfg["board_cert"],
                          years_experience=cfg["years_experience"],
                          organization=cfg["organization"], real_data_approved=False)
    assert asc_auth.authenticate(prod, cfg["email"], cfg["password"]) is not None

    assert asc_auth.ensure_mock_contributor(prod) is None
    assert asc_auth.authenticate(prod, cfg["email"], cfg["password"]) is None, (
        "the published password still signs in on production"
    )
    # The row survives so its historic submissions and export exclusion still resolve.
    row = prod.get_user_by_email(cfg["email"])
    assert row is not None and row["is_mock"] == 1
    assert not row["real_data_approved"]


def test_production_with_an_operator_set_password_still_gets_its_sandbox(monkeypatch):
    """Not a blanket ban: an operator who chose a private password keeps the
    demo, and V4 unlocks, exactly as before."""
    A.fresh_store()
    from asclepius.store import get_store
    st = get_store()
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ASCLEPIUS_MOCK_PASSWORD", "a-private-prod-password")
    u = asc_auth.ensure_mock_contributor(st)
    assert u is not None and u["is_mock"] == 1
    assert u["real_data_approved"] == 1
    cfg = asc_auth.mock_credentials()
    assert asc_auth.authenticate(st, cfg["email"], cfg["password"]) is not None


def test_dev_and_staging_are_untouched(monkeypatch):
    """The sandbox is genuinely useful where there are no real patients, and
    nothing about those environments changed."""
    for env in ("", "development", "staging"):
        A.fresh_store()
        from asclepius.store import get_store
        st = get_store()
        monkeypatch.setenv("ENV", env)
        monkeypatch.delenv("ASCLEPIUS_MOCK_PASSWORD", raising=False)
        cfg = asc_auth.mock_credentials()
        u = asc_auth.ensure_mock_contributor(st)
        assert u is not None and u["real_data_approved"] == 1, f"ENV={env!r}"
        assert asc_auth.authenticate(st, cfg["email"], cfg["password"]) is not None
