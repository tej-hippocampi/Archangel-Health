"""Seeded clean pairs and razor-blade mutations for grounding inspector validation."""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Sample structured data (from internal fixtures, extended) ─────────────────

PREOP_CASES: List[Tuple[dict, str]] = []

_appendix_sd = {
    "patient_name": "Marcus Webb",
    "procedure_name": "Laparoscopic Appendectomy",
    "procedure_date": "2025-03-18",
    "procedure_status": "scheduled",
    "key_diagnoses": ["Acute Appendicitis"],
    "medications": [
        {"name": "Metformin", "dose": "500mg", "frequency": "twice daily", "route": "oral", "status": "hold", "notes": "Stop 48 hours before surgery"},
        {"name": "Lisinopril", "dose": "10mg", "frequency": "daily", "route": "oral", "status": "continue", "notes": "Take morning of surgery with sip of water"},
        {"name": "Warfarin", "dose": "5mg", "frequency": "daily", "route": "oral", "status": "stop", "notes": "Stop 5 days before surgery"},
    ],
    "red_flags": [
        "Worsening abdominal pain — call ER",
        "Fever above 100.4°F before surgery — call us",
    ],
    "pre_op_instructions": "Arrive 2 hours before scheduled time. Shower with antibacterial soap night before and morning of surgery.",
    "diet_instructions": "Nothing to eat after midnight. Clear liquids until 4 hours before surgery. No solid food for 8 hours.",
    "activity_restrictions": "Arrange a driver — you cannot drive yourself home.",
    "allergies": ["Penicillin", "NSAIDs"],
    "follow_up": {"date": "2025-03-25", "provider": "Dr. Nguyen, General Surgery"},
}
_appendix_script = """
Hello Marcus. Before your laparoscopic appendectomy on March eighteenth, here is what you need to know.
Stop your Metformin forty-eight hours before surgery. Continue your Lisinopril the morning of surgery with a sip of water.
Stop your Warfarin five days before surgery.
Do not eat anything after midnight. Clear liquids are allowed until four hours before surgery — no solid food for eight hours.
If you have worsening abdominal pain, call the ER. If you develop a fever above one hundred point four degrees Fahrenheit before surgery, call us.
You are allergic to Penicillin and NSAIDs — do not take those.
Arrange a driver because you cannot drive yourself home.
Arrive two hours before your scheduled surgery time on March eighteenth.
Your follow-up is with Dr. Nguyen on March twenty-fifth.
""".strip()

PREOP_CASES.append((_appendix_sd, _appendix_script))

_hernia_sd = {
    "patient_name": "Elena Ruiz",
    "procedure_name": "Inguinal Hernia Repair",
    "procedure_date": "2025-04-02",
    "medications": [
        {"name": "Empagliflozin", "dose": "10mg", "frequency": "daily", "status": "stop", "notes": "Stop 3 days before surgery"},
        {"name": "Aspirin", "dose": "81mg", "frequency": "daily", "status": "continue", "notes": ""},
    ],
    "red_flags": ["Sudden severe groin pain"],
    "pre_op_instructions": "Check in at 6 AM. Bring insurance card.",
    "diet_instructions": "No solid food after midnight. NPO after midnight.",
    "activity_restrictions": "No heavy lifting over 10 pounds before surgery day.",
    "allergies": ["Latex"],
    "follow_up": {"date": "2025-04-09", "provider": "Dr. Okafor"},
}
_hernia_script = """
Elena, your inguinal hernia repair is April second. Stop Empagliflozin three days before surgery.
Continue your Aspirin eighty-one milligrams daily.
Nothing to eat after midnight — NPO after midnight.
If you have sudden severe groin pain, call us.
You have a Latex allergy.
Do not lift anything heavier than ten pounds before surgery.
Check in at six AM on April second and bring your insurance card.
Follow up with Dr. Okafor on April ninth.
""".strip()
PREOP_CASES.append((_hernia_sd, _hernia_script))

# Generate additional pre-op variants
for i in range(8):
    sd = copy.deepcopy(_appendix_sd)
    sd["patient_name"] = f"PreOp Patient {i + 3}"
    script = _appendix_script.replace("Marcus", sd["patient_name"].split()[0])
    PREOP_CASES.append((sd, script))

DIAGNOSIS_CASES: List[Tuple[dict, str]] = []

