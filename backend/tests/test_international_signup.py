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
    """"Assessed and clean" has to be distinguishable from "never assessed".

    Clean means ``flagged == 0``, not an empty list. Onboarding v2 §2 also
    records what an application did NOT bring — no CV, no board certification —
    as LOW-severity notes, and those are the right thing to see on an otherwise
    clean signup: an admin should be able to tell "we checked and this holds up"
    from "we checked and there is nothing here to check". They do not flag,
    which is what keeps ``flagged`` meaning what it meant.
    """
    store = fresh_store()
    user = _user(store)
    onboarding_module._run_signup_verification(store, user, _saudi_credentials())

    row = store.get_user_by_id(user["id"])
    assert row["flagged"] == 0
    findings = json.loads(row["flags_json"] or "null")
    assert findings is not None, "an assessed signup must not look like an unassessed one"
    # Nothing about this signup fails to hold together...
    assert [f for f in findings if f["issue"] != "not_provided"] == []
    # ...and everything recorded is a low note about absent evidence.
    assert all(f["severity"] == "low" for f in findings)


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


# ── Screen one, which is where the door was actually shut ────────────────────
#
# Everything above this line tests verification: what happens to a non-US doctor
# once their credentials reach the backend. None of it ran for the physician who
# reported the bug, because they never got past the first screen.
#
# "Outside the US" was passed to the state picker as its PLACEHOLDER, and
# SelectField renders every placeholder as a disabled option. The one honest
# answer a GMC-registered consultant had was the only entry they could not
# click, so they stopped and wrote to us. The country is now asked on screen 1,
# ahead of the state, and these tests hold that door open.

import sqlite3  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from tests._asclepius import app, uniq  # noqa: E402

_PW = "correct-horse-battery-1"

_GB_CREDS = {
    "fullLegalName": "Dr Eleanor Whitfield",
    "countryOfPractice": "GB",
    "countryOfLicensure": "GB",
    "countryOfDegree": "GB",
    "registrationNumber": "1234567",
    "qualification": "MBChB",
    "degree": "MBChB",
    "primarySpecialty": "Nephrology",
    "phone": "7700900123",
    "currentlyActive": True,
    "residencyCompleted": True,
    "practiceStatus": "active",
    # DELIBERATELY STALE, and the point of several assertions below. A CV parse
    # fills these two without ever checking the country, and a cached tab still
    # posts the old shape, so the server has to drop them rather than trust the
    # form to have cleared them. Left in place, credentials.py would ship this
    # consultant to a buyer as holding a California licence.
    "licenseState": "CA",
    "licenseNumber": "A12345",
}
_ATTS = {
    "consentCredentialShare": True, "attestIndependentJudgment": True,
    "ipAssignment": True, "noPhi": True, "attestConfidentiality": True,
    "attestNoDisciplinaryAction": True, "attestWorkQuality": True,
    "signedInitials": "EW",
}


@pytest.fixture()
def http():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def _mail(monkeypatch):
    sent = []
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _capture(to, subject, body, **kw):
        sent.append({"to": to, "subject": subject})
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _capture)
    return sent


@pytest.fixture(autouse=True)
def _no_real_nppes(monkeypatch):
    monkeypatch.setattr(credentialing, "fetch_npi_record",
                        lambda *a, **k: {"result": "unavailable", "reason": "test"})


def _invite(http, email):
    ts = http.app.state.team_store
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", director_email=email, product="asclepius")
    return invite["onboarding_url"].rsplit("/", 1)[-1], invite["health_system_id"]


def _prove_mailbox(http, hs_id):
    ts = http.app.state.team_store
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()


def _saved_credentials(http, hs_id, email):
    """The credentials blob as stored, read back off the asclepius_people row."""
    people = http.app.state.team_store.list_asclepius_people(hs_id)
    person = next(p for p in people
                  if (p.get("email") or "").lower() == email.lower())
    raw = person.get("credentials") or person.get("credentials_json") or {}
    return json.loads(raw) if isinstance(raw, str) else raw


def _step1(http, token, email, **extra):
    payload = {"token": token, "first_name": "Eleanor", "last_name": "Whitfield",
               "email": email, "password": _PW}
    payload.update(extra)
    return http.post("/api/onboarding/step1-identity", json=payload)


