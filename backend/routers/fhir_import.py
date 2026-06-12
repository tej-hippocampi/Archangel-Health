"""FHIR import endpoints — pull Patient/Coverage/DocumentReference data from
an EHR's FHIR R4 server and register it as eligibility documents, so the
existing parse → extract → evaluate pipeline runs unchanged on EHR-sourced
data instead of manual uploads.

Feature-flagged: every route returns 503 until ``FHIR_ENABLED=1`` and the
FHIR settings validate. See ``docs/FHIR_INTEGRATION.md``.

Routes:
  GET  /api/fhir/status            config + (optional ?probe=1) connectivity
  GET  /api/fhir/patients          search the FHIR server (identity only)
  POST /api/fhir/import            fetch + register docs for a local patient
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from eligibility import format_detect, store
from integrations.fhir import FhirAuthError, FhirClient, FhirError, get_settings
from integrations.fhir import fetch as fhir_fetch
from routers.eligibility import (
    UPLOAD_DIR,
    _actor_id,
    _assert_patient_access,
    _patient_store,
    _utc_iso,
)
from staff_context import StaffContext, get_staff_context_optional

log = logging.getLogger("fhir.router")

router = APIRouter(tags=["fhir"])


# ─── Guards ─────────────────────────────────────────────────────────────────
def _require_enabled() -> None:
    settings = get_settings()
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="FHIR integration is disabled (set FHIR_ENABLED=1)")
    errs = settings.validation_errors()
    if errs:
        raise HTTPException(status_code=503, detail=f"FHIR integration misconfigured: {'; '.join(errs)}")


def _require_staff(staff: Optional[StaffContext]) -> StaffContext:
    # Stricter than the upload endpoints on purpose: these routes reach into
    # an external EHR, so anonymous/demo access is never appropriate.
    if not staff:
        raise HTTPException(status_code=401, detail="Authentication required for FHIR access")
    return staff


# ─── Status ─────────────────────────────────────────────────────────────────
@router.get("/api/fhir/status")
async def fhir_status(
    probe: bool = False,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _require_staff(staff)
    settings = get_settings()
    out: Dict[str, Any] = {
        "enabled": settings.enabled,
        "baseUrl": settings.base_url,
        "authMode": settings.auth_mode,
        "configErrors": settings.validation_errors(),
    }
    if probe and settings.enabled and not out["configErrors"]:
        try:
            async with FhirClient(settings) as client:
                cap = await client.capability()
            out["connected"] = True
            out["fhirVersion"] = cap.get("fhirVersion")
        except (FhirError, FhirAuthError) as e:
            out["connected"] = False
            out["probeError"] = str(e)
    return out


# ─── Patient search (identity only — no clinical payload) ───────────────────
@router.get("/api/fhir/patients")
async def search_fhir_patients(
    identifier: Optional[str] = None,
    name: Optional[str] = None,
    birthdate: Optional[str] = None,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _require_enabled()
    _require_staff(staff)
    if not (identifier or name or birthdate):
        raise HTTPException(status_code=400, detail="Provide at least one of identifier / name / birthdate")
    try:
        async with FhirClient() as client:
            patients = await fhir_fetch.search_patients(
                client, identifier=identifier, name=name, birthdate=birthdate
            )
    except (FhirError, FhirAuthError) as e:
        raise HTTPException(status_code=502, detail=f"FHIR server error: {e}")
    return {"patients": patients}


# ─── Import ─────────────────────────────────────────────────────────────────
class FhirImportRequest(BaseModel):
    patientId: str        # local patient (draft or real) the docs attach to
    fhirPatientId: str    # Patient.id on the FHIR server
    includeDocuments: bool = False  # also pull DocumentReference attachments


def _register_document(
    *,
    patient_id: str,
    filename: str,
    content: bytes,
    store_dict: Dict[str, Any],
    actor: str,
    source_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist ``content`` under UPLOAD_DIR and register it in the eligibility
    doc store — same record shape and on-disk permissions as a manual upload,
    plus ``source: fhir`` metadata for provenance."""
    fmt = format_detect.detect_format(filename, content[:4096])
    if len(content) > format_detect.max_size_for(fmt):
        raise HTTPException(status_code=413, detail=f"Fetched {fmt} document exceeds size limit")

    ext = os.path.splitext(filename)[1].lower() or ".bin"
    doc_id = uuid.uuid4().hex
    patient_dir = UPLOAD_DIR / patient_id.replace("/", "_")
    patient_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = patient_dir / f"{doc_id}{ext}"
    dest.write_bytes(content)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass

    record = {
        "id": doc_id,
        "patient_id": patient_id,
        "filename": filename,
        "format": fmt,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "path": str(dest),
        "status": "validated",
        "uploaded_at": _utc_iso(),
        "source": "fhir",
        "source_meta": source_meta,
    }
    store.save_doc(doc_id, record)
    if patient_id in store_dict:
        store_dict[patient_id].setdefault("relevant_files", []).append(doc_id)
    store.append_audit(
        action="fhir_document_imported",
        actor=actor,
        patient_id=patient_id,
        meta={"doc_id": doc_id, "format": fmt, "size_bytes": len(content), **source_meta},
    )
    return record