_cardiac_dx_sd = {
    "patient_name": "James Harrington",
    "procedure_name": "Cardiac Catheterization with Stent",
    "procedure_date": "2025-03-10",
    "key_diagnoses": ["Coronary Artery Disease", "Single Vessel Disease — LAD"],
    "post_op_instructions": "Keep access site dry. Cardiology will monitor stent.",
    "follow_up": {"date": "2025-03-17", "provider": "Dr. Patel, Cardiology"},
}
_cardiac_dx_script = """
James, you had a cardiac catheterization with stent placement. You have coronary artery disease,
specifically single vessel disease of the LAD artery. Keep your access site dry for forty-eight hours.
Cardiology will monitor your stent. Your follow-up with Dr. Patel is March seventeenth.
""".strip()
DIAGNOSIS_CASES.append((_cardiac_dx_sd, _cardiac_dx_script))

_knee_dx_sd = {
    "patient_name": "Sandra Okafor",
    "procedure_name": "Right Total Knee Arthroplasty",
    "key_diagnoses": ["Severe Osteoarthritis — Right Knee"],
    "post_op_instructions": "Begin physical therapy within one week.",
    "follow_up": {"date": "2025-03-22", "provider": "Dr. Kim"},
}
_knee_dx_script = """
Sandra, you underwent a right total knee replacement. You have severe osteoarthritis of the right knee.
Begin physical therapy within one week. Follow up with Dr. Kim on March twenty-second.
""".strip()
DIAGNOSIS_CASES.append((_knee_dx_sd, _knee_dx_script))

for i in range(8):
    sd = copy.deepcopy(_cardiac_dx_sd)
    sd["patient_name"] = f"Dx Patient {i + 3}"
    script = _cardiac_dx_script.replace("James", sd["patient_name"].split()[0])
    DIAGNOSIS_CASES.append((sd, script))

TREATMENT_CASES: List[Tuple[dict, str]] = []

_cardiac_tx_sd = {
    "patient_name": "James Harrington",
    "procedure_name": "Cardiac Catheterization",
    "medications": [
        {"name": "Aspirin", "dose": "81mg", "frequency": "daily", "status": "new", "notes": "Do not stop without calling cardiologist"},
        {"name": "Clopidogrel", "dose": "75mg", "frequency": "daily", "status": "new", "notes": "Critical — dual antiplatelet therapy"},
        {"name": "Oxycodone", "dose": "5mg", "frequency": "every 6 hours as needed", "status": "new", "notes": "Do not drive while taking this"},
    ],
    "red_flags": [
        "Chest pain — call 911",
        "Fever above 100.4°F — call us immediately",
        "If your calf becomes swollen, red, and painful — call us or go to the ER now",
    ],
    "activity_restrictions": "No lifting anything heavier than 10 pounds for 5 days",
    "wound_care": "Keep bandage on for 24 hours",
    "diet_instructions": "Low sodium, heart-healthy diet",
    "follow_up": {"date": "2025-03-17", "provider": "Dr. Patel, Cardiology"},
}
_cardiac_tx_script = """
James, take Aspirin eighty-one milligrams daily and Clopidogrel seventy-five milligrams daily — critical dual antiplatelet therapy; do not stop Aspirin without calling your cardiologist.
For pain, Oxycodone five milligrams every six hours as needed — do not drive while taking this medication.
Do not lift anything heavier than ten pounds for five days.
Keep your bandage on for twenty-four hours. Eat a low sodium heart-healthy diet.
If you have chest pain, call nine-one-one. If your temperature goes above one hundred point four degrees Fahrenheit, call us immediately.
If your calf becomes swollen, red, and painful, call us or go to the ER now.
Follow up with Dr. Patel on March seventeenth.
Do not take ibuprofen — you are on blood thinners.
""".strip()
TREATMENT_CASES.append((_cardiac_tx_sd, _cardiac_tx_script))

