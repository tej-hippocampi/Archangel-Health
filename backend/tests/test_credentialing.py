"""PRD B Phase 1 — NPI verification (NPPES) + email domain classification.

The load-bearing property under test: every check has THREE outcomes. A
definitive negative (NOT_FOUND / MISMATCH) is distinct from "could not check"
(UNAVAILABLE), and UNAVAILABLE never collapses into a rejection path.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from tests._asclepius import fresh_store

from asclepius import credentialing
from asclepius.credentialing import (
    NpiResult,
    classify_email_domain,
    clean_npi,
    family_names_match,
    fetch_npi_record,
    is_health_system_domain,
    npi_checksum_ok,
    verify_npi,
)

# 1234567893 is the canonical Luhn-valid NPI test number.
VALID_NPI = "1234567893"
BAD_CHECKSUM_NPI = "1234567890"


def _nppes_record(
    last_name="Smith",
    first_name="Jane",
    status="A",
    enumeration_type="NPI-1",
    taxonomy_desc="Internal Medicine",
    other_names=(),
):
    return {
        "number": VALID_NPI,
        "enumeration_type": enumeration_type,
        "basic": {
            "first_name": first_name,
            "last_name": last_name,
            "credential": "M.D.",
            "status": status,
            "enumeration_date": "2008-05-23",
        },
        "taxonomies": [
            {"code": "207R00000X", "desc": taxonomy_desc, "primary": True,
             "state": "MA", "license": "123456"}
        ],
        "addresses": [
            {"address_purpose": "LOCATION", "city": "Boston", "state": "MA"}
        ],
        "other_names": [{"last_name": n} for n in other_names],
    }


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def _network_forbidden(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("network call was made but must not be")
    monkeypatch.setattr(credentialing.httpx, "get", _boom)


# ─── Checksum / format ────────────────────────────────────────────────────────
def test_checksum_accepts_valid_npi():
    assert npi_checksum_ok(VALID_NPI)


@pytest.mark.parametrize("bad", [BAD_CHECKSUM_NPI, "123456789", "12345678901",
                                 "abcdefghij", "", "12345 6789"])
def test_checksum_rejects_malformed(bad):
    assert not npi_checksum_ok(bad)


def test_clean_npi_strips_paste_noise():
    assert clean_npi(" 123-456.7893 ") == VALID_NPI


def test_bad_checksum_is_not_found_with_no_network_call(monkeypatch):
    _network_forbidden(monkeypatch)
    out = verify_npi(BAD_CHECKSUM_NPI, "Smith")
    assert out["result"] == NpiResult.NOT_FOUND.value
    assert out["reason"] == "invalid_format_or_checksum"


# ─── UNAVAILABLE is a distinct state ─────────────────────────────────────────
def test_timeout_is_unavailable_never_not_found(monkeypatch):
    def _timeout(*a, **kw):
        raise httpx.TimeoutException("nppes slow")
    monkeypatch.setattr(credentialing.httpx, "get", _timeout)
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.UNAVAILABLE.value
    assert out["result"] != NpiResult.NOT_FOUND.value
    assert "network_error" in out["reason"]


@pytest.mark.parametrize("code,reason", [(429, "rate_limited"), (500, "http_500"),
                                         (503, "http_503")])
def test_rate_limit_and_5xx_are_unavailable(monkeypatch, code, reason):
    monkeypatch.setattr(credentialing.httpx, "get",
                        lambda *a, **kw: _FakeResponse(status_code=code))
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.UNAVAILABLE.value
    assert out["reason"] == reason


def test_result_count_absent_is_unavailable_not_not_found(monkeypatch):
    """B-1.2 — the test that would have caught it.

    ``result_count`` ABSENT is not ``result_count`` ZERO. Any 200 we cannot
    recognize (schema change, Cloudflare JSON error page, captive portal) must
    be 'could not determine'. The difference is permanent: not_found stamps
    npi_checked_at, which makes the row cache-eligible for 30 days AND
    suppresses the retry path.
    """
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={"message": "unexpected shape"}),
    )
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.UNAVAILABLE.value
    assert out["result"] != NpiResult.NOT_FOUND.value
    assert out["reason"] == "unrecognized_payload"


def test_unrecognized_200_is_not_cached_and_leaves_retry_open(monkeypatch):
    """The consequence half of B-1.2: an unrecognized 200 must not become a
    30-day cached rejection, and must not suppress the retry."""
    store = fresh_store()
    u = _user_with_npi(store)
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={"nonsense": True}),
    )
    store.set_npi_result(u["id"], verify_npi(VALID_NPI, "Smith"))
    row = store.get_user_by_id(u["id"])
    assert row["npi_verified"] is None          # not 0 — never a definitive negative
    assert row["npi_checked_at"] is None        # cache-ineligible
    assert store.get_cached_npi_fetch(VALID_NPI) is None


@pytest.mark.parametrize("payload", [
    {"results": [], "result_count": 1},          # IndexError before the fix
    {"result_count": 1},                         # KeyError before the fix
    {"results": "x", "result_count": 1},         # ValueError before the fix
    {"results": [None], "result_count": 1},
    {"result_count": None},
    [],                                          # not even an object
])
def test_malformed_200_never_raises_and_is_unavailable(monkeypatch, payload):
    """Three of these raised today, violating the documented three-state
    contract. Both current callers wrap in try/except, so there is no 500 —
    until the next caller trusts the contract."""
    monkeypatch.setattr(credentialing.httpx, "get",
                        lambda *a, **kw: _FakeResponse(payload=payload))
    out = verify_npi(VALID_NPI, "Smith")   # must not raise
    assert out["result"] in (NpiResult.UNAVAILABLE.value, NpiResult.NOT_FOUND.value)
    if payload == {"result_count": None}:
        assert out["result"] == NpiResult.NOT_FOUND.value   # count present and falsy
    else:
        assert out["result"] == NpiResult.UNAVAILABLE.value


def test_result_count_zero_is_still_definitively_not_found(monkeypatch):
    """Do not over-correct: a well-formed 'no such NPI' must stay definitive."""
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={"result_count": 0, "results": []}),
    )
    assert verify_npi(VALID_NPI, "Smith")["result"] == NpiResult.NOT_FOUND.value


def test_timeout_budget_is_per_phase(monkeypatch):
    """B-1.1 tail: a bare timeout=6.0 applies 6s to each of connect/read/write/
    pool (~20s worst case for one hung request)."""
    seen = {}

    def _capture(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        return _FakeResponse(payload={"result_count": 0, "results": []})

    monkeypatch.setattr(credentialing.httpx, "get", _capture)
    verify_npi(VALID_NPI, "Smith")
    t = seen["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.connect == 2.0 and t.read == 4.0 and t.write == 2.0 and t.pool == 1.0


def test_api_error_object_is_unavailable_not_not_found(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={"Errors": [{"description": "x"}]}),
    )
    assert fetch_npi_record(VALID_NPI)["status"] == "unavailable"


# ─── Definitive outcomes ─────────────────────────────────────────────────────
def test_zero_results_is_not_found(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={"result_count": 0, "results": []}),
    )
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.NOT_FOUND.value


def test_active_individual_with_matching_family_name_is_verified(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(
            payload={"result_count": 1, "results": [_nppes_record()]}),
    )
    out = verify_npi(VALID_NPI, "smith")
    assert out["result"] == NpiResult.VERIFIED.value
    assert out["record"]["taxonomy"]["desc"] == "Internal Medicine"
    assert out["record"]["last_name"] == "Smith"


def test_family_name_mismatch_is_mismatch_not_rejection_state(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(
            payload={"result_count": 1, "results": [_nppes_record(last_name="Jones")]}),
    )
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.MISMATCH.value
    assert out["reason"] == "family_name_mismatch"
    # the registry record is preserved so the admin can see what WAS found
    assert out["record"]["last_name"] == "Jones"


def test_organizational_npi_is_mismatch(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(
            payload={"result_count": 1,
                     "results": [_nppes_record(enumeration_type="NPI-2")]}),
    )
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.MISMATCH.value
    assert out["reason"] == "organizational_npi"


def test_deactivated_npi_is_mismatch(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(
            payload={"result_count": 1, "results": [_nppes_record(status="D")]}),
    )
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.MISMATCH.value
    assert out["reason"] == "npi_not_active"


# ─── Name normalization ──────────────────────────────────────────────────────
@pytest.mark.parametrize("claimed,registry,expected", [
    ("O'Brien-Smith Jr.", "OBRIEN SMITH", True),
    ("smith", "Smith, MD", True),
    ("De La Cruz", "DELACRUZ", True),
    ("Smith", "Jones", False),
    ("", "Smith", False),
    ("Smith", "", False),
])
def test_family_name_normalization(claimed, registry, expected):
    assert family_names_match(claimed, registry) is expected


# ─── B-5.2: real physicians must not be false-flagged ───────────────────────
# The measured table from the audit. Every row here MISMATCHED before the fix:
# NPPES stores names ASCII-folded, so accented surnames mismatched by
# construction, and the legal-name field is free text.
@pytest.mark.parametrize("legal_name,registry_last,should_match", [
    ("Ana Muñoz",          "MUNOZ",     True),   # was MISMATCH ("mu oz")
    ("José García",        "GARCIA",    True),   # was MISMATCH ("garc a")
    ("Jane Doe, MD",       "DOE",       True),   # family read as "MD" -> ''
    ("John Smith Jr",      "SMITH",     True),   # family read as "Jr"  -> ''
    ("Minh Do",            "DO",        True),   # "do" stripped as Doctor of Osteopathy
    ("Søren Kierkegaard",  "KIERKEGAARD", True),
    ("Renée Dubois-Blanc", "DUBOIS BLANC", True),
    ("Dr. Tej Patel",      "PATEL",     True),
    ("Ana Muñoz",          "JONES",     False),  # a real mismatch still mismatches
    ("Jane Doe, MD",       "SMITH",     False),
])
def test_family_name_matching_against_ascii_folded_registry(
        legal_name, registry_last, should_match):
    from asclepius.credentialing import family_name_from_legal_name
    family = family_name_from_legal_name(legal_name)
    assert family_names_match(family, registry_last) is should_match, (
        f"{legal_name!r} -> family {family!r} vs registry {registry_last!r}")


@pytest.mark.parametrize("legal_name,expected", [
    ("Jane Doe, MD", "Doe"),
    ("John Smith Jr", "Smith"),
    ("Dr. Tej Patel", "Patel"),
    ("Ana Muñoz", "Muñoz"),
    ("Minh Do", "Do"),
    ("Doe, Jane", "Doe"),
    ("Cher", "Cher"),
    ("", ""),
])
def test_family_name_extraction(legal_name, expected):
    from asclepius.credentialing import family_name_from_legal_name
    assert family_name_from_legal_name(legal_name) == expected


def test_accented_signup_verifies_end_to_end(monkeypatch):
    """The consequence: before the fix this physician got a blocker, no tier
    proposal, and forced manual review — at 50 signups a day that buries the
    queue in false flags."""
    from asclepius.credentialing import family_name_from_legal_name
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={
            "result_count": 1,
            "results": [_nppes_record(last_name="MUNOZ", first_name="ANA")]}),
    )
    out = verify_npi(VALID_NPI, family_name_from_legal_name("Ana Muñoz, MD"))
    assert out["result"] == NpiResult.VERIFIED.value


def test_other_names_are_checked(monkeypatch):
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(
            payload={"result_count": 1,
                     "results": [_nppes_record(last_name="Garcia",
                                               other_names=("Garcia-Lopez",))]}),
    )
    out = verify_npi(VALID_NPI, "Garcia Lopez")
    assert out["result"] == NpiResult.VERIFIED.value


# ─── Cache ───────────────────────────────────────────────────────────────────
def test_cache_hit_skips_the_network(monkeypatch):
    _network_forbidden(monkeypatch)
    cached = {"status": "found", "record": _nppes_record(), "reason": "cached"}
    out = verify_npi(VALID_NPI, "Smith", cached=cached)
    assert out["result"] == NpiResult.VERIFIED.value
    assert out["from_cache"] is True


def test_cached_record_keeps_aliases(monkeypatch):
    """L3 — the trimmed record IS what the cache serves. Dropping other_names
    made a cached record mismatch a physician whose alias a fresh fetch would
    have matched: the same NPI answering differently by cache state."""
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={
            "result_count": 1,
            "results": [_nppes_record(last_name="Garcia", other_names=("Garcia-Lopez",))]}),
    )
    fresh = verify_npi(VALID_NPI, "Garcia Lopez")
    assert fresh["result"] == NpiResult.VERIFIED.value
    # replay the persisted record as the cache would
    replayed = verify_npi(VALID_NPI, "Garcia Lopez",
                          cached={"status": "found", "record": fresh["record"]})
    assert replayed["result"] == NpiResult.VERIFIED.value


def test_registry_record_without_a_name_has_its_own_reason(monkeypatch):
    """L2 — 'nothing to compare' and 'the names differ' are different facts."""
    rec = _nppes_record()
    rec["basic"].pop("last_name")
    monkeypatch.setattr(
        credentialing.httpx, "get",
        lambda *a, **kw: _FakeResponse(payload={"result_count": 1, "results": [rec]}),
    )
    out = verify_npi(VALID_NPI, "Smith")
    assert out["result"] == NpiResult.MISMATCH.value
    assert out["reason"] == "registry_record_has_no_name"


def test_trimmed_cached_record_still_verifies(monkeypatch):
    """Regression: the payload we persist (and later serve as cache) is the
    TRIMMED record — evaluation must read name/status from that shape too,
    or every cache hit silently becomes a MISMATCH."""
    _network_forbidden(monkeypatch)
    trimmed = {
        "number": VALID_NPI, "enumeration_type": "NPI-1", "status": "A",
        "first_name": "Jane", "last_name": "Smith", "credential": "M.D.",
        "enumeration_date": "2008-05-23",
        "taxonomy": {"code": "x", "desc": "Internal Medicine", "state": "MA", "license": "1"},
        "location": {"city": "Boston", "state": "MA"},
    }
    out = verify_npi(VALID_NPI, "Smith", cached={"status": "found", "record": trimmed})
    assert out["result"] == NpiResult.VERIFIED.value
    out2 = verify_npi(VALID_NPI, "Jones", cached={"status": "found", "record": trimmed})
    assert out2["result"] == NpiResult.MISMATCH.value


def test_cache_recomputes_name_match_per_signup(monkeypatch):
    _network_forbidden(monkeypatch)
    cached = {"status": "found", "record": _nppes_record(last_name="Smith")}
    out = verify_npi(VALID_NPI, "Different", cached=cached)
    assert out["result"] == NpiResult.MISMATCH.value


# ─── Store persistence: three states, cache discipline ──────────────────────
def _user_with_npi(store, npi=VALID_NPI):
    return store.provision_user(
        email=f"doc-{npi}-{id(store)}@example.org", password="pw-12345678",
        role="evaluator", full_name="Jane Smith", npi=npi,
    )


def test_store_verified_sets_flag_and_checked_at():
    store = fresh_store()
    u = _user_with_npi(store)
    store.set_npi_result(u["id"], {"result": "verified", "record": _nppes_record()})
    row = store.get_user_by_id(u["id"])
    assert row["npi_verified"] == 1
    assert row["npi_checked_at"] is not None


def test_store_not_found_is_definitive_zero():
    store = fresh_store()
    u = _user_with_npi(store)
    store.set_npi_result(u["id"], {"result": "not_found"})
    row = store.get_user_by_id(u["id"])
    assert row["npi_verified"] == 0
    assert row["npi_checked_at"] is not None


def test_store_unavailable_stays_null_and_never_populates_cache():
    store = fresh_store()
    u = _user_with_npi(store)
    store.set_npi_result(u["id"], {"result": "unavailable", "reason": "rate_limited"})
    row = store.get_user_by_id(u["id"])
    assert row["npi_verified"] is None          # NOT 0 — "could not check"
    assert row["npi_checked_at"] is None        # never satisfies the 30-day cache
    assert store.get_cached_npi_fetch(VALID_NPI) is None
    # F6: a non-definitive outcome is recorded as an ATTEMPT and never written
    # over the result columns, so it cannot erase evidence we already hold.
    assert row["npi_payload_json"] is None
    assert json.loads(row["npi_last_attempt_json"])["reason"] == "rate_limited"
    assert row["npi_last_attempt_at"] is not None


def test_store_cached_fetch_roundtrip():
    store = fresh_store()
    u = _user_with_npi(store)
    store.set_npi_result(
        u["id"], {"result": "verified", "record": _nppes_record()})
    cached = store.get_cached_npi_fetch(VALID_NPI)
    assert cached is not None and cached["status"] == "found"
    # the cached registry record verifies a matching signup without network
    out = verify_npi(VALID_NPI, "Smith", cached=cached)
    assert out["result"] == NpiResult.VERIFIED.value and out["from_cache"]


def test_store_cached_not_found_roundtrip():
    store = fresh_store()
    u = _user_with_npi(store)
    store.set_npi_result(u["id"], {"result": "not_found"})
    cached = store.get_cached_npi_fetch(VALID_NPI)
    assert cached is not None and cached["status"] == "not_found"


# ─── Email domain classification ─────────────────────────────────────────────
@pytest.mark.parametrize("email,expected", [
    ("resident@med.harvard.edu", "academic"),
    ("prof@ucl.ac.uk", "academic"),
    ("doc@sydney.edu.au", "academic"),
    ("attending@mountsinai.org", "academic"),   # academic medical center list
    ("dr@gmail.com", "consumer"),
    ("dr@GMAIL.COM", "consumer"),
    ("dr@yahoo.co.uk", "consumer"),
    ("dr@proton.me", "consumer"),
    ("dr@smallpractice.com", "business"),
    ("dr@kp.org", "business"),                  # health system -> business class
    ("not-an-email", "consumer"),
    ("", "consumer"),
])
def test_classify_email_domain(email, expected):
    assert classify_email_domain(email) == expected


def test_health_system_domain_flag():
    assert is_health_system_domain("kp.org")
    assert not is_health_system_domain("smallpractice.com")


def test_consumer_email_is_a_weight_not_a_gate():
    # classification never raises and never returns a rejecting state; the
    # tier scorer (Phase 3) applies a small negative weight and nothing more.
    assert classify_email_domain("dr@gmail.com") == "consumer"


# ─── Phase 2: CV upload + best-effort parse ──────────────────────────────────
SAMPLE_CV = """\
Jane Q. Smith, M.D.

