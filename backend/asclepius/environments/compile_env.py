"""The case→environment compiler (PRD §8.4) — the hard step, made explicit.

A synthetic/gold case is *authored as* an environment (ground truth + withheld
state are set by construction). A real de-identified chart is NOT — it is
continuous, ambiguous, and full of the answer. Converting it correctly is a
distinct compilation step and is where a real-data RL pipeline silently breaks
(PRD §8.4). This module is the single place the real→environment transformation
lives.

The compiler runs, in order (PRD §8.4):
  1. Decision-point identification.
  2. Temporal partition (leakage-critical) — enforced downstream by ``state.py``.
  3. Ground-truth resolution (tiered, with recorded confidence).
  4. Verifiability qualification filter (which real cases even qualify).
  5. Per-observation de-identification enforcement (at the tool boundary, in
     ``state.py``; the compiler records the requirement).
  6. Field-coverage handling.

Output: a ``CompiledEnv`` dict the runnable env (§4.5) and verifier (§5) read from.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..cases import as_dict
from . import catalog

# The per-case critical-negative flags the verifier hard-fails on (PRD §5.1) are
# derived from the case's own text — no separate rubric needed — via two lenses:
#   * AVOID: the CORRECT answer / hard_hook naming an action to avoid ("do not
#     give X", "avoid X").
#   * RECOMMEND: the INTENDED-FLAWED candidate answer / failure-mode label RECO-
#     MMENDING the unsafe action ("give X", "SIADH → 3% saline"). The flawed
#     answer's recommended intervention IS the critical negative.
_AVOID_PATTERNS = [
    re.compile(r"(?:do not|don't|never|avoid(?:ed)?|must not|should not)\s+(?:give\s+|administer\s+|start\s+|order\s+|bolus\s+)?([a-z0-9][a-z0-9 %\-/]{2,36})", re.IGNORECASE),
]
_RECOMMEND_PATTERNS = [
    re.compile(r"(?:give|administer|start|bolus|push|treat with|order)\s+([a-z0-9][a-z0-9 %\-/]{2,36})", re.IGNORECASE),
    re.compile(r"[→>]\s*([a-z0-9][a-z0-9 %\-/]{2,36})", re.IGNORECASE),  # "SIADH → 3% saline"
]
_STOPWORDS = {"the", "further", "additional", "a", "an", "more", "this", "that", "them", "it",
              "fluids" if False else "", "to", "and", "with"}


def _apply(patterns, text: str) -> List[str]:
    out: List[str] = []
    for pat in patterns:
        for m in pat.finditer(text or ""):
            phrase = m.group(1).strip().rstrip(".,;:)")
            # keep the leading drug/intervention noun (drop trailing clauses)
            phrase = re.split(r"\b(?:rapidly|now|to|because|since|which|and|then)\b", phrase)[0].strip()
            if 3 <= len(phrase) <= 36 and phrase.lower() not in _STOPWORDS:
                out.append(phrase)
    return out


def _extract_critical_negatives(
    case: Dict[str, Any], recommend_hint: Optional[str] = None,
) -> List[str]:
    avoid_text = " ".join(
        str(x) for x in [
            (case.get("ground_truth") or {}).get("answer"),
            (case.get("ground_truth") or {}).get("rationale"),
            case.get("hard_hook"),
        ] if x
    )
    flags = _apply(_AVOID_PATTERNS, avoid_text) + _apply(_RECOMMEND_PATTERNS, recommend_hint or "")
    seen, out = set(), []
    for f in flags:
        k = f.lower()
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out[:6]


class CompileError(ValueError):
    """A case that cannot be honestly compiled into a (deterministic) environment
    for the requested task type — routed to the preference/practice-variation
    path instead (PRD §8.4.4), never forced."""


# ─── Ground-truth resolution (PRD §8.4.3) ─────────────────────────────────────
def _resolve_ground_truth(case: Dict[str, Any], task_type: str) -> Dict[str, Any]:
    """Tiered ground truth with a recorded source + confidence. For gold/synthetic
    the authored ``ground_truth`` is authoritative (confidence 1.0). For real
    cases the priority is linked-outcome > treating-physician-action >
    physician-annotator-ratified; only unambiguous elements become deterministic
    checks (PRD §8.4.3)."""
    gt = case.get("ground_truth") or {}
    source = "authored"
    confidence = 1.0
    if (case.get("case_source") or "synthetic") == "real_deid":
        # A real case has NO authored answer key by construction. The strongest
        # signal is the linked outcome (future zone); absent that, the treating
        # physician's action; absent that, a physician annotator's ratified answer.
        if _has_future_outcome(case):
            source, confidence = "linked_outcome", 0.9
        elif gt.get("answer"):
            source, confidence = "treating_physician_action", 0.6
        else:
            source, confidence = "physician_annotator_ratified", 0.4
    # Only emit deterministic checks where the ground truth is unambiguous.
    checks = catalog.template_checks(task_type)
    if confidence < 0.5:
        # Ambiguous → drop deterministic/critical checks to the rubric/PRM path
        # (PRD §8.4.3: "never to a false exact match").
        checks = [c for c in checks if c["type"] in ("rubric",)]
    return {
        "answer": gt.get("answer", ""),
        "rationale": gt.get("rationale"),
        "key_data": gt.get("key_data") or [],
        "source": source,
        "confidence": confidence,
        "deterministic_checks": checks,
    }


def _has_future_outcome(case: Dict[str, Any]) -> bool:
    for p in case.get("lab_panels") or []:
        try:
            if int(p.get("collected_offset_days") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


# ─── Decision point + temporal partition (PRD §8.4.1–2) ───────────────────────
def _decision_offset(case: Dict[str, Any], explicit: Optional[int]) -> int:
    """Fix the decision point as a day-offset. For an authored case the whole
    chart is 'then' (offset 0). For a real case, the physician marks it during the
    §0.5 validity gate; absent an explicit mark we take the max non-positive
    offset present (drop the agent in at the latest pre-outcome timepoint)."""
    if explicit is not None:
        return int(explicit)
    offsets = []
    for p in case.get("lab_panels") or []:
        try:
            offsets.append(int(p.get("collected_offset_days") or 0))
        except (TypeError, ValueError):
            continue
    non_future = [o for o in offsets if o <= 0]
    return max(non_future) if non_future else 0


# ─── Verifiability qualification filter (PRD §8.4.4) ──────────────────────────
def _qualifies_deterministic(case: Dict[str, Any], gt: Dict[str, Any]) -> Tuple[bool, str]:
    """A real case becomes a deterministic environment only if it has a clear
    decision point, a decisive discriminating datum, and a checkable end state.
    Otherwise route to the preference/practice-variation path (still valuable)."""
    if not gt.get("answer"):
        return False, "no checkable end state (missing ground-truth answer)"
    if not (gt.get("key_data")):
        return False, "no decisive discriminating datum recorded"
    if gt.get("confidence", 0) < 0.5:
        return False, "end state is multi-defensible / low-confidence"
    return True, ""


# ─── Field-coverage handling (PRD §8.4.6) ─────────────────────────────────────
def _coverage_ok(case: Dict[str, Any], task_type: str) -> Tuple[bool, List[str]]:
    """Validate the case can support the task template's tools. A read tool over a
    missing field returns honest ``not_available`` at run time; only a
    decision-critical missing field disqualifies the case for this task type."""
    missing = []
    if not (case.get("lab_panels")):
        missing.append("lab_panels")
    # diagnostic/longitudinal need at least labs; med-management needs a med list.
    if task_type == "medication_management" and not (case.get("medications")):
        return False, ["medications (decision-critical for medication_management)"]
    return True, missing


def compile_environment(
    case: Any,
    *,
    task_type: str,
    question: str = "",
    decision_offset_days: Optional[int] = None,
    require_deterministic: bool = True,
    critical_hint_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile a validated ``ClinicalCase`` into a runnable environment spec.

    ``require_deterministic`` (default True) enforces the §8.4.4 filter: a real
    case that can't be honestly graded deterministically raises ``CompileError``
    (the caller routes it to the preference path). Gold/synthetic cases are
    authored-as-environment and always qualify.
    """
    c = as_dict(case) or {}
    task_type = task_type if task_type in catalog.task_types() else "diagnostic_workup"
    case_source = c.get("case_source") or "synthetic"

    dp = _decision_offset(c, decision_offset_days)
    gt = _resolve_ground_truth(c, task_type)

    ok_cov, missing = _coverage_ok(c, task_type)
    if not ok_cov:
        raise CompileError(f"field coverage insufficient for {task_type}: {missing}")

    is_real = case_source == "real_deid"
    if is_real and require_deterministic:
        qual, why = _qualifies_deterministic(c, gt)
        if not qual:
            raise CompileError(f"real case does not qualify as a deterministic environment: {why}")

    # observable_now = the panels at/before the decision point that are shown at
    # reset; earnable_map = the deeper panels/notes/studies the agent must request.
    observable_panels = _observable_now_panels(c, dp)
    earnable = _earnable_map(c, dp, observable_panels)

    return {
        "case_ref": c.get("case_id"),
        "case_source": case_source,
        "specialty": c.get("specialty") or "general",
        "task_template": task_type,
        "question": question,
        "decision_point": {"offset_days": dp},
        "observable_state": {"panels": observable_panels},
        "earnable_map": earnable,
        "held_out_outcome": {"has_future": _has_future_outcome(c)},
        "ground_truth": gt,
        "critical_negatives": _extract_critical_negatives(c, recommend_hint=critical_hint_text),
        "allowed_tools": catalog.allowed_tools(task_type),
        "checks": gt["deterministic_checks"],
        "deid_verified": True if is_real else None,
        "deid_recheck_required": is_real,
        # The full case travels with the compiled env so the runnable env can build
        # EHRState (the temporal cutoff is enforced by state.py, not the author).
        "case": c,
        "missing_fields": missing,
    }


