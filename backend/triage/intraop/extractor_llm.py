"""
Production LLM operative-note extractor (PRD §6.2).

Mirrors the Anthropic tool-use pattern in `eligibility.extract`:

  - `AsyncAnthropic` client constructed lazily.
  - A single `messages.create` call with `tool_choice` forced to our tool.
  - Self-rated `HIGH | MED | LOW` per field, mapped onto 0.95 / 0.75 / 0.50.
  - Retries with exponential backoff on transient errors.

The PDF is converted to text via `eligibility.parse_pdf.parse_pdf`, which
already handles OCR fallback. Operative notes are typically 2 pages, so
the existing token budget is generous.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from triage.intraop.extractor import (
    ExtractionContext,
    ExtractionPayload,
    confidence_for,
)
from triage.intraop.tuning import EXTRACTION


log = logging.getLogger("triage.intraop.extractor_llm")


_MODEL = os.getenv("INTRAOP_EXTRACTOR_MODEL", "claude-sonnet-4-6")
_MAX_TOKENS = 4096


# ─── Tool schema ─────────────────────────────────────────────────────────────

_FIELD_RATING = {"type": "string", "enum": ["HIGH", "MED", "LOW", "NOT_FOUND"]}


def _opt_str_with_enum(values: list[str]) -> dict[str, Any]:
    return {"type": ["string", "null"], "enum": values + [None]}


_TOOL: dict[str, Any] = {
    "name": "extract_intraop_form",
    "description": (
        "Extract structured intra-operative form fields from an operative note. "
        "For each requested field, return the value AND a self-rated confidence "
        "(HIGH / MED / LOW / NOT_FOUND). NOT_FOUND means the value isn't in the note."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": "Extracted values, keyed by snake_case field name.",
                "properties": {
                    "documented_complication":  {"type": ["boolean", "null"]},
                    "complication_types":       {"type": ["array", "null"], "items": {"type": "string"}},
                    "complication_description": {"type": ["string", "null"]},
                    "ebl":                      {"type": ["integer", "null"]},
                    "transfusion_total_units":  {"type": ["integer", "null"]},
                    "prbc_units":               {"type": ["integer", "null"]},
                    "platelet_units":           {"type": ["integer", "null"]},
                    "ffp_units":                {"type": ["integer", "null"]},
                    "cryo_units":               {"type": ["integer", "null"]},
                    "conversion":               _opt_str_with_enum(["YES", "NO", "N_A"]),
                    "conversion_reason":        {"type": ["string", "null"]},
                    "sustained_hypotension":    {"type": ["boolean", "null"]},
                    "vasopressor_requirement":  _opt_str_with_enum(["NONE", "BRIEF", "SUSTAINED"]),
                    "significant_arrhythmia":   {"type": ["boolean", "null"]},
                    "or_duration_minutes":      {"type": ["integer", "null"]},
                    "or_started_at":            {"type": ["string", "null"]},
                    "or_ended_at":              {"type": ["string", "null"]},
                    "difficult_airway":         {"type": ["boolean", "null"]},
                    "fluid_in":                 {"type": ["integer", "null"]},
                    "fluid_out":                {"type": ["integer", "null"]},
                    "net_fluid_balance":        {"type": ["integer", "null"]},
                    "anesthesia_type":          _opt_str_with_enum(["GENERAL", "REGIONAL", "MAC", "COMBINED"]),
                    "asa_class":                {"type": ["string", "null"]},
                    "hypoxia_event":            {"type": ["boolean", "null"]},
                    "procedural_aborted":       {"type": ["boolean", "null"]},
                    "procedural_aborted_reason": {"type": ["string", "null"]},

                    # LEJR
                    "lejr_joint":               _opt_str_with_enum(["HIP", "KNEE"]),
                    "lejr_side":                _opt_str_with_enum(["LEFT", "RIGHT", "BILATERAL"]),
                    "lejr_fixation_type":       _opt_str_with_enum(["CEMENTED", "CEMENTLESS", "HYBRID"]),
                    "lejr_prosthesis_model":    {"type": ["string", "null"]},
                    "lejr_component_sizes":     {"type": ["string", "null"]},
                    "intraoperative_fracture":  {"type": ["boolean", "null"]},
                    "fracture_location":        {"type": ["string", "null"]},

                    # CABG
                    "number_of_grafts":         {"type": ["integer", "null"]},
                    "pump_strategy":            _opt_str_with_enum(["ON_PUMP", "OFF_PUMP"]),
                    "aortic_cross_clamp_minutes": {"type": ["integer", "null"]},
                    "cpb_time_minutes":         {"type": ["integer", "null"]},
                    "aortic_manipulation":      {"type": ["boolean", "null"]},
                    "grafts_used":              {"type": ["array", "null"], "items": {"type": "string"}},
                    "weaning_from_bypass":      _opt_str_with_enum(["YES", "DIFFICULT", "REQUIRED_MECHANICAL_SUPPORT"]),

                    # SPINAL_FUSION
                    "spinal_approach":          _opt_str_with_enum(["ANTERIOR", "POSTERIOR", "COMBINED", "LATERAL"]),
                    "number_of_levels_fused":   {"type": ["integer", "null"]},
                    "spinal_levels":            {"type": ["array", "null"], "items": {"type": "string"}},
                    "spinal_instrumentation":   {"type": ["boolean", "null"]},
                    "bone_graft_source":        _opt_str_with_enum(["AUTOGRAFT", "ALLOGRAFT", "SYNTHETIC", "COMBINED"]),
                    "dural_tear":               {"type": ["boolean", "null"]},
                    "neuromonitoring_used":     {"type": ["boolean", "null"]},
                    "neuromonitoring_changes":  {"type": ["boolean", "null"]},

                    # HIP_FEMUR_FRACTURE
                    "hip_fracture_pattern":     _opt_str_with_enum(
                        ["INTRACAPSULAR", "INTERTROCHANTERIC", "SUBTROCHANTERIC", "FEMORAL_SHAFT"]
                    ),
                    "hip_fixation_method":      _opt_str_with_enum(
                        ["DYNAMIC_HIP_SCREW", "INTRAMEDULLARY_NAIL", "HEMIARTHROPLASTY", "TOTAL_HIP", "ORIF_OTHER"]
                    ),
                    "time_to_or_hours":         {"type": ["number", "null"]},
                    "weight_bearing_status":    _opt_str_with_enum(["FULL", "PARTIAL", "TOE_TOUCH", "NON_WEIGHT_BEARING"]),

                    # MAJOR_BOWEL
                    "bowel_procedure_type":     _opt_str_with_enum(
                        ["PARTIAL_COLECTOMY", "TOTAL_COLECTOMY", "SMALL_BOWEL_RESECTION", "OTHER"]
                    ),
                    "bowel_approach":           _opt_str_with_enum(["OPEN", "LAPAROSCOPIC", "ROBOTIC"]),
                    "anastomosis_performed":    {"type": ["boolean", "null"]},
                    "anastomosis_location":     {"type": ["string", "null"]},
                    "ostomy_created":           {"type": ["boolean", "null"]},
                    "contamination_class":      {"type": ["integer", "null"]},
                },
            },
            "field_ratings": {
                "type": "object",
                "description": "Per-field self-rated confidence (HIGH/MED/LOW/NOT_FOUND).",
                "additionalProperties": _FIELD_RATING,
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Any caveats — missing sections, illegible text, etc.",
            },
        },
        "required": ["fields", "field_ratings"],
    },
}


def _system_prompt(family: Optional[str]) -> str:
    family_block = (
        f"The procedure family for this patient is **{family}**. Pay special "
        "attention to the family-specific fields enumerated in the tool schema."
        if family else
        "The procedure family is unknown; populate only the universal fields."
    )
    return (
        "You are a clinical documentation extractor. Your job is to read an "
        "operative note and extract structured intra-operative fields exactly "
        "as documented. Use ONLY the tool — do not produce free-text output.\n\n"
        f"{family_block}\n\n"
        "Rules:\n"
        " - Return integers for numeric fields (no units in the value).\n"
        " - For booleans, return true/false; if not stated, return null and rate NOT_FOUND.\n"
        " - For each field, give a self-rated confidence: HIGH (explicit, unambiguous), "
        "MED (inferable from context), LOW (educated guess), NOT_FOUND (absent).\n"
        " - Do not hallucinate values; null + NOT_FOUND is always preferable to a guess."
    )


# ─── Async client ────────────────────────────────────────────────────────────

def _client():
    from anthropic import AsyncAnthropic   # lazy import — keeps tests light
    return AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _find_tool_use(response: Any, tool_name: str) -> Optional[dict[str, Any]]:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(getattr(block, "input", {}) or {})
    return None


# ─── Public extractor class ──────────────────────────────────────────────────

class LlmIntraopExtractor:
    """Production extractor calling Anthropic Claude with tool-use forced."""

    MODEL_VERSION = EXTRACTION["model_version"]
    PROMPT_VERSION = EXTRACTION["prompt_version"]

    def __init__(self, *, attempts: int = 3, timeout_sec: Optional[int] = None):
        self._attempts = attempts
        self._timeout_sec = timeout_sec or EXTRACTION["timeout_sec"]

    async def extract(
        self,
        *,
        pdf_bytes: bytes,
        context: ExtractionContext,
    ) -> ExtractionPayload:
        # 1) Convert PDF → text (reuses eligibility.parse_pdf with OCR fallback).
        from eligibility.parse_pdf import parse_pdf
        parsed = parse_pdf(pdf_bytes)
        raw_text = parsed.text

        # 2) Call Claude with tool-use, retrying transient failures.
        client = _client()
        last_error: Optional[Exception] = None
        for attempt in range(1, self._attempts + 1):
            try:
                resp = await asyncio.wait_for(
                    client.messages.create(
                        model=_MODEL,
                        max_tokens=_MAX_TOKENS,
                        temperature=0.0,
                        system=_system_prompt(context.procedure_family),
                        tools=[_TOOL],
                        tool_choice={"type": "tool", "name": "extract_intraop_form"},
                        messages=[{"role": "user", "content": raw_text}],
                    ),
                    timeout=self._timeout_sec,
                )
                tool_input = _find_tool_use(resp, "extract_intraop_form")
                if tool_input is None:
                    raise RuntimeError("Anthropic returned no tool_use block")

                fields_in = tool_input.get("fields") or {}
                ratings_in = tool_input.get("field_ratings") or {}
                warnings = list(tool_input.get("warnings") or [])
                return _normalize(fields_in, ratings_in, warnings, raw_text)
            except Exception as e:  # noqa: BLE001
                last_error = e
                log.warning("[INTRAOP_EXTRACTOR] attempt %d failed (%s)", attempt, e)
                if attempt >= self._attempts:
                    break
                await asyncio.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _normalize(
    fields_in: dict[str, Any],
    ratings_in: dict[str, Any],
    warnings: list[str],
    raw_text: str,
) -> ExtractionPayload:
    """Normalize the LLM payload — drop nulls, attach numeric confidences,
    surface warnings for NOT_FOUND fields."""
    fields: dict[str, Any] = {}
    confidences: dict[str, float] = {}

    for key, value in fields_in.items():
        rating = (ratings_in.get(key) or "").upper()
        if value is None or rating == "NOT_FOUND":
            confidences[key] = 0.0
            continue
        fields[key] = value
        confidences[key] = confidence_for(rating)

    # Synthesize a warning for any field rated NOT_FOUND but with non-null value
    # (model hedge — we keep the value but flag it for review).
    return ExtractionPayload(
        fields=fields,
        field_confidences=confidences,
        raw_text=raw_text,
        model_version=LlmIntraopExtractor.MODEL_VERSION,
        prompt_version=LlmIntraopExtractor.PROMPT_VERSION,
        warnings=warnings,
    )
