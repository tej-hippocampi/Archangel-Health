"""Physician-chosen passwords, recovery, and what a reset actually revokes.

Before this, the system generated a password, mailed it, and had no way to
change or recover it. The properties worth pinning are not "the endpoint
returns 200" but the ones that make the flow safe to expose publicly:

  * /forgot cannot be used to discover whether an address has an account;
  * a reset link works once, dies on time, and dies when superseded;
  * a completed reset ENDS sessions that were already open, which is most of
    what a reset is for;
  * a physician who had a system-generated password can still sign in, and can
    use the new flow to move off it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user, token_for

from asclepius import auth as asc_auth
from asclepius import passwords as asc_passwords
from asclepius import store as asc_store

_FORGOT = "/api/asclepius/auth/password/forgot"
_RESET = "/api/asclepius/auth/password/reset"
_CHANGE = "/api/asclepius/auth/password/change"
_LOGIN = "/api/asclepius/auth/login"
_ME = "/api/asclepius/auth/me"

GOOD = "correct-horse-battery-1"
ALSO_GOOD = "a-different-good-one-2"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _mint_reset(store, user) -> str:
    raw, hashed = asc_passwords.new_reset_token()
    store.create_password_reset(
        user_id=user["id"], token_hash=hashed, expires_at=asc_passwords.reset_expires_at()
    )
    return raw


# ─── Enumeration safety ──────────────────────────────────────────────────────

def test_forgot_answers_identically_for_a_real_and_an_unknown_address(client):
    store = fresh_store()
    user = make_user(store, email="real@example.org")

    known = client.post(_FORGOT, json={"email": "real@example.org"})
    unknown = client.post(_FORGOT, json={"email": "nobody@example.org"})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


def test_forgot_does_not_mint_a_token_for_an_address_with_no_account(client):
    store = fresh_store()
    client.post(_FORGOT, json={"email": "nobody@example.org"})
    with store._conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM password_resets").fetchone()["n"]
    assert n == 0


def test_the_unknown_address_event_carries_no_entity_id(client):
    """The provenance log must not imply an account exists for an address that
    has none."""
    store = fresh_store()
    client.post(_FORGOT, json={"email": "nobody@example.org"})
    with store._conn() as conn:
        row = conn.execute(
            "SELECT entity_id FROM events WHERE event_type = 'password_reset_requested_unknown'"
        ).fetchone()
    assert row is not None
    assert row["entity_id"] is None


def test_a_capped_out_account_still_gets_the_same_answer(client):
    store = fresh_store()
    user = make_user(store, email="busy@example.org")
    for _ in range(asc_passwords.MAX_LIVE_RESETS + 2):
        res = client.post(_FORGOT, json={"email": "busy@example.org"})
        assert res.status_code == 200
    assert store.count_live_password_resets(user["id"]) == asc_passwords.MAX_LIVE_RESETS


# ─── The reset itself ────────────────────────────────────────────────────────

def test_a_reset_link_works_exactly_once(client):
    store = fresh_store()
    user = make_user(store, email="once@example.org")
    raw = _mint_reset(store, user)

    first = client.post(_RESET, json={"token": raw, "new_password": GOOD})
    assert first.status_code == 200
    assert first.json()["token"]

    second = client.post(_RESET, json={"token": raw, "new_password": ALSO_GOOD})
    assert second.status_code == 400


def test_expired_used_and_never_existed_are_indistinguishable(client):
    """A person holding a bad link learns nothing about which kind of bad."""
    store = fresh_store()
    user = make_user(store, email="opaque@example.org")

    raw_used = _mint_reset(store, user)
    client.post(_RESET, json={"token": raw_used, "new_password": GOOD})

    raw_exp, hashed = asc_passwords.new_reset_token()
    past = (datetime.utcnow() - timedelta(minutes=5)).replace(microsecond=0).isoformat()
    store.create_password_reset(user_id=user["id"], token_hash=hashed, expires_at=past)

    bodies = set()
    for tok in (raw_used, raw_exp, "never-was-a-token"):
        res = client.post(_RESET, json={"token": tok, "new_password": ALSO_GOOD})
        assert res.status_code == 400
        bodies.add(res.json()["detail"])
    assert len(bodies) == 1, bodies


def test_a_weak_password_does_not_burn_the_link(client):
    """Rejecting the password before consuming the token means a typo does not
    strand someone with a dead link and no way back."""
    store = fresh_store()
    user = make_user(store, email="typo@example.org")
    raw = _mint_reset(store, user)

    weak = client.post(_RESET, json={"token": raw, "new_password": "short"})
    assert weak.status_code == 400

    ok = client.post(_RESET, json={"token": raw, "new_password": GOOD})
    assert ok.status_code == 200


def test_completing_a_reset_kills_every_other_live_link(client):
    store = fresh_store()
    user = make_user(store, email="rotate@example.org")
    first_raw = _mint_reset(store, user)
    second_raw = _mint_reset(store, user)

    assert client.post(_RESET, json={"token": second_raw, "new_password": GOOD}).status_code == 200
    assert client.post(_RESET, json={"token": first_raw, "new_password": ALSO_GOOD}).status_code == 400


def test_the_new_password_actually_signs_in(client):
    store = fresh_store()
    user = make_user(store, email="works@example.org")
    raw = _mint_reset(store, user)
    client.post(_RESET, json={"token": raw, "new_password": GOOD})

    res = client.post(_LOGIN, json={"email": "works@example.org", "password": GOOD})
    assert res.status_code == 200


# ─── What a reset revokes ────────────────────────────────────────────────────

def test_a_reset_ends_a_session_that_was_already_open(client):
    """The point of a reset is to evict whoever is already in, not merely to
    change what the owner types next time."""
    store = fresh_store()
    user = make_user(store, email="evict@example.org")
    stolen = headers_for(user)
    assert client.get(_ME, headers=stolen).status_code == 200

    time.sleep(2)  # password_changed_at is stamped at second resolution
    raw = _mint_reset(store, user)
    client.post(_RESET, json={"token": raw, "new_password": GOOD})

    assert client.get(_ME, headers=stolen).status_code == 401


def test_the_token_handed_back_by_the_reset_works_immediately(client):
    store = fresh_store()
    user = make_user(store, email="signedin@example.org")
    raw = _mint_reset(store, user)
    res = client.post(_RESET, json={"token": raw, "new_password": GOOD})
    fresh = {"Authorization": f"Bearer {res.json()['token']}"}
    assert client.get(_ME, headers=fresh).status_code == 200


# ─── Change, while signed in ─────────────────────────────────────────────────

def test_change_requires_the_current_password(client):
    store = fresh_store()
    user = make_user(store, email="change@example.org")
    store.set_user_password(user["id"], GOOD, stamp_changed=False)
    user = store.get_user_by_id(user["id"])

    wrong = client.post(_CHANGE, headers=headers_for(user),
                        json={"current_password": "not-it-at-all", "new_password": ALSO_GOOD})
    assert wrong.status_code == 400

    right = client.post(_CHANGE, headers=headers_for(user),
                        json={"current_password": GOOD, "new_password": ALSO_GOOD})
    assert right.status_code == 200
    assert right.json()["token"]


def test_a_pending_physician_can_still_change_their_own_password(client):
    """Everything else on the evaluator surface is gated on verification. A
    person's own credential must not be."""
    store = fresh_store()
    user = make_user(store, email="pending@example.org", tier=None)
    store.set_user_password(user["id"], GOOD, stamp_changed=False)
    store.set_verification_status(user["id"], "pending")
    user = store.get_user_by_id(user["id"])

    res = client.post(_CHANGE, headers=headers_for(user),
                      json={"current_password": GOOD, "new_password": ALSO_GOOD})
    assert res.status_code == 200


