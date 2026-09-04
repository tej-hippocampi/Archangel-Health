"""The claim link that replaced the temporary passphrase.

Two doors used to mail a credential: a colleague adding a teammate, and a
three-field signup clearing its code. Both now mail a one-time link, and the
person sets their own password on arrival. Three separate things are asserted
here because three separate things went wrong in the version this replaces.

  * A CREDENTIAL IN AN INBOX. A passphrase in an email is a passphrase in every
    forward of that email, and it guarded the account until somebody got round
    to replacing it.
  * A MACHINE NAME ON SCREEN. The account had no name, so the portal header
    rendered the derived username and greeted a hospital executive as
    "Berkeley 2". Claiming asks for a name, which is why the header can show one.
  * AN ORACLE. An unknown token must answer exactly as a live one does at the
    status-code level. ``GET /api/asclepius/hs-referral/{token}`` states the
    reasoning and this route inherits it: a 404 turns a token guess into a
    question about whether a health system is talking to us.

Asserted through the HTTP surface, following this suite's rule that a gate is
only real where a real caller meets it.
"""
from __future__ import annotations

import base64
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

API = "/api/asclepius"
_KEY = base64.urlsafe_b64encode(b"hs-invite-claim-test-key-32byte!").decode()
PASSWORD = "harbor-thistle-meadow-41"
REPO = Path(__file__).resolve().parents[2]
PROVIDER_JS = REPO / "frontend" / "provider" / "provider.js"
PROVIDER_HTML = REPO / "frontend" / "provider" / "index.html"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    yield


@pytest.fixture()
def mail(monkeypatch):
    sent = []

    async def _fake_send(to, subject, body, **kw):
        sent.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr("routers.asclepius_provider.is_email_transport_configured",
                        lambda: True, raising=False)
    monkeypatch.setattr("routers.asclepius_provider.send_html_email",
                        _fake_send, raising=False)
    monkeypatch.setattr("email_utils.send_html_email", _fake_send, raising=False)
    monkeypatch.setattr("email_utils.is_email_transport_configured",
                        lambda: True, raising=False)
    return sent


def _store():
    from asclepius.store import get_store
    return get_store()


def _client() -> TestClient:
    return TestClient(A.app, base_url="https://testserver")


def _signup(client, *, email=None, org=None, password=""):
    """One organization through signup and verify. The staged code is hashed, so
    the test re-stages the same signup with a code it chose."""
    store = _store()
    addr = email or f"it{uuid.uuid4().hex[:6]}@example.org"
    name = org or f"Test Health {uuid.uuid4().hex[:6]}"
    body = {"full_name": "Dana Reyes", "email": addr, "organization": name}
    if password:
        body["password"] = password
    assert client.post(f"{API}/hs/signup", json=body).status_code == 200
    store.create_hs_signup(email=addr, full_name="Dana Reyes", organization=name,
                           password=password or "x" * 40, code="424242",
                           needs_temp_password=not password)
    r = client.post(f"{API}/hs/signup/verify", json={"email": addr, "code": "424242"})
    assert r.status_code == 200, r.text
    hs = [h for h in store.list_health_systems() if h["name"] == name][0]
    return {"email": addr, "organization": name, "hs_id": hs["hs_id"],
            "username": r.json()["username"]}


def _payload(response) -> dict:
    """The response body without the portal's length pad.

    Every portal response is padded to a fixed size (``_pad_json_response``) so
    that a response's LENGTH cannot say whether an organization exists. The pad
    is chrome, not data, and no assertion here is about it.
    """
    return {k: v for k, v in response.json().items() if k != "_"}


def _token_in(body: str) -> str:
    found = re.search(r"[?&]invite=([A-Za-z0-9_-]+)", body)
    assert found, "no claim link in that letter"
    return found.group(1)


def _access_token(mail, addr: str) -> str:
    """The claim token out of the ACCESS letter, by subject.

    Filtering on the recipient alone would find the confirmation-code email
    first: both go to the same address, and only one of them carries a link.
    """
    letter = next(m for m in mail
                  if m["to"] == addr and "portal access" in m["subject"].lower())
    return _token_in(letter["body"])


def _invite_a_colleague(client, mail, addr="colleague@example.org") -> str:
    """A signed-in partner adds one teammate; returns their claim token."""
    r = client.post(f"{API}/hs/members", json={"emails": [addr]})
    assert r.status_code == 200, r.text
    letter = next(m for m in mail if m["to"] == addr)
    return _token_in(letter["body"])