_ortho_tx_sd = {
    "patient_name": "Sandra Okafor",
    "medications": [
        {"name": "Oxycodone", "dose": "5mg", "frequency": "every 6 hours as needed", "status": "new", "notes": "Max 4 tablets daily"},
        {"name": "Acetaminophen", "dose": "1000mg", "frequency": "every 6 hours", "status": "new", "notes": "No more than 3,000 mg in 24 hours"},
    ],
    "red_flags": ["Fever above 100.4°F — call surgeon", "Wound opening — go to ER"],
    "activity_restrictions": "No driving until cleared. No kneeling.",
    "wound_care": "Keep incision dry for 5 days",
    "diet_instructions": "High protein diet",
    "follow_up": {"date": "2025-03-22", "provider": "Dr. Kim, Orthopedic Surgery"},
}
_ortho_tx_script = """
Sandra, take Oxycodone five milligrams every six hours as needed — maximum four tablets daily.
Take Acetaminophen one thousand milligrams every six hours — no more than three thousand milligrams in twenty-four hours.
No driving until cleared. No kneeling. Keep your incision dry for five days. Eat a high protein diet.
If your temperature goes above one hundred point four degrees Fahrenheit, call your surgeon.
If your wound opens, go to the ER.
Follow up with Dr. Kim on March twenty-second for staple removal.
""".strip()
TREATMENT_CASES.append((_ortho_tx_sd, _ortho_tx_script))

for i in range(8):
    sd = copy.deepcopy(_cardiac_tx_sd)
    sd["patient_name"] = f"Tx Patient {i + 3}"
    script = _cardiac_tx_script.replace("James", sd["patient_name"].split()[0])
    TREATMENT_CASES.append((sd, script))


# ── Mutation library ─────────────────────────────────────────────────────────

def mut_fever_cutoff_drift(script: str) -> str:
    # 100.4°F -> 101.4°F
    return re.sub(r"100\.4|one hundred point four", "101.4", script, flags=re.I)


def mut_fever_unit_or_far_drift(script: str) -> str:
    return re.sub(r"100\.4|one hundred point four", "104", script, flags=re.I)


def mut_acetaminophen_ceiling_drift(script: str) -> str:
    return script.replace("3,000", "6,000").replace("three thousand", "six thousand")


def mut_acetaminophen_frequency_drift(script: str) -> str:
    return script.replace("every 6 hours", "every 4 hours").replace("every six hours", "every four hours")


def mut_opioid_extra_zero(script: str, med: dict) -> str:
    dose = med.get("dose", "5mg")
    return script.replace(dose, dose.replace("5", "50", 1)).replace("five milligrams", "fifty milligrams")


def mut_lifting_limit_drift(script: str) -> str:
    return script.replace("10 pounds", "50 pounds").replace("ten pounds", "fifty pounds")


def mut_anticoag_stop_timing_drift(script: str, med: dict) -> str:
    return script.replace("five days", "two days").replace("5 days", "2 days")


def mut_anticoag_restart_drift(script: str, med: dict) -> str:
    return script + " Restart warfarin 24 days after surgery."


def mut_sglt2_stop_window_drift(script: str, med: dict) -> str:
    return script.replace("three days", "the morning of surgery").replace("3 days", "the morning of surgery")


def mut_npo_window_drift(script: str) -> str:
    return script.replace("eight hours", "two hours").replace("8 hours", "2 hours")


def mut_med_direction_reversal(script: str, med: dict) -> str:
    name = med.get("name", "")
    if (med.get("status") or "").lower() in ("stop", "hold"):
        return re.sub(rf"Stop your {re.escape(name)}", f"Continue your {name}", script, flags=re.I)
    return script.replace("Continue your", "Stop your", 1)


def mut_driving_on_opioids_reversal(script: str) -> str:
    return script.replace("do not drive while taking", "you may drive while taking")


def mut_nsaid_contraindication_reversal(script: str) -> str:
    return script.replace("Do not take ibuprofen", "You can take ibuprofen for pain")


def mut_insert_wrong_doctor(script: str) -> str:
    return re.sub(r"Dr\. \w+(?:,\s*\w+)?", "Dr. Smith", script)


def mut_followup_date_drift(script: str) -> str:
    return re.sub(r"March (seventeenth|twenty-second|twenty-fifth|ninth)", "in two weeks", script, flags=re.I)


def mut_insert_phone_or_stat(script: str) -> str:
    return script + " Call us anytime at 555-0199. Ninety-nine percent of patients have no complications."


def mut_lookalike_med_swap(script: str, med: dict) -> str:
    swaps = {
        "Metformin": "Metronidazole",
        "Hydrocodone": "Hydralazine",
        "Clonidine": "Clonazepam",
    }
    name = med.get("name", "")
    if name in swaps:
        return script.replace(name, swaps[name])
    return script.replace("Clopidogrel", "Celebrex")


def mut_red_flag_strip_action(script: str, flag: str) -> str:
    return script.replace("call us or go to the ER now", "be aware of this").replace("call us immediately", "")


