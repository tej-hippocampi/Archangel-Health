"""Applicant evidence must survive CV parsing without becoming another field."""
import json

import pytest

from asclepius import credentialing as cv
from asclepius.onboarding_catalog import CURRICULUM
from asclepius.onboarding_specialties import canonical


def parse(text):
    return cv._parse_cv_text("Example Physician, MD\n" + text)


@pytest.mark.parametrize("row", CURRICULUM, ids=lambda row: row[0])
@pytest.mark.parametrize("label", ["Primary specialty", "Current practice"])
def test_all_released_specialties_keep_one_identity(row, label):
    name = row[0]
    parsed = parse(
        "Previous training\nInternal Medicine internship, 2001-2002\n"
        f"{label}: {name}\nReferences\nReference Physician\n"
        "Board Certified, Nephrology - American Board of Internal Medicine")
    assert parsed["specialty"] == canonical(name)
    assert canonical(parsed["specialty_display"]) == parsed["specialty"]
    assert parsed["specialty_status"] == "resolved"
    assert parsed["specialty_source"] == ("declared" if label == "Primary specialty" else "current_role")
    assert parsed["board_certifications_structured"] == []


@pytest.mark.parametrize("value,expected", [
    ("Pediatric Nephrology", "pediatric nephrology"),
    ("Paediatric Cardiology", "pediatric cardiology"),
    ("Radiation Oncology", "radiation oncology"),
    ("Surgical Oncology", "surgical oncology"),
    ("Interventional Radiology", "interventional radiology"),
])
def test_nested_identity_is_preserved_in_declaration_and_board(value, expected):
    for text in (f"Primary specialty: {value}", f"Board certified in {value}"):
        result = parse(text)
        assert result["specialty"] == expected
        assert canonical(result["specialty_display"]) == expected


@pytest.mark.parametrize("text", [
    "Specialties: Dermatology and Nephrology",
    "Primary specialty: Dermatology\nPrimary specialty: Nephrology",
    "Current practice: Dermatology\nCurrent practice: Nephrology",
    "Dermatologist\nNephrologist",
    "Board certified in Dermatology\nBoard certified in Nephrology",
])
def test_equally_strong_conflicting_fields_require_review(text):
    result = parse(text)
    assert result["specialty"] is None
    assert result["specialty_display"] == ""
    assert result["specialty_status"] == "ambiguous"
    assert {candidate["specialty"] for candidate in result["specialty_candidates"]} == {"dermatology", "nephrology"}


@pytest.mark.parametrize("heading", ["References", "Professional References", "Referees", "Professional Referees", "Publications", "Selected Publications", "Peer-reviewed Publications", "Research and Publications", "Presentations"])
def test_third_party_sections_cannot_supply_specialty_or_boards(heading):
    result = parse(
        f"Attending Dermatologist\n{heading}\nSpecialty: Nephrology\n"
        "Board Certified, Nephrology - American Board of Internal Medicine (ABIM)")
    assert result["specialty"] == "dermatology"
    assert result["board_certifications_structured"] == []
    assert result["specialty_candidates"] == [{"specialty": "dermatology", "source": "role"}]


@pytest.mark.parametrize("heading", ["Referees", "Professional Referees", "Research and Publications"])
def test_reference_consultant_does_not_outrank_applicant_board(heading):
    result = parse(f"Board certified in Dermatology\n{heading}\nConsultant Nephrologist")
    assert result["specialty"] == "dermatology"
    assert result["specialty_source"] == "board"


def test_recognized_applicant_section_reenters_after_publications():
    result = parse(
        "Publications\nNephrology outcomes 2020; doi:10.0000/example\n"
        "Professional Experience\nExample Clinic, 2021-Present\nAttending Dermatologist\n"
        "Board Certification\nBoard certified in Dermatology (ABD)")
    assert result["specialty"] == "dermatology"
    assert result["specialty_source"] == "current_role"
    assert result["board_certifications_structured"][0]["specialty"] == "Dermatology"


@pytest.mark.parametrize("claim", [
    "Not board certified in Nephrology",
    "Board eligible in Nephrology; exam pending",
    "Aspiring nephrologist",
    "Interested in nephrology",
    "Seeking board certification in Nephrology (ABIM)",
    "Plans to become board certified in Nephrology",
    "No current practice in nephrology",
])
def test_negated_or_aspirational_claims_do_not_override_current_role(claim):
    result = parse(claim + "\nCurrently working as a dermatologist")
    assert result["specialty"] == "dermatology"
    assert result["board_certifications_structured"] == []


