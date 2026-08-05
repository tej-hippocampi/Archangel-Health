"""Advisor PRD Phase 2 — referrals.

The funnel an advisor can see, and — more importantly — the funnel they cannot:
advisor A must never read advisor B's referrals by any parameter, and a referrer
is not entitled to the credentialing file of the person they referred. That
second rule is enforced by WHITELIST in ``_referral_public``, so the test asserts
on the SHAPE of the payload rather than on a list of fields somebody remembered
to strip.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _admin():
    return A.make_user(asc_store.get_store(), role="admin")


def _advisor(**kw):
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    return store.appoint_advisor(u["id"], agreement_ref="AGR-1", appointed_by="admin@x")


def _physician():
    return A.make_user(asc_store.get_store(), role="evaluator", specialty="nephrology")


def _invite(advisor, email, name="Dr Referred", note=None):
    return client.post("/api/asclepius/advisor/referrals",
                       json={"email": email, "name": name, "note": note},
                       headers=A.headers_for(advisor))


# ═══ The invite ══════════════════════════════════════════════════════════════
def test_only_an_advisor_can_refer():
    """REFER is the one capability a reviewer does not hold."""
    for user in (A.make_user(asc_store.get_store(), role="evaluator"),):
        r = _invite(user, "someone@example.com")
        assert r.status_code == 403
    assert client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(_physician())).status_code == 403


def test_invite_then_signup_resolves_the_referral_to_that_user():
    store = asc_store.get_store()
    advisor = _advisor()
    email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    r = _invite(advisor, email, name="Dr Chen")
    assert r.status_code == 200, r.text
    assert r.json()["referral"]["status"] == "invited"

    # The invitee signs up through the normal provisioning path.
    new_user = store.provision_user(email=email, password="pw-12345678",
                                    role="evaluator", full_name="Dr Chen")
    claimed = store.claim_referral_for_signup(email=email, user_id=new_user["id"])
    assert claimed is not None
    assert claimed["user_id"] == new_user["id"]
    assert claimed["status"] == "signed_up"

    body = client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(advisor)).json()
    assert body["total"] == 1
    assert body["referrals"][0]["status_word"] == "Signed up"


def test_the_funnel_advances_through_verified_to_active_and_never_backwards():
    store = asc_store.get_store()
    advisor = _advisor()
    admin_h = A.headers_for(_admin())
    email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    _invite(advisor, email)
    invitee = store.provision_user(email=email, password="pw-12345678", role="evaluator")
    store.claim_referral_for_signup(email=email, user_id=invitee["id"])
    store.advance_referral_for_user(invitee["id"], "verified")

    r = client.post(f"/api/asclepius/verify/queue/{invitee['id']}/approve",
                    json={"tier": "labeler"}, headers=admin_h)
    assert r.status_code == 200, r.text

    body = client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(advisor)).json()
    assert body["referrals"][0]["status"] == "approved"
    assert body["referrals"][0]["status_word"] == "Active"
    assert body["active"] == 1

    # A later event must never walk an approved referral back down the ladder —
    # the funnel is a record of what happened, not a live status light.
    store.advance_referral_for_user(invitee["id"], "signed_up")
    body = client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(advisor)).json()
    assert body["referrals"][0]["status"] == "approved"


def test_a_rejected_invitee_shows_as_declined_not_as_silence():
    store = asc_store.get_store()
    advisor = _advisor()
    email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    _invite(advisor, email)
    invitee = store.provision_user(email=email, password="pw-12345678", role="evaluator")
    store.claim_referral_for_signup(email=email, user_id=invitee["id"])
    r = client.post(f"/api/asclepius/verify/queue/{invitee['id']}/reject",
                    json={"note": "Could not verify board certification."},
                    headers=A.headers_for(_admin()))
    assert r.status_code == 200
    body = client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(advisor)).json()
    assert body["referrals"][0]["status"] == "declined"


def test_referring_an_existing_member_reports_it_instead_of_failing():
    """§3.2: 'already a member' is a fact, not an error — and it must never
    create a duplicate account."""
    store = asc_store.get_store()
    advisor = _advisor()
    member = _physician()
    before = len(store.list_users())
    r = _invite(advisor, member["email"])
    assert r.status_code == 200, r.text
    assert r.json()["already"] == "member"
    assert "already a member" in r.json()["message"].lower()
    assert len(store.list_users()) == before


def test_inviting_the_same_address_twice_does_not_duplicate_the_row():
    advisor = _advisor()
    email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    assert _invite(advisor, email).status_code == 200
    second = _invite(advisor, email)
    assert second.status_code == 200
    assert second.json()["already"] == "invited"
    body = client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(advisor)).json()
    assert body["total"] == 1


# ═══ Isolation ═══════════════════════════════════════════════════════════════
def test_advisor_a_cannot_read_advisor_bs_referrals_by_any_parameter():
    """Scoped from the SESSION, never from a query parameter. The endpoint takes
    no id at all — which is the design-level version of the IDOR rule, not a
    validation bolted on after."""
    a = _advisor()
    b = _advisor()
    _invite(b, f"b-invitee-{uuid.uuid4().hex[:8]}@example.com", name="B's referral")

    assert client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(a)).json()["total"] == 0
    assert client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(b)).json()["total"] == 1

    # Every shape of tampering an attacker would reach for.
    for query in (f"?user_id={b['id']}", f"?referrer_id={b['id']}",
                  f"?advisor_id={b['id']}", f"?id={b['id']}",
                  f"?referral_code={b.get('referral_code')}"):
        body = client.get("/api/asclepius/advisor/referrals" + query,
                          headers=A.headers_for(a)).json()
        assert body["total"] == 0, f"leaked B's funnel via {query}"


def test_the_referrers_payload_never_carries_credentialing_internals():
    """A referrer sees name, date and plain-words status. Not the invitee's NPI,
    tier score, or verification notes."""
    store = asc_store.get_store()
    advisor = _advisor()
    # Alpha-only local part: the harness's random hex can contain any short
    # digit run, and a two-character sentinel like "88" collides with it about
    # as often as it catches anything. A test that fails spuriously gets muted,
    # and a muted test guards nothing — so every sentinel here is distinctive.
    email = f"invitee-{A.uniq(10)}@example.com"
    _invite(advisor, email, name="Dr Chen")
    invitee = store.provision_user(
        email=email, password="pw-12345678", role="evaluator",
        npi="1234567893", full_name="Dr Chen")
    store.claim_referral_for_signup(email=email, user_id=invitee["id"])
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET tier_score = 88, verification_notes = "
            "'SENTINEL-CV-NOTE-strong', npi_verified = 1 WHERE id = ?",
            (invitee["id"],))

    raw = json.dumps(client.get("/api/asclepius/advisor/referrals",
                                headers=A.headers_for(advisor)).json()).lower()
    for leaked in ("npi", "tier_score", "verification", "sentinel-cv-note",
                   "1234567893", "board_cert", "cv_asset", "id_hashed"):
        assert leaked not in raw, f"the referrer's payload leaked {leaked!r}"


def test_the_invite_email_names_the_referrer():
    """That one sentence IS the referral mechanism — a named referral converts
    several times better than a cold invite (§3.2)."""
    from onboarding_emails import build_asclepius_invite_email

    html = build_asclepius_invite_email(
        invitee_first_name="Sarah", director_full_name="Toby Barrack",
        role_label="Physician contributor", org_name="Archangel Health",
        specialty="nephrology", onboarding_url="https://example.com/onboard/abc",
        invitee_email="sarah@example.com", referrer_name="Toby Barrack")
    assert "Toby Barrack" in html
    assert "suggested you" in html

    # And it stays ONE invite email: with no referrer it renders the original
    # copy, so the org invite path is byte-identical to before.
    plain = build_asclepius_invite_email(
        invitee_first_name="Sarah", director_full_name="Toby Barrack",
        role_label="Physician contributor", org_name="Archangel Health",
        specialty="nephrology", onboarding_url="https://example.com/onboard/abc",
        invitee_email="sarah@example.com")
    assert "suggested you" not in plain


def test_a_nameless_advisor_never_leaks_their_email_to_the_invitee():
    """An advisor appointed by email with no name on file must not have their
    address disclosed to every physician they invite — not in the body, and
    especially not in a subject line. No name means no named referral."""
    import routers.asclepius_advisor as advisor_router

    sent = {}

    class _FakeTS:
        def create_health_system_invite(self, **kw):
            return {"onboarding_url": "https://landing.example/onboard/tok"}

    async def _fake_send(to, subject, html, **kw):
        sent.update({"to": to, "subject": subject, "html": html})
        return True

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("email_utils.is_email_transport_configured", lambda: True)
        monkey.setattr("email_utils.send_html_email", _fake_send)
        nameless = _advisor()
        assert not (nameless.get("full_name") or "").strip()

        class _Req:
            class app:
                class state:
                    team_store = _FakeTS()

        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            advisor_router._send_referral_invite(
                _Req(), nameless,
                advisor_router.ReferralBody(email="invitee@example.com", name="Dr Chen"),
                "invitee@example.com", nameless["referral_code"]))
    finally:
        monkey.undo()

    assert sent, "no email was sent"
    assert nameless["email"] not in sent["subject"], (
        "the advisor's email address leaked into the subject line")
    assert nameless["email"] not in sent["html"], (
        "the advisor's email address leaked into the invite body")
    assert "@" not in sent["subject"]


def test_every_advisor_gets_a_unique_referral_code():
    codes = {_advisor()["referral_code"] for _ in range(5)}
    assert len(codes) == 5
    assert all(c and len(c) == 8 for c in codes)
    # No ambiguous glyphs — these get read to people over the phone.
    assert not any(set(c) & set("O0I1l") for c in codes)


def test_two_advisors_referring_the_same_physician_both_resolve():
    """Audit M5: only one referral was claimed; the other sat at 'invited'
    forever and rendered in that advisor's funnel as still pending long after
    the person joined. A well-connected candidate is exactly who gets referred
    twice, so this is the normal case, not the edge one."""
    store = asc_store.get_store()
    a, b = _advisor(), _advisor()
    email = f"popular-{A.uniq(10)}@example.com"
    assert _invite(a, email, name="Dr Popular").status_code == 200
    assert _invite(b, email, name="Dr Popular").status_code == 200

    invitee = store.provision_user(email=email, password="pw-12345678",
                                   role="evaluator", full_name="Dr Popular")
    store.claim_referral_for_signup(email=email, user_id=invitee["id"])

    for advisor in (a, b):
        funnel = client.get("/api/asclepius/advisor/referrals",
                            headers=A.headers_for(advisor)).json()
        assert funnel["total"] == 1
        assert funnel["referrals"][0]["status"] == "signed_up", (
            "one referrer's funnel was stranded at 'invited' forever")

    # And the funnel keeps following for BOTH of them.
    r = client.post(f"/api/asclepius/verify/queue/{invitee['id']}/approve",
                    json={"tier": "labeler"}, headers=A.headers_for(_admin()))
    assert r.status_code == 200
    for advisor in (a, b):
        funnel = client.get("/api/asclepius/advisor/referrals",
                            headers=A.headers_for(advisor)).json()
        assert funnel["active"] == 1


def test_the_referral_endpoint_is_not_an_account_existence_oracle():
    """Audit M4: ``get_user_by_email`` spans every role, so 'already a member'
    was a clean boolean on an admin, buyer or hospital-contact address — a probe
    an advisor could run one address at a time against a company inbox."""
    store = asc_store.get_store()
    advisor = _advisor()
    admin_account = A.make_user(store, role="admin")

    r = _invite(advisor, admin_account["email"], name="Probe")
    assert r.status_code == 200
    assert r.json().get("already") != "member", (
        "the endpoint confirmed the existence of a non-physician account")

    # A real physician still reports honestly.
    member = A.make_user(store, role="evaluator", specialty="nephrology")
    r = _invite(advisor, member["email"], name="Real")
    assert r.json().get("already") == "member"


def test_the_shareable_invite_link_points_at_a_route_that_exists():
    """A link an advisor pastes into a text message must not 404. It points at
    the existing physician signup page, not an invented referral route."""
    advisor = _advisor()
    body = client.get("/api/asclepius/advisor/referrals",
                      headers=A.headers_for(advisor)).json()
    url = body["invite_url"]
    assert url and url.endswith(f"/physicians?ref={advisor['referral_code']}")
    # The route is one the landing app actually serves.
    shell = (Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
             / "components" / "arch" / "ArchShell.tsx")
    if shell.exists():
        assert '"/physicians"' in shell.read_text(encoding="utf-8")


def test_a_referral_code_resolves_back_to_its_advisor():
    store = asc_store.get_store()
    advisor = _advisor()
    found = store.get_user_by_referral_code(advisor["referral_code"])
    assert found["id"] == advisor["id"]
    assert store.get_user_by_referral_code("NOPE9999") is None
    assert store.get_user_by_referral_code("") is None
