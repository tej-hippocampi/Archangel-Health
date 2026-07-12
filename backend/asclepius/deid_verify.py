"""De-identification VERIFIER (EHR Ingestion PRD §8) — tag, never scrub.

The partner de-identifies; we independently verify (Azure's Tag→Redact→Surrogate
model: we only need **Tag**). ``verify_deid(case)`` walks the assembled case and
returns a report — ``pass`` or ``flagged`` with per-finding field paths — that
ingestion uses to TRIAGE into quarantine instead of silently rejecting.

Pluggable backends (``ASCLEPIUS_DEID_VERIFIER``):
  * ``baseline``  (default) — the shared ``validation.residual_identifiers``
    scanner (regex + optional ``gold.deid``), applied per string field.
  * ``presidio``  — Microsoft Presidio AnalyzerEngine when installed; falls back
    to baseline (with a logged warning) when the import is unavailable.
  * ``comprehend_medical`` — AWS Comprehend Medical DetectPHI when boto3 +
    credentials are available; falls back to baseline otherwise.
A second-opinion engine can therefore be dropped in by config, never by code.

Finding shape (PRD §8): ``{kind, field_path, snippet_masked, span, confidence}``.
``snippet_masked`` has every digit/letter of the suspect span masked — a
suspected identifier is NEVER rendered in cleartext (not in the API, not in the
admin UI). ``span`` (start,end within the field) exists so the quarantine
"targeted scrub" action can redact the exact span without anyone re-reading it.

deidentify() (case_formats) remains the final HARD post-condition after this
triage — verification never replaces the guard.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Tuple

from asclepius.validation import residual_identifiers

log = logging.getLogger("asclepius.deid_verify")

# Baseline span-finding patterns (mirrors validation._PHI_PATTERNS but returns
# SPANS so quarantine can do a targeted scrub). Kinds match the scanner's names.
_SPAN_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn", re.compile(r"\b(?:MRN|MBI|Medical Record(?:\s*Number)?)\s*[:#]?\s*[A-Za-z0-9\-]+\b", re.I)),
    ("date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("long_number", re.compile(r"\b\d{7,}\b")),
]


def _mask(s: str) -> str:
    return re.sub(r"[A-Za-z0-9]", "•", s)


def _walk_strings(node: Any, path: str = "") -> List[Tuple[str, str]]:
    """(field_path, string) for every string in the case — same drift-proof walk
    as the deidentify guard, but path-annotated for actionable findings."""
    out: List[Tuple[str, str]] = []
    if isinstance(node, str):
        out.append((path or "$", node))
    elif isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            out.extend(_walk_strings(v, f"{path}[{i}]"))
    return out


# ─── Backends ─────────────────────────────────────────────────────────────────
def _baseline_findings(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for path, text in _walk_strings(case):
        if not text:
            continue
        # Fast pre-check with the shared scanner (incl. gold.deid when present)…
        kinds = residual_identifiers(text)
        span_hits = [(k, m) for k, pat in _SPAN_PATTERNS for m in pat.finditer(text)]
        if not kinds and not span_hits:
            continue
        if span_hits:
            for kind, m in span_hits:
                findings.append({
                    "kind": kind, "field_path": path,
                    "snippet_masked": _mask(m.group(0)),
                    "span": [m.start(), m.end()],
                    "confidence": 0.9,
                })
        else:
            # The richer scanner flagged something our span patterns can't place:
            # report the whole field (masked) so it still quarantines visibly.
            for kind in kinds:
                findings.append({
                    "kind": kind, "field_path": path,
                    "snippet_masked": _mask(text[:60]),
                    "span": [0, len(text)],
                    "confidence": 0.7,
                })
    return findings


def _presidio_findings(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore
    except Exception:
        log.warning("ASCLEPIUS_DEID_VERIFIER=presidio but presidio-analyzer is not "
                    "installed; falling back to the baseline scanner.")
        return _baseline_findings(case)
    engine = AnalyzerEngine()
    findings: List[Dict[str, Any]] = []
    for path, text in _walk_strings(case):
        if not text:
            continue
        for res in engine.analyze(text=text, language="en"):
            findings.append({
                "kind": str(res.entity_type).lower(), "field_path": path,
                "snippet_masked": _mask(text[res.start:res.end]),
                "span": [res.start, res.end],
                "confidence": round(float(res.score), 3),
            })
    return findings


def _comprehend_findings(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        import boto3  # type: ignore
        client = boto3.client("comprehendmedical")
    except Exception:
        log.warning("ASCLEPIUS_DEID_VERIFIER=comprehend_medical but boto3/credentials "
                    "are unavailable; falling back to the baseline scanner.")
        return _baseline_findings(case)
    findings: List[Dict[str, Any]] = []
    for path, text in _walk_strings(case):
        if not text:
            continue
        try:
            resp = client.detect_phi(Text=text[:20000])
        except Exception:
            log.warning("Comprehend Medical DetectPHI call failed; falling back to baseline.")
            return _baseline_findings(case)
        for ent in resp.get("Entities") or []:
            b, e = int(ent.get("BeginOffset", 0)), int(ent.get("EndOffset", 0))
            findings.append({
                "kind": str(ent.get("Type", "phi")).lower(), "field_path": path,
                "snippet_masked": _mask(text[b:e]),
                "span": [b, e],
                "confidence": round(float(ent.get("Score", 0.0)), 3),
            })
    return findings


_BACKENDS = {
    "baseline": _baseline_findings,
    "presidio": _presidio_findings,
    "comprehend_medical": _comprehend_findings,
}


def verifier_name() -> str:
    name = (os.getenv("ASCLEPIUS_DEID_VERIFIER") or "baseline").strip().lower()
    return name if name in _BACKENDS else "baseline"


def verify_deid(case: Dict[str, Any]) -> Dict[str, Any]:
    """Independent residual-identifier verification of an ASSEMBLED, timeline-
    normalized case. ``{"status": "pass"|"flagged", "verifier": name,
    "findings": [...]}`` — findings are masked, span-addressed, never cleartext."""
    name = verifier_name()
    findings = _BACKENDS[name](case or {})
    return {
        "status": "flagged" if findings else "pass",
        "verifier": name,
        "findings": findings,
    }


def apply_targeted_scrub(case: Dict[str, Any], findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The quarantine "scrub" action (PRD §8): redact EXACTLY the flagged spans
    (``[redacted]``), touching nothing else — an explicit, logged human action,
    never automatic. Spans are applied right-to-left per field so earlier
    replacements don't shift later offsets."""
    import copy
    out = copy.deepcopy(case or {})

    by_path: Dict[str, List[List[int]]] = {}
    for f in findings or []:
        span = f.get("span")
        if f.get("field_path") and isinstance(span, (list, tuple)) and len(span) == 2:
            by_path.setdefault(f["field_path"], []).append([int(span[0]), int(span[1])])

    def _set(node: Any, parts: List[str], spans: List[List[int]]) -> None:
        if not parts:
            return
        head, rest = parts[0], parts[1:]
        idx = None
        if "[" in head:
            head, _, i = head.partition("[")
            idx = int(i.rstrip("]"))
        child = node.get(head) if isinstance(node, dict) else None
        if idx is not None and isinstance(child, list) and 0 <= idx < len(child):
            child_container, key = child, idx
            target = child[idx]
        else:
            child_container, key = node, head
            target = child
        if rest:
            _set(target, rest, spans)
        elif isinstance(target, str):
            s = target
            for start, end in sorted(spans, key=lambda x: -x[0]):
                if 0 <= start < end <= len(s):
                    s = s[:start] + "[redacted]" + s[end:]
            if isinstance(child_container, dict):
                child_container[key] = s
            elif isinstance(child_container, list):
                child_container[key] = s

    for path, spans in by_path.items():
        _set(out, path.split("."), spans)
    return out
