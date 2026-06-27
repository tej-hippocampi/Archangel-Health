"""PHI de-identification for Gold Standard records (PRD §9.7).

Targets HIPAA Safe Harbor. Two layers, always combined for defense-in-depth:

  1. A deterministic regex pass (``_regex_deid``) that replaces direct
     identifiers with *typed* placeholders ([DATE], [PHONE], [EMAIL], [MRN],
     [ID], [PATIENT_NAME]). This runs with no API key, so de-id (and the tests)
     work offline and we always have a baseline scrub.
  2. An optional LLM scrubber (``call_llm(role="gold_deid")``) that catches names
     and context the regex misses. Enabled by ``GOLD_DEID_PROVIDER`` in
     {``llm``, ``both``} (default ``llm``). On any failure we degrade to the
     regex result rather than emitting un-scrubbed text.

Microsoft Presidio is supported when ``GOLD_DEID_PROVIDER`` is ``presidio`` or
``both`` and the package is importable; otherwise it is skipped gracefully.

A mandatory human-QA step (operator approval) always follows automated de-id —
no record is export-ready on the automated pass alone.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from gold.config import deid_provider

# ─── Deterministic typed-placeholder patterns (Safe-Harbor direct identifiers) ─
_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\.?\s+\d{{4}}\b", re.I),
]
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_MBI = re.compile(r"\b[0-9][A-Z][A-Z0-9]\d[A-Z][A-Z0-9]\d[A-Z]{2}\d{2}\b")
_MRN = re.compile(r"\b(?:MRN|MBI|Medical Record(?:\s*Number)?)\s*[:#]?\s*[A-Za-z0-9-]+\b", re.I)
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_LONG_NUM = re.compile(r"\b\d{7,}\b")
_AGE_OVER_89 = re.compile(r"\b(9\d|1\d\d)\s*(?:years?\s*old|y/?o|yo)\b", re.I)

# Direct identifiers that must NEVER survive into an export. Reused by the
# residual-identifier gate (A3) so validation/export reject any record that still
# carries one of these in any text field.
_RESIDUAL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("email", _EMAIL),
    ("phone", _PHONE),
    ("ssn", _SSN),
    ("mbi", _MBI),
    ("mrn", _MRN),
    ("long_number", _LONG_NUM),
] + [("date", p) for p in _DATE_PATTERNS]


def residual_identifiers(text: Optional[str]) -> List[str]:
    """Return the kinds of direct identifiers still present in ``text`` (empty ==
    clean). Used as the export safety net — see ``schema.validate_record``."""
    if not text:
        return []
    found: List[str] = []
    for kind, pat in _RESIDUAL_PATTERNS:
        if pat.search(text):
            found.append(kind)
    return sorted(set(found))


def _regex_deid(text: Optional[str], *, patient_name: Optional[str] = None) -> Tuple[str, List[str]]:
    if not text:
        return "", []
    out = text
    used: List[str] = []

    def sub(pattern: re.Pattern, token: str, value: str) -> None:
        nonlocal out
        new = pattern.sub(token, out)
        if new != out:
            used.append(token)
        out = new

    if patient_name:
        for term in sorted({patient_name, *patient_name.split()}, key=len, reverse=True):
            if len(term) > 1:
                esc = re.escape(term)
                new = re.sub(rf"\b{esc}[’']s\b", "[PATIENT_NAME]", out)
                new = re.sub(rf"\b{esc}\b", "[PATIENT_NAME]", new)
                if new != out:
                    used.append("[PATIENT_NAME]")
                out = new

    sub(_SSN, "[SSN]", "")
    sub(_MBI, "[MRN]", "")
    sub(_MRN, "[MRN]", "")
    sub(_EMAIL, "[EMAIL]", "")
    sub(_PHONE, "[PHONE]", "")
    sub(_AGE_OVER_89, "[AGE]", "")
    for pat in _DATE_PATTERNS:
        sub(pat, "[DATE]", "")
    sub(_ZIP, "[LOCATION]", "")
    sub(_LONG_NUM, "[ID]", "")
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out, sorted(set(used))


def _presidio_deid(text: str) -> Optional[str]:
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
    except Exception:
        return None
    try:
        analyzer = AnalyzerEngine()
        anonymizer = AnonymizerEngine()
        results = analyzer.analyze(text=text, language="en")
        return anonymizer.anonymize(text=text, analyzer_results=results).text
    except Exception:
        return None


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).rstrip("`").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None


async def _llm_deid(
    transcript: str, gold_note: str, ai_draft_note: str, *, visit_id: str
) -> Optional[Dict[str, Any]]:
    try:
        from ai.llm_client import call_llm, first_text
        from prompts.gold import GOLD_DEID_SYSTEM
    except Exception:
        return None
    user = json.dumps({
        "transcript": transcript,
        "gold_note": gold_note,
        "ai_draft_note": ai_draft_note,
    })
    try:
        resp, _rec = await call_llm(
            role="gold_deid",
            system=GOLD_DEID_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="gold_deid",
            patient_id=visit_id,
            purpose="gold_deid",
        )
        return _extract_json(first_text(resp))
    except Exception as exc:  # pragma: no cover - network/key dependent
        print(f"[gold.deid] LLM scrub failed, falling back to regex: {exc!r}")
        return None


def _regex_all(
    *texts: Optional[str], patient_name: Optional[str] = None
) -> Tuple[List[str], List[str]]:
    """Run the regex baseline over several texts; return (results, placeholders)."""
    results: List[str] = []
    placeholders: List[str] = []
    for t in texts:
        out, used = _regex_deid(t, patient_name=patient_name)
        results.append(out)
        placeholders.extend(used)
    return results, placeholders


async def deidentify(
    *,
    transcript: str,
    gold_note: str,
    ai_draft_note: str = "",
    error_labels: Optional[List[Dict[str, Any]]] = None,
    prior_auth: Optional[Dict[str, Any]] = None,
    patient_name: Optional[str] = None,
    visit_id: str = "",
) -> Dict[str, Any]:
    """De-identify EVERY free-text field that can reach an export.

    Scrubs: transcript, gold note, AI draft note, each error label's
    ``original_text`` / ``corrected_text``, and ``prior_auth.justification_text``.
    Always applies the deterministic regex baseline (off the event loop); layers
    LLM / Presidio on the long-form notes when configured. Never returns
    un-scrubbed text on failure.
    """
    provider = deid_provider()
    methods: List[str] = []
    error_labels = error_labels or []

    # Baseline regex (always) — run off the event loop (A7).
    (t_deid, n_deid, d_deid), reg_ph = await asyncio.to_thread(
        _regex_all, transcript, gold_note, ai_draft_note, patient_name=patient_name
    )
    placeholders = set(reg_ph)
    methods.append("regex")

    # Scrub label free-text in place (copies).
    labels_deid: List[Dict[str, Any]] = []
    for lbl in error_labels:
        lc = copy.deepcopy(lbl)
        for fld in ("original_text", "corrected_text"):
            if lc.get(fld):
                scrubbed, used = await asyncio.to_thread(
                    _regex_deid, lc[fld], patient_name=patient_name
                )
                lc[fld] = scrubbed
                placeholders.update(used)
        labels_deid.append(lc)

    # Scrub prior-auth justification.
    prior_auth_deid: Optional[Dict[str, Any]] = None
    if prior_auth:
        prior_auth_deid = copy.deepcopy(prior_auth)
        if prior_auth_deid.get("justification_text"):
            scrubbed, used = await asyncio.to_thread(
                _regex_deid, prior_auth_deid["justification_text"], patient_name=patient_name
            )
            prior_auth_deid["justification_text"] = scrubbed
            placeholders.update(used)

    if provider in ("llm", "both"):
        llm = await _llm_deid(t_deid, n_deid, d_deid, visit_id=visit_id)
        if llm and isinstance(llm, dict):
            t_deid = (llm.get("transcript_deid") or t_deid).strip()
            n_deid = (llm.get("gold_note_deid") or n_deid).strip()
            d_deid = (llm.get("ai_draft_note_deid") or d_deid).strip()
            for ph in llm.get("placeholders_used") or []:
                placeholders.add(ph)
            methods.append("llm")

    if provider in ("presidio", "both"):
        applied = False
        for name, val in (("t", t_deid), ("n", n_deid), ("d", d_deid)):
            p = _presidio_deid(val)
            if p is not None:
                applied = True
                if name == "t":
                    t_deid = p
                elif name == "n":
                    n_deid = p
                else:
                    d_deid = p
        if applied:
            methods.append("presidio")

    method_detail = "+".join(methods)
    return {
        "transcript_deid": t_deid,
        "gold_note_deid": n_deid,
        "ai_draft_note_deid": d_deid,
        "error_labels_deid": labels_deid,
        "prior_auth_deid": prior_auth_deid,
        "placeholders": sorted(placeholders),
        "method_detail": method_detail,
        "method": "automated (" + method_detail + ") + human QA",
    }
