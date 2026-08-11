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

from asclepius.validation import PHI_SPAN_PATTERNS, residual_identifiers

log = logging.getLogger("asclepius.deid_verify")

# Baseline span-finding patterns. IMPORTED, not restated: these used to be a
# hand-kept copy of ``validation._PHI_PATTERNS``, and the copies drifted — a
# tightened MRN rule in the scanner left the verifier still flagging (and
# quarantining) what the guard had stopped rejecting.
_SPAN_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = list(PHI_SPAN_PATTERNS)


def _mask(s: str) -> str:
    return re.sub(r"[A-Za-z0-9]", "•", s)


def _walk_strings(node: Any, segs: Tuple = ()) -> List[Tuple[Tuple, str]]:
    """(path_segments, string) for every string in the case — same drift-proof
    walk as the deidentify guard, path-annotated for actionable findings.

    Paths are SEGMENT TUPLES (keys/indices), never a dotted string: real field
    keys contain dots and brackets (FHIR vitals like "Oxygen saturation [%]"),
    which made string-path parsing crash or silently no-op (review finding)."""
    out: List[Tuple[Tuple, str]] = []
    if isinstance(node, str):
        out.append((segs, node))
    elif isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk_strings(v, segs + (k,)))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            out.extend(_walk_strings(v, segs + (i,)))
    return out


def _display_path(segs: Tuple) -> str:
    return ".".join(str(s) for s in segs) or "$"


# ─── Backends ─────────────────────────────────────────────────────────────────
def _baseline_findings(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for segs, text in _walk_strings(case):
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
                    "kind": kind, "field_path": _display_path(segs),
                    "path_segments": list(segs),
                    "snippet_masked": _mask(m.group(0)),
                    "span": [m.start(), m.end()],
                    "confidence": 0.9,
                })
        else:
            # The richer scanner flagged something our span patterns can't place:
            # report the whole field (masked) so it still quarantines visibly.
            for kind in kinds:
                findings.append({
                    "kind": kind, "field_path": _display_path(segs),
                    "path_segments": list(segs),
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
    for segs, text in _walk_strings(case):
        if not text:
            continue
        for res in engine.analyze(text=text, language="en"):
            findings.append({
                "kind": str(res.entity_type).lower(), "field_path": _display_path(segs),
                "path_segments": list(segs),
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
    for segs, text in _walk_strings(case):
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
                "kind": str(ent.get("Type", "phi")).lower(), "field_path": _display_path(segs),
                "path_segments": list(segs),
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
    never automatic. Navigation is by the finding's ``path_segments`` (keys /
    indices), so field keys containing dots or brackets (FHIR vitals like
    "Oxygen saturation [%]") scrub correctly instead of crashing (review
    finding). Spans apply right-to-left per field so earlier replacements don't
    shift later offsets."""
    import copy
    out = copy.deepcopy(case or {})

    by_path: Dict[tuple, List[List[int]]] = {}
    for f in findings or []:
        segs = f.get("path_segments")
        span = f.get("span")
        if segs and isinstance(span, (list, tuple)) and len(span) == 2:
            by_path.setdefault(tuple(segs), []).append([int(span[0]), int(span[1])])

    for segs, spans in by_path.items():
        # Walk to the PARENT container of the string leaf.
        node: Any = out
        ok = True
        for s in segs[:-1]:
            if isinstance(node, dict) and s in node:
                node = node[s]
            elif isinstance(node, list) and isinstance(s, int) and 0 <= s < len(node):
                node = node[s]
            else:
                ok = False
                break
        if not ok:
            continue
        leaf = segs[-1]
        container_ok = (
            (isinstance(node, dict) and leaf in node and isinstance(node[leaf], str))
            or (isinstance(node, list) and isinstance(leaf, int)
                and 0 <= leaf < len(node) and isinstance(node[leaf], str))
        )
        if not container_ok:
            continue
        s = node[leaf]
        for start_i, end_i in sorted(spans, key=lambda x: -x[0]):
            if 0 <= start_i < end_i <= len(s):
                s = s[:start_i] + "[redacted]" + s[end_i:]
        node[leaf] = s
    return out
