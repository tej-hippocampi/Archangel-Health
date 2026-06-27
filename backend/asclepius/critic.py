"""LLM consistency double-check + optional candidate generation (PRD §5.4, §8).

Both go through ``ai.llm_client.call_llm`` so they are audit-logged via the
existing LLM audit path and covered by the Anthropic BAA. They degrade
gracefully when no API key is configured (so the portal, tests, and local demos
work offline): the critic returns ``skipped=True`` rather than blocking, and the
submission still flows through the human-QA sampling gate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from asclepius.prompts import (
    ASCLEPIUS_CANDIDATE_GEN_SYSTEM,
    ASCLEPIUS_CRITIC_SYSTEM,
    ASCLEPIUS_GROUNDING_SYSTEM,
    ASCLEPIUS_PROMPT_GEN_SYSTEM,
    ASCLEPIUS_PROMPT_JUDGE_SYSTEM,
)
from asclepius.validation import is_valid_anchor

log = logging.getLogger("asclepius.critic")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # tolerate ```json fences / surrounding prose
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


def _candidate_text(task: Dict[str, Any], cid: Optional[str]) -> str:
    for c in task.get("candidate_answers", []) or []:
        if str(c.get("id")) == str(cid):
            return c.get("text", "") or ""
    return ""


def _build_critic_user(task: Dict[str, Any], submission: Dict[str, Any]) -> str:
    payload = submission.get("payload") or {}
    verdict = submission.get("verdict")
    revision = payload.get("chosen_revision") or {}
    critique = payload.get("rejected_critique") or {}
    fs = payload.get("from_scratch") or {}
    lines = [
        f"PROMPT:\n{task.get('prompt', '')}",
        f"\nVERDICT: {verdict}",
        f"CONFIDENCE: {submission.get('confidence')}",
    ]
    if verdict in ("A_better", "B_better"):
        lines.append(f"\nCHOSEN (original):\n{_candidate_text(task, submission.get('chosen_id'))}")
        if revision.get("edited") and revision.get("revised_text"):
            lines.append(f"\nCHOSEN (specialist revision):\n{revision.get('revised_text')}")
        lines.append(f"\nREJECTED:\n{_candidate_text(task, submission.get('rejected_id'))}")
        lines.append(f"\nWHY-BETTER TAGS: {revision.get('why_better_tags')}")
        lines.append(f"WHY-BETTER NOTES: {revision.get('why_better_notes')}")
        lines.append(f"ERROR TAGS ON REJECTED: {critique.get('error_tags')}")
        lines.append(f"WHY WORSE: {critique.get('why_worse')}")
    elif verdict == "both_inadequate":
        lines.append(f"\nIDEAL ANSWER (from scratch):\n{fs.get('ideal_answer')}")
        lines.append(f"\nAPPROACH NOTES: {fs.get('approach_notes')}")
    return "\n".join(lines)


async def run_critic(task: Dict[str, Any], submission: Dict[str, Any]) -> Dict[str, Any]:
    """Return {consistent, issues, explanation, skipped, model?}. Never raises."""
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {"consistent": True, "issues": [], "skipped": True, "error": f"import:{exc}"}

    user = _build_critic_user(task, submission)
    try:
        resp, rec = await call_llm(
            role="asclepius_critic",
            system=ASCLEPIUS_CRITIC_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_critic",
            purpose="asclepius_consistency_check",
        )
    except Exception as exc:
        log.info("asclepius critic skipped (no LLM): %s", exc)
        return {"consistent": True, "issues": [], "skipped": True, "error": str(exc)}

    parsed = _extract_json(first_text(resp)) or {}
    consistent = bool(parsed.get("consistent", True))
    return {
        "consistent": consistent,
        "issues": list(parsed.get("issues") or []),
        "explanation": parsed.get("explanation", ""),
        "skipped": False,
        "model": (rec or {}).get("model"),
    }


def _collect_anchored_claims(task: Dict[str, Any], submission: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gather (claim, anchor) pairs that carry a valid evidence anchor."""
    payload = submission.get("payload") or {}
    verdict = submission.get("verdict")
    claims: List[Dict[str, Any]] = []

    if verdict in ("A_better", "B_better"):
        revision = payload.get("chosen_revision") or {}
        if is_valid_anchor(revision.get("evidence_anchor")):
            claims.append(
                {"claim": (revision.get("why_better_notes") or "the chosen answer is better"),
                 "anchor": revision.get("evidence_anchor")}
            )
        critique = payload.get("rejected_critique") or {}
        for tag, anc in (critique.get("error_tag_anchors") or {}).items():
            if is_valid_anchor(anc):
                claims.append({"claim": f"rejected answer has error: {tag}", "anchor": anc})
        steps = payload.get("reasoning_steps") or []
    elif verdict == "both_inadequate":
        fs = payload.get("from_scratch") or {}
        if is_valid_anchor(fs.get("evidence_anchor")):
            claims.append(
                {"claim": (fs.get("approach_notes") or "the ideal answer"), "anchor": fs.get("evidence_anchor")}
            )
        steps = fs.get("reasoning_steps") or []
    else:
        steps = []

    for s in steps:
        if is_valid_anchor(s.get("evidence_anchor")):
            claims.append({"claim": s.get("text") or "reasoning step", "anchor": s.get("evidence_anchor")})

    return claims


