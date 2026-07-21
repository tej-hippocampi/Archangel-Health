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
from asclepius.validation import all_anchors, has_valid_anchor, is_valid_anchor


def _candidate_text(task: Dict[str, Any], cid: Optional[str]) -> str:
    if not cid:
        return ""
    for c in task.get("candidate_answers", []) or []:
        if str(c.get("id")) == str(cid):
            return c.get("text", "") or ""
    return ""


def _context(task: Dict[str, Any]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "specialty": task.get("specialty"),
        "difficulty": task.get("difficulty"),
    }
    # Multimodal (Synthetic Multimodal Cases PRD §5, §8): carry the PUBLIC case
    # (no answer key), the modality, and case_source onto every record so a
    # structured-multimodal buyer gets it while text-only buyers ignore it. Added
    # ONLY for multimodal tasks — text records are byte-identical to before.
    from asclepius.cases import is_multimodal, public_case

    if is_multimodal(task):
        gen = task.get("generation") or {}
        case = task.get("case") or {}
        ctx["modality"] = "multimodal"
        ctx["case"] = public_case(case)
        ctx["case_source"] = case.get("case_source") or gen.get("case_source") or "synthetic"
    return ctx


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
    # §11 (additive): capture provenance — present only when the V3/V4 UI set it,
    # so V1/V2 records stay byte-identical.
    if a.get("entry_method"):
        out["entry_method"] = a.get("entry_method")
    return out


