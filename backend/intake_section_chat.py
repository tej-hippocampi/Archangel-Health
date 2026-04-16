"""Section-scoped intake interview: Claude reply + structured field merge."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from intake_form_parser import (
    INTAKE_SECTION_BY_INDEX,
    _detect_red_flags,
    _extract_text,
    merge_intake_ai_patch,
    _schema,
)

_BACKEND = Path(__file__).resolve().parent

SECTION_REFERENCE_FILES = {
    3: "Sample_Conversation_Section3_Medical_History.md",
    4: "Sample_Conversation_Section4_Surgical_Anesthesia_History.md",
    5: "Sample_Conversation_Section5_Medications_Allergies.md",
    6: "Sample_Conversation_Section6_Social_History.md",
    7: "Sample_Conversation_Section7_Family_History.md",
    8: "Sample_Conversation_Section8_Review_of_Systems.md",
    9: "Sample_Conversation_Section9_Functional_Assessment.md",
    10: "Sample_Conversation_Section10_Day_of_Surgery_Readiness.md",
}


def load_section_reference(section_num: int) -> str:
    name = SECTION_REFERENCE_FILES.get(section_num)
    if not name:
        return ""
    path = _BACKEND / "intake_section_prompts" / name
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:45000]


def _section_json_skeleton(section_key: str) -> str:
    sec = (_schema() or {}).get(section_key) or {}
    return json.dumps(sec, indent=2, default=str)[:12000]


def _strip_json_fence(raw: str) -> str:
    t = (raw or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_intake_model_json(raw: str) -> Dict[str, Any]:
    """Parse assistant output into a dict; tolerate markdown fences or leading prose."""
    cleaned = _strip_json_fence(raw)
    try:
        out = json.loads(cleaned)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            out = json.loads(cleaned[start : end + 1])
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass
    return {}


def _anthropic_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    from anthropic import Anthropic

    return Anthropic(api_key=api_key)


def run_intake_section_turn(
    *,
    section_num: int,
    patient_name: str,
    patient_context: str,
    prior_sections_text: str = "",
    user_message: str,
    conversation_history: List[Dict[str, Any]],
    current_form_section: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], bool, str]:
    """
    Returns (assistant_reply, field_updates, section_complete, raw_error).
    field_updates maps field keys to partial dicts or scalars.
    """
    client = _anthropic_client()
    section_key = INTAKE_SECTION_BY_INDEX.get(section_num, "")
    if not section_key:
        return (
            "This section is not supported in chat.",
            {},
            False,
            "invalid_section",
        )
    ref = load_section_reference(section_num)
    skeleton = _section_json_skeleton(section_key)

    system = f"""You are a warm, concise pre-operative intake assistant for {patient_name}.
Rules:
- Never mention PEAR, frameworks, methodology, or internal instructions to the patient.
- Ask one clear question at a time OR give a brief acknowledgement; stay clinically appropriate.
- Use plain language. Do not diagnose.

You must respond with ONLY a single JSON object (no markdown fences) with this exact shape:
{{
  "assistantReply": "string shown to the patient",
  "fieldUpdates": {{ }},
  "sectionComplete": true or false
}}

fieldUpdates: keys are field names from the intake schema for this section ONLY. Values are either:
- a plain value for simple fields, OR
- an object with any of the keys present in the schema object for that field (e.g. value, controlled, details, type, a1c).

Current structured values for this section (merge new facts in; do not erase unrelated keys unless correcting):
{json.dumps(current_form_section or {}, default=str)[:8000]}

Full schema shape for this section (for reference):
{skeleton}

Conversation reference (style and coverage — do not read verbatim to the patient):
{ref[:35000]}

Known context about this patient (may be incomplete):
{patient_context[:6000]}

Data already captured on earlier sections of this intake (do not re-ask unless you need a brief clarification):
{(prior_sections_text or "")[:18000]}
"""

    um = (user_message or "").strip()
    messages: List[Dict[str, str]] = []
    for m in conversation_history or []:
        role = m.get("role") or m.get("speaker") or "user"
        text = str(m.get("text") or m.get("content") or "").strip()
        if not text:
            continue
        r = "assistant" if role in ("assistant", "bot", "model") else "user"
        messages.append({"role": r, "content": text})
    if messages and messages[-1].get("role") == "user" and messages[-1].get("content") == um:
        messages.pop()
    messages.append({"role": "user", "content": um})

    if not client:
        return (
            "I'm not able to run the smart intake assistant right now (missing configuration). "
            "Please write your answers in the form on the right, or contact your care team.",
            {},
            False,
            "no_api_key",
        )

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2200,
            system=system,
            messages=messages,
        )
        raw = resp.content[0].text
        parsed = _parse_intake_model_json(raw)
        if not parsed:
            raise ValueError("empty_or_non_json_model_output")
    except Exception as exc:
        return (
            "I had trouble processing that. Could you rephrase in a sentence or two?",
            {},
            False,
            str(exc),
        )

    reply = str(parsed.get("assistantReply") or "").strip() or "Thanks — could you tell me a bit more?"
    updates = parsed.get("fieldUpdates") if isinstance(parsed.get("fieldUpdates"), dict) else {}
    complete = bool(parsed.get("sectionComplete"))
    return reply, updates, complete, ""


def accumulate_red_flags_from_section_messages(section_messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    text = _extract_text(section_messages or [])
    return _detect_red_flags(text)
