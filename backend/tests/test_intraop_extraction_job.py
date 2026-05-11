"""
Tests for `run_extraction_job` (PRD §6.2 / §8.1).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop.extraction_job import (  # noqa: E402
    auto_populate_form,
    diff_against_form,
    run_extraction_job,
)
from triage.intraop.extractor import MockIntraopExtractor  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def store(monkeypatch):
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    monkeypatch.setenv("TEAM_DB_PATH", db_path)
    from team_store import TeamStore
    return TeamStore(db_path=db_path)


# ─── Pure helpers ────────────────────────────────────────────────────────────

def test_diff_against_form_returns_empty_when_agree_or_unfilled():
    diff = diff_against_form(
        form_fields={"ebl": 600, "or_duration_minutes": None},
        extracted={"ebl": 600, "or_duration_minutes": 90},
    )
    assert diff == {}


def test_diff_against_form_flags_disagreements():
    diff = diff_against_form(
        form_fields={"ebl": 500},
        extracted={"ebl": 600},
    )
    assert diff == {"ebl": {"existing": 500, "extracted": 600}}


def test_auto_populate_form_skips_filled_fields():
    new_fields, new_origins, populated = auto_populate_form(
        form_fields={"ebl": 500},
        field_origins={"ebl": {"origin": "MANUAL", "populated_at": "2026-05-08T10:00"}},
        extracted={"ebl": 600, "or_duration_minutes": 90},
        extracted_confidences={"ebl": 0.95, "or_duration_minutes": 0.50},
        extraction_id="e1",
        pdf_blob_url="blob://abc",
    )
    assert new_fields["ebl"] == 500          # surgeon's value preserved
    assert new_fields["or_duration_minutes"] == 90
    assert "or_duration_minutes" in populated
    assert "ebl" not in populated
    assert new_origins["or_duration_minutes"]["origin"] == "AUTO_POP_PDF"
    assert new_origins["or_duration_minutes"]["confidence"] == 0.50


def test_auto_populate_form_skips_none_extracted():
    new_fields, _, populated = auto_populate_form(
        form_fields={},
        field_origins={},
        extracted={"ebl": None},
        extracted_confidences={"ebl": 0.0},
        extraction_id="e1",
        pdf_blob_url="blob://abc",
    )
    assert "ebl" not in new_fields
    assert populated == []


# ─── Extraction job lifecycle ────────────────────────────────────────────────

def test_run_extraction_job_complete_path(store):
    store.get_or_create_intraop_form(patient_id="p1", or_ended_at="2026-05-08T10:00:00")
    store.save_intraop_extraction(extraction_id="ext1", patient_id="p1", pdf_blob_url="blob://x")

    out = _run(run_extraction_job(
        extraction_id="ext1", patient_id="p1",
        pdf_bytes=b"fake-pdf", pdf_blob_url="blob://x",
        procedure_family="LEJR", procedure_name="TKA",
        extractor=MockIntraopExtractor(), team_store=store,
    ))

    assert out["status"] == "COMPLETE"
    assert "ebl" in out["fields_populated"]
    ext = store.get_intraop_extraction("ext1")
    assert ext["status"] == "COMPLETE"
    assert ext["fields"]["ebl"] == 250
    assert ext["field_confidences"]["ebl"] == 0.95
    form = store.get_intraop_form("p1")
    assert form["fields"]["ebl"] == 250
    assert form["field_origins"]["ebl"]["origin"] == "AUTO_POP_PDF"
    assert form["status"] == "IN_PROGRESS"


def test_run_extraction_job_does_not_overwrite_surgeon_input(store):
    store.get_or_create_intraop_form(patient_id="p1")
    # Surgeon enters EBL manually first.
    store.update_intraop_form_fields(
        patient_id="p1",
        fields={"ebl": 999},
        field_origins={"ebl": {"origin": "MANUAL", "populated_at": "2026-05-08T10:00"}},
    )
    store.save_intraop_extraction(extraction_id="ext1", patient_id="p1", pdf_blob_url="blob://x")

    out = _run(run_extraction_job(
        extraction_id="ext1", patient_id="p1",
        pdf_bytes=b"x", pdf_blob_url="blob://x",
        procedure_family="LEJR", procedure_name="TKA",
        extractor=MockIntraopExtractor(), team_store=store,
    ))
    assert out["status"] == "COMPLETE"
    assert "ebl" in out["field_diffs"]
    assert out["field_diffs"]["ebl"] == {"existing": 999, "extracted": 250}

    form = store.get_intraop_form("p1")
    assert form["fields"]["ebl"] == 999          # surgeon wins
    assert form["field_origins"]["ebl"]["origin"] == "MANUAL"


def test_run_extraction_job_failed_path(store):
    store.get_or_create_intraop_form(patient_id="p1")
    store.save_intraop_extraction(extraction_id="ext1", patient_id="p1", pdf_blob_url="blob://x")

    out = _run(run_extraction_job(
        extraction_id="ext1", patient_id="p1",
        pdf_bytes=b"x", pdf_blob_url="blob://x",
        procedure_family="LEJR", procedure_name="TKA",
        extractor=MockIntraopExtractor(simulate_failure=True),
        team_store=store,
    ))
    assert out["status"] == "FAILED"
    assert "simulated" in out["error"]
    ext = store.get_intraop_extraction("ext1")
    assert ext["status"] == "FAILED"
    assert "simulated" in ext["error_message"]


def test_run_extraction_job_preserves_locked_status(store):
    """If the form is already LOCKED, the auto-pop merge must not regress it
    back to IN_PROGRESS — it should stay LOCKED until admin reopens."""
    store.get_or_create_intraop_form(patient_id="p1")
    store.update_intraop_form_fields(
        patient_id="p1",
        fields={"ebl": 100},
        field_origins={"ebl": {"origin": "MANUAL", "populated_at": "2026-05-08T10:00"}},
        status="LOCKED",
    )
    store.save_intraop_extraction(extraction_id="ext1", patient_id="p1", pdf_blob_url="blob://x")

    _run(run_extraction_job(
        extraction_id="ext1", patient_id="p1",
        pdf_bytes=b"x", pdf_blob_url="blob://x",
        procedure_family="LEJR", procedure_name="TKA",
        extractor=MockIntraopExtractor(), team_store=store,
    ))
    form = store.get_intraop_form("p1")
    assert form["status"] == "LOCKED"
