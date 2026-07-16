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

import re
from typing import Any, Dict, List, Optional

from asclepius.constants import RUBRIC_AXES, RUBRIC_CORE_AXES, RUBRIC_TIERS, tier_for_points

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


# ─── FIX-1: machine-checkable (specific) vs vague criteria ────────────────────
# A criterion that says "manages electrolytes appropriately" is ungradeable. We flag
# vague language and require a concrete, checkable claim (a fact / drug / dose /
# threshold). This is a NUDGE (not a hard block), but a non-specific critical/important
# criterion does not count toward "premium" (FIX-4).
_VAGUE_MARKERS = (
    "better than", "safer than", "clearer than", "more accurate than", "plausible alternative",
    "appropriately", "as appropriate", "as needed", "as indicated", "adequately", "properly",
    "correctly manage", "manages appropriately", "reasonable", "good clinical", "high quality",
)
# Concreteness signals: a number/threshold, a unit, or a specific clinical entity.
_UNIT_RE = re.compile(
    r"\b(mg|mcg|g|mmol|meq|ml|l|mmhg|mg/dl|mmol/l|meq/l|g/dl|ml/min|ml/kg|units?|%|mosm|mosm/kg)\b")
_CLINICAL_RE = re.compile(
    r"\b(calcium|potassium|sodium|magnesium|phosphate|bicarbonate|chloride|insulin|dextrose|"
    r"dialysate|dialysis|hemodialysis|thiazide|hydrochlorothiazide|loop diuretic|furosemide|"
    r"finerenone|spironolactone|amiloride|eplerenone|kayexalate|patiromer|osmolality|osmolarity|"
    r"creatinine|egfr|fena|feurea|urine|urea|guideline|kdigo|contraindicat|acei|arb|nsaid|sglt2|"
    r"hyperkalemia|hypokalemia|hyponatremia|hypernatremia|acidosis|alkalosis|volume|ddavp|"
    r"hypertonic saline|normal saline|fluid restriction|peaked t|ecg|ekg)\b")


