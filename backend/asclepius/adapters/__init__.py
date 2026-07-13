"""Clinical-data format adapters for real de-identified case ingestion.

Each adapter exposes a single ``parse(raw, *, specialty="general") -> dict``
function that maps one source format into a ClinicalCase-shaped fragment dict
(see :mod:`asclepius.cases`). Adapters do pure parsing only — no
de-identification, no calendar->offset conversion (a later pipeline stage owns
both). They raise :class:`asclepius.case_formats.CaseIngestError` on input the
adapter cannot parse.

The fragment shape (only sections a format actually carries are populated)::

    {
      "specialty": str,
      "patient_key": str | None,
      "demographics": {"age_band": str|None, "sex": str|None},
      "problem_list": [{"condition": str, "since": str|None}],
      "medications": [{"drug": str, "dose": str|None, "route": str|None, "freq": str|None}],
      "vitals": {str: value},
      "lab_panels": [{"panel": str, "collected_at": str|None, "results": [...]}],
      "notes": [{"note_type": str, "author_role": str, "text": str}],
    }

The concrete adapters intentionally emit ``collected_at`` (the RAW collection
date string) rather than ``collected_offset_days`` — the pipeline maps calendar
dates to relative day offsets downstream.
"""

from __future__ import annotations

from . import ccda, fhir_r4, hl7v2, lab_csv, note_text

__all__ = ["lab_csv", "note_text", "fhir_r4", "hl7v2", "ccda"]
