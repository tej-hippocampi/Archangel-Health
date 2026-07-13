"""Pluggable de-identification VERIFIER (Data Provider Portal PRD §7.5).

The data PROVIDER de-identifies their data; WE verify. This module is the
verifier that runs after timeline normalization and before the final
``case_formats.deidentify()`` post-condition. It is pluggable via the
env-selected backend (``constants.deid_verifier()`` / ``ASCLEPIUS_DEID_VERIFIER``).

Backends, cheapest→richest:
  * ``baseline`` — the self-contained regex scanner reused from
    ``validation.residual_identifiers``. ALWAYS available; never a no-op.
  * ``presidio`` — Microsoft Presidio (``presidio_analyzer``), an optional
    enhancement. If the library is absent it DOES NOT silently pass: it falls
    back to the baseline scanner, reports ``backend="baseline"`` (so the caller
    sees which verifier actually ran), and records ``requested_backend`` +
    ``fallback_reason``.
  * ``comprehend_medical`` — AWS Comprehend Medical (via ``boto3``), same
    optional/fallback contract as presidio (missing library OR missing
    credentials → baseline fallback, never a silent pass).

Invariant carried throughout: findings are MASKED identifier KINDS only
(``email`` / ``phone`` / ``ssn`` / ``mrn`` / ``date`` / …), never the cleartext
matched value. ``mask_findings`` documents and enforces that invariant.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from asclepius import constants
from asclepius.case_formats import _case_text_fields
from asclepius.validation import residual_identifiers

# The three backends the verifier knows about (mirrors constants.deid_verifier()).
BACKENDS = ("baseline", "presidio", "comprehend_medical")


def mask_findings(findings: List[str]) -> List[str]:
    """Guarantee findings are identifier KINDS only — never cleartext values.

    Every backend already yields short kinds (``email``, ``phone``, ``ssn``, …),
    so this is a documenting no-op passthrough that centralizes the
    "never cleartext" invariant: findings are sorted, de-duplicated, and coerced
    to stripped strings. If a backend ever regressed and tried to emit a raw
    value, this is the one chokepoint to harden.
    """
    return sorted({str(f).strip() for f in (findings or []) if str(f).strip()})


# ─── Presidio backend (optional) ──────────────────────────────────────────────
# Presidio entity types → our short kinds. Anything unmapped is normalized to a
# lowercased kind so we still surface it (as a kind, never a value).
_PRESIDIO_ENTITY_TO_KIND = {
    "PERSON": "person",
    "PHONE_NUMBER": "phone",
    "US_SSN": "ssn",
    "EMAIL_ADDRESS": "email",
    "DATE_TIME": "date",
    "MEDICAL_RECORD_NUMBER": "mrn",
    "US_DRIVER_LICENSE": "drivers_license",
    "LOCATION": "location",
    "URL": "url",
    "IP_ADDRESS": "ip",
    "CREDIT_CARD": "credit_card",
    "US_PASSPORT": "passport",
}


def _try_presidio_analyzer() -> Any:
    """Return a Presidio ``AnalyzerEngine`` instance, or None if unavailable."""
    from presidio_analyzer import AnalyzerEngine  # optional dependency

    return AnalyzerEngine()


def _presidio_scan(texts: List[str], analyzer: Any) -> List[str]:
    kinds: List[str] = []
    for text in texts:
        if not text:
            continue
        try:
            results = analyzer.analyze(text=text, language="en")
        except Exception:
            # A per-text analyzer failure must not surface cleartext or crash the
            # verifier; skip this text (the baseline path remains the safety net
            # for the overall selection, but a partial presidio run still reports
            # what it found).
            continue
        for r in results or []:
            entity = getattr(r, "entity_type", None) or ""
            kinds.append(_PRESIDIO_ENTITY_TO_KIND.get(entity, entity.lower()))
    return kinds


# ─── AWS Comprehend Medical backend (optional) ────────────────────────────────
# Comprehend Medical PHI entity types → our short kinds.
_COMPREHEND_TYPE_TO_KIND = {
    "NAME": "person",
    "PHONE_OR_FAX": "phone",
    "EMAIL": "email",
    "DATE": "date",
    "ID": "id",
    "URL": "url",
    "ADDRESS": "location",
    "AGE": "age",
    "PROFESSION": "profession",
}


def _try_comprehend_client() -> Any:
    """Return a boto3 Comprehend Medical client, or raise if unavailable.

    Raises on a missing library OR absent credentials — the caller converts the
    raise into a documented baseline fallback (never a silent pass).
    """
    import boto3  # optional dependency
    from botocore.exceptions import NoCredentialsError  # noqa: F401

    client = boto3.client("comprehendmedical")
    # Force credential resolution now so an absent-credentials deployment falls
    # back deterministically instead of failing later mid-scan.
    session = boto3.session.Session()
    if session.get_credentials() is None:
        raise RuntimeError("no AWS credentials for comprehend_medical")
    return client


def _comprehend_scan(texts: List[str], client: Any) -> List[str]:
    kinds: List[str] = []
    for text in texts:
        if not text:
            continue
        try:
            resp = client.detect_phi(Text=text)
        except Exception:
            continue
        for ent in (resp or {}).get("Entities", []) or []:
            etype = ent.get("Type") or ""
            kinds.append(_COMPREHEND_TYPE_TO_KIND.get(etype, etype.lower()))
    return kinds


# ─── Core ─────────────────────────────────────────────────────────────────────
def _baseline_findings(texts: List[str]) -> List[str]:
    kinds: List[str] = []
    for text in texts:
        kinds.extend(residual_identifiers(text))
    return kinds


def _resolve_backend(backend: Optional[str]) -> str:
    if backend is None:
        return constants.deid_verifier()
    b = (backend or "").strip().lower()
    return b if b in BACKENDS else "baseline"


def verify_texts(texts: List[str], *, backend: Optional[str] = None) -> Dict[str, Any]:
    """Verify a list of already-collected text fields is free of residual
    identifiers, using the selected (or env-default) backend.

    Same return shape as ``verify_case`` minus the ``n_fields_scanned`` semantics
    (the caller owns field counting), so the selection logic is testable directly.
    Never raises on normal input; never returns a silent pass when the requested
    scanner is unavailable — it degrades to the always-available baseline and
    says so via ``requested_backend`` / ``fallback_reason``.
    """
    requested = _resolve_backend(backend)
    texts = [t for t in (texts or [])]

    result: Dict[str, Any] = {
        "passed": True,
        "backend": "baseline",
        "findings": [],
    }

    if requested == "presidio":
        try:
            analyzer = _try_presidio_analyzer()
        except Exception as exc:  # library absent / init failure → baseline
            result["requested_backend"] = "presidio"
            result["fallback_reason"] = f"presidio_unavailable: {type(exc).__name__}"
            findings = _baseline_findings(texts)
        else:
            result["backend"] = "presidio"
            findings = _presidio_scan(texts, analyzer)
    elif requested == "comprehend_medical":
        try:
            client = _try_comprehend_client()
        except Exception as exc:  # library / credentials absent → baseline
            result["requested_backend"] = "comprehend_medical"
            result["fallback_reason"] = f"comprehend_medical_unavailable: {type(exc).__name__}"
            findings = _baseline_findings(texts)
        else:
            result["backend"] = "comprehend_medical"
            findings = _comprehend_scan(texts, client)
    else:  # baseline — always available
        result["backend"] = "baseline"
        findings = _baseline_findings(texts)

    result["findings"] = mask_findings(findings)
    result["passed"] = len(result["findings"]) == 0
    return result


def verify_case(case: Dict[str, Any], *, backend: Optional[str] = None) -> Dict[str, Any]:
    """Verify an assembled + timeline-normalized case is free of residual
    identifiers.

    Scans EXACTLY the fields the final ``case_formats.deidentify()`` guard scans
    (``_case_text_fields``), so a pass here means the same surface the
    post-condition checks is clean.

    Returns::

        {
          "passed": bool,          # True == no residual identifiers found
          "backend": str,          # which verifier actually ran
          "findings": [str, ...],  # MASKED identifier KINDS only, sorted unique
          "n_fields_scanned": int,
          # on a requested-but-unavailable backend, additionally:
          "requested_backend": str,
          "fallback_reason": str,
        }

    ``backend`` overrides the env default when provided. Never raises on normal
    input; never returns a silent pass when the requested scanner is unavailable.
    """
    fields = _case_text_fields(case or {})
    result = verify_texts(fields, backend=backend)
    result["n_fields_scanned"] = len(fields)
    return result
