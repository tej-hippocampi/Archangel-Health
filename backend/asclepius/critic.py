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
import random
import re
from typing import Any, Dict, List, Optional

from asclepius.prompts import (
    ASCLEPIUS_CANDIDATE_GEN_SYSTEM,
    ASCLEPIUS_CASE_GEN_SYSTEM,
    ASCLEPIUS_CASE_JUDGE_SYSTEM,
    ASCLEPIUS_CRITIC_SYSTEM,
    ASCLEPIUS_GROUNDING_SYSTEM,
    ASCLEPIUS_HARDNESS_JUDGE_SYSTEM,
    ASCLEPIUS_PRELABEL_SYSTEM,
    ASCLEPIUS_PROMPT_GEN_SYSTEM,
    ASCLEPIUS_PROMPT_JUDGE_SYSTEM,
    ASCLEPIUS_REASONING_PREGRADE_SYSTEM,
    ASCLEPIUS_REASONING_SPLIT_SYSTEM,
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
    flawed_src = parsed.get("intended_flawed_id")

    # Tag each answer with whether the model marked IT as the intended-flawed one
    # BEFORE we reassign A/B, so the marker follows the text, not the slot.
    items: List[Dict[str, Any]] = []
    for i, c in enumerate(cands[:2]):
        src_id = c.get("id") or ("A" if i == 0 else "B")
        items.append({"text": c.get("text", ""), "is_flawed": src_id == flawed_src})

    # Randomize the A/B slot so the intended-flawed answer isn't position-biased
    # (Eval Flow Upgrade §5) — the model is asked to randomize but we enforce it
    # server-side regardless. The blinded evaluator only ever sees A/B + text.
    random.shuffle(items)

    out: List[Dict[str, str]] = []
    flawed: Optional[str] = None
    for i, it in enumerate(items):
        new_id = "A" if i == 0 else "B"
        out.append({"id": new_id, "text": it["text"], "generator_model": model or "asclepius_candidate_gen"})
        if it["is_flawed"] and flawed_src is not None:
            flawed = new_id
    return {"candidates": out, "model": model, "intended_flawed_id": flawed}


_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+")
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")


def _heuristic_split(text: str) -> List[str]:
    """Offline fallback splitter (no LLM): prefer explicit lines / numbered or
    bulleted items; otherwise sentence-split a single block. Conservative — the
    specialist edits/merges the result."""
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [_LIST_MARKER.sub("", ln).strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    if len(lines) >= 2:
        return lines
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(raw) if p.strip()]
    return parts if parts else [raw]


async def run_reasoning_split(
    text: str, *, prompt: str = "", specialty: str = "general"
) -> Dict[str, Any]:
    """Split a clinical answer into ordered reasoning steps for tap-to-grade
    (Eval Flow Upgrade §4). Returns ``{steps, source, skipped, model?}``. Never
    raises; degrades to a local heuristic split when no LLM is configured so the
    feature still pre-populates offline (the doctor can always edit/add manually)."""
    text = (text or "").strip()
    if not text:
        return {"steps": [], "source": "empty", "skipped": True}
    try:
        from ai.llm_client import call_llm, first_text
    except Exception:  # pragma: no cover
        return {"steps": _heuristic_split(text), "source": "heuristic", "skipped": True}

    user = f"Specialty: {specialty}\n\nPROMPT:\n{prompt}\n\nANSWER:\n{text}"
    try:
        resp, rec = await call_llm(
            role="asclepius_reasoning_split",
            system=ASCLEPIUS_REASONING_SPLIT_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_reasoning_split",
            purpose="asclepius_reasoning_split",
        )
    except Exception as exc:
        log.info("asclepius reasoning-split unavailable, using heuristic: %s", exc)
        return {"steps": _heuristic_split(text), "source": "heuristic", "skipped": True}

    parsed = _extract_json(first_text(resp)) or {}
    steps = [str(s).strip() for s in (parsed.get("steps") or []) if str(s).strip()]
    if not steps:
        return {"steps": _heuristic_split(text), "source": "heuristic", "skipped": False}
    return {"steps": steps, "source": "llm", "skipped": False, "model": (rec or {}).get("model")}


async def run_prelabel(task: Dict[str, Any]) -> Dict[str, Any]:
    """Model-assisted pre-label of a blinded A/B task (Speed Optimization §2):
    suggested weaker answer + error tags + a draft rationale + verbatim error
    spans, with a calibrated confidence. VERIFY-not-author: the caller shows
    these as tap-to-accept hints only — nothing is ever applied server-side.

    Returns ``{skipped, suggested_weaker, suggested_error_tags,
    suggested_rationale, error_spans, confidence, model?}``. Never raises;
    degrades to ``skipped=True`` with no API key (like the other critic fns).
    ``generator_model`` is never read or returned — the suggestion stays blind."""
    from asclepius.constants import ERROR_TAXONOMY

    empty = {"skipped": True, "suggested_weaker": None, "suggested_error_tags": [],
             "suggested_rationale": None, "error_spans": [], "confidence": None}
    cands = task.get("candidate_answers") or []
    texts = {str(c.get("id")): (c.get("text") or "") for c in cands}
    if not (texts.get("A") or "").strip() or not (texts.get("B") or "").strip():
        return {**empty, "error": "missing_candidates"}
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {**empty, "error": f"import:{exc}"}

    user = (
        f"Specialty: {task.get('specialty', 'general')}\n\n"
        f"ALLOWED ERROR TAGS: {', '.join(ERROR_TAXONOMY)}\n\n"
        f"PROMPT:\n{task.get('prompt', '')}\n\n"
        f"ANSWER A:\n{texts.get('A', '')}\n\n"
        f"ANSWER B:\n{texts.get('B', '')}"
    )
    try:
        resp, rec = await call_llm(
            role="asclepius_prelabel",
            system=ASCLEPIUS_PRELABEL_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_prelabel",
            purpose="asclepius_prelabel_suggestion",
        )
    except Exception as exc:
        log.info("asclepius prelabel skipped (no LLM): %s", exc)
        return {**empty, "error": str(exc)}

    parsed = _extract_json(first_text(resp)) or {}
    weaker = parsed.get("suggested_weaker")
    if weaker not in ("A", "B"):
        return {**empty, "error": "unparseable_prelabel_response"}
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0  # unstated confidence is treated as low (hidden)
    tags = [t for t in (parsed.get("suggested_error_tags") or []) if t in ERROR_TAXONOMY]
    weaker_text = texts.get(weaker, "")
    # Only keep spans that actually occur verbatim in the weaker answer, so the
    # UI can highlight them (and a hallucinated span can't mislead the doctor).
    spans = [
        str(s) for s in (parsed.get("error_spans") or [])
        if str(s).strip() and str(s) in weaker_text
    ][:3]
    return {
        "skipped": False,
        "suggested_weaker": weaker,
        "suggested_error_tags": tags,
        "suggested_rationale": (parsed.get("suggested_rationale") or "").strip() or None,
        "error_spans": spans,
        "confidence": confidence,
        "model": (rec or {}).get("model"),
    }


async def run_reasoning_pregrade(
    text: str, *, prompt: str = "", specialty: str = "general"
) -> Dict[str, Any]:
    """Split a clinical answer into ordered steps WITH a suggested per-step label
    (Speed Optimization §2). Returns ``{steps, source, skipped, model?}`` where
    each step is ``{text, suggested_label, suggested_critique}``. Never raises;
    degrades to the heuristic splitter with ``suggested_label=None`` offline so
    the doctor can always grade manually (labels stay null — silence is not a
    suggestion)."""
    text = (text or "").strip()
    if not text:
        return {"steps": [], "source": "empty", "skipped": True}

    def _plain(steps: List[str], source: str, skipped: bool) -> Dict[str, Any]:
        return {
            "steps": [{"text": s, "suggested_label": None, "suggested_critique": None} for s in steps],
            "source": source,
            "skipped": skipped,
        }

    try:
        from ai.llm_client import call_llm, first_text
    except Exception:  # pragma: no cover
        return _plain(_heuristic_split(text), "heuristic", True)

    user = f"Specialty: {specialty}\n\nPROMPT:\n{prompt}\n\nANSWER:\n{text}"
    try:
        resp, rec = await call_llm(
            role="asclepius_reasoning_pregrade",
            system=ASCLEPIUS_REASONING_PREGRADE_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_reasoning_pregrade",
            purpose="asclepius_reasoning_pregrade",
        )
    except Exception as exc:
        log.info("asclepius reasoning-pregrade unavailable, using heuristic: %s", exc)
        return _plain(_heuristic_split(text), "heuristic", True)

    parsed = _extract_json(first_text(resp)) or {}
    steps: List[Dict[str, Any]] = []
    for s in parsed.get("steps") or []:
        if isinstance(s, dict):
            stext = str(s.get("text") or "").strip()
            label = s.get("label") if s.get("label") in ("good", "bad") else None
            critique = (str(s.get("critique") or "").strip() or None) if label == "bad" else None
        else:
            stext, label, critique = str(s).strip(), None, None
        if stext:
            steps.append({"text": stext, "suggested_label": label, "suggested_critique": critique})
    if not steps:
        return _plain(_heuristic_split(text), "heuristic", False)
    return {"steps": steps, "source": "llm", "skipped": False, "model": (rec or {}).get("model")}


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


async def run_hardness_judge(
    prompt: str,
    candidates: Optional[List[Dict[str, Any]]] = None,
    *,
    failure_domains: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Score how genuinely HARD a prompt is on the WS2 rubric (Seamless PRD).
    Returns ``{skipped, hardness_score (0–1), hardness_axes, explanation, model}``.
    Never raises; degrades to ``skipped=True`` with no API key so offline
    generation is unaffected (the caller then does NOT drop on hardness)."""
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {"skipped": True, "error": f"import:{exc}"}

    ctx = ""
    if failure_domains:
        ctx = "\n\nKNOWN MODEL-FAILURE DOMAINS FOR THIS SPECIALTY:\n- " + "\n- ".join(failure_domains)
    lines = [f"PROMPT:\n{prompt}"]
    for c in candidates or []:
        lines.append(f"[{c.get('id')}]: {c.get('text', '')}")
    try:
        resp, rec = await call_llm(
            role="asclepius_hardness_judge",
            system=ASCLEPIUS_HARDNESS_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(lines) + ctx}],
            prompt_id="asclepius_hardness_judge",
            purpose="asclepius_hardness_judge",
        )
    except Exception as exc:
        log.info("asclepius hardness-judge unavailable: %s", exc)
        return {"skipped": True, "error": str(exc)}
    parsed = _extract_json(first_text(resp))
    if parsed is None:
        return {"skipped": True, "error": "unparseable_hardness_response"}
    try:
        score = float(parsed.get("hardness_score"))
    except (TypeError, ValueError):
        return {"skipped": True, "error": "no_hardness_score"}
    axes = [a for a in (parsed.get("hardness_axes") or []) if isinstance(a, str)]
    return {
        "skipped": False,
        "hardness_score": max(0.0, min(1.0, score)),
        "hardness_axes": axes,
        "explanation": parsed.get("explanation", ""),
        "model": (rec or {}).get("model"),
    }


# ─── Multimodal case generation + judge (Synthetic Multimodal Cases PRD §3) ────
async def generate_case(archetype: Dict[str, Any], *, specialty: str = "general") -> Dict[str, Any]:
    """Author a PHI-free ClinicalCase from a multimodal archetype (the archetype's
    ``multimodal`` block seeds the panels/hooks/ground-truth). Returns
    ``{case, question, model, skipped}``; the case is coerced through the
    ClinicalCase model (so extra fields drop, shape is guaranteed) and stamped
    ``case_source='synthetic'``. Never raises; degrades to ``skipped=True`` with
    no LLM (the caller then drops nothing / disables generation, same contract as
    the other generators)."""
    empty = {"case": None, "question": None, "model": None, "skipped": True}
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {**empty, "error": f"import:{exc}"}
    from asclepius.cases import ClinicalCase

    mm = (archetype or {}).get("multimodal") or {}
    ctx = [f"Specialty: {specialty}", f"Archetype topic: {archetype.get('topic', '')}"]
    if archetype.get("why_hard"):
        ctx.append(f"Why hard: {archetype['why_hard']}")
    if mm.get("panels"):
        ctx.append("Lab panels to synthesize: " + ", ".join(map(str, mm["panels"])))
    if mm.get("note_types"):
        ctx.append("Note types: " + ", ".join(map(str, mm["note_types"])))
    if mm.get("hard_hook"):
        ctx.append("Hard hook (the data-integration trap): " + str(mm["hard_hook"]))
    if mm.get("ground_truth_spec"):
        ctx.append("Ground-truth spec (the objectively correct answer): " + str(mm["ground_truth_spec"]))
    if mm.get("reasoning_divergence"):
        ctx.append("Reasoning divergence (sound vs shortcut path): " + str(mm["reasoning_divergence"]))
    try:
        resp, rec = await call_llm(
            role="asclepius_case_gen",
            system=ASCLEPIUS_CASE_GEN_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(ctx)}],
            prompt_id="asclepius_case_gen",
            purpose="asclepius_case_generation",
        )
    except Exception as exc:
        log.info("asclepius case-gen unavailable: %s", exc)
        return empty
    # The LLM answered — from here a failure is a BAD CASE (drop this one item),
    # NOT "no LLM" (which is skipped=True and stops the whole run). Return
    # skipped=False + case=None so the caller counts case_gen_failed and continues
    # to the next archetype instead of aborting the batch as "no LLM configured".
    bad = {"case": None, "question": None, "model": (rec or {}).get("model"), "skipped": False}
    parsed = _extract_json(first_text(resp))
    if not isinstance(parsed, dict):
        return {**bad, "error": "unparseable_case"}
    question = (parsed.get("question") or "").strip()
    raw_case = parsed.get("case")
    if not question or not isinstance(raw_case, dict):
        return {**bad, "error": "incomplete_case"}
    try:
        case = ClinicalCase(**raw_case).model_dump()
    except Exception as exc:  # schema mismatch → drop this item
        log.info("asclepius case-gen schema error: %s", exc)
        return {**bad, "error": "case_schema"}
    case["case_source"] = "synthetic"
    case.setdefault("specialty", specialty)
    return {"case": case, "question": question, "model": (rec or {}).get("model"), "skipped": False}


async def run_case_judge(case: Dict[str, Any], case_source: str = "synthetic") -> Dict[str, Any]:
    """Score a case on multimodal dimensions ONLY (hardness is judged separately by
    ``run_hardness_judge``). Returns
    ``{skipped, coherence, ground_truth_determinable, multimodal_necessity,
    reasoning_divergence_potential, explanation, model}`` — each 0..1. Never
    raises; degrades to ``skipped=True`` with no LLM (the caller then does NOT
    drop on case dims, same contract as the hardness judge).

    REAL-CASE VARIANT (EHR PRD §9): for ``case_source='real_deid'`` the
    ``ground_truth_determinable`` dimension is returned as ``None`` and never
    judged — a real case carries no synthetic answer key; the SPECIALIST is the
    answer key (and, later, the real outcome). Gates must skip that dimension."""
    try:
        from ai.llm_client import call_llm, first_text
    except Exception as exc:  # pragma: no cover
        return {"skipped": True, "error": f"import:{exc}"}
    from asclepius.cases import as_dict, render_case_prompt

    is_real = (case_source == "real_deid")
    cd = as_dict(case) or {}
    serialized = render_case_prompt(cd, "(case under review)")   # PUBLIC render (no key)
    gt = cd.get("ground_truth") or {}
    if is_real:
        internal = (
            "\n\nNOTE: this is a REAL de-identified case with NO synthetic answer key. "
            "Do NOT judge ground_truth_determinable (return null for it); judge "
            "coherence, multimodal_necessity, and reasoning_divergence_potential only."
        )
    else:
        internal = (
            "\n\nINTERNAL (for judging ground_truth_determinable + divergence only):\n"
            f"ground_truth.answer: {gt.get('answer', '')}\n"
            f"hard_hook: {cd.get('hard_hook', '')}\n"
            f"reasoning_divergence: {cd.get('reasoning_divergence', '')}"
        )
    try:
        resp, rec = await call_llm(
            role="asclepius_case_judge",
            system=ASCLEPIUS_CASE_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": serialized + internal}],
            prompt_id="asclepius_case_judge",
            purpose="asclepius_case_judge",
        )
    except Exception as exc:
        log.info("asclepius case-judge unavailable: %s", exc)
        return {"skipped": True, "error": str(exc)}
    parsed = _extract_json(first_text(resp))
    if not isinstance(parsed, dict):
        return {"skipped": True, "error": "unparseable_case_judge"}

    def _f(v: Any) -> Optional[float]:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return None

    return {
        "skipped": False,
        "coherence": _f(parsed.get("coherence")),
        # Real cases NEVER carry this dimension (no synthetic answer key exists) —
        # forced None regardless of what the model returned, so no gate can
        # accidentally apply the floor to a real case.
        "ground_truth_determinable": (None if is_real else _f(parsed.get("ground_truth_determinable"))),
        "multimodal_necessity": _f(parsed.get("multimodal_necessity")),
        "reasoning_divergence_potential": _f(parsed.get("reasoning_divergence_potential")),
        "explanation": parsed.get("explanation", ""),
        "model": (rec or {}).get("model"),
    }
