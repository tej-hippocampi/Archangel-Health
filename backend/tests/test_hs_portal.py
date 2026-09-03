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
    """Fresh client per test → fresh cookie jar.

    Base URL is https:// on purpose. The session cookie is marked ``Secure``
    unconditionally (C-2.4), and a conforming cookie jar will not return a
    Secure cookie over plain http — so an http:// test client would silently
    exercise a session-less portal and prove nothing."""
    return TestClient(A.app, base_url="https://testserver")


def _login(c: TestClient, username: str, password: str):
    return c.post("/api/asclepius/hs/login", json={"username": username, "password": password})


def _login_h(c: TestClient, username: str, password: str, headers: dict):
    """Sign in from a specific client address. ``ratelimit.client_ip`` reads the
    LAST X-Forwarded-For hop, which is what the (username, ip) lock keys on."""
    return c.post("/api/asclepius/hs/login",
                  json={"username": username, "password": password}, headers=headers)


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
    # C-2.4: Secure is UNCONDITIONAL. It used to be gated on ENV=="production",
    # which nothing in the deployment sets, so a PHI portal's session cookie
    # shipped over plain HTTP. The old test asserted HttpOnly and SameSite and
    # conspicuously did not assert this one.
    assert "Secure" in cookie, "the session cookie is not marked Secure"

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


def test_known_and_unknown_usernames_are_byte_identical_under_brute_force():
    """C-2.1: the response must not distinguish a real account from a fake one.

    ONE loop, ONE assertion, both usernames — the previous pair of tests looped
    ``range(4)`` for the real account and ``range(5)`` for the fake one and so
    both passed over a genuine off-by-one: the 5th attempt returned 429 for a
    real username and 401 for an unknown one. Since usernames are derived
    deterministically from the organization name, that one bit told an outsider
    which hospitals are partners.
    """
    uname, _ = _mk_portal_user()
    ghost = "ghost" + uuid.uuid4().hex[:8]

    def vector(username: str):
        c = _client()
        out = []
        for _i in range(7):
            r = _login(c, username, "wrong-password-xx")
            out.append((r.status_code, r.json().get("detail"),
                        r.headers.get("retry-after")))
        return out

    known, unknown = vector(uname), vector(ghost)
    assert known == unknown, (
        f"account existence leaks through the login response:\n"
        f"  known  : {[s for s, _d, _r in known]}\n"
        f"  unknown: {[s for s, _d, _r in unknown]}"
    )
    # And the lock genuinely engages rather than both simply being 401 forever.
    assert [s for s, _d, _r in known] == [401, 401, 401, 401, 429, 429, 429]


def test_lock_refuses_even_the_correct_password():
    uname, _ = _mk_portal_user()
    c = _client()
    for _i in range(5):
        _login(c, uname, "bad-password-xx")
    assert _login(c, uname, "temp-passphrase-123").status_code == 429


def test_lockout_is_not_a_remote_kill_switch(monkeypatch):
    """C-2.2: an attacker must not be able to lock a hospital out of its own
    portal. The hard lock is scoped to (username, ip), so exhausting the
    threshold from one address leaves every other address unaffected."""
    import ratelimit
    monkeypatch.setattr(ratelimit, "is_enabled", lambda: False)  # isolate from the IP throttle
    uname, _ = _mk_portal_user()

    attacker = {"X-Forwarded-For": "203.0.113.9"}
    c = _client()
    for _i in range(5):
        _login_h(c, uname, "bad-password-xx", attacker)
    assert _login_h(c, uname, "bad-password-xx", attacker).status_code == 429

    # The hospital, from its own address, signs in normally.
    hospital = {"X-Forwarded-For": "198.51.100.4"}
    ok = _login_h(_client(), uname, "temp-passphrase-123", hospital)
    assert ok.status_code == 200, "a remote attacker locked the hospital out of its own portal"


def test_admin_can_unlock_a_locked_health_system(monkeypatch):
    """C-2.2: recovery must not require re-provisioning (which rotates the
    passphrase and forces another reset on the hospital)."""
    import ratelimit
    monkeypatch.setattr(ratelimit, "is_enabled", lambda: False)
    store = _store()
    uname, hs = _mk_portal_user()
    ip = {"X-Forwarded-For": "203.0.113.9"}
    c = _client()
    for _i in range(5):
        _login_h(c, uname, "bad-password-xx", ip)
    assert _login_h(c, uname, "temp-passphrase-123", ip).status_code == 429

    from tests import _asclepius as _A
    admin = _A.make_user(store, role="admin")
    r = _client().post(f"/api/asclepius/admin/health-systems/{hs['hs_id']}/unlock",
                       json={}, headers=_A.headers_for(admin))
    assert r.status_code == 200, r.text
    assert uname in r.json()["unlocked"]
    assert _login_h(_client(), uname, "temp-passphrase-123", ip).status_code == 200


