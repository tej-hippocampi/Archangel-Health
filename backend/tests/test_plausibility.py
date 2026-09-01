"""Does this signup hold together?

Anchored on a real test account that filled the credential form with keyboard
noise and scored 29 out of 100 — high enough to look like a marginal doctor
rather than a fake one. It scored that because the tier weights only asked
whether a field was non-empty: "n;n" is a truthy string, so it earned the full
twenty points for board certification, and "nlk" earned three for a LinkedIn
profile.

Two things have to stay true at once here, and the second is the harder one:
nonsense must not score, and real doctors must not be called nonsense. Every
check is a flag routed to a human, never a rejection, because the people most
likely to trip a heuristic are the ones whose names, qualifications and
training sit outside whatever we assumed was normal.
"""

from __future__ import annotations

import json

import pytest

from asclepius import credentialing, plausibility


# The credentials blob and user row from the account that scored 29.
_NOISE_CREDENTIALS = {
    "licenseNumber": "kkkl",
    "licenseState": "BL",
    "primarySpecialty": "Nephrolgy",
    "fellowship": {"institution": "jkj", "year": "7689"},
    "residency": {"institution": "lkj", "year": "2910"},
}

_NOISE_USER = {
    "email": "someone@example.com",
    "full_name": "Test Person",
    "email_domain_class": "business",
    "board_cert": "n;n — n;",
    "linkedin_url": "nlk",
    "npi_verified": 0,
    "npi_payload_json": json.dumps({"result": "not_found", "npi": "1929019212"}),
    "credentials_json": json.dumps(_NOISE_CREDENTIALS),
}

_REAL_USER = {
    "email": "j.okafor@stanford.edu",
    "full_name": "Jane Okafor",
    "email_domain_class": "academic",
    "board_cert": "American Board of Internal Medicine — Nephrology",
    "linkedin_url": "https://www.linkedin.com/in/jane-okafor",
    "years_experience": 14,
    "npi_verified": 1,
    "npi_payload_json": json.dumps({
        "result": "verified",
        "record": {"credential": "MD", "taxonomy": {"desc": "Nephrology"}},
    }),
    "credentials_json": json.dumps({
        "licenseNumber": "A94021",
        "licenseState": "CA",
        "primarySpecialty": "Nephrology",
        "residency": {"institution": "UCSF", "year": "2011"},
        "fellowship": {"institution": "Stanford", "year": "2014"},
    }),
}


# ─── The case this was built for ─────────────────────────────────────────────
def test_the_gibberish_signup_scores_far_lower_than_it_used_to():
    """It scored 29 by collecting +20 for "n;n" and +3 for "nlk"."""
    out = credentialing.propose_tier(_NOISE_USER)
    assert out["score"] < 15
    assert out["proposed_tier"] is None


def test_the_gibberish_signup_is_flagged_field_by_field():
    found = plausibility.flags(_NOISE_USER, _NOISE_CREDENTIALS)
    fields = {f["field"] for f in found}
    assert {"board_cert", "license_number", "license_state",
            "residency_year", "fellowship_year"} <= fields
    assert plausibility.should_flag(found)


def test_noise_earns_no_points_and_says_so():
    """An admin reading the reasons should see why, not just a low number."""
    reasons = " ".join(credentialing.propose_tier(_NOISE_USER)["reasons"])
    assert "does not read as one" in reasons
    assert "not a LinkedIn profile URL" in reasons


# ─── ...without calling real doctors liars ───────────────────────────────────
def _problems(user, creds):
    """Findings about what does not HOLD TOGETHER, which is what this module is
    for.

    Onboarding v2 §2 added a second, deliberately different category to
    ``flags()``: LOW notes recording what an application did not bring (no NPI,
    no CV, no certification), because v2 stopped requiring them. Absence is
    pending, never penalized — it does not flag, it does not suppress a tier
    proposal, and it is not an implausibility. So the tests below, which are all
    about implausibility, filter it out rather than asserting a total.
    """
    return [f for f in plausibility.flags(user, creds) if f["issue"] != "not_provided"]


def test_a_real_doctor_still_scores_and_is_not_flagged():
    out = credentialing.propose_tier(_REAL_USER)
    assert out["score"] >= 70
    assert out["blockers"] == []
    assert out["proposed_tier"] == "reviewer"
    assert _problems(_REAL_USER, json.loads(_REAL_USER["credentials_json"])) == []


@pytest.mark.parametrize("licence", [
    "A94021", "MD12345", "G-56789", "0123456", "AB1234", "123456", "A 94021",
])
def test_real_licence_numbers_are_not_noise(licence):
    """A licence number is a code, not a word: running the language heuristic
    over "A94021" flags a real doctor."""
    assert not plausibility.identifier_looks_like_noise(licence)


@pytest.mark.parametrize("value", ["kkkl", "jjj", "nlk", ""])
def test_noise_identifiers_are_caught(value):
    assert plausibility.identifier_looks_like_noise(value)


@pytest.mark.parametrize("name", [
    "Nguyen", "Ng", "Mbeki", "Al Otaibi", "Krishnan", "Björk", "O'Brien",
    "van der Berg", "Wu", "Sokolov",
])
def test_real_surnames_are_never_noise(name):
    """Short and consonant-heavy names are real names. A heuristic that fires
    on "Ng" is a heuristic that insults people."""
    assert not plausibility.looks_like_noise(name, min_len=3)


@pytest.mark.parametrize("board", [
    "American Board of Internal Medicine",
    "MRCP (UK)",
    "Royal College of Physicians",
    "Saudi Board of Internal Medicine",
    "National Board of Examinations — DNB Nephrology",
    "FRCPC",
])
def test_real_board_certifications_are_recognized(board):
    assert plausibility.board_certification_is_recognizable(board)


