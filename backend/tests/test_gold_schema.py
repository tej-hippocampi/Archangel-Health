"""Unit tests for the Gold Standard export schema, de-id, export, and crypto.

These run without the app, the LLM, or any network — they validate the
*product* (the exported record) and the building blocks that produce it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import field_crypto  # noqa: E402
from gold import export as gold_export  # noqa: E402
from gold import schema as gold_schema  # noqa: E402
from gold.deid import deidentify  # noqa: E402


def _valid_visit() -> dict:
    return {
        "id": "v1",
        "tenant_slug": "demo",
        "record_num": 142,
        "specialty": "general_surgery",
        "encounter_type": "post-op follow-up",
        "consent_given": True,
        "consent_method": "in_app_verbal",
        "consent_timestamp": "2026-06-22T15:04:00Z",
        "baa_on_file": True,
        "deid_method": "automated (regex) + human QA",
        "verified_by_operator": True,
        "audio_duration_sec": 734,
        "difficulty_tags": ["background_noise"],
        "languages": ["en"],
        "transcript_deid": "[PATIENT] potassium was high [DOCTOR] stop the drug",
        "ai_draft_note": "Continue drug.",
        "ai_draft_note_deid": "Continue drug.",
        "gold_note_deid": "Discontinue drug due to hyperkalemia.",
        "error_labels": [
            {
                "type": "medication_error",
                "subtype": "drug_discontinued_but_model_continued",
                "severity": "high",
                "section": "plan",
                "original_text": "Continue drug",
                "corrected_text": "Discontinue drug",
                "clinician_verified": True,
            }
        ],
        "billing_codes": [{"system": "ICD-10", "code": "N18.4", "verified_by": "clinician"}],
        "prior_auth": {"drug_or_service": "patiromer", "justification_text": "x", "outcome": "approved"},
        "clinician_review_seconds": 38,
        "clinician_id_hashed": "a91f",
        "created_at": "2026-06-22T15:10:00Z",
    }


def test_record_id_format():
    assert gold_schema.record_id_for("demo", 142) == "demo-gold-000142"
    assert gold_schema.record_id_for("", 1) == "tenant-gold-000001"


def test_valid_record_passes():
    record = gold_schema.build_record(_valid_visit())
    assert gold_schema.validate_record(record) == []
    assert record["record_id"] == "demo-gold-000142"
    assert record["created_at"] == "2026-06-22"
    assert record["gold_note"] == "Discontinue drug due to hyperkalemia."


def test_record_carries_version_hash_correction_tasks():
    record = gold_schema.build_record(_valid_visit())
    assert record["schema_version"] == gold_schema.SCHEMA_VERSION == "1.1.0"
    assert len(record["content_sha256"]) == 64
    corr = record["correction"]
    assert corr["was_edited"] is True
    assert corr["num_error_labels"] == 1
    assert corr["edit_distance_chars"] > 0
    assert corr["draft_note_deid"] == "Continue drug."
    assert corr["gold_note_deid"] == "Discontinue drug due to hyperkalemia."
    # tasks: always note_generation, + icd10 (ICD-10 billing) + prior_auth present.
    assert "note_generation" in record["tasks"]
    assert "icd10_coding" in record["tasks"]
    assert "prior_auth" in record["tasks"]
    # reviewer block is pseudonymous (no raw identity).
    assert record["reviewer"]["role"]
    assert record["deidentification"]["residual_scan_passed"] is True


def test_looks_correct_fastpath_marks_unedited():
    v = _valid_visit()
    v["error_labels"] = []
    v["ai_draft_note_deid"] = "Draft accepted as-is."
    v["gold_note_deid"] = "Draft accepted as-is."
    record = gold_schema.build_record(v)
    assert record["correction"]["was_edited"] is False
    assert record["correction"]["draft_note_deid"] == record["correction"]["gold_note_deid"]
    assert gold_schema.validate_record(record) == []


def test_residual_identifier_rejected():
    v = _valid_visit()
    v["gold_note_deid"] = "Patient stable; call 415-555-1234."
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("residual identifier" in e for e in errs)


def test_residual_identifier_in_label_rejected():
    v = _valid_visit()
    v["error_labels"][0]["corrected_text"] = "see chart MRN: 998877"
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("residual identifier" in e for e in errs)


def test_sft_messages_export_shape():
    _jsonl, exported, _rej = gold_export.build_export([_valid_visit()], pseudonymize=False)
    import json

    sft = gold_export.sft_messages_jsonl(exported)
    row = json.loads(sft.strip().splitlines()[0])
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
    assert row["messages"][2]["content"] == "Discontinue drug due to hyperkalemia."
    assert row["metadata"]["schema_version"] == gold_schema.SCHEMA_VERSION


def test_dataset_card_and_croissant():
    _jsonl, exported, _rej = gold_export.build_export([_valid_visit()], pseudonymize=False)
    card = gold_export.dataset_card_md(exported)
    assert "Dataset Card" in card and "HIPAA Safe Harbor" in card
    croissant = gold_export.croissant_json(exported)
    assert croissant["recordCount"] == 1
    assert any(f["name"] == "gold_note" for f in croissant["recordSet"][0]["field"])


def test_deidentify_scrubs_all_freetext_fields():
    res = asyncio.run(
        deidentify(
            transcript="Call 415-555-1234",
            gold_note="Note on 06/01/2026",
            ai_draft_note="Draft: reach me@example.com",
            error_labels=[{"type": "omission", "original_text": "old 212-555-9000", "corrected_text": "new 312-555-7777"}],
            prior_auth={"drug_or_service": "MRI", "justification_text": "dated 06/01/2026, ph 415-555-1234"},
            patient_name="Maria Lopez",
            visit_id="v1",
        )
    )
    assert "415-555-1234" not in res["ai_draft_note_deid"]
    assert "me@example.com" not in res["ai_draft_note_deid"]
    lbl = res["error_labels_deid"][0]
    assert "212-555-9000" not in lbl["original_text"]
    assert "312-555-7777" not in lbl["corrected_text"]
    assert "415-555-1234" not in res["prior_auth_deid"]["justification_text"]
    assert "06/01/2026" not in res["prior_auth_deid"]["justification_text"]
    assert res["method_detail"]  # records which layers ran


def test_missing_consent_fails():
    v = _valid_visit()
    v["consent_given"] = False
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("consent_given" in e for e in errs)


def test_baa_gate_in_schema():
    v = _valid_visit()
    v["baa_on_file"] = False
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("baa_on_file" in e for e in errs)


def test_unverified_operator_fails():
    v = _valid_visit()
    v["verified_by_operator"] = False
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("verified_by_operator" in e for e in errs)


def test_bad_error_label_type_fails():
    v = _valid_visit()
    v["error_labels"][0]["type"] = "not_a_real_type"
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("not in taxonomy" in e for e in errs)


def test_empty_gold_note_fails():
    v = _valid_visit()
    v["gold_note_deid"] = ""
    errs = gold_schema.validate_record(gold_schema.build_record(v))
    assert any("gold_note" in e for e in errs)


def test_looks_correct_empty_labels_valid():
    v = _valid_visit()
    v["error_labels"] = []
    assert gold_schema.validate_record(gold_schema.build_record(v)) == []


def test_export_jsonl_and_pseudonymize():
    jsonl, exported, rejected = gold_export.build_export([_valid_visit()], pseudonymize=True)
    assert rejected == []
    assert len(exported) == 1
    assert jsonl.strip()
    # tenant_slug stripped, record_id pseudonymized
    rec = exported[0]
    assert "tenant_slug" not in rec
    assert rec["record_id"].startswith("gold-")
    assert rec["record_id"] != "demo-gold-000142"


def test_export_rejects_invalid():
    bad = _valid_visit()
    bad["consent_given"] = False
    _jsonl, exported, rejected = gold_export.build_export([bad])
    assert exported == []
    assert len(rejected) == 1


def test_data_dictionary_nonempty():
    md = gold_export.data_dictionary_md()
    assert "Data Dictionary" in md
    assert "gold_note" in md


def test_regex_deid_typed_placeholders():
    res = asyncio.run(
        deidentify(
            transcript="Call me at 415-555-1234 or me@example.com",
            gold_note="Patient seen on 06/01/2026, MRN: 12345.",
            patient_name="Maria Lopez",
            visit_id="v1",
        )
    )
    # GOLD_DEID_PROVIDER defaults to "llm" but with no key the regex baseline runs.
    assert "[PHONE]" in res["transcript_deid"]
    assert "[EMAIL]" in res["transcript_deid"]
    assert "[DATE]" in res["gold_note_deid"]
    assert "415-555-1234" not in res["transcript_deid"]
    assert "human QA" in res["method"]


def test_field_crypto_bytes_roundtrip(monkeypatch):
    import base64

    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY_VERSION", "1")
    data = b"\x00\x01webm-audio-bytes\xff"
    token = field_crypto.encrypt_bytes(data)
    assert field_crypto.is_encrypted_bytes(token)
    assert token != data
    assert field_crypto.decrypt_bytes(token) == data


def test_field_crypto_bytes_passthrough_without_key(monkeypatch):
    monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
    data = b"plain"
    assert field_crypto.encrypt_bytes(data) == data
    assert field_crypto.decrypt_bytes(data) == data