def mut_drop_dose_keep_name(script: str, med: dict) -> str:
    name = med.get("name", "")
    dose = med.get("dose", "")
    if dose:
        return script.replace(dose, "").replace(" milligrams", "")
    return script


def mut_drop_new_med(script: str, med: dict) -> str:
    name = med.get("name", "")
    lines = [ln for ln in script.split("\n") if name.lower() not in ln.lower()]
    return "\n".join(lines)


def mut_drop_red_flag(script: str, flag: str) -> str:
    snippet = flag.split("—")[0].split("-")[0].strip()[:20]
    return re.sub(rf".*{re.escape(snippet)}.*\n?", "", script, flags=re.I)


def mut_clean(script: str) -> str:
    return script


MUTATIONS: List[Tuple[str, Callable[..., str], str, str]] = [
    ("threshold_drift", mut_fever_cutoff_drift, "BLOCK", "threshold_drift"),
    ("threshold_drift", mut_fever_unit_or_far_drift, "BLOCK", "threshold_drift"),
    ("dose_mismatch", mut_acetaminophen_ceiling_drift, "BLOCK", "dose_mismatch"),
    ("frequency_mismatch", mut_acetaminophen_frequency_drift, "BLOCK", "dose_mismatch"),
    ("restriction_drift", mut_lifting_limit_drift, "REVIEW", "restriction_drift"),
    ("threshold_drift", mut_npo_window_drift, "BLOCK", "threshold_drift"),
    ("direction_reversal", mut_driving_on_opioids_reversal, "BLOCK", "direction_reversal"),
    ("direction_reversal", mut_nsaid_contraindication_reversal, "BLOCK", "allergy_violation"),
    ("fabricated_doctor", mut_insert_wrong_doctor, "BLOCK", "fabricated_doctor"),
    ("fabricated_date", mut_followup_date_drift, "BLOCK", "fabricated_date"),
    ("fabrication", mut_insert_phone_or_stat, "REVIEW", "fabrication"),
    ("critical_partial", mut_red_flag_strip_action, "BLOCK", "critical_partial"),
    ("none", mut_clean, "PASS", "none"),
]


def _applies(mutation_name: str, sd: dict, script: str) -> bool:
    if mutation_name == "threshold_drift" and "100.4" not in script and "one hundred point four" not in script.lower():
        if mutation_name == mut_fever_cutoff_drift.__name__:
            return False
    if "acetaminophen" in mutation_name.lower() or mutation_name in ("dose_mismatch", "frequency_mismatch"):
        if "Acetaminophen" not in str(sd.get("medications")) and "acetaminophen" not in script.lower():
            return False
    if mutation_name == "restriction_drift" and "10 pounds" not in script and "ten pounds" not in script.lower():
        return False
    if mutation_name == mut_npo_window_drift.__name__ or "npo" in mutation_name:
        pass
    return True


