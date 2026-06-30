"""Packaging — transform a raw submission into frontier-lab-ready training
records (PRD §5 step 2, §6.3; opt §1.1, §1.2, §1.4).

Pure functions, no I/O: ``package_submission(task, submission)`` returns a list
of *canonical* record dicts. The store assigns ``record_id`` and persists them.
The buyer-specific field mapping + variant selection happens later in
``export.py``; here we emit the maximally-rich canonical signal so a buyer
profile can map/filter it with zero rework.

Three canonical record types, each in buyer-ready shape:
  * preference     — hh-rlhf style: flat ``{prompt, chosen, rejected}`` AND a
                     chat variant (``chosen_messages``/``rejected_messages`` with
                     roles). One submission → potentially multiple records.
  * ideal_answer   — SFT ``{prompt, completion}`` (alias instruction/response)
                     from the revised chosen OR the from-scratch answer.
  * reasoning_trace— PRM800K style: ordered steps each independently labeled
                     ``good|neutral|bad`` with an optional numeric ``step_reward``
                     and an optional evidence anchor.

Every record carries full provenance + rights attestation (opt §1.4):
credential, hashed id, taxonomy/config version, task source, buyer-request id,
license, ip_cleared, contains_phi (asserted), and grounded (evidence-anchored).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_TAXONOMY_VERSION,
    default_ip_cleared,
    default_license,
)
from asclepius.validation import is_valid_anchor


def _candidate_text(task: Dict[str, Any], cid: Optional[str]) -> str:
    if not cid:
        return ""
    for c in task.get("candidate_answers", []) or []:
        if str(c.get("id")) == str(cid):
            return c.get("text", "") or ""
    return ""


def _context(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "specialty": task.get("specialty"),
        "difficulty": task.get("difficulty"),
    }


def _anchor(a: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize an evidence anchor to the canonical 3-key shape, or None."""
    if not a or not isinstance(a, dict):
        return None
    if not any((a.get("citation_text"), a.get("source_type"), a.get("identifier"))):
        return None
    return {
        "citation_text": a.get("citation_text"),
        "source_type": a.get("source_type"),
        "identifier": a.get("identifier"),
    }


