"""
Background extraction job (PRD §6.2 / §8.1).

`run_extraction_job` does:

    1. Mark the extraction RUNNING.
    2. Read the stored PDF bytes from disk.
    3. Call the chosen extractor (MockIntraopExtractor or LlmIntraopExtractor).
    4. Persist `fields_json`, `field_confidences_json`, `warnings`, `raw_text`.
    5. Auto-populate the form for fields the surgeon hasn't yet touched
       (origin = AUTO_POP_PDF), and emit a structured `field_diffs`
       payload for fields the surgeon already filled in (UI surfaces a
       "PDF says X, you have Y" prompt).
    6. Mark the extraction COMPLETE / FAILED.

The function is fire-and-forget — designed to be scheduled with
`asyncio.create_task` from the upload endpoint. All errors are caught
and recorded in the extraction row's `error_message` field.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from triage.intraop.extractor import (
    ExtractionContext,
    ExtractionPayload,
    IntraopExtractor,
)

log = logging.getLogger("triage.intraop.extraction_job")


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def diff_against_form(
    *,
    form_fields: dict[str, Any],
    extracted: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """For every extracted key already populated in the form, return
    {key: {"existing": <surgeon value>, "extracted": <pdf value>}} when
    the values disagree. Surgeon's value is preserved upstream; the diff
    is a UI hint."""
    out: dict[str, dict[str, Any]] = {}
    for k, v in extracted.items():
        existing = form_fields.get(k)
        if existing is not None and existing != v:
            out[k] = {"existing": existing, "extracted": v}
    return out


def auto_populate_form(
    *,
    form_fields: dict[str, Any],
    field_origins: dict[str, dict[str, Any]],
    extracted: dict[str, Any],
    extracted_confidences: dict[str, float],
    extraction_id: str,
    pdf_blob_url: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    """Merge the extracted payload into the form *only for fields the
    surgeon has not touched yet* (origin not present, or value None).

    Returns:
        (new_fields, new_origins, populated_keys)
    """
    now = _utcnow_iso()
    new_fields: dict[str, Any] = dict(form_fields)
    new_origins: dict[str, dict[str, Any]] = dict(field_origins)
    populated: list[str] = []

    for key, value in extracted.items():
        if value is None:
            continue
        if new_fields.get(key) is not None:
            continue
        new_fields[key] = value
        new_origins[key] = {
            "origin": "AUTO_POP_PDF",
            "source": f"pdf:{extraction_id}:{pdf_blob_url}",
            "confidence": extracted_confidences.get(key, 0.0),
            "populated_at": now,
        }
        populated.append(key)
    return new_fields, new_origins, populated


async def run_extraction_job(
    *,
    extraction_id: str,
    patient_id: str,
    pdf_bytes: bytes,
    pdf_blob_url: str,
    procedure_family: Optional[str],
    procedure_name: Optional[str],
    extractor: IntraopExtractor,
    team_store,
) -> dict[str, Any]:
    """Drive a single extraction lifecycle and update the form in place.

    Returns a `dict` summary suitable for SSE / polling consumers:
        {
            "status": "COMPLETE" | "FAILED",
            "extraction_id": ...,
            "fields_populated": [..],
            "field_diffs": {...},
            "warnings": [...],
            "error": <str or None>,
        }
    """
    team_store.update_intraop_extraction(extraction_id=extraction_id, status="RUNNING")
    try:
        ctx = ExtractionContext(
            patient_id=patient_id,
            procedure_family=procedure_family,  # type: ignore[arg-type]
            procedure_name=procedure_name,
        )
        payload: ExtractionPayload = await extractor.extract(pdf_bytes=pdf_bytes, context=ctx)

        form = team_store.get_intraop_form(patient_id) or {}
        form_fields = form.get("fields") or {}
        field_origins = form.get("field_origins") or {}

        diffs = diff_against_form(form_fields=form_fields, extracted=payload.fields)
        new_fields, new_origins, populated = auto_populate_form(
            form_fields=form_fields,
            field_origins=field_origins,
            extracted=payload.fields,
            extracted_confidences=payload.field_confidences,
            extraction_id=extraction_id,
            pdf_blob_url=pdf_blob_url,
        )

        # Persist the merged form (only when something changed).
        if populated:
            team_store.update_intraop_form_fields(
                patient_id=patient_id,
                fields=new_fields,
                field_origins=new_origins,
                status=form.get("status") if form.get("status") in ("LOCKED",) else "IN_PROGRESS",
            )

        warnings = list(payload.warnings)
        if diffs:
            warnings.append(f"PDF disagrees with surgeon-entered values for {len(diffs)} field(s).")

        team_store.update_intraop_extraction(
            extraction_id=extraction_id,
            status="COMPLETE",
            raw_text=payload.raw_text,
            fields=payload.fields,
            field_confidences=payload.field_confidences,
            warnings=warnings,
        )

        log.info(
            "[INTRAOP_EXTRACT] %s complete (auto-pop=%d diffs=%d warnings=%d)",
            extraction_id, len(populated), len(diffs), len(warnings),
        )
        return {
            "status": "COMPLETE",
            "extraction_id": extraction_id,
            "fields_populated": populated,
            "field_diffs": diffs,
            "warnings": warnings,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("[INTRAOP_EXTRACT] %s failed", extraction_id)
        team_store.update_intraop_extraction(
            extraction_id=extraction_id, status="FAILED", error_message=str(e),
        )
        return {
            "status": "FAILED",
            "extraction_id": extraction_id,
            "fields_populated": [],
            "field_diffs": {},
            "warnings": [],
            "error": str(e),
        }
