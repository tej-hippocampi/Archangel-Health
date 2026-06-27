"""Validation tests: contamination, dedup, grounding, attestation (opt §1.5, §4.12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.packaging import package_submission  # noqa: E402
from asclepius.validation import (  # noqa: E402
    compute_dedupe_hash,
    contamination_hits,
    grounding_status,
    is_valid_anchor,
    validate_submission,
)


def _task(**kw):
    base = {
        "task_id": "t1", "specialty": "nephrology", "difficulty": "hard",
        "capture_reasoning": False, "source": "lab_supplied", "buyer_request_id": None,
        "grounding_mode": "optional",
        "prompt": "Patient with K+ 6.4 — how do you manage?",
        "candidate_answers": [{"id": "A", "text": "Give calcium then dialyze."}, {"id": "B", "text": "Set dialysate K+ 1.0."}],
    }
    base.update(kw)
    return base


def _submission(payload):
    return {
        "submission_id": "s1", "task_id": "t1", "verdict": payload.get("verdict"),
        "chosen_id": payload.get("chosen_id"), "rejected_id": payload.get("rejected_id"),
        "confidence": "high", "time_spent_sec": 120, "agreement_score": None,
        "created_at": "2026-06-26T12:00:00",
        "annotator": {"id_hashed": "h", "credentials": "board_certified_nephrology", "specialty": "nephrology", "years_experience": 10},
        "payload": payload,
    }


_GOOD_PAYLOAD = {
    "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "confidence": "high",
    "chosen_revision": {"edited": False, "why_better_notes": "safer", "why_better_tags": ["safer"]},
    "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
}


def test_clean_submission_validates():
    task, sub = _task(), _submission(_GOOD_PAYLOAD)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is True
    assert res["issues"] == []


def test_contamination_flags_public_benchmark_prompt():
    assert contamination_hits("This is from MedQA dataset, which of the following is the most likely diagnosis?")
    task = _task(prompt="Which of the following is the most likely diagnosis for this MedQA item?")
    sub = _submission(_GOOD_PAYLOAD)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is False
    assert any(i.startswith("contamination") for i in res["issues"])
    assert "MedQA" in res["contamination"]


def test_dedupe_hash_stable_and_distinct():
    task = _task()
    h1 = compute_dedupe_hash(task, _GOOD_PAYLOAD)
    h2 = compute_dedupe_hash(task, _GOOD_PAYLOAD)
    assert h1 == h2
    h3 = compute_dedupe_hash(_task(prompt="different prompt"), _GOOD_PAYLOAD)
    assert h1 != h3


def test_duplicate_flag():
    task, sub = _task(), _submission(_GOOD_PAYLOAD)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs, is_duplicate=True)
    assert "duplicate" in res["issues"]


def test_too_fast_flag():
    task = _task()
    sub = _submission(_GOOD_PAYLOAD)
    sub["time_spent_sec"] = 2
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert "too_fast" in res["issues"]


def test_anchor_validity():
    assert is_valid_anchor({"citation_text": "KDIGO 2024", "source_type": "guideline", "identifier": "KDIGO-2024"})
    assert not is_valid_anchor({"citation_text": "KDIGO", "source_type": "bogus", "identifier": "x"})
    assert not is_valid_anchor({"citation_text": "", "source_type": "guideline", "identifier": "x"})
    assert not is_valid_anchor(None)


def test_grounding_required_status():
    anchor = {"citation_text": "KDIGO 2024", "source_type": "guideline", "identifier": "KDIGO-2024-3.2"}
    # missing anchor -> not satisfied
    ok, reasons = grounding_status(_task(grounding_mode="required"), _GOOD_PAYLOAD)
    assert ok is False and "missing_rationale_anchor" in reasons
    # with anchor -> satisfied
    payload = {**_GOOD_PAYLOAD, "chosen_revision": {**_GOOD_PAYLOAD["chosen_revision"], "evidence_anchor": anchor}}
    ok2, _ = grounding_status(_task(grounding_mode="required"), payload)
    assert ok2 is True


def test_grounding_required_reasoning_each_step():
    anchor = {"citation_text": "KDIGO 2024", "source_type": "guideline", "identifier": "KDIGO-2024"}
    payload = {
        "verdict": "both_inadequate", "confidence": "high",
        "from_scratch": {
            "ideal_answer": "Calcium then dialyze.", "approach_notes": "ecg first", "evidence_anchor": anchor,
            "reasoning_steps": [
                {"step": 1, "text": "ECG", "label": "good", "evidence_anchor": anchor},
                {"step": 2, "text": "dialyze", "label": "good"},  # missing anchor
            ],
        },
    }
    task = _task(grounding_mode="required", capture_reasoning=True)
    ok, reasons = grounding_status(task, payload)
    assert ok is False and "missing_step_anchor" in reasons


def test_missing_rights_attestation_detected():
    task, sub = _task(), _submission(_GOOD_PAYLOAD)
    recs = package_submission(task, sub)
    # strip the attestation from one record to simulate a bug upstream
    recs[0].pop("license")
    res = validate_submission(task, sub, recs)
    assert "missing_license" in res["issues"]
