"""Sample data so the Asclepius tab is testable end-to-end without a live
evaluator flow yet. Synthetic prompts only — no PHI. Triggered from the admin
"Load sample data" button.
"""

from __future__ import annotations

from typing import Any

from .store import AsclepiusStore

_SAMPLES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (
        {
            "task_id": "t-neph-00231",
            "specialty": "nephrology",
            "difficulty": "hard",
            "capture_reasoning": False,
            "source": "internal_prompt_bank",
            "prompt": "72yo on hemodialysis, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
            "candidate_answers": [
                {"id": "A", "generator_model": "model_x", "text": "Start IV calcium gluconate for membrane stabilization, give insulin with dextrose, and dialyze against a 2.0 mEq/L potassium bath. Recheck K+ and ECG after the run."},
                {"id": "B", "generator_model": "model_y", "text": "Dialyze immediately against a 1.0 mEq/L potassium bath to drop potassium fast."},
            ],
        },
        {
            "submission_id": "s-00231-7c2a",
            "task_id": "t-neph-00231",
            "verdict": "A_better",
            "chosen_id": "A",
            "rejected_id": "B",
            "chosen_revision": {
                "edited": True,
                "revised_text": "First stabilize the myocardium with IV calcium gluconate, then shift potassium with insulin+dextrose, then dialyze against a 2.0 mEq/L bath (not lower). Recheck K+ and ECG mid- and post-run.",
                "why_better_tags": ["safer", "better_dosing"],
                "why_better_notes": "A sequences cardiac stabilization first; B's 1.0 mEq/L bath over-lowers and risks arrhythmia.",
                "evidence": [{"citation_text": "KDIGO/expert consensus on hyperkalemia in HD", "source_type": "guideline", "identifier": "KDIGO 2024"}],
            },
            "rejected_critique": {
                "error_tags": ["dosing_error", "unsafe_recommendation"],
                "why_worse": "1.0 mEq/L dialysate is too aggressive; rapid shift risks arrhythmia.",
            },
            "from_scratch": None,
            "reasoning_steps": [],
            "confidence": "high",
            "annotator": {"id_hashed": "a91f", "credentials": "board_certified_nephrology", "years_experience": 12},
            "time_spent_sec": 142,
            "status": "export_ready",
        },
    ),
    (
        {
            "task_id": "t-neph-00232",
            "specialty": "nephrology",
            "difficulty": "medium",
            "capture_reasoning": True,
            "source": "internal_prompt_bank",
            "prompt": "55yo CKD stage 4, eGFR 22, new metformin prescription for T2DM. Appropriate?",
            "candidate_answers": [
                {"id": "A", "generator_model": "model_x", "text": "Metformin is fine at any eGFR; continue full dose."},
                {"id": "B", "generator_model": "model_y", "text": "Avoid starting metformin below eGFR 30; consider alternative agents."},
            ],
        },
        {
            "submission_id": "s-00232-3b1d",
            "task_id": "t-neph-00232",
            "verdict": "both_inadequate",
            "chosen_id": None,
            "rejected_id": None,
            "chosen_revision": {},
            "rejected_critique": {},
            "from_scratch": {
                "ideal_answer": "At eGFR 22 (CKD4), do not initiate metformin — guidance contraindicates starting below eGFR 30 due to lactic acidosis risk. Prefer an SGLT2 inhibitor (renal/CV benefit) or a GLP-1 RA, with dose review.",
                "approach_notes": "Confirm eGFR trend, then apply the eGFR 30 initiation threshold, then pick a renoprotective alternative.",
                "reasoning_steps": [
                    {"step": 1, "text": "Confirm eGFR is stable at ~22 (CKD stage 4).", "evidence": {"citation_text": "metformin labeling eGFR threshold", "source_type": "guideline", "identifier": "FDA label"}},
                    {"step": 2, "text": "Do not initiate metformin below eGFR 30 (lactic acidosis risk)."},
                    {"step": 3, "text": "Choose SGLT2i or GLP-1 RA for renoprotection; adjust dose to renal function."},
                ],
            },
            "reasoning_steps": [],
            "confidence": "high",
            "annotator": {"id_hashed": "a91f", "credentials": "board_certified_nephrology", "years_experience": 12},
            "time_spent_sec": 205,
            "status": "export_ready",
        },
    ),
]


def seed_samples(store: AsclepiusStore) -> dict[str, int]:
    created = 0
    for task, submission in _SAMPLES:
        store.ingest_submission(submission, task)
        created += 1
    return {"submissions_seeded": created}
