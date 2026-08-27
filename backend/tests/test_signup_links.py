"""The three shareable links.

One product, three doors, and the difference between them is what the person
holding the link is being asked to do:

  /join                     a physician signs up to do the work
  /join?flavor=advisor      a supporter looks around and refers
  /join?flavor=referrer     someone holds a referral link and nothing else

The last one is the one with teeth. A referral link handed to somebody's
mother, who knows a lot of doctors, must not open the case queue or the
community, and approving that account must not quietly turn her into a
reviewer.
"""

from __future__ import annotations

import uuid

import pytest

from asclepius import capabilities as caps
from tests._asclepius import fresh_store


def _user(store, *, account_kind=None, status="approved", tier="labeler"):
    user = store.provision_user(
        email=f"p_{uuid.uuid4().hex[:8]}@example.com", password="pw-12345678",
        role="evaluator", full_name="Sam Okafor", account_kind=account_kind,
    )
    if status:
        store.set_verification_status(user["id"], status)
    if tier:
        with store._conn() as conn:
            conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user["id"]))
    return store.get_user_by_id(user["id"])


# ─── The physician door is unchanged ─────────────────────────────────────────
def test_a_physician_account_reaches_the_whole_product():
    store = fresh_store()
    doctor = _user(store)
    surfaces = caps.surfaces(doctor)
    assert caps.REAL_WORK in surfaces
    assert caps.COMMUNITY_READ in surfaces
    assert caps.REFERRAL in surfaces
    assert caps.account_kind(doctor) is None


# ─── The advisor door ────────────────────────────────────────────────────────
def test_an_advisor_can_look_around_and_refer():
    """They are here to introduce people, which is easier if they have seen
    the thing they are introducing."""
    store = fresh_store()
    advisor = _user(store, account_kind=caps.ADVISOR)
    surfaces = caps.surfaces(advisor)
    assert caps.REFERRAL in surfaces
    assert caps.COMMUNITY_READ in surfaces
    assert caps.BROWSE in surfaces


# ─── The referral-only door ──────────────────────────────────────────────────
def test_a_referral_only_account_reaches_only_its_referral_page():
    store = fresh_store()
    referrer = _user(store, account_kind=caps.REFERRER)
    surfaces = caps.surfaces(referrer)
    assert surfaces == {caps.BROWSE, caps.REFERRAL}
    assert caps.REAL_WORK not in surfaces
    assert caps.COMMUNITY_READ not in surfaces
    assert caps.COMMUNITY_WRITE not in surfaces
    assert caps.EARNINGS not in surfaces


def test_approving_a_referral_only_account_does_not_promote_it():
    """The cap holds however their verification lands. Otherwise the first
    admin who clicks approve turns the person who introduced us to a hospital
    into someone who grades cases."""
    store = fresh_store()
    referrer = _user(store, account_kind=caps.REFERRER, status="approved", tier="reviewer")
    assert caps.surfaces(referrer) == {caps.BROWSE, caps.REFERRAL}


def test_a_referral_only_account_can_still_refer():
    """The one thing the link is for."""
    from asclepius import referrals

    store = fresh_store()
    referrer = _user(store, account_kind=caps.REFERRER)
    assert referrals.can_refer(referrer) is True


def test_a_pending_referral_only_account_can_refer_from_the_first_minute():
    store = fresh_store()
    referrer = _user(store, account_kind=caps.REFERRER, status="pending", tier=None)
    from asclepius import referrals

    assert referrals.can_refer(referrer) is True
    assert caps.REFERRAL in caps.surfaces(referrer)


# ─── Provisioning writes the kind down ───────────────────────────────────────
@pytest.mark.parametrize("flavor,expected", [
    ("advisor", "advisor"),
    ("referrer", "referrer"),
    ("general", None),      # an invited non-clinical signer is still a person, not a role
    ("", None),
])
def test_the_link_flavor_decides_the_account_kind(flavor, expected):
    import routers.onboarding as onboarding

    assert onboarding.ACCOUNT_KIND_BY_FLAVOR.get(flavor) == expected


