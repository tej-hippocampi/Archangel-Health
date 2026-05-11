"""
Tests for `LlmIntraopExtractor`.

The Anthropic SDK call is mocked out — we verify the wiring (PDF parse →
prompt → tool_use → normalization) without making a real network call.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop.extractor import ExtractionContext  # noqa: E402
from triage.intraop.extractor_llm import LlmIntraopExtractor, _normalize  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_normalize_drops_nulls_and_attaches_confidences():
    fields_in = {"ebl": 600, "documented_complication": False, "asa_class": None}
    ratings_in = {"ebl": "HIGH", "documented_complication": "MED", "asa_class": "NOT_FOUND"}
    out = _normalize(fields_in, ratings_in, [], "raw")
    assert out.fields == {"ebl": 600, "documented_complication": False}
    assert out.field_confidences["ebl"] == 0.95
    assert out.field_confidences["documented_complication"] == 0.75
    assert out.field_confidences["asa_class"] == 0.0
    assert "asa_class" not in out.fields


def test_normalize_preserves_warnings():
    out = _normalize({}, {}, ["EBL not stated"], "raw")
    assert "EBL not stated" in out.warnings


def test_normalize_uses_canonical_low_for_low_rating():
    out = _normalize({"or_duration_minutes": 90}, {"or_duration_minutes": "LOW"}, [], "")
    assert out.field_confidences["or_duration_minutes"] == 0.50


def _stub_response(tool_input: dict) -> object:
    """Build the minimal shape `LlmIntraopExtractor` needs from a Claude response."""
    block = types.SimpleNamespace(type="tool_use", name="extract_intraop_form", input=tool_input)
    return types.SimpleNamespace(content=[block], id="msg_x", usage=None, stop_reason="tool_use")


def test_extract_end_to_end_with_mocked_anthropic():
    """parse_pdf → Claude tool_use → ExtractionPayload."""
    fake_response = _stub_response({
        "fields": {
            "ebl": 600,
            "documented_complication": False,
            "or_duration_minutes": 90,
            "anesthesia_type": "GENERAL",
        },
        "field_ratings": {
            "ebl": "HIGH",
            "documented_complication": "HIGH",
            "or_duration_minutes": "MED",
            "anesthesia_type": "HIGH",
        },
        "warnings": ["ASA class not documented"],
    })

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    fake_parse_result = types.SimpleNamespace(
        text="OPERATIVE NOTE: TKA, EBL 600 ml, anesthesia general, OR time 90 min.",
        pages=2, ocr_used=False, ocr_unavailable=False,
    )

    with patch("triage.intraop.extractor_llm._client", return_value=fake_client), \
         patch("eligibility.parse_pdf.parse_pdf", return_value=fake_parse_result):
        extractor = LlmIntraopExtractor()
        ctx = ExtractionContext(patient_id="p1", procedure_family="LEJR")
        out = _run(extractor.extract(pdf_bytes=b"%PDF-1.4 fake", context=ctx))

    assert out.fields["ebl"] == 600
    assert out.field_confidences["ebl"] == 0.95
    assert out.field_confidences["or_duration_minutes"] == 0.75
    assert "ASA class not documented" in out.warnings
    assert out.model_version == "intraop-extractor@1.0.0"

    args, kwargs = fake_client.messages.create.await_args
    assert kwargs["tool_choice"] == {"type": "tool", "name": "extract_intraop_form"}
    assert kwargs["temperature"] == 0.0


def test_extract_retries_on_transient_failure_then_succeeds():
    """First call raises; second returns a valid tool_use."""
    fake_response = _stub_response({
        "fields": {"ebl": 100},
        "field_ratings": {"ebl": "HIGH"},
    })
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=[
        RuntimeError("transient 503"),
        fake_response,
    ])
    fake_parse_result = types.SimpleNamespace(text="x", pages=1, ocr_used=False, ocr_unavailable=False)

    with patch("triage.intraop.extractor_llm._client", return_value=fake_client), \
         patch("eligibility.parse_pdf.parse_pdf", return_value=fake_parse_result), \
         patch("triage.intraop.extractor_llm.asyncio.sleep", new=AsyncMock()):
        extractor = LlmIntraopExtractor(attempts=3)
        ctx = ExtractionContext(patient_id="p1", procedure_family="LEJR")
        out = _run(extractor.extract(pdf_bytes=b"x", context=ctx))

    assert out.fields["ebl"] == 100
    assert fake_client.messages.create.await_count == 2


def test_extract_raises_after_max_attempts():
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=RuntimeError("permanent"))
    fake_parse_result = types.SimpleNamespace(text="x", pages=1, ocr_used=False, ocr_unavailable=False)

    with patch("triage.intraop.extractor_llm._client", return_value=fake_client), \
         patch("eligibility.parse_pdf.parse_pdf", return_value=fake_parse_result), \
         patch("triage.intraop.extractor_llm.asyncio.sleep", new=AsyncMock()):
        extractor = LlmIntraopExtractor(attempts=2)
        ctx = ExtractionContext(patient_id="p1", procedure_family="LEJR")
        try:
            _run(extractor.extract(pdf_bytes=b"x", context=ctx))
        except RuntimeError as e:
            assert "permanent" in str(e)
        else:  # pragma: no cover - assertion lives in the except path
            raise AssertionError("expected RuntimeError after exhausted retries")
    assert fake_client.messages.create.await_count == 2
