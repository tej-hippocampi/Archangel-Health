"""Anthropic tool-use extraction for the 6 TEAM eligibility dimensions.

PRD §7.2. Uses the same AsyncAnthropic client pattern as pipeline/generate.py
and the same model ("claude-sonnet-4-6") used in intake_section_chat.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from prompts.eligibility import (
    ELIGIBILITY_IDENTITY_SYSTEM_PROMPT,
    ELIGIBILITY_SEGMENTS_SYSTEM_PROMPT,
    ELIGIBILITY_SYSTEM_PROMPT,
)

log = logging.getLogger("eligibility.extract")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

_COVERAGE_PROPS = {
    "status": {"type": "string", "enum": ["ACTIVE", "INACTIVE", "UNKNOWN"]},
    "effectiveDate": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
    "terminationDate": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
    "sourceExcerpt": {"type": "string", "description": "Verbatim excerpt, <=200 chars"},
}

EXTRACT_TOOL: Dict[str, Any] = {
    "name": "extract_team_eligibility",
    "description": "Extract Medicare eligibility fields needed to determine TEAM eligibility.",
    "input_schema": {
        "type": "object",
        "properties": {
            "partA": {
                "type": "object",
                "properties": _COVERAGE_PROPS,
                "required": ["status", "sourceExcerpt"],
            },
            "partB": {
                "type": "object",
                "properties": _COVERAGE_PROPS,
                "required": ["status", "sourceExcerpt"],
            },
            "medicareAdvantage": {
                "type": "object",
                "properties": {
                    "enrolled": {"type": "string", "enum": ["YES", "NO", "UNKNOWN"]},
                    "contractId": {"type": ["string", "null"]},
                    "planName": {"type": ["string", "null"]},
                    "sourceExcerpt": {"type": "string"},
                },
                "required": ["enrolled", "sourceExcerpt"],
            },
            "medicarePrimary": {
                "type": "object",
                "properties": {
                    "isPrimary": {"type": "string", "enum": ["YES", "NO", "UNKNOWN"]},
                    "secondaryReason": {"type": ["string", "null"]},
                    "sourceExcerpt": {"type": "string"},
                },
                "required": ["isPrimary", "sourceExcerpt"],
            },
            "esrdBasis": {
                "type": "object",
                "properties": {
                    "isESRDBasis": {"type": "string", "enum": ["YES", "NO", "UNKNOWN"]},
                    "sourceExcerpt": {"type": "string"},
                },
                "required": ["isESRDBasis", "sourceExcerpt"],
            },
            "umwa": {
                "type": "object",
                "properties": {
                    "isUMWA": {"type": "string", "enum": ["YES", "NO", "UNKNOWN"]},
                    "sourceExcerpt": {"type": "string"},
                },
                "required": ["isUMWA", "sourceExcerpt"],
            },
            "overallConfidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        },
        "required": [
            "partA",
            "partB",
            "medicareAdvantage",
            "medicarePrimary",
            "esrdBasis",
            "umwa",
            "overallConfidence",
        ],
    },
}


IDENTITY_TOOL: Dict[str, Any] = {
    "name": "extract_patient_identity",
    "description": "Extract a single patient's identity from one document or split.",
    "input_schema": {
        "type": "object",
        "properties": {
            "firstName": {"type": ["string", "null"]},
            "lastName": {"type": ["string", "null"]},
            "dob": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
            "mbi": {"type": ["string", "null"]},
            "surgeryDate": {"type": ["string", "null"]},
            "anchorProcedure": {
                "type": ["string", "null"],
                "enum": [None, "LEJR", "HIP_FEMUR", "SPINAL_FUSION", "CABG", "MAJOR_BOWEL"],
            },
            "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        },
        "required": ["confidence"],
    },
}


PATIENT_SEGMENTS_TOOL: Dict[str, Any] = {
    "name": "extract_patient_segments",
    "description": (
        "Detect every distinct patient in a document and return one entry per patient. "
        "Used by the group-upload pipeline to split multi-patient files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "firstName": {"type": ["string", "null"]},
                        "lastName": {"type": ["string", "null"]},
                        "dob": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
                        "mbi": {"type": ["string", "null"]},
                        "surgeryDate": {"type": ["string", "null"]},
                        "anchorProcedure": {
                            "type": ["string", "null"],
                            "enum": [None, "LEJR", "HIP_FEMUR", "SPINAL_FUSION", "CABG", "MAJOR_BOWEL"],
                        },
                        "sectionAnchor": {
                            "type": ["string", "null"],
                            "description": (
                                "60-120 char verbatim substring from the first line(s) of this patient's "
                                "section. Must be unique within the document so the host can locate it via "
                                "text.find(anchor)."
                            ),
                        },
                        "preOpInstructions": {
                            "type": ["string", "null"],
                            "description": (
                                "Verbatim full text of this patient's Pre-Operative Instructions / Prep "
                                "Notes section, or null if absent."
                            ),
                        },
                        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    },
                    "required": ["confidence"],
                },
            },
        },
        "required": ["patients"],
    },
}


def _client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _build_user_content(parsed_docs: List[str], freeform_notes: str = "") -> str:
    blocks: List[str] = []
    for i, doc_text in enumerate(parsed_docs, 1):
        blocks.append(f"--- DOCUMENT {i} ---\n{doc_text}")
    if freeform_notes and freeform_notes.strip():
        blocks.append(f"--- FREEFORM NOTES ---\n{freeform_notes.strip()}")
    if not blocks:
        blocks = ["(no documents or notes provided)"]
    return "\n\n".join(blocks)


def _find_tool_use(response: Any, tool_name: str) -> Optional[Dict[str, Any]]:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(getattr(block, "input", {}) or {})
    return None


async def _call_with_retry(
    client: AsyncAnthropic,
    *,
    system: str,
    user: str,
    tool: Dict[str, Any],
    tool_name: str,
    attempts: int = 3,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            log.info(
                "[ELIGIBILITY] Sending %d chars to Anthropic (attempt %d, tool=%s)",
                len(user),
                attempt,
                tool_name,
            )
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=0.0,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}],
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                log.info(
                    "[ELIGIBILITY] tokens in=%s out=%s stop=%s",
                    getattr(usage, "input_tokens", "?"),
                    getattr(usage, "output_tokens", "?"),
                    getattr(resp, "stop_reason", "?"),
                )
            tool_input = _find_tool_use(resp, tool_name)
            if tool_input is None:
                raise RuntimeError("Anthropic returned no tool_use block")
            return {"extracted": tool_input, "request_id": getattr(resp, "id", None)}
        except Exception as e:  # noqa: BLE001 — intentionally broad: rate-limit, 5xx, etc.
            last_error = e
            if attempt >= attempts:
                break
            backoff = 2**attempt
            log.warning("[ELIGIBILITY] attempt %d failed (%s) — retrying in %ss", attempt, e, backoff)
            await asyncio.sleep(backoff)
    assert last_error is not None
    raise last_error


async def extract_eligibility(parsed_docs: List[str], surgery_date: str, freeform_notes: str = "") -> Dict[str, Any]:
    """Call Anthropic with the extraction tool; return the raw tool input + req id."""
    system = ELIGIBILITY_SYSTEM_PROMPT.replace("{{SURGERY_DATE}}", surgery_date or "(not provided)")
    user = _build_user_content(parsed_docs, freeform_notes)
    return await _call_with_retry(
        _client(),
        system=system,
        user=user,
        tool=EXTRACT_TOOL,
        tool_name="extract_team_eligibility",
    )


async def extract_identity(doc_text: str) -> Dict[str, Any]:
    """Identity-fanout call used by the group-upload pipeline."""
    return await _call_with_retry(
        _client(),
        system=ELIGIBILITY_IDENTITY_SYSTEM_PROMPT,
        user=doc_text,
        tool=IDENTITY_TOOL,
        tool_name="extract_patient_identity",
    )


async def extract_patient_segments(doc_text: str) -> Dict[str, Any]:
    """Multi-patient segmentation call used by the group-upload pipeline.

    Returns ``{"extracted": {"patients": [...]}, "request_id": ...}``. The
    list has one entry per distinct patient detected in the document. Single-
    patient documents return a 1-element list. The host program splits the
    document by ``sectionAnchor`` substrings to produce per-patient slices for
    downstream eligibility extraction.
    """
    return await _call_with_retry(
        _client(),
        system=ELIGIBILITY_SEGMENTS_SYSTEM_PROMPT,
        user=doc_text,
        tool=PATIENT_SEGMENTS_TOOL,
        tool_name="extract_patient_segments",
    )
