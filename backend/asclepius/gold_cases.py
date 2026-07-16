"""Gold multimodal nephrology cases — a ratified seed set + few-shot exemplars.

These 10 cases are hand-authored (not LLM-generated), each engineered against a
documented frontier-model failure mode (anchoring, right-answer-wrong-reason,
overtreatment, failure-to-seek-context, guideline/sequencing). Every case carries a
multi-timepoint lab trend, an EHR note that re-frames the labs, a med list that is
itself evidence, one decisive + one red-herring flag, and an internal
``ground_truth``/``reasoning_divergence`` that names the sound path vs. the seductive
shortcut.

Two jobs:
  1. **Seed the queue directly** (``load_gold_cases``) so V3 has real multimodal cases
     immediately — independent of live LLM generation. Each ships with an authored
     A/B candidate pair (the SOUND answer vs. the anchored SHORTCUT answer), i.e. the
     exact preference pair a specialist annotates.
  2. **Few-shot exemplars** (``fewshot_cases``) injected into the case-gen prompt so the
     model copies the exact ``ClinicalCase`` shape (never tripping ``extra='forbid'``)
     and the difficulty pattern.

The ``case`` objects use the LITERAL ClinicalCase schema keys; ``ground_truth`` /
``hard_hook`` / ``reasoning_divergence`` are internal (stripped by ``public_case``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

# ─────────────────────────────────────────────────────────────────────────────
# The 10 cases. Each entry: case_id, title, ai_failure_mode, question,
# intended_flawed_id, candidate_answers (A/B), and the full case.
# ─────────────────────────────────────────────────────────────────────────────

GOLD_NEPHROLOGY_CASES: List[Dict[str, Any]] = [
    {
        "case_id": "neph-gold-01-hyponatremia-potomania",
        "title": "Severe hyponatremia — overcorrection trap (beer potomania + thiazide)",
        "ai_failure_mode": "anchoring (SIADH → 3% saline); overtreatment / osmotic demyelination risk",
        "question": "A patient presents with confusion and a serum sodium of 110. How do you classify this hyponatremia and how do you correct it safely?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is low-solute hyponatremia (beer potomania) with a thiazide contribution — NOT SIADH. The urine "
                "osmolality is inappropriately LOW (120) with a low urine sodium, which excludes SIADH and marks this as "
                "the highest-overcorrection-risk group: once solute/volume are restored and the ADH stimulus is removed, "
                "a brisk aquaresis will auto-correct the sodium dangerously fast. Cap correction at ≤6–8 mmol/L per 24h, "
                "hold the hydrochlorothiazide, and reserve small hypertonic-saline boluses for active seizures only. "
                "Anticipate overcorrection and pre-empt it with a DDAVP clamp plus D5W, and monitor sodium every 2–4h.")},
            {"id": "B", "text": (
                "A sodium of 110 with a low serum osmolality is euvolemic SIADH. Start 3% hypertonic saline to raise the "
                "sodium and correct the neurologic symptoms, targeting normalization over the next day, and add fluid "
                "restriction. The low serum osmolality confirms dilutional hyponatremia, so active correction with "
                "hypertonic saline is the priority.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "50-59", "sex": "male"},
            "problem_list": [
                {"condition": "Alcohol use disorder", "since": "chronic"},
                {"condition": "Hypertension", "since": "2019"},
            ],
            "medications": [
                {"drug": "hydrochlorothiazide", "dose": "25 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "118/74", "hr": 88, "rr": 16, "weight_kg": 70,
                       "volume": "clinically euvolemic to mildly hypovolemic"},
            "lab_panels": [
                {"panel": "BMP", "collected_offset_days": 0, "results": [
                    {"analyte": "Sodium", "value": 110, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"},
                    {"analyte": "Potassium", "value": 3.3, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": "L"},
                    {"analyte": "Creatinine", "value": 0.8, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                    {"analyte": "Glucose", "value": 92, "unit": "mg/dL", "ref_low": 70, "ref_high": 99, "flag": ""},
                ]},
                {"panel": "Osmolality + urine studies", "collected_offset_days": 0, "results": [
                    {"analyte": "Serum osmolality", "value": 232, "unit": "mOsm/kg", "ref_low": 275, "ref_high": 295, "flag": "L"},
                    {"analyte": "Urine osmolality", "value": 120, "unit": "mOsm/kg", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "Urine sodium", "value": 14, "unit": "mmol/L", "ref_low": None, "ref_high": None, "flag": ""},
                ]},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "nephrology", "text": (
                    "50s male, heavy daily beer intake with poor food intake x weeks; minimal dietary protein/salt. "
                    "Started HCTZ ~3 weeks ago for BP. ED gave 1 L normal saline on arrival. Exam: mild orthostasis, no "
                    "edema, dry mucous membranes. Mentating but slowed. No seizures.")},
            ],
            "ground_truth": {
                "answer": (
                    "Low-solute hyponatremia (beer potomania) with a thiazide contribution, NOT SIADH — urine osm is "
                    "inappropriately LOW (120) and urine Na is low. Highest-overcorrection-risk group: cap correction at "
                    "≤6–8 mmol/L per 24h, hypertonic saline only if actively seizing, HOLD the thiazide, pre-empt "
                    "overcorrection with a DDAVP + D5W clamp, monitor Na q2–4h."),
                "rationale": "Low urine osm distinguishes low-solute states from SIADH (inappropriately HIGH urine osm). The danger is iatrogenic osmotic demyelination from over-rapid correction.",
                "key_data": ["urine osmolality 120 (LOW)", "urine sodium 14", "beer + low solute intake", "recent thiazide", "saline already given"],
            },
            "hard_hook": "The loud signal (Na 110, low serum osm) screams 'SIADH, give 3% saline.' The quiet decisive signal (urine osm 120 = LOW) says the opposite and flags overcorrection risk.",
            "reasoning_divergence": "Sound path reads urine osm/Na + the beer/thiazide history, classifies low-solute, and builds an overcorrection-PREVENTION plan. Shortcut anchors on serum osm + Na, labels SIADH, and gives hypertonic saline that will overcorrect and risk osmotic demyelination.",
        },
    },
    {
        "case_id": "neph-gold-02-hyponatremia-insufficient-data",
        "title": "Hyponatremia with insufficient data (context-seeking)",
        "ai_failure_mode": "failure to seek missing context — confabulates a diagnosis + plan from a bare sodium",
        "question": "A patient has a serum sodium of 118. What is the diagnosis and what treatment do you start?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "Low sodium on an SSRI is SIADH — sertraline is a well-known cause. Start fluid restriction to 1–1.5 L/day "
                "and recheck the sodium; if it does not improve, add salt tablets or consider a vaptan. The picture fits "
                "SIADH given the medication and the absence of obvious volume loss.")},
            {"id": "B", "text": (
                "You cannot safely classify or treat this yet — the work-up is incomplete. She is not severely "
                "symptomatic (no seizures/coma), so the correct step is to OBTAIN the discriminating data before "
                "committing: serum osmolality, urine osmolality, urine sodium, a documented volume assessment, plus TSH "
                "and a morning cortisol (and note the SSRI as a possible SIADH cause). Give only cautious supportive "
                "measures and avoid hypertonic saline in the absence of severe symptoms. A bare sodium cannot distinguish "
                "SIADH from hypovolemic, low-solute, or hypothyroid/adrenal causes — each has a different, sometimes "
                "opposite, treatment, so committing now would be unsafe.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "70-79", "sex": "female"},
            "problem_list": [
                {"condition": "Chronic kidney disease stage 3", "since": "2021"},
                {"condition": "Depression", "since": "chronic"},
            ],
            "medications": [
                {"drug": "sertraline", "dose": "100 mg", "route": "PO", "freq": "daily"},
                {"drug": "amlodipine", "dose": "5 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "132/78", "hr": 76, "rr": 15, "volume": "not documented on this visit"},
            "lab_panels": [
                {"panel": "BMP", "collected_offset_days": 0, "results": [
                    {"analyte": "Sodium", "value": 118, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"},
                    {"analyte": "Potassium", "value": 4.1, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                    {"analyte": "Creatinine", "value": 1.4, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Glucose", "value": 104, "unit": "mg/dL", "ref_low": 70, "ref_high": 99, "flag": "H"},
                ]},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "primary care", "text": (
                    "70s woman, incidental low sodium on routine labs. Feels 'a bit tired.' No seizures, no focal deficit, "
                    "no vomiting/diarrhea documented. Volume status not assessed this visit. On an SSRI. No urine studies, "
                    "serum osmolality, TSH, or cortisol drawn yet.")},
            ],
            "ground_truth": {
                "answer": (
                    "You cannot safely classify or definitively treat this yet — the work-up is incomplete. Because she is "
                    "not severely symptomatic, OBTAIN the discriminating data first: serum osmolality, urine osmolality, "
                    "urine sodium, a documented volume assessment, plus TSH and morning cortisol (and note the SSRI). "
                    "Provide cautious supportive measures only and avoid hypertonic saline. Committing to 'SIADH' or any "
                    "therapy now is unsafe."),
                "rationale": "Sodium alone cannot distinguish SIADH vs hypovolemic vs low-solute vs hypothyroid/adrenal causes; each has a different and sometimes opposite treatment.",
                "key_data": ["no urine osm/Na", "no serum osm", "volume status not assessed", "no TSH/cortisol", "not severely symptomatic", "SSRI on board"],
            },
            "hard_hook": "The required discriminating data (urine osm/Na, serum osm, volume, TSH/cortisol) are ABSENT. The safe answer is to seek them; the trap is to invent a diagnosis and a plan from a bare sodium.",
            "reasoning_divergence": "Sound path recognizes the data are insufficient and orders the specific tests (context-seeking). Shortcut pattern-matches 'low Na + SSRI = SIADH,' starts fluid restriction or saline, and may harm if she is actually hypovolemic or low-solute.",
        },
    },
    {
        "case_id": "neph-gold-03-hyperkalemia-pseudo-drugs",
        "title": "Hyperkalemia — pseudohyperkalemia + hidden drug drivers + normal ECG",
        "ai_failure_mode": "overtreatment (calcium on a hemolyzed value w/ normal ECG); anchoring on 'K 6.8 = emergency'",
        "question": "A patient's potassium is 6.8. Walk through your immediate management.",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "A potassium of 6.8 is a medical emergency. Give IV calcium gluconate immediately to stabilize the "
                "myocardium, then insulin with dextrose and nebulized albuterol to shift potassium intracellularly, and "
                "sodium polystyrene sulfonate to remove it. Recheck the potassium after treatment and place the patient on "
                "a cardiac monitor.")},
            {"id": "B", "text": (
                "Do not reflexively give the full emergency cocktail. The sample is HEMOLYZED and the 12-lead ECG is "
                "NORMAL, so 6.8 likely overstates the true potassium (pseudohyperkalemia) — repeat with a clean, "
                "non-hemolyzed draw first, and with a normal ECG IV calcium is NOT indicated. If a repeat confirms true "
                "hyperkalemia, shift potassium (insulin/dextrose) and remove it (binder ± diuretic), and — critically — "
                "address the DRIVERS: hold/adjust the ACE inhibitor and spironolactone and STOP the "
                "trimethoprim-sulfamethoxazole, because trimethoprim blocks ENaC and raises potassium. Treat the "
                "reversible causes rather than an artifactual number.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "60-69", "sex": "female"},
            "problem_list": [
                {"condition": "Heart failure with reduced EF", "since": "2020"},
                {"condition": "Type 2 diabetes", "since": "chronic"},
                {"condition": "Chronic kidney disease stage 3b", "since": "2022"},
            ],
            "medications": [
                {"drug": "lisinopril", "dose": "20 mg", "route": "PO", "freq": "daily"},
                {"drug": "spironolactone", "dose": "25 mg", "route": "PO", "freq": "daily"},
                {"drug": "trimethoprim-sulfamethoxazole", "dose": "DS", "route": "PO", "freq": "twice daily (day 3 for UTI)"},
            ],
            "vitals": {"bp": "128/70", "hr": 72, "rr": 16},
            "lab_panels": [
                {"panel": "BMP (hemolyzed sample flagged)", "collected_offset_days": 0, "results": [
                    {"analyte": "Potassium", "value": 6.8, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": "HH"},
                    {"analyte": "Creatinine", "value": 1.6, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Bicarbonate", "value": 22, "unit": "mmol/L", "ref_low": 22, "ref_high": 29, "flag": ""},
                    {"analyte": "Glucose", "value": 118, "unit": "mg/dL", "ref_low": 70, "ref_high": 99, "flag": "H"},
                ]},
                {"panel": "Prior BMP (baseline)", "collected_offset_days": -14, "results": [
                    {"analyte": "Potassium", "value": 4.7, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                    {"analyte": "Creatinine", "value": 1.5, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                ]},
            ],
            "notes": [
                {"note_type": "Nursing", "author_role": "nursing", "text": (
                    "Difficult venipuncture, sample noted grossly hemolyzed by lab; recollection requested. Patient "
                    "asymptomatic, no palpitations. 12-lead ECG done: normal sinus rhythm, no peaked T waves, no widening. "
                    "Started on Bactrim 3 days ago for a UTI.")},
            ],
            "ground_truth": {
                "answer": (
                    "Do not reflexively give the full emergency cocktail. The sample is HEMOLYZED and the ECG is NORMAL, "
                    "so 6.8 likely overstates true K+ (pseudohyperkalemia) — repeat with a clean draw; with a normal ECG "
                    "IV calcium is NOT indicated. If confirmed, insulin/dextrose + a binder ± diuretic, and address the "
                    "DRIVERS: hold/adjust the ACEi and spironolactone and STOP trimethoprim-sulfamethoxazole (trimethoprim "
                    "blocks ENaC and raises K+)."),
                "rationale": "Hemolysis falsely elevates K+; ECG changes (not the number) drive the urgency of calcium; trimethoprim is an under-recognized K+-raising drug stacked on an ACEi + MRA.",
                "key_data": ["sample hemolyzed", "ECG normal", "trimethoprim started day 3", "ACEi + spironolactone", "K+ 4.7 two weeks ago"],
            },
            "hard_hook": "'K 6.8' anchors the model to a code-level emergency and IV calcium. The nursing note (hemolyzed sample, normal ECG) and the med list (trimethoprim on top of ACEi + spironolactone) are what actually decide management.",
            "reasoning_divergence": "Sound path notes hemolysis + normal ECG (repeat before treating, no calcium) and reviews the three K+-raising drugs. Shortcut treats 6.8 as a true emergency, gives calcium unnecessarily, and misses that trimethoprim is a driver.",
        },
    },
    {
        "case_id": "neph-gold-04-aki-fena-trap-ain",
        "title": "AKI — the FeNa trap on diuretics, masking acute interstitial nephritis",
        "ai_failure_mode": "right-answer-wrong-reason (FeNa 0.8% → 'pre-renal'); anchoring",
        "question": "A patient's creatinine has risen over the past few days. What is the cause of the AKI and how do you manage it?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is drug-induced acute interstitial nephritis (AIN), not pre-renal azotemia. The FeNa of 0.8% is "
                "INVALID because the patient is on furosemide — a loop diuretic falsely raises FeNa, so a low FeNa on a "
                "diuretic is uninterpretable. The FeUrea (42%) and the sediment (pyuria, WBC casts, eosinophiluria), "
                "together with the drug timeline (naproxen and pantoprazole started recently), the low-grade fever and the "
                "rash, are the classic picture of AIN. Management: STOP the offending drugs (naproxen and pantoprazole), "
                "supportive care, and consider a short corticosteroid course if creatinine does not improve after "
                "withdrawal (biopsy if unclear). Do NOT give volume for 'pre-renal' AKI.")},
            {"id": "B", "text": (
                "The FeNa is 0.8%, which indicates pre-renal azotemia. Give an IV crystalloid fluid challenge to restore "
                "renal perfusion and recheck the creatinine; hold the furosemide temporarily while volume is repleted. The "
                "low FeNa confirms a pre-renal state, so volume resuscitation is the appropriate first step.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "Osteoarthritis", "since": "chronic"},
                {"condition": "Gastroesophageal reflux", "since": "chronic"},
                {"condition": "Hypertension", "since": "2018"},
            ],
            "medications": [
                {"drug": "furosemide", "dose": "40 mg", "route": "PO", "freq": "daily"},
                {"drug": "naproxen", "dose": "500 mg", "route": "PO", "freq": "twice daily (started ~10 days ago)"},
                {"drug": "pantoprazole", "dose": "40 mg", "route": "PO", "freq": "daily (started ~2 weeks ago)"},
            ],
            "vitals": {"bp": "138/82", "hr": 84, "rr": 16, "temp_c": 37.9},
            "lab_panels": [
                {"panel": "BMP - baseline", "collected_offset_days": -5, "results": [
                    {"analyte": "Creatinine", "value": 1.0, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                ]},
                {"panel": "BMP - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Creatinine", "value": 2.6, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Potassium", "value": 4.8, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                    {"analyte": "Bicarbonate", "value": 20, "unit": "mmol/L", "ref_low": 22, "ref_high": 29, "flag": "L"},
                ]},
                {"panel": "Urine studies", "collected_offset_days": 0, "results": [
                    {"analyte": "FeNa", "value": 0.8, "unit": "%", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "FeUrea", "value": 42, "unit": "%", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "Urine WBC", "value": "many", "unit": "/hpf", "ref_low": None, "ref_high": None, "flag": "H"},
                    {"analyte": "Urine WBC casts", "value": "present", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "Urine eosinophils", "value": "present", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "Urine protein", "value": "1+", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                ]},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "nephrology", "text": (
                    "Creatinine 1.0 -> 2.6 over 5 days. On chronic furosemide; started naproxen ~10 days ago for knee pain "
                    "and pantoprazole ~2 weeks ago. Low-grade fever, faint maculopapular rash on trunk. Non-oliguric. "
                    "Urine shows pyuria with WBC casts and eosinophils.")},
            ],
            "ground_truth": {
                "answer": (
                    "Acute interstitial nephritis (AIN), most likely drug-induced (NSAID and/or PPI), NOT pre-renal. FeNa "
                    "0.8% is INVALID on furosemide; the FeUrea (42%) and the sediment (pyuria, WBC casts, eosinophiluria) "
                    "point to interstitial disease. STOP naproxen and pantoprazole, supportive care, consider steroids if "
                    "no improvement after drug withdrawal. Do NOT give volume for 'pre-renal' AKI."),
                "rationale": "FeNa is confounded by diuretics; FeUrea is the diuretic-robust index. Drug timeline + fever + rash + eosinophiluria + WBC casts is AIN.",
                "key_data": ["on furosemide (invalidates FeNa)", "FeUrea 42%", "WBC casts + eosinophiluria", "naproxen + PPI started recently", "fever + rash", "creatinine 1.0->2.6"],
            },
            "hard_hook": "FeNa 0.8% is the seductive shortcut to 'pre-renal.' The furosemide on the med list invalidates it, and the sediment + drug timeline reveal AIN.",
            "reasoning_divergence": "Sound path notices the loop diuretic, distrusts FeNa, uses FeUrea + sediment + drug timeline, and stops the culprit drugs. Shortcut reads FeNa 0.8% -> pre-renal -> gives fluids, delaying AIN treatment and continuing the offending drugs.",
        },
    },
    {
        "case_id": "neph-gold-05-cardiorenal-permissive-creatinine",
        "title": "Cardiorenal — the 'worsening creatinine' trap during decongestion",
        "ai_failure_mode": "anchoring on rising creatinine ('AKI, stop diuretics'); harmful reversal of needed therapy",
        "question": "A patient admitted for decompensated heart failure now has a rising creatinine on IV diuretics. Do you continue or stop diuresis, and why?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "The creatinine has risen from 1.4 to 1.9, indicating diuretic-induced AKI from over-aggressive diuresis. "
                "Hold the furosemide infusion and give a modest fluid bolus to restore renal perfusion, then reassess "
                "volume status. Protecting the kidneys takes priority, so back off diuresis until the creatinine "
                "stabilizes.")},
            {"id": "B", "text": (
                "CONTINUE — in fact intensify — decongestion; do NOT stop diuretics or give fluids. This creatinine rise is "
                "permissive/pseudo-worsening: the patient is still congested (JVP ~12, 3+ edema, weight barely down) and "
                "the rising hemoglobin and albumin show hemoconcentration — intravascular volume is being appropriately "
                "removed, which is associated with good outcomes as long as decongestion continues. The low post-diuretic "
                "urine sodium signals diuretic resistance, so the fix is MORE natriuresis via sequential nephron blockade "
                "(add metolazone or acetazolamide), not less. Back off only for true intravascular depletion or "
                "hypotension.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "70-79", "sex": "male"},
            "problem_list": [
                {"condition": "Heart failure with reduced EF", "since": "2017"},
                {"condition": "Chronic kidney disease stage 3", "since": "2020"},
            ],
            "medications": [
                {"drug": "furosemide", "dose": "IV infusion", "route": "IV", "freq": "continuous"},
                {"drug": "sacubitril-valsartan", "dose": "held on admission", "route": "PO", "freq": "held"},
            ],
            "vitals": {"bp": "112/70", "hr": 78, "rr": 18, "weight_kg": 84, "jvp": "elevated ~12 cm", "edema": "3+ bilateral"},
            "lab_panels": [
                {"panel": "BMP - admission", "collected_offset_days": -2, "results": [
                    {"analyte": "Creatinine", "value": 1.4, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Hemoglobin", "value": 11.0, "unit": "g/dL", "ref_low": 13.5, "ref_high": 17.5, "flag": "L"},
                    {"analyte": "Albumin", "value": 3.4, "unit": "g/dL", "ref_low": 3.5, "ref_high": 5.0, "flag": "L"},
                ]},
                {"panel": "BMP - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Creatinine", "value": 1.9, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Hemoglobin", "value": 12.6, "unit": "g/dL", "ref_low": 13.5, "ref_high": 17.5, "flag": "L"},
                    {"analyte": "Albumin", "value": 3.9, "unit": "g/dL", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                ]},
                {"panel": "Urine", "collected_offset_days": 0, "results": [
                    {"analyte": "Spot urine sodium (post-diuretic)", "value": 18, "unit": "mmol/L", "ref_low": None, "ref_high": None, "flag": "L"},
                ]},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "cardiology", "text": (
                    "Day 2 IV diuresis. Still congested: JVP ~12 cm, 3+ edema, weight down only 1 kg. Hgb and albumin have "
                    "risen (hemoconcentration), suggesting effective fluid removal from the intravascular space. Low "
                    "post-diuretic urine Na indicates a blunted natriuretic response (diuretic resistance). Not "
                    "hypotensive, mentating well, good urine output overall.")},
            ],
            "ground_truth": {
                "answer": (
                    "CONTINUE — and intensify — decongestion; do NOT stop diuretics or give fluids. The creatinine rise is "
                    "permissive/pseudo-worsening amid ongoing congestion with clear hemoconcentration (rising Hgb/albumin). "
                    "The low post-diuretic urine Na signals diuretic resistance — add sequential nephron blockade "
                    "(metolazone or acetazolamide). Reserve backing off for true intravascular depletion/hypotension."),
                "rationale": "In cardiorenal syndrome, a modest creatinine rise during effective decongestion (hemoconcentration + persistent congestion) is expected; stopping diuresis re-congests the patient and worsens outcomes.",
                "key_data": ["still congested (JVP 12, 3+ edema, weight barely down)", "hemoconcentration (Hgb 11->12.6, albumin 3.4->3.9)", "low post-diuretic urine Na = diuretic resistance", "normotensive"],
            },
            "hard_hook": "The rising creatinine (1.4 -> 1.9) anchors the model to 'AKI: hold diuretics, give fluids.' The persistent congestion + hemoconcentration + diuretic-resistance signals say the opposite: keep going, harder.",
            "reasoning_divergence": "Sound path integrates congestion + hemoconcentration + urine Na, recognizes permissive creatinine rise, and intensifies decongestion. Shortcut anchors on creatinine, stops diuretics/gives fluids, and re-congests the patient — a harmful reversal.",
        },
    },
    {
        "case_id": "neph-gold-06-hrs-vs-atn-albumin-challenge",
        "title": "Hepatorenal syndrome vs ATN vs pre-renal in cirrhosis (failed albumin challenge)",
        "ai_failure_mode": "anchoring ('cirrhosis + low urine Na = pre-renal, give fluid'); misses the failed albumin challenge",
        "question": "A patient with cirrhosis has a rising creatinine. What is the diagnosis and treatment?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This meets HRS-AKI criteria — not simple pre-renal azotemia and not ATN. The creatinine failed to improve "
                "after 2 days of diuretic withdrawal PLUS albumin volume expansion, there is no shock and no "
                "nephrotoxin/contrast, the urine is bland with a very low urine sodium, and there is a clear precipitant "
                "(SBP). Treatment is a vasoconstrictor (terlipressin where available, or norepinephrine, or "
                "midodrine+octreotide) PLUS continued albumin, and treat the SBP. More crystalloid is not the answer — the "
                "albumin challenge already failed — and the bland sediment argues against ATN.")},
            {"id": "B", "text": (
                "Cirrhosis with a low blood pressure and a very low urine sodium (8) is pre-renal azotemia from "
                "intravascular underfilling. Give additional volume — crystalloid boluses plus more albumin — to restore "
                "perfusion and improve the creatinine. The low urine sodium confirms avid sodium retention from a "
                "pre-renal state, so continued volume expansion is the treatment.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "50-59", "sex": "male"},
            "problem_list": [
                {"condition": "Cirrhosis with ascites", "since": "2021"},
                {"condition": "Recent spontaneous bacterial peritonitis", "since": "this admission"},
            ],
            "medications": [
                {"drug": "furosemide", "dose": "held", "route": "PO", "freq": "held 48h"},
                {"drug": "spironolactone", "dose": "held", "route": "PO", "freq": "held 48h"},
                {"drug": "ceftriaxone", "dose": "2 g", "route": "IV", "freq": "daily"},
            ],
            "vitals": {"bp": "96/58", "hr": 94, "rr": 18, "map": 71, "no_shock": True},
            "lab_panels": [
                {"panel": "BMP - baseline", "collected_offset_days": -6, "results": [
                    {"analyte": "Creatinine", "value": 0.9, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                ]},
                {"panel": "BMP - today (day 2 of albumin)", "collected_offset_days": 0, "results": [
                    {"analyte": "Creatinine", "value": 2.4, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Sodium", "value": 129, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"},
                ]},
                {"panel": "Urine studies", "collected_offset_days": 0, "results": [
                    {"analyte": "Urine sodium", "value": 8, "unit": "mmol/L", "ref_low": None, "ref_high": None, "flag": "L"},
                    {"analyte": "Urine sediment", "value": "bland, no casts", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "Urine protein", "value": "trace", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                ]},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "nephrology", "text": (
                    "Cr 0.9 -> 2.4 during SBP treatment. Diuretics held 48h AND albumin 1 g/kg/day given x2 days per HRS "
                    "work-up: creatinine has NOT improved. No nephrotoxins, no contrast. No shock (MAP 71 off pressors). "
                    "Bland urine, very low urine Na. Recent SBP now on ceftriaxone.")},
            ],
            "ground_truth": {
                "answer": (
                    "Meets HRS-AKI criteria, not pre-renal and not ATN: creatinine failed to improve after 2 days of "
                    "diuretic withdrawal PLUS albumin, no shock, no nephrotoxin/contrast, bland urine with very low urine "
                    "Na, and an SBP precipitant. Treat with a vasoconstrictor (terlipressin, or norepinephrine, or "
                    "midodrine+octreotide) PLUS continued albumin, and treat the SBP. More fluid/crystalloid is not the "
                    "answer (the albumin challenge already failed); the bland sediment argues against ATN."),
                "rationale": "The defining feature is the FAILED albumin challenge + diuretic withdrawal — that separates HRS from pre-renal (which would have responded), and the bland urine separates it from ATN.",
                "key_data": ["no improvement after 48h albumin + diuretic hold", "no shock", "no nephrotoxin/contrast", "bland urine, urine Na 8", "SBP precipitant"],
            },
            "hard_hook": "'Cirrhosis + low urine Na + low BP' anchors to 'pre-renal, give fluids.' The note's FAILED albumin challenge is the single datum that reclassifies this as HRS-AKI and changes the drug.",
            "reasoning_divergence": "Sound path recognizes the completed, failed albumin+diuretic-withdrawal challenge and starts a vasoconstrictor. Shortcut anchors on low urine Na, calls it pre-renal, and gives more volume that has already been shown not to work.",
        },
    },
    {
        "case_id": "neph-gold-07-transplant-bk-vs-rejection",
        "title": "Post-transplant rising creatinine — BK nephropathy vs rejection",
        "ai_failure_mode": "anchoring ('transplant + rising Cr = rejection → increase immunosuppression') — the exact opposite of correct, and harmful",
        "question": "A kidney transplant recipient has a rising creatinine several months post-transplant. What is your diagnosis and management?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "A rising creatinine in a transplant recipient is acute rejection until proven otherwise. Start pulse "
                "corticosteroids and increase maintenance immunosuppression (raise the tacrolimus target), and obtain a "
                "biopsy. Empiric anti-rejection therapy should not be delayed while the work-up is pending, given the risk "
                "to the graft.")},
            {"id": "B", "text": (
                "BK virus-associated nephropathy is the leading diagnosis: a rising plasma BK PCR (undetectable → 45,000 "
                "copies/mL), a rising creatinine, a supratherapeutic tacrolimus trough (11.5), a negative donor-specific "
                "antibody, a non-tender graft, and decoy cells on cytology. Confirm with biopsy (SV40 staining). The "
                "correct management is to REDUCE immunosuppression — lower tacrolimus toward the low end and reduce or hold "
                "mycophenolate — NOT increase it. Increasing immunosuppression or giving pulse steroids for a presumed "
                "rejection would worsen BK replication and the nephropathy. Do not anchor on 'transplant + rising "
                "creatinine = rejection.'")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "40-49", "sex": "male"},
            "problem_list": [
                {"condition": "Deceased-donor kidney transplant ~8 months ago", "since": "this year"},
                {"condition": "Prior end-stage kidney disease", "since": "chronic"},
            ],
            "medications": [
                {"drug": "tacrolimus", "dose": "per level", "route": "PO", "freq": "twice daily"},
                {"drug": "mycophenolate mofetil", "dose": "1000 mg", "route": "PO", "freq": "twice daily"},
                {"drug": "prednisone", "dose": "5 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "134/82", "hr": 76, "rr": 15, "temp_c": 37.0, "graft": "non-tender"},
            "lab_panels": [
                {"panel": "Transplant labs - baseline", "collected_offset_days": -28, "results": [
                    {"analyte": "Creatinine", "value": 1.3, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                    {"analyte": "Tacrolimus trough", "value": 7.5, "unit": "ng/mL", "ref_low": 5, "ref_high": 8, "flag": ""},
                    {"analyte": "BK virus PCR (plasma)", "value": "not detected", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                ]},
                {"panel": "Transplant labs - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Creatinine", "value": 2.0, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Tacrolimus trough", "value": 11.5, "unit": "ng/mL", "ref_low": 5, "ref_high": 8, "flag": "H"},
                    {"analyte": "BK virus PCR (plasma)", "value": 45000, "unit": "copies/mL", "ref_low": None, "ref_high": None, "flag": "H"},
                    {"analyte": "Donor-specific antibody", "value": "negative", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                ]},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "transplant nephrology", "text": (
                    "8 months post-transplant. Cr 1.3 -> 2.0 over ~4 weeks. Tac trough now supratherapeutic (11.5) and "
                    "plasma BK PCR risen from undetectable to 45,000 copies/mL. DSA negative. Graft non-tender, afebrile. "
                    "Biopsy pending; decoy cells on urine cytology.")},
            ],
            "ground_truth": {
                "answer": (
                    "BK virus-associated nephropathy is the leading diagnosis (rising plasma BK PCR >10,000, rising "
                    "creatinine, supratherapeutic tacrolimus, negative DSA, non-tender graft, decoy cells) — confirm with "
                    "biopsy (SV40). The correct management is to REDUCE immunosuppression (lower tacrolimus, reduce/hold "
                    "mycophenolate), NOT increase it. Increasing immunosuppression or pulse steroids for presumed rejection "
                    "would worsen BK. Do not anchor on 'transplant + rising creatinine = rejection.'"),
                "rationale": "BK nephropathy and rejection present identically (rising creatinine) but require OPPOSITE immunosuppression changes; the rising BK PCR + supratherapeutic tac + negative DSA point to BK.",
                "key_data": ["BK PCR 0 -> 45,000", "tacrolimus 7.5 -> 11.5 (supratherapeutic)", "DSA negative", "decoy cells", "creatinine 1.3 -> 2.0"],
            },
            "hard_hook": "The dominant prior 'transplant + rising creatinine = acute rejection' pulls the model to INCREASE immunosuppression / pulse steroids. The BK PCR trend + supratherapeutic tac + negative DSA demand the OPPOSITE (reduce immunosuppression).",
            "reasoning_divergence": "Sound path reads the rising BK titer + negative DSA + high tac, diagnoses BK nephropathy, and REDUCES immunosuppression. Shortcut anchors on rejection, increases immunosuppression/gives steroids, and accelerates BK nephropathy — a directly harmful error.",
        },
    },
    {
        "case_id": "neph-gold-08-ckd-mbd-calcium-sequencing",
        "title": "CKD-MBD — the calcium/sequencing trap",
        "ai_failure_mode": "sequencing error ('high PTH → more calcitriol/calcium') worsening hypercalcemia; anchoring on PTH alone",
        "question": "A CKD patient has a high parathyroid hormone. How do you adjust their mineral-bone therapy?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "Do NOT escalate calcitriol or calcium to chase the PTH. The rising calcium (now 10.4) and persistent "
                "hyperphosphatemia on a calcium-based binder plus active vitamin D mean the calcium load must come DOWN: "
                "switch calcium carbonate to a NON-calcium phosphate binder (sevelamer or lanthanum) and reduce or hold "
                "calcitriol. For the still-high PTH, add a calcimimetic (cinacalcet or etelcalcetide), which lowers PTH "
                "while also lowering calcium — the correct sequencing. Repleting the low nutritional 25-OH vitamin D is "
                "reasonable, but active vitamin D should not be increased while calcium is high.")},
            {"id": "B", "text": (
                "The PTH is high and still climbing (420 → 520), indicating undertreated secondary hyperparathyroidism. "
                "Increase the calcitriol dose to suppress the parathyroid glands and continue the calcium carbonate as "
                "both a binder and a calcium source. Titrate the active vitamin D upward until the PTH falls toward the "
                "CKD target range.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "60-69", "sex": "female"},
            "problem_list": [
                {"condition": "Chronic kidney disease stage 4", "since": "2020"},
                {"condition": "Secondary hyperparathyroidism", "since": "chronic"},
            ],
            "medications": [
                {"drug": "calcium carbonate", "dose": "1200 mg with meals", "route": "PO", "freq": "three times daily"},
                {"drug": "calcitriol", "dose": "0.5 mcg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "140/84", "hr": 70, "rr": 15},
            "lab_panels": [
                {"panel": "CKD-MBD panel - 3 months ago", "collected_offset_days": -90, "results": [
                    {"analyte": "Calcium", "value": 9.4, "unit": "mg/dL", "ref_low": 8.5, "ref_high": 10.2, "flag": ""},
                    {"analyte": "Phosphate", "value": 5.6, "unit": "mg/dL", "ref_low": 2.5, "ref_high": 4.5, "flag": "H"},
                    {"analyte": "PTH", "value": 420, "unit": "pg/mL", "ref_low": 15, "ref_high": 65, "flag": "H"},
                ]},
                {"panel": "CKD-MBD panel - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Calcium", "value": 10.4, "unit": "mg/dL", "ref_low": 8.5, "ref_high": 10.2, "flag": "H"},
                    {"analyte": "Phosphate", "value": 5.9, "unit": "mg/dL", "ref_low": 2.5, "ref_high": 4.5, "flag": "H"},
                    {"analyte": "PTH", "value": 520, "unit": "pg/mL", "ref_low": 15, "ref_high": 65, "flag": "H"},
                    {"analyte": "25-OH vitamin D", "value": 22, "unit": "ng/mL", "ref_low": 30, "ref_high": 100, "flag": "L"},
                ]},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "nephrology", "text": (
                    "CKD4 with secondary hyperparathyroidism. PTH still climbing (420 -> 520) BUT calcium has now risen "
                    "into the high range (9.4 -> 10.4) and phosphate remains high on a calcium-based binder plus active "
                    "vitamin D. Vascular calcification on prior imaging.")},
            ],
            "ground_truth": {
                "answer": (
                    "Do NOT escalate calcitriol or calcium to chase the PTH. The rising calcium (10.4) and persistent "
                    "hyperphosphatemia on a calcium-based binder + active vitamin D mean the calcium load must come DOWN: "
                    "switch to a non-calcium binder (sevelamer/lanthanum), reduce/hold calcitriol, and add a calcimimetic "
                    "(cinacalcet/etelcalcetide) to lower PTH and calcium together. Replete nutritional 25-OH vitamin D, but "
                    "do not increase active vitamin D while calcium is high."),
                "rationale": "In CKD-MBD you cannot treat PTH in isolation — calcium and phosphate constrain the therapy. Rising calcium on calcium-based binder + calcitriol mandates de-escalating the calcium load and using a calcimimetic.",
                "key_data": ["calcium 9.4 -> 10.4 (now high)", "phosphate persistently high", "on calcium-based binder + calcitriol", "PTH rising", "vascular calcification"],
            },
            "hard_hook": "A high, rising PTH anchors the model to 'give more active vitamin D / calcium.' The rising calcium + phosphate on the current regimen are the decisive signals that the calcium load must be reduced instead.",
            "reasoning_divergence": "Sound path integrates Ca + Phos + the current meds, de-escalates calcium/calcitriol, switches to a non-calcium binder, and adds a calcimimetic. Shortcut treats PTH in isolation, escalates calcitriol/calcium, and worsens hypercalcemia and vascular calcification.",
        },
    },
    {
        "case_id": "neph-gold-09-triple-acid-base-albumin",
        "title": "Triple acid-base disorder with albumin correction",
        "ai_failure_mode": "computation trap (uncorrected anion gap looks normal because albumin is low); misses concurrent metabolic alkalosis",
        "question": "Interpret this patient's acid-base status and identify all disturbances present.",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "There are three disturbances. (1) Correct the anion gap for albumin: measured AG = 138 − 103 − 15 = 20; "
                "add ~2.5 per 1 g/dL of albumin below 4.0, giving a corrected AG ≈ 20 + 2.5×2.0 ≈ 25 — a clear HIGH-anion-"
                "gap metabolic acidosis (DKA + lactic acidosis). (2) Delta-delta: the AG rose ~13 above normal while HCO3 "
                "fell only ~7, so the AG rose more than HCO3 dropped, revealing a CONCURRENT metabolic alkalosis (from the "
                "days of vomiting). (3) Winter's formula: expected pCO2 = 1.5×15 + 8 ≈ 30 ± 2; measured 28 is appropriate, "
                "so respiratory compensation is adequate (no separate respiratory disorder). Net: high-AG metabolic "
                "acidosis + metabolic alkalosis, appropriately compensated. Treat the DKA/sepsis and recognize the "
                "alkalosis so bicarbonate is not over-interpreted.")},
            {"id": "B", "text": (
                "The pH is 7.34 with a bicarbonate of 15 and an anion gap of about 20, which is only mildly elevated. This "
                "is a single mild high-anion-gap metabolic acidosis from the DKA, with appropriate respiratory "
                "compensation. Treat the DKA with insulin and fluids and the acidosis will resolve.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "60-69", "sex": "female"},
            "problem_list": [
                {"condition": "Type 2 diabetes", "since": "chronic"},
                {"condition": "Sepsis (urinary source)", "since": "this admission"},
            ],
            "medications": [
                {"drug": "insulin infusion", "dose": "per protocol", "route": "IV", "freq": "continuous"},
                {"drug": "piperacillin-tazobactam", "dose": "4.5 g", "route": "IV", "freq": "q8h"},
            ],
            "vitals": {"bp": "104/62", "hr": 108, "rr": 26, "temp_c": 38.6},
            "lab_panels": [
                {"panel": "ABG + BMP", "collected_offset_days": 0, "results": [
                    {"analyte": "pH", "value": 7.34, "unit": "", "ref_low": 7.35, "ref_high": 7.45, "flag": "L"},
                    {"analyte": "pCO2", "value": 28, "unit": "mmHg", "ref_low": 35, "ref_high": 45, "flag": "L"},
                    {"analyte": "Bicarbonate", "value": 15, "unit": "mmol/L", "ref_low": 22, "ref_high": 29, "flag": "L"},
                    {"analyte": "Sodium", "value": 138, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": ""},
                    {"analyte": "Chloride", "value": 103, "unit": "mmol/L", "ref_low": 98, "ref_high": 107, "flag": ""},
                    {"analyte": "Albumin", "value": 2.0, "unit": "g/dL", "ref_low": 3.5, "ref_high": 5.0, "flag": "LL"},
                    {"analyte": "Glucose", "value": 410, "unit": "mg/dL", "ref_low": 70, "ref_high": 99, "flag": "H"},
                    {"analyte": "Beta-hydroxybutyrate", "value": 4.2, "unit": "mmol/L", "ref_low": 0, "ref_high": 0.3, "flag": "H"},
                    {"analyte": "Lactate", "value": 3.1, "unit": "mmol/L", "ref_low": 0.5, "ref_high": 2.0, "flag": "H"},
                ]},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "ICU", "text": (
                    "Septic from a urinary source, hyperglycemic with ketosis (DKA), and with several days of vomiting "
                    "before admission. Profoundly hypoalbuminemic (albumin 2.0). Tachypneic with a Kussmaul pattern. No "
                    "bicarbonate has been given. The measured anion gap looks only mildly elevated at first glance, which "
                    "seems reassuring, but the low albumin should be accounted for before interpreting it.")},
            ],
            "ground_truth": {
                "answer": (
                    "Three disturbances. (1) Albumin-correct the AG: measured 138−103−15 = 20; +2.5 per g/dL albumin below "
                    "4.0 → corrected AG ≈ 25 → a clear HAGMA (DKA + lactate). (2) Delta-delta: ΔAG (~13) > ΔHCO3 (~7) → a "
                    "CONCURRENT metabolic alkalosis (vomiting). (3) Winter's: expected pCO2 ≈ 30 ± 2; measured 28 → adequate "
                    "respiratory compensation, no separate respiratory disorder. Net: HAGMA + metabolic alkalosis, "
                    "appropriately compensated. Treat DKA/sepsis; recognize the alkalosis so bicarbonate is not "
                    "over-interpreted."),
                "rationale": "The low albumin masks the true anion gap; without correcting it the acidosis is under-called, and without delta-delta the concurrent vomiting-induced alkalosis is missed.",
                "key_data": ["albumin 2.0 (masks AG)", "beta-hydroxybutyrate 4.2 + lactate 3.1", "days of vomiting", "delta-delta reveals alkalosis"],
            },
            "hard_hook": "The uncorrected anion gap looks only mildly high because albumin is 2.0. Correcting for albumin (and running delta-delta) is what exposes both the true HAGMA and the hidden metabolic alkalosis.",
            "reasoning_divergence": "Sound path corrects the AG for albumin and runs delta-delta + Winter's, naming all three components. Shortcut reads the uncorrected AG as near-normal, calls a single simple acidosis, and misses the concurrent alkalosis.",
        },
    },
    {
        "case_id": "neph-gold-10-hypokalemia-alkalosis-gitelman-vs-vomiting",
        "title": "Refractory hypokalemia + metabolic alkalosis — Gitelman vs surreptitious vomiting/diuretic",
        "ai_failure_mode": "anchoring/availability ('hypokalemia + alkalosis + low Mg = Gitelman') without the discriminating urine studies",
        "question": "A normotensive patient has persistent hypokalemia and metabolic alkalosis. What is the diagnosis and how do you confirm it?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "The combination of hypokalemia, metabolic alkalosis, hypomagnesemia, and normotension is classic Gitelman "
                "syndrome. Start potassium and magnesium repletion, add a potassium-sparing agent such as spironolactone "
                "or amiloride, and consider genetic testing to confirm the SLC12A3 mutation. The full triad in a young "
                "normotensive patient makes a renal tubulopathy the clear diagnosis.")},
            {"id": "B", "text": (
                "The LOW urine chloride (8) argues AGAINST Gitelman/Bartter and current diuretic use — all of which cause "
                "salt wasting with a HIGH urine chloride — and points instead to a chloride-responsive alkalosis from "
                "surreptitious VOMITING (or remote/intermittent diuretic use): a chloride-DEPLETION alkalosis, not a renal "
                "tubulopathy. Do not accept the 'Gitelman' anchor. Confirm by interpreting the urine chloride (low = "
                "vomiting/remote diuretic; high = Bartter/Gitelman/current diuretic), send a urine calcium/creatinine (low "
                "in Gitelman) and a urine diuretic screen, and explore an eating disorder sensitively. Manage the true "
                "cause (volume/chloride and K/Mg repletion, plus the behavior) rather than labeling a lifelong "
                "tubulopathy.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "20-29", "sex": "female"},
            "problem_list": [
                {"condition": "Anxiety/depression", "since": "chronic"},
                {"condition": "Recurrent hypokalemia", "since": "this year"},
            ],
            "medications": [
                {"drug": "potassium chloride", "dose": "40 mEq", "route": "PO", "freq": "twice daily"},
            ],
            "vitals": {"bp": "108/68", "hr": 82, "rr": 14, "bmi": "low-normal"},
            "lab_panels": [
                {"panel": "BMP + Mg", "collected_offset_days": 0, "results": [
                    {"analyte": "Potassium", "value": 2.9, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": "L"},
                    {"analyte": "Bicarbonate", "value": 34, "unit": "mmol/L", "ref_low": 22, "ref_high": 29, "flag": "H"},
                    {"analyte": "Magnesium", "value": 1.4, "unit": "mg/dL", "ref_low": 1.7, "ref_high": 2.4, "flag": "L"},
                    {"analyte": "Chloride", "value": 92, "unit": "mmol/L", "ref_low": 98, "ref_high": 107, "flag": "L"},
                ]},
                {"panel": "Urine electrolytes", "collected_offset_days": 0, "results": [
                    {"analyte": "Urine chloride", "value": 8, "unit": "mmol/L", "ref_low": None, "ref_high": None, "flag": "L"},
                    {"analyte": "Urine calcium/creatinine", "value": "not sent", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                    {"analyte": "Urine diuretic screen", "value": "not sent", "unit": "", "ref_low": None, "ref_high": None, "flag": ""},
                ]},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "nephrology", "text": (
                    "20s woman, normotensive, recurrent hypokalemia + metabolic alkalosis + low Mg, refractory to oral K. "
                    "Referred as 'likely Gitelman syndrome.' History notably incomplete re: vomiting, laxative or diuretic "
                    "use, or an eating disorder. Urine chloride is LOW at 8.")},
            ],
            "ground_truth": {
                "answer": (
                    "The LOW urine chloride (8) argues AGAINST Gitelman/Bartter and current diuretic use (all cause salt "
                    "wasting with HIGH urine chloride) and points to a chloride-responsive alkalosis from surreptitious "
                    "VOMITING (or remote/intermittent diuretic use). Do not accept the 'Gitelman' anchor. Confirm with "
                    "urine chloride interpretation, a urine calcium/creatinine (low in Gitelman), and a urine diuretic "
                    "screen, and explore an eating disorder sensitively. Manage the true cause rather than a lifelong "
                    "tubulopathy label."),
                "rationale": "Urine chloride separates renal salt-wasting tubulopathies (high) from extrarenal chloride depletion like vomiting (low); the case was pre-labeled Gitelman but the low urine chloride refutes it.",
                "key_data": ["urine chloride LOW (8)", "normotensive", "urine Ca/Cr and diuretic screen NOT sent", "history incomplete re vomiting/diuretics", "referred as 'Gitelman'"],
            },
            "hard_hook": "The referral label ('likely Gitelman') plus the classic triad anchors the model to a tubulopathy. The LOW urine chloride is the one datum that refutes it and redirects to chloride-depletion alkalosis (vomiting/diuretic), and the discriminating urine calcium + diuretic screen were not even sent.",
            "reasoning_divergence": "Sound path uses urine chloride (low -> not Gitelman/Bartter) and seeks urine calcium + a diuretic screen before labeling. Shortcut accepts the 'Gitelman' anchor from the triad, ignores the low urine chloride, and misses surreptitious vomiting/diuretic use.",
        },
    },
]


def _validated() -> List[Dict[str, Any]]:
    """Fail fast (at import) if a case does not clear the real content gate or is
    missing its A/B pair — a broken seed must never ship silently."""
    from asclepius.cases import assert_multimodal_content, MultimodalContentError

    ok: List[Dict[str, Any]] = []
    for entry in GOLD_NEPHROLOGY_CASES:
        case = entry["case"]
        try:
            assert_multimodal_content(case)
        except MultimodalContentError as exc:  # pragma: no cover - guarded by tests
            raise ValueError(f"gold case {entry['case_id']} fails content gate: {exc}") from exc
        cands = entry.get("candidate_answers") or []
        if len(cands) != 2 or entry.get("intended_flawed_id") not in ("A", "B"):
            raise ValueError(f"gold case {entry['case_id']} missing a valid A/B pair")
        ok.append(entry)
    return ok


def fewshot_cases(k: int = 2, start: int = 0) -> List[Dict[str, Any]]:
    """Return ``k`` gold ``{question, case}`` exemplars (public case only — the
    answer key is stripped) to inject as few-shot into the case-gen prompt. ``start``
    rotates the window so calls don't always show the same cases."""
    from asclepius.cases import public_case

    n = len(GOLD_NEPHROLOGY_CASES)
    if n == 0 or k <= 0:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(min(k, n)):
        entry = GOLD_NEPHROLOGY_CASES[(start + i) % n]
        out.append({"question": entry["question"], "case": public_case(entry["case"])})
    return out