# ─── The letter carries a link and nothing else ─────────────────────────────
def test_an_invited_member_is_sent_a_link_and_never_a_credential(mail):
    """The whole point. Nothing that guards this account travels by email, so
    there is nothing in the letter to leak, reuse or forget to rotate."""
    client = _client()
    org = _signup(client)
    client.post(f"{API}/hs/invite/{_access_token(mail, org['email'])}/claim",
                json={"full_name": "Dana Reyes", "password": PASSWORD})

    client.post(f"{API}/hs/members", json={"emails": ["late@example.org"]})
    body = next(m for m in mail if m["to"] == "late@example.org")["body"]
    assert "Set up your account" in body
    assert "Temporary password" not in body
    assert "Sign in with" not in body
    # No passphrase, in the shape the credentials card used to print them.
    assert not re.search(r">([a-z]+-[a-z]+-[a-z]+-[0-9a-f]{6})<", body)


def test_the_letter_says_why_they_are_being_added(mail):
    """The recipient usually did not fill the form in. Somebody on their team
    did, and a letter that does not say so leaves a hospital executive guessing
    why a company they have not heard of wants them to set a password."""
    client = _client()
    _signup(client)
    _invite_a_colleague(client, mail)
    body = next(m for m in mail if m["to"] == "colleague@example.org")["body"]
    assert "added you" in body
    assert "review" in body.lower() or "read through" in body.lower()


# ─── The lookup is not an oracle ────────────────────────────────────────────
def test_an_unknown_token_is_found_false_and_not_a_404(mail):
    """Same reasoning as ``GET /api/asclepius/hs-referral/{token}``: a 404 makes
    the status code an answer to "is this token live", and a live token names a
    health system that is talking to us."""
    client = _client()
    r = client.get(f"{API}/hs/invite/definitely-not-a-real-token")
    assert r.status_code == 200, r.text
    assert _payload(r) == {"found": False}


def test_an_expired_token_is_found_false_too(mail):
    """The page renders its used-or-expired state either way, which is what it
    should do for a stale link regardless of why the link is stale."""
    client = _client()
    _signup(client)
    token = _invite_a_colleague(client, mail)
    store = _store()
    row = store.get_hs_portal_user_by_invite_hash(
        __import__("hashlib").sha256(token.encode()).hexdigest())
    with store._conn() as conn:
        conn.execute("UPDATE hs_portal_users SET invite_expires_at = ? "
                     "WHERE username = ?", ("2020-01-01T00:00:00", row["username"]))
    assert _payload(client.get(f"{API}/hs/invite/{token}")) == {"found": False}
    # And it cannot be spent either. One resolver answers both routes, so
    # "expired" cannot mean two different things to the page and the write.
    r = client.post(f"{API}/hs/invite/{token}/claim",
                    json={"full_name": "Kim Patel", "password": PASSWORD})
    assert r.status_code == 400


def test_the_lookup_returns_only_what_the_letter_already_said(mail):
    """Built by whitelist. Their own address, their organization and the name of
    the colleague who added them were all in the email the link came in; a
    username, an approval status or a password state were not."""
    client = _client()
    org = _signup(client)
    # Claim the founder account first so it HAS a name to be the inviter by.
    client.post(f"{API}/hs/invite/{_access_token(mail, org['email'])}/claim",
                json={"full_name": "Dana Reyes", "password": PASSWORD})
    token = _invite_a_colleague(client, mail)

    payload = _payload(client.get(f"{API}/hs/invite/{token}"))
    assert payload["found"] is True
    assert payload["email"] == "colleague@example.org"
    assert payload["organization"] == org["organization"]
    # The inviter by NAME, never by the derived username this change exists to
    # keep off every screen.
    assert payload["invited_by"] == "Dana Reyes"
    assert org["username"] not in str(payload)
    assert set(payload) == {"found", "email", "organization", "invited_by"}


