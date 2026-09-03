"""Sandbox PRD §2 (seed) and §3.2 (Accounts, Reset, fresh doctor), §3.3 (Outbox).

The seed is idempotent by stable ids, runs only in the sandbox realm, and the
ten physicians it creates can sign in through the real login with the realm
header and land on the dashboard (first run complete, practice case passed).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

import realm  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import sandbox_seed  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)

ADMIN_PW = "sandbox-admin-secret-1"
DOCTOR_PW = "sandbox-doctor-secret-1"


@pytest.fixture
def sandbox(monkeypatch):
    """Sandbox ON, bound to a fresh sandbox store; returns the admin headers."""
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, ADMIN_PW)
    monkeypatch.setenv(realm.DOCTOR_PASSWORD_VAR, DOCTOR_PW)
    with realm.scoped("sandbox"):
        store = A.fresh_store()
        from community.store import reset_community_store_for_tests
        reset_community_store_for_tests(os.path.join(A.TMP_DIR, f"community_sb_{A.uniq()}.db"))
        admin = sandbox_seed.ensure_sandbox_admin()
        token = asc_auth.create_token(admin)
    return {"store": store, "headers": {"Authorization": "Bearer " + token, realm.HEADER: "sandbox"}}


def _login(email: str, password: str, *, header: bool = True):
    h = {realm.HEADER: "sandbox"} if header else {}
    return client.post("/api/asclepius/auth/login", headers=h, json={"email": email, "password": password})


# ─── Bootstrap ───────────────────────────────────────────────────────────────
def test_sandbox_admin_exists_once_the_realm_is_on(sandbox):
    store = sandbox["store"]
    with realm.scoped("sandbox"):
        admin = store.get_user_by_email(sandbox_seed.ADMIN_EMAIL)
    assert admin and admin["role"] == "admin"
    r = _login(sandbox_seed.ADMIN_EMAIL, ADMIN_PW)
    assert r.status_code == 200, r.text
    # …and the same address is a 401 in live: the account exists only in the sandbox DB.
    assert _login(sandbox_seed.ADMIN_EMAIL, ADMIN_PW, header=False).status_code == 401


def test_ensure_sandbox_admin_is_a_noop_outside_the_realm_or_when_dark(monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, ADMIN_PW)
    assert sandbox_seed.ensure_sandbox_admin() is None          # live realm
    monkeypatch.delenv(realm.ADMIN_PASSWORD_VAR)
    with realm.scoped("sandbox"):
        assert sandbox_seed.ensure_sandbox_admin() is None      # dark


def test_status_is_readable_before_login_and_ensures_the_admin(sandbox):
    r = client.get("/api/asclepius/sandbox/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["realm"] == "sandbox" and body["admin_email"] == sandbox_seed.ADMIN_EMAIL
    assert body["seeded"] is True and body["physicians"] == 0


# ─── Seed (§2) ───────────────────────────────────────────────────────────────
def test_seed_creates_the_ten_physicians_and_is_idempotent(sandbox):
    r = client.post("/api/asclepius/sandbox/seed", headers=sandbox["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["physicians"]) == 10
    assert body["physicians"][0] == "sb-labeler-1@archangelhealth.ai"
    assert body["physicians"][-1] == "sb-reviewer-3@archangelhealth.ai"
    assert body["fresh"] is None

    store = sandbox["store"]
    with realm.scoped("sandbox"):
        users = {u["email"]: u for u in store.list_users()}
        for spec in sandbox_seed.PHYSICIANS:
            u = users[spec["email"]]
            assert u["role"] == "evaluator" and u["active"]
            assert u["tier"] == spec["tier"] and u["specialty"] == spec["specialty"]
            assert u["verification_status"] == "approved"
            assert u["real_data_approved"] == 1
            assert u["referral_code"]
            assert store.get_first_run(u["id"])["completed_at"]
            assert store.get_tutorial_state(u["id"])["gate"]["state"] == "passed"
            assert u["slack_joined"] == 1   # community-welcomed
        n_before = len(users)
    # Second run: same ten, nothing duplicated.
    r = client.post("/api/asclepius/sandbox/seed", headers=sandbox["headers"])
    assert r.status_code == 200
    with realm.scoped("sandbox"):
        assert len(store.list_users()) == n_before


def test_seeded_physician_signs_in_with_the_header_and_lands_on_the_dashboard(sandbox):
    client.post("/api/asclepius/sandbox/seed", headers=sandbox["headers"])
    r = _login("sb-labeler-3@archangelhealth.ai", DOCTOR_PW)
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    me = client.get("/api/asclepius/auth/me", headers={"Authorization": "Bearer " + tok})
    assert me.status_code == 200
    assert me.json()["specialty"] == "cardiology" and me.json()["tier"] == "labeler"
    # No walkthrough gate, no practice-case gate: the queue answers (empty is fine).
    nxt = client.get("/api/asclepius/tasks/next", headers={"Authorization": "Bearer " + tok})
    assert nxt.status_code == 200, nxt.text
    # Same credentials, no header → 401. The live DB has no such user.
    assert _login("sb-labeler-3@archangelhealth.ai", DOCTOR_PW, header=False).status_code == 401


def test_fresh_doctor_is_left_un_onboarded(sandbox):
    r = client.post("/api/asclepius/sandbox/seed?fresh=1", headers=sandbox["headers"])
    assert r.status_code == 200, r.text
    fresh = r.json()["fresh"]
    assert fresh == "sb-fresh-1@archangelhealth.ai"
    with realm.scoped("sandbox"):
        u = sandbox["store"].get_user_by_email(fresh)
        assert sandbox["store"].get_first_run(u["id"])["completed_at"] is None
        assert sandbox["store"].get_tutorial_state(u["id"]).get("gate", {}).get("state") != "passed"
    # The button mints the next one.
    r = client.post("/api/asclepius/sandbox/accounts/fresh", headers=sandbox["headers"])
    assert r.status_code == 200 and r.json()["email"] == "sb-fresh-2@archangelhealth.ai"


def test_seed_requires_the_doctor_password(sandbox, monkeypatch):
    monkeypatch.delenv(realm.DOCTOR_PASSWORD_VAR)
    r = client.post("/api/asclepius/sandbox/seed", headers=sandbox["headers"])
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "sandbox_doctor_password_unset"


def test_seed_refuses_outside_the_sandbox_realm(sandbox):
    with pytest.raises(sandbox_seed.NotSandbox):
        sandbox_seed.seed_sync(admin_password=ADMIN_PW, doctor_password=DOCTOR_PW)


def test_seed_needs_an_admin_not_a_physician(sandbox):
    client.post("/api/asclepius/sandbox/seed", headers=sandbox["headers"])
    tok = _login("sb-labeler-1@archangelhealth.ai", DOCTOR_PW).json()["token"]
    r = client.post("/api/asclepius/sandbox/seed", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 403


# ─── Accounts (§3.2) ─────────────────────────────────────────────────────────
def test_accounts_tab_carries_credentials_and_links(sandbox):
    client.post("/api/asclepius/sandbox/seed", headers=sandbox["headers"])
    r = client.get("/api/asclepius/sandbox/accounts", headers=sandbox["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["admin"] == {"email": sandbox_seed.ADMIN_EMAIL, "password": ADMIN_PW}
    assert len(body["physicians"]) == 10
    first = body["physicians"][0]
    assert first["name"] == "Dr. Ada Test" and first["password"] == DOCTOR_PW
    assert first["seeded"] is True and first["onboarded"] is True
    assert body["links"]["physician_onboarding"].endswith("/join?realm=sandbox")
    assert "realm=sandbox" in body["links"]["sign_in"]
    assert body["links"]["admin"] == "/sandbox/admin"


# ─── Reset (§3.2, §6.6) ──────────────────────────────────────────────────────
def test_reset_needs_the_typed_confirmation(sandbox):
    r = client.post("/api/asclepius/sandbox/reset", headers=sandbox["headers"], json={"confirm": "yes"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "confirmation_required"


def test_reset_drops_only_sandbox_files_and_reseeds(sandbox, monkeypatch):
    # Point the SANDBOX derivation at a scratch dir by moving the live env
    # paths there; the sandbox files are derived from them.
    scratch = pathlib.Path(A.TMP_DIR) / f"reset_{A.uniq()}"
    scratch.mkdir()
    (scratch / "assets").mkdir()
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(scratch / "asclepius.db"))
    monkeypatch.setenv("COMMUNITY_DB_PATH", str(scratch / "community.db"))
    monkeypatch.setenv("TEAM_DB_PATH", str(scratch / "team.db"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(scratch / "assets"))
    monkeypatch.setenv("ASCLEPIUS_EXPORT_DIR", str(scratch / "exports"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(scratch / "ingest"))
    live_db = scratch / "asclepius.db"
    live_db.write_text("LIVE — must survive")
    (scratch / "assets" / "ab").mkdir()
    (scratch / "assets" / "ab" / "abcdef").write_bytes(b"live blob")

    # Bind the sandbox realm to the DERIVED files (the fixture bound it to a
    # temp file), put an admin + a marker row in them, and sidecars beside.
    from asclepius.store import drop_store_for_realm, get_store
    from community.store import drop_community_store_for_realm, get_community_store
    from team_store import drop_team_store_for_realm, get_team_store
    drop_store_for_realm("sandbox"); drop_community_store_for_realm("sandbox"); drop_team_store_for_realm("sandbox")
    sb_paths = realm.paths("sandbox")
    with realm.scoped("sandbox"):
        admin = sandbox_seed.ensure_sandbox_admin()
        get_store().create_user(email="marker@sandbox.test", password="pw-12345678")
        get_community_store(); get_team_store()
        token = asc_auth.create_token(admin)
    for k in ("asclepius", "community", "team"):
        assert pathlib.Path(sb_paths[k]).exists()
        pathlib.Path(sb_paths[k] + "-wal").touch()
    for k in ("assets", "exports", "ingest"):
        pathlib.Path(sb_paths[k]).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(sb_paths[k]) / "x").write_text("x")

    r = client.post("/api/asclepius/sandbox/reset", headers={"Authorization": "Bearer " + token},
                    json={"confirm": "reset sandbox"})
    assert r.status_code == 200, r.text
    removed = set(r.json()["reset"]["removed"])
    assert sb_paths["asclepius"] in removed and sb_paths["asclepius"] + "-wal" in removed
    assert sb_paths["community"] in removed and sb_paths["team"] in removed
    assert sb_paths["assets"] in removed and sb_paths["exports"] in removed and sb_paths["ingest"] in removed
    # Live files untouched.
    assert live_db.read_text() == "LIVE — must survive"
    assert (scratch / "assets" / "ab" / "abcdef").read_bytes() == b"live blob"
    # Reseeded into a fresh sandbox DB at the derived path: marker gone, ten doctors present.
    assert len(r.json()["physicians"]) == 10
    with realm.scoped("sandbox"):
        assert os.path.abspath(get_store().db_path) == os.path.abspath(sb_paths["asclepius"])
        assert get_store().get_user_by_email("marker@sandbox.test") is None
        assert get_store().get_user_by_email("sb-labeler-1@archangelhealth.ai")
    drop_store_for_realm("sandbox"); drop_community_store_for_realm("sandbox"); drop_team_store_for_realm("sandbox")


def test_reset_is_a_403_before_any_file_is_touched_when_the_realm_is_live(sandbox, monkeypatch):
    """§6.6: with the context var somehow ``live``, the reset is refused
    before the filesystem is touched — at the module level too, not just the
    router's dependency."""
    touched = {"n": 0}
    monkeypatch.setattr(sandbox_seed.os, "remove", lambda *a, **k: touched.__setitem__("n", touched["n"] + 1))
    monkeypatch.setattr(sandbox_seed.shutil, "rmtree", lambda *a, **k: touched.__setitem__("n", touched["n"] + 1))
    with pytest.raises(sandbox_seed.NotSandbox):
        sandbox_seed.reset_files()
    assert touched["n"] == 0
    # The router refuses a request that reaches it in the live realm the same way.
    from routers import asclepius_sandbox as R
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        R.require_sandbox()
    assert ei.value.status_code == 403 and ei.value.detail["code"] == "not_sandbox"