def is_specific_text(text: Optional[str]) -> bool:
    """True when a criterion names a concrete, machine-checkable entity (a number,
    dose/unit, or a specific clinical term) and is not couched in vague language
    (FIX-1). Deterministic + offline. Empty text is not specific."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(m in t for m in _VAGUE_MARKERS):
        return False
    if re.search(r"\d", t):            # a number / threshold / dose
        return True
    if _UNIT_RE.search(t):             # a clinical unit
        return True
    if _CLINICAL_RE.search(t):         # a specific drug / lab / entity
        return True
    return False


def _clip_sentence(text: str, cap: int = 400) -> str:
    """Keep the full step text (FIX-1 de-truncation) up to ``cap`` chars, cutting at a
    sentence boundary rather than mid-word/mid-sentence. No 160-char fragments."""
    t = (text or "").strip()
    if len(t) <= cap:
        return t
    window = t[:cap]
    cut = max(window.rfind(". "), window.rfind("; "), window.rfind(", "))
    if cut >= int(cap * 0.5):          # only honor a boundary that isn't absurdly early
        return window[:cut + 1].strip()
    sp = window.rfind(" ")
    return (window[:sp] if sp > 0 else window).strip() + "…"


def propose_rubric(task: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Auto-seed proposed rubric criteria from the doctor's already-captured tags
    (FEAT-2). Returns a list of ``{text, points, axis, source}`` suggestions —
    NEVER applied without confirmation. Deterministic + deduped by text."""
    payload = payload or {}
    verdict = payload.get("verdict")
    out: List[Dict[str, Any]] = []

    # FIX-1: seed CONCRETE positives from the case's decisive data (``ground_truth.
    # key_data``) — "A correct answer accounts for urine osmolality 120 (LOW)" names a
    # specific datum, so it is machine-checkable rather than "is accurate". Server-side
    # ground_truth is the answer key; only the short key_data phrases are surfaced.
    gt = ((task or {}).get("case") or {}).get("ground_truth") or {}
    for datum in list(gt.get("key_data") or [])[:4]:
        d = (str(datum) or "").strip()
        if not d:
            continue
        out.append({
            "text": f"A correct answer accounts for {_clip_sentence(d, 200)}.",
            "points": 7.0,               # important — a decisive datum
            "axis": "accuracy",
            "source": "key_data",
        })

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
            # FIX-1: keep the FULL corrected/step text (no 160-char fragment) so the
            # criterion is a concrete, gradeable claim.
            out.append({
                "text": f"A correct answer does not make this error: {_clip_sentence(orig)}" if orig
                        else f"A correct answer performs this step correctly: {_clip_sentence(stext)}",
                "points": _DEFAULT_NEG,
                "axis": "reasoning",
                "source": "corrected_step",
            })
        elif (s.get("confirmed") or (s.get("label") == "good")):
            out.append({
                "text": f"A correct answer includes: {_clip_sentence(stext)}",
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
        c["specific"] = is_specific_text(c["text"])   # FIX-1 concreteness flag
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
        entry = {
            "text": text,
            "points": round(pts, 2),
            "axis": _axis(c.get("axis")),
            "source": c.get("source") or "manual",
            "tier": tier,
            "critical": tier == "critical",
            # FIX-1: concreteness is recomputed from the final text (never trusted from wire).
            "specific": is_specific_text(text),
        }
        # FIX-3: carry a valid per-criterion evidence anchor through to the record.
        anchor = c.get("evidence_anchor")
        if isinstance(anchor, dict) and _anchor_is_valid(anchor):
            entry["evidence_anchor"] = anchor
        out.append(entry)
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


# ─── FIX-3: evidence grounding ────────────────────────────────────────────────
def _anchor_is_valid(anchor: Optional[Dict[str, Any]]) -> bool:
    """A citation anchor is meaningful when it names a source (citation_text or an
    identifier like a PMID/DOI/guideline id)."""
    if not isinstance(anchor, dict):
        return False
    return bool((anchor.get("citation_text") or "").strip() or (anchor.get("identifier") or "").strip())


def grounding_summary(criteria: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """FIX-3: a rubric is ``grounded`` when EVERY critical criterion carries a valid
    evidence anchor (a grounded safety criterion is defensible, not an opinion).
    Returns ``{grounded, n_grounded_criteria, n_critical}``."""
    crit = [c for c in (criteria or []) if isinstance(c, dict)]
    n_grounded = sum(1 for c in crit if _anchor_is_valid(c.get("evidence_anchor")))
    criticals = [c for c in crit if c.get("critical") or c.get("tier") == "critical"]
    all_critical_grounded = bool(criticals) and all(_anchor_is_valid(c.get("evidence_anchor")) for c in criticals)
    return {"grounded": all_critical_grounded, "n_grounded_criteria": n_grounded, "n_critical": len(criticals)}


# ─── FIX-4: completeness / premium gate ───────────────────────────────────────
# A rich rubric (a real reusable grader) ships as ``premium``; a thin one still ships
# but flagged ``standard`` at base price. The critical-negative half already ships as
# the submit gate — this ADDS the ≥5-criteria, ≥3-axis, and all-critical/important-
# specific (FIX-1) checks on top. Bar values are the documented default.
_PREMIUM_MIN_CRITERIA = 5
_PREMIUM_MIN_AXES = 3


def rubric_completeness(criteria: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Compute the completeness scorecard + ``premium`` flag (FIX-4). Deterministic.
    ``premium`` requires: ≥5 criteria, ≥1 positive AND ≥1 negative, ≥1 CRITICAL
    negative, ≥3 axes covered, and every critical/important criterion ``specific``
    (FIX-1). Returns the flags + the list of what's missing (for a ≤60s UI fix)."""
    # Filter blank-text criteria (matches the frontend's `(c.text||'').trim()` filter
    # exactly, so a transient empty row never changes the score). Backend inputs are
    # already normalized, so this is defensive parity, not a behavior change.
    crit = [c for c in (criteria or []) if isinstance(c, dict) and str(c.get("text") or "").strip()]
    n = len(crit)
    n_pos = sum(1 for c in crit if (c.get("points") or 0) > 0)
    n_neg = sum(1 for c in crit if (c.get("points") or 0) < 0)
    axes = {c.get("axis") for c in crit if c.get("axis")}
    has_crit_neg = has_critical_negative(crit)
    # A non-specific critical/important criterion does not count toward premium (FIX-1).
    # Recompute tier (from |points|) and specificity (from text) rather than trusting the
    # stored fields, so this matches the frontend mirror byte-for-byte on ANY input shape
    # (the fields may be absent on a freshly-seeded criterion).
    key_tiers = ("critical", "important")
    key_criteria = [c for c in crit if tier_for_points(c.get("points")) in key_tiers]
    all_key_specific = (
        all(is_specific_text(c.get("text")) for c in key_criteria) if key_criteria else False
    )

    missing: List[str] = []
    if n < _PREMIUM_MIN_CRITERIA:
        missing.append(f"add {_PREMIUM_MIN_CRITERIA - n} more criteria (≥{_PREMIUM_MIN_CRITERIA} total)")
    if n_pos < 1:
        missing.append("add ≥1 positive criterion")
    if n_neg < 1:
        missing.append("add ≥1 negative criterion")
    if not has_crit_neg:
        missing.append("name ≥1 CRITICAL negative (−8 to −10)")
    if len(axes) < _PREMIUM_MIN_AXES:
        missing.append(f"cover ≥{_PREMIUM_MIN_AXES} axes (have {len(axes)})")
    if not all_key_specific:
        missing.append("make every critical/important criterion specific (name the fact/drug/dose/threshold)")

    premium = not missing

    # FIX-7 (axis-coverage nudge): ADVISORY, never a gate. A defensible grader almost
    # always touches safety, accuracy, and reasoning; if one is missing we surface a
    # suggestion so the physician can round out coverage — but the rubric still ships
    # (and can still be premium). ``core_axes_missing`` is kept OUT of ``missing`` so
    # it never blocks the premium gate.
    core_axes_missing = [a for a in RUBRIC_CORE_AXES if a not in axes]
    nudges: List[str] = []
    if core_axes_missing:
        nudges.append(
            "consider covering " + ", ".join(core_axes_missing)
            + " (a grader is stronger when it scores safety, accuracy, and reasoning)"
        )

    return {
        "premium": premium,
        "tier": "premium" if premium else "standard",
        "n_criteria": n,
        "n_positive": n_pos,
        "n_negative": n_neg,
        "n_axes": len(axes),
        "axes": sorted(a for a in axes if a),
        "has_critical_negative": has_crit_neg,
        "all_key_specific": all_key_specific,
        "missing": missing,
        # FIX-7: advisory core-axis coverage (does NOT affect premium/missing).
        "core_axes": list(RUBRIC_CORE_AXES),
        "core_axes_missing": core_axes_missing,
        "covers_core_axes": not core_axes_missing,
        "nudges": nudges,
    }


def is_premium(criteria: Optional[List[Dict[str, Any]]]) -> bool:
    return bool(rubric_completeness(criteria)["premium"])


# ─── FIX-8: failure-surface coverage (deterministic half) ─────────────────────
def failure_coverage(
    criteria: Optional[List[Dict[str, Any]]], task: Optional[Dict[str, Any]],
    submission: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """FIX-8: the rubric's NEGATIVE criteria should COVER the case's failure surface —
    each error tag the doctor put on the rejected answer, and the case's central trap
    (``hard_hook`` / ``reasoning_divergence`` / ``ai_failure_mode``). A negative for
    each named failure is what makes the rubric RL-safe AND makes §D's failure taxonomy
    complete. Returns ``{covered, uncovered_failure_modes}`` (deterministic; the LLM
    hackability probe is added separately by grader_eval)."""
    crit = [c for c in (criteria or []) if isinstance(c, dict)]
    negatives = [c for c in crit if (c.get("points") or 0) < 0]
    neg_sources = " ".join(str(c.get("source") or "") for c in negatives).lower()
    neg_text = " ".join(str(c.get("text") or "") for c in negatives).lower()
    uncovered: List[str] = []
    payload = (submission or {}).get("payload") or {}
    error_tags = list(((payload.get("rejected_critique") or {}).get("error_tags")) or [])
    for tag in error_tags:
        t = str(tag).lower()
        if f"error_tag:{t}" in neg_sources or t.replace("_", " ") in neg_text:
            continue
        uncovered.append(tag)
    gen = (task or {}).get("generation") or {}
    case = (task or {}).get("case") or {}
    has_trap = bool(gen.get("ai_failure_mode") or case.get("hard_hook") or case.get("reasoning_divergence"))
    if has_trap and not negatives:
        uncovered.append("central_trap")
    return {"covered": not uncovered, "uncovered_failure_modes": uncovered}
