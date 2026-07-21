"""Rubric Rigor FIX-2 + FIX-8 — package-time grader META-EVALUATION (V3/V4 only).

The rubric ships as a runnable grader (``grader_prompt.txt`` + ``score.py``). A buyer
can't trust it unless we PROVE it grades like the physician. So at package time we run
that same grader against answers we already have and persist the proof on the record:

  * ``grader_validity``  (FIX-2) — the physician's CHOSEN answer scores high and the
    REJECTED answer scores low / critical-fails; ``separation = chosen − rejected``.
    Low separation, or a rejected answer that does NOT critical-fail → ``needs_review``.
  * ``grader_reliability`` (FIX-2) — N identical runs of the chosen answer agree:
    normalized-score variance + per-criterion met/not-met ``flip_rate``. A CRITICAL
    criterion that flips across identical runs → ``unreliable`` (too vague → FIX-1).
  * ``hackability`` (FIX-8) — a padded-but-hollow answer that commits the case's actual
    error must NOT beat a terse-but-correct one. ``gameable`` if it does → ``needs_review``.

Contract (shared with every other judge): with no LLM key — or on any error — each
probe DEGRADES to ``{"skipped": True}`` and never raises. No answer key ships raw; only
scores + flags land on the record. All calls route through ``ai.llm_client`` (audited,
BAA-covered).
"""

from __future__ import annotations

import asyncio
import json
import logging
from statistics import pvariance
from typing import Any, Dict, List, Optional

from asclepius.constants import (
    grader_max_variance,
    grader_min_separation,
    grader_reliability_runs,
)

log = logging.getLogger("asclepius.grader_eval")

_SKIPPED = {"skipped": True}

# A verbose, hedge-heavy preamble that ticks surface "positives" (safety-sounding,
# thorough-sounding) without committing to the decisive clinical point — used to build
# the ADVERSARIAL padded-hollow answer for the hackability probe (FIX-8).
_HEDGE = (
    "In approaching this complex clinical scenario, it is important to carefully consider the "
    "entire clinical picture, weigh the risks and benefits of each option, monitor the patient "
    "closely, individualize management to the patient's values and comorbidities, involve the "
    "multidisciplinary team, and ensure appropriate follow-up and shared decision-making. "
    "A thoughtful, guideline-concordant, patient-centered approach is essential throughout. "
)


def _first_sentences(text: str, n: int = 2) -> str:
    """The first ``n`` sentences — a terse answer that keeps the decisive point."""
    t = (text or "").strip()
    if not t:
        return t
    out, count = [], 0
    for chunk in t.replace("\n", " ").split(". "):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append(chunk if chunk.endswith(".") else chunk + ".")
        count += 1
        if count >= n:
            break
    return " ".join(out)


