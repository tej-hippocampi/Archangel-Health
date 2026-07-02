"""Async draft pipeline for Gold Standard visits (PRD §5.4).

Runs after End Visit + audio upload:

  audio → STT (swappable provider) → transcript
        → call_llm(role="gold_draft_note") → draft SOAP note + suggested codes

Progress is streamed to the UI over SSE using the same status/result/error event
shape as the eligibility ``/stream`` endpoint. The draft is scaffolding only; it
is never exported as truth.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from compliance.subprocessors import SubprocessorPHIError
from gold import store
from integrations.stt import get_stt_provider


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).rstrip("`").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None


def _emit(visit_id: str, event: str, **data: Any) -> None:
    store.emit(visit_id, event, data)


async def _draft_note(transcript: str, *, visit_id: str) -> Dict[str, Any]:
    """Call the LLM for a structured draft note. Degrades to an empty scaffold
    if the LLM is unavailable (no key / error) so the surgeon still gets a
    review screen to write the gold note from scratch."""
    try:
        from ai.llm_client import call_llm, first_text
        from prompts.gold import GOLD_DRAFT_NOTE_SYSTEM
    except Exception:
        return {"note_text": "", "suggested_codes": []}
    try:
        resp, _rec = await call_llm(
            role="gold_draft_note",
            system=GOLD_DRAFT_NOTE_SYSTEM,
            messages=[{"role": "user", "content": transcript}],
            prompt_id="gold_draft_note",
            patient_id=visit_id,
            purpose="gold_draft",
        )
        parsed = _extract_json(first_text(resp)) or {}
    except Exception as exc:  # pragma: no cover - network/key dependent
        print(f"[gold.pipeline] draft note LLM failed: {exc!r}")
        return {"note_text": "", "suggested_codes": []}

    note = parsed.get("note") or {}
    note_text = parsed.get("note_text") or _join_sections(note)
    codes = parsed.get("suggested_codes") or []
    if not isinstance(codes, list):
        codes = []
    # Expose the pre-sectioned SOAP draft (B8.1) so the review UI renders one
    # card per section without re-splitting a string. Only the four canonical
    # sections, each a plain string.
    sections: Dict[str, str] = {}
    if isinstance(note, dict):
        for key in ("subjective", "objective", "assessment", "plan"):
            val = note.get(key)
            if isinstance(val, str) and val.strip():
                sections[key] = val.strip()
    return {"note_text": note_text, "suggested_codes": codes, "sections": sections}


def _join_sections(note: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key, label in (
        ("subjective", "Subjective"),
        ("objective", "Objective"),
        ("assessment", "Assessment"),
        ("plan", "Plan"),
    ):
        val = (note.get(key) or "").strip()
        if val:
            parts.append(f"{label}:\n{val}")
    return "\n\n".join(parts)


async def run_draft_pipeline(visit_id: str, audio_path: str, mime_type: str) -> None:
    """Transcribe + draft. Emits SSE status/result/error and persists results."""
    try:
        store.update_visit(visit_id, status=store.ST_DRAFTING, pipeline_error=None)
        _emit(visit_id, "status", stage="TRANSCRIBING", message="Transcribing audio…")

        provider = get_stt_provider()
        transcript = await provider.transcribe(audio_path=audio_path, mime_type=mime_type)

        turns_text = json.dumps(transcript.turns) if transcript.turns else None
        store.update_visit(
            visit_id,
            stt_provider=transcript.provider,
            transcript=transcript.text,
            transcript_turns=turns_text,
            audio_duration_sec=transcript.duration_sec,
        )
        if transcript.languages:
            existing = store.get_visit(visit_id) or {}
            if not existing.get("languages"):
                store.update_visit(visit_id, languages=transcript.languages)

        _emit(visit_id, "status", stage="DRAFTING", message="Generating draft note…")
        draft = await _draft_note(transcript.text, visit_id=visit_id)
        sections = draft.get("sections") or {}
        store.update_visit(
            visit_id,
            ai_draft_note=draft["note_text"],
            ai_draft_sections=(json.dumps(sections) if sections else None),
            suggested_codes=draft["suggested_codes"],
            status=store.ST_NEEDS_REVIEW,
        )
        _emit(
            visit_id,
            "result",
            status=store.ST_NEEDS_REVIEW,
            stt_provider=transcript.provider,
            is_stub=transcript.is_stub,
        )
    except SubprocessorPHIError as exc:
        # A2: configured STT vendor without a signed BAA — refuse and surface it.
        msg = "STT vendor has no BAA on file"
        print(f"[gold.pipeline] BAA gate blocked STT for {visit_id}: {exc!r}")
        store.update_visit(visit_id, status=store.ST_ERROR, pipeline_error=msg)
        _emit(visit_id, "error", message=msg)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[gold.pipeline] pipeline failed for {visit_id}: {exc!r}")
        store.update_visit(visit_id, status=store.ST_ERROR, pipeline_error=str(exc))
        _emit(visit_id, "error", message=str(exc))
    finally:
        # A6: pipeline reached a terminal state — release the live SSE queue so no
        # client blocks on it (the ring is kept briefly for late-connect replay).
        store.finalize_stream(visit_id)
