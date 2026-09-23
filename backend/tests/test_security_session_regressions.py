"""Independent synthetic checks for the security audit's session boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import sqlite3
import zipfile

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import auth
import audio_storage
import patient_session
from asclepius import auth as asc_auth, passwords, store as asc_store
from eligibility import pipeline
from integrations.elevenlabs import ElevenLabsClient
from routers import eligibility
from staff_context import StaffContext
from tests._asclepius import app, fresh_store, make_user


def test_unverified_reregistration_requires_original_password(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(auth, "_users", {})
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.json")

    async def no_external_email(email):
        pass

    monkeypatch.setattr(main, "_issue_account_verification", no_external_email)
    client = TestClient(app)
    user = {"email": "synthetic-owner@example.org", "password": "Correct-original-password-7"}
    assert client.post("/api/auth/register", json=user).status_code == 200
    bad = client.post("/api/auth/register", json={**user, "password": "Different-password-8"})
    assert bad.status_code == 400 and "access_token" not in bad.json()
    repeated = client.post("/api/auth/register", json=user)
    assert repeated.status_code == 200
    auth.mark_email_verified(user["email"])
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + repeated.json()["access_token"]}).status_code == 200


def test_same_second_password_change_revokes_old_session_and_keeps_replacement(tmp_path, monkeypatch):
    store = fresh_store()
    user = make_user(store)
    old = asc_auth.create_token(user)
    # Align the password change with the exact issue second of the old token.
    issued = asc_auth.decode_token(old)["iat"]
    frozen = datetime.utcfromtimestamp(issued)

    class SameSecond:
        @staticmethod
        def utcnow():
            return frozen

    monkeypatch.setattr(asc_store, "datetime", SameSecond)
    store.set_user_password(user["id"], "Replacement-password-8")
    assert asc_auth.get_current_user_optional("Bearer " + old) is None
    fresh_user = store.get_user_by_id(user["id"])
    replacement = asc_auth.create_token(fresh_user)
    assert asc_auth.get_current_user_optional("Bearer " + replacement)["id"] == user["id"]
    legacy = asc_auth.decode_token(old)
    legacy.pop("pwdv", None)
    legacy.pop("iat", None)
    legacy_token = jwt.encode(legacy, asc_auth.get_asclepius_secret(), algorithm="HS256")
    assert asc_auth.get_current_user_optional("Bearer " + legacy_token) is None


def test_profile_password_change_replaces_session_and_invalidates_prior_reset_link():
    store = fresh_store()
    user = make_user(store)
    token = asc_auth.create_token(user)
    raw, hashed = passwords.new_reset_token()
    store.create_password_reset(user_id=user["id"], token_hash=hashed, expires_at=passwords.reset_expires_at())
    client = TestClient(app)
    changed = client.post("/api/asclepius/me/password", headers={"Authorization": "Bearer " + token},
                          json={"current_password": "pw-12345678", "new_password": "Replacement-password-8"})
    assert changed.status_code == 200
    assert asc_auth.get_current_user_optional("Bearer " + token) is None
    assert asc_auth.get_current_user_optional("Bearer " + changed.json()["token"])["id"] == user["id"]
    assert client.post("/api/asclepius/auth/password/reset",
                       json={"token": raw, "new_password": "Another-password-9"}).status_code == 400


def test_profile_shares_password_safety_checks_without_changing_existing_length_promise():
    store = fresh_store()
    user = make_user(store)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + asc_auth.create_token(user)}
    body = {"current_password": "pw-12345678", "new_password": "password"}
    assert client.post("/api/asclepius/me/password", headers=headers, json=body).status_code == 422
    body["new_password"] = "Mixed-8!"
    changed = client.post("/api/asclepius/me/password", headers=headers, json=body)
    assert changed.status_code == 200
    assert asc_auth.authenticate(store, user["email"], "Mixed-8!")
    # The separate signup/recovery policy retains its twelve-character floor.
    with pytest.raises(passwords.PasswordRejected, match="12"):
        passwords.validate("Mixed-8!")


def test_password_change_invalidates_outstanding_prepassword_signin_link():
    store = fresh_store()
    user = make_user(store)
    raw, hashed = passwords.new_signin_token()
    store.create_signin_link(user_id=user["id"], token_hash=hashed, expires_at=passwords.signin_expires_at())
    store.set_user_password(user["id"], "Replacement-password-8")
    client = TestClient(app)
    assert client.post("/api/asclepius/auth/signin-link/exchange", json={"token": raw}).status_code == 400
    # Password recovery remains possible through a newly issued reset link.
    raw_reset, hash_reset = passwords.new_reset_token()
    store.create_password_reset(user_id=user["id"], token_hash=hash_reset, expires_at=passwords.reset_expires_at())
    recovered = client.post("/api/asclepius/auth/password/reset",
                            json={"token": raw_reset, "new_password": "Another-password-9"})
    assert recovered.status_code == 200


def test_password_and_link_revocations_roll_back_together_on_database_failure():
    store = fresh_store()
    user = make_user(store)
    _, signin_hash = passwords.new_signin_token()
    _, reset_hash = passwords.new_reset_token()
    store.create_signin_link(user_id=user["id"], token_hash=signin_hash, expires_at=passwords.signin_expires_at())
    store.create_password_reset(user_id=user["id"], token_hash=reset_hash, expires_at=passwords.reset_expires_at())
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER synthetic_link_write_failure BEFORE UPDATE ON signin_links "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic database failure'); END")
    with pytest.raises(sqlite3.DatabaseError, match="synthetic database failure"):
        store.set_user_password(user["id"], "Replacement-password-8")
    assert store.get_user_by_id(user["id"])["password_hash"] == user["password_hash"]
    assert store.get_password_reset_by_token_hash(reset_hash)["invalidated_at"] is None
    with store._conn() as conn:
        assert conn.execute("SELECT used_at FROM signin_links WHERE token_hash = ?", (signin_hash,)).fetchone()[0] is None


def test_patient_entry_token_has_exactly_one_parallel_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "patient.db"))
    token = patient_session.create_entry_token("synthetic-patient", "synthetic-hospital")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(patient_session.consume_entry_token, [token] * 8))
    assert sum(result is not None for result in results) == 1


def test_patient_sessions_fail_closed_if_revocation_store_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "patient.db"))
    token = patient_session.create_patient_session("synthetic-patient", "synthetic-hospital")
    assert patient_session.decode_patient_session(token) is not None
    entry = patient_session.create_entry_token("synthetic-patient", "synthetic-hospital")

    def unavailable():
        raise sqlite3.OperationalError("synthetic unavailable database")

    monkeypatch.setattr(patient_session, "_conn", unavailable)
    assert patient_session.decode_patient_session(token) is None
    assert patient_session.consume_entry_token(entry) is None


def test_staff_revocation_failure_never_restores_a_revoked_session(monkeypatch):
    import token_revocation
    from tenant_jwt import create_tenant_staff_token, decode_tenant_staff_token
    token = create_tenant_staff_token(email="synthetic@example.org", name="Synthetic", role="surgeon",
                                     health_system_id="synthetic-hospital", tenant_slug="synthetic", health_system_code="SYNTHETIC")
    assert decode_tenant_staff_token(token)
    assert token_revocation.revoke_token(token)
    assert decode_tenant_staff_token(token) is None

    def unavailable():
        raise sqlite3.OperationalError("synthetic unavailable revocation database")

    monkeypatch.setattr(token_revocation, "_conn", unavailable)
    assert decode_tenant_staff_token(token) is None


def test_tenant_login_limits_repeated_password_checks(monkeypatch):
    import ratelimit
    from routers.tenant_portal import router
    from types import SimpleNamespace
    calls = []
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.team_store = SimpleNamespace(authenticate_team_member=lambda *args: calls.append(1))
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    ratelimit.reset()
    try:
        client = TestClient(test_app)
        statuses = [client.post("/api/tenant/synthetic/auth/login", json={
            "email": "synthetic@example.org", "password": "wrong"}).status_code for _ in range(12)]
        assert statuses[:10] == [401] * 10 and statuses[10:] == [429, 429]
        assert len(calls) == 10
    finally:
        ratelimit.reset()


def test_audio_storage_exposes_only_generated_opaque_files(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_storage.tempfile, "gettempdir", lambda: str(tmp_path))
    private = tmp_path / "private-upload.txt"
    private.write_text("Synthetic private upload")
    writer = ElevenLabsClient.__new__(ElevenLabsClient)
    url = asyncio.run(writer._save_audio(b"synthetic-audio", "patient-identifier"))
    assert "patient-identifier" not in url
    assert audio_storage.cached_audio_url(url) == url
    app = FastAPI()
    app.mount("/audio", StaticFiles(directory=str(audio_storage.audio_root())), name="audio")
    client = TestClient(app)
    assert client.get(url).content == b"synthetic-audio"
    assert client.get("/audio/private-upload.txt").status_code == 404
    assert audio_storage.cached_audio_url("/audio/../private-upload.txt") is None
    symlink_name = "f" * 32 + ".mp3"
    (audio_storage.audio_root() / symlink_name).symlink_to(private)
    assert audio_storage.cached_audio_url("/audio/" + symlink_name) is None
    assert client.get("/audio/" + symlink_name).status_code == 404


@pytest.mark.parametrize("source,tenant", [("landing", None), ("tenant", None), ("tenant", "other-hospital")])
def test_eligibility_denies_foreign_or_missing_tenant_scope(source, tenant):
    staff = StaffContext(source, "synthetic@example.org", "Synthetic", "surgeon", tenant, None, None)
    patient = {"synthetic-patient": {"health_system_id": "own-hospital"}}
    for guard in (lambda: eligibility._assert_patient_access("synthetic-patient", staff, patient),
                  lambda: eligibility._assert_batch_access({"health_system_id": "own-hospital"}, staff)):
        with pytest.raises(HTTPException) as exc:
            guard()
        assert exc.value.status_code == 404
    own = StaffContext("tenant", "synthetic@example.org", "Synthetic", "surgeon", "own-hospital", None, None)
    eligibility._assert_patient_access("synthetic-patient", own, patient)
    eligibility._assert_batch_access({"health_system_id": "own-hospital"}, own)


def test_eligibility_zip_refuses_expansion_before_accepting_partial_output(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_BATCH_EXPANDED_BYTES", 64)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("first.txt", b"x" * 32)
        archive.writestr("second.txt", b"y" * 33)
    with pytest.raises(ValueError, match="processing limit"):
        pipeline._split_batch_payload([("fixture.zip", stream.getvalue())])