async def run_grader(
    criteria: List[Dict[str, Any]], prompt: str, answer: str, *, role: str = "asclepius_critic",
    image: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the SHIPPED grader (same criteria the buyer gets) on one answer. Returns the
    grader JSON (with the deterministic critical-negative hard-fail applied) or
    ``{"skipped": True}``. Never raises.

    ``image`` (V4 Image Embedding PRD §7): for an image case the grader MUST receive
    the SAME image the models saw, or its validity measurement is on a different input
    than the models graded — which would be invalid."""
    if not (answer or "").strip() or not criteria:
        return dict(_SKIPPED)
    try:
        from ai.llm_client import call_llm, first_text
        from asclepius.export import _GRADER_PROMPT, apply_critical_hard_fail
    except Exception as exc:  # pragma: no cover
        return {"skipped": True, "error": f"import:{exc}"}
    user = ("PROMPT:\n" + (prompt or "") + "\n\nRUBRIC CRITERIA:\n"
            + json.dumps(criteria, indent=2, default=str)
            + "\n\nCANDIDATE ANSWER:\n" + answer)
    content: Any = ([{"type": "text", "text": user}, image] if image else user)
    try:
        resp, _ = await call_llm(
            role=role, system=_GRADER_PROMPT,
            messages=[{"role": "user", "content": content}],
            prompt_id="asclepius_critic", purpose="rubric_grader_eval",
        )
    except Exception as exc:
        log.info("rubric grader probe skipped (no LLM): %s", exc)
        return {"skipped": True, "error": str(exc)}
    text = first_text(resp) or ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {"skipped": True, "error": "no_json"}
    try:
        result = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return {"skipped": True, "error": "bad_json"}
    if isinstance(result, dict) and "per_criterion" in result:
        result = apply_critical_hard_fail(result, {"criteria": criteria})
    return result


def _norm(g: Dict[str, Any]) -> float:
    try:
        return float(g.get("normalized") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def grader_validity(
    criteria: List[Dict[str, Any]], prompt: str, chosen: str, rejected: str,
    *, image: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """FIX-2: run the grader on the physician's own chosen vs rejected answers. Proves
    the criteria discriminate. ``needs_review`` when separation is low OR the rejected
    answer does not score below the chosen (ideally it critical-fails)."""
    if not (chosen or "").strip() or not (rejected or "").strip():
        return dict(_SKIPPED)
    gc, gr = await asyncio.gather(
        run_grader(criteria, prompt, chosen, image=image),
        run_grader(criteria, prompt, rejected, image=image),
    )
    if gc.get("skipped") or gr.get("skipped"):
        return dict(_SKIPPED)
    cn, rn = _norm(gc), _norm(gr)
    rej_crit = bool(gr.get("critical_failure"))
    separation = round(cn - rn, 3)
    needs_review = (separation < grader_min_separation()) or not (rej_crit or rn < cn)
    return {
        "chosen_normalized": round(cn, 3),
        "rejected_normalized": round(rn, 3),
        "rejected_critical_failed": rej_crit,
        "separation": separation,
        "min_separation": grader_min_separation(),
        "grader_model": (gc.get("grader_model") or "asclepius_critic"),
        "needs_review": needs_review,
    }


async def grader_reliability(
    criteria: List[Dict[str, Any]], prompt: str, chosen: str, *, runs: Optional[int] = None,
    image: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """FIX-2: grade the chosen answer N identical times; report normalized-score
    variance + per-criterion met/not-met flip rate. A CRITICAL criterion that flips
    across identical runs → ``unreliable`` (that criterion is too vague — loops to FIX-1)."""
    if not (chosen or "").strip():
        return dict(_SKIPPED)
    n = runs or grader_reliability_runs()
    grades = await asyncio.gather(*[run_grader(criteria, prompt, chosen, image=image) for _ in range(n)])
    grades = [g for g in grades if not g.get("skipped")]
    if len(grades) < 2:
        return dict(_SKIPPED)
    norms = [_norm(g) for g in grades]
    variance = round(pvariance(norms), 4) if len(norms) > 1 else 0.0
    # Per-criterion met/not-met across runs → flip rate + which flipped.
    crit_texts = [(c.get("text") or "").strip() for c in criteria]
    critical_texts = {(c.get("text") or "").strip() for c in criteria if c.get("critical") or c.get("tier") == "critical"}
    flipped: List[str] = []
    for ct in crit_texts:
        mets = set()
        for g in grades:
            for pc in (g.get("per_criterion") or []):
                if (pc.get("text") or "").strip() == ct:
                    mets.add(bool(pc.get("met")))
        if len(mets) > 1:
            flipped.append(ct)
    flip_rate = round(len(flipped) / len(crit_texts), 3) if crit_texts else 0.0
    critical_flip = any(ct in critical_texts for ct in flipped)
    consistent = variance <= grader_max_variance() and flip_rate == 0.0
    return {
        "runs": len(grades),
        "score_variance": variance,
        "flip_rate": flip_rate,
        "critical_flip": critical_flip,
        "consistent": consistent,
        "unreliable": critical_flip,
    }


async def hackability(
    criteria: List[Dict[str, Any]], prompt: str, chosen: str, rejected: str,
    *, image: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """FIX-8: build a padded-but-hollow answer (verbose hedging that ticks surface
    positives while committing the case's actual error) and a terse-but-correct answer,
    then grade both. ``gameable`` when the padded answer scores ≥ the terse-correct one
    — the classic reward-hacking failure. Adversarial answers are constructed from the
    physician's own chosen/rejected answers (no extra authoring needed)."""
    if not (chosen or "").strip() or not (rejected or "").strip():
        return dict(_SKIPPED)
    padded = (_HEDGE * 3) + rejected.strip()          # long + hollow + commits the error
    terse = _first_sentences(chosen, 2) or chosen.strip()   # short + nails the point
    gp, gt = await asyncio.gather(
        run_grader(criteria, prompt, padded, image=image),
        run_grader(criteria, prompt, terse, image=image),
    )
    if gp.get("skipped") or gt.get("skipped"):
        return dict(_SKIPPED)
    pn, tn = round(_norm(gp), 3), round(_norm(gt), 3)
    return {
        "padded_normalized": pn,
        "terse_correct_normalized": tn,
        "padded_critical_failed": bool(gp.get("critical_failure")),
        "gameable": pn >= tn,
    }


async def run_rubric_probes(
    rubric_record: Dict[str, Any], task: Dict[str, Any], submission: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all package-time grader probes and return the fields to patch onto the
    rubric record (FIX-2 + FIX-8). Never raises; each probe degrades to ``skipped``.
    Returns ``{}`` when there is nothing to add (no criteria / no answers)."""
    criteria = rubric_record.get("criteria") or []
    if not criteria:
        return {}
    # Fast path: with no LLM configured every probe would just skip — avoid the calls.
    try:
        from ai.model_config import is_anthropic_configured, is_openai_configured
        if not (is_anthropic_configured() or is_openai_configured()):
            return {"grader_validity": dict(_SKIPPED), "grader_reliability": dict(_SKIPPED),
                    "hackability": dict(_SKIPPED)}
    except Exception:  # pragma: no cover
        pass
    prompt = rubric_record.get("prompt") or task.get("prompt") or ""
    payload = submission.get("payload") or {}
    chosen = _chosen_text(task, payload)
    rejected = _rejected_text(task, payload)
    # V4 image case (PRD §7): the grader MUST receive the SAME image the models saw, or
    # the validity measurement is on a different input than they graded. If no vision
    # grader is available, mark the grader block skipped: vision_grader_unavailable —
    # never grade an image case text-only and call it validated.
    image = None
    try:
        from asclepius.baselines import _case_image_for_baseline
        image = _case_image_for_baseline(task)[0]
    except Exception:  # pragma: no cover
        image = None
    if image is not None:
        try:
            from ai.model_config import resolve, is_vision_capable
            grader_model = (resolve("asclepius_critic") or {}).get("model")
            if not is_vision_capable(grader_model):
                skip = {"skipped": True, "reason": "vision_grader_unavailable"}
                return {"grader_validity": dict(skip), "grader_reliability": dict(skip),
                        "hackability": dict(skip), "needs_review": True}
        except Exception:  # pragma: no cover
            pass
    try:
        validity, reliability, hack = await asyncio.gather(
            grader_validity(criteria, prompt, chosen, rejected, image=image),
            grader_reliability(criteria, prompt, chosen, image=image),
            hackability(criteria, prompt, chosen, rejected, image=image),
        )
    except Exception as exc:  # pragma: no cover - meta-eval must never break submit
        log.exception("run_rubric_probes failed: %s", exc)
        return {}
    patch: Dict[str, Any] = {
        "grader_validity": validity,
        "grader_reliability": reliability,
        "hackability": hack,
    }
    # Roll the probe verdicts into a single needs_review flag (already carries the
    # deterministic completeness/coverage signals via the record).
    patch["needs_review"] = bool(
        validity.get("needs_review") or reliability.get("unreliable") or hack.get("gameable")
        or rubric_record.get("uncovered_failure_modes")
    )
    return patch


def _chosen_text(task: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """The physician's endorsed answer: the revised chosen text, else the chosen
    candidate, else the blind ideal answer."""
    rev = (payload.get("chosen_revision") or {})
    if (rev.get("revised_text") or "").strip():
        return rev["revised_text"].strip()
    fs = (payload.get("from_scratch") or {})
    if (fs.get("ideal_answer") or "").strip():
        return fs["ideal_answer"].strip()
    cid = payload.get("chosen_id") or (task.get("candidate_answers") and None)
    for c in (task.get("candidate_answers") or []):
        if str(c.get("id")) == str(cid):
            return (c.get("text") or "").strip()
    ia = (payload.get("independent_answer") or {})
    return (ia.get("text") or "").strip()


def _rejected_text(task: Dict[str, Any], payload: Dict[str, Any]) -> str:
    rid = payload.get("rejected_id")
    for c in (task.get("candidate_answers") or []):
        if str(c.get("id")) == str(rid):
            return (c.get("text") or "").strip()
    return ""
