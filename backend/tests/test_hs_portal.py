"""PRD C Phase 1 — the health-system upload portal door.

Covers the security posture the PRD calls non-negotiable: generic login
failures that never reveal whether a username exists, per-account lockout
after repeated failures, HttpOnly/SameSite=Strict session cookie, forced
first-login reset before uploading, bare-.json acceptance through the shared
``wrap_loose_files`` packer, the health_system_id stamp, and hospital-facing
error copy that never leaks internal vocabulary ("magic bytes",
"quarantined").
"""
from __future__ import annotations

import base64
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

_KEY = base64.urlsafe_b64encode(b"hs-portal-test-key-32-bytes-pad!").decode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _mk_portal_user(password: str = "temp-passphrase-123", *, must_reset: bool = True):
    """A provisioned health system + portal account. Unique username per test so
    the in-process unknown-username lockout map never bleeds between tests."""
    store = _store()
    uname = "hs" + uuid.uuid4().hex[:10]
    hs = store.ensure_health_system("Test Health " + uname, contact_email="it@test.org")
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"], password=password,
                                email="it@test.org")
    if not must_reset:
        store.set_hs_portal_password(uname, password, must_reset=False)
    return uname, hs


def _client() -> TestClient:
    # Fresh client per test → fresh cookie jar.
    return TestClient(A.app)


def _login(c: TestClient, username: str, password: str):
    return c.post("/api/asclepius/hs/login", json={"username": username, "password": password})


# ─── Login ───────────────────────────────────────────────────────────────────
def test_login_success_sets_hardened_cookie_and_flags_reset():
    uname, hs = _mk_portal_user()
    c = _client()
    res = _login(c, uname, "temp-passphrase-123")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["must_reset"] is True
    assert body["organization"] == hs["name"]

    cookie = res.headers.get("set-cookie", "")
    assert "hs_portal_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("SameSite=Strict", "SameSite=strict")

    me = c.get("/api/asclepius/hs/me")
    assert me.status_code == 200
    assert me.json()["username"] == uname


def test_wrong_password_and_unknown_username_are_indistinguishable():
    uname, _ = _mk_portal_user()
    c = _client()
    real = _login(c, uname, "wrong-password-000")
    fake = _login(c, "nosuchuser" + uuid.uuid4().hex[:8], "wrong-password-000")
    assert real.status_code == fake.status_code == 401
    assert real.json()["detail"] == fake.json()["detail"]
    # The message itself names neither "username" existence nor internals.
    assert "exist" not in real.json()["detail"].lower()


def test_brute_force_locks_the_account():
    uname, _ = _mk_portal_user()
    c = _client()
    for _i in range(4):
        assert _login(c, uname, "bad-password-xx").status_code == 401
    # 5th failure trips the lock…
    assert _login(c, uname, "bad-password-xx").status_code == 429
    # …and even the CORRECT password is refused while locked.
    assert _login(c, uname, "temp-passphrase-123").status_code == 429


def test_brute_force_on_unknown_username_locks_identically():
    ghost = "ghost" + uuid.uuid4().hex[:8]
    c = _client()
    for _i in range(5):
        r = _login(c, ghost, "bad-password-xx")
    assert _login(c, ghost, "bad-password-xx").status_code == 429


# ─── Forced reset ────────────────────────────────────────────────────────────
def test_upload_blocked_until_password_reset():
    uname, _ = _mk_portal_user()
    c = _client()
    _login(c, uname, "temp-passphrase-123")
    res = c.post("/api/asclepius/hs/uploads",
                 files=[("files", ("a.json", b'{"resourceType": "Bundle"}', "application/json"))])
    assert res.status_code == 403

    # Too-short replacement is refused; a proper one goes through.
    assert c.post("/api/asclepius/hs/password", json={"new_password": "short"}).status_code == 400
    assert c.post("/api/asclepius/hs/password",
                  json={"new_password": "my-own-long-password-1"}).status_code == 200

    res2 = c.post("/api/asclepius/hs/uploads",
                  files=[("files", ("a.json", b'{"resourceType": "Bundle"}', "application/json"))])
    assert res2.status_code == 200, res2.text

    # After reset, the OLD temp password no longer signs in.
    c2 = _client()
    assert _login(c2, uname, "temp-passphrase-123").status_code == 401
    assert _login(c2, uname, "my-own-long-password-1").status_code == 200


