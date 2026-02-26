"""
EHR Data Ingestion Layer
Purpose: Validate, timestamp, and package the minimum necessary patient data
         into a secure source bundle before any processing occurs.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict


class IngestLayer:
    # Sections we accept and their display labels
    SECTIONS = {
        "pmh":                  "Past Medical History",
        "procedure_context":    "Procedure Context",
        "after_visit_summary":  "After Visit Summary",
        "clinical_notes":       "Clinical Notes",
        "medication_list":      "Medication List",
        "allergies":            "Allergies",
        "problem_list":         "Problem List",
    }

    def process(self, raw_bundle: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates and packages raw EHR fields.

        Returns a timestamped source bundle:
        {
            "metadata": { patient_id, patient_name, ingested_at, bundle_hash },
            "clinical_data": { pmh, procedure_context, ... }
        }
        """
        self._validate(raw_bundle)

        clinical = {
            key: raw_bundle.get(key, "").strip()
            for key in self.SECTIONS
        }

        return {
            "metadata": {
                "patient_id":   raw_bundle["patient_id"],
                "patient_name": raw_bundle["patient_name"],
                "phone_number": raw_bundle["phone_number"],
                "ingested_at":  datetime.now(timezone.utc).isoformat(),
                "bundle_hash":  self._hash(raw_bundle),
            },
            "clinical_data": clinical,
        }

    # ── Private ──────────────────────────────────────────────

    def _validate(self, bundle: Dict[str, Any]) -> None:
        for field in ("patient_id", "patient_name", "phone_number"):
            if not bundle.get(field, "").strip():
                raise ValueError(f"EHR bundle is missing required field: {field}")

    def _hash(self, bundle: Dict[str, Any]) -> str:
        """SHA-256 fingerprint of clinical content (excluding PII phone number)."""
        content = {k: v for k, v in bundle.items() if k != "phone_number"}
        serialized = json.dumps(content, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]
