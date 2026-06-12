"""FHIR_JSON format detection + Bundle parsing/rendering + ``_parse_one``
integration with the eligibility pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eligibility import format_detect  # noqa: E402
from eligibility.parse_fhir import (  # noqa: E402
    InvalidFhirError,
    format_for_llm,
    parse_fhir_bundle,
)

MBI_SYSTEM = "http://hl7.org/fhir/sid/us-mbi"


def sample_bundle() -> dict:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "okafor-margaret",
                    "identifier": [{"system": MBI_SYSTEM, "value": "4WH7QD2RT55"}],
                    "name": [{"family": "Okafor", "given": ["Margaret", "Anne"]}],
                    "birthDate": "1954-09-17",
                    "gender": "female",
                }
            },
            {
                "resource": {
                    "resourceType": "Coverage",
                    "id": "cov-a",
                    "status": "active",
                    "type": {
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                            "code": "MCPOL",
                            "display": "Medicare Part A (Hospital Insurance)",
                        }]
                    },
                    "payor": [{"display": "Original Medicare (Fee-for-Service)"}],
                    "subscriberId": "4WH7QD2RT55",
                    "period": {"start": "2019-10-01"},
                    "order": 1,
                }
            },
        ],
    }


# ─── format detection ───────────────────────────────────────────────────────
def test_detect_fhir_json():
    raw = json.dumps(sample_bundle()).encode()
    assert format_detect.detect_format("fhir_coverage.json", raw[:4096]) == "FHIR_JSON"


def test_detect_fhir_json_size_cap():
    assert format_detect.max_size_for("FHIR_JSON") == 10 * 1024 * 1024


def test_json_without_resource_type_stays_other():
    raw = b'{"hello": "world", "items": [1, 2, 3]}'
    assert format_detect.detect_format("data.json", raw) == "OTHER"


def test_existing_formats_unaffected():
    assert format_detect.detect_format("file.pdf", b"%PDF-1.7 ...") == "PDF"
    assert format_detect.detect_format("elig.271", b"ISA*00*        ") == "X12_271"
    assert format_detect.detect_format("roster.csv", b"name,mbi\na,1\n") == "CSV"


# ─── parsing + rendering ────────────────────────────────────────────────────
def test_parse_bundle_splits_patient_and_coverage():
    res = parse_fhir_bundle(json.dumps(sample_bundle()).encode())
    assert res.patient is not None and res.patient["id"] == "okafor-margaret"
    assert len(res.coverages) == 1


def test_parse_accepts_bare_resource():
    cov = sample_bundle()["entry"][1]["resource"]
    res = parse_fhir_bundle(json.dumps(cov).encode())
    assert res.patient is None
    assert len(res.coverages) == 1


def test_parse_rejects_non_json():
    with pytest.raises(InvalidFhirError):
        parse_fhir_bundle(b"this is not json")


def test_parse_rejects_bundle_without_relevant_resources():
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": []}
    with pytest.raises(InvalidFhirError, match="no Patient or Coverage"):
        parse_fhir_bundle(json.dumps(bundle).encode())


def test_render_surfaces_identity_and_coverage_fields():
    res = parse_fhir_bundle(json.dumps(sample_bundle()).encode())
    text = format_for_llm(res, "fhir_coverage_okafor.json")
    assert "FHIR COVERAGE RECORD" in text
    assert "MBI (Medicare Beneficiary Identifier): 4WH7QD2RT55" in text
    assert "Margaret Anne Okafor" in text
    assert "1954-09-17" in text
    assert "Status: active" in text
    assert "Medicare Part A (Hospital Insurance)" in text
    assert "MCPOL" in text  # raw coding preserved for the extractor
    assert "Original Medicare (Fee-for-Service)" in text
    assert "Coordination-of-benefits order: 1" in text


def test_render_notes_missing_coverage():
    bundle = {"resourceType": "Bundle", "entry": [sample_bundle()["entry"][0]]}
    res = parse_fhir_bundle(json.dumps(bundle).encode())
    assert "No Coverage resources were returned" in format_for_llm(res)


# ─── pipeline integration (_parse_one) ──────────────────────────────────────
def test_parse_one_handles_fhir_json(tmp_path):
    from eligibility.pipeline import _parse_one

    p = tmp_path / "doc.json"
    p.write_bytes(json.dumps(sample_bundle()).encode())
    doc = {"path": str(p), "format": "FHIR_JSON", "filename": "doc.json", "size_bytes": p.stat().st_size}
    out = _parse_one(doc)
    assert out["parse_error"] is None
    assert "FHIR COVERAGE RECORD" in out["llm_text"]
    assert out["parse_meta"]["coverages"] == 1
    assert out["parse_meta"]["has_patient"] is True


def test_parse_one_flags_invalid_fhir(tmp_path):
    from eligibility.pipeline import _parse_one

    p = tmp_path / "bad.json"
    p.write_bytes(b'{"resourceType": "Bundle", "entry": []}')
    doc = {"path": str(p), "format": "FHIR_JSON", "filename": "bad.json", "size_bytes": 10}
    out = _parse_one(doc)
    assert out["parse_error"] == "INVALID_FHIR"
    assert out["llm_text"] == ""