def test_a_uk_physician_completes_signup_end_to_end(http, _mail):
    """The headline. This is the doctor who wrote to us, all the way through."""
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, email)

    assert _step1(http, token, email, country_of_licensure="GB").status_code == 200
    _prove_mailbox(http, hs_id)

    assert http.post("/api/onboarding/asclepius/credentials",
                     json={"token": token, "credentials": _GB_CREDS}).status_code == 200
    assert http.post("/api/onboarding/asclepius/attestations",
                     json={"token": token, "attestations": _ATTS}).status_code == 200
    finish = http.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert finish.status_code == 200, finish.text

    user = http.app.state.asclepius_store.get_user_by_email(email)
    assert user is not None, "a UK doctor finished the form and got no application"
    assert (user["country_of_licensure"] or "").upper() == "GB"
    # Their GMC number is on the row, where a US doctor's NPI would be.
    assert (user["registry_id"] or "") == "1234567"
    assert not (user["npi"] or ""), "a UK consultant was given an NPI"
    # GB verifies by DOCUMENT (config.py), so this waits for a human rather than
    # being auto-rejected for failing a US lookup it was never eligible for.
    assert user["registry_verified"] is None or user["registry_verified"] == 0
    # The stale US licence posted in _GB_CREDS was DROPPED. This is the one that
    # reaches buyers: credentials.py turns a non-empty licenseState into
    # `state_licensed: true` and a US medical-board lookup handle.
    stored = json.loads(user["credentials_json"] or "{}")
    assert not (stored.get("licenseState") or ""), "a UK consultant kept a US state"
    assert not (stored.get("licenseNumber") or "")


def test_screen_one_carries_the_country_into_a_resumed_session(http, _mail):
    """A reload used to come back with the country forgotten, and `isUS`
    silently calls a forgotten country American."""
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, _ = _invite(http, email)
    assert _step1(http, token, email, country_of_licensure="GB").status_code == 200

    body = http.get(f"/api/onboarding/session?token={token}").json()
    assert body["director_country_of_licensure"] == "GB"
    assert body["director_license_state"] == ""


def test_a_stale_client_cannot_pin_a_us_state_on_a_non_us_doctor(http, _mail):
    """A cached tab running the previous bundle still posts a state. Anything
    non-empty in that column ships to buyers as `state_licensed: true`."""
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, email)
    assert _step1(http, token, email,
                  country_of_licensure="GB", license_state="CA").status_code == 200

    row = http.app.state.team_store.get_health_system_by_id(hs_id)
    assert (row["director_license_state"] or "") == ""
    assert (row["director_country_of_licensure"] or "") == "GB"


def test_correcting_the_country_clears_a_state_already_stored(http, _mail):
    """Screen 1 is re-submittable, and every other field on it is COALESCE'd so
    a resubmit cannot blank it. The state is the one field that must clear:
    otherwise a physician who picks California and then corrects the country to
    GB leaves 'CA' on the row forever."""
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, email)

    assert _step1(http, token, email,
                  country_of_licensure="US", license_state="CA").status_code == 200
    assert http.app.state.team_store.get_health_system_by_id(
        hs_id)["director_license_state"] == "CA"

    assert _step1(http, token, email, country_of_licensure="GB").status_code == 200
    row = http.app.state.team_store.get_health_system_by_id(hs_id)
    assert (row["director_license_state"] or "") == "", "the stale state stuck"
    assert (row["director_country_of_licensure"] or "") == "GB"


def test_an_unconfigured_country_is_accepted_rather_than_rejected(http, _mail):
    """The dead end must not move. A whitelist here would shut the door on the
    first doctor from a country nobody has configured yet, which is exactly the
    failure this change exists to remove."""
    fresh_store()
    email = f"dr-{uniq()}@hospital.example"
    token, hs_id = _invite(http, email)
    assert _step1(http, token, email, country_of_licensure="ZW").status_code == 200
    assert http.app.state.team_store.get_health_system_by_id(
        hs_id)["director_country_of_licensure"] == "ZW"


