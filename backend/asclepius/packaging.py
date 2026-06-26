"""Transform a raw expert submission into canonical training records.

One submission can yield multiple records (e.g. a "both inadequate" with
reasoning steps yields an ideal-answer SFT record AND a reasoning trace).

Canonical record shapes follow the PRD §6.3. The export layer
(export.py) maps these canonical fields onto whatever a specific buyer needs.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional


def _text_of(task: dict[str, Any], answer_id: Optional[str]) -> str:
    if not answer_id:
        return ""
    for cand in task.get("candidate_answers", []):
        if cand.get("id") == answer_id:
            return cand.get("text", "")
    return ""


def _is_grounded(submission: dict[str, Any]) -> bool:
    """A record is 'grounded' when at least one evidence anchor is present."""
    rev = submission.get("chosen_revision") or {}
    crit = submission.get("rejected_critique") or {}
    fs = submission.get("from_scratch") or {}
    if rev.get("evidence"):
        return True
    if crit.get("evidence"):
        return True
    for step in (fs.get("reasoning_steps") or []) + (submission.get("reasoning_steps") or []):
        if isinstance(step, dict) and step.get("evidence"):
            return True
    return False


def dedupe_hash(prompt: str, *texts: str) -> str:
    norm = "".join((prompt or "").strip().lower().split())
    for t in texts:
        norm += "" + " ".join((t or "").strip().lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def package_submission(submission: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of canonical training records for one submission."""
    records: list[dict[str, Any]] = []
    sid = submission.get("submission_id")
    prompt = task.get("prompt", "")
    ctx = {"specialty": task.get("specialty"), "difficulty": task.get("difficulty")}
    cred = (submission.get("annotator") or {}).get("credentials")
    grounded = _is_grounded(submission)
    verdict = submission.get("verdict")

    rev = submission.get("chosen_revision") or {}
    crit = submission.get("rejected_critique") or {}

    # Preference pair — when A or B was chosen.
    if verdict in ("A_better", "B_better"):
        chosen_original = _text_of(task, submission.get("chosen_id"))
        chosen_text = rev.get("revised_text") or chosen_original
        rejected_text = _text_of(task, submission.get("rejected_id"))
        records.append({
            "type": "preference",
            "prompt": prompt,
            "chosen": chosen_text,
            "rejected": rejected_text,
            "context": ctx,
            "rationale": rev.get("why_better_notes"),
            "why_better_tags": rev.get("why_better_tags", []),
            "error_tags_on_rejected": crit.get("error_tags", []),
            "annotator_credential": cred,
            "confidence": submission.get("confidence"),
            "agreement_score": submission.get("agreement_score"),
            "grounded": grounded,
            "evidence": rev.get("evidence", []),
            "submission_id": sid,
        })
        # If the chosen answer was revised, that revision is also a clean SFT target.
        if rev.get("edited") and rev.get("revised_text"):
            records.append({
                "type": "ideal_answer",
                "prompt": prompt,
                "ideal_answer": rev.get("revised_text"),
                "approach_notes": rev.get("why_better_notes"),
                "context": ctx,
                "annotator_credential": cred,
                "grounded": grounded,
                "evidence": rev.get("evidence", []),
                "submission_id": sid,
            })

    # From-scratch ideal answer + reasoning — when both were inadequate.
    fs = submission.get("from_scratch")
    if verdict == "both_inadequate" and fs:
        records.append({
            "type": "ideal_answer",
            "prompt": prompt,
            "ideal_answer": fs.get("ideal_answer"),
            "approach_notes": fs.get("approach_notes"),
            "context": ctx,
            "annotator_credential": cred,
            "grounded": grounded,
            "evidence": [s.get("evidence") for s in (fs.get("reasoning_steps") or []) if isinstance(s, dict) and s.get("evidence")],
            "submission_id": sid,
        })
        steps = fs.get("reasoning_steps") or []
        if steps:
            records.append({
                "type": "reasoning_trace",
                "prompt": prompt,
                "steps": steps,
                "final_answer": fs.get("ideal_answer"),
                "context": ctx,
                "annotator_credential": cred,
                "grounded": grounded,
                "submission_id": sid,
            })

    # Standalone reasoning trace captured on an A/B task (capture_reasoning).
    top_steps = submission.get("reasoning_steps") or []
    if top_steps and verdict in ("A_better", "B_better"):
        records.append({
            "type": "reasoning_trace",
            "prompt": prompt,
            "steps": top_steps,
            "final_answer": rev.get("revised_text") or _text_of(task, submission.get("chosen_id")),
            "context": ctx,
            "annotator_credential": cred,
            "grounded": grounded,
            "submission_id": sid,
        })

    return records
