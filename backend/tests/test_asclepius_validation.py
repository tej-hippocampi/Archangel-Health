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
    "prompt_review": {"reviewed": True, "verdict": "valid", "reviewed_at": "2026-06-26T12:00:00"},
    "independent_answer": {"text": "Stabilize the myocardium with IV calcium, shift potassium intracellularly with insulin and dextrose plus a beta-agonist, then remove it via dialysis given the ESRD."},
    "chosen_revision": {"edited": False, "why_better_notes": "safer", "why_better_tags": ["safer"]},
    "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
}


def test_clean_submission_validates():
    task, sub = _task(), _submission(_GOOD_PAYLOAD)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is True
    assert res["issues"] == []


def test_missing_independent_answer_routes_to_qa_not_rejected():
    """Eval Flow Upgrade §3: a non-flagged submission with no blind independent
    answer is flagged for QA — never hard-rejected (no lost submissions)."""
    payload = {k: v for k, v in _GOOD_PAYLOAD.items() if k != "independent_answer"}
    task, sub = _task(), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is False
    assert "missing_independent_answer" in res["issues"]
    # The preference record is still packaged — the attempt is captured, not lost.
    assert any(r["type"] == "preference" for r in recs)


def test_independent_answer_packaged_as_blind_ideal_record():
    """In ``full`` independent mode the blind independent answer becomes its own
    ``ideal_answer`` record tagged ``independent: true`` (premium uncontaminated
    SFT)."""
    task, sub = _task(independent_mode="full"), _submission(_GOOD_PAYLOAD)
    recs = package_submission(task, sub)
    blind = [r for r in recs if r["type"] == "ideal_answer" and r.get("independent")]
    assert len(blind) == 1
    assert blind[0]["ideal_answer"].startswith("Stabilize the myocardium")
    assert blind[0]["prompt_clinician_reviewed"] is True


def test_step_critique_is_phi_scanned():
    """A PHI identifier in a step's free-text critique (Eval Flow Upgrade §4) is
    caught by the defensive scan — not just the step body."""
    payload = {
        "verdict": "both_inadequate", "confidence": "high",
        "independent_answer": {"text": "Give IV calcium to stabilize, then dialyze given the ESRD."},
        "from_scratch": {
            "ideal_answer": "Give IV calcium to stabilize, then dialyze given the ESRD.",
            "reasoning_steps": [
                {"step": 1, "text": "Give IV calcium", "label": "bad", "step_reward": 0,
                 "critique": "per chart, contact jdoe@example.com"},
            ],
        },
    }
    task, sub = _task(capture_reasoning=True), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert any(i.startswith("phi:") for i in res["issues"])
    assert "email" in res["phi_kinds"]


def test_capture_reasoning_pending_step_routes_to_qa():
    """Edit-to-Correct gating: a split step left ``pending`` (neither confirmed nor
    corrected nor added) flags a capture_reasoning submission for QA — silence is
    not endorsement, but we never hard-reject (no lost submissions)."""
    payload = {
        **_GOOD_PAYLOAD,
        "reasoning_steps": [
            {"step": 1, "text": "Give IV calcium", "original_text": "Give IV calcium",
             "corrected": False, "confirmed": True, "added": False, "label": "good"},
            # pending: text === original, never confirmed/corrected/added
            {"step": 2, "text": "Then dialyze", "original_text": "Then dialyze",
             "corrected": False, "confirmed": False, "added": False},
        ],
    }
    task, sub = _task(capture_reasoning=True), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is False
    assert "unreviewed_reasoning_step" in res["issues"]


def test_corrected_step_missing_reason_routes_to_qa():
    """A corrected step with no ``correction_reason`` can't derive a label — flagged
    for QA (and the same gate rejects an unknown reason value)."""
    payload = {
        **_GOOD_PAYLOAD,
        "reasoning_steps": [
            {"step": 1, "text": "Give IV calcium gluconate", "original_text": "give calcium",
             "corrected": True, "confirmed": False, "added": False, "correction_reason": None},
        ],
    }
    task, sub = _task(capture_reasoning=True), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is False
    assert "missing_correction_reason" in res["issues"]


