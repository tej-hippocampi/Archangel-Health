"""
Content Generation Layer
Purpose: Generate the voice script and battlecard HTML from structured clinical data.

Step 4 of the pipeline:
  structured_data + pipeline_type → (voice_script, battlecard_html)
"""

import os
from typing import Any, Dict, Tuple

from anthropic import Anthropic

from prompts.preop  import PREOP_VOICE_PROMPT,  PREOP_BATTLECARD_PROMPT
from prompts.postop import POSTOP_VOICE_PROMPT, POSTOP_BATTLECARD_PROMPT


class GenerationLayer:
    def __init__(self) -> None:
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    async def generate(
        self,
        structured_data: Dict[str, Any],
        pipeline_type: str,
    ) -> Tuple[str, str]:
        """
        Returns (voice_script, battlecard_html).
        voice_script → ElevenLabs TTS + Tavus knowledge base
        battlecard_html → rendered in patient dashboard tab
        """
        if pipeline_type == "pre_op":
            voice_sys      = PREOP_VOICE_PROMPT
            battlecard_sys = PREOP_BATTLECARD_PROMPT
        else:
            voice_sys      = POSTOP_VOICE_PROMPT
            battlecard_sys = POSTOP_BATTLECARD_PROMPT

        clinical_input = self._format_clinical_input(structured_data)

        # Generate voice script first
        voice_script = self._call_claude(
            system=voice_sys,
            user=f"[Clinical Input Layer]\n\n{clinical_input}\n\nGenerate the voice script.",
            max_tokens=2000,
        )

        # Generate battlecard from the voice script (battlecard extracts from script,
        # not raw EHR, to ensure 100% content alignment)
        battlecard_html = self._call_claude(
            system=battlecard_sys,
            user=f"[Voice Script]\n\n{voice_script}\n\nGenerate the battlecard HTML.",
            max_tokens=4000,
        )

        return voice_script, battlecard_html

    # ── Private ──────────────────────────────────────────────

    def _call_claude(self, system: str, user: str, max_tokens: int) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text

    def _format_clinical_input(self, d: Dict[str, Any]) -> str:
        """Convert structured EHR fields into a clean clinical text block for prompts."""
        lines = [
            f"Patient Name: {d.get('patient_name', 'Unknown')}",
            f"Procedure: {d.get('procedure_name', 'Unknown')}",
            f"Procedure Date: {d.get('procedure_date', 'Unknown')}",
            f"Status: {d.get('procedure_status', 'Unknown')}",
        ]

        diagnoses = d.get("key_diagnoses") or []
        if diagnoses:
            lines += ["", "KEY DIAGNOSES:"] + [f"  - {dx}" for dx in diagnoses]

        meds = d.get("medications") or []
        if meds:
            lines.append("\nMEDICATIONS:")
            for m in meds:
                tag   = m.get("status", "").upper()
                notes = f"  ({m.get('notes', '')})" if m.get("notes") else ""
                lines.append(
                    f"  [{tag}] {m.get('name','')} {m.get('dose','')} "
                    f"{m.get('frequency','')} {m.get('route','')}{notes}"
                )

        text_sections = {
            "PRE-OP INSTRUCTIONS":  d.get("pre_op_instructions"),
            "POST-OP INSTRUCTIONS": d.get("post_op_instructions"),
            "DIET INSTRUCTIONS":    d.get("diet_instructions"),
            "ACTIVITY RESTRICTIONS":d.get("activity_restrictions"),
            "WOUND CARE":           d.get("wound_care"),
            "ALLERGIES":            ", ".join(d.get("allergies") or []),
            "PRIMARY PATIENT CONCERN": d.get("primary_concern"),
        }
        for label, val in text_sections.items():
            if val:
                lines += ["", f"{label}:", val]

        red_flags = d.get("red_flags") or []
        if red_flags:
            lines += ["", "RED FLAG SYMPTOMS:"] + [f"  - {s}" for s in red_flags]

        normal = d.get("normal_symptoms") or []
        if normal:
            lines += ["", "EXPECTED NORMAL SYMPTOMS:"] + [f"  - {s}" for s in normal]

        fu = d.get("follow_up") or {}
        if fu.get("date"):
            lines += [
                "", "FOLLOW-UP:",
                f"  Date: {fu.get('date')}",
                f"  Provider: {fu.get('provider', 'Care team')}",
            ]
            if fu.get("notes"):
                lines.append(f"  Notes: {fu['notes']}")

        missing = d.get("missing_critical_data") or []
        if missing:
            lines += ["", "⚠ MISSING CRITICAL DATA:"] + [f"  - {m}" for m in missing]

        return "\n".join(lines)
