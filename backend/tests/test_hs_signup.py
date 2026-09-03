"""Self-signup for the health-system portal.

The most important test in this file is
``test_signing_up_as_an_existing_partner_cannot_read_their_uploads``. Everything
else here is hygiene; that one is the reason the feature is shaped the way it
is, and weakening it re-opens a cross-tenant read of a real hospital's data.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

GOOD_PASSWORD = "harbor-thistle-meadow-41"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    # No transport, not production: the route logs the code instead of mailing
    # it, which is the only way this flow is testable without a network.
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("EMAIL_DEV_MODE", raising=False)
    monkeypatch.setenv("ENV", "test")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _client() -> TestClient:
    """https:// because the session cookie is unconditionally Secure; a plain
    http client would silently exercise a session-less portal."""
    return TestClient(A.app, base_url="https://testserver")


def _signup(client, *, email=None, org="St Mary's Health", name="Dana Reyes",
            password=GOOD_PASSWORD, honeypot=""):
    email = email or f"it+{uuid.uuid4().hex[:8]}@stmarys.org"
    r = client.post("/api/asclepius/hs/signup", json={
        "full_name": name, "email": email, "organization": org,
        "password": password, "company_website": honeypot,
    })
    return email, r


def _code_from_log(caplog) -> str:
    for rec in reversed(caplog.records):
        m = re.search(r"code for \S+ is (\d{6})", rec.getMessage())
        if m:
            return m.group(1)
    raise AssertionError("no code was logged")


def _counts():
    store = _store()
    with store._conn() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("hs_signups", "health_systems", "hs_portal_users")
        }


# ─── The one that matters ────────────────────────────────────────────────────

def test_signing_up_as_an_existing_partner_cannot_read_their_uploads(caplog):
    """A stranger types a real partner's name into the signup form.

    ensure_health_system is create-or-reuse by case-insensitive name and
    list_uploads_for_health_system scopes on hs_id alone, so wiring signup to the
    obvious store method would have handed this person Mass General's entire
    upload history. They must get their own row and see nothing.
    """
    store = _store()
    incumbent = store.ensure_health_system("Mass General Hospital", contact_email="it@mgh.org")
    store.insert_ingest_upload(
        upload_id="up-incumbent", link_id="hs-portal", partner_id=incumbent["hs_id"],
        filename="mgh-export.zip", sha256="a" * 64, size_bytes=1234,
        raw_path=None, source_ip=None)
    store.set_upload_health_system("up-incumbent", incumbent["hs_id"])

    client = _client()
    with caplog.at_level("WARNING"):
        email, r = _signup(client, org="mass general hospital")  # different case, same name
        assert r.status_code == 200
        code = _code_from_log(caplog)
    r = client.post("/api/asclepius/hs/signup/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text

    impostor_hs = store.get_hs_portal_user(r.json()["username"])["hs_id"]
    assert impostor_hs != incumbent["hs_id"], "SELF-SIGNUP WAS ATTACHED TO THE INCUMBENT"

    # And the session really cannot see the other hospital's history. Approved
    # first: the uploads list is gated on the upload surface like every other
    # upload door, and a 403 from a self-signup still in review would prove the
    # approval gate rather than the tenancy boundary this test is about.
    store.set_hs_approval(r.json()["username"], "approved", by="test")
    hist = client.get("/api/asclepius/hs/uploads")
    assert hist.status_code == 200
    assert hist.json()["uploads"] == []
    assert len(store.list_uploads_for_health_system(incumbent["hs_id"])) == 1


# ─── The happy path ──────────────────────────────────────────────────────────

def test_signup_verify_signs_them_in_without_a_forced_reset(caplog):
    client = _client()
    with caplog.at_level("WARNING"):
        email, r = _signup(client)
        assert r.status_code == 200 and r.json()["ok"] is True
        code = _code_from_log(caplog)

    # Nothing durable exists until the code comes back.
    assert _counts()["hs_portal_users"] == 0

    r = client.post("/api/asclepius/hs/signup/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["organization"] == "St Mary's Health"
    # Derived from the org name, which is why the welcome mail has to state it.
    assert body["username"] == "stmarys"
    # The regression this guards: create_hs_portal_user hardcoded must_reset=1,
    # which would ask someone to replace the password they chose a minute ago.
    assert body["must_reset"] is False

    me = client.get("/api/asclepius/hs/me")
    assert me.status_code == 200
    assert me.json()["must_reset"] is False
    assert me.json()["account_state"] == "in review"
    assert me.json()["intake_needed"] is True
    assert "upload" not in me.json()["surfaces"]


def test_the_chosen_password_is_the_one_that_works(caplog):
    """The password is hashed at staging and the plaintext is gone by the time
    the account exists, so it is carried across as a hash. If that carry breaks,
    signup silently creates an account nobody can ever sign into again."""
    client = _client()
    with caplog.at_level("WARNING"):
        email, _ = _signup(client)
        code = _code_from_log(caplog)
    username = client.post("/api/asclepius/hs/signup/verify",
                           json={"email": email, "code": code}).json()["username"]

    fresh = _client()  # new cookie jar: prove the password, not the session
    r = fresh.post("/api/asclepius/hs/login",
                   json={"username": username, "password": GOOD_PASSWORD})
    assert r.status_code == 200, "the password they chose does not work"
    assert fresh.post("/api/asclepius/hs/login",
                      json={"username": username, "password": "wrong-one-entirely"}
                      ).status_code == 401


# ─── Abuse guards ────────────────────────────────────────────────────────────

def test_honeypot_writes_nothing_and_looks_identical():
    client = _client()
    before = _counts()
    _, real = _signup(client, email="human@stmarys.org")
    _, bot = _signup(client, email="bot@spam.example", honeypot="http://spam.example")
    assert bot.status_code == real.status_code
    assert bot.json() == real.json(), "the decoy is distinguishable from the real answer"
    after = _counts()
    # The real signup staged one row; the bot staged none.
    assert after["hs_signups"] == before["hs_signups"] + 1
    assert after["health_systems"] == before["health_systems"]
    assert after["hs_portal_users"] == before["hs_portal_users"]


def test_per_email_cap_is_silent_rather_than_confirming_the_address():
    """Onboarding answers an over-cap address with a 429 whose text confirms we
    have seen it. A signup form is the cheapest enumeration oracle there is, so
    this one returns the same body it always returns."""
    client = _client()
    email = "repeat@stmarys.org"
    bodies = []
    for _ in range(5):
        _, r = _signup(client, email=email)
        bodies.append((r.status_code, json.dumps(r.json(), sort_keys=True)))
    assert len(set(bodies)) == 1, f"over-cap responses differ: {bodies}"
    # Capped at 3 staged rows, and the later attempts wrote nothing.
    assert _counts()["hs_signups"] == 3


def test_a_weak_password_is_refused_before_anything_is_staged():
    client = _client()
    _, r = _signup(client, password="short")
    assert r.status_code == 400
    assert "12" in r.json()["detail"]
    assert _counts()["hs_signups"] == 0


def test_missing_details_are_refused():
    client = _client()
    _, r = _signup(client, name="")
    assert r.status_code == 400
    _, r = _signup(client, org="")
    assert r.status_code == 400


# ─── Code handling ───────────────────────────────────────────────────────────

def test_five_wrong_codes_kill_the_challenge(caplog):
    client = _client()
    with caplog.at_level("WARNING"):
        email, _ = _signup(client)
        code = _code_from_log(caplog)
    for _ in range(5):
        r = client.post("/api/asclepius/hs/signup/verify",
                        json={"email": email, "code": "000000"})
        assert r.status_code == 400
    # The correct code no longer works: a six-digit secret must not be grindable.
    r = client.post("/api/asclepius/hs/signup/verify", json={"email": email, "code": code})
    assert r.status_code == 400
    assert _counts()["hs_portal_users"] == 0


def test_a_code_cannot_be_replayed(caplog):
    client = _client()
    with caplog.at_level("WARNING"):
        email, _ = _signup(client)
        code = _code_from_log(caplog)
    assert client.post("/api/asclepius/hs/signup/verify",
                       json={"email": email, "code": code}).status_code == 200
    second = _client()
    assert second.post("/api/asclepius/hs/signup/verify",
                       json={"email": email, "code": code}).status_code == 400
    assert _counts()["hs_portal_users"] == 1


def test_verifying_an_unknown_address_fails_the_same_way(caplog):
    client = _client()
    with caplog.at_level("WARNING"):
        email, _ = _signup(client)
        _code_from_log(caplog)
    unknown = client.post("/api/asclepius/hs/signup/verify",
                          json={"email": "nobody@nowhere.example", "code": "123456"})
    wrong = client.post("/api/asclepius/hs/signup/verify",
                        json={"email": email, "code": "000000"})
    assert unknown.status_code == wrong.status_code == 400
    assert unknown.json() == wrong.json()


def test_resend_keeps_the_password_and_always_answers_the_same(caplog):
    """Resend has to mint a new row, because the old code is stored hashed and
    cannot be recovered. The password must survive that, or the account is
    created with one its owner never chose."""
    client = _client()
    with caplog.at_level("WARNING"):
        email, _ = _signup(client)
        known = client.post("/api/asclepius/hs/signup/resend", json={"email": email})
        code2 = _code_from_log(caplog)
    unknown = client.post("/api/asclepius/hs/signup/resend",
                          json={"email": "nobody@nowhere.example"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()

    r = client.post("/api/asclepius/hs/signup/verify", json={"email": email, "code": code2})
    assert r.status_code == 200
    fresh = _client()
    assert fresh.post("/api/asclepius/hs/login",
                      json={"username": r.json()["username"], "password": GOOD_PASSWORD}
                      ).status_code == 200


# ─── Secrets at rest ─────────────────────────────────────────────────────────

def test_no_secret_is_stored_or_returned_in_the_clear(caplog):
    client = _client()
    with caplog.at_level("WARNING"):
        email, r = _signup(client)
        code = _code_from_log(caplog)
    assert code not in r.text and GOOD_PASSWORD not in r.text

    store = _store()
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM hs_signups WHERE email = ?", (email,)).fetchone()
    assert row["code_hash"] != code
    assert row["password_hash"] != GOOD_PASSWORD
    from asclepius.store import verify_password
    assert verify_password(code, row["code_hash"])
    assert verify_password(GOOD_PASSWORD, row["password_hash"])
