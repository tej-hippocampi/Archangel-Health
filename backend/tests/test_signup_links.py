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