def test_the_us_path_is_unchanged_for_a_client_that_sends_no_country(http, _mail):
    """The regression guard for most of the traffic, and for the deploy window
    in which a browser is still running the previous bundle."""
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, hs_id = _invite(http, email)
    # No country key at all, exactly as the old bundle posts it.
    assert _step1(http, token, email, license_state="ca").status_code == 200

    row = http.app.state.team_store.get_health_system_by_id(hs_id)
    assert row["director_license_state"] == "CA", "the US state was not kept"
    assert (row["director_country_of_licensure"] or "") == ""


def _steps_source() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
            / "components" / "onboarding" / "steps.tsx").read_text(encoding="utf-8")


def _screen_one() -> str:
    src = _steps_source()
    return src[src.index("export function Step1NameEmail"):
               src.index("export function Step2Verify")]


def test_screen_one_takes_its_countries_from_the_shared_config():
    """config.py is the single country list, and screen 1 must not grow a
    second one. It reads the same hook the Review screen does, whose offline
    fallback covers every country in countries.json — so a failed config fetch
    degrades to slightly plainer labels, never to a dropdown the doctor's
    country is missing from."""
    screen1 = _screen_one()
    assert "useCredentialConfig(" in screen1
    assert "credentialCfg.countries.map" in screen1
    # No literal country list of its own.
    assert "country_name:" not in screen1
    assert "United Kingdom" not in screen1


def test_screen_one_asks_only_a_physician_for_a_country():
    """An advisor and a referral partner are never shown the country block, so
    they must not fetch the list that fills it."""
    assert 'useCredentialConfig(isAsclepius && kind === "physician")' in _screen_one()


def test_a_malformed_country_is_treated_as_not_supplied(http, _mail):
    """The 200-that-stored-nothing. `registry_config.normalize_country` is a
    lookup helper: it truncates and never checks it got letters, so 'G' and
    'U1' used to read as a non-US country (which blanked the state) while the
    store rejected them (so no country was written either). The row then read
    as US everywhere downstream, with a success code on it."""
    for bad in ("G", "U1", "1"):
        fresh_store()
        email = f"dr-{uniq()}@nephrology-associates.com"
        token, hs_id = _invite(http, email)
        r = _step1(http, token, email, country_of_licensure=bad, license_state="CA")
        assert r.status_code == 200, f"{bad}: {r.text}"
        row = http.app.state.team_store.get_health_system_by_id(hs_id)
        # Treated exactly as if no country had been sent: the US path.
        assert (row["director_country_of_licensure"] or "") == "", bad
        assert row["director_license_state"] == "CA", (
            f"{bad} silently discarded the state as if it were a real country")


def test_a_country_less_resubmit_cannot_re_pin_a_state_on_a_non_us_row(http, _mail):
    """Screen 1 is re-submittable and the state-clearing CASE has to read the
    country ALREADY ON THE ROW when a call carries none. Reading only the
    incoming value let a later country-less write put 'CA' back on a row stored
    as GB, which is the stale value credentials.py ships as a US licence."""
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, email)

    assert _step1(http, token, email, country_of_licensure="GB").status_code == 200
    # No country key at all, but carrying a state.
    assert _step1(http, token, email, license_state="CA").status_code == 200

    row = http.app.state.team_store.get_health_system_by_id(hs_id)
    assert (row["director_country_of_licensure"] or "") == "GB"
    assert (row["director_license_state"] or "") == "", "a US state was re-pinned on a GB row"


def test_the_credentials_blob_drops_a_us_licence_from_a_non_us_doctor(http, _mail):
    """The form clears these when the country changes, but a CV parse fills
    both without ever looking at the country, so the endpoint cannot trust it.
    This blob is what reaches the Tier B vault and the buyer-facing block."""
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, email)
    assert _step1(http, token, email, country_of_licensure="GB").status_code == 200
    _prove_mailbox(http, hs_id)

    assert http.post("/api/onboarding/asclepius/credentials",
                     json={"token": token, "credentials": _GB_CREDS}).status_code == 200

    saved = _saved_credentials(http, hs_id, email)
    assert not (saved.get("licenseState") or ""), "a GB consultant kept a US state"
    assert not (saved.get("licenseNumber") or "")
    # And the country's own identifier is untouched.
    assert saved.get("registrationNumber") == "1234567"