async def run_grounding_check(task: Dict[str, Any], submission: Dict[str, Any]) -> Dict[str, Any]:
    """Optional evidence-grounding sanity check (opt §1.2, §5).

    Only runs when the submission carries valid evidence anchors. Asks the judge
    whether each citation plausibly supports its claim. Returns
    {grounding_ok, issues, explanation, skipped, checked_anchors}. Never raises;
    degrades to skipped=True with no API key."""
    claims = _collect_anchored_claims(task, submission)
    if not claims:
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {"grounding_ok": True, "issues": [], "skipped": True, "error": f"import:{exc}",
                "checked_anchors": len(claims)}

    lines = [f"PROMPT:\n{task.get('prompt', '')}", "\nCLAIMS + CITATIONS:"]
    for i, c in enumerate(claims, start=1):
        a = c["anchor"]
        lines.append(
            f"{i}. CLAIM: {c['claim']}\n   CITATION: {a.get('citation_text')} "
            f"[{a.get('source_type')}] ({a.get('identifier')})"
        )
    try:
        resp, rec = await call_llm(
            role="asclepius_grounding",
            system=ASCLEPIUS_GROUNDING_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            prompt_id="asclepius_grounding",
            purpose="asclepius_grounding_check",
        )
    except Exception as exc:
        log.info("asclepius grounding check skipped (no LLM): %s", exc)
        return {"grounding_ok": True, "issues": [], "skipped": True, "error": str(exc),
                "checked_anchors": len(claims)}

    parsed = _extract_json(first_text(resp)) or {}
    return {
        "grounding_ok": bool(parsed.get("grounding_ok", True)),
        "issues": list(parsed.get("issues") or []),
        "explanation": parsed.get("explanation", ""),
        "skipped": False,
        "checked_anchors": len(claims),
        "model": (rec or {}).get("model"),
    }


async def generate_candidates_ex(
    prompt: str, *, specialty: str = "general", ai_failure_mode: Optional[str] = None
) -> Dict[str, Any]:
    """Generate two blinded candidate answers (one strong, one plausibly-flawed).

    Returns ``{candidates, model, intended_flawed_id}``. ``candidates`` is empty
    on failure (no LLM key / parse error). ``intended_flawed_id`` (the answer the
    model deliberately made weaker, PRD §7.2, §16) is kept server-side only — the
    blinded eval screen never sees it. Never raises."""
    empty = {"candidates": [], "model": None, "intended_flawed_id": None}
    try:
        from ai.llm_client import call_llm, first_text
    except Exception:  # pragma: no cover
        return empty
    user = f"Specialty: {specialty}\n\nPROMPT:\n{prompt}"
    if ai_failure_mode:
        user += f"\n\nAI_FAILURE_MODE (key the flawed answer to this): {ai_failure_mode}"
    try:
        resp, rec = await call_llm(
            role="asclepius_candidate_gen",
            system=ASCLEPIUS_CANDIDATE_GEN_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_candidate_gen",
            purpose="asclepius_candidate_generation",
        )
    except Exception as exc:
        log.info("asclepius candidate-gen unavailable: %s", exc)
        return empty
    parsed = _extract_json(first_text(resp)) or {}
    cands = parsed.get("candidate_answers") or []
    model = (rec or {}).get("model")
    out: List[Dict[str, str]] = []
    for i, c in enumerate(cands[:2]):
        out.append(
            {
                "id": c.get("id") or ("A" if i == 0 else "B"),
                "text": c.get("text", ""),
                "generator_model": model or "asclepius_candidate_gen",
            }
        )
    flawed = parsed.get("intended_flawed_id")
    valid_ids = {c["id"] for c in out}
    if flawed not in valid_ids:
        flawed = None
    return {"candidates": out, "model": model, "intended_flawed_id": flawed}


