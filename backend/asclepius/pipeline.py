"""Submission pipeline orchestration (PRD §5).

capture (router) -> package -> auto-validate -> LLM critic -> QA gate.

Status transitions enforced here:
  submitted -> auto_validated -> qa_checked -> export_ready    (happy path, not sampled)
  submitted -> needs_qa                                        (validation fail / critic flag / sampled)
  needs_qa  -> export_ready | rejected                         (human QA decision, see router)

No record reaches ``export_ready`` without passing auto-validation AND the QA
gate (PRD §5, §12).
"""

from __future__ import annotations

import logging
import os
import random
from typing import Any, Dict, List, Optional

from asclepius.agreement import cohens_kappa, jaccard
from asclepius.critic import run_critic, run_grounding_check
from asclepius.packaging import package_submission
from asclepius.store import AsclepiusStore
from asclepius.validation import validate_submission
from asclepius.value import estimate_value

log = logging.getLogger("asclepius.pipeline")


def qa_sample_pct() -> float:
    try:
        return float(os.getenv("ASCLEPIUS_QA_SAMPLE_PCT", "15"))
    except ValueError:
        return 15.0


def _should_sample() -> bool:
    pct = qa_sample_pct()
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    return random.random() < (pct / 100.0)


def _error_tags(submission: Dict[str, Any]) -> list:
    payload = submission.get("payload") or {}
    return list((payload.get("rejected_critique") or {}).get("error_tags") or [])


def estimate_and_store_value(
    store: AsclepiusStore, task: Dict[str, Any], submission_id: str, *, log_event: bool = True
) -> Dict[str, Any]:
    """Value-per-Minute (PRD Part A): estimate the sellable dollar value of a
    judgment from its packaged records + captured attributes and persist it on
    the submission alongside the clinician-minutes it took. Measurement only — it
    never alters records, status, routing, or any v1/v2 capture behavior, and a
    modeling hiccup must never fail a real submission (no lost work).

    Reads the stored records so it can be re-run when agreement lands (the
    double-labeled + credentialed premium multiplier depends on ``agreement_score``,
    which is only known once a second labeler submits). ``log_event=False`` for
    that re-valuation pass so the ``value_estimated`` event is emitted exactly
    once per submission (the persisted value itself is idempotent). Returns the
    value fields for the submit response, or ``{}`` on any failure."""
    try:
        sub = store.get_submission(submission_id) or {}
        recs = [r.get("payload") or {} for r in store.records_for_submission(submission_id)]
        est = estimate_value(recs, task, sub)
        secs = int(sub.get("time_spent_sec") or 0)
        store.set_submission_value(
            submission_id,
            realized=est["realized_value"],
            projected=est["projected_value"],
            clinician_review_seconds=secs,
        )
        if log_event:
            store.log_event(
                entity_type="submission", entity_id=submission_id, event_type="value_estimated",
                actor=sub.get("evaluator_id"),
                payload={**est, "clinician_review_seconds": secs},
            )
        return {
            "value_estimate_usd": est["realized_value"],
            "value_estimate_projected_usd": est["projected_value"],
        }
    except Exception:  # pragma: no cover - defensive: never break capture on value math
        log.exception("asclepius value estimation failed for %s", submission_id)
        return {}


