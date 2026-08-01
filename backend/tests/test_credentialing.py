"""PRD B Phase 1 — NPI verification (NPPES) + email domain classification.

The load-bearing property under test: every check has THREE outcomes. A
definitive negative (NOT_FOUND / MISMATCH) is distinct from "could not check"
(UNAVAILABLE), and UNAVAILABLE never collapses into a rejection path.
"""

from __future__ import annotations

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
    assert json.loads(row["npi_payload_json"])["reason"] == "rate_limited"
    assert store.get_cached_npi_fetch(VALID_NPI) is None


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
