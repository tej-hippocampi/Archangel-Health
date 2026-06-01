"""Seeded teach-back grading cases for recall/false-fail checks."""

from __future__ import annotations

SEED_ANSWERS = [
    {
        "case_id": "red_flag_fail_1",
        "question": {"id": "q1", "severity": "CRITICAL", "domain": "RED_FLAG", "expected": "If chest pain or trouble breathing, call 911 now."},
        "answer": "I should rest at home and wait.",
        "expect_status": "FAIL",
        "defect_type": "critical_red_flag_fail",
    },
    {
        "case_id": "red_flag_fail_2",
        "question": {"id": "q2", "severity": "CRITICAL", "domain": "RED_FLAG", "expected": "Go to ER for fever above 100.4 with wound drainage."},
        "answer": "I can call next week if it gets worse.",
        "expect_status": "FAIL",
        "defect_type": "critical_red_flag_fail",
    },
    {
        "case_id": "med_fail_1",
        "question": {"id": "q3", "severity": "CRITICAL", "domain": "MED_HOLD", "expected": "Stop warfarin 5 days before surgery."},
        "answer": "Continue warfarin until surgery day.",
        "expect_status": "FAIL",
        "defect_type": "critical_med_fail",
    },
    {
        "case_id": "med_fail_2",
        "question": {"id": "q4", "severity": "CRITICAL", "domain": "MED", "expected": "Take aspirin daily and do not stop without cardiology advice."},
        "answer": "I can stop aspirin whenever I feel better.",
        "expect_status": "FAIL",
        "defect_type": "critical_med_fail",
    },
    {
        "case_id": "fasting_partial",
        "question": {"id": "q5", "severity": "CRITICAL", "domain": "FASTING", "expected": "Nothing to eat after midnight and clear liquids until 4 hours before surgery."},
        "answer": "No food after midnight.",
        "expect_status": "PARTIAL",
        "defect_type": "partial",
    },
    {
        "case_id": "followup_pass",
        "question": {"id": "q6", "severity": "MAJOR", "domain": "FOLLOWUP", "expected": "Follow up with Dr. Kim on March 22."},
        "answer": "I need to follow up with Dr. Kim on March 22.",
        "expect_status": "PASS",
        "defect_type": "none",
    },
    {
        "case_id": "open_book_pass",
        "question": {"id": "q7", "severity": "MAJOR", "domain": "WOUND_CARE", "expected": "Keep incision dry for 5 days and call if opening or foul drainage."},
        "answer": "Keep incision dry for 5 days and call if opening or foul drainage.",
        "expect_status": "PASS",
        "defect_type": "none",
    },
    {
        "case_id": "nonsure_partial",
        "question": {"id": "q8", "severity": "MAJOR", "domain": "MAIN_PROBLEM", "expected": "You had severe osteoarthritis of the right knee."},
        "answer": "I'm not sure",
        "expect_status": "PARTIAL",
        "defect_type": "non_answer",
    },
]
