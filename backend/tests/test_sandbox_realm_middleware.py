"""Sandbox PRD §1.3 and §6.2 — how a request gets its realm, and why a token's
realm always wins.

  * sandbox OFF (no ``ASCLEPIUS_SANDBOX_ADMIN_PASSWORD``): ``/sandbox/*`` is
    404 and the header is ignored — the feature is dark (§7);
  * sandbox ON: the ``X-Asclepius-Realm`` header routes an unauthenticated
    login to the sandbox DB, whose accounts do not exist in live (the stated
    sign-in asymmetry);
  * a token minted in one realm never authenticates in the other, whichever
    way the header or path points — 401 ``realm_mismatch`` (§6.2).
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

import realm  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)

PW = "pw-12345678"


@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "sandbox-admin-secret")
    monkeypatch.setenv(realm.DOCTOR_PASSWORD_VAR, "sandbox-doctor-secret")
    yield


@pytest.fixture
def two_realms(sandbox_on):
    """A fresh live store and a fresh sandbox store, each with one evaluator
    whose email exists ONLY in its own realm."""
    live = A.fresh_store()
    live_user = A.make_user(live, email="live-only@asclepius.example.com")
    with realm.scoped("sandbox"):
        sb = A.fresh_store()
        sb_user = A.make_user(sb, email="sb-only@asclepius.example.com")
        sb_token = asc_auth.create_token(sb_user)
    live_token = asc_auth.create_token(live_user)
    return {"live": live, "sandbox": sb, "live_user": live_user, "sb_user": sb_user,
            "live_token": live_token, "sb_token": sb_token}


# ─── Dark until switched on (§7) ─────────────────────────────────────────────
def test_sandbox_routes_404_when_disabled(monkeypatch):
    monkeypatch.delenv(realm.ADMIN_PASSWORD_VAR, raising=False)
    assert not realm.enabled()
    for path in ("/sandbox/admin", "/sandbox/asclepius", "/sandbox/provider",
                 "/sandbox/buyer", "/sandbox/community", "/api/asclepius/sandbox/seed"):
        r = client.get(path) if path.startswith("/sandbox") else client.post(path)
        assert r.status_code == 404, (path, r.status_code, r.text)


def test_header_is_ignored_when_disabled(monkeypatch):
    monkeypatch.delenv(realm.ADMIN_PASSWORD_VAR, raising=False)
    live = A.fresh_store()
    A.make_user(live, email="live-x@asclepius.example.com")
    with realm.scoped("sandbox"):
        sb = A.fresh_store()
        A.make_user(sb, email="sb-x@asclepius.example.com")
    # The header names the sandbox, but the realm is dark: this is a LIVE login
    # and the live DB has no such user.
    r = client.post("/api/asclepius/auth/login", headers={realm.HEADER: "sandbox"},
                    json={"email": "sb-x@asclepius.example.com", "password": PW})
    assert r.status_code == 401


# ─── The header on unauthenticated entry (§1.3) ──────────────────────────────
def test_login_with_header_lands_in_sandbox_and_without_it_is_a_401(two_realms):
    body = {"email": "sb-only@asclepius.example.com", "password": PW}
    r = client.post("/api/asclepius/auth/login", headers={realm.HEADER: "sandbox"}, json=body)
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    payload = asc_auth.decode_token(tok)
    assert payload[realm.CLAIM] == "sandbox"
    # Same credentials, no header → plain 401: the live DB has no such user.
    r = client.post("/api/asclepius/auth/login", json=body)
    assert r.status_code == 401
    # And a live user cannot stumble into the sandbox.
    r = client.post("/api/asclepius/auth/login", headers={realm.HEADER: "sandbox"},
                    json={"email": "live-only@asclepius.example.com", "password": PW})
    assert r.status_code == 401


def test_live_login_still_works_and_mints_a_live_token(two_realms):
    r = client.post("/api/asclepius/auth/login",
                    json={"email": "live-only@asclepius.example.com", "password": PW})
    assert r.status_code == 200, r.text
    assert asc_auth.decode_token(r.json()["token"])[realm.CLAIM] == "live"


def test_unknown_realm_header_is_a_400(sandbox_on):
    r = client.get("/api/asclepius/auth/me", headers={realm.HEADER: "staging"})
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_realm"


# ─── Token realm wins (§1.3, §6.2) ───────────────────────────────────────────
def test_sandbox_token_routes_to_sandbox_without_any_header(two_realms):
    r = client.get("/api/asclepius/auth/me",
                   headers={"Authorization": "Bearer " + two_realms["sb_token"]})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "sb-only@asclepius.example.com"


def test_sandbox_token_plus_live_header_is_realm_mismatch(two_realms):
    r = client.get("/api/asclepius/auth/me",
                   headers={"Authorization": "Bearer " + two_realms["sb_token"], realm.HEADER: "live"})
    assert r.status_code == 401
    assert r.json()["code"] == "realm_mismatch"


def test_live_token_plus_sandbox_header_is_realm_mismatch(two_realms):
    r = client.get("/api/asclepius/auth/me",
                   headers={"Authorization": "Bearer " + two_realms["live_token"], realm.HEADER: "sandbox"})
    assert r.status_code == 401
    assert r.json()["code"] == "realm_mismatch"


def test_live_token_on_sandbox_path_is_a_401(two_realms):
    r = client.post("/api/asclepius/sandbox/seed",
                    headers={"Authorization": "Bearer " + two_realms["live_token"]})
    assert r.status_code == 401
    assert r.json()["code"] == "realm_mismatch"


def test_token_realm_is_enforced_by_the_auth_dependency_itself(two_realms):
    """Even with the middleware bypassed, a sandbox token in a live context
    authenticates nobody (belt-and-braces in ``get_current_user_optional``)."""
    sb_payload = asc_auth.decode_token(two_realms["sb_token"])
    assert realm.current() == "live"
    assert asc_auth.get_current_user_optional("Bearer " + two_realms["sb_token"]) is None
    with realm.scoped("sandbox"):
        assert asc_auth.get_current_user_optional("Bearer " + two_realms["sb_token"])["id"] == sb_payload["sub"]
        assert asc_auth.get_current_user_optional("Bearer " + two_realms["live_token"]) is None


def test_forged_realm_claim_grants_nothing(two_realms):
    """A live token re-signed with a sandbox claim is a different token with a
    bad signature; a *correctly signed* token cannot be re-labelled. Peeking at
    the claim to pick a store is therefore safe: the peek routes, the signature
    authenticates."""
    import jwt
    payload = jwt.decode(two_realms["live_token"], options={"verify_signature": False})
    payload[realm.CLAIM] = "sandbox"
    forged = jwt.encode(payload, "not-the-secret", algorithm="HS256")
    r = client.get("/api/asclepius/auth/me", headers={"Authorization": "Bearer " + forged})
    assert r.status_code == 401


def test_legacy_token_without_claim_is_live(two_realms):
    """Tokens minted before the claim existed are live tokens."""
    import jwt
    payload = jwt.decode(two_realms["live_token"], options={"verify_signature": False})
    payload.pop(realm.CLAIM)
    legacy = jwt.encode(payload, asc_auth.get_asclepius_secret(), algorithm="HS256")
    r = client.get("/api/asclepius/auth/me", headers={"Authorization": "Bearer " + legacy})
    assert r.status_code == 200
    r = client.get("/api/asclepius/auth/me",
                   headers={"Authorization": "Bearer " + legacy, realm.HEADER: "sandbox"})
    assert r.status_code == 401


# ─── The pure resolver ───────────────────────────────────────────────────────
@pytest.mark.parametrize("path,header,claim,expect", [
    ("/api/x", None, None, ("live", None)),
    ("/api/x", "sandbox", None, ("sandbox", None)),
    ("/api/x", "live", None, ("live", None)),
    ("/api/x", None, "sandbox", ("sandbox", None)),
    ("/api/x", "sandbox", "sandbox", ("sandbox", None)),
    ("/api/x", "live", "sandbox", ("sandbox", (401, "realm_mismatch"))),
    ("/api/x", "sandbox", "live", ("live", (401, "realm_mismatch"))),
    ("/sandbox/admin", None, None, ("sandbox", None)),
    ("/sandbox/admin", None, "live", ("live", (401, "realm_mismatch"))),
    ("/api/asclepius/sandbox/seed", None, "sandbox", ("sandbox", None)),
    ("/sandboxes", None, None, ("live", None)),   # prefix must be a path segment
    ("/api/x", "prod", None, ("live", (400, "unknown_realm"))),
])
def test_resolver_rules(sandbox_on, path, header, claim, expect):
    assert realm.resolve_for_request(path=path, header=header, token_claim=claim) == expect


def test_resolver_dark(monkeypatch):
    monkeypatch.delenv(realm.ADMIN_PASSWORD_VAR, raising=False)
    assert realm.resolve_for_request(path="/sandbox/admin", header=None, token_claim=None) == ("live", (404, "sandbox_disabled"))
    assert realm.resolve_for_request(path="/api/x", header="sandbox", token_claim=None) == ("live", None)
    # A sandbox token in the dark deployment resolves to live and then fails auth.
    assert realm.resolve_for_request(path="/api/x", header=None, token_claim="sandbox") == ("live", None)


def test_hs_cookie_name_matches_provider_router():
    from routers import asclepius_provider
    assert asclepius_provider._HS_COOKIE == realm.HS_COOKIE


# ─── The SPA aliases (§1.3) ──────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/sandbox/asclepius", "/sandbox/admin", "/sandbox/provider",
                                  "/sandbox/buyer", "/sandbox/workspace", "/sandbox/community"])
def test_sandbox_shells_are_the_same_html_tagged(sandbox_on, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:200])
    assert "window.__REALM='sandbox'" in r.text
    # The tag lands in <head>, before every deferred script.
    assert r.text.index("window.__REALM") < r.text.index("</head>")


# ─── Tokens that cannot ride in a header (§1.3) ──────────────────────────────
def _scope(kind="http", *, headers=(), query=b"", path="/api/x"):
    return {"type": kind, "path": path, "query_string": query,
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


def test_claim_from_scope_reads_bearer_cookie_query_token_and_ws_realm(two_realms):
    sb, live = two_realms["sb_token"], two_realms["live_token"]
    assert realm.claim_from_scope(_scope(headers=[("authorization", "Bearer " + sb)]))["claim"] == "sandbox"
    assert realm.claim_from_scope(_scope(headers=[("cookie", f"{realm.HS_COOKIE}={live}")]))["claim"] == "live"
    # A <video> element's ?ticket= and a WebSocket's ?token= carry a JWT.
    assert realm.claim_from_scope(_scope(query=("ticket=" + sb).encode()))["claim"] == "sandbox"
    assert realm.claim_from_scope(_scope("websocket", query=("token=" + sb).encode()))["claim"] == "sandbox"
    # An opaque WS ticket carries no claim; the client names the realm beside it.
    found = realm.claim_from_scope(_scope("websocket", query=b"ticket=opaque-nonce&realm=sandbox"))
    assert found["claim"] is None and found["query_realm"] == "sandbox"
    # Bearer wins over a query token; a bad ?realm= is ignored.
    found = realm.claim_from_scope(_scope(headers=[("authorization", "Bearer " + live)],
                                          query=("ticket=" + sb + "&realm=nope").encode()))
    assert found["claim"] == "live" and found["query_realm"] is None
    assert realm.claim_from_scope(_scope(headers=[("x-asclepius-realm", "sandbox")]))["header"] == "sandbox"


def test_websocket_ticket_is_bound_to_the_realm_it_was_minted_in(sandbox_on):
    from community import router as croute
    with realm.scoped("sandbox"):
        t = croute._mint_ws_ticket("u-sandbox")
    assert croute._redeem_ws_ticket(t) is None            # live socket: refused (and consumed)
    with realm.scoped("sandbox"):
        t2 = croute._mint_ws_ticket("u-sandbox")
        assert croute._redeem_ws_ticket(t2) == "u-sandbox"
    t3 = croute._mint_ws_ticket("u-live")
    with realm.scoped("sandbox"):
        assert croute._redeem_ws_ticket(t3) is None


def test_websocket_scope_with_a_live_token_on_a_sandbox_realm_is_refused(two_realms):
    """The middleware closes the handshake (4401) instead of answering HTTP."""
    import asyncio
    sent = []

    async def app(scope, receive, send):  # never reached
        sent.append("app")

    async def send(msg):
        sent.append(msg)

    mw = realm.RealmMiddleware(app)
    scope = _scope("websocket", query=("token=" + two_realms["live_token"] + "&realm=sandbox").encode(),
                   path="/api/community/ws")
    asyncio.run(mw(scope, None, send))
    assert sent == [{"type": "websocket.close", "code": 4401}]
    # And a consistent one goes through, in the sandbox realm.
    seen = {}

    async def app2(scope, receive, send):
        seen["realm"] = realm.current()

    scope = _scope("websocket", query=("ticket=opaque&realm=sandbox").encode(), path="/api/community/ws")
    asyncio.run(realm.RealmMiddleware(app2)(scope, None, send))
    assert seen["realm"] == "sandbox"


# ─── Audit finding: one portal cookie per realm ──────────────────────────────
def test_portal_cookie_is_named_per_realm():
    assert realm.hs_cookie("live") == realm.HS_COOKIE == "hs_portal_session"
    assert realm.hs_cookie("sandbox") == realm.HS_COOKIE_SANDBOX != realm.HS_COOKIE
    with realm.scoped("sandbox"):
        assert realm.hs_cookie() == realm.HS_COOKIE_SANDBOX


def test_claim_from_scope_peeks_only_the_cookie_of_the_requested_realm(two_realms):
    from routers import asclepius_provider as P
    with realm.scoped("sandbox"):
        sb_cookie = P._hs_token("sb-user", "hs-sb")
    live_cookie = P._hs_token("live-user", "hs-live")
    both = f"{realm.HS_COOKIE}={live_cookie}; {realm.HS_COOKIE_SANDBOX}={sb_cookie}"
    # A live request reads the live cookie even with the sandbox one beside it…
    assert realm.claim_from_scope(_scope(headers=[("cookie", both), ("x-asclepius-realm", "live")]))["claim"] == "live"
    assert realm.claim_from_scope(_scope(headers=[("cookie", both)]))["claim"] == "live"
    # …and a sandbox request (header or path) reads the sandbox one.
    assert realm.claim_from_scope(_scope(headers=[("cookie", both), ("x-asclepius-realm", "sandbox")]))["claim"] == "sandbox"
    assert realm.claim_from_scope(_scope(headers=[("cookie", both)], path="/sandbox/provider"))["claim"] == "sandbox"
    # A stray sandbox cookie says nothing about a live request.
    assert realm.claim_from_scope(_scope(headers=[("cookie", f"{realm.HS_COOKIE_SANDBOX}={sb_cookie}")]))["claim"] is None


def test_a_sandbox_portal_session_does_not_lock_the_browser_out_of_the_live_portal(two_realms):
    """Both cookies are path=/. With one name, a sandbox sign-in rode on every
    live /provider call — login and logout included — as 401 realm_mismatch
    for the cookie's TTL. And the reverse: a live cookie 401'd the sandbox shells."""
    from routers import asclepius_provider as P
    with realm.scoped("sandbox"):
        sb_cookie = P._hs_token("sb-user", "hs-sb")
    live_cookie = P._hs_token("live-user", "hs-live")
    sb_jar = {realm.HS_COOKIE_SANDBOX: sb_cookie}
    r = client.post("/api/asclepius/hs/login", headers={realm.HEADER: "live"}, cookies=sb_jar,
                    json={"username": "nobody", "password": "pw-12345678"})
    assert "realm_mismatch" not in r.text, r.text
    r = client.post("/api/asclepius/hs/logout", headers={realm.HEADER: "live"}, cookies=sb_jar)
    assert "realm_mismatch" not in r.text, r.text
    for path in ("/sandbox/asclepius", "/sandbox/provider"):
        r = client.get(path, cookies={realm.HS_COOKIE: live_cookie})
        assert r.status_code == 200 and "window.__REALM='sandbox'" in r.text, (path, r.status_code)


def test_realm_query_param_stands_in_for_the_header_on_a_plain_navigation(two_realms):
    """An outbox link (``/community/join/<t>?realm=sandbox``) is a navigation
    with no header; ``?realm=`` names the realm exactly as the header would."""
    live = two_realms["live_token"]
    ok = client.get("/api/asclepius/auth/me", headers={"Authorization": "Bearer " + live})
    assert ok.status_code == 200
    r = client.get("/api/asclepius/auth/me?realm=sandbox", headers={"Authorization": "Bearer " + live})
    assert r.status_code == 401 and "realm_mismatch" in r.text
    r = client.get("/api/asclepius/auth/me?realm=live", headers={"Authorization": "Bearer " + live})
    assert r.status_code == 200
