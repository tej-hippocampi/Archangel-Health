"""The admin's way to unstick a legacy passwordless applicant.

Onboarding Master PRD §3.2 step 6, against finding F4.

The applicant-facing door is the gate card's "Set my password", and it covers
everybody who reaches it. This covers the ones who do not: the physician who
emails support instead of clicking, and the one who gave up at the sign-in form
before the card ever rendered.

The properties worth pinning are almost all about what it REFUSES. A button on
an admin console that mails password-reset links is a small credential-issuing
machine, and the things that keep it safe are the bounds on it:

  * it works only for accounts that have no password, so it can never be a way
    to mint a reset against a colleague's live credential;
  * it decides nothing — verification_status is untouched, so "help them sign
    in" cannot become an accidental approval;
  * it shares one mint with the applicant's own forgot door, so the ceiling on
    live reset tokens is a real ceiling rather than one of two copies of it;
  * it is admin-only, and it says so on the wire.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import passwords as asc_passwords
from asclepius import store as asc_store_mod


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _legacy_applicant(store):
    """The stranded set: finished the wizard during the passwordless window."""
    user = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Rosalind Achebe", credentials={}, attestations={},
    )
    store.set_verification_status(user["id"], "pending")
    return user


def _url(uid: str) -> str:
    return f"/api/asclepius/verify/queue/{uid}/password-setup-link"


# ─── It works ────────────────────────────────────────────────────────────────

def test_it_mails_a_link_that_actually_sets_the_password(client, monkeypatch):
    """End to end, with only the SEND intercepted: the raw token exists solely
    in the mail, and a button that mails an unusable link is worse than no
    button because it looks like it worked."""
    from routers import asclepius as asc_router

    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = _legacy_applicant(store)

    sent: list = []

    async def _capture(email, raw_token):
        sent.append((email, raw_token))

    monkeypatch.setattr(asc_router, "_mail_password_reset", _capture)

    r = client.post(_url(applicant["id"]), json={}, headers=headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["email"] == applicant["email"]
    assert len(sent) == 1 and sent[0][0] == applicant["email"]

    r = client.post("/api/asclepius/auth/password/reset",
                    json={"token": sent[0][1],
                          "new_password": "Corr3ct-Horse-Battery!"})
    assert r.status_code == 200, r.text

    r = client.post("/api/asclepius/auth/login",
                    json={"email": applicant["email"],
                          "password": "Corr3ct-Horse-Battery!"})
    assert r.status_code == 200, r.text


def test_helping_them_sign_in_is_not_a_decision_about_them(client, monkeypatch):
    """The one thing this must never quietly become."""
    from routers import asclepius as asc_router
    monkeypatch.setattr(asc_router, "_mail_password_reset",
                        lambda *a, **k: None)

    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = _legacy_applicant(store)

    client.post(_url(applicant["id"]), json={}, headers=headers_for(admin))
    after = store.get_user_by_id(applicant["id"])
    assert (after.get("verification_status") or "pending") == "pending"
    assert not after.get("tier")


# ─── It refuses ──────────────────────────────────────────────────────────────

def test_it_refuses_an_account_that_already_has_a_password(client):
    """Otherwise a misclick on the wrong row is a way to mint a reset against a
    working credential — the hazard _needs_credentials exists to bound."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    real = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password="Corr3ct-Horse-Battery!",
        role="evaluator", full_name="Amara Okafor", credentials={}, attestations={},
    )

    r = client.post(_url(real["id"]), json={}, headers=headers_for(admin))
    assert r.status_code == 400, r.text
    assert "already has a password" in r.json()["detail"]
    assert store.count_live_password_resets(real["id"]) == 0, (
        "a refused call must not leave a token behind"
    )


def test_it_is_admin_only(client):
    store = fresh_store()
    applicant = _legacy_applicant(store)
    evaluator = make_user(store, role="evaluator")

    assert client.post(_url(applicant["id"]), json={}).status_code in (401, 403)
    r = client.post(_url(applicant["id"]), json={}, headers=headers_for(evaluator))
    assert r.status_code == 403, r.text


def test_an_unknown_user_is_a_404(client):
    store = fresh_store()
    admin = make_user(store, role="admin")
    r = client.post(_url("no-such-user"), json={}, headers=headers_for(admin))
    assert r.status_code == 404


def test_the_reset_ceiling_is_shared_not_reimplemented(client, monkeypatch):
    """The reason mint_password_reset was extracted. Two hand-rolled copies
    drift, and the direction they drift in is an admin button that quietly
    issues tokens past the cap the applicant's own door respects."""
    from routers import asclepius as asc_router
    monkeypatch.setattr(asc_router, "_mail_password_reset",
                        lambda *a, **k: None)

    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = _legacy_applicant(store)

    for _ in range(asc_passwords.MAX_LIVE_RESETS):
        r = client.post(_url(applicant["id"]), json={}, headers=headers_for(admin))
        assert r.status_code == 200, r.text

    r = client.post(_url(applicant["id"]), json={}, headers=headers_for(admin))
    assert r.status_code == 429, r.text
    assert store.count_live_password_resets(applicant["id"]) \
        == asc_passwords.MAX_LIVE_RESETS


# ─── The queue tells the console which rows can offer it ─────────────────────

def test_the_dossier_flags_who_needs_this(client):
    store = fresh_store()
    admin = make_user(store, role="admin")
    stranded = _legacy_applicant(store)
    fine = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password="Corr3ct-Horse-Battery!",
        role="evaluator", full_name="Amara Okafor", credentials={}, attestations={},
    )
    store.set_verification_status(fine["id"], "pending")

    a = client.get(f"/api/asclepius/verify/queue/{stranded['id']}",
                   headers=headers_for(admin)).json()
    b = client.get(f"/api/asclepius/verify/queue/{fine['id']}",
                   headers=headers_for(admin)).json()
    assert a["needs_password_setup"] is True
    assert b["needs_password_setup"] is False


def test_the_dossier_still_never_carries_the_hash(client):
    """The flag is a PREDICATE precisely so the console never needs the hash."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = _legacy_applicant(store)
    body = client.get(f"/api/asclepius/verify/queue/{applicant['id']}",
                      headers=headers_for(admin)).text
    assert "password_hash" not in body
    assert asc_store_mod.NO_PASSWORD_HASH not in body
