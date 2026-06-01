"""Unit tests for grounding check (mocked judge)."""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.grounding_check import (
    GROUNDING_PROMPT_V,
    GroundingReport,
    assert_script_is_grounded,
    build_required_items,
    check_grounding,
    compute_accuracy,
    parse_grounding_response,
)


CARDIAC_SD = {
    "patient_name": "James Harrington",
    "procedure_name": "Cardiac Catheterization",
    "procedure_date": "2025-03-10",
    "medications": [
        {"name": "Aspirin", "dose": "81mg", "frequency": "daily", "status": "new"},
        {"name": "Warfarin", "dose": "5mg", "frequency": "daily", "status": "stop", "notes": "Stop 5 days before surgery"},
    ],
    "red_flags": ["Chest pain or pressure", "Fever above 100.4°F — call us immediately"],
    "diet_instructions": "Low sodium diet",
    "activity_restrictions": "No strenuous activity for 5 days",
    "wound_care": "Keep bandage on for 24 hours",
    "allergies": ["Penicillin"],
    "follow_up": {"date": "2025-03-17", "provider": "Dr. Patel, Cardiology"},
    "key_diagnoses": ["Coronary Artery Disease"],
    "post_op_instructions": "Keep access site dry for 48 hours",
}

PREOP_SD = {
    "patient_name": "Marcus Webb",
    "procedure_date": "2025-03-18",
    "medications": [
        {"name": "Metformin", "dose": "500mg", "status": "hold", "notes": "Stop 48 hours before surgery"},
        {"name": "Lisinopril", "dose": "10mg", "status": "continue", "notes": "Take morning of surgery"},
    ],
    "pre_op_instructions": "Arrive 2 hours early. Nothing to eat after midnight.",
    "diet_instructions": "Clear liquids only after midnight. No solid food.",
    "activity_restrictions": "Arrange driver — you cannot drive yourself home.",
    "red_flags": ["Fever above 100.4°F before surgery"],
    "allergies": ["Penicillin"],
}


def _mock_response(verdict: str, **kwargs) -> MagicMock:
    payload = {
        "track": kwargs.get("track", "post_op_treatment"),
        "coverage": kwargs.get("coverage", []),
        "faithfulness": kwargs.get("faithfulness", []),
        "critical_failures": kwargs.get("critical_failures", []),
        "verdict": verdict,
        "summary": kwargs.get("summary", f"Test verdict {verdict}"),
    }
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload))]
    resp = MagicMock()
    resp.content = msg.content
    return resp


@pytest.mark.parametrize(
    "track,field",
    [
        ("pre_op", "pre_op_instructions"),
        ("post_op_treatment", "medications"),
        ("post_op_diagnosis", "key_diagnoses"),
    ],
)
def test_build_required_items_only_from_source(track, field):
    sd = dict(PREOP_SD if track == "pre_op" else CARDIAC_SD)
    sd.pop(field, None)
    if track == "post_op_treatment":
        sd["medications"] = []
        sd["red_flags"] = []
        sd["follow_up"] = {}
        sd["wound_care"] = ""
        sd["diet_instructions"] = ""
        sd["activity_restrictions"] = ""
    if track == "post_op_diagnosis":
        sd["key_diagnoses"] = []
        sd["post_op_instructions"] = ""
        sd["follow_up"] = {}
    items = build_required_items(sd, track)
    for item in items:
        assert item["severity"] in ("CRITICAL", "MAJOR", "MINOR")


def test_build_required_items_preop_meds():
    items = build_required_items(PREOP_SD, "pre_op")
    ids = {i["id"] for i in items}
    assert any("metformin" in i for i in ids)
    assert any("lisinopril" in i for i in ids)
    assert any(i["id"] == "preop_npo" for i in items)


def test_compute_accuracy():
    report = GroundingReport(
        track="post_op_treatment",
        coverage=[
            {"status": "COVERED"},
            {"status": "PARTIAL"},
            {"status": "MISSING"},
        ],
        faithfulness=[
            {"status": "SUPPORTED"},
            {"status": "UNSUPPORTED"},
        ],
        critical_failures=["x"],
        verdict="BLOCK",
        summary="bad",
    )
    acc = compute_accuracy(report)
    assert acc["coverage_pct"] == 33.3
    assert acc["faithfulness_pct"] == 50.0
    assert acc["items_partial"] == 1
    assert acc["unsupported_claims"] == 1


