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

import asyncio
import hashlib
import logging
import random
from typing import Any, Dict, List, Optional

from asclepius.constants import (
    baseline_models,
    ab_source,
    fallback_window,
    legacy_baseline_models,
    max_fallback_rate,
    two_frontier_v4_enabled,
)
from ai.model_config import resolve_provider, UnknownProvider

log = logging.getLogger("asclepius.baselines")


def _provider_of(model: Optional[str]) -> Optional[str]:
    try:
        return resolve_provider(model or "")
    except UnknownProvider:
        return None


def _prompt_hash(system: str, user: str, image_sha256: Optional[str] = None) -> str:
    """One hash over (system + "\n" + user [+ "\n" + image_sha256]) so a buyer can
    verify BOTH frontier answers were produced from byte-identical input — INCLUDING
    the image (V4 Image Embedding PRD §5.3). The pair-divergence guard then also
    catches a case where the two models somehow received different images."""
    base = system + "\n" + user
    if image_sha256:
        base += "\n" + image_sha256
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _case_image_for_baseline(task: Dict[str, Any]):
    """Return (image_block, sha256, mime) for the case's PRIMARY image-bearing study
    (V4 Image PRD §5.2), or (None, None, None) for a text-only case. Loads the cleaned
    asset bytes ONCE so the SAME bytes reach both providers. V4-only by construction —
    images never live on a non-real case."""
    case = (task or {}).get("case") or {}
    if case.get("case_source") != "real_deid":
        return (None, None, None)
    for s in case.get("studies") or []:
        a = (s or {}).get("asset") if isinstance(s, dict) else None
        if not (isinstance(a, dict) and a.get("sha256")):
            continue
        try:
            import base64
            from asclepius.assets import load_asset
            from ai.llm_client import image_block
            data, mime = load_asset(a)
            b64 = base64.b64encode(data).decode("ascii")
            return (image_block(mime, b64), a.get("sha256"), mime)
        except Exception as exc:  # a missing/corrupt blob → treat as text-only, logged
            log.warning("baseline image load failed for %s: %s", a.get("asset_id"), exc)
            return (None, None, None)
    return (None, None, None)


# Image-case baseline system (V4 Image PRD §5.4): tell the model the IMAGE is the
# primary data and to ground the answer in what it SEES — this is what surfaces the
# pixel-grounding failure the data is meant to capture.
_BASELINE_SYSTEM_IMAGE = (
    "You are an expert physician answering a clinical question. The PRIMARY data is the "
    "ATTACHED IMAGE (an ECG strip, an echo/CT/PET still, or a pathology image) — read it "
    "directly and ground your answer in WHAT YOU SEE in the image, not only the text "
    "findings. Then integrate the labs, notes, medications, and problem list. Give your "
    "best, concise clinical answer and plan, confidently as a specialist would; do not "
    "hedge with disclaimers. Base your answer only on the information provided."
)


# ── Batch-balanced A/B placement (PRD §A2) ───────────────────────────────────
# Slot assignment must be TRULY RANDOM — a doctor must never be able to learn "A is
# always OpenAI." Every pair draws from a CSPRNG (`SystemRandom`); we only *softly*
# bias the probability to correct batch drift toward 50/50, never a deterministic
# A,B,A,B alternation (which a doctor could learn). State is in-memory per process
# (fine for a batch); the durable QC metric is recomputed from the stored candidates'
# server-side `provider` field (`store.ab_slot_balance`).
_AB_STATE: Dict[str, int] = {"n_pairs": 0, "openai_in_A": 0}
_SYSRAND = random.SystemRandom()

# Never let the drift-correction push the probability past these bounds — keeps every
# single assignment substantially random (defeats a runs-test for alternation) while
# still pulling a drifted batch back toward balance.
_AB_P_MIN, _AB_P_MAX = 0.15, 0.85


def reset_ab_state() -> None:
    _AB_STATE["n_pairs"] = 0
    _AB_STATE["openai_in_A"] = 0


def openai_as_A_rate() -> Optional[float]:
    n = _AB_STATE["n_pairs"]
    return (_AB_STATE["openai_in_A"] / n) if n else None