@router.post("/api/fhir/import")
async def import_from_fhir(
    request: Request,
    body: FhirImportRequest,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    _require_enabled()
    staff_ctx = _require_staff(staff)
    store_dict = _patient_store(request)
    _assert_patient_access(body.patientId, staff_ctx, store_dict)
    actor = _actor_id(staff_ctx)

    fhir_pid = body.fhirPatientId.strip()
    if not fhir_pid:
        raise HTTPException(status_code=400, detail="fhirPatientId is required")

    created: List[Dict[str, Any]] = []
    try:
        async with FhirClient() as client:
            # 1. Patient + Coverage → one FHIR_JSON eligibility document.
            bundle = await fhir_fetch.fetch_eligibility_bundle(client, fhir_pid)
            raw = json.dumps(bundle, indent=2).encode("utf-8")
            created.append(
                _register_document(
                    patient_id=body.patientId,
                    filename=f"fhir_coverage_{fhir_pid}.json",
                    content=raw,
                    store_dict=store_dict,
                    actor=actor,
                    source_meta={"fhir_patient_id": fhir_pid, "kind": "coverage_bundle"},
                )
            )
            # 2. Optionally pull clinical documents (PDF / text attachments).
            if body.includeDocuments:
                attachments = await fhir_fetch.fetch_document_attachments(client, fhir_pid)
                for att in attachments:
                    suffix = {"application/pdf": ".pdf", "text/plain": ".txt", "text/html": ".html"}.get(
                        att["content_type"], ".bin"
                    )
                    safe_title = "".join(
                        c if c.isalnum() or c in ("-", "_", " ") else "_" for c in att["title"]
                    ).strip() or "document"
                    created.append(
                        _register_document(
                            patient_id=body.patientId,
                            filename=f"{safe_title}{suffix}",
                            content=att["content"],
                            store_dict=store_dict,
                            actor=actor,
                            source_meta={
                                "fhir_patient_id": fhir_pid,
                                "kind": "document_reference",
                                "fhir_docref_id": att.get("fhir_docref_id"),
                            },
                        )
                    )
    except (FhirError, FhirAuthError) as e:
        # Documents registered before the failure stay registered — partial
        # imports are visible in the doc list and audit log, never silent.
        log.warning("FHIR import for local patient %s failed: %s", body.patientId, e)
        raise HTTPException(
            status_code=502,
            detail=f"FHIR server error after importing {len(created)} document(s): {e}",
        )

    store.append_audit(
        action="fhir_import_completed",
        actor=actor,
        patient_id=body.patientId,
        meta={
            "fhir_patient_id": fhir_pid,
            "documents": [d["id"] for d in created],
            "include_documents": body.includeDocuments,
        },
    )
    return {
        "patientId": body.patientId,
        "fhirPatientId": fhir_pid,
        "documents": [
            {
                "id": d["id"],
                "filename": d["filename"],
                "format": d["format"],
                "sizeBytes": d["size_bytes"],
                "sha256": d["sha256"],
                "status": d["status"],
            }
            for d in created
        ],
    }