def fewshot_prompt_block(k: int = 2, start: int = 0) -> str:
    """A ready-to-inject text block of ``k`` full exemplars for the case-gen user
    message. Empty string when no cases are available."""
    ex = fewshot_cases(k=k, start=start)
    if not ex:
        return ""
    blocks = [json.dumps(e, ensure_ascii=False) for e in ex]
    return (
        "\n\nWORKED EXAMPLES — author a DIFFERENT case in this EXACT shape (do not copy "
        "the content). Each is a valid {question, case} object:\n" + "\n\n".join(blocks)
    )


def load_gold_cases(store: Any, *, specialty: str = "nephrology") -> Dict[str, Any]:
    """Insert the gold cases as ready-to-serve multimodal tasks, idempotently (a
    stable ``gold-<case_id>`` task id is skipped if already present). Each ships with
    its authored A/B candidate pair, so it is a complete V3 task with NO LLM needed.
    Returns ``{loaded, skipped, total, task_ids}``.

    ``specialty`` filters WHICH gold cases load (the gold set is nephrology-only
    today): a mismatched specialty loads nothing rather than mis-tagging nephrology
    cases under it, so ``POST /generation/<other>/load-gold`` is a correct no-op."""
    from asclepius.cases import render_case_prompt

    # Only cases whose own specialty matches the request (the set is authored per
    # specialty; the case's specialty is authoritative, never the path param).
    eligible = [e for e in _validated() if (e.get("case") or {}).get("specialty", "nephrology") == specialty]

    loaded, skipped, task_ids = 0, 0, []
    for entry in eligible:
        tid = "gold-" + entry["case_id"]
        if store.get_task(tid):
            skipped += 1
            continue
        case = entry["case"]
        prompt = render_case_prompt(case, entry["question"])
        store.insert_task(
            task_id=tid,
            prompt=prompt,
            specialty=case.get("specialty", specialty),
            difficulty="hard",
            capture_reasoning=True,          # the multimodal value IS the reasoning trace
            source="gold_seed",
            candidate_answers=list(entry["candidate_answers"]),
            case=case,
            generation={
                "mode": "gold_seed",
                "ratified": True,
                "case_id": entry["case_id"],
                "title": entry.get("title"),
                "ai_failure_mode": entry.get("ai_failure_mode"),
                "intended_flawed_id": entry["intended_flawed_id"],
                "modality": "multimodal",
                "case_source": case.get("case_source", "synthetic"),
            },
            created_by="system:gold_seed",
        )
        loaded += 1
        task_ids.append(tid)
    return {"loaded": loaded, "skipped": skipped, "total": len(eligible), "task_ids": task_ids}
