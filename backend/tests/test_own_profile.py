"""A physician's own profile and settings.

Before this, the only self-scoped write on the entire Asclepius API was the
tutorial state. A doctor could not see what we hold about them, could not fix a
mistyped phone number, and could not change their password without signing out
and pretending to have forgotten it. Everything about them was visible to
admins and to nobody else, including them.

The line these tests hold is which fields are theirs to change. Contact details
are. The registration number, the country, the degree, the verification status
and the tier are not: those were checked against a registry or attested to, and
a surface that let someone edit them after approval would make the check
meaningless. They are shown, plainly, and marked settled.
"""

from __future__ import annotations

import json

import pytest

from tests._asclepius import app, fresh_store, headers_for, make_user
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture
def doctor():
    store = fresh_store()
    user = make_user(store, role="evaluator", tier="labeler", specialty="nephrology")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET full_name = ?, phone = ?, npi = ?, board_cert = ?, "
            "country_of_licensure = ?, registry_id = ?, credentials_json = ?, "
            "attestations_json = ? WHERE id = ?",
            ("Ahmed Al Otaibi", "+966 55 000 1122", None,
             "Saudi Board of Internal Medicine", "SA", "1234567",
             json.dumps({"qualification": "MBBS"}),
             json.dumps({"signedInitials": "AAO"}), user["id"]),
        )
    return store, store.get_user_by_id(user["id"])


def test_a_doctor_can_read_their_own_profile(doctor):
    store, user = doctor
    r = client.get("/api/asclepius/me/profile", headers=headers_for(user))
    assert r.status_code == 200
    body = r.json()
    assert body["editable"]["full_name"] == "Ahmed Al Otaibi"
    assert body["credentials"]["registration_number"] == "1234567"
    assert body["credentials"]["country_of_licensure"] == "SA"
    assert body["standing"]["tier_word"] == "Labeler"


def test_the_registry_is_named_rather_than_left_as_a_country_code(doctor):
    """"SA" means nothing to the doctor who registered with SCFHS."""
    store, user = doctor
    body = client.get("/api/asclepius/me/profile", headers=headers_for(user)).json()
    assert "Saudi Commission" in body["credentials"]["registry_name"]


def test_the_signed_initials_are_visible_to_the_person_who_signed_them(doctor):
    """They were collected, stored, and then never shown to anybody at all."""
    store, user = doctor
    body = client.get("/api/asclepius/me/profile", headers=headers_for(user)).json()
    assert body["credentials"]["signed_initials"] == "AAO"


def test_contact_details_are_theirs_to_correct(doctor):
    store, user = doctor
    r = client.patch(
        "/api/asclepius/me/profile",
        json={"phone": "+966 55 999 0000",
              "linkedin_url": "https://www.linkedin.com/in/aalotaibi"},
        headers=headers_for(user),
    )
    assert r.status_code == 200
    row = store.get_user_by_id(user["id"])
    assert row["phone"] == "+966 55 999 0000"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/aalotaibi"


def test_a_partial_update_leaves_everything_else_alone(doctor):
    store, user = doctor
    client.patch("/api/asclepius/me/profile", json={"phone": "+966 55 111 2222"},
                 headers=headers_for(user))
    row = store.get_user_by_id(user["id"])
    assert row["full_name"] == "Ahmed Al Otaibi"


def test_an_empty_string_clears_a_field_that_may_legitimately_be_empty(doctor):
    store, user = doctor
    client.patch("/api/asclepius/me/profile", json={"linkedin_url": ""},
                 headers=headers_for(user))
    assert store.get_user_by_id(user["id"])["linkedin_url"] is None


@pytest.mark.parametrize("field,value", [
    ("registry_id", "9999999"),
    ("country_of_licensure", "US"),
    ("verification_status", "approved"),
    ("tier", "reviewer"),
    ("npi", "1234567893"),
    ("board_cert", "American Board of Internal Medicine"),
])
def test_credential_fields_cannot_be_edited_from_the_profile(doctor, field, value):
    """The whole point of checking a credential is that its holder cannot then
    change it. An unknown field is ignored rather than erroring, so this asserts
    the column, not the response."""
    store, user = doctor
    before = store.get_user_by_id(user["id"])[field]
    client.patch("/api/asclepius/me/profile", json={field: value},
                 headers=headers_for(user))
    assert store.get_user_by_id(user["id"])[field] == before


# ─── Password ────────────────────────────────────────────────────────────────
def test_a_doctor_can_change_their_password_while_signed_in(doctor):
    store, user = doctor
    store.set_user_password(user["id"], "old-password-1")
    r = client.post(
        "/api/asclepius/me/password",
        json={"current_password": "old-password-1", "new_password": "new-password-2"},
        headers=headers_for(store.get_user_by_id(user["id"])),
    )
    assert r.status_code == 200
    from asclepius.store import verify_password
    assert verify_password("new-password-2",
                           store.get_user_by_id(user["id"])["password_hash"])


def test_the_current_password_is_required(doctor):
    """A session left open on a ward computer is not enough to take the
    account."""
    store, user = doctor
    store.set_user_password(user["id"], "old-password-1")
    r = client.post(
        "/api/asclepius/me/password",
        json={"current_password": "not-it", "new_password": "new-password-2"},
        headers=headers_for(store.get_user_by_id(user["id"])),
    )
    assert r.status_code == 403
    from asclepius.store import verify_password
    assert verify_password("old-password-1",
                           store.get_user_by_id(user["id"])["password_hash"])


def test_a_short_password_is_refused(doctor):
    store, user = doctor
    store.set_user_password(user["id"], "old-password-1")
    r = client.post(
        "/api/asclepius/me/password",
        json={"current_password": "old-password-1", "new_password": "short"},
        headers=headers_for(store.get_user_by_id(user["id"])),
    )
    assert r.status_code == 422


# ─── Reachable while waiting ─────────────────────────────────────────────────
def test_a_physician_awaiting_verification_can_still_reach_their_profile():
    """They are the people most likely to want to fix a typo in what they just
    submitted."""
    store = fresh_store()
    user = make_user(store, role="evaluator", tier=None)
    store.set_verification_status(user["id"], "pending")
    r = client.get("/api/asclepius/me/profile",
                   headers=headers_for(store.get_user_by_id(user["id"])))
    assert r.status_code == 200
    assert r.json()["standing"]["verification_status"] == "pending"