def test_current_role_outranks_historical_training_and_broad_board():
    result = parse(
        "Education and training\nInternship, Internal Medicine, 2001-2002\n"
        "Residency, Dermatology, 2002-2005\nBoard certified in Internal Medicine (ABIM)\n"
        "Professional Experience\nExample Hospital, 2005-Present\nAttending Dermatologist")
    assert result["specialty"] == "dermatology"
    assert result["specialty_source"] == "current_role"


def test_current_role_outranks_old_dated_post_and_retired_title():
    result = parse(
        "Former nephrologist\nProfessional Experience\n"
        "Example Hospital, 2000-2005\nAttending Nephrologist\n"
        "Example Clinic, 2010-Present\nAttending Dermatologist")
    assert result["specialty"] == "dermatology"
    assert {c["specialty"] for c in result["specialty_candidates"]} == {"dermatology"}


@pytest.mark.parametrize("dates_first", [True, False], ids=["date-before-title", "title-before-date"])
@pytest.mark.parametrize("separator", ["\n", "\n\n"], ids=["continuous", "paragraphs"])
@pytest.mark.parametrize("historical_first", [True, False], ids=["chronological", "reverse-chronological"])
def test_date_blocks_bind_to_their_own_role(dates_first, separator, historical_first):
    entries = [("Attending Dermatologist", "Example Clinic, 2020-Present"),
               ("Attending Nephrologist", "Example Hospital, 2000-2010")]
    if historical_first:
        entries.reverse()
    blocks = ["\n".join((dates, title) if dates_first else (title, dates))
              for title, dates in entries]
    result = parse("Professional Experience\n" + separator.join(blocks))
    assert result["specialty"] == "dermatology"
    assert result["specialty_source"] == "current_role"
    assert result["specialty_candidates"] == [{"specialty": "dermatology", "source": "current_role"}]


def test_mixed_entry_layout_without_boundaries_does_not_guess_current_role():
    result = parse(
        "Professional Experience\nExample Clinic, 2020-Present\n"
        "Attending Dermatologist\nAttending Nephrologist\nExample Hospital, 2000-2010")
    assert result["specialty"] is None
    assert result["specialty_status"] == "ambiguous"
    assert {c["source"] for c in result["specialty_candidates"]} == {"role"}


def test_one_date_cannot_choose_between_two_adjacent_roles():
    result = parse(
        "Professional Experience\nAttending Dermatologist\n"
        "Consultant Nephrologist\nExample Clinic, 2020-Present")
    assert result["specialty"] is None
    assert result["specialty_status"] == "ambiguous"
    assert {c["source"] for c in result["specialty_candidates"]} == {"role"}


def test_inline_role_dates_cannot_be_reused_by_following_role():
    result = parse(
        "Professional Experience\nAttending Dermatologist, 2020-Present\n"
        "Attending Nephrologist\nExample Hospital, 2000-2010")
    assert result["specialty"] == "dermatology"
    assert result["specialty_candidates"] == [{"specialty": "dermatology", "source": "current_role"}]


def test_current_fellowship_outranks_prior_general_board():
    result = parse(
        "Board certified in Internal Medicine (ABIM)\nPostgraduate Training\n"
        "Example Hospital, 2025-Present\nFellow, Nephrology")
    assert result["specialty"] == "nephrology"
    assert result["specialty_source"] == "training"


def test_bullet_prefix_does_not_hide_explicit_specialty():
    result = parse("• Primary specialty: Dermatology\nBoard certified in Internal Medicine (ABIM)")
    assert result["specialty"] == "dermatology"
    assert result["specialty_source"] == "declared"


def test_explicit_declaration_beyond_2000_characters_wins():
    result = parse(
        "Internship, Internal Medicine, 2001-2002\n"
        + "Administrative details without clinical field.\n" * 60
        + "Primary specialty: Dermatology")
    assert result["specialty"] == "dermatology"
    assert result["specialty_display"] == "Dermatology"
    assert result["specialty_source"] == "declared"


@pytest.mark.parametrize("declaration", ["Surgery", "Unrecognized Clinical Field"])
def test_unresolved_explicit_declaration_blocks_historical_fallback(declaration):
    result = parse(f"Primary specialty: {declaration}\nInternship, Internal Medicine, 2001-2002")
    assert result["specialty"] is None
    assert result["specialty_display"] == ""
    assert result["specialty_status"] == "missing"


