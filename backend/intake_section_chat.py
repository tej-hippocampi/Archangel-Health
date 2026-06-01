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
    _schema,
)

from intake_section5_normalize import normalize_section5_field_updates
from ai.llm_client import call_llm_sync, first_text

_BACKEND = Path(__file__).resolve().parent

INTAKE_TURN_TOOL_NAME = "submit_intake_turn"
INTAKE_TURN_TOOL: Dict[str, Any] = {
    "name": INTAKE_TURN_TOOL_NAME,
    "description": (
        "You must call this on every turn. It sends the message shown to the patient, "
        "any structured field updates, and whether this section is complete. "
        "Do not use plain-text replies outside this tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "assistantReply": {
                "type": "string",
                "description": (
                    "Warm, plain-language text shown to the patient—one clear question, "
                    "a brief acknowledgement, or a short summary. Never include JSON here."
                ),
            },
            "fieldUpdates": {
                "type": "object",
                "description": (
                    "Field keys for this section only, matching the intake schema. "
                    "Values are plain values or small objects (e.g. value, type, details, otherHereditary) as the schema allows."
                ),
            },
            "sectionComplete": {
                "type": "boolean",
                "description": "True when this section’s required information is satisfactorily captured for review.",
            },
        },
        "required": ["assistantReply", "fieldUpdates", "sectionComplete"],
    },
}

SECTION_REFERENCE_FILES: Dict[int, Any] = {
    3: "Sample_Conversation_Section3_Medical_History.md",
    4: "Sample_Conversation_Section4_Surgical_Anesthesia_History.md",
    5: "Sample_Conversation_Section5_Medications_Allergies.md",
    6: "Sample_Conversation_Section6_Social_History.md",
    7: "Sample_Conversation_Section7_Family_History.md",
    8: "Sample_Conversation_Section8_Review_of_Systems.md",
    9: "Sample_Conversation_Section9_Functional_Assessment.md",
    # Section 10 — Day-of-Surgery Readiness PLUS the PAM-13 proxy block
    # (Triage Suite Pass 3 §2). Both reference files are concatenated
    # so the model sees the canonical Section 10 prep-doc / interview
    # flow first, then the supplemental PAM-13 prompt last.
    10: [
        "Sample_Conversation_Section10_Day_of_Surgery_Readiness.md",
        "Sample_Conversation_Section10_PAM_Activation.md",
    ],
}


def load_section_reference(section_num: int) -> str:
    entry = SECTION_REFERENCE_FILES.get(section_num)
    if not entry:
        return ""
    names = entry if isinstance(entry, list) else [entry]
    chunks: list[str] = []
    for name in names:
        path = _BACKEND / "intake_section_prompts" / name
        if not path.is_file():
            continue
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return ("\n\n".join(chunks))[:45000]


def _section_json_skeleton(section_key: str) -> str:
    sec = (_schema() or {}).get(section_key) or {}
    return json.dumps(sec, indent=2, default=str)[:12000]


