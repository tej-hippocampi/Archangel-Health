"""Signing up should leave you signed in.

It did not. A physician completed the wizard, was told their workspace was
ready, and got a login screen.

The last round fixed the wrong half. ``/asclepius/finish`` was minting no token
at all because it called ``authenticate(..., director_pwd)`` and that name did
not exist in the function; that is fixed and the token comes back. But the
wizard then handed it over by writing ``localStorage["asclepius_token"]``, and
in production the two surfaces are different origins:

    landing   https://archangelhealth.ai       <- wrote the token here
    portal    https://app.archangelhealth.ai   <- read it from here

localStorage is partitioned by origin, so the portal's ``boot()`` found nothing
and rendered the login screen. It failed for every signup in production and
worked in local dev, where both are served off :8000 -- which is why it lived
through a review that was looking at the token and not at the transport.

There is already a correct mechanism (``/auth/portal-handoff``, a 60-second
single-use code) and ``SignInDialog`` was already using it. These tests hold the
server side of all of it, plus the second half of the same complaint: an
onboarding link opened by somebody who already has an account should offer them
a sign-in rather than walking them through a signup that quietly repoints their
password.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, uniq

_LANDING = Path(__file__).resolve().parents[2] / "landing" / "src"
_WIZARD = _LANDING / "app" / "components" / "OnboardingWizard.tsx"
_AUTH_API = _LANDING / "lib" / "auth-api.ts"

PW = "correct-horse-battery-1"


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    from routers import onboarding as onboarding_module

    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _stub_send(*_a, **_k):
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _stub_send)
    from asclepius import credentialing as _cred
    monkeypatch.setattr(_cred, "fetch_npi_record",
                        lambda *a, **k: {"result": "unavailable", "reason": "test"})


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _fresh(client):
    """A fresh store that BOTH code paths see.

    ``fresh_store()`` rebinds the module-level store, but
    ``onboarding._asclepius_store(request)`` prefers ``app.state.asclepius_store``
    (set once at startup) and ``asclepius.auth`` reads the module-level one. In
    production those are the same object; in a test they diverge, so onboarding
    provisions into one store and authentication looks in the other. Point them
    at the same place rather than writing tests against a split brain.
    """
    store = fresh_store()
    client.app.state.asclepius_store = store
    return store


def _invite(client, *, email=None, flavor=None, verified=True):
    ts = client.app.state.team_store
    email = email or f"dr-{uniq()}@hospital.org"
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", director_email=email,
        product="asclepius")
    hs_id = invite["health_system_id"]
    ts.update_health_system_director_identity(
        hs_id, first_name="Amara", last_name="Okafor", email=email)
    if flavor:
        ts.set_health_system_signup_flavor(hs_id, flavor)
    if verified:
        with sqlite3.connect(ts.db_path) as conn:
            conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?",
                         (hs_id,))
            conn.commit()
    return invite["onboarding_url"].rsplit("/", 1)[-1], email


# ─── The token, and what can be done with it ─────────────────────────────────
def test_finishing_hands_back_a_session_token(client):
    _fresh(client)
    token, _ = _invite(client, flavor="advisor")
    client.post("/api/onboarding/asclepius/password", json={"token": token, "password": PW})
    body = client.post("/api/onboarding/asclepius/finish", json={"token": token}).json()
    assert body.get("token")


def test_an_invited_member_gets_one_too(client):
    """/member/finish returned no token AT ALL, so an invited clinician could
    never land signed in by any route."""
    import inspect

    from routers import onboarding

    src = inspect.getsource(onboarding.member_finish)
    assert '"token": session_token' in src, (
        "/member/finish is not minting a session; invited clinicians land logged out")


def test_that_token_can_be_traded_for_a_handoff_code(client):
    """The whole path, end to end: finish -> handoff -> redeem -> a usable
    session. This is what "lands in the product signed in" actually means."""
    _fresh(client)
    token, email = _invite(client, flavor="advisor")
    client.post("/api/onboarding/asclepius/password", json={"token": token, "password": PW})
    session = client.post("/api/onboarding/asclepius/finish",
                          json={"token": token}).json()["token"]

    made = client.post("/api/asclepius/auth/portal-handoff",
                       headers={"Authorization": f"Bearer {session}"})
    assert made.status_code == 200, made.text
    code = made.json()["handoff_code"]

    redeemed = client.post("/api/asclepius/auth/portal-handoff/consume",
                           json={"handoff_code": code})
    assert redeemed.status_code == 200, redeemed.text
    portal_token = redeemed.json()["token"]

    me = client.get("/api/asclepius/me/profile",
                    headers={"Authorization": f"Bearer {portal_token}"})
    assert me.status_code == 200
    assert me.json()["credentials"]["email"] == email


def test_a_handoff_code_is_single_use(client):
    _fresh(client)
    token, _ = _invite(client, flavor="advisor")
    client.post("/api/onboarding/asclepius/password", json={"token": token, "password": PW})
    session = client.post("/api/onboarding/asclepius/finish",
                          json={"token": token}).json()["token"]
    code = client.post("/api/asclepius/auth/portal-handoff",
                       headers={"Authorization": f"Bearer {session}"}).json()["handoff_code"]
    assert client.post("/api/asclepius/auth/portal-handoff/consume",
                       json={"handoff_code": code}).status_code == 200
    assert client.post("/api/asclepius/auth/portal-handoff/consume",
                       json={"handoff_code": code}).status_code == 404


# ─── The transport, pinned in the source ─────────────────────────────────────
def test_the_wizard_does_not_hand_the_session_over_in_localstorage():
    """The bug, as a test. localStorage cannot cross an origin, and in
    production these two ARE different origins."""
    src = _WIZARD.read_text(encoding="utf-8")
    # The CALL, not the word: the note explaining why it is gone names it.
    assert "authApi.storeAsclepiusSession(" not in src
    assert "redirectToAsclepiusPortal" in src


def test_the_function_that_caused_it_is_gone_rather_than_unused():
    """Deleted, not left lying around. It looks exactly like the thing you
    want, and the next person will reach for it."""
    src = _AUTH_API.read_text(encoding="utf-8")
    assert "export function storeAsclepiusSession" not in src
    assert 'localStorage.setItem("asclepius_token"' not in src


def test_the_success_screen_hands_off_rather_than_plain_redirecting():
    src = _WIZARD.read_text(encoding="utf-8")
    m = re.search(r"openAsclepiusWorkspace = useCallback\(async \(\) => \{(.*?)\}, \[",
                  src, re.S)
    assert m, "openAsclepiusWorkspace changed shape; re-check it still hands off"
    body = m.group(1)
    assert "redirectToAsclepiusPortal" in body
    # And still falls back to the plain redirect, because a doctor who can sign
    # in beats one staring at an error on the success page.
    assert "window.location.href" in body


# ─── Already have an account ─────────────────────────────────────────────────
def test_an_invite_for_an_existing_account_says_so(client):
    """Opening a /join link with an address that already has an account used to
    walk the whole wizard and reach finish, which passes password_hash
    unconditionally -- silently repointing the live account's password to
    whatever got typed on the way through."""
    store = _fresh(client)
    email = f"dr-{uniq()}@hospital.org"
    store.provision_user(email=email, password="an-existing-password-1",
                         role="evaluator", full_name="Amara Okafor")
    token, _ = _invite(client, email=email)

    body = client.get(f"/api/onboarding/session?token={token}").json()
    assert body["status"] == "account_exists"


def test_a_fresh_address_still_reports_pending(client):
    _fresh(client)
    token, _ = _invite(client)
    body = client.get(f"/api/onboarding/session?token={token}").json()
    assert body["status"] == "pending"


def test_an_account_with_no_password_is_not_treated_as_existing(client):
    """A row provisioned by some other path without a credential is not an
    account anybody can sign into, so sending them to a login screen would be
    a dead end."""
    store = _fresh(client)
    email = f"dr-{uniq()}@hospital.org"
    user = store.provision_user(email=email, password="tmp-password-123",
                                role="evaluator")
    with store._conn() as conn:
        conn.execute("UPDATE users SET password_hash = '' WHERE id = ?", (user["id"],))
    token, _ = _invite(client, email=email)
    assert client.get(f"/api/onboarding/session?token={token}").json()["status"] == "pending"


def test_the_wizard_routes_both_terminal_states_to_a_sign_in():
    """The old behaviour sent them to the SUCCESS screen, which says "You're
    already signed in" (they are not) and whose button drops them on an
    unauthenticated portal."""
    src = _WIZARD.read_text(encoding="utf-8")
    assert 'd.status === "account_exists"' in src
    assert 'setStep("ascSignIn")' in src
    assert "StepAsclepiusSignIn" in src


def test_the_dead_end_error_screen_offers_a_way_out():
    """An expired or already-used link overwhelmingly belongs to somebody who
    already has an account. It used to render a sentence and nothing else."""
    src = _WIZARD.read_text(encoding="utf-8")
    i = src.index("if (bootError)")
    block = src[i:i + 1400]
    assert "Sign in" in block