def test_unlock_requires_admin():
    store = _store()
    _uname, hs = _mk_portal_user()
    from tests import _asclepius as _A
    doc = _A.make_user(store, role="evaluator")
    assert _client().post(f"/api/asclepius/admin/health-systems/{hs['hs_id']}/unlock",
                          json={}).status_code in (401, 403)
    assert _client().post(f"/api/asclepius/admin/health-systems/{hs['hs_id']}/unlock",
                          json={}, headers=_A.headers_for(doc)).status_code in (401, 403)


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


def test_password_change_invalidates_other_sessions_but_not_your_own():
    """C-2.3: a leaked cookie must not outlive the victim's password reset.

    Sessions are stateless JWTs, so before this the only thing that ended one
    was the 12-hour TTL — a stolen cookie stayed live for half a day after the
    user did the one thing they would do about it."""
    uname, _ = _mk_portal_user("settled-password-abc", must_reset=False)
    victim, thief = _client(), _client()
    assert _login(victim, uname, "settled-password-abc").status_code == 200
    assert _login(thief, uname, "settled-password-abc").status_code == 200
    assert thief.get("/api/asclepius/hs/me").status_code == 200

    r = victim.post("/api/asclepius/hs/password",
                    json={"current_password": "settled-password-abc",
                          "new_password": "a-brand-new-password-1"})
    assert r.status_code == 200
    # The other session is dead…
    assert thief.get("/api/asclepius/hs/me").status_code == 401
    # …and the one that made the change is not signed out by its own action.
    assert victim.get("/api/asclepius/hs/me").status_code == 200


def test_logout_actually_revokes_the_token():
    """C-2.3: on a shared hospital workstation 'Sign out' must be real. The
    handler only deleted the cookie, so a copy already taken kept working."""
    uname, _ = _mk_portal_user("settled-password-abc", must_reset=False)
    c = _client()
    assert _login(c, uname, "settled-password-abc").status_code == 200
    stolen = c.cookies.get("hs_portal_session")
    assert stolen
    c.post("/api/asclepius/hs/logout")

    replay = _client()
    replay.cookies.set("hs_portal_session", stolen)
    assert replay.get("/api/asclepius/hs/me").status_code == 401


def test_admin_can_revoke_and_restore_portal_access():
    """C-2.3: nothing ever wrote hs_portal_users.active / health_systems.active
    — they were revocation columns with no revocation path."""
    store = _store()
    uname, hs = _mk_portal_user("settled-password-abc", must_reset=False)
    live = _client()
    assert _login(live, uname, "settled-password-abc").status_code == 200

    from tests import _asclepius as _A
    admin = _A.make_user(store, role="admin")
    hdrs = _A.headers_for(admin)
    r = _client().post(f"/api/asclepius/admin/health-systems/{hs['hs_id']}/access",
                       json={"active": False}, headers=hdrs)
    assert r.status_code == 200, r.text
    # The live session dies immediately, and a fresh sign-in is refused with the
    # SAME generic message — "disabled" would confirm the username exists.
    assert live.get("/api/asclepius/hs/me").status_code == 401
    denied = _login(_client(), uname, "settled-password-abc")
    assert denied.status_code == 401
    assert denied.json()["detail"] == "Incorrect username or password."

    r2 = _client().post(f"/api/asclepius/admin/health-systems/{hs['hs_id']}/access",
                        json={"active": True}, headers=hdrs)
    assert r2.status_code == 200
    assert _login(_client(), uname, "settled-password-abc").status_code == 200


def test_upload_filename_is_sanitized_at_insert():
    """C-2.5: the stored name is echoed into a quoted Content-Disposition, so a
    hospital account could choose what the admin's browser saved the file as."""
    uname, _ = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    evil = 'x"; filename="Q3-invoice.pdf'
    res = c.post("/api/asclepius/hs/uploads",
                 files=[("files", (evil, b'{"resourceType": "Bundle"}', "application/json"))])
    assert res.status_code == 200, res.text
    stored = _store().get_ingest_upload(res.json()["upload_id"])["filename"]
    assert '"' not in stored and ";" not in stored
    assert "Q3-invoice.pdf" not in stored or stored.count("_") > 0
    assert all(ch.isalnum() or ch in "._-" for ch in stored), stored


