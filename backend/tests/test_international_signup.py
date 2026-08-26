"""Signing up from outside the United States.

The form required a 10-digit NPI and a two-letter US state licence, so a Saudi
consultant registered with SCFHS could not finish it — not because we would
turn them away, but because there was nowhere to put what they hold. A doctor
asked us directly whether we take Saudi degrees; we do, and the site did not
say so anywhere.

What has to stay true, and is what these tests hold:

  * A US signup behaves exactly as it did. That is most of the traffic and
    none of it should feel this change.
  * A doctor licensed elsewhere gets through the form, gets checked against
    their own country's registry, and is never rejected for the state of
    someone else's database.
  * Country routes verification and never scores. It is one step from IMG
    status, which §3.3 forbids as a score input.
"""

from __future__ import annotations

import json
import uuid

import pytest

import routers.onboarding as onboarding_module
from asclepius import credentialing, tiering
from tests._asclepius import fresh_store


def _user(store, **kw):
    email = kw.pop("email", f"dr_{uuid.uuid4().hex[:8]}@hospital.example")
    return store.provision_user(
        email=email, password="pw-12345678", role="evaluator",
        full_name=kw.pop("full_name", "Ahmed Al Otaibi"), **kw,
    )


def _saudi_credentials(**overrides):
    base = {
        "fullLegalName": "Ahmed Al Otaibi",
        "countryOfPractice": "SA",
        "countryOfLicensure": "SA",
        "countryOfDegree": "SA",
        "registrationNumber": "1234567",
        "qualification": "MBBS",
        "degree": "MBBS",
        "phone": "+966 55 000 1122",
        "primarySpecialty": "Nephrology",
        "residencyCompleted": True,
        "practiceStatus": "active",
        "residency": {"institution": "King Faisal Specialist Hospital", "year": "2013"},
    }
    base.update(overrides)
    return base


def _india_credentials(**overrides):
    base = {
        "fullLegalName": "Anoopkumar Prakash",
        "countryOfPractice": "IN",
        "countryOfLicensure": "IN",
        "registrationNumber": "45678",
        "registryExtras": {"stateCouncil": "Maharashtra Medical Council",
                           "registrationYear": "1981"},
        "qualification": "MBBS",
        "degree": "MBBS",
        "phone": "+91 98200 00000",
        "primarySpecialty": "Nephrology",
        "residencyCompleted": True,
        "practiceStatus": "active",
    }
    base.update(overrides)
    return base


# ─── The signup gets through ─────────────────────────────────────────────────
def test_a_saudi_doctor_can_sign_up_without_an_npi():
    """The case that prompted this: no NPI, no US state licence, still a
    complete signup that lands in the queue rather than nowhere."""
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    row = store.get_user_by_id(user["id"])
    assert row["country_of_licensure"] == "SA"
    assert row["country_of_practice"] == "SA"
    assert row["registry_id"] == "1234567"
    assert row["verification_status"] == "pending"


def test_a_country_with_no_queryable_register_says_so_rather_than_failing():
    """SCFHS's public check is behind two captchas and keyed on a national ID.
    That is a document-review path, not a failed verification."""
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    row = store.get_user_by_id(user["id"])
    payload = json.loads(row["registry_payload_json"] or "{}")
    assert payload["result"] == "document_only"
    # ...and nothing has been decided against them.
    assert row["registry_verified"] is None


def test_a_registry_we_can_query_is_left_queued_for_the_agent():
    """NPPES answers in under a second; a foreign register may not answer at
    all, and the signup form is the last place to find that out."""
    store = fresh_store()
    user = _user(store, full_name="Anoopkumar Prakash")
    onboarding_module._run_signup_verification(store, user, _india_credentials())

    row = store.get_user_by_id(user["id"])
    payload = json.loads(row["registry_payload_json"] or "{}")
    assert payload["result"] == "queued"
    assert row["registry_id"] == "45678"