def _anchors(obj: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize EVERY evidence anchor on an object into the canonical shape,
    merging singular + multi-anchor fields (BUG-3b). Drops empties."""
    return [na for na in (_anchor(a) for a in all_anchors(obj)) if na]


def _first_anchor(obj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The back-compat SINGULAR anchor for a record: the first normalized anchor
    (so existing exports/buyer profiles that read ``evidence_anchor`` still work)."""
    anchors = _anchors(obj)
    return anchors[0] if anchors else None


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
        # Two-frontier provenance (PRD §A3 / §E-2): HOW the A/B pair was assembled —
        # ``two_frontier`` (cross-frontier, full value) | ``legacy_fallback``
        # (same-provider fallback, worth less) | ``anthropic_only_v4`` — so a buyer can
        # separate or discount same-model pairs. ``None`` for generated (non-baseline) pairs.
        "ab_source": (task.get("generation") or {}).get("ab_source"),
        "fallback_reason": (task.get("generation") or {}).get("fallback_reason"),
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


# ─── step_note → step_error_tag (Eval UX Overhaul §13) ───────────────────────
# On V3/V4 the physician types a free-text "what's off with this step?" instead
# of picking a tag; the SERVER classifies it onto the existing controlled
# STEP_CORRECTION_REASONS vocabulary. Deterministic keyword rules so it always
# works offline (an LLM refinement can be layered later without changing the
# contract); the default is factual_error — the modal correction in practice.
# Rules are ordered most-specific-first; keywords are conservative on purpose
# (a wrong specific tag is worse than the factual_error default).
_STEP_TAG_RULES: List[tuple] = [
    ("unsafe", (
        "unsafe", "danger", "harmful", "contraindicat", "toxic", "overdose",
        "life-threatening", "fatal", "kill", "unmonitored",
    )),
    ("outdated_guideline", (
        "outdated", "superseded", "old guideline", "older guideline",
        "no longer recommended", "deprecated", "current guideline", "since updated",
    )),
    ("wrong_order", (
        "wrong order", "out of order", "sequence", "sequencing", "premature",
        "too early", "too late", "should come after", "should come before",
        "before confirming", "before checking",
    )),
    ("incomplete", (
        "incomplete", "missing", "omits", "omitted", "fails to mention",
        "doesn't mention", "does not mention", "no mention", "leaves out",
        "doesn't address", "does not address", "partial", "misses the",
    )),
    ("minor_wording", (
        "wording", "phrasing", "typo", "grammar", "stylistic", "minor wording",
        "cosmetic", "reads better",
    )),
]


def derive_step_error_tag(note: str) -> Optional[str]:
    """Classify a free-text step note onto STEP_CORRECTION_REASONS. Empty/blank
    note → None (nothing to classify)."""
    t = (note or "").strip().lower()
    if not t:
        return None
    for tag, keywords in _STEP_TAG_RULES:
        if any(k in t for k in keywords):
            return tag
    return "factual_error"


def apply_step_notes(steps: List[Dict[str, Any]]) -> None:
    """In-place §13 derivation over raw payload steps (run BEFORE validation, so
    a note-only corrected step never trips missing_correction_reason):
      * ``step_error_tag`` is always (re)derived from ``step_note`` — never
        trusted from the wire;
      * a corrected step with a note but no ``correction_reason`` gets the
        derived tag as its reason, and its label/step_reward re-derived, so
        every downstream consumer (validation, packaging, model-failure
        records) sees the exact shape the tag-picker flow produces."""
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        note = (s.get("step_note") or "").strip()
        s["step_error_tag"] = derive_step_error_tag(note)
        if not note:
            continue
        if s.get("corrected") and not (s.get("correction_reason") or "").strip():
            s["correction_reason"] = s["step_error_tag"]
            s["label"] = label_for_correction_reason(s["correction_reason"])
            s["step_reward"] = 1 if s["label"] == "good" else 0
        # The note doubles as the per-step critique when none was typed — it is
        # the same "what's off?" signal (never overwrites an explicit critique).
        if not (s.get("critique") or "").strip():
            s["critique"] = note


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
        # §13 (additive): the physician's verbatim "what's off" + the server-
        # derived classification — included ONLY when a note exists, so V1/V2
        # (and untouched V3 steps) package byte-identically to before.
        note = (s.get("step_note") or "").strip()
        extra = {}
        if note:
            extra["step_note"] = note
            extra["step_error_tag"] = s.get("step_error_tag") or derive_step_error_tag(note)
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
                # Multi-anchor (BUG-3b): the full list + a singular back-compat alias.
                "evidence_anchor": _first_anchor(s),
                "evidence_anchors": _anchors(s),
                **extra,
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
    # A step is grounded if it carries ≥1 valid anchor across singular + plural
    # (BUG-3b). A packaged step already exposes both fields.
    return bool(steps) and all(has_valid_anchor(s) for s in steps)


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

        rationale_anchor = _first_anchor(revision)
        rationale_anchors = _anchors(revision)
        # Per-error-tag evidence anchors (opt §1.2).
        tag_anchors_raw = critique.get("error_tag_anchors") or {}
        error_tag_anchors = {
            tag: _anchor(anc) for tag, anc in tag_anchors_raw.items() if _anchor(anc)
        }
        # Premium-tier grounded flag counts a valid anchor on the rationale (across
        # singular + multi-anchor, BUG-3b) OR on any error tag, so the grounded
        # count is not undercounted (FIX 5).
        grounded = has_valid_anchor(revision) or any(
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
            "evidence_anchors": rationale_anchors,
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
                    "evidence_anchors": rationale_anchors,
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
        rationale_anchor = _first_anchor(fs)
        rationale_anchors = _anchors(fs)
        grounded = has_valid_anchor(fs)
        records.append(
            {
                "type": "ideal_answer",
                "prompt": prompt,
                "ideal_answer": ideal,
                "completion": ideal,  # SFT {prompt, completion} alias
                "messages": _chat(prompt, ideal),
                "approach_notes": approach,
                "evidence_anchor": rationale_anchor,
                "evidence_anchors": rationale_anchors,
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
                "evidence_anchor": _first_anchor(ia),
                "evidence_anchors": _anchors(ia),
                "context": _context(task),
                "confidence": submission.get("confidence"),
                "grounded": has_valid_anchor(ia),
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

    # Rubric record (FEAT-2): a standalone, sellable, HealthBench-shaped scoring
    # function — the weighted +/− criteria the doctor CONFIRMED. Emitted only when
    # the submission carries a non-empty confirmed rubric (nothing auto-applied).
    from asclepius.rubric import (
        failure_coverage, grounding_summary, has_critical_negative, normalize_rubric,
        rubric_completeness, rubric_max_points,
    )

    criteria = normalize_rubric(payload.get("rubric"))
    if criteria:
        axes: Dict[str, int] = {}
        tiers: Dict[str, int] = {}
        for c in criteria:
            axes[c["axis"]] = axes.get(c["axis"], 0) + 1
            tiers[c["tier"]] = tiers.get(c["tier"], 0) + 1
        grounding = grounding_summary(criteria)          # FIX-3
        completeness = rubric_completeness(criteria)     # FIX-4
        coverage = failure_coverage(criteria, task, submission)  # FIX-8 (deterministic half)
        records.append(
            {
                "type": "rubric",
                "prompt": prompt,
                "criteria": criteria,
                "max_points": rubric_max_points(criteria),
                "n_positive": sum(1 for c in criteria if c["points"] > 0),
                "n_negative": sum(1 for c in criteria if c["points"] < 0),
                "axes": axes,
                # Tiered rubric (Two-Model PRD WS-B): tier histogram + critical rollup
                # so a buyer can filter on rubrics that name a critical failure.
                "tiers": tiers,
                "n_critical": tiers.get("critical", 0),
                "has_critical_negative": has_critical_negative(criteria),
                # Rubric Rigor: FIX-1 concreteness, FIX-3 grounding, FIX-4 completeness/
                # premium, FIX-8 failure-surface coverage. The FIX-2 (validity/reliability)
                # and FIX-8 hackability LLM probes are added asynchronously by the pipeline
                # (grader_eval) and degrade to skipped without a key.
                "n_specific": sum(1 for c in criteria if c.get("specific")),
                "grounded": grounding["grounded"],
                "n_grounded_criteria": grounding["n_grounded_criteria"],
                "completeness": completeness,
                "premium": completeness["premium"],
                "uncovered_failure_modes": coverage["uncovered_failure_modes"],
                "context": _context(task),
                "confidence": submission.get("confidence"),
                **prov,
            }
        )

    return records