def _observable_now_panels(case: Dict[str, Any], dp: int) -> List[str]:
    """Only the FIRST presenting panel is visible at reset; every deeper panel —
    including panels at the same day-offset — must be EARNED via ``get_labs``
    (PRD §13: "information must be earned, or the environment is trivial"). This
    is what makes a §0.5-authored case (all panels at offset 0) non-trivial."""
    vis = []
    for idx, p in enumerate(case.get("lab_panels") or []):
        try:
            off = int(p.get("collected_offset_days") or 0)
        except (TypeError, ValueError):
            off = 0
        if off > dp:
            continue
        vis.append((off, idx, p.get("panel")))
    if not vis:
        return []
    # earliest offset, then earliest declared order → the presenting panel only.
    vis.sort(key=lambda t: (t[0], t[1]))
    return [vis[0][2]]


def _earnable_map(case: Dict[str, Any], dp: int, observable: List[str]) -> Dict[str, Any]:
    obs = set(observable)
    panels = []
    for p in case.get("lab_panels") or []:
        try:
            off = int(p.get("collected_offset_days") or 0)
        except (TypeError, ValueError):
            off = 0
        if off > dp:  # future zone — never earnable
            continue
        name = p.get("panel")
        if name not in obs:
            panels.append(name)
    return {
        "labs": panels,
        "notes": [n.get("note_type") for n in (case.get("notes") or [])],
        "studies": [s.get("modality") for s in (case.get("studies") or [])],
        "medications": bool(case.get("medications")),
    }