def _generation_provenance(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Buyer-facing synthetic-prompt provenance (PRD §9.1): a record's prompt was
    auto-generated (not lab-supplied), traceable to the corpus version + models.
    The server-side ``intended_flawed_id`` is stripped — it never leaves the
    portal in a delivered record."""
    gen = task.get("generation")
    if not gen or not isinstance(gen, dict):
        return None
    out = {k: v for k, v in gen.items() if k != "intended_flawed_id"}
    return out


def _prompt_clinician_reviewed(submission: Dict[str, Any]) -> bool:
    """True when the clinician signed off the prompt as valid at eval time (Eval
    Flow Upgrade §2). Carries onto every record so the datasheet can upgrade the
    synthetic-prompt provenance from AI-drafted to clinician-reviewed."""
    review = (submission.get("payload") or {}).get("prompt_review") or {}
    return review.get("verdict") == "valid"


def _provenance(task: Dict[str, Any], submission: Dict[str, Any]) -> Dict[str, Any]:
    annotator = submission.get("annotator") or {}
    prov = {
        # prompt provenance upgrade (Eval Flow Upgrade §2) — the prompt was
        # reviewed and accepted as clinically valid by the credentialed evaluator.
        "prompt_clinician_reviewed": _prompt_clinician_reviewed(submission),
        # credentialing (the premium signal)
        "annotator_credential": annotator.get("credentials"),
        "annotator_specialty": annotator.get("specialty"),
        "annotator_years_experience": annotator.get("years_experience"),
        "annotator_id_hashed": annotator.get("id_hashed"),
        # lineage
        "submission_id": submission.get("submission_id"),
        "task_id": task.get("task_id"),
        "source": task.get("source"),
        "buyer_request_id": task.get("buyer_request_id"),
        # versioning
        "taxonomy_version": ASCLEPIUS_TAXONOMY_VERSION,
        "config_version": ASCLEPIUS_CONFIG_VERSION,
        "ai_config_version": ASCLEPIUS_CONFIG_VERSION,
        # rights attestation (opt §1.4)
        "license": default_license(),
        "ip_cleared": bool(default_ip_cleared()),
        "contains_phi": False,
        # status-change timestamp (capture time; export stamps exported_at)
        "captured_at": submission.get("created_at"),
    }
    gen = _generation_provenance(task)
    if gen is not None:
        prov["generation"] = gen
    return prov


def _steps_payload(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PRM800K-style ordered steps; each independently labeled + optionally
    anchored (opt §1.1, §1.2).

    Edit-to-Correct (Reasoning Capture v2): each step carries BOTH the AI's split
    step (``original_text`` — the negative) and the expert's confirmed/corrected
    gold (``text``), so the record is a step-level preference/correction pair."""
    out = []
    for i, s in enumerate(steps or [], start=1):
        out.append(
            {
                "step": s.get("step", i),
                "text": s.get("text", ""),  # confirmed/corrected gold
                # The AI's split step before the edit (negative); None if authored.
                "original_text": s.get("original_text"),
                "corrected": bool(s.get("corrected")),
                "confirmed": bool(s.get("confirmed")),
                "added": bool(s.get("added")),
                # PRM800K per-step label; fall back to legacy free-text tag.
                "label": s.get("label") if s.get("label") is not None else s.get("tag"),
                # Why the edited step was wrong (drives the derived label).
                "correction_reason": s.get("correction_reason"),
                "step_reward": s.get("step_reward"),
                # One-line "what's off?" critique on graded steps (Eval Flow Upgrade §4).
                "critique": s.get("critique"),
                "evidence_anchor": _anchor(s.get("evidence_anchor")),
            }
        )
    return out


def _step_pairs(steps: List[Dict[str, Any]], prompt: str) -> List[Dict[str, Any]]:
    """Ready-made step-level preference pairs for every corrected step: the AI's
    original step (rejected) vs the expert's gold (chosen) + the clinical reason.
    Additive convenience over the per-step fields (those stay the source of truth)."""
    pairs: List[Dict[str, Any]] = []
    for s in steps or []:
        if s.get("corrected") and (s.get("original_text") or "").strip():
            pairs.append(
                {
                    "prompt_context": prompt,
                    "rejected": s.get("original_text"),
                    "chosen": s.get("text", ""),
                    "reason": s.get("correction_reason"),
                }
            )
    return pairs


def _steps_grounded(steps: List[Dict[str, Any]]) -> bool:
    return bool(steps) and all(is_valid_anchor(s.get("evidence_anchor")) for s in steps)


def _chat(prompt: str, completion: str) -> List[Dict[str, str]]:
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": completion},
    ]


