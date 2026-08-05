"""Empirical-difficulty measurement — the prime-directive gate (PRD §9).

A hard case is only worth building if a frontier model **fundamentally fails** it —
wrong ground truth, OR right answer via broken reasoning. This module measures that
mechanically: it runs a case through the live frontier baseline models, grades each
attempt on BOTH axes against the case's internal answer key, and returns the
``empirical_difficulty`` = fraction of frontier attempts that fail.

Both axes count (PRD §9): because oncology's documented failure is
right-answer-wrong-reason, a model that reaches the correct conclusion via the
shortcut path (never grounding the decisive datum) is scored as a FAILURE.

Degrades gracefully: with no frontier API key (or when the models are unreachable)
it returns ``measured=False`` and the caller keeps the case's DECLARED difficulty
rather than blocking — the serving gate only enforces a MEASURED floor when
``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY`` is on (constants.require_measured_difficulty).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.empirical_difficulty")


def _answer_key_text(case: Dict[str, Any]) -> str:
    gt = (case or {}).get("ground_truth") or {}
    parts = []
    if gt.get("answer"):
        parts.append("GROUND-TRUTH ANSWER: " + str(gt["answer"]))
    if gt.get("rationale"):
        parts.append("RATIONALE: " + str(gt["rationale"]))
    if case.get("hard_hook"):
        parts.append("HARD HOOK (the single deciding datum): " + str(case["hard_hook"]))
    if case.get("reasoning_divergence"):
        parts.append("REASONING DIVERGENCE (sound path vs the seductive shortcut): "
                     + str(case["reasoning_divergence"]))
    return "\n".join(parts)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """String-aware balanced-object extractor (Buyer Response PRD §10 G5): braces
    inside a quoted evidence span must not corrupt depth counting, and prose braces
    before the verdict must not be swallowed by a greedy match."""
    from asclepius.model_sampling import extract_json
    return extract_json(text)


async def _one_frontier_answer(model: str, prompt: str, image_blocks=None) -> Optional[str]:
    """Get a single frontier answer for the case prompt. Returns None if the call
    fails (e.g. no key) so the caller can mark the run unmeasured."""
    try:
        from ai.llm_client import call_llm, first_text
        from asclepius.baselines import _BASELINE_SYSTEM  # neutral 'answer from the data' system
    except Exception as exc:  # pragma: no cover
        log.info("empirical-difficulty: llm client unavailable: %s", exc)
        return None
    content: Any = prompt
    if image_blocks:
        content = [{"type": "text", "text": prompt}, *image_blocks]
    try:
        from asclepius.model_sampling import temperature_for
        resp, _rec = await call_llm(
            role="asclepius_baseline",
            system=_BASELINE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            prompt_id="asclepius_empirical_difficulty_probe",
            purpose="asclepius_empirical_difficulty",
            model=model,
            # Measurement must be reproducible (Buyer Response PRD §10 G1/G2): the
            # difficulty probe runs at temperature 0, not a provider default near 1.0.
            temperature=temperature_for("difficulty_probe"),
        )
        return first_text(resp) or ""
    except Exception as exc:
        log.info("empirical-difficulty: frontier call failed (%s): %s", model, exc)
        return None


async def _judge_failure(case: Dict[str, Any], question: str, model_answer: str) -> Optional[Dict[str, Any]]:
    """Grade a model answer on BOTH axes. Returns {answer_correct, reasoning_sound,
    failed, ...} or None if the judge is unavailable."""
    try:
        from ai.llm_client import call_llm, first_text
        from asclepius.prompts import ASCLEPIUS_EMPIRICAL_DIFFICULTY_JUDGE_SYSTEM
        from asclepius.cases import render_case_prompt
    except Exception:  # pragma: no cover
        return None
    user = (
        "CASE:\n" + render_case_prompt(case, question)
        + "\n\nINTERNAL ANSWER KEY (never shown to the model under test):\n" + _answer_key_text(case)
        + "\n\nMODEL ANSWER TO GRADE:\n" + (model_answer or "")
    )
    try:
        from asclepius.model_sampling import temperature_for, verify_span
        resp, _rec = await call_llm(
            role="asclepius_critic",
            system=ASCLEPIUS_EMPIRICAL_DIFFICULTY_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_empirical_difficulty_judge",
            purpose="asclepius_empirical_difficulty_judge",
            temperature=temperature_for("judge"),  # grading must be reproducible
        )
        parsed = _extract_json(first_text(resp))
        if not isinstance(parsed, dict):
            return None
        # Span validation (Buyer Response PRD §10 G4): if the judge quotes evidence
        # that does not appear in the graded answer, the verdict is unverified and is
        # EXCLUDED, not merely annotated — a fabricated span cannot count as a vote.
        span = parsed.get("evidence_span") or parsed.get("quote")
        if span and not verify_span(str(span), model_answer or ""):
            parsed["span_verified"] = False
            parsed["excluded"] = True
            return parsed
        parsed["span_verified"] = bool(span) or None
        ac = bool(parsed.get("answer_correct"))
        rs = bool(parsed.get("reasoning_sound"))
        parsed["failed"] = (not ac) or (not rs)  # trust our own both-axes rule
        return parsed
    except Exception as exc:
        log.info("empirical-difficulty judge failed: %s", exc)
        return None


async def measure_empirical_difficulty(
    case: Dict[str, Any], question: str, *,
    models: Optional[List[str]] = None, k: Optional[int] = None,
    attempts: Optional[int] = None, image_blocks=None,
) -> Dict[str, Any]:
    """Run ``case`` through the frontier baseline models with **k attempts per model
    at temperature 0** and grade each on BOTH axes (PRD §9; Buyer Response PRD §10
    G2). Returns a block suitable to store under ``generation['empirical_difficulty']``.

    Reports BOTH aggregations, exactly as the eval PRD requires:
      * ``failed_any``  — the model failed in ANY of its k draws (the safety-relevant
        number: a model that gives the unsafe answer 1 in 3 times is unsafe);
      * ``failed_mean`` — the mean failure rate across all draws (expected behaviour);
      * ``consistency`` — fraction of (model) panels whose k draws all agreed.

    A Wilson interval is reported on the pooled per-attempt rate — with 2-4 models the
    interval is wide, and it must be SHOWN rather than hidden behind a point estimate
    that gets used for pricing (``value_lower`` is the number pricing must use, §10 G2).

    When no frontier answer can be obtained (no key / unreachable), returns
    ``measured=False`` with ``value=None`` so the caller keeps the declared value."""
    from asclepius.constants import (
        baseline_models, empirical_difficulty_attempts, min_empirical_difficulty,
    )
    from asclepius.cases import render_case_prompt, public_case
    from asclepius.model_sampling import provider_family, wilson_interval

    models = models or baseline_models()
    # ``k`` (per-model draws) supersedes the legacy ``attempts`` arg name.
    per_model = k or attempts or empirical_difficulty_attempts()
    prompt = render_case_prompt(public_case(case) or case, question)

    per_provider: Dict[str, Dict[str, Any]] = {}
    n_attempts = 0
    n_failures = 0
    any_answer = False
    n_failed_any = 0          # models that failed at least one draw
    models_with_draws = 0
    consistent_models = 0     # models whose k draws all agreed (all fail or all pass)
    mean_rates: List[float] = []
    for model in models:
        pm = per_provider.setdefault(model, {
            "n": 0, "failures": 0, "rate": None, "failed_any": False,
            "provider_family": provider_family(model),
        })
        draws: List[bool] = []
        for _ in range(per_model):
            answer = await _one_frontier_answer(model, prompt, image_blocks=image_blocks)
            if answer is None:
                continue  # call failed — do not count as pass or fail
            any_answer = True
            verdict = await _judge_failure(case, question, answer)
            if verdict is None:
                continue
            if verdict.get("excluded"):
                # Span-unverified verdict (G4): excluded from the vote entirely.
                continue
            n_attempts += 1
            pm["n"] += 1
            failed = bool(verdict.get("failed"))
            draws.append(failed)
            if failed:
                n_failures += 1
                pm["failures"] += 1
        if pm["n"]:
            pm["rate"] = round(pm["failures"] / pm["n"], 3)
            pm["failed_any"] = any(draws)
            mean_rates.append(pm["failures"] / pm["n"])
            models_with_draws += 1
            if any(draws):
                n_failed_any += 1
            if len(draws) >= 1 and (all(draws) or not any(draws)):
                consistent_models += 1

    floor = min_empirical_difficulty()
    if not any_answer or n_attempts == 0:
        return {
            "value": None, "measured": False, "both_axes": True,
            "n_attempts": 0, "n_failures": 0, "per_provider": per_provider,
            "floor": floor, "k": per_model,
            "note": "no live frontier measurement available (no key / unreachable); "
                    "kept declared difficulty (PRD §9)",
        }
    value = round(n_failures / n_attempts, 3)
    lo, hi = wilson_interval(n_failures, n_attempts)
    failed_any = round(n_failed_any / models_with_draws, 3) if models_with_draws else None
    failed_mean = round(sum(mean_rates) / len(mean_rates), 3) if mean_rates else None
    consistency = round(consistent_models / models_with_draws, 3) if models_with_draws else None
    return {
        "value": value,               # pooled per-attempt failure rate (point estimate)
        "value_lower": round(lo, 3),  # Wilson lower bound — PRICING must use THIS (§10 G2)
        "value_upper": round(hi, 3),
        "failed_any": failed_any,     # fraction of models that failed ≥1 of k draws
        "failed_mean": failed_mean,   # mean per-model failure rate
        "consistency": consistency,   # fraction of models whose k draws agreed
        "measured": True, "both_axes": True, "k": per_model,
        "n_attempts": n_attempts, "n_failures": n_failures,
        "n_models": models_with_draws,
        "per_provider": per_provider, "floor": floor,
        # The gate uses the LOWER bound, not the point estimate: pricing a case as
        # frontier-hard off a point estimate whose interval runs low is a claim we
        # cannot support in diligence (Buyer Response PRD §10 G2).
        "passes_gate": round(lo, 3) >= floor,
    }