def test_a_re_onboard_that_omits_the_kind_does_not_promote_the_account():
    """provision_user is an idempotent upsert, and a second pass without the
    flavor must not silently turn a referral-only account into a physician."""
    store = fresh_store()
    email = f"p_{uuid.uuid4().hex[:8]}@example.com"
    store.provision_user(email=email, password="pw-12345678", role="evaluator",
                         account_kind="referrer")
    store.provision_user(email=email, password="pw-12345678", role="evaluator")
    assert store.get_user_by_email(email)["account_kind"] == "referrer"


def test_the_session_says_which_door_they_came_through():
    """The portal needs it to stop showing a referral-only account a rail full
    of doors it will never open."""
    from asclepius import auth as asc_auth

    store = fresh_store()
    referrer = _user(store, account_kind=caps.REFERRER)
    assert asc_auth.public_user(referrer)["account_kind"] == "referrer"
    assert asc_auth.public_user(_user(store))["account_kind"] is None


# ═══ The short signup ═════════════════════════════════════════════════════════
# The links shipped, and then walked the person holding one straight into the
# nine-screen physician wizard: institution, NPI or registration number,
# residency, and the seven clinical attestations, which include independent
# clinical judgment and no active board disciplinary action. A referral partner
# signing those is not completing a formality, it is signing something untrue.
#
# So advisor and referrer get four screens -- name, code to the mailbox,
# password, done -- and ``finish`` has to accept an account with no credentials
# and no attestations on it. These tests pin BOTH halves of that: it accepts one
# for a non-clinical flavor, and still refuses one for a physician.

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from tests._asclepius import app, uniq

_WIZARD = (Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
           / "components" / "OnboardingWizard.tsx")
_JOIN = (Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
         / "components" / "JoinEntry.tsx")

_PW = "correct-horse-battery-1"


@pytest.fixture()
def signup_client(monkeypatch):
    from routers import onboarding as onboarding_module

    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _stub_send(*_a, **_k):
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _stub_send)
    from asclepius import credentialing as _cred
    monkeypatch.setattr(_cred, "fetch_npi_record",
                        lambda *a, **k: {"result": "unavailable", "reason": "test"})
    with TestClient(app) as c:
        yield c


def _open_invite(client, *, flavor=None, email=None):
    """An invite with its mailbox already proven, which is where the short
    signup's third screen begins."""
    ts = client.app.state.team_store
    email = email or f"a-{uniq()}@example.com"
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", director_email=email,
        product="asclepius")
    hs_id = invite["health_system_id"]
    ts.update_health_system_director_identity(
        hs_id, first_name="Dana", last_name="Whitfield", email=email)
    if flavor:
        ts.set_health_system_signup_flavor(hs_id, flavor)
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()
    return invite["onboarding_url"].rsplit("/", 1)[-1], email


@pytest.mark.parametrize("flavor", ["advisor", "referrer"])
def test_a_non_clinical_signup_finishes_without_credentials(signup_client, flavor):
    fresh_store()
    token, email = _open_invite(signup_client, flavor=flavor)
    assert signup_client.post("/api/onboarding/asclepius/password",
                              json={"token": token, "password": _PW}).status_code == 200
    r = signup_client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text
    user = signup_client.app.state.asclepius_store.get_user_by_email(email)
    assert user and user["account_kind"] == flavor


def test_a_physician_still_cannot_finish_without_credentials(signup_client):
    """The relaxation is scoped to the doors that produce a capped account. If
    it leaked to the physician door, anyone could hold a full account having
    answered nothing."""
    fresh_store()
    token, _ = _open_invite(signup_client)
    assert signup_client.post("/api/onboarding/asclepius/password",
                              json={"token": token, "password": _PW}).status_code == 200
    r = signup_client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 400
    assert "credentials" in r.json()["detail"].lower()


def test_finishing_a_signup_hands_back_a_session(signup_client):
    """``finish`` called ``authenticate(..., director_pwd)`` and that name does
    not exist in the function: the plaintext password was last seen several
    requests earlier. The NameError was swallowed by a bare except, so the token
    came back None for EVERY signup and every new doctor -- not just advisors --
    landed on the success screen with no session."""
    fresh_store()
    token, _ = _open_invite(signup_client, flavor="advisor")
    signup_client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": _PW})
    body = signup_client.post("/api/onboarding/asclepius/finish",
                              json={"token": token}).json()
    assert body.get("token"), "signup completed without signing the person in"


