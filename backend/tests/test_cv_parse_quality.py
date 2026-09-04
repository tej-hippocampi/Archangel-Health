"""The CV parse is held against a corpus, not against one sample.

The founders uploaded a real nephrology CV during a walkthrough. The Review
screen came back showing three board certifications:

    "Nephrologist"        the job title under the physician's name
    "nephrologist with"   a summary sentence, truncated at the first digit
    "ABIM"                the issuing board, listed as a third certification

while the two real certifications, the fellowship, the residency and both state
licences were left blank. A physician was looking at a form that had invented
three of their credentials and missed five.

One sample cannot hold a fix for that, so every fixture in
``tests/fixtures/cvs`` is checked field by field, and the two rules that govern
the module are the two rules asserted here:

  * a WRONG value is worse than a missing one;
  * nothing is filled that the source text does not contain.

The second one is checked mechanically at the bottom, against every fixture at
once, which is the assertion most likely to catch the next regression: it does
not need anyone to have predicted what the wrong answer would be.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from asclepius import credentialing as cred
from tests.fixtures.cvs import CV_FIXTURES

_BY_NAME = {f["name"]: f for f in CV_FIXTURES}


def _parse(name: str):
    return cred._parse_cv_text(_BY_NAME[name]["text"])


@pytest.mark.parametrize("fixture", CV_FIXTURES, ids=lambda f: f["name"])
def test_every_fixture_parses_to_what_the_cv_says(fixture):
    parsed = cred._parse_cv_text(fixture["text"])
    expect = fixture["expect"]

    for key in ("full_name", "specialty", "specialty_display", "npi",
                "linkedin_url", "years_in_practice", "employer", "degrees"):
        if key in expect:
            assert parsed.get(key) == expect[key], (
                f"{fixture['name']}: {key} is {parsed.get(key)!r}, "
                f"expected {expect[key]!r}")

    if "board_certifications_structured" in expect:
        got = [{"board": c["board"], "specialty": c["specialty"]}
               for c in parsed["board_certifications_structured"]]
        assert got == expect["board_certifications_structured"], fixture["name"]

    if "licenses" in expect:
        want = expect["licenses"]
        got = parsed["licenses"]
        assert len(got) == len(want), f"{fixture['name']}: {got}"
        for g, w in zip(got, want):
            for key, value in w.items():
                assert g[key] == value, f"{fixture['name']}: {g}"

    if "training" in expect:
        want = expect["training"]
        got = parsed["training"]
        assert len(got) == len(want), f"{fixture['name']}: {got}"
        for g, w in zip(got, want):
            for key, value in w.items():
                assert g[key] == value, f"{fixture['name']}: {g}"


def test_the_three_certifications_the_old_parser_invented_are_gone():
    """Named individually, so a regression reads as the bug it is."""
    parsed = _parse("us_md_nephrologist")
    labels = [c["specialty"] for c in parsed["board_certifications_structured"]]
    for junk in _BY_NAME["us_md_nephrologist"]["expect"]["board_labels_must_not_contain"]:
        assert junk not in labels, f"the parser is still emitting {junk!r}"
    assert len(parsed["board_certifications_structured"]) == 2


def test_a_certification_is_never_active_from_a_document():
    """"Currently valid" is a claim about TODAY.

    A CV written last year cannot make it, and the checkbox is one a physician
    signs for. Filling it in for them is putting words in their mouth on a
    compliance field.
    """
    for fixture in CV_FIXTURES:
        for cert in cred._parse_cv_text(fixture["text"])["board_certifications_structured"]:
            assert cert["active"] is None, fixture["name"]


def test_nothing_is_filled_that_the_cv_does_not_say():
    """The general rule, mechanically.

    Every extracted string has to appear in the source text, case-insensitively
    and ignoring whitespace. This is the assertion that catches the next
    regression, because it does not require anyone to have guessed in advance
    what the wrong answer would be.
    """
    def _norm(value: str) -> str:
        return "".join(value.split()).lower()

    for fixture in CV_FIXTURES:
        haystack = _norm(fixture["text"])
        parsed = cred._parse_cv_text(fixture["text"])
        checked = []
        checked += [c["institution"] for c in parsed["training"]]
        checked += [lic["number"] for lic in parsed["licenses"]]
        checked += [parsed.get("employer") or ""]
        checked += [parsed.get("full_name") or ""]
        checked += [parsed.get("npi") or ""]
        for cert in parsed["board_certifications_structured"]:
            checked.append(cert["specialty"])
        for value in checked:
            if not value:
                continue
            assert _norm(value) in haystack, (
                f"{fixture['name']}: the parse produced {value!r}, which is not "
                f"in the CV")


def test_a_state_licence_is_only_read_off_a_labelled_line():
    """A bare alphanumeric on a CV is a pager, an office suite or a DEA number
    far more often than it is a licence number."""
    parsed = cred._parse_cv_text(
        "Samuel Adeyemi, MD\nSuite 4400\nOffice A-88213\nPager 555-0199\n")
    assert parsed["licenses"] == []


def test_a_current_employer_is_never_a_job_they_left():
    parsed = _parse("us_md_nephrologist")
    assert parsed["employer"] == "Lakeshore Nephrology & Hypertension Associates"
    assert "Summit" not in (parsed["employer"] or "")


def test_the_failure_shape_has_every_key_the_success_shape_has():
    """The Review screen indexes these directly, and this has drifted before."""
    full = set(cred._parse_cv_text(_BY_NAME["us_md_nephrologist"]["text"]))
    empty = set(cred._empty_parse("sha", "unreadable"))
    assert full - empty == set(), f"missing from the failure shape: {full - empty}"


# ── The file types ──────────────────────────────────────────────────────────

def _docx(lines):
    """A real .docx, built with stdlib, so the test exercises the real reader.

    The text is XML-escaped, which is not incidental: the first version of this
    helper wrote "Lakeshore Nephrology & Hypertension Associates" raw, the bare
    ampersand made word/document.xml invalid, and the reader correctly returned
    nothing. Word escapes it; so must a fixture claiming to be a Word file.
    """
    from xml.sax.saxutils import escape

    body = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>"
                   for line in lines)
    doc = ('<?xml version="1.0"?><w:document '
           'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


def test_a_word_document_is_accepted_and_read():
    """The single most common thing a physician attaches, and it used to be
    refused with a message that did not say what to attach instead."""
    lines = _BY_NAME["us_md_nephrologist"]["text"].strip().splitlines()
    blob = _docx([ln for ln in lines if ln.strip()])
    mime = cred.sniff_cv_mime(blob)
    assert mime and mime.endswith("wordprocessingml.document")
    parsed = cred._parse_cv_text(cred.extract_cv_text(blob, mime))
    assert len(parsed["board_certifications_structured"]) == 2
    assert parsed["employer"] == "Lakeshore Nephrology & Hypertension Associates"


def test_a_zip_that_is_not_a_word_document_is_still_refused():
    """.docx, .xlsx, .jar, .epub and a plain .zip all start PK\\x03\\x04.

    Accepting on the magic bytes would accept all of them, and the stored blob
    is served inline from our own origin to an admin whose bearer token is in
    localStorage.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.txt", "not a cv")
    assert cred.sniff_cv_mime(buf.getvalue()) is None


