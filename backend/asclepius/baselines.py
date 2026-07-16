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

import hashlib
import logging
import random
from typing import Any, Dict, List, Optional

from asclepius.constants import baseline_models, ab_source
from ai.model_config import resolve_provider, UnknownProvider

log = logging.getLogger("asclepius.baselines")


def _provider_of(model: Optional[str]) -> Optional[str]:
    try:
        return resolve_provider(model or "")
    except UnknownProvider:
        return None


def _prompt_hash(system: str, user: str) -> str:
    """One hash over (system + "\n" + user) so a buyer can verify BOTH frontier
    answers were produced from byte-identical input."""
    return hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()


# ── Batch-balanced A/B placement (A3) ────────────────────────────────────────
# The old per-pair random.shuffle is unbiased per pair but drifts on small N. This
# nudges P(OpenAI = slot A) toward 0.5 across a batch/session. State is in-memory per
# process (fine for a batch); the durable QC metric is computed from the stored
# candidates' server-side `provider` field.
_AB_STATE: Dict[str, int] = {"n_pairs": 0, "openai_in_A": 0}


def reset_ab_state() -> None:
    _AB_STATE["n_pairs"] = 0
    _AB_STATE["openai_in_A"] = 0


def openai_as_A_rate() -> Optional[float]:
    n = _AB_STATE["n_pairs"]
    return (_AB_STATE["openai_in_A"] / n) if n else None


def place_AB(openai_ans: Dict[str, Any], anthropic_ans: Dict[str, Any],
             state: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Place the OpenAI and Anthropic answers into slots A/B, nudging P(OpenAI=A)→0.5."""
    st = state if state is not None else _AB_STATE
    target = 0.5
    cur = (st["openai_in_A"] / st["n_pairs"]) if st["n_pairs"] else 0.5
    if abs(cur - target) < 0.02:
        openai_is_A = random.random() < 0.5          # near balance → pure coin
    else:
        openai_is_A = cur < target                   # drifted → nudge back toward 0.5
    st["n_pairs"] += 1
    if openai_is_A:
        st["openai_in_A"] += 1
    a, b = (openai_ans, anthropic_ans) if openai_is_A else (anthropic_ans, openai_ans)
    return [{"id": "A", **a}, {"id": "B", **b}]

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
    # Compute the shared input hash ONCE — every model answers byte-identical input
    # (same system + same rendered case, no hints/archetype/answer key). Both rows
    # carry it so a buyer can prove the pair was answered from the same prompt.
    prompt_hash = _prompt_hash(_BASELINE_SYSTEM, prompt)
    runs: List[Dict[str, Any]] = []
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        for m in models:
            runs.append(store.insert_baseline_run(task_id=task_id, model=m, response_text=None,
                                                  error=f"import:{exc}", provider=_provider_of(m),
                                                  prompt_hash=prompt_hash))
        return runs
    for model in models:
        try:
            resp, rec = await call_llm(
                role="asclepius_baseline",
                system=_BASELINE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                prompt_id="asclepius_baseline",
                purpose="asclepius_baseline_capture",
                model=model,  # per-model override → router picks the provider by id
            )
        except Exception as exc:
            log.info("asclepius baseline %s failed: %s", model, exc)
            runs.append(store.insert_baseline_run(task_id=task_id, model=model,
                                                  response_text=None, error=str(exc),
                                                  provider=_provider_of(model), prompt_hash=prompt_hash))
            continue
        usage = (getattr(resp, "usage", None) or None)
        runs.append(store.insert_baseline_run(
            task_id=task_id, model=model,
            response_text=first_text(resp) or "",
            latency_ms=(rec or {}).get("latency_ms"),
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
            provider=(rec or {}).get("provider") or _provider_of(model),
            prompt_hash=prompt_hash,
        ))
    return runs


def build_baseline_candidates(
    runs: List[Dict[str, Any]], *, gold_text: Optional[str] = None, mode: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Build a blinded A/B pair from stored baseline runs.

    ``two_frontier`` (default for V3/V4): require **exactly one non-empty OpenAI answer
    and one non-empty Anthropic answer**; place them via the batch balancer (A3). If
    either provider is missing/errored, return ``[]`` (caller marks the task
    ``needs_baseline``) — NEVER a silent gold fall-back. ``source='baseline'``,
    ``baseline_model`` and ``provider`` are SERVER-SIDE ONLY (stripped by
    ``_blind_task``'s allowlist).

    ``legacy`` (opt-in only): the historical one-baseline-plus-``gold_text`` (or first
    two) path with a per-pair shuffle."""
    mode = (mode or ab_source() or "two_frontier").strip().lower()

    def _ans(r):
        return {"text": (r.get("response_text") or "").strip(),
                "source": "baseline", "baseline_model": r.get("model"),
                "provider": r.get("provider") or _provider_of(r.get("model"))}

    if mode == "two_frontier":
        oa = next((_ans(r) for r in (runs or [])
                   if (r.get("response_text") or "").strip()
                   and (r.get("provider") or _provider_of(r.get("model"))) == "openai"), None)
        an = next((_ans(r) for r in (runs or [])
                   if (r.get("response_text") or "").strip()
                   and (r.get("provider") or _provider_of(r.get("model"))) == "anthropic"), None)
        if not oa or not an:
            return []  # one provider missing → caller sets needs_baseline (no gold stand-in)
        return place_AB(oa, an)

    # ── legacy mode (explicit opt-in): one-frontier + gold, or first two ──
    answers: List[Dict[str, Any]] = []
    for r in runs or []:
        text = (r.get("response_text") or "").strip()
        if text:
            answers.append({"text": text, "source": "baseline", "baseline_model": r.get("model"),
                            "provider": r.get("provider") or _provider_of(r.get("model"))})
    if gold_text and (gold_text or "").strip():
        answers.append({"text": gold_text.strip(), "source": "gold", "baseline_model": None, "provider": None})
    if len(answers) < 2:
        return []
    answers = answers[:2]
    random.shuffle(answers)
    return [{"id": "A" if i == 0 else "B", **a} for i, a in enumerate(answers)]


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
                provider=_provider_of(model),
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
