"""
Parsing + Clinical Extraction Layer
Purpose: Convert messy EHR text into clean structured fields using Claude.
"""

import json
import os
from typing import Any, Dict

from anthropic import Anthropic

EXTRACTION_SYSTEM = """You are a clinical NLP extraction system.
Parse raw EHR text and return ONLY a single valid JSON object — no markdown, no commentary.
If a field is absent from the text, use null (not guesses or assumptions).
ZERO HALLUCINATIONS: extract only what is explicitly stated."""

EXTRACTION_PROMPT = """
Extract the following fields from the EHR sections below and return as JSON:

{
  "patient_name":        "string",
  "procedure_name":      "string — exact name of surgery or procedure",
  "procedure_date":      "string — ISO date if known, else descriptive",
  "procedure_status":    "'scheduled' | 'completed' | 'unknown'",
  "pre_op_instructions": "string — full prep instructions or null",
  "post_op_instructions":"string — discharge/recovery instructions or null",
  "medications": [
    {
      "name":      "string — exact name",
      "dose":      "string",
      "frequency": "string",
      "route":     "string",
      "status":    "'new' | 'changed' | 'continue' | 'stop'",
      "notes":     "string or null"
    }
  ],
  "red_flags":            ["list of warning symptoms to contact provider about"],
  "normal_symptoms":      ["list of expected/normal post-op symptoms"],
  "diet_instructions":    "string or null",
  "activity_restrictions":"string or null",
  "wound_care":           "string or null",
  "follow_up": {
    "date":     "string or null",
    "provider": "string or null",
    "notes":    "string or null"
  },
  "allergies":            ["list"],
  "key_diagnoses":        ["list"],
  "primary_concern":      "string — documented patient worry, or null",
  "note_type":            "'pre_op_note' | 'discharge_note' | 'post_op_visit' | 'unknown'",
  "missing_critical_data":["list of important fields that are absent"]
}

## EHR DATA
"""


class ExtractionLayer:
    def __init__(self) -> None:
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    async def extract(self, raw_package: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sends combined EHR text to Claude for structured extraction.
        Merges result with package metadata.
        """
        combined = self._combine(raw_package["clinical_data"])

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT + combined}],
        )

        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()

        extracted: Dict[str, Any] = json.loads(raw_text)

        # Always preserve identity from ingest metadata
        extracted["patient_id"]   = raw_package["metadata"]["patient_id"]
        extracted["patient_name"] = (
            extracted.get("patient_name")
            or raw_package["metadata"]["patient_name"]
        )

        # Keep raw text for downstream use (classifier, avatar prompt)
        extracted["_raw_clinical"] = raw_package["clinical_data"]

        return extracted

    # ── Private ──────────────────────────────────────────────

    def _combine(self, clinical: Dict[str, str]) -> str:
        labels = {
            "pmh":                 "PAST MEDICAL HISTORY",
            "procedure_context":   "PROCEDURE CONTEXT",
            "after_visit_summary": "AFTER VISIT SUMMARY",
            "clinical_notes":      "CLINICAL NOTES",
            "medication_list":     "MEDICATION LIST",
            "allergies":           "ALLERGIES",
            "problem_list":        "PROBLEM LIST",
        }
        sections = []
        for key, label in labels.items():
            text = clinical.get(key, "").strip()
            if text:
                sections.append(f"## {label}\n{text}")
        return "\n\n".join(sections)