def test_normal_password_change_requires_current():
    uname, _ = _mk_portal_user("settled-password-abc", must_reset=False)
    c = _client()
    _login(c, uname, "settled-password-abc")
    r = c.post("/api/asclepius/hs/password",
               json={"current_password": "wrong-current-1", "new_password": "another-long-pass-1"})
    assert r.status_code == 400
    r2 = c.post("/api/asclepius/hs/password",
                json={"current_password": "settled-password-abc", "new_password": "another-long-pass-1"})
    assert r2.status_code == 200


# ─── Uploads ─────────────────────────────────────────────────────────────────
def test_bare_json_upload_lands_with_health_system_id():
    uname, hs = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    payload = json.dumps({"resourceType": "Bundle", "type": "collection", "entry": []}).encode()
    res = c.post("/api/asclepius/hs/uploads",
                 files=[("files", ("export.json", payload, "application/json"))])
    assert res.status_code == 200, res.text
    upload_id = res.json()["upload_id"]

    up = _store().get_ingest_upload(upload_id)
    assert up is not None
    assert up["health_system_id"] == hs["hs_id"]
    assert up["link_id"] == "hs-portal"

    # The portal history shows the four plain-language states only.
    hist = c.get("/api/asclepius/hs/uploads")
    assert hist.status_code == 200
    rows = hist.json()["uploads"]
    assert len(rows) == 1
    assert rows[0]["status"] in ("received", "processing", "accepted", "needs_attention")
    assert rows[0]["filename"] == "export.json"


def test_unreadable_upload_copy_is_actionable_not_internal():
    uname, _ = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    # Starts with PK so the shared packer passes it through as a "zip", then the
    # unpacker rejects it — the classic "bad magic bytes / corrupt zip" path.
    res = c.post("/api/asclepius/hs/uploads",
                 files=[("files", ("data.zip", b"PK\x03\x04not-actually-a-zip", "application/zip"))])
    assert res.status_code == 200
    hist = c.get("/api/asclepius/hs/uploads").json()["uploads"]
    assert hist[0]["status"] == "needs_attention"
    detail = (hist[0]["detail"] or "").lower()
    assert "magic" not in detail
    assert "quarantin" not in detail
    assert ".zip" in detail or "closer look" in detail  # actionable, not raw


def test_uploads_scoped_to_own_health_system():
    uname_a, hs_a = _mk_portal_user("ready-password-123", must_reset=False)
    uname_b, _ = _mk_portal_user("ready-password-456", must_reset=False)
    ca = _client()
    _login(ca, uname_a, "ready-password-123")
    ca.post("/api/asclepius/hs/uploads",
            files=[("files", ("a.json", b'{"resourceType": "Bundle"}', "application/json"))])
    cb = _client()
    _login(cb, uname_b, "ready-password-456")
    assert cb.get("/api/asclepius/hs/uploads").json()["uploads"] == []


# ─── Session ─────────────────────────────────────────────────────────────────
def test_logout_and_anonymous_are_unauthorized():
    uname, _ = _mk_portal_user()
    c = _client()
    _login(c, uname, "temp-passphrase-123")
    assert c.get("/api/asclepius/hs/me").status_code == 200
    c.post("/api/asclepius/hs/logout")
    assert c.get("/api/asclepius/hs/me").status_code == 401
    assert _client().get("/api/asclepius/hs/me").status_code == 401
    assert _client().post("/api/asclepius/hs/uploads",
                          files=[("files", ("a.json", b"{}", "application/json"))]).status_code == 401


def test_events_logged_for_login_and_upload():
    uname, _ = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    c.post("/api/asclepius/hs/uploads",
           files=[("files", ("a.json", b'{"resourceType": "Bundle"}', "application/json"))])
    store = _store()
    with store._conn() as conn:
        types = {r["event_type"] for r in conn.execute(
            "SELECT event_type FROM events").fetchall()}
    assert "login_succeeded" in types
    assert "upload_received" in types
