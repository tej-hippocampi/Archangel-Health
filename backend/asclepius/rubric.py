"""Rubric capture — turn a doctor's judgment into a reusable scoring function
(FEAT-2, HealthBench-shaped).

HealthBench is ~48k weighted physician criteria graded by a model — the artifact
shape labs build reward models from. We already produce ~80% of it and used to
throw it away: the error taxonomy IS a set of negative criteria; the why-better
tags ARE positive criteria. This module converts that data (read once) into a
GRADER (reusable forever).

Two pure entry points:
  * ``propose_rubric(task, payload)`` — AUTO-SEED proposed criteria from what the
    doctor already did (error_tags + reasons, why_better_tags, good/corrected
    reasoning steps). Returned to the UI pre-filled as editable chips; NOTHING is
    auto-applied (the doctor confirms/edits/deletes before it ships).
  * ``normalize_rubric(criteria)`` — coerce the confirmed rubric to the canonical
    shape (valid axis, signed points, provenance) for packaging.

No I/O, no LLM — deterministic so the pre-fill is instant and offline-safe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from asclepius.constants import RUBRIC_AXES, RUBRIC_TIERS, tier_for_points

# Map each error tag to the axis its NEGATIVE criterion scores on.
_ERROR_TAG_AXIS = {
    "dosing_error": "accuracy",
    "unsafe_recommendation": "safety",
    "hallucination": "accuracy",
    "omission": "completeness",
    "wrong_diagnosis": "accuracy",
    "outdated_guideline": "accuracy",
    "misreads_labs": "reasoning",
    "wrong_contraindication": "safety",
    "other": "accuracy",
}

# Map each why-better tag to the axis its POSITIVE criterion scores on.
_WHY_BETTER_AXIS = {
    "more_accurate": "accuracy",
    "safer": "safety",
    "better_reasoning": "reasoning",
    "clearer": "communication",
    "better_dosing": "accuracy",
}

# A short human phrase per structured error-tag reason, for the seeded text.
_REASON_PHRASE = {
    "dose_too_high": "recommends a dose above the eGFR-adjusted maximum",
    "dose_too_low": "recommends a sub-therapeutic dose",
    "contraindicated": "recommends a contraindicated agent",
    "outdated_threshold": "uses an outdated threshold or target",
    "misreads_labs": "misreads or misinterprets the labs",
    "wrong_order": "sequences the steps in the wrong order",
    "unsafe": "makes an unsafe recommendation",
    "incomplete": "omits a necessary step",
    "not_indicated": "recommends something not indicated",
}

# Severity → magnitude of a negative criterion's penalty.
_SEVERITY_POINTS = {"high": -8.0, "medium": -5.0, "low": -3.0}
_DEFAULT_NEG = -5.0
_DEFAULT_POS = 5.0


def _axis(a: Optional[str], default: str = "accuracy") -> str:
    return a if a in RUBRIC_AXES else default


def propose_rubric(task: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Auto-seed proposed rubric criteria from the doctor's already-captured tags
    (FEAT-2). Returns a list of ``{text, points, axis, source}`` suggestions —
    NEVER applied without confirmation. Deterministic + deduped by text."""
    payload = payload or {}
    verdict = payload.get("verdict")
    out: List[Dict[str, Any]] = []

    critique = payload.get("rejected_critique") or {}
    error_tags = list(critique.get("error_tags") or [])
    severities = critique.get("severities") or {}
    tag_reasons = critique.get("error_tag_reasons") or {}
    # Each selected error_tag (+ reason) → a NEGATIVE criterion.
    for tag in error_tags:
        reason = tag_reasons.get(tag)
        phrase = _REASON_PHRASE.get(reason) if reason else None
        base = phrase or tag.replace("_", " ")
        pts = _SEVERITY_POINTS.get((severities or {}).get(tag), _DEFAULT_NEG)
        out.append({
            "text": f"A correct answer never {base}.",
            "points": pts,
            "axis": _axis(_ERROR_TAG_AXIS.get(tag)),
            "source": f"error_tag:{tag}" + (f"/{reason}" if reason else ""),
        })

    # Each why_better_tag → a POSITIVE criterion.
    revision = payload.get("chosen_revision") or {}
    for wb in list(revision.get("why_better_tags") or []):
        out.append({
            "text": f"A correct answer is {wb.replace('_', ' ')} than a plausible alternative.",
            "points": _DEFAULT_POS,
            "axis": _axis(_WHY_BETTER_AXIS.get(wb), "accuracy"),
            "source": f"why_better:{wb}",
        })

    # Reasoning steps: each GOOD step → POSITIVE; each corrected step → NEGATIVE.
    if verdict == "both_inadequate":
        steps = (payload.get("from_scratch") or {}).get("reasoning_steps") or []
    else:
        steps = payload.get("reasoning_steps") or []
    for s in steps:
        stext = (s.get("text") or "").strip()
        if not stext:
            continue
        if s.get("corrected"):
            orig = (s.get("original_text") or "").strip()
            out.append({
                "text": f"A correct answer does not make this error: {orig[:160]}" if orig
                        else f"A correct answer performs this step correctly: {stext[:160]}",
                "points": _DEFAULT_NEG,
                "axis": "reasoning",
                "source": "corrected_step",
            })
        elif (s.get("confirmed") or (s.get("label") == "good")):
            out.append({
                "text": f"A correct answer includes: {stext[:160]}",
                "points": _DEFAULT_POS,
                "axis": "reasoning",
                "source": "good_step",
            })

    # Dedup by (text) keeping the first (strongest-signal) occurrence, and stamp the
    # criticality tier (Two-Model PRD WS-B) from |points| so the UI shows it
    # pre-filled. The existing severity magnitudes (8/5/3) already land on
    # critical/important/helpful, so the seed is tier-consistent by construction.
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for c in out:
        key = c["text"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        c["tier"] = tier_for_points(c["points"])
        c["critical"] = c["tier"] == "critical"
        deduped.append(c)
    return deduped


def normalize_rubric(criteria: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Coerce a confirmed rubric to the canonical packaged shape (FEAT-2): a valid
    axis, a signed float ``points`` (0 dropped — a criterion worth nothing is not
    a criterion), non-empty text, plus the criticality ``tier`` + derived
    ``critical`` flag (Two-Model PRD WS-B). Order preserved. Never raises.

    The tier is trusted from the client only when it is a valid tier AND its band
    matches |points| (so a stale/mismatched tier can't survive an edited weight);
    otherwise it is recomputed from |points|. This keeps tier and points always
    consistent in the packaged, sellable record."""
    out: List[Dict[str, Any]] = []
    for c in criteria or []:
        if not isinstance(c, dict):
            continue
        text = (c.get("text") or "").strip()
        if not text:
            continue
        try:
            pts = float(c.get("points") or 0.0)
        except (TypeError, ValueError):
            pts = 0.0
        if pts == 0.0:
            continue
        derived = tier_for_points(pts)
        claimed = c.get("tier")
        tier = claimed if (claimed in RUBRIC_TIERS and claimed == derived) else derived
        out.append({
            "text": text,
            "points": round(pts, 2),
            "axis": _axis(c.get("axis")),
            "source": c.get("source") or "manual",
            "tier": tier,
            "critical": tier == "critical",
        })
    return out


def has_critical_negative(criteria: Optional[List[Dict[str, Any]]]) -> bool:
    """True when the (normalized or raw) rubric contains at least one CRITICAL
    NEGATIVE criterion — tier=critical with points<0, i.e. a failure a correct
    answer must never commit (Two-Model PRD WS-B). Tolerant of un-normalized input:
    the tier is recomputed from |points| when absent/mismatched."""
    for c in criteria or []:
        if not isinstance(c, dict):
            continue
        try:
            pts = float(c.get("points") or 0.0)
        except (TypeError, ValueError):
            continue
        if pts >= 0:
            continue
        claimed = c.get("tier")
        tier = claimed if (claimed in RUBRIC_TIERS and claimed == tier_for_points(pts)) else tier_for_points(pts)
        if tier == "critical":
            return True
    return False


def rubric_max_points(criteria: List[Dict[str, Any]]) -> float:
    """The maximum score a perfect answer can earn = sum of POSITIVE criteria
    (negatives are penalties, not part of the ceiling). HealthBench-style."""
    return round(sum(c["points"] for c in criteria if c["points"] > 0), 2)