def test_check_grounding_pass():
    async def _run():
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=_mock_response("PASS", track="pre_op"))
        return await check_grounding(PREOP_SD, "script text", "pre_op", client=client)

    report = asyncio.run(_run())
    assert report.verdict == "PASS"
    assert report.prompt_version == GROUNDING_PROMPT_V


def test_check_grounding_block_on_bad_json():
    async def _run():
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=MagicMock(content=[MagicMock(text="not json")]))
        return await check_grounding(PREOP_SD, "script", "pre_op", client=client)

    report = asyncio.run(_run())
    assert report.verdict == "BLOCK"
    assert any("inspector_unavailable" in f for f in report.critical_failures)


def test_check_grounding_block_missing_med():
    async def _run():
        items = build_required_items(CARDIAC_SD, "post_op_treatment")
        coverage = [{"id": i["id"], "category": i["category"], "status": "MISSING", "severity": i["severity"], "evidence": None} for i in items]
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=_mock_response(
                "BLOCK",
                track="post_op_treatment",
                coverage=coverage,
                critical_failures=["missing new medication Aspirin"],
            )
        )
        return await check_grounding(CARDIAC_SD, "Take it easy after your procedure.", "post_op_treatment", client=client)

    report = asyncio.run(_run())
    assert report.verdict == "BLOCK"


def test_check_grounding_block_fabricated_doctor():
    async def _run():
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=_mock_response(
                "BLOCK",
                faithfulness=[
                    {"claim": "Dr. Smith", "status": "UNSUPPORTED", "claim_type": "doctor_name", "severity": "CRITICAL"}
                ],
                critical_failures=["fabricated provider Dr. Smith"],
            )
        )
        return await check_grounding(CARDIAC_SD, "Follow up with Dr. Smith", "post_op_treatment", client=client)

    report = asyncio.run(_run())
    assert report.verdict == "BLOCK"


def test_check_grounding_block_threshold_drift():
    async def _run():
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=_mock_response(
                "BLOCK",
                faithfulness=[
                    {"claim": "101.4°F", "status": "UNSUPPORTED", "claim_type": "threshold", "severity": "CRITICAL"}
                ],
                critical_failures=["fever threshold drift"],
            )
        )
        return await check_grounding(
            CARDIAC_SD, "Call if fever above 101.4 degrees", "post_op_treatment", client=client
        )

    report = asyncio.run(_run())
    assert report.verdict == "BLOCK"


def test_check_grounding_block_direction_reversal():
    async def _run():
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=_mock_response("BLOCK", critical_failures=["warfarin direction reversed"])
        )
        return await check_grounding(
            CARDIAC_SD, "Continue your warfarin as usual", "post_op_treatment", client=client
        )

    report = asyncio.run(_run())
    assert report.verdict == "BLOCK"


def test_check_grounding_review_missing_followup():
    async def _run():
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=_mock_response("REVIEW", critical_failures=[]))
        return await check_grounding(CARDIAC_SD, "script without followup", "post_op_treatment", client=client)

    report = asyncio.run(_run())
    assert report.verdict == "REVIEW"


def test_assert_script_is_grounded_raises():
    report = GroundingReport(
        track="pre_op",
        coverage=[],
        faithfulness=[],
        critical_failures=["x"],
        verdict="BLOCK",
        summary="blocked",
    )
    with pytest.raises(ValueError, match="blocked"):
        assert_script_is_grounded(report)


def test_parse_grounding_response_with_fences():
    required = build_required_items(PREOP_SD, "pre_op")
    raw = "```json\n" + json.dumps(
        {
            "track": "pre_op",
            "coverage": [],
            "faithfulness": [],
            "critical_failures": [],
            "verdict": "PASS",
            "summary": "ok",
        }
    ) + "\n```"
    report = parse_grounding_response(raw, "pre_op", required)
    assert report.verdict == "PASS"
    assert report.required_items == required


def test_check_grounding_routes_through_call_llm_with_provenance(monkeypatch):
    async def _fake_call_llm(**kwargs):
        resp = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps({
            "track": "pre_op",
            "coverage": [],
            "faithfulness": [],
            "critical_failures": [],
            "verdict": "PASS",
            "summary": "ok",
        }))])
        _fake_call_llm.kwargs = kwargs
        return resp, {}

    monkeypatch.setattr("pipeline.grounding_check.call_llm", _fake_call_llm)
    report = asyncio.run(
        check_grounding(PREOP_SD, "script", "pre_op", patient_id="p_ground")
    )
    assert report.verdict == "PASS"
    assert _fake_call_llm.kwargs["prompt_id"] == "grounding_judge"
    assert _fake_call_llm.kwargs["patient_id"] == "p_ground"