def test_a_us_doctors_licence_survives_the_same_endpoint(http, _mail):
    """The other half of the rule above: sanitising must not eat the field for
    the physicians it is actually for."""
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"
    token, hs_id = _invite(http, email)
    assert _step1(http, token, email, country_of_licensure="US",
                  license_state="CA").status_code == 200
    _prove_mailbox(http, hs_id)

    us_creds = dict(_GB_CREDS)
    us_creds.update({"countryOfPractice": "US", "countryOfLicensure": "US",
                     "countryOfDegree": "US", "npi": "1234567893", "degree": "MD"})
    assert http.post("/api/onboarding/asclepius/credentials",
                     json={"token": token, "credentials": us_creds}).status_code == 200

    saved = _saved_credentials(http, hs_id, email)
    assert saved.get("licenseState") == "CA"
    assert saved.get("licenseNumber") == "A12345"


def test_screen_one_mirrors_the_country_only_while_the_fields_agree():
    """"Fill only a blank" is the wrong rule for a control somebody can change
    twice. Pick GB, correct a mis-click back to US, and practice and degree
    would stay GB forever while licensure said US — and country_of_practice is
    what renders the physician's card, their community profile and the
    verification queue row."""
    screen1 = _screen_one()
    assert "(c.countryOfPractice || prev) === prev" in screen1
    assert "(c.countryOfDegree || prev) === prev" in screen1
    assert "countryOfPractice: data.credentials.countryOfPractice || next" not in screen1


def test_the_blob_sanitiser_reads_practice_when_licensure_is_blank(http, _mail):
    """The sanitiser and `_run_signup_verification` must agree on who is non-US.

    finish resolves licensure as `countryOfLicensure or countryOfPractice`. A
    narrower predicate here let a blob whose licensure was blank and whose
    practice was GB keep a US licence, while finish treated that same physician
    as British — a US state licence attached to a doctor the system had already
    decided was not American.
    """
    fresh_store()
    email = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, email)
    assert _step1(http, token, email, country_of_licensure="GB").status_code == 200
    _prove_mailbox(http, hs_id)

    creds = dict(_GB_CREDS)
    creds["countryOfLicensure"] = ""          # blank, as an older blob can be
    creds["countryOfPractice"] = "GB"
    assert http.post("/api/onboarding/asclepius/credentials",
                     json={"token": token, "credentials": creds}).status_code == 200

    saved = _saved_credentials(http, hs_id, email)
    assert not (saved.get("licenseState") or ""), "practice country was ignored"
    assert not (saved.get("licenseNumber") or "")


def test_an_invited_member_cannot_store_a_foreign_us_licence(http, _mail):
    """`/member/credentials` is a second door into the same blob, and an
    invited clinician reaches it without ever seeing screen 1."""
    fresh_store()
    director = f"dr-{uniq()}@nhs-trust.example"
    token, hs_id = _invite(http, director)
    assert _step1(http, token, director, country_of_licensure="GB").status_code == 200
    _prove_mailbox(http, hs_id)

    ts = http.app.state.team_store
    member_email = f"reg-{uniq()}@nhs-trust.example"
    # add-member gates on institution details, which the v2 physician path has
    # no screen for, so the director's own flow never sets them.
    assert http.post("/api/onboarding/asclepius/institution",
                     json={"token": token, "org_name": "Northridge",
                           "specialty": "Nephrology",
                           "phone": "5551234"}).status_code == 200
    assert http.post("/api/onboarding/asclepius/add-member",
                     json={"token": token, "full_name": "Rhys Morgan",
                           "email": member_email, "role": "np"}).status_code == 200
    # The emailed token is not returned by the API; mint a usable raw one, as
    # tests/test_asclepius_onboarding.py does.
    member_token = ts.issue_asclepius_member_token(hs_id, member_email)

    r = http.post("/api/onboarding/member/credentials",
                  json={"token": member_token, "credentials": _GB_CREDS})
    assert r.status_code == 200, r.text
    saved = _saved_credentials(http, hs_id, member_email)
    assert not (saved.get("licenseState") or ""), "a member kept a US state under GB"
    assert not (saved.get("licenseNumber") or "")