# ─── Claiming ───────────────────────────────────────────────────────────────
def test_claiming_sets_the_name_clears_must_reset_and_signs_them_in(mail):
    """Straight into the portal. A second sign-in would ask for a username they
    have never seen, and a forced-reset screen would ask them to replace a
    password they chose ten seconds ago."""
    client = _client()
    _signup(client)
    token = _invite_a_colleague(client, mail)

    fresh = _client()
    r = fresh.post(f"{API}/hs/invite/{token}/claim",
                   json={"full_name": "Kim Patel", "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["must_reset"] is False
    me = fresh.get(f"{API}/hs/me").json()
    assert me["full_name"] == "Kim Patel"
    assert me["must_reset"] is False
    assert me["email"] == "colleague@example.org"


def test_a_token_can_only_be_spent_once(mail):
    """Single use is one statement in the store: the password write clears the
    hash. A second visit finds nothing, which is the right answer, because the
    account now has an owner."""
    client = _client()
    _signup(client)
    token = _invite_a_colleague(client, mail)

    assert _client().post(f"{API}/hs/invite/{token}/claim",
                          json={"full_name": "Kim Patel",
                                "password": PASSWORD}).status_code == 200
    second = _client().post(f"{API}/hs/invite/{token}/claim",
                            json={"full_name": "Someone Else",
                                  "password": "different-passphrase-9911"})
    assert second.status_code == 400
    assert _payload(_client().get(f"{API}/hs/invite/{token}")) == {"found": False}
    # The first claimant's password still works, which is what "already used"
    # has to mean for the person who used it. A second claim that had gone
    # through would have overwritten it with a stranger's.
    member = next(u for u in _store().list_hs_portal_users()
                  if (u.get("email") or "") == "colleague@example.org")
    r = _client().post(f"{API}/hs/login",
                       json={"username": member["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text


def test_the_body_cannot_choose_the_accounts_email(mail):
    """The account's address is whatever the invite was minted against. A body
    that could name it would make a forwarded invite a way to attach an account
    on somebody else's organization to an address of your choosing, which is the
    hole ``/hs/members`` closes by taking the organization from the session."""
    client = _client()
    _signup(client)
    token = _invite_a_colleague(client, mail)

    fresh = _client()
    r = fresh.post(f"{API}/hs/invite/{token}/claim",
                   json={"full_name": "Kim Patel", "password": PASSWORD,
                         "email": "attacker@example.org"})
    assert r.status_code == 200, r.text
    assert fresh.get(f"{API}/hs/me").json()["email"] == "colleague@example.org"


def test_a_claim_needs_a_name_and_a_real_password(mail):
    """Both are the point of the screen. A blank name puts us back to rendering
    a derived username in the header, and the password is held to the same
    policy the signup door holds."""
    client = _client()
    _signup(client)
    token = _invite_a_colleague(client, mail)

    assert _client().post(f"{API}/hs/invite/{token}/claim",
                          json={"full_name": "  ", "password": PASSWORD}
                          ).status_code == 400
    assert _client().post(f"{API}/hs/invite/{token}/claim",
                          json={"full_name": "Kim Patel", "password": "short"}
                          ).status_code == 400
    # Neither attempt spent the token.
    assert _payload(_client().get(f"{API}/hs/invite/{token}"))["found"] is True


def test_setting_a_password_any_other_way_kills_the_outstanding_invite(mail):
    """A self-signup is signed in AND mailed a link. If it sets a password on the
    forced-reset screen without ever following the link, the token in that inbox
    would still be a live password-setting door on an account that now has an
    owner."""
    client = _client()
    org = _signup(client)
    token = _access_token(mail, org["email"])

    # The forced reset, on the session the signup already holds.
    r = client.post(f"{API}/hs/password",
                    json={"current_password": "", "new_password": PASSWORD})
    assert r.status_code == 200, r.text

    assert _payload(_client().get(f"{API}/hs/invite/{token}")) == {"found": False}
    assert _client().post(f"{API}/hs/invite/{token}/claim",
                          json={"full_name": "Someone Else",
                                "password": "another-passphrase-4417"}
                          ).status_code == 400
    # And the password they actually chose still works.
    assert _client().post(f"{API}/hs/login",
                          json={"username": org["username"], "password": PASSWORD}
                          ).status_code == 200


# ─── The screen ─────────────────────────────────────────────────────────────
# Source-level, following ``test_hs_signin_split``: there is no browser here,
# and what actually breaks is the screen quietly losing a field the server
# requires, or the header going back to the username.
def test_the_portal_has_a_claim_screen_behind_the_invite_parameter():
    js = PROVIDER_JS.read_text(encoding="utf-8")
    html = PROVIDER_HTML.read_text(encoding="utf-8")
    assert 'id="tplClaim"' in html
    for field in ("prvClaimEmail", "prvClaimName", "prvClaimPw", "prvClaimConfirm"):
        assert f'id="{field}"' in html, f"the claim screen has no {field}"
    assert 'id="prvClaimEmail"' in html and "readonly" in html
    assert '"invite"' in js and "/hs/invite/" in js
    # The two screens every existing account still needs, untouched.
    assert "function renderLogin()" in js and "function renderReset()" in js


def test_the_header_never_renders_the_derived_username():
    """The bug this whole change is named after: "Berkeley Health System /
    Berkeley 2" at somebody who chose neither string."""
    js = PROVIDER_JS.read_text(encoding="utf-8")
    block = js[js.index("function renderHeader()"):]
    block = block[:block.index("\n  }")]
    assert "currentUser.full_name" in block
    assert "currentUser.email" in block
    assert "currentUser.username" not in block
