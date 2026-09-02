"""Advisor accounts: the whole product, view-only.

An advisor is here to understand what we do and introduce people to it. They
are not a clinician, and the signup that produced them never asked them to be
one: four screens, one confidentiality line, no NPI and none of the seven
clinical attestations.

Two things must hold for that to be safe, and they are what this file tests.

The first is that asking for less at the door produces an account that can do
less, permanently. The surface cap is applied on every call and intersected
with whatever the access level grants, so an admin clicking Approve on an
advisor -- which is the obvious thing to do with an unfamiliar row in a queue
-- moves them to FULL and changes nothing about what they can reach.

The second is that ``view-only`` is enforced by the server rather than drawn by
the client. Hiding a composer is a nicety; refusing the POST is the control.
``COMMUNITY_WRITE`` was a dead constant before this -- declared, never imported
-- so reading and writing in the community were the same permission, and every
content route would have accepted an advisor.

Throughout: a physician's behaviour must not change. A doctor under review is
PROVISIONAL, holds COMMUNITY_WRITE, and still posts exactly as they always did;
several tests below exist only to pin that.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from asclepius import capabilities as caps
from tests._asclepius import app, fresh_store, headers_for

client = TestClient(app)


def _account(store, *, kind=None, status="approved", tier="labeler", name="Sam Okafor"):
    user = store.provision_user(
        email=f"v_{uuid.uuid4().hex[:8]}@example.com", password="pw-12345678",
        role="evaluator", full_name=name, account_kind=kind,
    )
    if status:
        store.set_verification_status(user["id"], status)
    if tier:
        with store._conn() as conn:
            conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user["id"]))
    return store.get_user_by_id(user["id"])


# ─── The cap ─────────────────────────────────────────────────────────────────
def test_an_advisor_holds_neither_real_work_nor_community_write():
    store = fresh_store()
    surfaces = caps.surfaces(_account(store, kind=caps.ADVISOR))
    assert caps.REAL_WORK not in surfaces
    assert caps.COMMUNITY_WRITE not in surfaces
    # ...and does hold everything the tour is made of.
    assert {caps.BROWSE, caps.TUTORIAL, caps.COMMUNITY_READ,
            caps.EARNINGS, caps.REFERRAL} <= surfaces


@pytest.mark.parametrize("status,tier", [
    ("approved", "reviewer"),   # the dangerous one: approval used to grant everything
    ("approved", "labeler"),
    ("pending", None),
    (None, "reviewer"),         # the pre-verification-era NULL, which reads as FULL
])
def test_approving_an_advisor_does_not_make_them_a_physician(status, tier):
    """The cap is intersected with the access level on every call, so there is
    no state -- and no admin action -- that reaches REAL_WORK."""
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR, status=status, tier=tier)
    surfaces = caps.surfaces(advisor)
    assert caps.REAL_WORK not in surfaces
    assert caps.COMMUNITY_WRITE not in surfaces


def test_a_deactivated_advisor_reaches_nothing():
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR)
    with store._conn() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (advisor["id"],))
    assert caps.surfaces(store.get_user_by_id(advisor["id"])) == frozenset()


def test_a_physician_is_capped_by_nothing():
    """The whole mechanism must be invisible to the accounts that predate it."""
    store = fresh_store()
    doctor = _account(store)
    assert caps.surfaces(doctor) == frozenset(caps.SURFACES)


# ─── The community, enforced server-side ─────────────────────────────────────
def test_an_advisor_can_read_the_community():
    """They carry a NULL verification_status and no vault row, so the ordinary
    gate would refuse them. The point of showing them around is that they can
    see the room."""
    store = fresh_store()
    r = client.get("/api/community/channels",
                   headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 200


def test_a_referral_only_account_cannot_read_the_community():
    """"A referral link and nothing else" has to mean something."""
    store = fresh_store()
    r = client.get("/api/community/channels",
                   headers=headers_for(_account(store, kind=caps.REFERRER)))
    assert r.status_code == 403


def test_an_advisor_cannot_post_in_a_channel():
    store = fresh_store()
    r = client.post("/api/community/channels/general/messages",
                    json={"body": "Hello everyone, I am not a doctor."},
                    headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 403


def test_the_refusal_says_view_only_rather_than_unverified():
    """An advisor is not waiting on a credential check. Telling them their
    credentials are being verified would be telling them to wait for something
    that is never going to arrive."""
    store = fresh_store()
    r = client.post("/api/community/channels/general/messages", json={"body": "hi"},
                    headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert "view-only" in r.json()["detail"]


def test_an_applicant_under_review_cannot_post_to_the_community():
    """This REVERSES an earlier decision, so it is worth saying why rather than
    just flipping the expected status code.

    An applicant used to be able to post here, on the reasoning that they had
    cleared a mailbox OTP and signed the attestations. But the rooms' whole
    value is that everyone in them is a verified clinician, and a post from an
    unvetted account under a physician identity is exactly what the review
    queue exists to prevent. Rejecting the application afterwards does not
    unsend the post: the colleagues have already read it.

    What an applicant gets instead is the practice case, which is real work
    and is the thing the wait is now for."""
    store = fresh_store()
    doctor = _account(store, status="pending", tier=None)
    r = client.post("/api/community/channels/general/messages",
                    json={"body": "Anyone else seeing this pattern in CKD staging?"},
                    headers=headers_for(doctor))
    assert r.status_code == 403, r.text


def test_an_approved_physician_still_posts():
    """The guard on the change above: narrowing the applicant state must not
    touch the state everyone actually works in."""
    store = fresh_store()
    doctor = _account(store, status="approved", tier="labeler")
    r = client.post("/api/community/channels/general/messages",
                    json={"body": "Anyone else seeing this pattern in CKD staging?"},
                    headers=headers_for(doctor))
    assert r.status_code == 200, r.text


def test_an_advisor_cannot_open_a_direct_message():
    """A DM is strictly more privileged than a channel post. It is also the
    gate an advisor would otherwise have walked straight through: they carry a
    NULL status, and access_level folds NULL in with 'approved'."""
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR)
    doctor = _account(store)
    r = client.post("/api/community/dms", json={"user_id": doctor["id"]},
                    headers=headers_for(advisor))
    assert r.status_code == 403


def test_an_advisor_cannot_react_to_a_physicians_message():
    store = fresh_store()
    doctor = _account(store)
    posted = client.post("/api/community/channels/general/messages",
                         json={"body": "A note about contrast timing."},
                         headers=headers_for(doctor))
    assert posted.status_code == 200, posted.text
    message_id = posted.json()["id"]
    r = client.post(f"/api/community/messages/{message_id}/reactions",
                    json={"emoji": "👍"},
                    headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 403


def test_the_composer_is_told_it_is_read_only():
    """The server refuses either way; this is so the client can say why
    instead of accepting a message and then failing to send it."""
    store = fresh_store()
    advisor = client.get("/api/community/me",
                         headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert advisor.json()["can_post"] is False
    doctor = client.get("/api/community/me", headers=headers_for(_account(store)))
    assert doctor.json()["can_post"] is True


def test_an_advisor_sees_their_own_name_rather_than_former_member():
    """They are deliberately absent from the member directory, and the ghost
    fallback would have introduced them to themselves as "Former member"."""
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR, name="Dana Whitfield")
    body = client.get("/api/community/me", headers=headers_for(advisor)).json()
    assert body["member"]["display_name"] == "Dana Whitfield"
    assert body["member"]["initials"] == "DW"


def test_an_advisor_is_not_in_the_member_directory():
    """They must not appear as a colleague, must not count toward the specialty
    and country channel thresholds, and must not receive a mention or a digest."""
    from community.router import member_map

    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR)
    doctor = _account(store)
    members = member_map()
    assert doctor["id"] in members
    assert advisor["id"] not in members


# ─── The practice case is theirs to run ──────────────────────────────────────
def test_an_advisor_can_open_the_practice_case():
    """It is the whole demo, and it is virtual end to end: assembled in memory,
    never inserted into ``tasks``, and its submission never enters the
    pipeline."""
    store = fresh_store()
    r = client.get("/api/asclepius/tutorial/task",
                   headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 200


def test_an_advisor_cannot_draw_a_real_case():
    store = fresh_store()
    r = client.get("/api/asclepius/tasks/available",
                   headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 403


def test_a_referral_only_account_cannot_open_the_practice_case():
    store = fresh_store()
    r = client.get("/api/asclepius/tutorial/task",
                   headers=headers_for(_account(store, kind=caps.REFERRER)))
    assert r.status_code == 403