def compute_and_store_agreement(store: AsclepiusStore, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Inter-annotator agreement on a double-labeled task (opt §1.3).

    Computes Cohen's κ on the verdict and Jaccard on the error-tag sets for the
    first two labels, stores a per-task observation row (folded into the aggregate
    κ surfaced in the quality report), stamps the observed agreement onto every
    submission + its preference records, and flags disagreeing tasks for
    re-review (κ/agreement below the substantial-agreement threshold).

    Returns ``{agree, flagged, kappa, jaccard, n}`` or ``None`` (< 2 labels)."""
    task_id = task["task_id"]
    subs = [s for s in store.submissions_for_task(task_id) if s.get("verdict")]
    if len(subs) < 2:
        return None

    a, b = subs[0], subs[1]
    verdict_a, verdict_b = a.get("verdict"), b.get("verdict")
    tags_a, tags_b = _error_tags(a), _error_tags(b)
    jac = jaccard(tags_a, tags_b)
    pair_kappa = cohens_kappa([(verdict_a, verdict_b)])
    agree = verdict_a == verdict_b
    flagged = not agree  # disagreement -> route for re-review, never silent export

    store.upsert_agreement(
        task_id=task_id,
        specialty=task.get("specialty"),
        sub_a=a["submission_id"],
        sub_b=b["submission_id"],
        verdict_a=verdict_a,
        verdict_b=verdict_b,
        tags_a=tags_a,
        tags_b=tags_b,
        jaccard_tags=jac,
        verdict_agree=agree,
        n_labels=len(subs),
        flagged=flagged,
    )

    # Observed agreement (majority share) stamped on each submission + preference
    # record for buyer-facing per-record agreement signal.
    tally: Dict[str, int] = {}
    for s in subs:
        tally[s["verdict"]] = tally.get(s["verdict"], 0) + 1
    score = round(max(tally.values()) / len(subs), 3)
    for s in subs:
        store.update_submission(s["submission_id"], agreement_score=score)
        for rec in store.records_for_submission(s["submission_id"]):
            if rec["type"] == "preference":
                store.patch_record_payload(rec["record_id"], {"agreement_score": score})
        # Re-value each label now that a κ-reportable agreement exists (Value-per-
        # Minute PRD): double-labeled + credentialed unlocks the premium tier
        # multiplier the earlier labeler's first estimate could not have seen.
        # log_event=False so the current submission (re-valued again right after,
        # in process_submission) logs its ``value_estimated`` event only once.
        estimate_and_store_value(store, task, s["submission_id"], log_event=False)

    # On disagreement, pull back any sibling that already reached export_ready so a
    # low-agreement task is never silently exported (opt §1.3). Not yet-exported.
    if flagged:
        for s in subs:
            if s.get("status") in ("auto_validated", "export_ready"):
                store.update_submission(s["submission_id"], status="needs_qa", qa_reason="low_agreement")
                store.update_records_status_for_submission(s["submission_id"], "needs_qa")
                store.log_event(
                    entity_type="submission", entity_id=s["submission_id"],
                    event_type="routed_to_qa", payload={"reason": "low_agreement"},
                )

    return {"agree": agree, "flagged": flagged, "kappa": pair_kappa, "jaccard": jac, "n": len(subs)}


async def process_submission(
    store: AsclepiusStore, task: Dict[str, Any], submission: Dict[str, Any]
) -> Dict[str, Any]:
    """Run the full pipeline for a freshly-captured submission row. Returns a
    result dict {submission_id, status, issues, record_count, critic, agreement_score}."""
    sid = submission["submission_id"]

    def _progress(phase: str, pct: int, detail: Optional[str] = None) -> None:
        # Real submit progress (BUG-5): stamp the phase the moment it starts, so a
        # polling client shows a truthful phase name — never an invented percentage.
        try:
            store.set_submission_progress(sid, phase=phase, pct=pct, detail=detail)
        except Exception:  # progress is best-effort; never fail capture on it
            pass

    # 1. Package
    _progress("packaging", 20, "Packaging training records")
    packaged = package_submission(task, submission)
    record_ids: List[str] = []
    for rec in packaged:
        rid = store.insert_record(
            submission_id=sid,
            task_id=task["task_id"],
            rtype=rec["type"],
            specialty=task.get("specialty"),
            payload=rec,
            status="submitted",
        )
        record_ids.append(rid)
    store.log_event(
        entity_type="submission",
        entity_id=sid,
        event_type="packaged",
        actor=submission.get("evaluator_id"),
        payload={"record_count": len(record_ids), "types": [r["type"] for r in packaged]},
    )

    # "Did the doctor catch it?" (PRD §16): on a generated task that carries a
    # server-side intended-flawed candidate, record whether the evaluator
    # rejected that exact candidate. Kept internal (never shown to the blinded
    # evaluator); surfaced only in the admin dashboard.
    flawed_id = ((task.get("generation") or {}) or {}).get("intended_flawed_id")
    if flawed_id and submission.get("verdict") in ("A_better", "B_better"):
        caught = 1 if str(submission.get("rejected_id")) == str(flawed_id) else 0
        store.update_submission(sid, caught_flaw=caught)

    # 2. Duplicate check (same dedupe_hash on a different submission)
    # TODO(scale): this full-table scan is fine at pod scale; switch to an indexed
    # dedupe_hash lookup (idx_sub_dedupe already exists) when volume grows.
    is_dup = False
    dh = submission.get("dedupe_hash")
    if dh:
        for other in store.list_submissions(limit=100000):
            if other["submission_id"] != sid and other.get("dedupe_hash") == dh:
                is_dup = True
                break

    # 3. Auto-validate
    _progress("validating", 40, "Validating completeness, PHI, duplicates")
    vres = validate_submission(task, submission, packaged, is_duplicate=is_dup)
    store.update_submission(sid, validation=vres)

    agreement = compute_and_store_agreement(store, task)
    agreement_score = (
        store.get_submission(sid) or {}
    ).get("agreement_score") if agreement else None

    # Value-per-Minute (PRD Part A): estimate AFTER agreement so a double-labeled
    # credentialed judgment picks up its κ-reportable premium. compute_and_store_
    # agreement has already refreshed the sibling's value when it stamped the
    # shared agreement score, so both labels end up consistently valued.
    value_fields = estimate_and_store_value(store, task, sid)

    if not vres["valid"]:
        _progress("needs_qa", 100, "Routed to QA review")
        store.update_submission(sid, status="needs_qa", qa_reason=",".join(vres["issues"]))
        store.update_records_status_for_submission(sid, "needs_qa")
        store.log_event(
            entity_type="submission",
            entity_id=sid,
            event_type="validation_failed",
            actor=submission.get("evaluator_id"),
            payload={"issues": vres["issues"]},
        )
        return {
            "submission_id": sid,
            "status": "needs_qa",
            "issues": vres["issues"],
            "record_count": len(record_ids),
            "critic": None,
            "agreement_score": agreement_score,
            **value_fields,
        }

    store.update_submission(sid, status="auto_validated")
    store.update_records_status_for_submission(sid, "auto_validated")
    store.log_event(
        entity_type="submission", entity_id=sid, event_type="auto_validated", payload={}
    )

    # 4. LLM consistency critic (double-check) + optional evidence-grounding check
    _progress("consistency_check", 60, "LLM consistency critic")
    critic = await run_critic(task, submission)
    _progress("grounding_check", 80, "Evidence grounding check")
    grounding = await run_grounding_check(task, submission)
    store.update_submission(sid, critic={"consistency": critic, "grounding": grounding})

    critic_flagged = critic.get("consistent") is False
    grounding_flagged = grounding.get("grounding_ok") is False
    # Disagreement on a double-labeled task -> re-review, never silent export.
    low_agreement = bool(agreement and agreement.get("flagged"))
    sampled = _should_sample()

    if critic_flagged or grounding_flagged or low_agreement or sampled:
        _progress("qa_routing", 95, "Routing to QA review")
        if critic_flagged:
            reason = "critic_inconsistent"
        elif grounding_flagged:
            reason = "grounding_unsupported"
        elif low_agreement:
            reason = "low_agreement"
        else:
            reason = "sampled_for_qa"
        store.update_submission(sid, status="needs_qa", qa_reason=reason)
        store.update_records_status_for_submission(sid, "needs_qa")
        store.log_event(
            entity_type="submission",
            entity_id=sid,
            event_type="routed_to_qa",
            payload={"reason": reason, "critic": critic, "grounding": grounding},
        )
        return {
            "submission_id": sid,
            "status": "needs_qa",
            "issues": [reason],
            "record_count": len(record_ids),
            "critic": critic,
            "agreement_score": agreement_score,
            **value_fields,
        }

    # 5. Passed validation + critic + grounding, agreed, not sampled -> export-ready.
    _progress("complete", 100, "Complete — export-ready")
    store.update_submission(sid, status="export_ready")
    store.update_records_status_for_submission(sid, "export_ready")
    store.log_event(
        entity_type="submission", entity_id=sid, event_type="qa_checked", payload={"auto": True}
    )
    store.log_event(
        entity_type="submission", entity_id=sid, event_type="export_ready", payload={}
    )
    return {
        "submission_id": sid,
        "status": "export_ready",
        "issues": [],
        "record_count": len(record_ids),
        "critic": critic,
        "agreement_score": agreement_score,
        **value_fields,
    }


def apply_qa_decision(
    store: AsclepiusStore, submission: Dict[str, Any], *, decision: str, reviewer_id: str, notes: Optional[str]
) -> str:
    """Human QA gate decision. Returns the new status."""
    sid = submission["submission_id"]
    qa_block = {"decision": decision, "reviewer_id": reviewer_id, "notes": notes}
    if decision == "approve":
        store.update_submission(sid, status="export_ready", qa=qa_block, qa_reason=None)
        store.update_records_status_for_submission(sid, "export_ready")
        store.log_event(
            entity_type="submission", entity_id=sid, event_type="qa_approved", actor=reviewer_id, payload=qa_block
        )
        store.log_event(entity_type="submission", entity_id=sid, event_type="export_ready", payload={})
        return "export_ready"
    # reject
    store.update_submission(sid, status="rejected", qa=qa_block)
    store.update_records_status_for_submission(sid, "rejected")
    store.log_event(
        entity_type="submission", entity_id=sid, event_type="qa_rejected", actor=reviewer_id, payload=qa_block
    )
    return "rejected"