def test_a_non_clinical_signup_is_not_queued_as_a_doctor_to_review(signup_client):
    """There is no NPI to look up and no registration to match, so a row in the
    verification queue is work no admin can do and a queue depth that lies."""
    fresh_store()
    token, email = _open_invite(signup_client, flavor="advisor")
    signup_client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": _PW})
    signup_client.post("/api/onboarding/asclepius/finish", json={"token": token})
    user = signup_client.app.state.asclepius_store.get_user_by_email(email)
    assert user["verification_status"] is None


# ─── The wizard has to actually skip those screens ───────────────────────────
def test_the_wizard_gives_a_non_clinical_signup_the_short_order():
    """A backend that accepts a credential-free finish is no use if the wizard
    still walks the person through nine screens to reach it."""
    src = _WIZARD.read_text(encoding="utf-8")
    i = src.index("function orderFor")
    order = src[i:src.index("\n}\n", i)]
    m = re.search(r'if \(kind !== "physician"\) return \[\.\.\.head, "ascSuccess"\]', order)
    assert m, "orderFor no longer short-circuits the non-physician flavors"


def test_the_short_order_is_offered_to_exactly_the_capped_flavors():
    """"Skip the credential screens" and "this account is capped" must name the
    same set. A door that asks for less and grants everything is worse than
    either half alone."""
    import routers.onboarding as onboarding

    src = _WIZARD.read_text(encoding="utf-8")
    i = src.index("const KIND_BY_FLAVOR")
    block = src[i:src.index("}", i)]
    for flavor in onboarding.ACCOUNT_KIND_BY_FLAVOR:
        assert flavor in block, f"{flavor} is capped server-side but not short-signed-up"
    assert "general" not in block, (
        "'general' maps to no account kind, so it is capped by nothing and must "
        "keep the full wizard"
    )


# ═══ Referral credit is automatic ═════════════════════════════════════════════
def test_join_never_asks_anyone_to_type_a_code():
    """The link records the credit on its own. A code offered as an alternative
    made a manual step look supported, and every colleague who forgot it was an
    introduction a physician made and was not paid for."""
    src = _JOIN.read_text(encoding="utf-8")
    assert "Referral code" not in src
    assert "typedCode" not in src


def test_a_referral_follows_an_email_change_during_signup():
    """Attribution is keyed on the address the invite was addressed to, and the
    identity screen is where that address can change. Someone opening a
    colleague's link with a personal address and correcting it to their hospital
    one used to cost the referrer the credit, silently."""
    store = fresh_store()
    referrer = _user(store)
    typed = f"personal-{uuid.uuid4().hex[:6]}@gmail.com"
    corrected = f"work-{uuid.uuid4().hex[:6]}@hospital.org"
    store.insert_referral(referrer_id=referrer["id"],
                          referral_code=store.ensure_referral_code(referrer["id"]),
                          invitee_email=typed)

    assert store.move_open_referrals(typed, corrected) == 1
    assert store.find_open_referral_for_email(typed) is None
    assert store.find_open_referral_for_email(corrected) is not None

    signup = store.provision_user(email=corrected, password="pw-12345678",
                                  role="evaluator")
    claimed = store.claim_referral_for_signup(email=corrected, user_id=signup["id"])
    assert claimed is not None, "the referrer lost the credit for a real signup"


def test_a_claimed_referral_is_never_rewritten():
    """Settled history. Only rows still waiting for a signup may move."""
    store = fresh_store()
    referrer = _user(store)
    invitee = f"taken-{uuid.uuid4().hex[:6]}@hospital.org"
    store.insert_referral(referrer_id=referrer["id"],
                          referral_code=store.ensure_referral_code(referrer["id"]),
                          invitee_email=invitee)
    signup = store.provision_user(email=invitee, password="pw-12345678", role="evaluator")
    store.claim_referral_for_signup(email=invitee, user_id=signup["id"])
    assert store.move_open_referrals(invitee, "somewhere-else@example.com") == 0
