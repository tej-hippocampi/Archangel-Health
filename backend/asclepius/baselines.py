"""Frontier-model failure capture (FEAT-1).

Today both candidate answers are engineered by our own generator (one deliberately
flawed) — synthetic. We CLAIM our cases are hard; we cannot PROVE it. This module
turns "trust us" into "here is your model failing, with the expert's correction",
and makes the data ON-POLICY (the highest-value category):

  * ``run_baselines(store, task, models)`` — send the RENDERED CASE PROMPT COLD (no
    hints, no archetype leakage) to each configured frontier model and store the
    VERBATIM response in ``baseline_runs``.
  * ``build_baseline_candidates(...)`` — "Grade the real models" mode: turn two
    stored baseline answers into a blinded A/B pair (``source='baseline'`` +
    ``baseline_model``, both SERVER-SIDE ONLY), with the same 50/50 slot
    randomization as the generated pairs.
  * ``record_model_failure(store, task_id, submission_id)`` — after the specialist
    grades, persist the per-model failure record (which model was rejected, which
    error tags applied, which steps were corrected, + the expert correction).

Everything routes through ``ai.llm_client`` so calls are audit-logged and BAA-
covered. Degrades gracefully: with no LLM key each model records an errored run,
never a crash.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional

from asclepius.constants import baseline_models

log = logging.getLogger("asclepius.baselines")

# A neutral clinical-answer system prompt — the model answers the case as a
# confident clinician WITH NO HINTS (no archetype, no failure mode, no answer key).
_BASELINE_SYSTEM = (
    "You are an expert physician answering a clinical question. Read the case (labs, "
    "notes, medications, problem list) and give your best, concise clinical answer and "
    "plan. Answer directly and confidently as a specialist would; do not hedge with "
    "disclaimers. Base your answer only on the information provided."
)


async def run_baselines(
    store: Any, task: Dict[str, Any], models: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Answer the task's rendered prompt COLD with each configured frontier model
    and store the verbatim response. Returns the stored run rows. Never raises."""
    models = models or baseline_models()
    prompt = (task or {}).get("prompt") or ""
    task_id = (task or {}).get("task_id")
    runs: List[Dict[str, Any]] = []
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        for m in models:
            runs.append(store.insert_baseline_run(task_id=task_id, model=m, response_text=None,
                                                  error=f"import:{exc}"))
        return runs
    for model in models:
        try:
            resp, rec = await call_llm(
                role="asclepius_baseline",
                system=_BASELINE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                prompt_id="asclepius_baseline",
                purpose="asclepius_baseline_capture",
                model=model,  # per-model override (see llm_client._build_kwargs)
            )
        except Exception as exc:
            log.info("asclepius baseline %s failed: %s", model, exc)
            runs.append(store.insert_baseline_run(task_id=task_id, model=model,
                                                  response_text=None, error=str(exc)))
            continue
        usage = (getattr(resp, "usage", None) or None)
        runs.append(store.insert_baseline_run(
            task_id=task_id, model=model,
            response_text=first_text(resp) or "",
            latency_ms=(rec or {}).get("latency_ms"),
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
        ))
    return runs


def build_baseline_candidates(
    runs: List[Dict[str, Any]], *, gold_text: Optional[str] = None
) -> List[Dict[str, str]]:
    """"Grade the real models" mode: build a blinded A/B pair from stored baseline
    runs (or one baseline + one gold). ``source='baseline'`` + ``baseline_model``
    are SERVER-SIDE ONLY. 50/50 slot randomization, same as the generated pairs.
    Returns [] when there isn't enough material (fewer than two answers)."""
    answers: List[Dict[str, Any]] = []
    for r in runs or []:
        text = (r.get("response_text") or "").strip()
        if text:
            answers.append({"text": text, "source": "baseline", "baseline_model": r.get("model")})
    if gold_text and (gold_text or "").strip():
        answers.append({"text": gold_text.strip(), "source": "gold", "baseline_model": None})
    if len(answers) < 2:
        return []
    answers = answers[:2]
    random.shuffle(answers)  # enforce 50/50 slot placement server-side
    out: List[Dict[str, str]] = []
    for i, a in enumerate(answers):
        out.append({
            "id": "A" if i == 0 else "B",
            "text": a["text"],
            "source": a["source"],
            "baseline_model": a.get("baseline_model"),
        })
    return out


def record_model_failure(store: Any, task_id: str, submission_id: str) -> Optional[str]:
    """After a specialist grades a real-model A/B pair, persist the per-model
    failure record: which baseline model was rejected (or both, on
    ``both_inadequate``), which error tags applied, which steps were corrected,
    plus the expert's correction. No-op (returns None) when the task carries no
    baseline candidate. Never raises."""
    try:
        task = store.get_task(task_id)
        submission = store.get_submission(submission_id)
        if not task or not submission:
            return None
        cands = {str(c.get("id")): c for c in (task.get("candidate_answers") or [])}
        if not any((c.get("source") == "baseline") for c in cands.values()):
            return None
        payload = submission.get("payload") or {}
        verdict = submission.get("verdict") or payload.get("verdict")
        prompt = task.get("prompt")

        def _correction() -> str:
            rev = (payload.get("chosen_revision") or {})
            if (rev.get("revised_text") or "").strip():
                return rev["revised_text"].strip()
            fs = (payload.get("from_scratch") or {})
            if (fs.get("ideal_answer") or "").strip():
                return fs["ideal_answer"].strip()
            ia = (payload.get("independent_answer") or {})
            return (ia.get("text") or "").strip()

        def _corrected_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return [
                {"original": s.get("original_text"), "corrected": s.get("text"),
                 "reason": s.get("correction_reason")}
                for s in (steps or []) if s.get("corrected")
            ]

        error_tags = list((payload.get("rejected_critique") or {}).get("error_tags") or [])
        correction = _correction()
        fids: List[str] = []

        def _persist(model: Optional[str], tags: List[str], steps: List[Dict[str, Any]]):
            if not model:
                return
            fids.append(store.insert_model_failure(
                task_id=task_id, submission_id=submission_id, model=model, verdict=verdict,
                error_tags=tags, corrected_steps=steps, expert_correction=correction, prompt=prompt,
            ))

        if verdict in ("A_better", "B_better"):
            rejected = cands.get(str(submission.get("rejected_id") or payload.get("rejected_id")))
            if rejected and rejected.get("source") == "baseline":
                _persist(rejected.get("baseline_model"), error_tags,
                         _corrected_steps(payload.get("reasoning_steps")))
        elif verdict == "both_inadequate":
            steps = _corrected_steps((payload.get("from_scratch") or {}).get("reasoning_steps"))
            for c in cands.values():
                if c.get("source") == "baseline":
                    _persist(c.get("baseline_model"), error_tags, steps)
        return fids[0] if fids else None
    except Exception:  # pragma: no cover - failure capture must never break submit
        log.exception("asclepius: record_model_failure failed for %s", submission_id)
        return None
