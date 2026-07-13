"""Value model — dollars of sellable data value per clinician judgment
(Value-per-Minute PRD, Part A).

Pure functions, no I/O. The north-star metric is ``value ÷ time``: sellable
dollars produced per minute of clinician time. The two levers MULTIPLY —
``value_per_minute = value_per_judgment ÷ minutes_per_judgment`` — so we lift the
ratio by raising value per judgment (extract more premium signal) AND cutting
minutes (the V2 assist features), never by degrading judgment.

Two entry points:
  * ``estimate_value(records, task, submission)`` — the REALIZED estimate from
    the actual packaged records + captured attributes. Persisted per submission
    and reported (never baked into a shipped buyer-facing record).
  * ``expected_value_for_task(task)`` — a forward estimate from a task's
    attributes alone (what a full V2 capture would yield), used by value-aware
    routing to rank the queue before any judgment exists.

Every coefficient lives in ``constants.py`` and is env-overridable, so the model
recalibrates to realized sales without a code change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from asclepius import constants as C


def _difficulty_mult(difficulty: Optional[str]) -> float:
    table = C.value_difficulty_mult()
    return table.get((difficulty or "medium").strip().lower(), table.get("medium", 1.0))


def _content_value(*, has_ideal: bool, has_reasoning: bool, num_step_pairs: int) -> float:
    """§A1: PREFERENCE_BASE is unconditional (every completed judgment is at least
    a preference-grade signal); ideal + reasoning are marginal add-ons; capped
    step-pairs each add a step-level preference pair."""
    pairs = max(0, min(int(num_step_pairs or 0), C.value_step_pair_max()))
    return (
        C.value_preference_base()
        + (C.value_ideal_answer_marginal() if has_ideal else 0.0)
        + (C.value_reasoning_trace_marginal() if has_reasoning else 0.0)
        + pairs * C.value_step_pair_each()
    )


def _tier_mult(
    *,
    is_grounded: bool,
    difficulty: Optional[str],
    is_mode_b: bool,
    is_full_independent: bool,
    is_double_labeled_credentialed: bool,
    is_multimodal: bool = False,
    is_real_case: bool = False,
) -> float:
    """§A1: multiplicative premium factors, hard-capped so nothing stacks into a
    fantasy number. ``is_multimodal`` adds the structured-case premium (PRD §9);
    ``is_real_case`` adds the REAL de-identified case premium on top (EHR PRD
    §9.5 — the 2–3× tier, keyed on case_source, still under the cap)."""
    mult = (
        (C.value_grounded_mult() if is_grounded else 1.0)
        * _difficulty_mult(difficulty)
        * (C.value_on_policy_mult() if is_mode_b else 1.0)
        * (C.value_full_independent_mult() if is_full_independent else 1.0)
        * (C.value_credentialed_kappa_mult() if is_double_labeled_credentialed else 1.0)
        * (C.value_multimodal_mult() if is_multimodal else 1.0)
        * (C.value_real_case_mult() if is_real_case else 1.0)
    )
    return min(C.value_tier_mult_cap(), mult)


def _is_mode_b(task: Dict[str, Any]) -> bool:
    """Mode B (on-policy) = grading the buyer's OWN model outputs — the highest-
    value path (PRD B4). Modeled as ``source == 'lab_supplied'``."""
    return (task.get("source") or "") == "lab_supplied"


def _is_multimodal(task: Dict[str, Any], records: List[Dict[str, Any]]) -> bool:
    """A multimodal (structured-case) judgment (Synthetic Multimodal Cases PRD §9).
    True from the task's modality/case, or a record's ``context.modality``."""
    if (task.get("modality") or "text") == "multimodal" or task.get("case"):
        return True
    for r in records or []:
        if (r.get("context") or {}).get("modality") == "multimodal":
            return True
    return False


def _is_real_case(task: Dict[str, Any], records: List[Dict[str, Any]]) -> bool:
    """A REAL de-identified case judgment (EHR PRD §9.5). True from the task's
    case_source (column or case body), or a record's ``context.case_source`` —
    the ground truth, never the version label."""
    if task.get("case_source") == "real_deid":
        return True
    if ((task.get("case") or {}) or {}).get("case_source") == "real_deid":
        return True
    for r in records or []:
        if (r.get("context") or {}).get("case_source") == "real_deid":
            return True
    return False


def _annotator_credentialed(submission: Dict[str, Any]) -> bool:
    ann = submission.get("annotator") or {}
    return bool(ann.get("credentials") or ann.get("board_cert") or ann.get("specialty"))


def _records_grounded(records: List[Dict[str, Any]], submission: Dict[str, Any]) -> bool:
    if submission.get("grounded"):
        return True
    return any(r.get("grounded") for r in (records or []))


def _num_step_pairs(records: List[Dict[str, Any]]) -> int:
    total = 0
    for r in records or []:
        if r.get("type") == "reasoning_trace":
            total += len(r.get("step_pairs") or [])
    return total


def _independent_full(records: List[Dict[str, Any]], task: Dict[str, Any], submission: Dict[str, Any]) -> bool:
    """A full blind ideal answer was captured (premium uncontaminated SFT). True
    when a packaged ideal_answer record is flagged ``independent`` (the reveal
    commit stamps ``kind='full'``), else falls back to the task's mode."""
    if any(r.get("type") == "ideal_answer" and r.get("independent") for r in (records or [])):
        return True
    ia = (submission.get("payload") or {}).get("independent_answer") or {}
    return (task.get("independent_mode") or ia.get("kind")) == "full"


