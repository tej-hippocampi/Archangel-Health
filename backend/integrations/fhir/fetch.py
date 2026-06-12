"""High-level FHIR fetchers used by the import router.

Everything returns plain dicts shaped for the eligibility pipeline:
  - ``search_patients``            → roster-picker summaries (no clinical data)
  - ``fetch_eligibility_bundle``   → a FHIR ``Bundle`` (Patient + Coverage) we
                                     persist verbatim as the source document
  - ``fetch_document_attachments`` → decoded DocumentReference attachments
                                     (PDF / text) ready to register as docs
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

from integrations.fhir.client import FhirClient, FhirError

log = logging.getLogger("fhir.fetch")

MBI_SYSTEM = "http://hl7.org/fhir/sid/us-mbi"

# DocumentReference safety caps — an EHR chart can hold thousands of notes.
MAX_DOCREFS = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # mirrors the PDF upload cap
ALLOWED_ATTACHMENT_TYPES = {"application/pdf", "text/plain", "text/html"}


def _human_name(patient: Dict[str, Any]) -> str:
    for name in patient.get("name") or []:
        given = " ".join(name.get("given") or [])
        family = name.get("family") or ""
        text = name.get("text") or f"{given} {family}".strip()
        if text:
            return text
    return ""


def _identifiers(patient: Dict[str, Any]) -> List[Dict[str, str]]:
    out = []
    for ident in patient.get("identifier") or []:
        out.append({"system": ident.get("system") or "", "value": ident.get("value") or ""})
    return out


def patient_summary(patient: Dict[str, Any]) -> Dict[str, Any]:
    """Identity-only view for the patient picker. No clinical content."""
    idents = _identifiers(patient)
    mbi = next((i["value"] for i in idents if i["system"] == MBI_SYSTEM), None)
    return {
        "fhirId": patient.get("id"),
        "name": _human_name(patient),
        "birthDate": patient.get("birthDate"),
        "gender": patient.get("gender"),
        "mbi": mbi,
        "identifiers": idents,
    }


async def search_patients(
    client: FhirClient,
    *,
    identifier: Optional[str] = None,
    name: Optional[str] = None,
    birthdate: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"_count": 20}
    if identifier:
        params["identifier"] = identifier
    if name:
        params["name"] = name
    if birthdate:
        params["birthdate"] = birthdate
    resources = await client.search("Patient", params, max_pages=1)
    return [patient_summary(p) for p in resources]


async def fetch_eligibility_bundle(client: FhirClient, fhir_patient_id: str) -> Dict[str, Any]:
    """Patient + all Coverage resources, packaged as a FHIR collection Bundle.

    The Bundle is what we write to disk — a verbatim, auditable record of
    exactly what the EHR returned at import time.
    """
    patient = await client.read("Patient", fhir_patient_id)
    coverages = await client.search("Coverage", {"patient": fhir_patient_id, "_count": 50})
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": patient}] + [{"resource": c} for c in coverages],
    }


async def fetch_document_attachments(
    client: FhirClient,
    fhir_patient_id: str,
    *,
    max_docs: int = MAX_DOCREFS,
) -> List[Dict[str, Any]]:
    """Resolve DocumentReference attachments to bytes.

    Returns ``[{"title", "content_type", "content": bytes, "fhir_docref_id"}]``.
    Inline base64 ``attachment.data`` is preferred; otherwise ``attachment.url``
    is fetched (Binary/... or absolute). Unsupported types and oversized
    attachments are skipped with a log line, never an error — a bad note must
    not sink the whole import.
    """
    docrefs = await client.search(
        "DocumentReference",
        {"patient": fhir_patient_id, "status": "current", "_count": max_docs},
        max_pages=1,
    )
    out: List[Dict[str, Any]] = []
    for ref in docrefs[:max_docs]:
        for content in ref.get("content") or []:
            att = content.get("attachment") or {}
            ctype = (att.get("contentType") or "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_ATTACHMENT_TYPES:
                log.info("Skipping DocumentReference/%s attachment (type %s)", ref.get("id"), ctype)
                continue
            try:
                if att.get("data"):
                    blob = base64.b64decode(att["data"])
                elif att.get("url"):
                    blob = await client.read_binary(att["url"])
                else:
                    continue
            except (FhirError, ValueError) as e:
                log.warning("Could not resolve attachment on DocumentReference/%s: %s", ref.get("id"), e)
                continue
            if not blob or len(blob) > MAX_ATTACHMENT_BYTES:
                log.info("Skipping DocumentReference/%s attachment (empty or >25MB)", ref.get("id"))
                continue
            out.append(
                {
                    "title": att.get("title")
                    or (ref.get("type") or {}).get("text")
                    or f"document_{ref.get('id')}",
                    "content_type": ctype,
                    "content": blob,
                    "fhir_docref_id": ref.get("id"),
                }
            )
            break  # one attachment per DocumentReference is enough
    return out
