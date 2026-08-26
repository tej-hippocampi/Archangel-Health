"""International registry verification.

The rule these tests exist to hold: a registry we do not fully trust may
confirm a doctor, it may never disprove one. Only an authoritative source can
return NOT_FOUND; everywhere else a miss is INCONCLUSIVE and routes to
document review. Getting that backwards rejects real physicians on the
strength of a stale register, which is the one failure here we would never
see and they would never forget.

Payload shapes are recorded from the live registries (NMC's IMR search and
PMDC's practitioner search), so these run offline and stay honest about what
those endpoints actually return -- including the parts that make matching
hard: one registration number belongs to a different doctor in every state
council, and Indian register entries write names in whatever order they like.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from asclepius.registry import config as registry_config
from asclepius.registry.adapters import nmc_in, pmdc_pk
from asclepius.registry.dispatch import RegistryResult, verify_credential


# ─── Recorded payloads ───────────────────────────────────────────────────────
# Real rows for registration number 45678, which fourteen different doctors
# hold across fourteen councils.
_IMR_ROWS = [
    [1, 1981, "45678", "Maharashtra Medical Council", "Anoopkumar Prakash", None, ""],
    [2, 1987, "45678", "West Bengal Medical Council", "Resmi  Pal ( Mrs. Kundu )",
     "Dr. Ranajit Kumar Pal", ""],
    [3, 1989, "45678", "Tamil Nadu Medical Council", "Purushothaman, Munuswamy ",
     "A Munuswamy", ""],
    [4, 1997, "45678", "Karnataka Medical Council", "KRISHNA C", "S K SESHA CHANDRIKA", ""],
    [5, 2019, "45678", "Rajasthan Medical Council", "KUMAWAT, (MISS) SUMAN", None, ""],
]

_PMDC_ROW = {
    "RegistrationNo": "81910-P", "Name": "YASIR IQBAL", "FatherName": "MUHAMMAD IQBAL",
    "Gender": None, "RegistrationType": "Permanent", "RegistrationDate": "18/03/2016",
    "ValidUpto": "13/11/2028", "Status": "ACTIVE", "IsFaculty": False,
    "Qualifications": None,
}


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def imr(monkeypatch):
    """Stub the IMR endpoint; returns a dict the test can mutate."""
    state: Dict[str, Any] = {"rows": list(_IMR_ROWS), "status": 200, "payload": None}

    def _request(method, url, **kwargs):
        if state["payload"] is not None:
            return _Response(state["payload"], state["status"])
        return _Response({"data": state["rows"], "recordsFiltered": len(state["rows"])},
                         state["status"])

    monkeypatch.setattr(nmc_in._http, "request", _request)
    return state


@pytest.fixture
def pmdc(monkeypatch):
    state: Dict[str, Any] = {"rows": [dict(_PMDC_ROW)], "status": 200, "ok": True}

    def _request(method, url, **kwargs):
        return _Response({"status": state["ok"], "data": state["rows"],
                          "message": f"{len(state['rows'])} Records Found!"},
                         state["status"])

    monkeypatch.setattr(pmdc_pk._http, "request", _request)
    return state


# ─── The rule: a non-authoritative miss is never a rejection ─────────────────
def test_india_miss_is_inconclusive_not_not_found(imr):
    imr["rows"] = []
    out = verify_credential("IN", "99999999", "Whoever", extras={})
    assert out["result"] == RegistryResult.INCONCLUSIVE.value
    assert out["result"] != RegistryResult.NOT_FOUND.value


def test_pakistan_miss_is_inconclusive_not_not_found(pmdc):
    pmdc["rows"] = []
    out = verify_credential("PK", "999999-Z", "Someone", extras={})
    assert out["result"] == RegistryResult.INCONCLUSIVE.value


def test_no_configured_registry_can_reject_unless_authoritative():
    """A guard on the config itself: any country whose adapter is allowed to
    say NOT_FOUND must be a real API we trust to know."""
    for cfg in registry_config.REGISTRY_CONFIGS.values():
        if cfg.authoritative:
            assert cfg.method == registry_config.METHOD_API, cfg.country


def test_transport_failure_is_unavailable_not_a_verdict(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(nmc_in._http, "request", _boom)
    out = verify_credential("IN", "45678", "Prakash", extras={})
    assert out["result"] == RegistryResult.UNAVAILABLE.value


def test_unexpected_response_shape_is_unavailable(imr):
    """A registry that answers 200 with something we do not recognize has told
    us nothing about the doctor."""
    imr["payload"] = {"unexpected": "shape"}
    out = verify_credential("IN", "45678", "Prakash", extras={})
    assert out["result"] == RegistryResult.UNAVAILABLE.value


def test_adapter_that_raises_never_escapes(monkeypatch):
    monkeypatch.setattr(nmc_in, "match", lambda *a, **k: 1 / 0)
    out = verify_credential("IN", "45678", "Prakash", extras={})
    assert out["result"] == RegistryResult.UNAVAILABLE.value


# ─── India: one number, fourteen doctors ─────────────────────────────────────
def test_india_verifies_on_number_council_and_name(imr):
    out = verify_credential(
        "IN", "45678", "Anoopkumar Prakash",
        extras={"stateCouncil": "Maharashtra Medical Council", "registrationYear": "1981"},
    )
    assert out["result"] == RegistryResult.VERIFIED.value


@pytest.mark.parametrize("claimed,council", [
    ("Munuswamy Purushothaman", "Tamil Nadu Medical Council"),   # surname first, comma
    ("Purushothaman", "Tamil Nadu Medical Council"),             # surname alone
    ("Resmi Pal", "West Bengal Medical Council"),                # married-name aside
    ("Krishna", "Karnataka Medical Council"),                    # trailing initial
    ("Suman Kumawat", "Rajasthan Medical Council"),              # "SURNAME, (MISS) GIVEN"
])
def test_india_matches_names_in_any_order(imr, claimed, council):
    """The register writes names however it likes. Position-based surname
    matching -- the NPPES rule -- is wrong about half the time here."""
    out = verify_credential("IN", "45678", claimed, extras={"stateCouncil": council})
    assert out["result"] == RegistryResult.VERIFIED.value, out["reason"]


def test_india_wrong_council_is_a_review_flag(imr):
    out = verify_credential(
        "IN", "45678", "Anoopkumar Prakash",
        extras={"stateCouncil": "Kerala State Medical Council"},
    )
    assert out["result"] == RegistryResult.MISMATCH.value
    assert out["reason"] == "registration_found_under_a_different_council"


def test_india_unknown_name_does_not_verify(imr):
    out = verify_credential(
        "IN", "45678", "Zzzznotareal",
        extras={"stateCouncil": "Maharashtra Medical Council"},
    )
    assert out["result"] == RegistryResult.MISMATCH.value


def test_india_ambiguous_name_within_a_council_is_not_an_identification(imr):
    """Two registrants under one council whose names both overlap is a
    coincidence to hand a human, not a verification."""
    imr["rows"] = [
        [1, 2001, "45678", "Punjab Medical Council", "Amandeep Kaur", None, ""],
        [2, 2004, "45678", "Punjab Medical Council", "Kaur Simranjit", None, ""],
    ]
    out = verify_credential("IN", "45678", "Kaur", extras={"stateCouncil": "Punjab Medical Council"})
    assert out["result"] == RegistryResult.MISMATCH.value
    assert out["reason"] == "several_registrants_match_this_name_and_number"


def test_india_single_letter_tokens_never_match(imr):
    """"KRISHNA C" must not be matched by everyone whose name contains a C."""
    imr["rows"] = [[1, 1997, "45678", "Karnataka Medical Council", "KRISHNA C", None, ""]]
    out = verify_credential("IN", "45678", "C", extras={"stateCouncil": "Karnataka Medical Council"})
    assert out["result"] == RegistryResult.MISMATCH.value


# ─── Pakistan ────────────────────────────────────────────────────────────────
def test_pakistan_verifies_and_reports_validity(pmdc):
    out = verify_credential("PK", "81910-P", "Yasir Iqbal", extras={})
    assert out["result"] == RegistryResult.VERIFIED.value
    assert out["record"]["status"] == "ACTIVE"
    assert out["record"]["valid_until"] == "13/11/2028"


def test_pakistan_suspended_registration_is_a_review_flag(pmdc):
    pmdc["rows"][0]["Status"] = "SUSPENDED"
    out = verify_credential("PK", "81910-P", "Yasir Iqbal", extras={})
    assert out["result"] == RegistryResult.MISMATCH.value
    assert out["reason"] == "registration_suspended"


def test_pakistan_registry_rejecting_the_query_is_unavailable(pmdc):
    """PMDC answers 200 with status:false when it will not process a query.
    That is not evidence about the doctor."""
    pmdc["ok"] = False
    out = verify_credential("PK", "81910-P", "Yasir Iqbal", extras={})
    assert out["result"] == RegistryResult.UNAVAILABLE.value


# ─── Countries with no lookup ────────────────────────────────────────────────
def test_saudi_arabia_routes_to_document_review():
    """SCFHS's check is behind two captchas and keyed on a national ID. The
    certificate is the evidence, and this must not read as a failure."""
    out = verify_credential("SA", "123456", "Al Otaibi", extras={})
    assert out["result"] == RegistryResult.DOCUMENT_ONLY.value
    assert "Saudi" in out["registry"]


def test_a_country_we_have_never_heard_of_still_onboards():
    out = verify_credential("ZZ", "123", "Somebody", extras={})
    assert out["result"] == RegistryResult.DOCUMENT_ONLY.value


def test_blank_country_is_treated_as_us(monkeypatch):
    """Legacy rows predate the country question; they are all US signups."""
    seen = {}

    def _verify_npi(npi, family_name, timeout=None, cached=None):
        seen["npi"] = npi
        return {"result": "verified", "npi": npi, "reason": None, "record": {},
                "from_cache": False}

    monkeypatch.setattr("asclepius.credentialing.verify_npi", _verify_npi)
    out = verify_credential("", "1234567893", "Chen", extras={})
    assert seen["npi"] == "1234567893"
    assert out["result"] == "verified"


# ─── Format checking is advice, not a gate ───────────────────────────────────
def test_format_check_is_advisory_only():
    assert registry_config.format_looks_right("PK", "81910-P") is True
    assert registry_config.format_looks_right("PK", "nonsense") is False
    # ...and a country with no published format never fails one.
    assert registry_config.format_looks_right("KE", "anything at all") is True


def test_every_country_offers_a_way_through():
    """No configured country may leave a doctor with nothing to submit."""
    for cfg in registry_config.REGISTRY_CONFIGS.values():
        assert cfg.id_label
        assert cfg.method in {
            registry_config.METHOD_API,
            registry_config.METHOD_SCRAPE,
            registry_config.METHOD_DOCUMENT,
        }
        if cfg.method == registry_config.METHOD_DOCUMENT:
            # A human has to be able to act on it: either a place to look it
            # up, or an instruction saying what to do instead.
            assert cfg.lookup_url or cfg.note, cfg.country