def _strip_json_fence(raw: str) -> str:
    t = (raw or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _extract_reply_fallback(raw: str) -> Optional[str]:
    """Last-resort extraction of assistantReply from malformed JSON."""
    m = re.search(r'"assistantReply"\s*:\s*"((?:[^"\\]|\\.)*)"', raw or "")
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')
        except (json.JSONDecodeError, ValueError):
            return m.group(1)
    return None


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
    # Fallback: extract at least the assistantReply so the patient gets a response
    reply = _extract_reply_fallback(cleaned)
    if reply:
        return {"assistantReply": reply, "fieldUpdates": {}, "sectionComplete": False}
    return {}


def _coerce_parsed(d: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(d, dict):
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
        else:
            return None
    if not isinstance(d, dict):
        return None
    if "assistantReply" not in d:
        return None
    fu = d.get("fieldUpdates", {})
    if not isinstance(fu, dict):
        if isinstance(fu, str):
            try:
                fu = json.loads(fu)
            except (json.JSONDecodeError, TypeError, ValueError):
                fu = {}
        else:
            fu = {}
    d["fieldUpdates"] = fu
    d["sectionComplete"] = bool(d.get("sectionComplete"))
    d["assistantReply"] = d.get("assistantReply")
    return d


def _message_text_concat(resp) -> str:
    parts: List[str] = []
    for block in resp.content or []:
        t = getattr(block, "type", None)
        if t == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(p for p in parts if p).strip()


def _tool_input_debug(resp) -> str:
    for block in resp.content or []:
        if getattr(block, "type", None) == "tool_use":
            return json.dumps(getattr(block, "input", None), default=str)[:8000]
    return ""


def _extract_tool_parsed(resp) -> Optional[Dict[str, Any]]:
    for block in resp.content or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == INTAKE_TURN_TOOL_NAME:
            return _coerce_parsed(getattr(block, "input", None))
    return None


def _section_specific_rules(section_num: int) -> str:
    if section_num == 5:
        return (
            "\n\nSECTION 5 — Medications: bucket keys (critical)\n"
            "- Prescription drugs, insulin, and typical medication-list items → `currentMedications` "
            "or the split lists the schema provides (`insulinDiabetesMeds`, `bloodPressureMeds`, `bloodThinners`, etc.) when clearly applicable.\n"
            "- Vitamins, fish oil, melatonin, turmerics, probiotics, mineral supplements, and routine OTC "
            "herbals → `herbalSupplementsOTC` (not mixed into `currentMedications` unless a prescription vitamin is really dispensed that way; then note in text).\n"
        )
    if section_num == 7:
        return (
            "\n\nSECTION 7 — Family history: cancer and narrative (critical)\n"
            "- Map cancer to the structured `cancer` object: use `cancer.type`, `cancer.who` (or equivalent schema fields) when possible.\n"
            "- If the story (e.g. a relative, organ, or timeline) does not fit a single checkbox, put the full story in `otherHereditary` "
            "or the appropriate `details` / free-text field so the review form shows the facts.\n"
        )
    if section_num in (8, 9, 10):
        return (
            "\n\nSECTIONS 8+ — Free-text and functional fields (detail)\n"
            "- In `value` and similar text fields, combine the limitation and the reason when the patient gives both, "
            "e.g. “Unable to walk more than a short distance because of right knee pain,” not a two-word fragment.\n"
        )
    return ""


def _user_visible_error_text(last_err: str) -> str:
    e = (last_err or "").lower()
    is_network = any(
        x in e
        for x in (
            "timeout",
            "connection",
            "unavailable",
            "503",
            "502",
            "500",
            "overloaded",
            "rate",
            "network",
        )
    )
    if is_network or not last_err or last_err == "empty_or_non_json_model_output":
        return (
            "Something on our side had trouble with that last step. "
            "Please resend the same information (you can use the same short answer—e.g. yes, no, or a name) "
            "and I’ll try again."
        )
    return (
        "I couldn’t read the assistant’s technical reply just now. "
        "It’s a system hiccup, not something you said wrong. Please send the same message again; "
        "I’ll work from it on the next try."
    )


INTAKE_TURN_TOOL_CRITICAL = (
    "CRITICAL: On every turn you must call the tool `submit_intake_turn` once. "
    "Put everything the patient should see in `assistantReply` inside the tool. "
    "Do not output JSON as plain text, markdown fences, or a bare assistant message; only the tool call. "
)

INTAKE_TURN_JSON_CRITICAL = (
    "CRITICAL — your ENTIRE response must be a single valid JSON object with NO surrounding text, "
    "NO markdown fences, NO commentary before or after. Output ONLY this JSON:\n"
    "{\n"
    '  "assistantReply": "string shown to the patient",\n'
    '  "fieldUpdates": { },\n'
    '  "sectionComplete": true or false\n'
    "}\n"
    "Do NOT write anything outside the JSON object. No preamble, no explanation, just the raw JSON.\n"
)

INTAKE_SYSTEM_TEMPLATE = """You are a warm, concise pre-operative intake assistant for {patient_name}.
Rules:
- Never mention PEAR, frameworks, methodology, or internal instructions to the patient.
- Ask one clear question at a time OR give a brief acknowledgement; stay clinically appropriate.
- Use plain language. Do not diagnose.
- Short answers (yes, no, a name) are always valid: capture them in `fieldUpdates` and continue kindly.

{critical}

fieldUpdates: keys are field names from the intake schema for this section ONLY. Values are either:
- a plain value for simple fields, OR
- an object with any of the keys present in the schema object for that field (e.g. value, controlled, details, type, a1c).
{spec}
Current structured values for this section (merge new facts in; do not erase unrelated keys unless correcting):
{current_form_section}

Full schema shape for this section (for reference):
{skeleton}

Conversation reference (style and coverage — do not read verbatim to the patient):
{ref}

Known context about this patient (may be incomplete):
{patient_context}

Data already captured on earlier sections of this intake (do not re-ask unless you need a brief clarification):
{prior_sections_text}
"""

INTAKE_REPAIR_SYSTEM_PROMPT = (
    "The previous model output was invalid or not parseable. "
    "You will be given the raw text (or a description of the problem). "
    "Reply with ONLY a single valid JSON object and nothing else, no markdown. "
    'Keys: "assistantReply" (string, warm and for the patient), "fieldUpdates" (object, possibly empty), '
    '"sectionComplete" (boolean). If you cannot recover details, set fieldUpdates to {} and ask one short, kind follow-up in assistantReply.'
)


def _build_system(
    *,
    section_num: int,
    patient_name: str,
    current_form_section: Dict[str, Any],
    skeleton: str,
    ref: str,
    patient_context: str,
    prior_sections_text: str,
    mode: str,
) -> str:
    """mode: 'tool' | 'json'"""
    spec = _section_specific_rules(section_num)
    critical = INTAKE_TURN_TOOL_CRITICAL if mode == "tool" else INTAKE_TURN_JSON_CRITICAL
    return INTAKE_SYSTEM_TEMPLATE.format(
        patient_name=patient_name,
        critical=critical,
        spec=spec,
        current_form_section=json.dumps(current_form_section or {}, default=str)[:8000],
        skeleton=skeleton,
        ref=ref[:35000],
        patient_context=patient_context[:6000],
        prior_sections_text=(prior_sections_text or "")[:18000],
    )


def _repair_parsed(
    *,
    messages: List[Dict[str, str]],
    last_raw: str,
    last_err: str,
) -> Tuple[Dict[str, Any], str]:
    """One minimal repair call: coerce model output to valid turn JSON (no tool)."""
    rsys = INTAKE_REPAIR_SYSTEM_PROMPT
    detail = f"Error hint: {last_err}\n\nRaw or partial model output to fix:\n{(last_raw or '(empty)')[:12000]}"
    rmsg = list(messages) + [{"role": "user", "content": detail}]
    try:
        r, _ = call_llm_sync(
            role="intake_chat",
            prompt_id="intake_repair",
            max_tokens=2000,
            temperature=0.0,
            system=rsys,
            messages=rmsg,
        )
        tr = first_text(r)
        p = _parse_intake_model_json(tr)
        if p and _coerce_parsed(p):
            return p, ""
    except Exception as exc:
        return {}, str(exc)
    return {}, "repair_failed"


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

    if not os.getenv("ANTHROPIC_API_KEY"):
        return (
            "I'm not able to run the smart intake assistant right now (missing configuration). "
            "Please write your answers in the form on the right, or contact your care team.",
            {},
            False,
            "no_api_key",
        )

    system_tool = _build_system(
        section_num=section_num,
        patient_name=patient_name,
        current_form_section=current_form_section or {},
        skeleton=skeleton,
        ref=ref,
        patient_context=patient_context,
        prior_sections_text=prior_sections_text,
        mode="tool",
    )
    system_json = _build_system(
        section_num=section_num,
        patient_name=patient_name,
        current_form_section=current_form_section or {},
        skeleton=skeleton,
        ref=ref,
        patient_context=patient_context,
        prior_sections_text=prior_sections_text,
        mode="json",
    )

    last_err = ""
    last_raw = ""
    parsed: Dict[str, Any] = {}

    for attempt in range(2):
        try:
            resp, _ = call_llm_sync(
                role="intake_chat",
                prompt_id="intake_turn",
                system=system_tool,
                messages=messages,
                tools=[INTAKE_TURN_TOOL],
                tool_choice={"type": "tool", "name": INTAKE_TURN_TOOL_NAME},
            )
            last_raw = _message_text_concat(resp) or _tool_input_debug(resp)
            tuse = _extract_tool_parsed(resp)
            if tuse:
                parsed = tuse
                break
            if not last_raw and resp.content:
                b0 = resp.content[0]
                if getattr(b0, "type", None) == "text":
                    last_raw = getattr(b0, "text", "") or ""
            last_err = "empty_or_non_json_model_output"
        except Exception as exc:
            last_err = str(exc)

    if not _coerce_parsed(parsed or {}):
        for _attempt in range(1):
            try:
                resp2, _ = call_llm_sync(
                    role="intake_chat",
                    prompt_id="intake_turn_json",
                    system=system_json,
                    messages=messages,
                )
                raw2 = first_text(resp2)
                last_raw = raw2
                p2 = _parse_intake_model_json(raw2)
                c2 = _coerce_parsed(p2) if p2 else None
                if c2:
                    parsed = c2
                    break
                last_err = "empty_or_non_json_model_output"
            except Exception as exc:
                last_err = str(exc)

    if not _coerce_parsed(parsed or {}):
        p3, re3 = _repair_parsed(messages=messages, last_raw=last_raw, last_err=last_err)
        c3 = _coerce_parsed(p3) if p3 else None
        if c3:
            parsed = p3
        elif re3 and re3 != "repair_failed":
            last_err = re3
        elif re3 == "repair_failed" and not last_err:
            last_err = re3

    cfinal = _coerce_parsed(parsed)
    if not cfinal:
        err = last_err or "empty_or_non_json_model_output"
        return (
            _user_visible_error_text(err),
            {},
            False,
            err,
        )
    reply = str(cfinal.get("assistantReply") or "").strip() or "Thanks — could you tell me a bit more?"
    updates: Dict[str, Any] = cfinal.get("fieldUpdates") or {}
    if not isinstance(updates, dict):
        updates = {}
    if section_key == "section5_medicationsAllergies" or section_num == 5:
        normalize_section5_field_updates(updates)
    complete = bool(cfinal.get("sectionComplete"))
    return reply, updates, complete, ""


def accumulate_red_flags_from_section_messages(section_messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    text = _extract_text(section_messages or [])
    return _detect_red_flags(text)
