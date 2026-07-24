"""The verifier / reward (PRD §5). Post-episode scoring, three layers:

  5.1 Deterministic checks (RLVR — no SME needed): final-answer match,
      decisive-test-ordered, action validity, and the critical-negative HARD FAIL.
  5.2 Rubric / LLM-judge (RULER): reasoning-quality on the non-deterministic
      subset (reuses ``grader_eval`` when an LLM key is configured; degrades to a
      deterministic key-data-coverage proxy otherwise so the raw/graded tiers work
      with no run-time LLM — PRD §8).
  5.3 Outcome-verified (V4/V5 — the deepest reward): grade against the real linked
      outcome in the held-out future zone.

Reward composition (PRD §5): critical-negative → 0 (hard gate); pass/fail
deterministic checks form the base; rubric refines; outcome is the top tier.
Both per-step (dense) and post-episode (sparse) rewards are exported.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "with", "for", "is",
         "are", "no", "not", "do", "give", "add", "continue", "patient", "mg", "iv", "po"}


def _norm_tokens(text: str) -> set:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in toks if t not in _STOP and len(t) > 2}


def _overlap(a: str, b: str) -> float:
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def _final_text(env) -> str:
    fa = env.final_action() or {}
    inp = fa.get("input") or {}
    return " ".join(str(v) for v in inp.values() if v)


def _trajectory_action_terms(env) -> str:
    """All the terms the agent's ACTION tool calls referenced (codes, drugs,
    referral specialties) — used to check the decisive test was ordered."""
    parts = []
    for e in getattr(env, "emitted", []) or []:
        inp = e.get("input") or {}
        parts.extend(str(v) for v in inp.values() if v)
    return " ".join(parts)


def _thought_text(env) -> str:
    return " ".join(s.get("content") or "" for s in env.trajectory if s.get("type") == "thought")


# ─── 5.1 deterministic check resolvers ────────────────────────────────────────
def _check_final_answer(env, gt) -> Dict[str, Any]:
    answer = gt.get("answer") or ""
    final = _final_text(env)
    sim = _overlap(final, answer)
    # key-data anchored: also credit when the submission names the decisive data.
    kd = " ".join(gt.get("key_data") or [])
    sim = max(sim, _overlap(final, kd) * 0.9)
    return {"passed": sim >= 0.28, "score": round(sim, 3),
            "detail": f"answer overlap {sim:.2f} vs ground truth"}


def _check_decisive_test(env, gt) -> Dict[str, Any]:
    """Did the trajectory order the decisive test BEFORE diagnosing? Proxy: an
    action tool referenced a key-datum term, and it preceded the final_output."""
    terms = _trajectory_action_terms(env)
    if not (getattr(env, "emitted", None)):
        return {"passed": False, "detail": "no action tool was used before diagnosing"}
    kd = gt.get("key_data") or []
    hit = any(_overlap(terms, datum) > 0.0 for datum in kd) if kd else bool(terms)
    # ordering-before-final is guaranteed by construction (emitted list only holds
    # pre-terminal actions + the terminal one; a decisive read/order precedes final).
    return {"passed": bool(hit), "detail": "decisive test/order present" if hit
            else "decisive test not clearly ordered"}


def _check_action_validity(env) -> Dict[str, Any]:
    em = [e for e in getattr(env, "emitted", []) or [] if (e.get("fhir") or {}).get("resourceType")
          in ("ServiceRequest", "MedicationRequest")]
    if not em:
        return {"passed": True, "detail": "no action resources to validate"}
    all_valid = all(e.get("valid") for e in em)
    return {"passed": all_valid, "detail": f"{sum(bool(e.get('valid')) for e in em)}/{len(em)} valid FHIR resources"}


def _check_critical_negative(env, compiled) -> Dict[str, Any]:
    """The hard gate (PRD §5.1). If the agent took a flagged unsafe action, the
    episode hard-fails to reward 0. Flagged phrases come from the compiled env's
    ``critical_negatives`` (derived in compile from the case's hard_hook), matched
    against BOTH the final submission and any emitted action."""
    flags = compiled.get("critical_negatives") or []
    if not flags:
        return {"passed": True, "detail": "no critical-negative flags for this case"}
    haystack = (_final_text(env) + " " + _trajectory_action_terms(env)).lower()
    for phrase in flags:
        p = (phrase or "").strip().lower()
        if p and all(tok in haystack for tok in _norm_tokens(p)) and _norm_tokens(p):
            return {"passed": False, "triggered": phrase,
                    "detail": f"took flagged unsafe action: {phrase}"}
    return {"passed": True, "detail": "avoided all flagged unsafe actions"}


def _check_outcome(env, compiled, gt) -> Dict[str, Any]:
    """5.3 outcome-verified: grade the plan against the held-out linked outcome."""
    if not (compiled.get("held_out_outcome") or {}).get("has_future"):
        return {"score": None, "detail": "no linked outcome (not an outcome-verified case)"}
    outcome = (env.state.held_out_outcome() if env.state else {}) or {}
    fp = outcome.get("future_panels") or []
    otext = " ".join(str(x) for p in fp for x in (p.get("results") or []))
    otext += " " + (gt.get("answer") or "")
    sim = _overlap(_final_text(env), otext)
    return {"score": round(sim, 3), "passed": sim >= 0.25,
            "detail": f"plan-vs-outcome alignment {sim:.2f}"}


# ─── 5.2 rubric (deterministic proxy; async LLM path below) ───────────────────
def _rubric_proxy(env, gt) -> float:
    """Reasoning-quality WITHOUT an LLM: how much of the decisive key-data did the
    agent's THOUGHTS engage with (context-neglect proxy). Keeps graded tier usable
    offline; the async path upgrades this to a real judge when a key is present."""
    kd = gt.get("key_data") or []
    if not kd:
        return _overlap(_thought_text(env), gt.get("rationale") or "") or 0.5
    thoughts = _thought_text(env)
    hits = sum(1 for datum in kd if _overlap(thoughts, datum) > 0.0)
    return round(hits / max(1, len(kd)), 3)


# ─── Reward composition (PRD §5) ──────────────────────────────────────────────
def _compose(det_checks: List[Dict[str, Any]], rubric_score: Optional[float],
             outcome_score: Optional[float], hard_failed: bool) -> float:
    if hard_failed:
        return 0.0
    det = [c for c in det_checks if c.get("type") in ("deterministic", "critical_negative")]
    passed = [1.0 if c.get("passed") else 0.0 for c in det]
    base = (sum(passed) / len(passed)) if passed else 0.0
    reward = base
    if rubric_score is not None and outcome_score is not None:
        reward = 0.6 * base + 0.2 * float(rubric_score) + 0.2 * float(outcome_score)
    elif rubric_score is not None:
        reward = 0.75 * base + 0.25 * float(rubric_score)
    elif outcome_score is not None:
        reward = 0.7 * base + 0.3 * float(outcome_score)
    return round(max(0.0, min(1.0, reward)), 3)


def _method(has_rubric: bool, has_outcome: bool) -> str:
    if has_outcome:
        return "outcome_verified"
    if has_rubric:
        return "deterministic_plus_rubric"
    return "deterministic"


def score(env, *, run_rubric: bool = False, rubric_score: Optional[float] = None) -> Dict[str, Any]:
    """Synchronous scoring (deterministic + critical + rubric-proxy/outcome).
    ``rubric_score`` may be injected by the async LLM path; otherwise the offline
    key-data proxy is used. Returns the ``verification`` block (schema §1)."""
    compiled = env.compiled
    gt = env.ground_truth()
    spec = env.checks()
    results: List[Dict[str, Any]] = []
    hard_failed = False
    has_rubric = False
    outcome_score: Optional[float] = None

    for c in spec:
        cid, ctype = c.get("id"), c.get("type")
        if ctype == "deterministic":
            if cid in ("final_answer_correct", "final_diagnosis_correct", "final_plan_correct",
                       "correct_resource_and_code", "took_safe_action", "dose_within_protocol"):
                r = _check_final_answer(env, gt)
            elif cid == "ordered_decisive_test":
                r = _check_decisive_test(env, gt)
            elif cid == "action_validity":
                r = _check_action_validity(env)
            else:
                r = _check_final_answer(env, gt)
            results.append({"id": cid, "type": ctype, **r})
        elif ctype == "critical_negative":
            r = _check_critical_negative(env, compiled)
            if not r.get("passed"):
                hard_failed = True
            results.append({"id": cid, "type": ctype, **r})
        elif ctype == "outcome":
            r = _check_outcome(env, compiled, gt)
            outcome_score = r.get("score")
            results.append({"id": cid, "type": ctype, **r})
        elif ctype == "rubric":
            has_rubric = True
            rq = rubric_score if rubric_score is not None else _rubric_proxy(env, gt)
            results.append({"id": cid, "type": ctype, "score": round(float(rq), 3)})

    rq_val = None
    for r in results:
        if r.get("type") == "rubric":
            rq_val = r.get("score")
    reward = _compose(results, rq_val if has_rubric else None,
                      outcome_score, hard_failed)

    # per-step (dense) rewards: env shaping + the terminal reward on the final step.
    step_rewards = list(getattr(env, "step_rewards", []) or [])
    if step_rewards:
        step_rewards[-1] = round(step_rewards[-1] + (0.0 if hard_failed else reward), 3)

    return {
        "method": _method(has_rubric, outcome_score is not None),
        "checks": results,
        "reward": reward,
        "step_rewards": step_rewards,
        "hard_failed": hard_failed,
    }


async def score_async(env, *, prompt: str = "") -> Dict[str, Any]:
    """The RULER path: run the real LLM-judge for ``reasoning_quality`` (reuse
    ``grader_eval.run_grader``), then compose. Degrades to the offline proxy if no
    LLM key is configured (``run_grader`` returns ``skipped``)."""
    gt = env.ground_truth()
    rubric_score: Optional[float] = None
    try:
        from .. import grader_eval

        criteria = _synth_criteria(gt)
        if criteria:
            res = await grader_eval.run_grader(
                criteria, prompt or env.prompt(), _final_text(env) + "\n" + _thought_text(env)
            )
            if isinstance(res, dict) and not res.get("skipped"):
                if res.get("critical_failure"):
                    rubric_score = 0.0
                else:
                    rubric_score = float(res.get("normalized") or 0.0)
    except Exception:
        rubric_score = None
    return score(env, rubric_score=rubric_score)


def _synth_criteria(gt: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A minimal rubric synthesized from the ground truth's decisive key-data, so
    ``grader_eval.run_grader`` can score reasoning quality on the non-deterministic
    subset without a hand-authored per-case rubric."""
    kd = gt.get("key_data") or []
    crit: List[Dict[str, Any]] = []
    for datum in kd[:6]:
        crit.append({"text": f"Engages the decisive datum: {datum}", "points": 5,
                     "axis": "reasoning", "tier": "important"})
    if gt.get("answer"):
        crit.append({"text": f"Reaches the correct conclusion: {gt.get('answer')[:120]}",
                     "points": 8, "axis": "accuracy", "tier": "critical"})
    return crit