def test_the_us_path_is_untouched(monkeypatch):
    store = fresh_store()
    user = _user(store, full_name="Jane Okafor")
    captured = {}

    def _verify_npi(npi, family_name, timeout=None, cached=None):
        captured["npi"] = npi
        return {"result": "verified", "npi": npi, "reason": None,
                "record": {"credential": "MD"}, "from_cache": False}

    monkeypatch.setattr(credentialing, "verify_npi", _verify_npi)
    onboarding_module._run_signup_verification(store, user, {
        "fullLegalName": "Jane Okafor", "npi": "1234567893",
        "licenseNumber": "A94021", "licenseState": "CA", "degree": "MD",
        "primarySpecialty": "Nephrology",
    })

    row = store.get_user_by_id(user["id"])
    assert captured["npi"] == "1234567893"
    assert row["npi_verified"] == 1
    # No country on the form means a US signup, which is who was signing up
    # before the question existed.
    assert (row["country_of_licensure"] or "US") == "US"


# ─── Gates ───────────────────────────────────────────────────────────────────
def test_a_non_us_doctor_is_not_failed_for_lacking_an_npi():
    """A1 used to read NPPES and nothing else, so every international doctor
    failed a gate for not holding an American identifier."""
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    row = store.get_user_by_id(user["id"])
    gates = tiering.hard_gates(row)["gates"]
    assert gates["A1"]["state"] == tiering.UNKNOWN   # pending a human, not failed
    assert gates["A1"]["state"] != tiering.FAIL
    assert "document" in gates["A1"]["detail"].lower()


def test_a_non_us_doctor_is_not_asked_for_a_us_state_licence():
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    gates = tiering.hard_gates(store.get_user_by_id(user["id"]))["gates"]
    assert gates["A2"]["state"] != tiering.FAIL
    assert "registration" in gates["A2"]["detail"].lower()


def test_a_registry_verified_doctor_passes_the_identity_gate():
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _india_credentials())
    store.set_registry_result(user["id"], {
        "result": "verified", "registry": "Indian Medical Register",
        "identifier": "45678", "record": {"full_name": "Anoopkumar Prakash"},
    })

    gates = tiering.hard_gates(store.get_user_by_id(user["id"]))["gates"]
    assert gates["A1"]["state"] == tiering.PASS
    assert gates["A2"]["state"] == tiering.PASS


def test_the_us_only_exclusion_list_cannot_clear_an_international_doctor():
    """Absence from a list of people excluded from US federal health
    programmes says nothing about a doctor who was never in them. That
    unresolved gate is exactly why an international signup reaches a human."""
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    gates = tiering.hard_gates(
        store.get_user_by_id(user["id"]), leie_status="clear")["gates"]
    assert gates["A5"]["state"] == tiering.UNKNOWN
    assert "US-only" in gates["A5"]["detail"]


@pytest.mark.parametrize("qualification", ["MBBS", "MBChB", "Staatsexamen", "MD", "DO"])
def test_qualifications_that_are_not_called_md_still_clear_the_degree_gate(qualification):
    """"MD" is the primary qualification across much of Europe and a
    POSTGRADUATE degree in India. Germany awards no degree in the usual sense
    at all -- physicians finish with the Staatsexamen."""
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(
        store, user, _saudi_credentials(qualification=qualification, degree=qualification))

    gates = tiering.hard_gates(store.get_user_by_id(user["id"]))["gates"]
    assert gates["A3"]["state"] != tiering.FAIL


# ─── Country routes verification; it never scores ────────────────────────────
def test_a_registry_verified_doctor_scores_the_same_as_an_npi_verified_one():
    store = fresh_store()
    intl = _user(store)
    onboarding_module._run_signup_verification(store, intl, _saudi_credentials())
    store.set_registry_result(intl["id"], {"result": "verified", "registry": "SCFHS"})

    domestic = _user(store, full_name="Ahmed Al Otaibi")
    store.set_npi_result(domestic["id"], {
        "result": "verified", "npi": "1234567893", "record": {}, "reason": None})

    a = credentialing.propose_tier(store.get_user_by_id(intl["id"]))
    b = credentialing.propose_tier(store.get_user_by_id(domestic["id"]))
    assert a["score"] == b["score"]