def test_rtf_is_accepted_and_read():
    blob = (rb"{\rtf1\ansi Rachel A. Kessler, MD\par "
            rb"Board Certified, Nephrology - American Board of Internal Medicine (ABIM)\par}")
    assert cred.sniff_cv_mime(blob) == "application/rtf"
    parsed = cred._parse_cv_text(cred.extract_cv_text(blob, "application/rtf"))
    assert parsed["board_certifications_structured"] == [
        {"board": "ABIM", "specialty": "Nephrology", "subspecialty": "", "active": None}]


def test_a_photo_of_a_cv_is_accepted():
    """OCR may or may not be installed. What must hold either way is that the
    upload is not REFUSED: a physician whose only copy is a phone photo was
    being turned away at the door."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert cred.sniff_cv_mime(png) == "image/png"


@pytest.mark.parametrize("blob", [
    b"<html><script>alert(1)</script></html>",
    b"<svg onload=alert(1)>",
    b"<?xml version='1.0'?><x/>",
    b"MZ\x90\x00",
    b"\x7fELF",
])
def test_markup_and_executables_are_still_refused(blob):
    """Widening the accepted set must not widen THIS. The stored CV is served
    inline from the app origin to an admin whose bearer token lives in
    localStorage, which is what turns an accepted upload into stored XSS."""
    assert cred.sniff_cv_mime(blob) is None


def test_the_rejection_message_says_what_to_attach_instead():
    with pytest.raises(cred.CvUploadError) as exc:
        cred.store_cv(b"\x7fELF\x02\x01", "application/octet-stream")
    assert "PDF" in str(exc.value) and "Word" in str(exc.value)


# ── The other half: a good parse must reach the form ────────────────────────
#
# A correct parse is worth nothing if the wizard drops it, and that is exactly
# what happened to `training`. The server has extracted fellowship and residency
# since v2 and `applyCvParse` never mapped either, so a physician whose CV
# plainly listed both retyped both while the data sat in the response.
#
# Asserted on the shipped source, in the style the rest of this suite uses for
# the landing app: there is no JS runner here, and the property is "which parse
# key reaches which form field", which the source answers.

from pathlib import Path  # noqa: E402

_WIZARD = (Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
           / "components" / "OnboardingWizard.tsx").read_text(encoding="utf-8")
_APPLY = _WIZARD[_WIZARD.index("function applyCvParse"):
                 _WIZARD.index("export default function OnboardingWizard")]


@pytest.mark.parametrize("parse_key,form_field", [
    ("specialty_display", "primarySpecialty"),
    ("employer", "healthSystem"),
    ("board_certifications_structured", "boardCertifications"),
    ("training", "fellowship"),
    ("training", "residency"),
    ("licenses", "licenseNumber"),
    ("licenses", "licenseState"),
])
def test_every_extracted_field_reaches_the_form(parse_key, form_field):
    assert parse_key in _APPLY, f"applyCvParse never reads parsed.{parse_key}"
    assert form_field in _APPLY, f"applyCvParse never writes {form_field}"


def test_the_form_prefills_the_display_specialty_not_the_registry_key():
    """"nephrology" in a box on a form asking a physician to vouch for their
    own credentials reads as carelessness, and it is a correction they should
    not have to make."""
    assert 'fill("primarySpecialty", parsed.specialty_display' in _APPLY


def test_a_certification_never_arrives_pre_ticked_as_active():
    """The compliance field a physician signs for. A document written last year
    cannot answer a question about today."""
    assert "active: true" not in _APPLY
    assert "active: false" in _APPLY


def test_the_autofill_still_never_overwrites_the_physician():
    """The oldest rule in this function, and the one most easily lost while
    adding fields to it: a value they typed is theirs."""
    assert 'if ((current[key] ?? "").toString().trim()) return;' in _APPLY
    for guard in ("certsUntouched", "fellowshipUntouched", "residencyUntouched"):
        assert guard in _APPLY, f"{guard} is missing; a resume would overwrite"
