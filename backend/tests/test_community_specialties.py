"""Clinical identity and specialty discovery are independent of paid cases."""
import json

import pytest

from community import router, store as community_store
from asclepius.onboarding_specialties import CLINICAL_SPECIALTIES
from test_community_v2 import (
    BASE, channel_slugs, client, headers_for, make_approved_physician,
    make_vault_physician, setup_world,
)


@pytest.mark.parametrize("old_specialty", [None, "nephrology"])
def test_confirmed_dermatologist_has_profile_directory_and_room(old_specialty, monkeypatch):
    monkeypatch.delenv("COMMUNITY_SPECIALTY_MIN_MEMBERS", raising=False)
    astore, cstore, _ = setup_world()
    doctor = make_vault_physician(astore, specialty=old_specialty)
    with astore._conn() as conn:
        conn.execute("UPDATE users SET credentials_json=? WHERE id=?", (
            json.dumps({"primarySpecialty": "Dermatology", "licenseNumber": "PRIVATE-LICENSE",
                        "cvParsed": {"specialty": "nephrology"}}), doctor["id"]))
    before = astore.get_user_by_id(doctor["id"])
    hdr = headers_for(doctor)
    for endpoint in ("/me", "/members/" + doctor["id"]):
        response = client.get(BASE + endpoint, headers=hdr)
        assert response.status_code == 200
        assert response.json()["member"]["specialty"] == "dermatology"
        assert "nephrology" not in response.json()["member"]["blurb"].lower()
        assert "PRIVATE-LICENSE" not in response.text
        assert "cvParsed" not in response.text
    members = client.get(BASE + "/members?specialty=dermatology", headers=hdr).json()["members"]
    assert [m["user_id"] for m in members] == [doctor["id"]]
    assert "dermatology" in channel_slugs(doctor)
    assert "nephrology" not in channel_slugs(doctor)
    response = client.post(BASE + "/channels/dermatology/messages", headers=hdr,
                           json={"body": "Hello dermatology colleagues"})
    assert response.status_code == 200
    assert astore.get_user_by_id(doctor["id"]) == before  # read projection never repairs source rows


@pytest.mark.parametrize("credentials", [None, "{bad json", "[]", '{"primarySpecialty": "other"}'])
def test_legacy_vault_fallback_and_malformed_declarations(credentials):
    assert router._member_specialty(
        {"credentials_json": credentials, "specialty": "nephrology"},
        {"primary_specialty": "Dermatologist"}) == "dermatology"
    assert router._member_specialty({"credentials_json": credentials}, {}) is None


def test_first_approved_dermatologist_opens_room_but_staff_and_pending_do_not(monkeypatch):
    monkeypatch.delenv("COMMUNITY_SPECIALTY_MIN_MEMBERS", raising=False)
    astore, _, admin = setup_world()
    assert "dermatology" not in channel_slugs(admin)
    doctor = make_approved_physician(astore, specialty="dermatology")
    assert "dermatology" in channel_slugs(doctor)
    astore.record_verification_decision(doctor["id"], status="pending", decided_by="test")
    assert "dermatology" not in channel_slugs(admin)


def test_specialty_catalog_covers_clinical_vocabulary_without_slug_collisions():
    from community.subspecialties import SUBSPECIALTIES
    from asclepius.specialties import SPECIALTY_REGISTRY
    rooms = community_store.specialty_channel_defs()
    assert {c["specialty"] for c in rooms} == set(CLINICAL_SPECIALTIES)
    slugs = {c["slug"] for c in rooms}
    assert len(slugs) == len(rooms)
    assert not slugs.intersection(s.slug for s in SUBSPECIALTIES)
    assert all(" " not in slug for slug in slugs)
    assert "dermatology" not in SPECIALTY_REGISTRY  # no paid generation enabled


def test_dermatology_and_multiword_region_rooms_are_supported():
    rooms = community_store.specialty_region_channel_defs([
        "dermatology|north-america", "internal medicine|africa", "radiation oncology|africa"])
    assert {r["slug"] for r in rooms} == {
        "dermatology-north-america", "internal-medicine-africa", "specialty-radiation-oncology-africa"}
    assert {r["specialty"] for r in rooms} == {"dermatology", "internal medicine", "radiation oncology"}


def test_cohort_seeding_uses_confirmed_specialty_for_legacy_account(monkeypatch):
    import main
    monkeypatch.setenv("COMMUNITY_SPECIALTY_REGION_MIN_MEMBERS", "1")
    astore, cstore, _ = setup_world()
    doctor = make_approved_physician(astore, specialty=None)
    with astore._conn() as conn:
        conn.execute("UPDATE users SET credentials_json=?, country_of_practice='US' WHERE id=?",
                     (json.dumps({"primarySpecialty": "Dermatology"}), doctor["id"]))
    cohorts = main._member_cohorts()
    assert cohorts["specialty_regions"] == ["dermatology|north-america"]
    cstore.ensure_default_channels(specialty_regions=cohorts["specialty_regions"])
    assert "dermatology-north-america" in channel_slugs(doctor)


def test_reseeding_preserves_existing_subspecialty_identity_and_messages():
    _, cstore, _ = setup_world()
    cstore.ensure_default_channels(subspecialties=["radiation-oncology"])
    before = cstore.get_channel_by_slug("radiation-oncology")
    cstore.ensure_default_channels(subspecialties=["radiation-oncology"])
    after = cstore.get_channel_by_slug("radiation-oncology")
    assert after["id"] == before["id"]
    assert after["grp"] == before["grp"] == "subspecialty"
    assert cstore.get_channel_by_slug("specialty-radiation-oncology")["grp"] == "specialty"


def test_legacy_city_collision_fails_without_repurposing_history():
    _, cstore, _ = setup_world()
    with cstore._conn() as conn:
        conn.execute("UPDATE community_channels SET grp='city',specialty=NULL,city='dermatology' WHERE slug='dermatology'")
    before = cstore.list_channels(include_inactive=True)
    channel = cstore.get_channel_by_slug("dermatology")
    message = cstore.insert_message(channel_id=channel["id"], author_user_id="fixture",
                                    body="Original city room history")
    with pytest.raises(ValueError, match="Channel group conflict for dermatology"):
        cstore.ensure_default_channels()
    assert cstore.list_channels(include_inactive=True) == before
    assert cstore.get_message(message["id"])["body"] == "Original city room history"