def test_signup_flags_are_recorded_even_when_clean():
    """"Assessed and clean" has to be distinguishable from "never assessed"."""
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    row = store.get_user_by_id(user["id"])
    assert row["flagged"] == 0
    assert json.loads(row["flags_json"] or "null") == []


def test_a_nonsense_international_signup_is_flagged():
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials(
        registrationNumber="kkkl",
        residency={"institution": "jkj", "year": "7689"},
    ))

    row = store.get_user_by_id(user["id"])
    assert row["flagged"] == 1
    fields = {f["field"] for f in json.loads(row["flags_json"])}
    assert "residency_year" in fields


def test_registration_numbers_are_only_duplicates_within_one_country():
    """A PMDC number and an Indian council number that share digits are not
    the same credential, and calling them a duplicate accuses two unrelated
    doctors."""
    store = fresh_store()
    a = _user(store)
    onboarding_module._run_signup_verification(
        store, a, _india_credentials(registrationNumber="45678"))
    b = _user(store, full_name="Yasir Iqbal")
    onboarding_module._run_signup_verification(
        store, b, _saudi_credentials(registrationNumber="45678"))

    assert len(store.find_users_by_registry_id("45678", country="IN")) == 1
    assert len(store.find_users_by_registry_id("45678", country="SA")) == 1
    assert len(store.find_users_by_registry_id("45678")) == 2


# ─── What an admin can actually see ──────────────────────────────────────────
def test_the_admin_dossier_shows_the_registry_that_answers_for_this_doctor():
    """"No NPI provided" over a physician registered with SCFHS reports an
    absence that was never expected. The queue now names their registry, says
    what it returned, and links where to check by hand."""
    from tests._asclepius import app, headers_for, make_user
    from fastapi.testclient import TestClient

    store = fresh_store()
    admin = make_user(store, role="admin")
    doctor = _user(store)
    onboarding_module._run_signup_verification(store, doctor, _saudi_credentials())

    client = TestClient(app)
    r = client.get(f"/api/asclepius/verify/queue/{doctor['id']}",
                   headers=headers_for(admin))
    assert r.status_code == 200
    registry = r.json()["registry"]
    assert registry["is_us"] is False
    assert "Saudi Commission" in registry["registry_name"]
    assert registry["identifier"] == "1234567"
    assert registry["lookup_url"]          # somewhere to go
    assert registry["note"]                # and what to do when you get there


def test_the_admin_dossier_carries_the_signup_flags():
    from tests._asclepius import app, headers_for, make_user
    from fastapi.testclient import TestClient

    store = fresh_store()
    admin = make_user(store, role="admin")
    doctor = _user(store)
    onboarding_module._run_signup_verification(store, doctor, _saudi_credentials(
        residency={"institution": "jkj", "year": "7689"}))

    client = TestClient(app)
    body = client.get(f"/api/asclepius/verify/queue/{doctor['id']}",
                      headers=headers_for(admin)).json()
    assert any(f["field"] == "residency_year" for f in body["flags"])


def test_the_admin_physician_profile_returns_the_credentials_blob():
    """It was captured at signup and rendered by nothing, so licence number,
    training and the signed initials were invisible on every admin surface."""
    from tests._asclepius import app, headers_for, make_user
    from fastapi.testclient import TestClient

    store = fresh_store()
    admin = make_user(store, role="admin")
    doctor = _user(store)
    creds = _india_credentials()
    onboarding_module._run_signup_verification(store, doctor, creds)
    # Provisioning writes these blobs; this test drives the verification step
    # directly, so it has to stand them up itself.
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET credentials_json = ?, attestations_json = ? WHERE id = ?",
            (json.dumps(creds), json.dumps({"signedInitials": "AP"}), doctor["id"]))

    client = TestClient(app)
    body = client.get(f"/api/asclepius/admin/physicians/{doctor['id']}",
                      headers=headers_for(admin)).json()
    assert body["credentials"]["registrationNumber"] == "45678"
    assert body["attestations"]["signedInitials"] == "AP"
    assert body["physician"]["registry_name"]
    assert body["physician"]["country_of_licensure"] == "IN"