def test_reset_refuses_a_path_that_is_not_a_sandbox_path():
    with pytest.raises(sandbox_seed.NotSandbox):
        sandbox_seed._assert_sandbox_path("/data/asclepius.db")
    with pytest.raises(sandbox_seed.NotSandbox):
        sandbox_seed._assert_sandbox_path("/data/assets")
    assert sandbox_seed._assert_sandbox_path("/data/asclepius_sandbox.db")
    assert sandbox_seed._assert_sandbox_path("/data/assets/sandbox")


def test_reset_with_a_live_token_is_a_401(sandbox):
    live = A.fresh_store()
    live_admin = A.make_user(live, role="admin")
    r = client.post("/api/asclepius/sandbox/reset", headers=A.headers_for(live_admin),
                    json={"confirm": "RESET SANDBOX"})
    assert r.status_code == 401 and r.json()["code"] == "realm_mismatch"


# ─── Outbox (§3.3) ───────────────────────────────────────────────────────────
def test_outbox_endpoints(sandbox):
    import email_utils
    with realm.scoped("sandbox"):
        asyncio.run(email_utils.send_html_email("sb@x.ai", "Your code", "<p>Code 654321 https://x.ai/v?t=1</p>"))
    r = client.get("/api/asclepius/sandbox/outbox", headers=sandbox["headers"])
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1 and msgs[0]["codes"] == ["654321"] and msgs[0]["links"] == ["https://x.ai/v?t=1"]
    one = client.get(f"/api/asclepius/sandbox/outbox/{msgs[0]['id']}", headers=sandbox["headers"])
    assert one.status_code == 200 and "654321" in one.json()["html"]
    assert client.delete("/api/asclepius/sandbox/outbox", headers=sandbox["headers"]).json()["cleared"] == 1
    assert client.get("/api/asclepius/sandbox/outbox", headers=sandbox["headers"]).json()["messages"] == []


# ─── The CLI (§2) ────────────────────────────────────────────────────────────
def test_cli_seed_runs_the_same_code_path(sandbox, capsys):
    from scripts import sandbox_seed as cli
    rc = cli.main(["--fresh"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sb-labeler-1@archangelhealth.ai" in out and "sb-fresh-1@archangelhealth.ai" in out
    assert ADMIN_PW not in out and DOCTOR_PW not in out
    with realm.scoped("sandbox"):
        assert sandbox["store"].get_user_by_email("sb-reviewer-3@archangelhealth.ai")


def test_cli_refuses_without_passwords(monkeypatch, capsys):
    from scripts import sandbox_seed as cli
    monkeypatch.delenv(realm.ADMIN_PASSWORD_VAR, raising=False)
    monkeypatch.delenv(realm.DOCTOR_PASSWORD_VAR, raising=False)
    assert cli.main([]) == 2
