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
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


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
        resp, _rec = await call_llm(
            role="asclepius_baseline",
            system=_BASELINE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            prompt_id="asclepius_empirical_difficulty_probe",
            purpose="asclepius_empirical_difficulty",
            model=model,
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
        resp, _rec = await call_llm(
            role="asclepius_critic",
            system=ASCLEPIUS_EMPIRICAL_DIFFICULTY_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_empirical_difficulty_judge",
            purpose="asclepius_empirical_difficulty_judge",
        )
        parsed = _extract_json(first_text(resp))
        if not isinstance(parsed, dict):
            return None
        ac = bool(parsed.get("answer_correct"))
        rs = bool(parsed.get("reasoning_sound"))
        parsed["failed"] = (not ac) or (not rs)  # trust our own both-axes rule
        return parsed
    except Exception as exc:
        log.info("empirical-difficulty judge failed: %s", exc)
        return None


async def measure_empirical_difficulty(
    case: Dict[str, Any], question: str, *,
    models: Optional[List[str]] = None, attempts: Optional[int] = None,
    image_blocks=None,
) -> Dict[str, Any]:
    """Run ``case`` through the frontier baseline models and grade each attempt on
    BOTH axes (PRD §9). Returns a block suitable to store under
    ``generation['empirical_difficulty']``::

        {value, measured, both_axes: True, n_attempts, n_failures,
         per_provider: {model: {n, failures, rate}}, judge_model, floor}

    ``value`` = n_failures / n_attempts (wrong answer OR wrong reasoning). When no
    frontier answer can be obtained (no key / unreachable), returns
    ``measured=False`` with ``value=None`` so the caller keeps the declared value."""
    from asclepius.constants import (
        baseline_models, empirical_difficulty_attempts, min_empirical_difficulty,
    )
    from asclepius.cases import render_case_prompt, public_case

    models = models or baseline_models()
    per_attempt = attempts or empirical_difficulty_attempts()
    prompt = render_case_prompt(public_case(case) or case, question)

    per_provider: Dict[str, Dict[str, Any]] = {}
    n_attempts = 0
    n_failures = 0
    any_answer = False
    for model in models:
        pm = per_provider.setdefault(model, {"n": 0, "failures": 0, "rate": None})
        for _ in range(per_attempt):
            answer = await _one_frontier_answer(model, prompt, image_blocks=image_blocks)
            if answer is None:
                continue  # call failed — do not count as pass or fail
            any_answer = True
            verdict = await _judge_failure(case, question, answer)
            if verdict is None:
                continue
            n_attempts += 1
            pm["n"] += 1
            if verdict.get("failed"):
                n_failures += 1
                pm["failures"] += 1
        if pm["n"]:
            pm["rate"] = round(pm["failures"] / pm["n"], 3)

    floor = min_empirical_difficulty()
    if not any_answer or n_attempts == 0:
        return {
            "value": None, "measured": False, "both_axes": True,
            "n_attempts": 0, "n_failures": 0, "per_provider": per_provider,
            "floor": floor,
            "note": "no live frontier measurement available (no key / unreachable); "
                    "kept declared difficulty (PRD §9)",
        }
    value = round(n_failures / n_attempts, 3)
    return {
        "value": value, "measured": True, "both_axes": True,
        "n_attempts": n_attempts, "n_failures": n_failures,
        "per_provider": per_provider, "floor": floor,
        "passes_gate": value >= floor,
    }