@pytest.mark.parametrize("board", ["n;n — n;", "kkkl", "x", ";;;", "aaaa"])
def test_noise_board_certifications_are_not(board):
    assert not plausibility.board_certification_is_recognizable(board)


# ─── Years ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("year", ["7689", "2910", "0000", "12", "twenty", ""])
def test_impossible_years_are_rejected(year):
    assert plausibility.plausible_year(year) is None


@pytest.mark.parametrize("year", ["1975", "2003", "2019"])
def test_possible_years_are_accepted(year):
    assert plausibility.plausible_year(year) == int(year)


def test_a_timeline_that_cannot_happen_is_flagged():
    creds = {"degreeYear": "2015", "residency": {"year": "2011"}}
    found = plausibility.flags({}, creds)
    assert any(f["issue"] == "finished_residency_before_qualifying" for f in found)


def test_claiming_more_practice_than_time_since_qualifying_is_flagged():
    creds = {"degreeYear": "2020"}
    found = plausibility.flags({"years_experience": 30}, creds)
    assert any(f["issue"] == "more_practice_than_time_since_qualifying" for f in found)


def test_missing_years_are_not_suspicious():
    """Saying nothing is not evidence of anything."""
    assert _problems({}, {"residency": {}, "fellowship": {}}) == []


# ─── LinkedIn ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/jane-okafor",
    "linkedin.com/in/jokafor",
    "https://uk.linkedin.com/in/j-okafor-1234",
])
def test_real_linkedin_urls_pass(url):
    assert plausibility.linkedin_url_is_wellformed(url)


@pytest.mark.parametrize("url", ["nlk", "https://example.com/in/x", "jane okafor"])
def test_non_linkedin_values_do_not(url):
    assert not plausibility.linkedin_url_is_wellformed(url)


# ─── International ───────────────────────────────────────────────────────────
def test_a_registry_verified_doctor_earns_identity_without_an_npi():
    """An SCFHS or NMC match proves what an NPPES match proves."""
    user = {
        "email": "a.alotaibi@kfshrc.edu.sa",
        "full_name": "Ahmed Al Otaibi",
        "email_domain_class": "academic",
        "board_cert": "Saudi Board of Internal Medicine",
        "years_experience": 11,
        "npi_verified": None,
        "registry_verified": 1,
        "registry_payload_json": json.dumps({"result": "verified", "registry": "SCFHS"}),
        "credentials_json": json.dumps({
            "countryOfLicensure": "SA",
            "registrationNumber": "1234567",
            "primarySpecialty": "Nephrology",
        }),
    }
    out = credentialing.propose_tier(user)
    assert out["score"] >= 70
    assert out["blockers"] == []
    # ...and can reach the senior tier, which used to require an NPI and so
    # capped every international physician at labeler.
    assert out["proposed_tier"] == "reviewer"


def test_identity_pending_document_review_is_not_held_against_them():
    user = {
        "email": "dr@hospital.sa",
        "full_name": "Ahmed Al Otaibi",
        "registry_verified": None,
        "registry_payload_json": json.dumps({"result": "document_only"}),
        "credentials_json": json.dumps({"countryOfLicensure": "SA"}),
    }
    out = credentialing.propose_tier(user)
    assert "pending document review" in " ".join(out["reasons"])
    assert out["blockers"] == []


def test_a_registry_mismatch_forces_review():
    user = {
        "email": "dr@hospital.pk",
        "full_name": "Someone Else",
        "registry_verified": 0,
        "registry_payload_json": json.dumps({
            "result": "mismatch", "reason": "registration_suspended",
        }),
        "credentials_json": json.dumps({"countryOfLicensure": "PK"}),
    }
    out = credentialing.propose_tier(user)
    assert out["proposed_tier"] is None
    assert any("Registry mismatch" in b for b in out["blockers"])


def test_non_us_signups_are_not_judged_against_us_state_codes():
    """"BL" is not a US state, but a doctor licensed in Saudi Arabia has no
    business being asked for one."""
    found = plausibility.flags({}, {"countryOfLicensure": "SA", "licenseState": "BL"})
    assert not any(f["field"] == "license_state" for f in found)


def test_an_unusual_registration_format_is_a_note_not_a_blocker():
    found = plausibility.flags({}, {
        "countryOfLicensure": "PK", "registrationNumber": "nonsense",
    })
    entry = next(f for f in found if f["field"] == "registration_number")
    assert entry["severity"] == plausibility.SEVERITY_MEDIUM


# ─── Residents and fellows ───────────────────────────────────────────────────
def test_a_resident_expecting_to_finish_in_the_future_is_not_flagged():
    """They are doctors who have not finished yet, and the year they expect to
    finish is them answering the question correctly. Flagging it would turn
    every trainee into a suspicious signup."""
    from datetime import datetime, timezone

    future = datetime.now(timezone.utc).year + 3
    found = plausibility.flags({}, {
        "residency": {"institution": "Mass General", "year": str(future)},
        "residencyCompleted": False,
    })
    assert not any(f["field"] == "residency_year" for f in found)


def test_a_fellowship_that_follows_a_future_residency_is_not_flagged():
    from datetime import datetime, timezone

    year = datetime.now(timezone.utc).year
    found = _problems({}, {
        "residency": {"institution": "Mass General", "year": str(year + 2)},
        "fellowship": {"institution": "Stanford", "year": str(year + 5)},
    })
    assert found == []


def test_a_year_far_beyond_any_training_programme_is_still_flagged():
    """"Not finished yet" stretches to about a decade. 7689 is a typo."""
    from datetime import datetime, timezone

    absurd = datetime.now(timezone.utc).year + 40
    found = plausibility.flags({}, {"residency": {"year": str(absurd)}})
    assert any(f["field"] == "residency_year" for f in found)