def package_submission(task: Dict[str, Any], submission: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = submission.get("payload") or {}
    verdict = submission.get("verdict") or payload.get("verdict")
    prompt = task.get("prompt", "")
    prov = _provenance(task, submission)
    records: List[Dict[str, Any]] = []

    if verdict in ("A_better", "B_better"):
        chosen_id = submission.get("chosen_id") or payload.get("chosen_id")
        rejected_id = submission.get("rejected_id") or payload.get("rejected_id")
        original_chosen = _candidate_text(task, chosen_id)
        rejected_text = _candidate_text(task, rejected_id)

        revision = payload.get("chosen_revision") or {}
        revised_text = (revision.get("revised_text") or "").strip()
        chosen_text = revised_text if (revision.get("edited") and revised_text) else original_chosen

        critique = payload.get("rejected_critique") or {}
        error_tags = list(critique.get("error_tags") or [])
        rationale = (revision.get("why_better_notes") or "").strip() or (
            critique.get("why_worse") or ""
        ).strip()

        rationale_anchor = _anchor(revision.get("evidence_anchor"))
        # Per-error-tag evidence anchors (opt §1.2).
        tag_anchors_raw = critique.get("error_tag_anchors") or {}
        error_tag_anchors = {
            tag: _anchor(anc) for tag, anc in tag_anchors_raw.items() if _anchor(anc)
        }
        # Premium-tier grounded flag counts a valid anchor on the rationale OR on
        # any error tag, so the grounded count is not undercounted (FIX 5).
        grounded = is_valid_anchor(revision.get("evidence_anchor")) or any(
            is_valid_anchor(anc) for anc in tag_anchors_raw.values()
        )

        preference = {
            "type": "preference",
            # flat hh-rlhf variant
            "prompt": prompt,
            "chosen": chosen_text,
            "rejected": rejected_text,
            # chat hh-rlhf variant (messages arrays with roles)
            "chosen_messages": _chat(prompt, chosen_text),
            "rejected_messages": _chat(prompt, rejected_text),
            "context": _context(task),
            "rationale": rationale,
            "evidence_anchor": rationale_anchor,
            "why_better_tags": list(revision.get("why_better_tags") or []),
            "error_tags_on_rejected": error_tags,
            "error_tag_anchors": error_tag_anchors,
            "error_severities": dict(critique.get("severities") or {}),
            "confidence": submission.get("confidence"),
            "grounded": grounded,
            "agreement_score": submission.get("agreement_score"),
            **prov,
        }
        records.append(preference)

        # A specialist revision of the chosen answer is also a high-quality SFT
        # target (the corrected ideal answer).
        if revision.get("edited") and revised_text:
            records.append(
                {
                    "type": "ideal_answer",
                    "prompt": prompt,
                    "ideal_answer": revised_text,
                    "completion": revised_text,  # SFT {prompt, completion} alias
                    "messages": _chat(prompt, revised_text),
                    "approach_notes": (revision.get("why_better_notes") or "").strip(),
                    "evidence_anchor": rationale_anchor,
                    "context": _context(task),
                    "confidence": submission.get("confidence"),
                    "grounded": grounded,
                    **prov,
                }
            )

    elif verdict == "both_inadequate":
        fs = payload.get("from_scratch") or {}
        ideal = (fs.get("ideal_answer") or "").strip()
        approach = (fs.get("approach_notes") or "").strip()
        rationale_anchor = _anchor(fs.get("evidence_anchor"))
        grounded = is_valid_anchor(fs.get("evidence_anchor"))
        records.append(
            {
                "type": "ideal_answer",
                "prompt": prompt,
                "ideal_answer": ideal,
                "completion": ideal,  # SFT {prompt, completion} alias
                "messages": _chat(prompt, ideal),
                "approach_notes": approach,
                "evidence_anchor": rationale_anchor,
                "context": _context(task),
                "confidence": submission.get("confidence"),
                "grounded": grounded,
                **prov,
            }
        )
        steps = _steps_payload(fs.get("reasoning_steps") or [])
        if steps:
            records.append(
                {
                    "type": "reasoning_trace",
                    "prompt": prompt,
                    "steps": steps,
                    "step_pairs": _step_pairs(steps, prompt),
                    "final_answer": ideal,
                    "context": _context(task),
                    "grounded": _steps_grounded(steps),
                    **prov,
                }
            )

    # Blind independent answer (Eval Flow Upgrade §3): the doctor's full ideal
    # answer, written BEFORE the A/B candidates were revealed. Emitted as an
    # ADDITIONAL premium SFT record so one submission can yield preference +
    # revised-ideal + independent-ideal (+ reasoning_trace). Flagged ``independent``
    # so a buyer can isolate uncontaminated gold answers.
    ia = payload.get("independent_answer") or {}
    ia_text = (ia.get("text") or "").strip()
    if ia_text:
        records.append(
            {
                "type": "ideal_answer",
                "prompt": prompt,
                "ideal_answer": ia_text,
                "completion": ia_text,  # SFT {prompt, completion} alias
                "messages": _chat(prompt, ia_text),
                "independent": True,  # written BEFORE seeing A/B (premium SFT)
                "evidence_anchor": _anchor(ia.get("evidence_anchor")),
                "context": _context(task),
                "confidence": submission.get("confidence"),
                "grounded": is_valid_anchor(ia.get("evidence_anchor")),
                **prov,
            }
        )

    # Top-level reasoning steps (reasoning-trace tasks, PRD §4.2) attach to any
    # verdict path. For the both_inadequate path the from-scratch trace already
    # captured them, so only add when not already present.
    top_steps = _steps_payload(payload.get("reasoning_steps") or [])
    if top_steps and not any(r["type"] == "reasoning_trace" for r in records):
        final = ""
        if verdict in ("A_better", "B_better"):
            revision = payload.get("chosen_revision") or {}
            final = (revision.get("revised_text") or "").strip() or _candidate_text(
                task, submission.get("chosen_id")
            )
        records.append(
            {
                "type": "reasoning_trace",
                "prompt": prompt,
                "steps": top_steps,
                "step_pairs": _step_pairs(top_steps, prompt),
                "final_answer": final,
                "context": _context(task),
                "grounded": _steps_grounded(top_steps),
                **prov,
            }
        )

    return records
