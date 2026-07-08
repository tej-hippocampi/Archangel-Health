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
    label_for_correction_reason,
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
    """Normalize an evidence anchor to the canonical shape, or None. Carries the
    library ``url`` and the ``citation_confirmed`` flag (Seamless PRD WS3) so a
    buyer can tell a clinician-confirmed library citation from a hand-typed one."""
    if not a or not isinstance(a, dict):
        return None
    if not any((a.get("citation_text"), a.get("source_type"), a.get("identifier"))):
        return None
    out = {
        "citation_text": a.get("citation_text"),
        "source_type": a.get("source_type"),
        "identifier": a.get("identifier"),
    }
    if a.get("url"):
        out["url"] = a.get("url")
    if a.get("citation_confirmed") is not None:
        out["citation_confirmed"] = bool(a.get("citation_confirmed"))
    return out


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
    payload = submission.get("payload") or {}
    prov = {
        # Which evaluator flow produced this record (Asclepius V2): "v1" classic
        # | "v2" assisted — carried onto every record so admin/buyers segment by
        # product version.
        "portal_version": _portal_version(submission, payload),
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
        reason = s.get("correction_reason")
        # PRM800K per-step label; fall back to legacy free-text tag.
        label = s.get("label") if s.get("label") is not None else s.get("tag")
        # Authoritative: a corrected step's buyer-facing label is DERIVED from the
        # clinical reason here, never trusted from the client — keeping label and
        # reason consistent is what makes this data sellable.
        if s.get("corrected") and reason:
            label = label_for_correction_reason(reason)
        out.append(
            {
                "step": s.get("step", i),
                "text": s.get("text", ""),  # confirmed/corrected gold
                # The AI's split step before the edit (negative); None if authored.
                "original_text": s.get("original_text"),
                "corrected": bool(s.get("corrected")),
                "confirmed": bool(s.get("confirmed")),
                "added": bool(s.get("added")),
                "label": label,
                # The pre-grader's suggestion (Speed Optimization §2), carried
                # ALONGSIDE the human label so override rate is monitorable.
                "suggested_label": s.get("suggested_label"),
                # Why the edited step was wrong (drives the derived label).
                "correction_reason": reason,
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


def _portal_version(submission: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """Which evaluator flow produced this submission — the authoritative source
    is the submission row (stamped server-side); fall back to the payload for
    pure packaging unit tests, then the default."""
    from asclepius.constants import normalize_portal_version

    ia = payload.get("independent_answer") or {}
    return normalize_portal_version(
        submission.get("portal_version") or payload.get("portal_version") or ia.get("portal_version")
    )


def _independent_kind(task: Dict[str, Any], ia: Dict[str, Any], portal_version: str) -> str:
    """Stage-2 capture kind, by portal version (delegates to the single source of
    truth in constants): V1 always ``full``; V3 defaults to the ~10s ``instinct``
    one-liner (``full`` only when the admin marked the task so); V2 respects the
    task's ``independent_mode`` (``stance`` default). The TASK + portal version are
    authoritative — a client-supplied ``kind`` can never upgrade a lightweight
    capture into a premium blind-gold record."""
    from asclepius.constants import independent_capture_kind

    return independent_capture_kind(
        portal_version, task.get("independent_mode") or ia.get("kind")
    )


def _assist_block(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sanitized model-assist provenance (Speed Optimization §2): the machine
    SUGGESTIONS stored next to the human finals so override rate is monitorable.
    Only known keys are carried — a client can't smuggle arbitrary fields onto a
    shipped record through the assist block."""
    assist = payload.get("assist")
    if not assist or not isinstance(assist, dict) or not assist.get("prelabeled"):
        return None
    return {
        "prelabeled": True,
        "suggested_verdict": assist.get("suggested_verdict"),
        "suggested_error_tags": list(assist.get("suggested_error_tags") or []),
        "suggested_rationale": assist.get("suggested_rationale"),
        "suggested_step_labels": list(assist.get("suggested_step_labels") or []),
        "confidence": assist.get("confidence"),
    }


def package_submission(task: Dict[str, Any], submission: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = submission.get("payload") or {}
    verdict = submission.get("verdict") or payload.get("verdict")
    prompt = task.get("prompt", "")
    prov = _provenance(task, submission)
    records: List[Dict[str, Any]] = []

    # Stage-2 independent capture (Eval Flow Upgrade §3 / Speed Optimization §1).
    ia = payload.get("independent_answer") or {}
    ia_text = (ia.get("text") or "").strip()
    portal_version = _portal_version(submission, payload)
    ia_kind = _independent_kind(task, ia, portal_version)
    # instinct (V3, ~10s one-liner) and stance (V2, quick take) are both
    # LIGHTWEIGHT pre-reveal anchoring signals attached to the primary record as
    # context — NOT gold ideal answers. Only ``full`` packages a premium blind
    # ideal SFT record (below). ``ia_kind`` is stamped on the record so a buyer
    # can tell an instinct one-liner from a stance from a full blind answer.
    from asclepius.constants import LIGHTWEIGHT_INDEPENDENT_KINDS

    stance_text = ia_text if (ia_text and ia_kind in LIGHTWEIGHT_INDEPENDENT_KINDS) else None
    assist = _assist_block(payload)

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

        # Structured per-tag reasons (Speed Optimization §6): only reasons for
        # tags actually selected ship (a deselected tag's stale reason is dropped).
        tag_reasons = {
            tag: reason
            for tag, reason in (critique.get("error_tag_reasons") or {}).items()
            if tag in error_tags and (reason or "").strip()
        }

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
            "error_tag_reasons": tag_reasons,
            # Pre-reveal quick stance (Speed Optimization §1): context/anchoring
            # signal only — the gold ideal_answer stays the refined chosen answer.
            "stance": stance_text,
            # Which lightweight pre-reveal capture produced ``stance`` (instinct |
            # stance), so a buyer can segment V3 instinct one-liners from V2 stances.
            "independent_kind": ia_kind if stance_text else None,
            # Model-assist provenance (suggested_* next to the human finals).
            "assist": assist,
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
                # Pre-reveal quick stance rides the primary record (Speed Opt §1).
                "stance": stance_text,
                "independent_kind": ia_kind if stance_text else None,
                "assist": assist,
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

    # Blind independent answer (Eval Flow Upgrade §3): the doctor's FULL ideal
    # answer, written BEFORE the A/B candidates were revealed. Emitted as an
    # ADDITIONAL premium SFT record so one submission can yield preference +
    # revised-ideal + independent-ideal (+ reasoning_trace). Flagged ``independent``
    # so a buyer can isolate uncontaminated gold answers. Only ``kind == "full"``
    # captures qualify (Speed Optimization §1) — a quick stance is an anchoring
    # guard, not a gold answer, and ships as the ``stance`` field above instead.
    if ia_text and ia_kind == "full":
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
