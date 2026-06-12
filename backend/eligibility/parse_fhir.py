"""Parse a FHIR R4 Bundle (Patient + Coverage) into extractor-ready text.

The FHIR import endpoint persists exactly what the EHR returned — a
``Bundle`` of ``Patient`` and ``Coverage`` resources — and registers it as
an eligibility document with format ``FHIR_JSON``. This module renders that
Bundle deterministically (no LLM) so the existing eligibility extractor can
read it the same way it reads X12/PDF/CSV text.

Rendering philosophy: surface EVERY coding (system + code + display) rather
than trying to interpret Medicare semantics here — interpretation is the
extractor's job, and lossy pre-filtering is how fields go missing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MBI_SYSTEM = "http://hl7.org/fhir/sid/us-mbi"


class InvalidFhirError(Exception):
    """Raised when the bytes are not a parseable FHIR Bundle/resource."""


@dataclass
class FhirParseResult:
    patient: Optional[Dict[str, Any]]
    coverages: List[Dict[str, Any]] = field(default_factory=list)
    other_resources: List[Dict[str, Any]] = field(default_factory=list)
    source_server: str = ""
    retrieved_at: str = ""


def parse_fhir_bundle(raw: bytes) -> FhirParseResult:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidFhirError(f"Not valid FHIR JSON: {e}") from e
    if not isinstance(data, dict) or not data.get("resourceType"):
        raise InvalidFhirError("JSON has no resourceType — not a FHIR resource")

    # Accept either a Bundle or a bare resource.
    if data["resourceType"] == "Bundle":
        resources = [
            e.get("resource")
            for e in data.get("entry") or []
            if isinstance(e.get("resource"), dict)
        ]
    else:
        resources = [data]

    result = FhirParseResult(
        patient=None,
        source_server=str((data.get("meta") or {}).get("source") or ""),
        retrieved_at=str(data.get("timestamp") or ""),
    )
    for res in resources:
        rt = res.get("resourceType")
        if rt == "Patient" and result.patient is None:
            result.patient = res
        elif rt == "Coverage":
            result.coverages.append(res)
        else:
            result.other_resources.append(res)
    if result.patient is None and not result.coverages:
        raise InvalidFhirError("Bundle contains no Patient or Coverage resources")
    return result


# ─── Rendering ───────────────────────────────────────────────────────────────
def _codeable(cc: Any) -> str:
    """Render a CodeableConcept (or Coding) as 'display [system|code]' lines."""
    if not isinstance(cc, dict):
        return ""
    codings = cc.get("coding")
    if codings is None and ("system" in cc or "code" in cc):
        codings = [cc]  # bare Coding
    parts: List[str] = []
    for c in codings or []:
        seg = c.get("display") or ""
        sys_code = f"{c.get('system') or ''}|{c.get('code') or ''}".strip("|")
        if sys_code:
            seg = f"{seg} [{sys_code}]".strip()
        if seg:
            parts.append(seg)
    text = cc.get("text")
    if text and text not in parts:
        parts.append(text)
    return "; ".join(parts)


def _period(p: Any) -> str:
    if not isinstance(p, dict):
        return ""
    start, end = p.get("start") or "?", p.get("end") or "(open)"
    return f"{start} → {end}"


def _render_patient(patient: Dict[str, Any], lines: List[str]) -> None:
    lines.append("── PATIENT ──")
    names = patient.get("name") or []
    if names:
        n = names[0]
        full = n.get("text") or f"{' '.join(n.get('given') or [])} {n.get('family') or ''}".strip()
        lines.append(f"Name: {full}")
    if patient.get("birthDate"):
        lines.append(f"Date of birth: {patient['birthDate']}")
    if patient.get("gender"):
        lines.append(f"Gender: {patient['gender']}")
    if patient.get("deceasedBoolean") or patient.get("deceasedDateTime"):
        lines.append(f"Deceased: {patient.get('deceasedDateTime') or 'yes'}")
    for ident in patient.get("identifier") or []:
        system = ident.get("system") or "unknown-system"
        label = "MBI (Medicare Beneficiary Identifier)" if system == MBI_SYSTEM else f"Identifier ({system})"
        lines.append(f"{label}: {ident.get('value') or ''}")


def _render_coverage(cov: Dict[str, Any], idx: int, lines: List[str]) -> None:
    lines.append(f"── COVERAGE #{idx} ──")
    lines.append(f"Status: {cov.get('status') or 'unknown'}")
    if cov.get("type"):
        lines.append(f"Type: {_codeable(cov['type'])}")
    if cov.get("subscriberId"):
        lines.append(f"Subscriber ID: {cov['subscriberId']}")
    for payor in cov.get("payor") or []:
        display = payor.get("display") or payor.get("reference") or ""
        if display:
            lines.append(f"Payor: {display}")
    if cov.get("period"):
        lines.append(f"Period: {_period(cov['period'])}")
    if cov.get("order") is not None:
        # order=1 is primary; >1 means another payer pays first (MSP signal)
        lines.append(f"Coordination-of-benefits order: {cov['order']}")
    if cov.get("relationship"):
        lines.append(f"Relationship to subscriber: {_codeable(cov['relationship'])}")
    for cls in cov.get("class") or []:
        cls_type = _codeable(cls.get("type")) or "class"
        val = cls.get("name") or cls.get("value") or ""
        lines.append(f"Class ({cls_type}): {val}")
    for ext in cov.get("extension") or []:
        url = ext.get("url") or ""
        val = next((v for k, v in ext.items() if k.startswith("value")), "")
        if url and val != "":
            lines.append(f"Extension {url}: {val}")


def format_for_llm(result: FhirParseResult, filename: Optional[str] = None) -> str:
    lines: List[str] = [f"=== FHIR COVERAGE RECORD ({filename or 'fhir-import'}) ==="]
    lines.append(
        "Source: FHIR R4 export pulled directly from the EHR/payer server"
        + (f" ({result.source_server})" if result.source_server else "")
        + (f" at {result.retrieved_at}" if result.retrieved_at else "")
    )
    if result.patient:
        _render_patient(result.patient, lines)
    if not result.coverages:
        lines.append("── COVERAGE ──")
        lines.append("No Coverage resources were returned for this patient.")
    for i, cov in enumerate(result.coverages, start=1):
        _render_coverage(cov, i, lines)
    if result.other_resources:
        kinds = sorted({r.get("resourceType", "?") for r in result.other_resources})
        lines.append(f"(Bundle also contained: {', '.join(kinds)})")
    return "\n".join(lines)