def place_AB(openai_ans: Dict[str, Any], anthropic_ans: Dict[str, Any],
             state: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Place the OpenAI and Anthropic answers into slots A/B with a CSPRNG-random
    orientation, softly nudged toward P(OpenAI=A)=0.5 across the batch (PRD §A2).

    Both providers can land in A or in B on any pair; the assignment is never a fixed
    alternation. The nudge only shifts the *probability* (clamped to [0.15, 0.85])
    based on the running slot rate, so a drifted batch self-corrects without becoming
    predictable."""
    st = state if state is not None else _AB_STATE
    n = st["n_pairs"]
    cur = (st["openai_in_A"] / n) if n else 0.5
    # cur high (OpenAI has been landing in A too often) → lower p(OpenAI=A), and vice
    # versa. Gentle gain (0.5) so it corrects over a few pairs, never in one deterministic flip.
    p = min(_AB_P_MAX, max(_AB_P_MIN, 0.5 + 0.5 * (0.5 - cur)))
    openai_is_A = _SYSRAND.random() < p
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
    # V4 vision A/B (V4 Image PRD §5): load the case's primary image ONCE and send the
    # SAME bytes to both providers via the vision path; the image sha256 folds into the
    # prompt_hash so "same prompt, same image" is enforceable, not assumed.
    image_block, image_sha, _image_mime = _case_image_for_baseline(task)
    system = _BASELINE_SYSTEM_IMAGE if image_block else _BASELINE_SYSTEM
    # Compute the shared input hash ONCE — every model answers byte-identical input
    # (same system + same rendered case + same image, no hints/archetype/answer key).
    prompt_hash = _prompt_hash(system, prompt, image_sha)
    runs: List[Dict[str, Any]] = []
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        for m in models:
            runs.append(store.insert_baseline_run(task_id=task_id, model=m, response_text=None,
                                                  error=f"import:{exc}", provider=_provider_of(m),
                                                  prompt_hash=prompt_hash))
        return runs

    # Vision preflight (V4 Image PRD §5.1): for an image case BOTH models must be
    # vision-capable. A misconfigured non-vision model records an errored run with an
    # actionable message → the pair degrades to needs_baseline rather than silently
    # grading the image case text-only.
    if image_block:
        from ai.model_config import is_vision_capable
        incapable = [m for m in models if not is_vision_capable(m)]
        if incapable:
            for m in models:
                runs.append(store.insert_baseline_run(
                    task_id=task_id, model=m, response_text=None,
                    error=(f"vision_incapable: model {incapable} cannot accept an image; an image "
                           f"case needs two vision-capable baseline models (set "
                           f"ASCLEPIUS_BASELINE_MODELS to vision-capable ids)"),
                    provider=_provider_of(m), prompt_hash=prompt_hash))
            return runs

    def _content(_p):
        # Vision path: a text block + the identical image block; else plain text.
        return ([{"type": "text", "text": _p}, image_block] if image_block else _p)
    # Call the frontier models CONCURRENTLY (they are independent; sequential
    # in-request calls stacked their latencies and risked a gateway timeout on the
    # admin's grade-real-models request). Each _one() degrades to an error dict — the
    # gather never raises. DB writes happen AFTER the gather, in model order, so the
    # synchronous SQLite store is never written from two coroutines at once.
    async def _one(model: str) -> Dict[str, Any]:
        try:
            resp, rec = await call_llm(
                role="asclepius_baseline",
                system=system,
                messages=[{"role": "user", "content": _content(prompt)}],
                prompt_id="asclepius_baseline",
                purpose="asclepius_baseline_capture",
                model=model,  # per-model override → router picks the provider by id
            )
            return {"model": model, "resp": resp, "rec": rec, "error": None}
        except Exception as exc:
            log.info("asclepius baseline %s failed: %s", model, exc)
            return {"model": model, "resp": None, "rec": None, "error": str(exc)}

    results = await asyncio.gather(*[_one(m) for m in models])
    for r in results:
        model = r["model"]
        if r["error"] is not None or r["resp"] is None:
            runs.append(store.insert_baseline_run(task_id=task_id, model=model, response_text=None,
                                                  error=r["error"] or "no response",
                                                  provider=_provider_of(model), prompt_hash=prompt_hash))
            continue
        resp, rec = r["resp"], r["rec"]
        text = (first_text(resp) or "").strip()
        if not text:
            # Empty/incomplete answer (e.g. a reasoning model consumed its whole
            # output budget on hidden reasoning → status="incomplete", output_text="").
            # Record it as an ERRORED run with an actionable message rather than a
            # blank "successful" run — so the admin sees WHY and build_baseline_candidates
            # correctly treats this provider as missing.
            runs.append(store.insert_baseline_run(
                task_id=task_id, model=model, response_text=None,
                error="empty/incomplete response (a reasoning model may have exhausted its "
                      "output budget on reasoning — raise LLM_OPENAI_REASONING_RESERVE or "
                      "asclepius_baseline max_tokens)",
                provider=(rec or {}).get("provider") or _provider_of(model), prompt_hash=prompt_hash))
            continue
        usage = (getattr(resp, "usage", None) or None)
        runs.append(store.insert_baseline_run(
            task_id=task_id, model=model,
            response_text=text,
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
        def _pick(provider: str) -> Optional[Dict[str, Any]]:
            return next((r for r in (runs or [])
                         if (r.get("response_text") or "").strip()
                         and (r.get("provider") or _provider_of(r.get("model"))) == provider), None)
        oa_run, an_run = _pick("openai"), _pick("anthropic")
        if not oa_run or not an_run:
            return []  # one provider missing → caller runs the fallback ladder (§A3)
        # PRD §A1 — both answers MUST come from byte-identical input. ``prompt_hash`` is
        # sha(system + rendered case), computed once and stamped on every run; a mismatch
        # means the pair is comparing PROMPTS, not MODELS — the data is corrupt, so
        # DISCARD the pair (never let a divergent pair reach a doctor).
        h_oa, h_an = oa_run.get("prompt_hash"), an_run.get("prompt_hash")
        if h_oa and h_an and h_oa != h_an:
            log.error("asclepius: pair_prompt_divergence — openai=%s anthropic=%s; pair discarded",
                      str(h_oa)[:12], str(h_an)[:12])
            return []
        return place_AB(_ans(oa_run), _ans(an_run))

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


# ── The A/B assembly ladder (PRD §A3 + §A7) ──────────────────────────────────
def _classify_error(err: Optional[str]) -> str:
    """Coarse-classify a baseline run error string into a stable reason token."""
    e = (err or "").lower()
    if not e:
        return "empty"
    if "timed out" in e or "timeout" in e:
        return "timeout"
    if "empty/incomplete" in e:
        return "empty"
    if "429" in e or "rate limit" in e or "ratelimit" in e:
        return "rate_limit"
    if any(s in e for s in ("401", "403", "404", "authentication", "invalid x-api-key",
                            "permission", "not found", "no access", "does not exist")):
        return "4xx"
    return "error"


def _fallback_reason(runs: List[Dict[str, Any]]) -> str:
    """Why did two-frontier fail to form a pair? A stable, provider-tagged token like
    ``openai_timeout`` / ``openai_4xx`` / ``anthropic_empty`` (PRD §A3 Rung 2)."""
    parts: List[str] = []
    for r in runs or []:
        if (r.get("response_text") or "").strip():
            continue
        prov = r.get("provider") or _provider_of(r.get("model")) or "unknown"
        parts.append(f"{prov}_{_classify_error(r.get('error'))}")
    return ",".join(parts) if parts else "shortfall"


def _is_anthropic_ok(r: Dict[str, Any]) -> bool:
    return bool((r.get("response_text") or "").strip()) and \
        (r.get("provider") or _provider_of(r.get("model"))) == "anthropic"


async def _anthropic_only_pair(
    store: Any, task: Dict[str, Any], *, existing_runs: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    """Assemble a legacy same-provider A/B pair from **two DISTINCT Anthropic** answers
    (all BAA-covered). Used both for the OLD-method fallback (§A3 Rung 2) and as the
    intended V4 path when two-frontier is disabled for V4 (§A7).

    Reuses any surviving Anthropic run from ``existing_runs`` (never re-calls a model
    that already answered) and tops up with additional Anthropic models until it has two
    distinct-model answers. Returns ``(candidates, reason|None)``. Never raises."""
    have = [r for r in (existing_runs or []) if _is_anthropic_ok(r)]
    # Distinct Anthropic models we haven't already used, in configured order.
    used = {r.get("model") for r in have}
    topup = [m for m in legacy_baseline_models() if _provider_of(m) == "anthropic" and m not in used]
    i = 0
    while len({r.get("model") for r in have}) < 2 and i < len(topup):
        new = await run_baselines(store, task, models=[topup[i]])
        have += [r for r in new if _is_anthropic_ok(r)]
        i += 1
    # Keep two runs from DISTINCT models (two answers from one model would be a
    # degenerate "pair"); order-preserving dedup.
    seen, distinct = set(), []
    for r in have:
        m = r.get("model")
        if m in seen:
            continue
        seen.add(m)
        distinct.append(r)
        if len(distinct) == 2:
            break
    if len(distinct) < 2:
        return [], "anthropic_unavailable"
    return build_baseline_candidates(distinct, mode="legacy"), None


async def assemble_ab_pair(store: Any, task: Dict[str, Any]) -> tuple:
    """The full A/B assembly ladder (PRD §A3 + §A7). Returns ``(candidates, meta)``.
    Never raises. ``meta = {ab_source, fallback_reason, alert, fallback_rate}``.

    Ladder:
      * **Rung 0 — V4 BAA gate (§A7):** a V4 real case with two-frontier disabled uses
        the Anthropic-only path by design (``anthropic_only_v4``) — not a fallback incident.
      * **Rung 1 — two-frontier, hard (§A3):** one OpenAI + one Anthropic, concurrent,
        each already retried once inside ``call_llm`` on a transient error. The ~always path.
      * **Rung 3 — guard (§A3):** if the rolling fallback rate already exceeds the ceiling,
        SUPPRESS Rung 2 and raise the alert (``needs_baseline``) so an operator fixes the
        provider — Rung 1 is still attempted every request, so recovery is automatic.
      * **Rung 2 — OLD-method fallback (§A3):** two distinct Anthropic answers, tagged
        ``legacy_fallback`` + ``fallback_reason``. Reuses the surviving Anthropic answer.
      * **Shortfall:** ``[]`` → caller marks ``needs_baseline``. NEVER a gold stand-in."""
    is_v4 = (task or {}).get("case_source") == "real_deid"
    has_image = bool(_case_image_for_baseline(task)[0])

    # Rung 0 — V4 BAA gate (§A7). For an IMAGE case the vision A/B is the whole point
    # (V4 Image PRD §5.7): two-frontier SHOULD be on so OpenAI + Anthropic each ground
    # the SAME pixels. Log the explicit config decision when it is off — an
    # Anthropic-only image pair is lower value (carried on ``ab_source``).
    if is_v4 and not two_frontier_v4_enabled():
        if has_image:
            log.warning(
                "V4 IMAGE case %s ran WITHOUT two-frontier: the vision A/B needs "
                "ASCLEPIUS_TWO_FRONTIER_V4=1 so OpenAI + Anthropic both read the same image "
                "(logged config decision, PRD §5.7). Using the Anthropic-only vision pair.",
                (task or {}).get("task_id"))
        pair, why = await _anthropic_only_pair(store, task)
        return pair, {"ab_source": "anthropic_only_v4" if pair else None,
                      "fallback_reason": None if pair else (why or "anthropic_unavailable"),
                      "alert": False, "fallback_rate": None, "image_case": has_image}

    # Rung 1 — two-frontier.
    runs = await run_baselines(store, task, models=baseline_models())
    pair = build_baseline_candidates(runs, mode="two_frontier")
    if pair:
        return pair, {"ab_source": "two_frontier", "fallback_reason": None,
                      "alert": False, "fallback_rate": None}

    reason = _fallback_reason(runs)

    # Rung 3 — fallback-rate guard (suppress Rung 2 when fallback is already an incident).
    rate = None
    try:
        rate = store.ab_fallback_rate(window=fallback_window())
    except Exception:  # pragma: no cover - a metric read must never break assembly
        rate = None
    if rate is not None and rate > max_fallback_rate():
        return [], {"ab_source": None, "fallback_reason": "fallback_rate_exceeded",
                    "alert": True, "fallback_rate": rate}

    # Rung 2 — OLD-method Anthropic-only fallback (reuse the surviving Anthropic answer).
    pair, why = await _anthropic_only_pair(store, task, existing_runs=runs)
    if pair:
        return pair, {"ab_source": "legacy_fallback", "fallback_reason": reason,
                      "alert": False, "fallback_rate": rate}

    # Rung 2 failed too (Anthropic itself unavailable) → needs_baseline.
    return [], {"ab_source": None, "fallback_reason": reason or why or "both_failed",
                "alert": False, "fallback_rate": rate}


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
