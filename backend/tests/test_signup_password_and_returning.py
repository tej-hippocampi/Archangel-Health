"""Screen one creates an account, and a returning physician is not spammed.

Two changes from the founders' walkthrough, and they interact, so they are
pinned together.

THE RETURNING PHYSICIAN. A doctor who already had an account clicked "Become a
contributor" on the landing page and received the entire new-applicant
sequence: the "pick up any time" mail, an internal alert announcing that a
physician we onboarded months ago had just started, and a day later a nudge to
finish an application they finished long ago. Nothing on the self-serve path
looked at whether the address already had an account.

The fix is deliberately asymmetric. The CHECK happens, the ANSWER does not
change. ``/api/onboarding/self-serve`` is anonymous, and a response that
differed by branch would be a clean oracle for "is this named physician an
Archangel contributor", which is a fact about a real person's professional
affiliation and exactly what ``request_signin_link`` and ``forgot_password``
both spend effort hiding. So the body, the status and the work are identical,
and what changes is the mail.

THE PASSWORD ON SCREEN ONE. A physician used to finish the whole wizard and own
no account: a credential was minted only when an admin approved, and the way
back in was an emailed link. Screen one now takes a password.

That moves a real credential earlier in the flow, so the mailbox gate matters
more than it did. ``/asclepius/password`` has checked ``onboarding_step >= 2``
since it was written; ``/credentials``, ``/attestations`` and ``/finish`` never
did. That was survivable when finishing minted nothing. It is not survivable
now, because reaching finish without the OTP would mint a usable account on an
address the caller may not control.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, uniq

from routers import onboarding as onboarding_module

PW = "correct-horse-battery-1"

CREDS = {
    "fullLegalName": "Dr Amara Okafor",
    "npi": "1234567893",
    "degree": "MD",
    "primarySpecialty": "Nephrology",
    "phone": "5551234567",
    "currentlyActive": True,
    "licenseNumber": "A12345",
    "licenseState": "CA",
    "residencyCompleted": True,
    "practiceStatus": "active",
}
ATTS = {"accurate": True, "noPhi": True, "ip": True, "independent": True,
        "confidential": True, "noDiscipline": True, "initials": "AO"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def mail(monkeypatch):
    """Capture every send, so "nothing was sent" is assertable rather than hoped."""
    sent = []
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _capture(to, subject, body, **kw):
        sent.append({"to": to, "subject": subject})
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _capture)
    return sent


@pytest.fixture(autouse=True)
def _no_real_nppes(monkeypatch):
    from asclepius import credentialing as _cred
    monkeypatch.setattr(_cred, "fetch_npi_record",
                        lambda *a, **k: {"result": "unavailable", "reason": "test"})


def _self_serve(client, email):
    return client.post("/api/onboarding/self-serve",
                       json={"email": email, "first_name": "Amara", "last_name": "Okafor"})


def _existing_account(client, email, *, password=PW):
    """An account that can actually be signed in to, which is the predicate."""
    from asclepius.store import hash_password

    asc = client.app.state.asclepius_store
    asc.provision_user(email=email, password_hash=hash_password(password),
                       role="evaluator", full_name="Amara Okafor")
    return asc.get_user_by_email(email)


# ── The returning physician ──────────────────────────────────────────────────

def test_a_returning_physician_is_sent_no_mail_at_all(client, mail):
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    _existing_account(client, email)

    assert _self_serve(client, email).status_code == 200
    assert mail == [], f"a physician who already has an account was mailed: {mail}"


def test_a_new_physician_still_gets_the_founder_alert(client, mail):
    """The control. If nothing is sent for anybody, the test above is vacuous."""
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"

    assert _self_serve(client, email).status_code == 200
    subjects = [m["subject"] for m in mail]
    assert any("contributor started" in s for s in subjects), subjects
    # And the "pick up any time" mail is NOT one of them any more: it moved to
    # the sweep, an hour later, because at this instant the physician is looking
    # at screen one and about to ask for a verification code.
    assert not any("Pick up your" in s for s in subjects), subjects


def test_the_response_is_indistinguishable_either_way(client, mail):
    """The anti-enumeration property, and the reason the row is still minted.

    If this ever fails, an anonymous caller can ask the public endpoint whether
    a named physician contributes to Archangel, one address at a time.
    """
    fresh_store()
    fresh_email = f"dr-{uniq()}@nephrology-associates.com"
    known_email = f"dr-{uniq()}@nephrology-associates.com"
    _existing_account(client, known_email)

    a = _self_serve(client, fresh_email)
    b = _self_serve(client, known_email)
    assert a.status_code == b.status_code == 200
    assert sorted(a.json().keys()) == sorted(b.json().keys())
    for key in ("ok", "expires_at"):
        assert (key in a.json()) == (key in b.json())
    # Both got a real, resolvable onboarding URL of the same shape.
    assert a.json()["onboarding_url"].rsplit("/", 2)[-2] == "onboard"
    assert b.json()["onboarding_url"].rsplit("/", 2)[-2] == "onboard"


def test_the_returning_row_is_stamped_and_never_nudged(client, mail):
    """The stamp is what keeps the sweep away from it tomorrow.

    Without it the row is an ordinary unfinished application, and the 24-hour
    nudge arrives to chase somebody for work they completed months ago: the
    original bug, one day late.
    """
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    _existing_account(client, email)
    _self_serve(client, email)

    ts = client.app.state.team_store
    row = next(r for r in ts.list_health_systems_admin()
               if (r.get("director_email") or "").lower() == email.lower())
    assert row["existing_account_at"], "the row was not stamped"

    for kind in ("resume", "nudge", "expiry"):
        due = ts.list_unfinished_asclepius_invites(kind=kind, older_than_hours=0, limit=50)
        assert row["id"] not in [d["id"] for d in due], f"{kind} would still fire"


def test_an_application_in_review_is_not_an_account(client, mail):
    """A row with no password is an APPLICATION, and telling that person to go
    and sign in is a door with nothing behind it. They keep the full sequence."""
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    from asclepius import store as asc_store_mod

    client.app.state.asclepius_store.provision_user(
        email=email, password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Amara Okafor")

    assert _self_serve(client, email).status_code == 200
    assert any("contributor started" in m["subject"] for m in mail), mail


# ── The password on screen one ───────────────────────────────────────────────

def _invite(client, email):
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", director_email=email, product="asclepius")
    return invite["onboarding_url"].rsplit("/", 1)[-1], invite["health_system_id"]


def _prove_mailbox(client, hs_id):
    ts = client.app.state.team_store
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()


def test_screen_one_stores_a_hash_and_never_the_password(client, mail):
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, hs_id = _invite(client, email)

    r = client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Amara", "last_name": "Okafor",
        "email": email, "password": PW, "license_state": "ca"})
    assert r.status_code == 200, r.text
    assert r.json()["password_set"] is True

    ts = client.app.state.team_store
    row = ts.get_health_system_by_id(hs_id)
    assert row["director_password_hash"], "no hash was stored"
    assert row["director_password_set_at"]
    assert row["director_license_state"] == "CA", "the state was not normalised"
    assert ts.verify_team_password(PW, row["director_password_hash"])

    # The plaintext is nowhere in the database file. This is the property, not
    # the hash: a hash that exists alongside a stashed plaintext is no better
    # than no hash at all.
    with sqlite3.connect(ts.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert PW not in dump

    # And no account exists yet. The mailbox is not proven until screen two.
    assert client.app.state.asclepius_store.get_user_by_email(email) is None


def test_a_weak_password_is_refused_with_a_reason(client, mail):
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, _ = _invite(client, email)

    r = client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Amara", "last_name": "Okafor",
        "email": email, "password": "short"})
    assert r.status_code == 400
    assert r.json()["detail"], "refused without telling the physician why"


def test_screen_one_still_works_without_a_password(client, mail):
    """A browser holding the previous bundle posts the old body. It must not 400
    mid-deploy: the field is optional on the wire and gated on the client."""
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, hs_id = _invite(client, email)

    r = client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Amara", "last_name": "Okafor", "email": email})
    assert r.status_code == 200, r.text
    assert r.json()["password_set"] is False


@pytest.mark.parametrize("path,payload", [
    ("credentials", {"credentials": CREDS}),
    ("attestations", {"attestations": ATTS}),
    ("finish", {}),
])
def test_nothing_is_written_before_the_mailbox_is_proven(client, mail, path, payload):
    """The security half of moving the password earlier.

    Reaching finish without the OTP would mint a usable account on an address
    the caller may not control. The two writes before it are gated for the same
    reason one step earlier: a credential blob and a signed attestation written
    by a stranger are what an admin would be reading when they decide about a
    real person.
    """
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, _ = _invite(client, email)
    client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Amara", "last_name": "Okafor",
        "email": email, "password": PW})

    r = client.post(f"/api/onboarding/asclepius/{path}", json={"token": token, **payload})
    assert r.status_code == 403, f"{path} accepted an unverified caller: {r.text}"
    assert client.app.state.asclepius_store.get_user_by_email(email) is None


def test_the_password_from_screen_one_is_the_one_on_the_account(client, mail):
    """End to end: choose it on screen one, sign in with it after finishing."""
    from asclepius.store import verify_password

    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, hs_id = _invite(client, email)

    assert client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Amara", "last_name": "Okafor",
        "email": email, "password": PW, "license_state": "CA"}).status_code == 200
    _prove_mailbox(client, hs_id)
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": CREDS}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text

    # Still awaiting review. A password is not an approval, and the two used to
    # be the same boolean.
    assert r.json()["awaiting_review"] is True
    assert any("got your application" in m["subject"].lower() for m in mail), \
        [m["subject"] for m in mail]
    assert not any("workspace is ready" in m["subject"].lower() for m in mail), \
        [m["subject"] for m in mail]

    user = client.app.state.asclepius_store.get_user_by_email(email)
    assert user and verify_password(PW, user["password_hash"])
    assert not user.get("must_change_password"), \
        "a password the physician chose was flagged as temporary"
    assert user["verification_status"] == "pending"


def test_the_session_reports_the_password_without_leaking_it(client, mail):
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, _ = _invite(client, email)
    client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Amara", "last_name": "Okafor",
        "email": email, "password": PW, "license_state": "NY"})

    body = client.get(f"/api/onboarding/session?token={token}").json()
    assert body["director_password_set"] is True
    assert body["director_license_state"] == "NY"
    assert "director_password_hash" not in body
    assert PW not in str(body)