def build_all_cases() -> List[Dict[str, Any]]:
    """Expand clean pairs × applicable mutations into labeled cases."""
    cases: List[Dict[str, Any]] = []
    track_pairs = [
        ("pre_op", PREOP_CASES),
        ("post_op_diagnosis", DIAGNOSIS_CASES),
        ("post_op_treatment", TREATMENT_CASES),
    ]

    for track, pairs in track_pairs:
        for idx, (sd, good_script) in enumerate(pairs):
            # Clean control
            cases.append({
                "case_id": f"{track}_clean_{idx}",
                "track": track,
                "structured_data": copy.deepcopy(sd),
                "script": good_script,
                "expect_verdict": "PASS",
                "expect_defect_type": "none",
                "expect_item_id": None,
                "clinical_rationale": "Clean script — no planted defect",
            })

            # Static mutations
            for mut_key, mut_fn, expect_verdict, defect_type in MUTATIONS:
                if mut_key == "none":
                    continue
                try:
                    if mut_key == "threshold_drift":
                        mutated = mut_fever_cutoff_drift(good_script)
                        if mutated == good_script:
                            continue
                    elif mut_key == "dose_mismatch" and "Acetaminophen" in good_script:
                        mutated = mut_acetaminophen_ceiling_drift(good_script)
                    elif mut_key == "frequency_mismatch" and "Acetaminophen" in good_script:
                        mutated = mut_acetaminophen_frequency_drift(good_script)
                    elif mut_key == "restriction_drift":
                        mutated = mut_lifting_limit_drift(good_script)
                        if mutated == good_script:
                            continue
                    elif mut_key == "direction_reversal" and defect_type == "direction_reversal":
                        mutated = mut_driving_on_opioids_reversal(good_script)
                        if "drive" not in good_script.lower():
                            continue
                    elif mut_key == "direction_reversal" and defect_type == "allergy_violation":
                        if "ibuprofen" not in good_script.lower():
                            continue
                        mutated = mut_nsaid_contraindication_reversal(good_script)
                    elif mut_key == "fabricated_doctor":
                        if "Dr." not in good_script:
                            continue
                        mutated = mut_insert_wrong_doctor(good_script)
                    elif mut_key == "fabricated_date":
                        if "March" not in good_script:
                            continue
                        mutated = mut_followup_date_drift(good_script)
                    elif mut_key == "fabrication":
                        mutated = mut_insert_phone_or_stat(good_script)
                    elif mut_key == "critical_partial":
                        if "call us or go to the ER" not in good_script.lower():
                            continue
                        mutated = mut_red_flag_strip_action(good_script, "")
                    else:
                        continue
                    if mutated == good_script:
                        continue
                    cases.append({
                        "case_id": f"{track}_{mut_key}_{idx}",
                        "track": track,
                        "structured_data": copy.deepcopy(sd),
                        "script": mutated,
                        "expect_verdict": expect_verdict,
                        "expect_defect_type": defect_type,
                        "expect_item_id": None,
                        "clinical_rationale": f"Planted {defect_type} via {mut_fn.__name__}",
                    })
                except Exception:
                    continue

            # Med-specific mutations
            for med in sd.get("medications") or []:
                name = med.get("name", "")
                status = (med.get("status") or "").lower()
                for label, fn, verdict, dtype in [
                    ("dose_mismatch", lambda s, m=med: mut_opioid_extra_zero(s, m), "BLOCK", "dose_mismatch"),
                    ("direction_reversal", lambda s, m=med: mut_med_direction_reversal(s, m), "BLOCK", "direction_reversal"),
                    ("partial_dose", lambda s, m=med: mut_drop_dose_keep_name(s, m), "BLOCK", "partial_dose"),
                    ("critical_coverage", lambda s, m=med: mut_drop_new_med(s, m), "BLOCK", "critical_coverage"),
                    ("threshold_drift", lambda s, m=med: mut_anticoag_stop_timing_drift(s, m), "BLOCK", "threshold_drift"),
                    ("threshold_drift_sglt2", lambda s, m=med: mut_sglt2_stop_window_drift(s, m), "BLOCK", "threshold_drift"),
                    ("wrong_medication", lambda s, m=med: mut_lookalike_med_swap(s, m), "BLOCK", "wrong_medication"),
                ]:
                    if label == "direction_reversal" and status not in ("stop", "hold", "continue"):
                        continue
                    if label == "critical_coverage" and status not in ("new", "changed"):
                        continue
                    if label == "threshold_drift" and "warfarin" not in name.lower():
                        continue
                    if label == "threshold_drift_sglt2" and "empagliflozin" not in name.lower():
                        continue
                    if label == "dose_mismatch" and "oxycodone" not in name.lower():
                        continue
                    if label == "wrong_medication" and name not in ("Metformin", "Clopidogrel"):
                        continue
                    try:
                        mutated = fn(good_script)
                        if mutated == good_script:
                            continue
                        cases.append({
                            "case_id": f"{track}_{label}_{_slug(name)}_{idx}",
                            "track": track,
                            "structured_data": copy.deepcopy(sd),
                            "script": mutated,
                            "expect_verdict": verdict,
                            "expect_defect_type": dtype,
                            "expect_item_id": None,
                            "clinical_rationale": f"{dtype} on {name}",
                        })
                    except Exception:
                        continue

            for flag in sd.get("red_flags") or []:
                mutated = mut_drop_red_flag(good_script, flag)
                if mutated != good_script:
                    cases.append({
                        "case_id": f"{track}_drop_red_flag_{idx}",
                        "track": track,
                        "structured_data": copy.deepcopy(sd),
                        "script": mutated,
                        "expect_verdict": "BLOCK",
                        "expect_defect_type": "critical_coverage",
                        "expect_item_id": None,
                        "clinical_rationale": "Removed entire red flag",
                    })

    return cases


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:32]


ALL_CASES = build_all_cases()
