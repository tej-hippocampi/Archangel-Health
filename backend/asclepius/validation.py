"""Auto-validation (PRD §5 step 3, §8).

Runs on every submission BEFORE the QA gate. Checks:
  * verdict + required-field completeness for the chosen path
  * packaged records present + non-empty required fields
  * ``time_spent_sec`` above a configurable floor (too-fast == flag)
  * defensive PHI / direct-identifier scan — a self-contained baseline scanner
    that ALWAYS runs (prefers ``gold.deid`` when that package is importable, but
    NEVER silently degrades to a no-op)
  * duplicate detection via a normalized hash of prompt + key texts

Returns an issues list. Empty issues == passes auto-validation. Any issue keeps
the record OUT of ``export_ready`` and routes the submission to QA with reasons
(PRD §5: "no record can reach export_ready without passing auto-validation").
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from asclepius.constants import (
    CONTAMINATION_SIGNATURES,
    ERROR_TAXONOMY,
    EVIDENCE_SOURCE_TYPES,
    VERDICTS,
    WHY_BETTER_TAGS,
)

# ─── PHI / direct-identifier scanner (BLOCKER 3) ──────────────────────────────
# A self-contained baseline so the PHI scan ALWAYS runs even when the optional
# ``gold`` package is absent. The earlier ``except: return []`` fallback made the
# scan a SILENT NO-OP while records were still stamped ``contains_phi: false`` —
# a false trust claim on the exact dimension we sell. We now: (1) prefer the
# richer ``gold.deid`` scanner when importable, (2) otherwise use this baseline,
# and (3) never fall back to a no-op. Returns matched identifier *kinds* (not the
# matched values), keeping the ``phi:<kinds>`` issue format intact.
_PHI_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn", re.compile(r"\b(?:MRN|MBI|Medical Record(?:\s*Number)?)\s*[:#]?\s*[A-Za-z0-9\-]+\b", re.I)),
    ("date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
]


def _baseline_residual_identifiers(text: Optional[str]) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    for kind, pat in _PHI_PATTERNS:
        if pat.search(text):
            found.append(kind)
    return sorted(set(found))


try:  # prefer the richer shared Safe-Harbor scanner when the gold package exists
    from gold.deid import residual_identifiers as _gold_residual_identifiers

    PHI_SCANNER = "gold.deid"

    def residual_identifiers(text: Optional[str]) -> List[str]:
        return list(_gold_residual_identifiers(text))
except Exception:  # gold absent — use the self-contained baseline (NOT a no-op)
    PHI_SCANNER = "baseline"

    def residual_identifiers(text: Optional[str]) -> List[str]:
        return _baseline_residual_identifiers(text)


def time_floor_sec() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_TIME_FLOOR_SEC", "20"))
    except ValueError:
        return 20


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


# ─── Evidence anchors & grounding (opt §1.2) ──────────────────────────────────
def is_valid_anchor(anchor: Optional[Dict[str, Any]]) -> bool:
    """A valid evidence anchor has a non-empty citation, a known source_type, and
    a non-empty identifier (e.g. KDIGO 2024 / PMID / DOI)."""
    if not anchor or not isinstance(anchor, dict):
        return False
    if not (anchor.get("citation_text") or "").strip():
        return False
    if (anchor.get("source_type") or "") not in EVIDENCE_SOURCE_TYPES:
        return False
    if not (anchor.get("identifier") or "").strip():
        return False
    return True


def _rationale_anchor(payload: Dict[str, Any], verdict: Optional[str]) -> Optional[Dict[str, Any]]:
    if verdict in ("A_better", "B_better"):
        return (payload.get("chosen_revision") or {}).get("evidence_anchor")
    if verdict == "both_inadequate":
        return (payload.get("from_scratch") or {}).get("evidence_anchor")
    return None


def _reasoning_steps(payload: Dict[str, Any], verdict: Optional[str]) -> List[Dict[str, Any]]:
    if verdict == "both_inadequate":
        steps = (payload.get("from_scratch") or {}).get("reasoning_steps") or []
    else:
        steps = payload.get("reasoning_steps") or []
    return list(steps)


def grounding_status(task: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Whether a submission meets the grounding bar required for ``required`` mode.

    Returns ``(satisfied, reasons)``. ``satisfied`` requires a valid rationale
    anchor, AND — on reasoning tasks (capture_reasoning, or any steps present) —
    a valid anchor on every reasoning step (opt §1.2)."""
    verdict = payload.get("verdict")
    reasons: List[str] = []
    if not is_valid_anchor(_rationale_anchor(payload, verdict)):
        reasons.append("missing_rationale_anchor")
    steps = _reasoning_steps(payload, verdict)
    is_reasoning_task = bool(task.get("capture_reasoning")) or bool(steps)
    if is_reasoning_task and steps:
        for s in steps:
            if not is_valid_anchor(s.get("evidence_anchor")):
                reasons.append("missing_step_anchor")
                break
    return (len(reasons) == 0, reasons)


