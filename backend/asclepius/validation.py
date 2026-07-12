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
    ERROR_TAG_REASONS,
    ERROR_TAXONOMY,
    EVIDENCE_SOURCE_TYPES,
    STEP_CORRECTION_REASONS,
    VERDICTS,
    WHY_BETTER_TAGS,
    assist_time_floor_sec,
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

    # 0. The V4 packaging wall (EHR PRD §9.5): case_source=='real_deid' ⇔
    # portal_version=='v4' on everything that would ship. The router already
    # derives the version server-side; this is the belt-and-braces assertion at
    # the packaging layer — a mismatch (a bug, a direct DB write) routes the
    # submission to needs_qa and NO record ships mislabeled. Never silent.
    pv = (payload.get("portal_version") or submission.get("portal_version") or "")
    if (task.get("case_source") == "real_deid") != (pv == "v4"):
        issues.append("portal_version_case_source_mismatch")

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
        # Structured per-tag reasons (Speed Optimization §6) come from a
        # controlled vocabulary; an off-vocabulary value routes to QA (never a
        # hard reject) so the tap-to-capture signal stays clean for buyers.
        bad_reasons = [
            r for r in (critique.get("error_tag_reasons") or {}).values()
            if (r or "").strip() and r not in ERROR_TAG_REASONS
        ]
        if bad_reasons:
            issues.append("unknown_error_tag_reason")
        revision = payload.get("chosen_revision") or {}
        bad_why = [t for t in (revision.get("why_better_tags") or []) if t not in WHY_BETTER_TAGS]
        if bad_why:
            issues.append("unknown_why_better_tag")
    elif verdict == "both_inadequate":
        fs = payload.get("from_scratch") or {}
        if not (fs.get("ideal_answer") or "").strip():
            issues.append("missing_ideal_answer")

    # 1b. blind independent answer present (Eval Flow Upgrade §3). The new flow
    # captures the doctor's full ideal answer BEFORE revealing A/B; a non-flagged
    # submission missing it is routed to QA — never hard-rejected ("no lost
    # submissions"). Flagged prompts short-circuit before validation, so any
    # submission reaching here is expected to carry one.
    if not ((payload.get("independent_answer") or {}).get("text") or "").strip():
        issues.append("missing_independent_answer")

    # 1c. Edit-to-Correct gating (Reasoning Capture v2). Every captured reasoning
    # step must be explicitly resolved — confirmed as-is, corrected (with a
    # reason), or manually authored (added). A "pending" step is silence, and
    # silence is NOT endorsement; route to QA rather than ship an unreviewed step.
    # A corrected step without a valid reason can't derive a buyer-facing label,
    # so it is flagged too. As always, issues route to needs_qa — never a hard
    # reject ("no lost submissions").
    for s in _reasoning_steps(payload, verdict):
        if not (s.get("text") or "").strip():
            continue
        confirmed, corrected, added = (
            bool(s.get("confirmed")),
            bool(s.get("corrected")),
            bool(s.get("added")),
        )
        if task.get("capture_reasoning") and not (confirmed or corrected or added):
            issues.append("unreviewed_reasoning_step")
        if corrected:
            reason = (s.get("correction_reason") or "").strip()
            if not reason:
                issues.append("missing_correction_reason")
            elif reason not in STEP_CORRECTION_REASONS:
                issues.append("unknown_correction_reason")

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

    # 3b. assist time-floor guard (Speed Optimization §2): confirming a
    # model-assisted task implausibly fast smells like rubber-stamping the
    # suggestions — route to QA for a human look, never hard-reject. "Assisted"
    # is derived from the payload itself (a prelabel block OR any pre-graded
    # step), not just the client's self-declared flag, so a pregrade-only
    # bulk-confirm can't slip under the base floor.
    assisted = bool((payload.get("assist") or {}).get("prelabeled")) or any(
        s.get("suggested_label") for s in _reasoning_steps(payload, verdict)
    )
    if assisted and int(submission.get("time_spent_sec") or 0) < assist_time_floor_sec():
        issues.append("assist_too_fast")

    # 4. defensive PHI scan over prompt + every emitted text field
    scan_targets: List[Optional[str]] = [task.get("prompt")]
    for c in task.get("candidate_answers", []) or []:
        scan_targets.append(c.get("text"))
    for r in records:
        # ``stance`` (Speed Optimization §1) is free text the doctor typed/dictated
        # pre-reveal — scan it like every other emitted text field. The assist
        # block's suggested rationale is also emitted text (client-supplied on the
        # wire), so it gets the same treatment.
        scan_targets.extend([r.get("chosen"), r.get("rejected"), r.get("ideal_answer"),
                             r.get("rationale"), r.get("stance"),
                             (r.get("assist") or {}).get("suggested_rationale")])
        for step in r.get("steps") or []:
            # Scan both the step text AND its free-text critiques (Eval Flow Upgrade
            # §4 / Speed Optimization §2) — critiques can carry PHI like the body.
            scan_targets.extend([step.get("text"), step.get("critique"),
                                 step.get("suggested_critique")])
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