async def generate_candidates(prompt: str, *, specialty: str = "general") -> List[Dict[str, str]]:
    """Generate two blinded candidate answers for a prompt. Returns [] on failure.

    Thin back-compat wrapper over :func:`generate_candidates_ex` (drops the
    server-side intended-flawed id) for existing callers (/tasks/generate)."""
    return (await generate_candidates_ex(prompt, specialty=specialty)).get("candidates", [])


async def run_prompt_gen(
    *,
    specialty: str,
    bucket_id: str,
    bucket_label: str,
    exemplars: List[Dict[str, Any]],
    failure_modes: List[str],
    n: int,
    difficulty: Optional[str] = None,
) -> Dict[str, Any]:
    """Synthesize ``n`` novel prompts for a taxonomy bucket from few-shot seed
    exemplars (PRD §7.1). Returns ``{prompts, model, skipped}``. Never raises.

    ``difficulty`` (optional) steers the requested difficulty for ``difficulty_mix``
    quotas; ``None`` leaves the model free to choose (back-compatible)."""
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {"prompts": [], "skipped": True, "error": f"import:{exc}"}

    ex_lines = []
    for i, e in enumerate(exemplars, start=1):
        ex_lines.append(
            f"{i}. [{e.get('difficulty')}] {e.get('prompt')}\n"
            f"   (failure mode: {e.get('ai_failure_mode')})"
        )
    fm_lines = "\n".join(f"- {m}" for m in failure_modes if m) or "- (use clinical judgment)"
    diff_line = (
        f"Target difficulty for these prompts: {difficulty} "
        f"(set each prompt's \"difficulty\" field to \"{difficulty}\").\n\n"
        if difficulty in ("easy", "medium", "hard")
        else ""
    )
    user = (
        f"Specialty: {specialty}\n"
        f"Taxonomy bucket: {bucket_id} — {bucket_label}\n\n"
        f"{diff_line}"
        f"Known AI failure modes to target in this bucket:\n{fm_lines}\n\n"
        f"EXEMPLAR seed prompts (write NEW, distinct vignettes in this profile — do NOT paraphrase these):\n"
        + "\n".join(ex_lines)
        + f"\n\nProduce exactly {n} new prompt object(s) for bucket '{bucket_id}'."
    )
    try:
        resp, rec = await call_llm(
            role="asclepius_prompt_gen",
            system=ASCLEPIUS_PROMPT_GEN_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_prompt_gen",
            purpose="asclepius_prompt_generation",
        )
    except Exception as exc:
        log.info("asclepius prompt-gen unavailable: %s", exc)
        return {"prompts": [], "skipped": True, "error": str(exc)}
    parsed = _extract_json(first_text(resp)) or {}
    prompts = parsed.get("prompts") or []
    return {"prompts": list(prompts), "model": (rec or {}).get("model"), "skipped": False}


async def run_prompt_judge(prompt: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score a candidate (prompt, answers) on error_likelihood / revision_value /
    on_specialty / safety_ok (PRD §7.3). Returns the scores + ``skipped``. Never
    raises; degrades to skipped=True with no API key (caller drops the item)."""
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {"skipped": True, "error": f"import:{exc}"}

    lines = [f"PROMPT:\n{prompt}", "\nCANDIDATE ANSWERS:"]
    for c in candidates:
        lines.append(f"[{c.get('id')}]: {c.get('text', '')}")
    try:
        resp, rec = await call_llm(
            role="asclepius_prompt_judge",
            system=ASCLEPIUS_PROMPT_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            prompt_id="asclepius_prompt_judge",
            purpose="asclepius_prompt_judge",
        )
    except Exception as exc:
        log.info("asclepius prompt-judge unavailable: %s", exc)
        return {"skipped": True, "error": str(exc)}
    parsed = _extract_json(first_text(resp))
    if parsed is None:
        return {"skipped": True, "error": "unparseable_judge_response"}

    def _f(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "skipped": False,
        "error_likelihood": _f(parsed.get("error_likelihood")),
        "revision_value": _f(parsed.get("revision_value")),
        "on_specialty": bool(parsed.get("on_specialty", True)),
        "safety_ok": bool(parsed.get("safety_ok", True)),
        "explanation": parsed.get("explanation", ""),
        "model": (rec or {}).get("model"),
    }