def is_grounded(task: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """Premium-tier flag: at least the rationale carries a valid evidence anchor."""
    verdict = payload.get("verdict")
    return is_valid_anchor(_rationale_anchor(payload, verdict))


# ─── Contamination check (opt §1.5) ───────────────────────────────────────────
def contamination_hits(prompt: Optional[str]) -> List[str]:
    """Flag prompts that look lifted from public medical benchmarks.

    Substring/shingle check against known benchmark fingerprints (MedQA,
    MedMCQA, PubMedQA, MMLU-med). Returns the matched benchmark names (empty ==
    clean)."""
    norm = _norm(prompt)
    if not norm:
        return []
    hits: List[str] = []
    for signature, benchmark in CONTAMINATION_SIGNATURES.items():
        if signature in norm:
            hits.append(benchmark)
    return sorted(set(hits))


def compute_dedupe_hash(task: Dict[str, Any], submission_payload: Dict[str, Any]) -> str:
    """Stable hash of the normalized prompt + candidate texts + key free-text so
    an identical re-submission is detectable (PRD §5)."""
    parts = [_norm(task.get("prompt"))]
    for c in task.get("candidate_answers", []) or []:
        parts.append(_norm(c.get("text")))
    revision = submission_payload.get("chosen_revision") or {}
    parts.append(_norm(revision.get("revised_text")))
    fs = submission_payload.get("from_scratch") or {}
    parts.append(_norm(fs.get("ideal_answer")))
    parts.append(_norm(submission_payload.get("verdict")))
    joined = "||".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _scan_phi(texts: List[Optional[str]]) -> List[str]:
    kinds: List[str] = []
    for t in texts:
        kinds.extend(residual_identifiers(t))
    return sorted(set(kinds))


def validate_submission(
    task: Dict[str, Any],
    submission: Dict[str, Any],
    records: List[Dict[str, Any]],
    *,
    is_duplicate: bool = False,
) -> Dict[str, Any]:
    payload = submission.get("payload") or {}
    verdict = submission.get("verdict") or payload.get("verdict")
    issues: List[str] = []

    # 1. verdict + completeness
    if verdict not in VERDICTS:
        issues.append("invalid_verdict")
    elif verdict in ("A_better", "B_better"):
        if not submission.get("chosen_id") or not submission.get("rejected_id"):
            issues.append("missing_chosen_or_rejected")
        critique = payload.get("rejected_critique") or {}
        bad_tags = [t for t in (critique.get("error_tags") or []) if t not in ERROR_TAXONOMY]
        if bad_tags:
            issues.append("unknown_error_tag")
        revision = payload.get("chosen_revision") or {}
        bad_why = [t for t in (revision.get("why_better_tags") or []) if t not in WHY_BETTER_TAGS]
        if bad_why:
            issues.append("unknown_why_better_tag")
    elif verdict == "both_inadequate":
        fs = payload.get("from_scratch") or {}
        if not (fs.get("ideal_answer") or "").strip():
            issues.append("missing_ideal_answer")

    # 2. packaged records present + required fields non-empty
    if not records:
        issues.append("no_records_packaged")
    for r in records:
        if not (r.get("prompt") or "").strip():
            issues.append("empty_prompt")
        if r["type"] == "preference":
            if not (r.get("chosen") or "").strip() or not (r.get("rejected") or "").strip():
                issues.append("empty_preference_text")
        elif r["type"] == "ideal_answer":
            if not (r.get("ideal_answer") or "").strip():
                issues.append("empty_ideal_answer")
        elif r["type"] == "reasoning_trace":
            if not r.get("steps"):
                issues.append("empty_reasoning_trace")

    # 3. time floor (too-fast == suspicious)
    if int(submission.get("time_spent_sec") or 0) < time_floor_sec():
        issues.append("too_fast")

    # 4. defensive PHI scan over prompt + every emitted text field
    scan_targets: List[Optional[str]] = [task.get("prompt")]
    for c in task.get("candidate_answers", []) or []:
        scan_targets.append(c.get("text"))
    for r in records:
        scan_targets.extend([r.get("chosen"), r.get("rejected"), r.get("ideal_answer"), r.get("rationale")])
        for step in r.get("steps") or []:
            scan_targets.append(step.get("text"))
    # A PHI scanner must always be available; a missing scanner is a validation
    # FAILURE, never a silent pass (BLOCKER 3).
    if not PHI_SCANNER:
        issues.append("phi_scanner_unavailable")
    phi = _scan_phi(scan_targets)
    if phi:
        issues.append("phi:" + ",".join(phi))

    # 5. duplicate
    if is_duplicate:
        issues.append("duplicate")

    # 6. contamination — prompt lifted from a public benchmark (opt §1.5)
    contamination = contamination_hits(task.get("prompt"))
    if contamination:
        issues.append("contamination:" + ",".join(contamination))

    # 7. grounding_mode=required — must carry a valid evidence anchor (opt §1.2)
    if (task.get("grounding_mode") or "optional") == "required":
        ok, reasons = grounding_status(task, payload)
        if not ok:
            issues.extend(reasons)

    # 8. license / rights attestation present on every emitted record (opt §1.4)
    for r in records:
        if not (r.get("license") or "").strip():
            issues.append("missing_license")
            break
    for r in records:
        if r.get("ip_cleared") is not True or r.get("contains_phi") is not False:
            issues.append("missing_rights_attestation")
            break

    return {
        "valid": len(issues) == 0,
        "issues": sorted(set(issues)),
        "phi_kinds": phi,
        "contamination": contamination,
    }
