"""The landing → Asclepius sign-in handoff.

The landing SPA and the Asclepius portal can be different origins, so the
landing page cannot write ``localStorage['asclepius_token']`` for the portal.
It trades the token for a short-lived, single-use, server-held code and sends
the browser to ``/asclepius?asc_handoff=<code>``.

The properties that make that safe are the ones worth pinning:

  * the raw JWT never travels in a URL (browser history, access logs, Referer);
  * a code works exactly once, so a leaked Referer cannot be replayed;
  * a code expires, so one captured out of a log is dead by the time it is read;
  * minting is not stricter than ``/auth/login`` — a physician still awaiting
    credential verification signs in fine on the portal directly, and must not
    be refused one hop earlier just because they came via the landing page.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user, token_for

from routers import asclepius as asc_router

_MINT = "/api/asclepius/auth/portal-handoff"
_CONSUME = "/api/asclepius/auth/portal-handoff/consume"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_handoffs():
    asc_router._ASC_HANDOFF_STORE.clear()
    yield
    asc_router._ASC_HANDOFF_STORE.clear()


def test_minting_requires_an_asclepius_bearer_token(client):
    assert client.post(_MINT).status_code == 401


def test_the_code_is_not_the_token(client):
    """The whole point: what lands in the URL must not be the credential."""
    store = fresh_store()
    user = make_user(store)
    token = token_for(user)

    res = client.post(_MINT, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    code = res.json()["handoff_code"]

    assert code != token
    assert token not in code
    assert res.json()["expires_in_seconds"] == asc_router._ASC_HANDOFF_TTL_SECONDS


def test_consuming_returns_the_original_token_exactly_once(client):
    store = fresh_store()
    user = make_user(store)
    token = token_for(user)

    code = client.post(_MINT, headers={"Authorization": f"Bearer {token}"}).json()["handoff_code"]

    first = client.post(_CONSUME, json={"handoff_code": code})
    assert first.status_code == 200
    assert first.json()["token"] == token

    # Replay of a code captured from a Referer header or a shared link.
    second = client.post(_CONSUME, json={"handoff_code": code})
    assert second.status_code == 404


def test_an_empty_code_is_rejected_before_the_lookup(client):
    res = client.post(_CONSUME, json={"handoff_code": "   "})
    assert res.status_code == 400


def test_an_unknown_code_is_a_404_and_not_a_500(client):
    res = client.post(_CONSUME, json={"handoff_code": "not-a-real-code"})
    assert res.status_code == 404


def test_an_expired_code_is_dead_even_though_it_was_never_used(client):
    store = fresh_store()
    user = make_user(store)
    token = token_for(user)

    code = client.post(_MINT, headers={"Authorization": f"Bearer {token}"}).json()["handoff_code"]
    # Age it past the TTL rather than sleeping through a real minute.
    asc_router._ASC_HANDOFF_STORE[code]["expires_at"] = datetime.utcnow() - timedelta(seconds=1)

    assert client.post(_CONSUME, json={"handoff_code": code}).status_code == 404
    assert code not in asc_router._ASC_HANDOFF_STORE  # swept, not merely refused


def test_a_pending_physician_can_still_mint_a_handoff(client):
    """Minting must not be stricter than /auth/login, which hands a pending
    physician a token unconditionally. Refusing here would mean signing in from
    the landing page fails for someone who can sign in on the portal directly."""
    store = fresh_store()
    user = make_user(store, tier=None)
    store.set_verification_status(user["id"], "pending")
    user = store.get_user_by_id(user["id"])
    token = token_for(user)

    res = client.post(_MINT, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert client.post(_CONSUME, json={"handoff_code": res.json()["handoff_code"]}).json()["token"] == token


def test_a_landing_plane_token_cannot_mint_an_asclepius_handoff(client):
    """The two planes are signed with different secrets; a tenant/landing JWT
    must not resolve here."""
    res = client.post(_MINT, headers={"Authorization": "Bearer not.a.valid-asclepius-jwt"})
    assert res.status_code == 401