def test_all_steps_confirmed_or_corrected_validates():
    """A capture_reasoning submission with every step resolved (confirmed, corrected
    with a reason, or added) passes the Edit-to-Correct gate."""
    payload = {
        **_GOOD_PAYLOAD,
        "reasoning_steps": [
            {"step": 1, "text": "Give IV calcium", "original_text": "Give IV calcium",
             "confirmed": True, "label": "good"},
            {"step": 2, "text": "Shift K+ with insulin and dextrose", "original_text": "give resin",
             "corrected": True, "correction_reason": "factual_error", "label": "bad"},
            {"step": 3, "text": "Then dialyze given the ESRD", "original_text": None,
             "added": True, "label": "good"},
        ],
    }
    task, sub = _task(capture_reasoning=True), _submission(payload)
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


def test_assist_prelabeled_too_fast_confirm_routes_to_qa(monkeypatch):
    """Speed Optimization §2 time-floor guard: a pre-labeled task confirmed
    implausibly fast smells like rubber-stamping — needs_qa, never a hard
    reject."""
    monkeypatch.setenv("ASCLEPIUS_ASSIST_TIME_FLOOR_SEC", "60")
    payload = {
        **_GOOD_PAYLOAD,
        "assist": {"prelabeled": True, "suggested_verdict": "A_better",
                   "suggested_error_tags": ["dosing_error"], "confidence": 0.9},
    }
    task = _task()
    sub = _submission(payload)
    sub["time_spent_sec"] = 30  # above the base 20s floor, below the assist floor
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is False
    assert "assist_too_fast" in res["issues"]
    assert "too_fast" not in res["issues"]
    # A deliberate confirm above the assist floor passes.
    sub2 = _submission(payload)
    sub2["time_spent_sec"] = 120
    res2 = validate_submission(task, sub2, package_submission(task, sub2))
    assert "assist_too_fast" not in res2["issues"]


def test_unknown_error_tag_reason_routes_to_qa():
    """Feature 6: per-tag reasons come from a controlled vocabulary; an
    off-vocabulary value flags the submission for QA."""
    payload = {
        **_GOOD_PAYLOAD,
        "rejected_critique": {
            **_GOOD_PAYLOAD["rejected_critique"],
            "error_tag_reasons": {"dosing_error": "not_a_real_reason"},
        },
    }
    task, sub = _task(), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is False
    assert "unknown_error_tag_reason" in res["issues"]


def test_known_error_tag_reason_validates():
    payload = {
        **_GOOD_PAYLOAD,
        "rejected_critique": {
            **_GOOD_PAYLOAD["rejected_critique"],
            "error_tag_reasons": {"dosing_error": "dose_too_high"},
        },
    }
    task, sub = _task(), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert res["valid"] is True
    pref = [r for r in recs if r["type"] == "preference"][0]
    assert pref["error_tag_reasons"] == {"dosing_error": "dose_too_high"}


def test_stance_text_is_phi_scanned():
    """The pre-reveal stance is free text — the defensive PHI scan covers it."""
    payload = {
        **_GOOD_PAYLOAD,
        "independent_answer": {"text": "as I told jdoe@example.com, dialyze early", "kind": "stance"},
    }
    task, sub = _task(), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert any(i.startswith("phi:") for i in res["issues"])
    assert "email" in res["phi_kinds"]


def test_pregrade_only_bulk_confirm_still_hits_assist_floor(monkeypatch):
    """The assist floor is derived from the payload (pre-graded steps), not just
    the client's self-declared assist block — a pregrade-only bulk-confirm can't
    slip under the base floor by omitting the block."""
    monkeypatch.setenv("ASCLEPIUS_ASSIST_TIME_FLOOR_SEC", "60")
    payload = {
        **_GOOD_PAYLOAD,
        "reasoning_steps": [
            {"step": 1, "text": "Give IV calcium", "original_text": "Give IV calcium",
             "confirmed": True, "label": "good", "suggested_label": "good"},
        ],
    }
    task = _task(capture_reasoning=True)
    sub = _submission(payload)
    sub["time_spent_sec"] = 30
    res = validate_submission(task, sub, package_submission(task, sub))
    assert "assist_too_fast" in res["issues"]


def test_assist_suggested_rationale_is_phi_scanned():
    """Client-supplied assist text ships on records — the defensive PHI scan
    covers it like every other emitted field."""
    payload = {
        **_GOOD_PAYLOAD,
        "assist": {"prelabeled": True, "suggested_verdict": "A_better",
                   "suggested_error_tags": [],
                   "suggested_rationale": "per chart contact jdoe@example.com"},
    }
    task, sub = _task(), _submission(payload)
    recs = package_submission(task, sub)
    res = validate_submission(task, sub, recs)
    assert "email" in res["phi_kinds"]