EDUCATION AND TRAINING
Harvard Medical School, M.D., 2001-2005
Internal Medicine Residency, Massachusetts General Hospital, 2005-2008
Cardiology Fellowship, Brigham and Women's Hospital, 2008-2011

CERTIFICATIONS
Board Certified in Cardiovascular Disease
Diplomate, American Board of Internal Medicine

PUBLICATIONS
Smith JQ, et al. Outcomes in HFrEF. NEJM. 2019.
Smith JQ, et al. TAVR at ten years. JACC. 2021. doi:10.1000/xyz

EXPERIENCE
Attending Cardiologist, Mount Sinai Health System, 2011-present
"""


def _store_sample_cv(text=SAMPLE_CV):
    from asclepius.credentialing import store_cv
    return store_cv(text.encode("utf-8"), "text/plain")


def test_store_cv_rejects_bad_mime_and_empty_and_oversize():
    from asclepius.credentialing import CV_MAX_BYTES, CvUploadError, store_cv
    with pytest.raises(CvUploadError):
        store_cv(b"x", "application/msword")
    with pytest.raises(CvUploadError):
        store_cv(b"", "application/pdf")
    with pytest.raises(CvUploadError):
        store_cv(b"x" * (CV_MAX_BYTES + 1), "application/pdf")


def test_an_image_is_accepted_now_and_a_photo_of_a_cv_is_a_cv():
    """PNG used to be in the rejection list above, alongside svg and MZ.

    It was there as "not a document" rather than as active content, and it cost
    us every physician whose only copy of their CV is a photograph of a printed
    one. It goes through the same OCR path a scanned PDF already took.
    """
    from asclepius.credentialing import sniff_cv_mime, store_cv

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert sniff_cv_mime(png) == "image/png"
    assert store_cv(png, "image/png")["mime"] == "image/png"


@pytest.mark.parametrize("payload,declared", [
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "text/plain"),
    (b"<!DOCTYPE html><html><script>alert(1)</script></html>", "text/plain"),
    (b"<?xml version='1.0'?><x/>", "text/plain"),
    # A zip that is NOT a Word document. .docx is accepted now, and it is
    # accepted by opening the archive and finding word/document.xml, never by
    # the PK magic bytes: .xlsx, .jar, .epub and a plain .zip share them.
    (b"PK\x03\x04zzzz", "application/pdf"),
    (b"MZ\x90\x00binary", "text/plain"),
    (b"text with a \x00 nul byte", "text/plain"),
])
def test_store_cv_sniffs_bytes_and_rejects_active_content(payload, declared):
    """B-5.5 — the declared Content-Type is attacker-controlled and the blob
    is later served inline from the app origin to an admin whose bearer token
    is in localStorage. The bytes decide what is stored."""
    from asclepius.credentialing import CvUploadError, store_cv
    fresh_store()
    with pytest.raises(CvUploadError):
        store_cv(payload, declared)


def test_store_cv_trusts_bytes_over_a_lying_header():
    from asclepius.credentialing import store_cv
    fresh_store()
    meta = store_cv(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n", "text/plain")
    assert meta["mime"] == "application/pdf"      # sniffed, not declared


def test_store_cv_scrubs_pdf_metadata():
    """B-5.6 — process_upload (the metadata-stripping path) is bypassed for
    CVs on purpose, so XMP/DocInfo was retained verbatim."""
    pytest.importorskip("PyPDF2")
    from PyPDF2 import PdfReader, PdfWriter
    from asclepius import assets
    from asclepius.credentialing import store_cv
    fresh_store()

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Author": "Dr Secret Author",
                         "/Producer": "/home/secret/path/cv-final-v3.docx"})
    buf = io.BytesIO()
    writer.write(buf)
    original = buf.getvalue()
    # PyPDF2 encodes text strings, so assert through the parser, not raw bytes.
    assert (PdfReader(io.BytesIO(original)).metadata or {}).get("/Author") == "Dr Secret Author"

    meta = store_cv(original, "application/pdf")
    stored, _ = assets.load_asset(meta["sha256"])
    assert stored.startswith(b"%PDF-")
    info = PdfReader(io.BytesIO(stored)).metadata or {}
    assert not info.get("/Author")
    # The rewriter stamps its own generic /Producer; what must not survive is
    # anything the UPLOADER's authoring tool put there.
    assert "/home/secret/path" not in str(info.get("/Producer") or "")
    assert not any("Secret" in str(v) for v in info.values())
    # and not present in any encoding we wrote it in
    for form in (b"Dr Secret Author", "Dr Secret Author".encode("utf-16-be"),
                 b"/home/secret/path", "/home/secret/path".encode("utf-16-be")):
        assert form not in stored


def test_store_cv_keeps_an_unrewritable_pdf_rather_than_refusing_it():
    """The scrub is best-effort: a CV is optional evidence, so a PDF PyPDF2
    cannot rewrite is stored as-is rather than rejected."""
    from asclepius.credentialing import store_cv
    fresh_store()
    meta = store_cv(b"%PDF-1.4 not really a parseable pdf\n", "application/pdf")
    assert meta["mime"] == "application/pdf"


def test_store_cv_is_content_addressed_and_loadable():
    from asclepius import assets
    fresh_store()
    meta = _store_sample_cv()
    again = _store_sample_cv()
    assert meta["sha256"] == again["sha256"]  # dedupe
    data, _ = assets.load_asset(meta["sha256"])
    assert data.decode("utf-8") == SAMPLE_CV


def test_parse_cv_extracts_suggestions():
    from asclepius.credentialing import parse_cv
    fresh_store()
    meta = _store_sample_cv()
    out = parse_cv(meta["sha256"], mime="text/plain")
    assert out["ok"] is True
    assert any("Harvard Medical School" in i for i in out["institutions"])
    assert any("Massachusetts General Hospital" in i for i in out["institutions"])
    assert any("Cardiovascular Disease" in c for c in out["board_certifications"])
    assert any("Internal Medicine" in c for c in out["board_certifications"])
    # fellowship ended 2011 -> a double-digit years-in-practice suggestion
    assert out["years_in_practice"] is not None and out["years_in_practice"] >= 10
    assert out["publications_count"] >= 2


def test_parse_cv_explicit_years_wins():
    from asclepius.credentialing import parse_cv, store_cv
    fresh_store()
    meta = store_cv("Physician with 7 years of clinical experience.\nCommunity Hospital\n"
                    .encode(), "text/plain")
    out = parse_cv(meta["sha256"], mime="text/plain")
    assert out["years_in_practice"] == 7


def test_parse_cv_missing_asset_is_nonfatal():
    from asclepius.credentialing import parse_cv
    out = parse_cv("0" * 64)
    assert out["ok"] is False
    assert out["reason"].startswith("load_failed")
    assert out["years_in_practice"] is None


def test_parse_cv_garbage_pdf_is_nonfatal():
    from asclepius.credentialing import parse_cv, store_cv
    fresh_store()
    meta = store_cv(b"%PDF-1.4 garbage not a real pdf" + b"\x00" * 64, "application/pdf")
    out = parse_cv(meta["sha256"], mime="application/pdf")
    assert out["ok"] is False  # no text layer, no OCR in CI — degrades cleanly
    assert out["institutions"] == []


def test_store_set_cv_roundtrip():
    store = fresh_store()
    u = _user_with_npi(store)
    meta = _store_sample_cv()
    store.set_cv(u["id"], meta["sha256"], {"ok": True, "institutions": ["X"]})
    row = store.get_user_by_id(u["id"])
    assert row["cv_asset_sha"] == meta["sha256"]
    assert json.loads(row["cv_parsed_json"])["institutions"] == ["X"]


# ─── Phase 3: tier scoring — the score proposes, a human decides ─────────────
from asclepius.credentialing import propose_tier  # noqa: E402


def _verified_payload(taxonomy_desc="Internal Medicine", credential="M.D."):
    rec = _nppes_record(taxonomy_desc=taxonomy_desc)
    rec_trimmed = {
        "number": VALID_NPI, "enumeration_type": "NPI-1", "status": "A",
        "first_name": "Jane", "last_name": "Smith", "credential": credential,
        "enumeration_date": "2008-05-23",
        "taxonomy": {"code": "x", "desc": taxonomy_desc, "state": "MA", "license": "1"},
        "location": {"city": "Boston", "state": "MA"},
    }
    return {"result": "verified", "npi": VALID_NPI, "record": rec_trimmed}


def _scored_user(**overrides):
    base = {
        "email": "jane@smallpractice.com",
        "email_domain_class": None,
        "npi_verified": None,
        "npi_payload_json": None,
        "specialty": None,
        "board_cert": None,
        "years_experience": None,
        "cv_parsed_json": None,
        "linkedin_url": None,
    }
    base.update(overrides)
    return base


def test_senior_verified_physician_proposes_reviewer():
    u = _scored_user(
        email="jane@med.harvard.edu", email_domain_class="academic",
        npi_verified=1, npi_payload_json=json.dumps(_verified_payload("Cardiovascular Disease")),
        board_cert="Cardiovascular Disease", years_experience=15,
    )
    out = propose_tier(u)
    # 25 npi + 10 academic + 20 board + 20 years + 10 subspecialty = 85
    assert out["score"] == 85
    assert out["proposed_tier"] == "reviewer"
    assert out["blockers"] == []
    assert len(out["reasons"]) >= 4


def test_junior_verified_physician_proposes_labeler():
    u = _scored_user(
        email="jr@gmail.com", email_domain_class="consumer",
        npi_verified=1, npi_payload_json=json.dumps(_verified_payload()),
        years_experience=6,
    )
    out = propose_tier(u)
    # 25 npi - 4 consumer + 10 years = 31
    assert out["score"] == 31
    assert out["proposed_tier"] == "labeler"


def test_high_score_without_npi_never_proposes_reviewer():
    u = _scored_user(
        email="prof@med.harvard.edu", email_domain_class="academic",
        board_cert="Internal Medicine", years_experience=20,
        cv_parsed_json=json.dumps({"ok": True}),
        linkedin_url="https://linkedin.com/in/jane-okafor",
    )
    out = propose_tier(u)
    # 10 + 20 + 20 + 5 + 3 = 58: labeler territory, but reviewer requires npi_verified
    assert out["score"] == 58
    assert out["proposed_tier"] == "labeler"


def test_no_npi_and_thin_signal_is_no_proposal_not_a_rejection():
    u = _scored_user(email="dr@gmail.com", email_domain_class="consumer")
    out = propose_tier(u)
    assert out["proposed_tier"] is None
    assert out["blockers"] == []          # nothing blocking — just not enough signal
    assert out["score"] < 30


def test_mismatch_blocks_proposal_even_with_high_score():
    payload = {"result": "mismatch", "reason": "family_name_mismatch",
               "record": _verified_payload()["record"]}
    u = _scored_user(
        email="jane@med.harvard.edu", email_domain_class="academic",
        npi_verified=0, npi_payload_json=json.dumps(payload),
        board_cert="IM", years_experience=20,
        cv_parsed_json=json.dumps({"ok": True}),
    )
    out = propose_tier(u)
    assert out["proposed_tier"] is None
    assert any("mismatch" in b.lower() for b in out["blockers"])


def test_specialty_divergence_blocks_but_unmapped_taxonomy_does_not():
    diverged = _scored_user(
        specialty="nephrology", npi_verified=1,
        npi_payload_json=json.dumps(_verified_payload("Cardiovascular Disease")),
        years_experience=12, board_cert="American Board of Internal Medicine",
    )
    out = propose_tier(diverged)
    assert any("divergence" in b for b in out["blockers"])
    assert out["proposed_tier"] is None

    # "could not determine": generic IM taxonomy maps to no served specialty
    unmapped = _scored_user(
        specialty="nephrology", npi_verified=1,
        npi_payload_json=json.dumps(_verified_payload("Internal Medicine")),
        years_experience=12, board_cert="American Board of Internal Medicine",
    )
    out2 = propose_tier(unmapped)
    assert out2["blockers"] == []
    assert out2["proposed_tier"] == "reviewer"


def test_duplicate_npi_blocks_and_store_flags_both_records():
    store = fresh_store()
    a = store.provision_user(email="a@example.org", password="pw-12345678",
                             role="evaluator", npi=VALID_NPI)
    b = store.provision_user(email="b@example.org", password="pw-12345678",
                             role="evaluator", npi=VALID_NPI)
    claims = store.find_users_by_npi(VALID_NPI)
    assert {c["id"] for c in claims} == {a["id"], b["id"]}  # both records flagged
    u = _scored_user(npi_verified=1, npi_payload_json=json.dumps(_verified_payload()),
                     years_experience=12, board_cert="x")
    out = propose_tier(u, duplicate_npi=True)
    assert out["proposed_tier"] is None
    assert any("Duplicate NPI" in bl for bl in out["blockers"])


def test_reasons_nonempty_whenever_a_tier_is_proposed():
    for user in (
        _scored_user(npi_verified=1, npi_payload_json=json.dumps(_verified_payload()),
                     email_domain_class="business", years_experience=7),
        _scored_user(email_domain_class="academic", board_cert="IM", years_experience=11),
    ):
        out = propose_tier(user)
        if out["proposed_tier"] is not None:
            assert out["reasons"], f"proposal without reasons: {out}"


def test_unavailable_npi_is_visible_in_reasons_but_not_penalized():
    u = _scored_user(npi_payload_json=json.dumps({"result": "unavailable",
                                                  "reason": "rate_limited"}),
                     email_domain_class="business", years_experience=11,
                     board_cert="American Board of Internal Medicine")
    out = propose_tier(u)
    assert any("unavailable" in r for r in out["reasons"])
    assert out["blockers"] == []           # could-not-check never blocks or rejects
    # 6 business + 20 years + 20 board = 46 -> labeler despite the pending check
    assert out["proposed_tier"] == "labeler"


def test_health_system_domain_outweighs_generic_business():
    base = dict(npi_verified=1, npi_payload_json=json.dumps(_verified_payload()),
                years_experience=3)
    hs = propose_tier(_scored_user(email="dr@kp.org", email_domain_class="business", **base))
    biz = propose_tier(_scored_user(email="dr@corp.com", email_domain_class="business", **base))
    assert hs["score"] == biz["score"] + 4  # +10 vs +6