def test_change_is_refused_without_a_token(client):
    assert client.post(_CHANGE, json={"current_password": "x", "new_password": GOOD}).status_code == 401


# ─── Existing accounts keep working ──────────────────────────────────────────

def test_an_account_with_a_system_generated_password_still_signs_in(client):
    """Nobody is force-reset. A physician mid-case does not get logged out
    because we shipped a feature."""
    store = fresh_store()
    user = make_user(store, email="legacy@example.org")
    store.set_user_password(user["id"], "Kf3-tQ92mXbW7p", stamp_changed=False)

    assert store.get_user_by_id(user["id"])["password_changed_at"] is None
    res = client.post(_LOGIN, json={"email": "legacy@example.org", "password": "Kf3-tQ92mXbW7p"})
    assert res.status_code == 200
    assert client.get(_ME, headers={"Authorization": f"Bearer {res.json()['token']}"}).status_code == 200


def test_re_onboarding_does_not_reset_a_live_physicians_password(client):
    """provision_user used to rewrite password_hash unconditionally, so any
    re-run of onboarding silently locked the physician out of an account they
    were working in."""
    store = fresh_store()
    store.provision_user(email="stable@example.org", password=GOOD, specialty="Nephrology")
    before = store.get_user_by_email("stable@example.org")["password_hash"]

    store.provision_user(email="stable@example.org", specialty="Nephrology", full_name="Dr. Stable")

    assert store.get_user_by_email("stable@example.org")["password_hash"] == before
    assert client.post(_LOGIN, json={"email": "stable@example.org", "password": GOOD}).status_code == 200