def test_non_latin1_filename_does_not_break_the_admin_download():
    """C-2.5: a non-latin-1 name used to raise on header encode → 500."""
    store = _store()
    uname, _ = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    res = c.post("/api/asclepius/hs/uploads",
                 files=[("files", ("пациенты.json", b'{"resourceType": "Bundle"}',
                                   "application/json"))])
    assert res.status_code == 200, res.text
    upload_id = res.json()["upload_id"]

    from tests import _asclepius as _A
    admin = _A.make_user(store, role="admin")
    dl = _client().get(f"/api/asclepius/ingestion/uploads/{upload_id}/download",
                       headers=_A.headers_for(admin))
    assert dl.status_code != 500, "non-latin-1 filename still breaks header encoding"
    if dl.status_code == 200:
        cd = dl.headers.get("content-disposition", "")
        assert "filename*=UTF-8''" in cd
        assert cd.count('"') == 2, f"unbalanced quoting in {cd!r}"


def test_upload_quota_is_enforced_per_health_system(monkeypatch):
    """C-2.6: the per-request cap and per-IP limiter bound one request and one
    address, not cumulative volume from a single partner."""
    monkeypatch.setenv("ASCLEPIUS_HS_QUOTA_BYTES", "200")
    uname, _ = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    body = b'{"resourceType": "Bundle", "entry": []}'
    seen = [c.post("/api/asclepius/hs/uploads",
                   files=[("files", (f"f{i}.json", body, "application/json"))]).status_code
            for i in range(6)]
    assert 429 in seen, f"quota never engaged: {seen}"
    # The refusal tells the hospital what to do next, in their language.
    last = c.post("/api/asclepius/hs/uploads",
                  files=[("files", ("f.json", body, "application/json"))])
    if last.status_code == 429:
        detail = last.json()["detail"].lower()
        assert "limit" in detail and "secure bulk transfer" in detail


def test_healthy_inflight_upload_never_reads_as_a_problem():
    """C-3.1: 'parsing' was missing from the portal status map, and it is the
    pipeline's most common in-flight state — so a perfectly healthy upload told
    the hospital "Needs attention: our team is taking a closer look" while it
    was mid-parse."""
    from routers import asclepius_provider as P
    from asclepius import ingestion as I

    for status in I._NON_TERMINAL_UPLOAD_STATUSES:
        mapped = P._HS_PORTAL_STATUS.get(status)
        assert mapped in ("received", "processing"), (
            f"in-flight pipeline status {status!r} maps to {mapped!r}, which renders "
            f"to a hospital as 'Needs attention'"
        )
    # And through the view function the hospital actually sees.
    view = P._hs_upload_view({"upload_id": "u1", "status": "parsing", "files": []})
    assert view["status"] == "processing"
    assert not view["detail"], "a healthy in-flight upload should carry no alarm copy"


def test_portal_upload_is_not_stamped_with_a_guessed_specialty(monkeypatch, tmp_path):
    """C-3.2: the portal's sentinel link_id has no link row, so the fallback
    chain ended at the literal 'nephrology' and stamped every hospital upload
    with it — a bare cardiology .json landed labeled nephrology."""
    from asclepius import ingestion as I

    store = _store()
    uname, hs = _mk_portal_user("ready-password-123", must_reset=False)
    c = _client()
    _login(c, uname, "ready-password-123")
    payload = json.dumps({"resourceType": "Bundle", "type": "collection", "entry": []}).encode()
    res = c.post("/api/asclepius/hs/uploads",
                 files=[("files", ("cardiology_export.json", payload, "application/json"))])
    assert res.status_code == 200, res.text
    upload_id = res.json()["upload_id"]

    cases = store.list_ingest_cases(upload_id=upload_id)
    assert all((cs.get("specialty") or None) != "nephrology" for cs in cases), (
        "a portal upload was silently stamped nephrology"
    )
    # Undetermined is the neutral 'general', which the admin renders as
    # "not yet determined" rather than as a clinical claim.
    assert all((cs.get("specialty") or "general") == "general" for cs in cases)

    # The operator can set the real value, which is the point of leaving it NULL.
    from tests import _asclepius as _A
    admin = _A.make_user(store, role="admin")
    r = _client().post(f"/api/asclepius/admin/uploads/{upload_id}/specialty",
                       json={"specialty": "cardiology"}, headers=_A.headers_for(admin))
    assert r.status_code == 200, r.text
    if cases:
        assert all(cs.get("specialty") == "cardiology"
                   for cs in store.list_ingest_cases(upload_id=upload_id))