@pytest.mark.parametrize("current", ["Current specialty: Addiction Medicine", "Current practice: Medical Microbiology"])
def test_unknown_current_field_blocks_historical_training_and_boards(current):
    result = parse(current + "\nInternal Medicine residency, 2001-2004\nBoard certified in Internal Medicine (ABIM)")
    assert result["specialty"] is None
    assert result["specialty_display"] == ""
    assert result["specialty_status"] == "missing"
    assert result["specialty_source"] == "current_role"


@pytest.mark.parametrize("first,second", [
    ("Primary specialty: Dermatology", "Primary specialty: Surgery"),
    ("Primary specialty: Surgery", "Primary specialty: Dermatology"),
    ("Current practice: Dermatology", "Current specialty: Addiction Medicine"),
])
def test_unknown_equally_strong_declaration_requires_selection(first, second):
    result = parse(first + "\n" + second)
    assert result["specialty"] is None
    assert result["specialty_display"] == ""
    assert result["specialty_status"] == "ambiguous"


@pytest.mark.parametrize("label", ["Specialties", "Current practice"])
@pytest.mark.parametrize("value", [
    "Dermatology and Tropical Medicine",
    "Tropical Medicine / Dermatology",
    "Dermatology; Tropical Medicine",
    "Dermatology, Tropical Medicine",
    "Dermatology & Tropical Medicine",
    "Dermatologist and microbiologist",
    "Pediatric Nephrology / Tropical Medicine",
])
def test_supported_member_cannot_hide_unsupported_specialty_in_same_list(label, value):
    result = parse(f"{label}: {value}\nBoard certified in Internal Medicine (ABIM)")
    assert result["specialty"] is None
    assert result["specialty_display"] == ""
    assert result["specialty_status"] == "ambiguous"


@pytest.mark.parametrize("value,expected", [
    ("Allergy & Immunology", "allergy and immunology"),
    ("Allergy and Immunology", "allergy and immunology"),
    ("Ob/Gyn", "obstetrics and gynecology"),
    ("Physical Medicine & Rehabilitation", "physical medicine and rehabilitation"),
    ("Hematology/Oncology", "oncology"),
    ("Pulmonary and Critical Care", "pulmonary and critical care"),
    ("Nephrology / Renal Medicine", "nephrology"),
    ("Pediatric Nephrology / Paediatric Nephrology", "pediatric nephrology"),
])
def test_recognized_compounds_and_duplicate_aliases_are_complete_list_members(value, expected):
    result = parse(f"Primary specialty: {value}")
    assert result["specialty"] == expected
    assert canonical(result["specialty_display"]) == expected
    assert result["specialty_status"] == "resolved"


def test_recognized_compound_plus_unknown_field_requires_selection():
    result = parse("Specialties: Allergy & Immunology and Tropical Medicine")
    assert result["specialty"] is None
    assert result["specialty_status"] == "ambiguous"


def test_distinct_adult_and_pediatric_list_members_remain_ambiguous():
    result = parse("Specialties: Nephrology / Pediatric Nephrology")
    assert result["specialty"] is None
    assert result["specialty_status"] == "ambiguous"
    assert {c["specialty"] for c in result["specialty_candidates"]} == {"nephrology", "pediatric nephrology"}


def test_supported_primary_declaration_still_outranks_unknown_current_role():
    result = parse("Current role: Medical Microbiology\nPrimary specialty: Dermatology")
    assert result["specialty"] == "dermatology"
    assert result["specialty_source"] == "declared"


@pytest.mark.parametrize("text", ["", "Attending Dcrmatologist", "Publications\nNephrology outcomes, 2022"])
def test_missing_or_unrecognized_evidence_never_defaults_to_nephrology(text):
    result = parse(text)
    assert result["specialty"] is None
    assert result["specialty_status"] == "missing"


def test_provenance_contains_vocabulary_values_only():
    result = parse("Current practice: Dermatology\nBoard certified in Internal Medicine (ABIM)")
    metadata = {key: value for key, value in result.items()
                if key in {"specialty_source", "specialty_status", "specialty_candidates"}}
    assert "Example Physician" not in json.dumps(metadata)
    assert "Current practice" not in json.dumps(metadata)
    assert all(set(candidate) == {"specialty", "source"} for candidate in metadata["specialty_candidates"])


def test_unreadable_cv_returns_complete_missing_evidence(monkeypatch):
    from asclepius import assets
    monkeypatch.setattr(assets, "load_asset", lambda sha: (b"", {}))
    result = cv.parse_cv("synthetic-cv", mime="text/plain")
    assert result["ok"] is False
    assert result["specialty"] is None
    assert result["specialty_status"] == "missing"
    assert result["specialty_candidates"] == []