def _round_money(x: float) -> float:
    return round(float(x), 2)


def estimate_value(
    records: List[Dict[str, Any]],
    task: Dict[str, Any],
    submission: Dict[str, Any],
) -> Dict[str, Any]:
    """Realized + projected dollar value of one judgment, from its packaged
    records and captured attributes.

    ``realized`` is bankable to a single buyer; ``projected`` = realized × reuse
    (non-exclusive + benchmark repackaging — a forecast, reported separately and
    never used to hit the target). Returns a self-describing breakdown so the
    metrics tile and tests can see exactly how a number was built.
    """
    records = records or []
    task = task or {}
    submission = submission or {}

    has_ideal = any(r.get("type") == "ideal_answer" for r in records)
    has_reasoning = any(r.get("type") == "reasoning_trace" for r in records)
    num_pairs = _num_step_pairs(records)

    content = _content_value(
        has_ideal=has_ideal, has_reasoning=has_reasoning, num_step_pairs=num_pairs
    )

    is_grounded = _records_grounded(records, submission)
    is_mode_b = _is_mode_b(task)
    is_full = _independent_full(records, task, submission)
    is_dl_cred = submission.get("agreement_score") is not None and _annotator_credentialed(submission)
    is_mm = _is_multimodal(task, records)
    is_real = _is_real_case(task, records)

    tier = _tier_mult(
        is_grounded=is_grounded,
        difficulty=task.get("difficulty"),
        is_mode_b=is_mode_b,
        is_full_independent=is_full,
        is_double_labeled_credentialed=is_dl_cred,
        is_multimodal=is_mm,
        is_real_case=is_real,
    )

    realized = content * tier
    projected = realized * C.value_reuse_mult()

    return {
        "content_value": _round_money(content),
        "tier_mult": round(tier, 4),
        "realized_value": _round_money(realized),
        "projected_value": _round_money(projected),
        "breakdown": {
            "has_preference": any(r.get("type") == "preference" for r in records),
            "has_ideal_answer": has_ideal,
            "has_reasoning_trace": has_reasoning,
            "num_step_pairs": num_pairs,
            "is_grounded": is_grounded,
            "difficulty": task.get("difficulty"),
            "is_mode_b": is_mode_b,
            "is_full_independent": is_full,
            "is_double_labeled_credentialed": is_dl_cred,
        },
    }


def value_per_minute(realized_or_projected: float, clinician_review_seconds: Optional[float]) -> Optional[float]:
    """dollars ÷ minutes. None when time is missing/zero (undefined ratio — never
    a divide-by-zero or an infinite V/T)."""
    secs = clinician_review_seconds or 0
    if secs <= 0:
        return None
    return round(float(realized_or_projected) / (secs / 60.0), 2)


def expected_value_for_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Forward value estimate from a task's attributes alone, for value-aware
    routing (PRD B3). Assumes a full V2 capture (all formats by default, B1): a
    preference + a refined ideal, a reasoning trace when the task captures
    reasoning, and a small expected number of corrected steps on hard cases.
    Tier factors come straight from the task's admin-set attributes."""
    task = task or {}
    capture_reasoning = bool(task.get("capture_reasoning"))
    difficulty = (task.get("difficulty") or "medium").strip().lower()
    # Hard tasks are likelier to surface a correctable step; keep this modest so
    # routing leans on the tier multiplier, not a speculative pair count.
    expected_pairs = 1 if (capture_reasoning and difficulty == "hard") else 0

    content = _content_value(
        has_ideal=True, has_reasoning=capture_reasoning, num_step_pairs=expected_pairs
    )
    tier = _tier_mult(
        is_grounded=(task.get("grounding_mode") == "required"),
        difficulty=difficulty,
        is_mode_b=_is_mode_b(task),
        is_full_independent=(task.get("independent_mode") == "full"),
        is_double_labeled_credentialed=int(task.get("max_labels") or 1) >= 2,
        is_multimodal=_is_multimodal(task, []),
        is_real_case=_is_real_case(task, []),
    )
    realized = content * tier
    return {
        "content_value": _round_money(content),
        "tier_mult": round(tier, 4),
        "realized_value": _round_money(realized),
        "projected_value": _round_money(realized * C.value_reuse_mult()),
    }


def routing_score(task: Dict[str, Any], contributor_median_seconds: Optional[float]) -> float:
    """Value-aware routing score (PRD B3): expected realized value ÷ the
    contributor's rolling median seconds. A higher score = more sellable dollars
    per minute of THIS clinician's time. Falls back to a neutral 7 min when the
    contributor has no history yet, so a fast clinician isn't starved on day one."""
    expected = expected_value_for_task(task)["realized_value"]
    secs = contributor_median_seconds or 0
    if secs <= 0:
        secs = 7 * 60.0
    return expected / (secs / 60.0)