def test_portal_upload_failure_notifies_the_health_system():
    """C-3.3: the portal door had no failure loop — its sentinel link_id has no
    link row, so _recipient_for returned (None, None) and a rejected hospital
    upload emailed nobody. That is the one door whose users we cannot support in
    real time."""
    from asclepius import ingest_notify

    store = _store()
    hs = store.ensure_health_system("Notify General", contact_email="it@notify.example.org")
    up = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="x.zip", sha256="0" * 64,
        size_bytes=10, raw_path=None, source_ip=None)
    store.set_upload_health_system(up["upload_id"], hs["hs_id"])
    upload = store.get_ingest_upload(up["upload_id"])

    email, name = ingest_notify._recipient_for(store, upload)
    assert email == "it@notify.example.org", "a rejected portal upload still emails nobody"
    assert name == "Notify General"


def test_rejected_uploads_do_not_sit_in_needs_review():
    """C-3.4: an outright-rejected upload is dead, not pending work. Filing it
    under 'uploaded, not yet examined' inflates the operator's queue with rows
    they cannot action."""
    store = _store()
    hs = store.ensure_health_system("Bucket General")
    up = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="bad.zip", sha256="0" * 64,
        size_bytes=10, raw_path=None, source_ip=None)
    store.set_upload_health_system(up["upload_id"], hs["hs_id"])
    store.update_ingest_upload(up["upload_id"], status="rejected", reason="nothing ingested")

    from tests import _asclepius as _A
    admin = _A.make_user(store, role="admin")
    detail = _client().get(f"/api/asclepius/admin/health-systems/{hs['hs_id']}",
                           headers=_A.headers_for(admin)).json()
    ids = {k: {e["upload_id"] for e in v} for k, v in detail["buckets"].items()}
    assert up["upload_id"] in ids["rejected"]
    assert up["upload_id"] not in ids["needs_review"]


def test_definition_of_done_end_to_end(monkeypatch):
    """PRD C §2 verbatim: type an organization and an email → the contact
    receives credentials → logs into a password-protected portal → signs the
    agreement → uploads a bare .json → it appears under that health system in
    the admin, with no partner IDs or specialty fields anywhere in the flow.

    The signature is a step in this list now, not a formality it skipped.
    Provisioning used to leave the organization's state NULL, which reads as
    ACTIVE, so this test walked from "an admin typed a name" to "bytes accepted"
    without a contract existing anywhere, which is exactly the door the state
    machine was built to close.
    """
    import re as _re
    from routers import asclepius_admin as RA

    store = _store()
    sent = []

    async def _fake_send(to, subject, html, **kw):
        sent.append(html)
        return True

    monkeypatch.setattr(RA, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(RA, "send_html_email", _fake_send)
    from tests import _asclepius as _A
    admin = _A.make_user(store, role="admin")

    ca = _client()
    # 1. Two fields. Nothing else.
    r = ca.post("/api/asclepius/admin/health-systems/provision",
                json={"organization": "Mass General Hospital", "email": "data@mgh.harvard.edu", "purpose": "task_creation"},
                headers=_A.headers_for(admin))
    assert r.status_code == 200, r.text
    username = r.json()["username"]
    hs_id = r.json()["health_system"]["hs_id"]

    # 2. The credentials arrive by email; the passphrase appears nowhere else.
    codes = _re.findall(r"<code>([^<]+)</code>", sent[0])
    passphrase = codes[1]

    # 3. Sign in and take the forced reset.
    cp = _client()
    login = cp.post("/api/asclepius/hs/login",
                    json={"username": username, "password": passphrase})
    assert login.status_code == 200 and login.json()["must_reset"] is True
    assert cp.post("/api/asclepius/hs/password",
                   json={"new_password": "hospital-it-passphrase-1"}).status_code == 200

    # 4. Sign the agreement. Read the hash off the page they were shown rather
    # than recomputing it, because that is the only version of the document the
    # signature is allowed to be taken against.
    shown = cp.get("/api/asclepius/hs/agreement")
    assert shown.status_code == 200, shown.text
    assert shown.json()["can_sign"] is True, shown.json()["state"]
    signed = cp.post("/api/asclepius/hs/agreement/sign", json={
        "typed_name": "Dana Reyes", "typed_title": "Chief Information Officer",
        "authority_affirmed": True, "consent_esign": True,
        "doc_sha256": shown.json()["doc_sha256"]})
    assert signed.status_code == 200, signed.text

    # 5. Now the bare .json goes through.
    up = cp.post("/api/asclepius/hs/uploads",
                 files=[("files", ("export.json", b'{"resourceType": "Bundle"}', "application/json"))])
    assert up.status_code == 200, up.text

    # 6. It appears under that health system in the admin buckets.
    detail = ca.get(f"/api/asclepius/admin/health-systems/{hs_id}",
                    headers=_A.headers_for(admin)).json()
    all_entries = [e for bucket in detail["buckets"].values() for e in bucket]
    assert any(e["upload_id"] == up.json()["upload_id"] for e in all_entries)


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
