"""Advisor access requires an owner appointment as Reviewer.

Pending advisors may only read their application status. Approved reviewers
receive ordinary reviewer access; rejected or inactive advisors receive none.
The ordinary physician and referral-only account controls remain unchanged.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from asclepius import capabilities as caps
from tests._asclepius import app, fresh_store, headers_for

client = TestClient(app)


_DEFAULT = object()


def _account(store, *, kind=None, status=_DEFAULT, tier=_DEFAULT, name="Sam Okafor"):
    if status is _DEFAULT:
        status = "pending" if kind == caps.ADVISOR else "approved"
    if tier is _DEFAULT:
        tier = None if kind == caps.ADVISOR else "labeler"
    user = store.provision_user(
        email=f"v_{uuid.uuid4().hex[:8]}@example.com", password="pw-12345678",
        role="evaluator", full_name=name, account_kind=kind,
    )
    if status is not None:
        store.set_verification_status(user["id"], status)
    else:
        with store._conn() as conn:
            conn.execute("UPDATE users SET verification_status=NULL WHERE id=?", (user["id"],))
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
    assert surfaces == {caps.BROWSE}


@pytest.mark.parametrize("status,tier,can_work", [
    ("approved", "reviewer", True),
    ("approved", "labeler", False),  # Historical invalid tier is no reviewer grant.
    ("pending", None, False),
    (None, "reviewer", False),       # A tier with no decision cannot open access.
    ("rejected", "reviewer", False),
])
def test_only_reviewer_approval_opens_advisor_access(status, tier, can_work):
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR, status=status, tier=tier)
    surfaces = caps.surfaces(advisor)
    assert (caps.REAL_WORK in surfaces) is can_work
    assert (caps.COMMUNITY_WRITE in surfaces) is can_work


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
def test_a_pending_advisor_cannot_read_the_community():
    store = fresh_store()
    r = client.get("/api/community/channels",
                   headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 403


def test_an_approved_reviewer_advisor_can_read_the_community():
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR, status="approved", tier="reviewer")
    assert client.get("/api/community/channels", headers=headers_for(advisor)).status_code == 200


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


def test_a_pending_advisor_does_not_have_view_only_community_access():
    store = fresh_store()
    r = client.post("/api/community/channels/general/messages", json={"body": "hi"},
                    headers=headers_for(_account(store, kind=caps.ADVISOR)))
    assert r.status_code == 403
    assert "view-only" not in r.json()["detail"]


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


def test_pending_advisors_cannot_load_the_composer_but_approved_reviewers_can_post():
    store = fresh_store()
    pending = _account(store, kind=caps.ADVISOR)
    assert client.get("/api/community/me", headers=headers_for(pending)).status_code == 403
    approved = _account(store, kind=caps.ADVISOR, status="approved", tier="reviewer")
    assert client.get("/api/community/me", headers=headers_for(approved)).json()["can_post"] is True
    posted = client.post("/api/community/channels/general/messages", json={"body": "Hello colleagues."},
                         headers=headers_for(approved))
    assert posted.status_code == 200, posted.text


def test_an_advisor_sees_their_own_name_rather_than_former_member():
    """An approved advisor appears as a colleague with their own identity."""
    store = fresh_store()
    advisor = _account(store, kind=caps.ADVISOR, name="Dana Whitfield", status="approved", tier="reviewer")
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
def test_an_approved_reviewer_advisor_can_open_optional_practice():
    """Practice is available after approval, and is never an admission gate."""
    store = fresh_store()
    r = client.get("/api/asclepius/tutorial/task",
                   headers=headers_for(_account(store, kind=caps.ADVISOR, status="approved", tier="reviewer")))
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


# ─── The applicant reads nothing (the mirror of the write narrowing above) ────
def test_an_applicant_under_review_cannot_read_the_community():
    """The other half of the narrowing, and the half that was still open.

    ``require_poster`` refused the POST while ``_passes_gate`` fast-passed
    ``verification_status == 'pending'`` on the read, so an account that had
    done nothing but submit a form could open every physician-only channel and
    read every message in it. Rejecting the application afterwards does not
    unread them, which is the same argument that closed the write.
    """
    store = fresh_store()
    applicant = _account(store, status="pending", tier=None)
    for path in ("/api/community/me",
                 "/api/community/channels",
                 "/api/community/channels/general/messages",
                 "/api/community/members"):
        r = client.get(path, headers=headers_for(applicant))
        assert r.status_code == 403, f"{path} -> {r.status_code}"


def test_an_approved_physician_reads_and_posts():
    """The guard on the line above: the accounts that SHOULD be in the room are
    in it. A padlock on an approved physician's community tab is the same bug
    with the sign flipped."""
    store = fresh_store()
    doctor = _account(store, status="approved", tier="labeler")
    assert caps.COMMUNITY_READ in caps.surfaces(doctor)
    assert caps.COMMUNITY_WRITE in caps.surfaces(doctor)
    me = client.get("/api/community/me", headers=headers_for(doctor))
    assert me.status_code == 200, me.text
    assert me.json()["can_post"] is True
    assert client.get("/api/community/channels",
                      headers=headers_for(doctor)).status_code == 200
    posted = client.post("/api/community/channels/general/messages",
                         json={"body": "Reading and posting, as an approved colleague."},
                         headers=headers_for(doctor))
    assert posted.status_code == 200, posted.text


def test_an_applicant_is_not_a_member_and_is_not_mailed_channel_content():
    """A member row is not only a directory entry. It is a mention target, a
    head that counts towards whether a room opens, and the address
    ``resolve_member_for_notify`` hands the digest mailer, which sends message
    snippets. An applicant left in that map would go on receiving
    physician-only content by email after being shut out of the room."""
    from community.router import member_map, resolve_member_for_notify

    store = fresh_store()
    applicant = _account(store, status="pending", tier=None)
    doctor = _account(store, status="approved", tier="labeler")
    members = member_map()
    assert doctor["id"] in members
    assert applicant["id"] not in members
    assert resolve_member_for_notify(applicant["id"]) is None


def test_the_member_list_is_a_summary_and_the_profile_is_a_fetch():
    """The list is downloaded by every doctor on every page open, so it carries
    what the rail and @mention completion render and nothing else; the profile
    fields it used to duplicate a thousand times are one request per colleague
    somebody actually opens."""
    store = fresh_store()
    doctor = _account(store, status="approved", tier="labeler", name="Ada Reyes")
    listed = client.get("/api/community/members", headers=headers_for(doctor)).json()
    row = next(m for m in listed["members"] if m["user_id"] == doctor["id"])
    assert row["display_name"] == "Ada Reyes"
    for absent in ("blurb", "institution", "years_in_practice", "board_certified",
                   "fellowship_trained", "country", "city", "region", "subspecialties"):
        assert absent not in row, absent

    full = client.get(f"/api/community/members/{doctor['id']}",
                      headers=headers_for(doctor))
    assert full.status_code == 200, full.text
    assert set(("blurb", "institution", "years_in_practice")) <= set(full.json()["member"])
    # Same gate as everything else in here, and no oracle for who has an account.
    assert client.get(f"/api/community/members/{doctor['id']}",
                      headers=headers_for(_account(store, status="pending", tier=None))
                      ).status_code == 403
    assert client.get("/api/community/members/nobody-at-all",
                      headers=headers_for(doctor)).status_code == 404
