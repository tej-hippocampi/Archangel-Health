"""Keep blind capture immutable while retaining a physician's later correction."""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict

from asclepius.schemas import EvidenceAnchor, IndependentAnswerRevision
from asclepius.validation import residual_identifiers


def _content(answer: Dict[str, Any]) -> Dict[str, Any]:
    # Compare meaningful content, not client/server timestamps or capture kind.
    anchors = answer.get("evidence_anchors") or []
    if not anchors and answer.get("evidence_anchor"):
        anchors = [answer["evidence_anchor"]]
    anchors = [EvidenceAnchor.model_validate(a).model_dump() for a in anchors]
    anchors = [{k: v.strip() if isinstance(v, str) else v for k, v in a.items()} for a in anchors]
    anchors = [a for a in anchors if any(a.values())]
    return {"text": (answer.get("text") or "").strip(), "anchors": anchors}


def preserve_blind_answer(store: Any, task_id: str, evaluator_id: str,
                          payload: Dict[str, Any]) -> None:
    """Called before every persistence path, including flags and examinations.

    The submitted independent_answer is the physician's current draft. Derive
    its exposure phase from the server commit, never a client-supplied claim.
    No commit or no difference means no post-reveal correction. Accepted rows
    and independent_commits are never updated by this function.
    """
    payload.pop("independent_answer_revision", None)
    commit = store.get_independent_commit(task_id, evaluator_id)
    if not commit:
        return
    original = commit["payload"]
    latest = payload.get("independent_answer")
    payload["independent_answer"] = deepcopy(original)
    if not isinstance(latest, dict) or _content(latest) == _content(original):
        return
    revision = IndependentAnswerRevision.model_validate({
        "text": latest.get("text") or "",
        "evidence_anchor": latest.get("evidence_anchor"),
        "evidence_anchors": latest.get("evidence_anchors") or [],
        "capture_phase": "after_model_reveal",
        "revised_at": datetime.now(timezone.utc).isoformat(),
    }).model_dump()
    # Late flags bypass the normal validation pipeline. Scan each free-text
    # leaf here as well so corrections cannot introduce unscreened identifiers.
    revision["text"] = scrub_clinical_text(revision["text"])
    revision["evidence_anchor"] = scrub_clinical_text(revision["evidence_anchor"])
    revision["evidence_anchors"] = scrub_clinical_text(revision["evidence_anchors"])
    payload["independent_answer_revision"] = revision


def scrub_clinical_text(value):
    """Redact identifiers in newly supplied clinical prose, preserving structure."""
    if isinstance(value, str):
        return "[redacted: possible identifier detected]" if residual_identifiers(value) else value
    if isinstance(value, list):
        return [scrub_clinical_text(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_clinical_text(v) for k, v in value.items()}
    return value


def screen_flag_work(payload):
    # Late flags carry work from any previous step. Their early-return route
    # does not run the normal clinical validation pipeline.
    for key in ("chosen_revision", "rejected_critique", "from_scratch", "reasoning_steps",
                "rubric", "expected_trajectory", "decisive_action"):
        if key in payload:
            payload[key] = scrub_clinical_text(payload[key])
    review = payload.get("prompt_review") or {}
    if review.get("verdict") in ("flagged", "case_incoherent"):
        review["attest_clinically_valid"] = False
