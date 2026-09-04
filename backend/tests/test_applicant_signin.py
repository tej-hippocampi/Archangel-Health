"""Getting an applicant into the practice case, and no further.

Onboarding v2 removed the password step, which left an applicant with no way
into the product at all: approval was the moment a credential came into
existence. That was right while there was nothing behind the door. The practice
case changed it, because now there IS something an applicant is supposed to do
before we decide about them, and it cannot live behind a door they cannot open.

The properties worth pinning are not "the endpoint returns 200":

  * an applicant lands in the portal at submit, without a password existing;
  * the way BACK in is single-use, short-lived, and offered only to accounts
    that have no password, so it can never be a downgrade attack on a real one;
  * the door does not answer the question "is this named physician still under
    review", which is a fact about their professional standing;
  * what the session reaches is the practice case and a status page, and
    nothing that belongs to a verified colleague.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import passwords as asc_passwords
from asclepius import store as asc_store


def _strip_js_comments(source: str) -> str:
    """Same helper test_admin_earnings_dom.py uses, for the same reason: this
    codebase explains its rules in prose next to the code that follows them."""
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


_REQUEST = "/api/asclepius/auth/signin-link"
_EXCHANGE = "/api/asclepius/auth/signin-link/exchange"
_ME = "/api/asclepius/auth/me"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _applicant(store, **kw):
    """An account in the state the wizard leaves behind: pending, no password."""
    kw.setdefault("role", "evaluator")
    u = make_user(store, **kw)
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, verification_status = 'pending', "
            "tier = NULL WHERE id = ?",
            (asc_store.NO_PASSWORD_HASH, u["id"]),
        )
    return store.get_user_by_id(u["id"])


def _mint(store, user, *, minutes=15):
    raw, hashed = asc_passwords.new_signin_token()
    expires = (datetime.utcnow() + timedelta(minutes=minutes)) \
        .replace(microsecond=0).isoformat()
    store.create_signin_link(user_id=user["id"], token_hash=hashed, expires_at=expires)
    return raw


# ─── The door ────────────────────────────────────────────────────────────────
def test_the_link_signs_an_applicant_in_once(client):
    store = fresh_store()
    user = _applicant(store)
    raw = _mint(store, user)

    r = client.post(_EXCHANGE, json={"token": raw})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert client.get(_ME, headers={"Authorization": f"Bearer {token}"}).status_code == 200

    # Once. A forwarded email is not a standing key.
    again = client.post(_EXCHANGE, json={"token": raw})
    assert again.status_code == 400


def test_an_expired_link_is_refused(client):
    store = fresh_store()
    user = _applicant(store)
    raw = _mint(store, user, minutes=-1)
    assert client.post(_EXCHANGE, json={"token": raw}).status_code == 400


def test_requesting_a_new_link_kills_the_previous_one(client):
    """Two working links means a forwarded email keeps opening the account
    after the person asked for a fresh one."""
    store = fresh_store()
    user = _applicant(store)
    first = _mint(store, user)
    second = _mint(store, user)

    assert client.post(_EXCHANGE, json={"token": first}).status_code == 400
    assert client.post(_EXCHANGE, json={"token": second}).status_code == 200


def test_a_garbage_token_reads_the_same_as_a_dead_one(client):
    """Expired, already used and never existed collapse to one sentence, so
    the holder of a bad link learns nothing about which it was."""
    store = fresh_store()
    user = _applicant(store)
    used = _mint(store, user)
    client.post(_EXCHANGE, json={"token": used})

    dead = client.post(_EXCHANGE, json={"token": used})
    never = client.post(_EXCHANGE, json={"token": "not-a-real-token-at-all"})
    assert dead.status_code == never.status_code == 400
    assert dead.json()["detail"] == never.json()["detail"]


# ─── What the door does not tell you ─────────────────────────────────────────
def test_the_request_endpoint_is_not_an_enumeration_oracle(client):
    """The interesting question here is not "does this address exist" but "is
    this named physician still under review", which is a claim about their
    professional standing."""
    store = fresh_store()
    applicant = _applicant(store)
    approved = make_user(store, role="evaluator")

    answers = [
        client.post(_REQUEST, json={"email": applicant["email"]}),
        client.post(_REQUEST, json={"email": approved["email"]}),
        client.post(_REQUEST, json={"email": "nobody-at-all@example.org"}),
    ]
    assert {a.status_code for a in answers} == {200}
    assert len({a.text for a in answers}) == 1, "the bodies differ between branches"


def test_an_account_with_a_password_is_not_mailed_a_second_door(client):
    """Offering a weaker way in beside a real credential is a downgrade attack
    on the credential."""
    store = fresh_store()
    approved = make_user(store, role="evaluator")   # has a real password hash
    assert client.post(_REQUEST, json={"email": approved["email"]}).status_code == 200

    with store._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM signin_links WHERE user_id = ?", (approved["id"],)
        ).fetchone()["c"]
    assert n == 0, "a link was minted for an account that already has a password"


def test_a_link_is_minted_for_an_applicant(client):
    """The other half of the test above: the door has to actually work for the
    person it exists for."""
    store = fresh_store()
    applicant = _applicant(store)
    assert client.post(_REQUEST, json={"email": applicant["email"]}).status_code == 200

    with store._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM signin_links WHERE user_id = ?", (applicant["id"],)
        ).fetchone()["c"]
    assert n == 1


# ─── What the session reaches ────────────────────────────────────────────────
def test_the_session_reaches_the_practice_case_and_not_a_colleague(client):
    """The point of letting an applicant in is the practice case. The point of
    scoping them is that everything else stays shut until somebody decides."""
    store = fresh_store()
    user = _applicant(store)
    raw = _mint(store, user)
    token = client.post(_EXCHANGE, json={"token": raw}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/asclepius/tutorial/task", headers=auth).status_code == 200

    # Real case work waits for the decision.
    assert client.get("/api/asclepius/tasks/next", headers=auth).status_code == 403

    # The earnings PAGE does not. Somebody deciding whether to do this work
    # should be able to see how they would be paid for it, and the page reads
    # zero, which is honest. What waits is every endpoint that MOVES money:
    # opening a billable session accrues paid minutes, and the surface used to
    # carry both permissions at once.
    assert client.get("/api/asclepius/earnings", headers=auth).status_code == 200
    opened = client.post("/api/asclepius/sessions", json={"kind": "labeling"},
                         headers=auth)
    assert opened.status_code == 403, opened.text


def test_the_raw_token_is_never_stored(client):
    """A read of this table must not be a set of working keys."""
    store = fresh_store()
    user = _applicant(store)
    raw = _mint(store, user)
    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM signin_links").fetchall()]
    assert rows and all(raw not in str(v) for r in rows for v in r.values())


# ─── The door in the portal ──────────────────────────────────────────────────
#
# The endpoints above were reachable by curl and by nothing else. An applicant
# who closed the tab still had no route back to their own practice case, which
# is the whole failure this feature exists to fix.
import pathlib  # noqa: E402

_PORTAL_JS = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
              / "asclepius.js").read_text(encoding="utf-8")


def test_a_link_already_in_a_mailbox_can_still_be_redeemed():
    """The REQUEST control is gone; the REDEMPTION path must not be.

    The wizard now takes a password on screen 1, so a new physician has an
    ordinary credential and the magic link is no longer offered on the sign-in
    screen. Deleting the redemption path along with the button would have
    broken every link already sitting in a mailbox, mid-deploy, for the fifteen
    minutes each one has left.
    """
    assert "/auth/signin-link/exchange" in _PORTAL_JS
    assert "'signin'" in _PORTAL_JS


def test_the_sign_in_screen_no_longer_offers_a_magic_link():
    """One door, and it is the ordinary one.

    What replaced the button is a sentence pointing a LEGACY passwordless
    applicant at "Forgot your password?", which is the right door for them even
    though it reads like the wrong one: forgot_password mints a reset for any
    active account without consulting password_is_unset, and set_user_password
    clears must_change_password in the same statement, so a reset SETS a first
    password rather than replacing one.
    """
    assert "Email me a sign in link" not in _PORTAL_JS
    assert "Applied before and never set a password" in _PORTAL_JS
    assert "Forgot your password?" in _PORTAL_JS


def test_the_sign_in_screen_still_reads_no_account_state():
    """The SECURITY property the old button had, kept for what replaced it.

    A control shown only to passwordless accounts would answer, by its
    presence, the question the endpoints refuse to: it would turn the sign-in
    screen into an account-enumeration oracle and hand an attacker a list of
    physicians whose applications are still undecided. The legacy hint is
    therefore unconditional, exactly as the button was.
    """
    # Comments stripped first. This block talks ABOUT account state in prose
    # explaining why it does not read any, and a grep that cannot tell prose
    # from code is a test that gets deleted the first time somebody documents
    # the rule it is enforcing.
    code = _strip_js_comments(_PORTAL_JS)
    start = code.index("Applied before and never set a password")
    block = code[start - 1800:start + 600]
    for oracle in ("password_is_unset", "state.user", "/auth/me", "accountExists"):
        assert oracle not in block, f"the sign-in screen branches on {oracle}"
