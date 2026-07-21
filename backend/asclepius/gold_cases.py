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


# ═════════════════════════════════════════════════════════════════════════════
# CARDIOLOGY — 10 hard multimodal cases (Asclepius_Cardiology_Oncology_Cases.md,
# Part A). Each hides the decisive signal in a STUDY (ECG/echo/cath/biomarker)
# that contradicts the loud vignette (PRD §4.3). ``candidate_answers`` A/B = the
# SOUND path vs the anchored SHORTCUT; ``intended_flawed_id`` names the shortcut.
# ═════════════════════════════════════════════════════════════════════════════

GOLD_CARDIOLOGY_CASES: List[Dict[str, Any]] = [
    {
        "case_id": "card-gold-01-dissection-as-inferior-stemi",
        "title": "Aortic dissection masquerading as inferior STEMI",
        "ai_failure_mode": "anchoring (ST-elevation → STEMI → thrombolytic/DAPT); catastrophic unsafe_recommendation",
        "taxonomy_bucket": "great_mimics",
        "subtopic": "dissection_as_mi",
        "question": "A 60-69y man with tearing chest pain and inferior ST-elevation. What is your immediate management?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "Do NOT give antiplatelets, anticoagulation, or thrombolytics. The inter-arm systolic differential "
                "(178/64 right vs 122/50 left = 56 mmHg), the dilated aortic root (4.6 cm) with new aortic regurgitation "
                "and a pericardial effusion on TTE, and the pain that migrated from chest to back are a type A aortic "
                "DISSECTION extending into the RCA ostium — the inferior ST-elevation is secondary. Obtain an emergent CT "
                "angiogram of the aorta, control heart rate and blood pressure (IV esmolol first to blunt dP/dt, then a "
                "vasodilator), and activate emergent cardiac surgery. Antithrombotics here can precipitate fatal "
                "tamponade.")},
            {"id": "B", "text": (
                "Inferior ST-elevation in II, III and aVF is an acute inferior STEMI. Activate the cath lab, load dual "
                "antiplatelet therapy (aspirin + ticagrelor) and heparin now, and if PCI is not immediately available give "
                "a thrombolytic. Time is myocardium — reperfuse the occluded right coronary artery without delay.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "Hypertension", "since": "chronic"},
                {"condition": "Former smoker", "since": "chronic"},
            ],
            "medications": [
                {"drug": "amlodipine", "dose": "10 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp_right_arm": "178/64", "bp_left_arm": "122/50", "hr": 58, "rr": 20,
                       "pain": "tearing, migrated chest → back"},
            "lab_panels": [
                {"panel": "Cardiac + renal", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T", "value": 0.06, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                    {"analyte": "Creatinine", "value": 1.3, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                    {"analyte": "D-dimer", "value": 4.8, "unit": "µg/mL FEU", "ref_low": 0, "ref_high": 0.5, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "Sinus bradycardia at 58. ST-elevation in II, III and aVF WITHOUT reciprocal ST-depression in I/aVL. PR-segment depression. No pathologic Q waves.",
                 "measurements": [
                     {"analyte": "PR interval", "value": 168, "unit": "ms", "ref_low": 120, "ref_high": 200, "flag": ""},
                     {"analyte": "QRS duration", "value": 92, "unit": "ms", "ref_low": 80, "ref_high": 120, "flag": ""},
                 ],
                 "impression": "Inferior ST-elevation, atypical (no reciprocal change) — correlate for a non-coronary cause."},
                {"modality": "echo", "label": "TTE (limited, bedside)",
                 "findings": "Dilated aortic root with an intimal flap suggested; moderate aortic regurgitation; small circumferential pericardial effusion without tamponade physiology yet. No inferior wall akinesis.",
                 "measurements": [
                     {"analyte": "Aortic root diameter", "value": 4.6, "unit": "cm", "ref_low": 2.0, "ref_high": 3.7, "flag": "H"},
                     {"analyte": "LVEF", "value": 55, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": ""},
                 ],
                 "impression": "Root dilation + AR + effusion — concerning for type A dissection."},
            ],
            "notes": [
                {"note_type": "Nursing", "author_role": "nursing", "text": (
                    "60s man, abrupt severe chest pain that he describes as 'tearing' and which has now MIGRATED from the "
                    "chest through to the back between the shoulder blades. Radial pulse feels weaker in the LEFT arm and "
                    "the automated cuff reads much lower on the left. He looks pale and diaphoretic but is not "
                    "hypotensive. History of poorly controlled hypertension. No prior cardiac history.")},
            ],
            "ground_truth": {
                "answer": (
                    "Type A aortic dissection presenting with inferior ST-elevation (RCA-ostium involvement) — NOT a "
                    "primary STEMI. Withhold all antithrombotics/thrombolytics, obtain an emergent CT angiogram of the "
                    "aorta, control dP/dt with IV esmolol then a vasodilator (target SBP ~100-120 and HR ~60), and "
                    "activate emergent cardiac surgery. Anticoagulation/lysis risks fatal tamponade."),
                "rationale": "The inter-arm BP differential, root dilation + AR + pericardial effusion, and migrating tearing pain define dissection; the inferior ST-elevation is from RCA-ostial extension, and the absence of reciprocal depression is a red flag against simple STEMI.",
                "key_data": ["inter-arm SBP differential 56 mmHg", "aortic root 4.6 cm + AR + effusion", "pain migrated chest→back", "no reciprocal ST-depression", "D-dimer 4.8"],
            },
            "hard_hook": "Inferior ST-elevation screams STEMI → give DAPT + thrombolytic/cath. The inter-arm BP differential and the echo (root 4.6, AR, effusion) say type A dissection, where antithrombotics can kill.",
            "reasoning_divergence": "Sound path grounds the echo + the BP differential, withholds antithrombotics, and gets a CT aorta + surgery. Shortcut anchors on the ST-elevation, gives DAPT/lytic, and precipitates tamponade.",
        },
    },
    {
        "case_id": "card-gold-02-wellens-discharged",
        "title": "Wellens syndrome read as atypical, low-risk chest pain",
        "ai_failure_mode": "under-calling a benign-looking ECG; failure to ground T-wave morphology (impending LAD occlusion)",
        "taxonomy_bucket": "ecg_high_risk_subtle",
        "subtopic": "wellens",
        "question": "A 50-59y man with resolved chest pain, now pain-free, normal first troponin. What is the disposition?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "He is pain-free with a normal initial troponin and looks well, so this is atypical low-risk chest pain. "
                "Discharge home with outpatient follow-up, or arrange an outpatient exercise stress test to risk-stratify. "
                "No admission is needed given the reassuring troponin and resolved symptoms.")},
            {"id": "B", "text": (
                "The V2-V3 biphasic/deeply-inverted T waves with preserved R waves and an isoelectric ST segment in a "
                "pain-free patient are Wellens syndrome (type B) — a marker of critical proximal LAD stenosis and impending "
                "anterior MI. Admit, start antithrombotic therapy, and obtain URGENT invasive coronary angiography. Do NOT "
                "stress-test and do NOT discharge: exercise can precipitate a widow-maker anterior infarction. The normal "
                "troponin and resolved pain are expected in the pain-free phase and are falsely reassuring.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "50-59", "sex": "male"},
            "problem_list": [
                {"condition": "Hyperlipidemia", "since": "chronic"},
                {"condition": "Tobacco use", "since": "chronic"},
            ],
            "medications": [
                {"drug": "atorvastatin", "dose": "40 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "134/82", "hr": 74, "rr": 15, "spo2": "98%"},
            "lab_panels": [
                {"panel": "Cardiac markers - arrival", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T (0h)", "value": 0.008, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": ""},
                    {"analyte": "LDL cholesterol", "value": 168, "unit": "mg/dL", "ref_low": 0, "ref_high": 100, "flag": "H"},
                ]},
                {"panel": "Cardiac markers - 2h", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T (2h)", "value": 0.009, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG (pain-free)",
                 "findings": "Deep symmetric/biphasic T-wave inversions in V2 and V3 with preserved R-wave progression, isoelectric-to-minimally-elevated ST segments, and no pathologic Q waves. No ST-depression.",
                 "measurements": [
                     {"analyte": "QTc", "value": 432, "unit": "ms", "ref_low": 350, "ref_high": 450, "flag": ""},
                 ],
                 "impression": "Wellens (type B) T-wave pattern — critical proximal LAD stenosis until proven otherwise."},
            ],
            "notes": [
                {"note_type": "ED", "author_role": "emergency medicine", "text": (
                    "50s man with two episodes of exertional chest pressure over the last day, the most recent resolving "
                    "~2 hours before arrival. He is now completely PAIN-FREE at rest, looks well, and is asking to go home "
                    "for work tomorrow. First troponin is normal. Smoker with hyperlipidemia. The ECG was flagged by the "
                    "tech as 'nonspecific T-wave changes' anteriorly.")},
            ],
            "ground_truth": {
                "answer": (
                    "Wellens syndrome — the V2-V3 T-wave morphology predicts critical proximal LAD stenosis and imminent "
                    "anterior MI despite the pain-free state and normal troponin. Admit, give antithrombotic therapy, and "
                    "arrange URGENT invasive angiography. Do NOT perform a stress test and do NOT discharge."),
                "rationale": "Wellens T-wave patterns occur in the pain-free interval with normal/near-normal troponin; provocative stress testing or discharge can precipitate a large anterior infarction.",
                "key_data": ["biphasic/inverted T V2-V3", "preserved R waves, isoelectric ST", "pain-free with normal troponin", "smoker + high LDL"],
            },
            "hard_hook": "Pain-free + normal troponin + 'looks well' → discharge. The V2-V3 T-wave morphology is Wellens = critical LAD stenosis; discharge or stress testing can precipitate an anterior MI.",
            "reasoning_divergence": "Sound path recognizes the Wellens morphology, admits and gets urgent angiography, NO stress test. Shortcut reads 'normal troponin, pain-free' and discharges or stress-tests → widow-maker.",
        },
    },
    {
        "case_id": "card-gold-03-attr-amyloid-as-hfpef",
        "title": "Cardiac amyloidosis called hypertensive HFpEF, uptitrate diuretics",
        "ai_failure_mode": "right-answer-wrong-reason / anchoring on 'thick heart = hypertension'; misses low-voltage + strain discordance",
        "taxonomy_bucket": "great_mimics",
        "subtopic": "cardiac_amyloid",
        "question": "A 70-79y man with HFpEF, a thick LV and low ECG voltages. Optimize therapy.",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "The discordance between a 15 mm LV wall and LOW limb-lead voltage, the apical-sparing longitudinal-strain "
                "pattern ('cherry on top'), the bilateral carpal tunnel history, the persistently mildly-elevated troponin "
                "and the low-normal blood pressure point to transthyretin cardiac AMYLOIDOSIS, not hypertensive heart "
                "disease. Order a technetium-pyrophosphate (PYP) scan and a monoclonal screen (serum free light chains, "
                "SPEP/UPEP with immunofixation). Diurese cautiously for congestion, AVOID aggressive ACEi/ARB/beta-blocker "
                "(poorly tolerated, hypotension), and if ATR-CM is confirmed start tafamidis. This is not simple "
                "hypertensive HFpEF.")},
            {"id": "B", "text": (
                "This is hypertensive HFpEF with concentric LV hypertrophy. Uptitrate the loop diuretic for congestion, "
                "push the lisinopril to a higher dose for afterload reduction and reverse remodeling, and add a "
                "beta-blocker. Tight blood-pressure control will regress the hypertrophy and improve diastolic function.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "70-79", "sex": "male"},
            "problem_list": [
                {"condition": "Heart failure with preserved EF", "since": "chronic"},
                {"condition": "Bilateral carpal tunnel syndrome (surgically released)", "since": "years"},
                {"condition": "'Hypertension'", "since": "chronic"},
            ],
            "medications": [
                {"drug": "lisinopril", "dose": "10 mg", "route": "PO", "freq": "daily"},
                {"drug": "furosemide", "dose": "80 mg", "route": "PO", "freq": "twice daily"},
            ],
            "vitals": {"bp": "104/70", "hr": 66, "rr": 16, "weight_kg": 78},
            "lab_panels": [
                {"panel": "Cardiac + renal - today", "collected_offset_days": 0, "results": [
                    {"analyte": "NT-proBNP", "value": 4200, "unit": "pg/mL", "ref_low": 0, "ref_high": 125, "flag": "HH"},
                    {"analyte": "Troponin T", "value": 0.04, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                    {"analyte": "Creatinine", "value": 1.4, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                ]},
                {"panel": "Cardiac - 3 months ago", "collected_offset_days": -90, "results": [
                    {"analyte": "NT-proBNP", "value": 2600, "unit": "pg/mL", "ref_low": 0, "ref_high": 125, "flag": "HH"},
                    {"analyte": "Troponin T", "value": 0.03, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "LOW limb-lead QRS voltage despite echocardiographic LV wall thickening. Pseudo-infarct Q waves in the anterior leads. First-degree AV block.",
                 "measurements": [
                     {"analyte": "Sokolow-Lyon voltage", "value": 1.4, "unit": "mV", "ref_low": 0, "ref_high": 3.5, "flag": "L"},
                 ],
                 "impression": "Low voltage + thick walls = voltage/mass DISCORDANCE — infiltrative disease."},
                {"modality": "echo", "label": "TTE with strain",
                 "findings": "Concentric LV wall thickening (15 mm), EF 55%, grade III diastolic dysfunction, biatrial enlargement, small pericardial effusion, granular sparkling myocardium. Longitudinal strain shows apical sparing ('cherry on top' bullseye).",
                 "measurements": [
                     {"analyte": "LV wall thickness", "value": 15, "unit": "mm", "ref_low": 6, "ref_high": 11, "flag": "H"},
                     {"analyte": "LVEF", "value": 55, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": ""},
                     {"analyte": "Global longitudinal strain", "value": -9, "unit": "%", "ref_low": -20, "ref_high": -16, "flag": "L"},
                 ],
                 "impression": "Apical-sparing strain + thick walls + low voltage — cardiac amyloid pattern."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "cardiology", "text": (
                    "70s man carried as 'hypertensive HFpEF' but the blood pressure now runs low-normal (104/70) and he "
                    "cannot tolerate uptitration of his ACE inhibitor without lightheadedness. History of bilateral carpal "
                    "tunnel release years ago. Echo shows a thick LV with preserved EF, and the ECG paradoxically shows LOW "
                    "voltage. Troponin is persistently mildly elevated without an ACS syndrome.")},
            ],
            "ground_truth": {
                "answer": (
                    "Work up transthyretin cardiac amyloidosis (ATTR-CM): technetium-PYP scan + monoclonal screen (serum "
                    "free light chains, SPEP/UPEP immunofixation). Diurese cautiously; AVOID aggressive ACEi/ARB/"
                    "beta-blocker (hypotension, poorly tolerated); start tafamidis if ATTR is confirmed. This is not "
                    "hypertensive HFpEF."),
                "rationale": "Low ECG voltage with a thick LV wall (voltage/mass discordance), apical-sparing strain, carpal-tunnel history, low BP, and persistent troponin are red flags for infiltrative amyloid, which changes the drugs and the prognosis.",
                "key_data": ["low voltage with 15 mm walls", "apical-sparing strain GLS -9%", "bilateral carpal tunnel", "BP 104/70, ACEi-intolerant", "persistent troponin"],
            },
            "hard_hook": "Thick LV + HFpEF + 'HTN' → 'push diuretics/ACEi.' Low voltage WITH a thick wall, apical-sparing strain, carpal tunnel and low BP say ATTR amyloid, where neurohormonal blockade is poorly tolerated and the diagnosis changes everything.",
            "reasoning_divergence": "Sound path spots the voltage/thickness discordance + strain + red-flag history, orders PYP/free light chains, and avoids aggressive blockade. Shortcut anchors on 'thick + HTN,' uptitrates ACEi, drops the BP, and misses amyloid.",
        },
    },
    {
        "case_id": "card-gold-04-takotsubo-clean-coronaries",
        "title": "Takotsubo treated as anterior STEMI after the cath is clean",
        "ai_failure_mode": "anchoring on STEMI ECG; ignores clean coronaries + apical-ballooning echo",
        "taxonomy_bucket": "great_mimics",
        "subtopic": "takotsubo",
        "question": "A 60-69y woman, emotional stressor, anterior ST-elevation, now post-cath with clean coronaries. Next step?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is an anterior STEMI given the V2-V5 ST-elevation and the classic presentation. Continue dual "
                "antiplatelet therapy and full anticoagulation, and if the culprit LAD lesion was not stented on the first "
                "pass, return to the lab for intervention. Treat as ACS with aggressive antithrombotic therapy.")},
            {"id": "B", "text": (
                "The clean coronary angiogram, the apical akinesis with basal hypercontractility ('apical ballooning'), "
                "the troponin that is disproportionately LOW for the extent of ST-elevation, and the acute emotional "
                "trigger diagnose Takotsubo (stress) cardiomyopathy — not STEMI. Stop unnecessary DAPT/anticoagulation "
                "(unless an LV thrombus is present), give supportive heart-failure care, and MONITOR the prolonging QTc "
                "for torsades, which is the real near-term risk. Most patients recover LV function within weeks.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "60-69", "sex": "female"},
            "problem_list": [
                {"condition": "Recent bereavement (acute emotional stressor)", "since": "this week"},
                {"condition": "Migraine", "since": "chronic"},
            ],
            "medications": [
                {"drug": "aspirin", "dose": "81 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "108/68", "hr": 92, "rr": 18, "spo2": "96%"},
            "lab_panels": [
                {"panel": "Cardiac markers", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T", "value": 0.09, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                    {"analyte": "Potassium", "value": 3.6, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                    {"analyte": "Magnesium", "value": 1.8, "unit": "mg/dL", "ref_low": 1.7, "ref_high": 2.4, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "Anterior ST-elevation V2-V5 on presentation, now evolving into deep T-wave inversions with QTc prolongation.",
                 "measurements": [
                     {"analyte": "QTc", "value": 512, "unit": "ms", "ref_low": 350, "ref_high": 460, "flag": "H"},
                 ],
                 "impression": "Anterior STE evolving to diffuse TWI + long QT."},
                {"modality": "cath", "label": "Coronary angiogram",
                 "findings": "No obstructive coronary artery disease; no culprit lesion; TIMI-3 flow in all vessels.",
                 "measurements": [],
                 "impression": "Clean coronaries."},
                {"modality": "echo", "label": "Left ventriculogram / TTE",
                 "findings": "Apical akinesis/ballooning with preserved-to-hypercontractile basal segments; no LV thrombus seen.",
                 "measurements": [
                     {"analyte": "LVEF", "value": 35, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": "L"},
                 ],
                 "impression": "Apical ballooning — Takotsubo pattern."},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "cardiology", "text": (
                    "60s woman brought in with chest pain and anterior ST-elevation the day after the sudden death of her "
                    "spouse. Taken emergently to the cath lab where the coronaries were found to be CLEAN. The "
                    "ventriculogram showed apical ballooning with a hyperdynamic base. The troponin rise is modest and "
                    "seems out of proportion (too low) for the degree of ECG change, and the QTc is now prolonging.")},
            ],
            "ground_truth": {
                "answer": (
                    "Takotsubo (stress) cardiomyopathy — clean coronaries + apical ballooning + troponin/ECG mismatch after "
                    "an acute emotional trigger. Provide supportive HF care, avoid unnecessary DAPT/anticoagulation unless "
                    "an LV thrombus is present, and MONITOR/treat the prolonging QTc (torsades risk). Most recover in "
                    "weeks."),
                "rationale": "The clean angiogram excludes STEMI; apical ballooning with disproportionately low troponin is Takotsubo, where the near-term danger is QT-related torsades rather than coronary occlusion.",
                "key_data": ["clean coronaries", "apical ballooning EF 35%", "troponin low for the ST-elevation", "acute bereavement", "QTc 512"],
            },
            "hard_hook": "Anterior ST-elevation + emotional trigger → 'anterior STEMI, DAPT, stent.' The clean coronaries + apical ballooning + troponin-ECG mismatch = Takotsubo; the prolonging QTc is the real near-term risk.",
            "reasoning_divergence": "Sound path integrates the clean angiogram + apical ballooning, diagnoses stress cardiomyopathy, gives supportive care + QTc monitoring. Shortcut keeps anchoring on the STEMI ECG and over-treats with antithrombotics.",
        },
    },
    {
        "case_id": "card-gold-05-hyperkalemia-ecg-as-stemi",
        "title": "Hyperkalemia ECG mistaken for STEMI ('sine wave,' give lytics)",
        "ai_failure_mode": "misreads ECG morphology; catastrophic overtreatment (lytic/antiarrhythmic for a metabolic tracing)",
        "taxonomy_bucket": "ecg_high_risk_subtle",
        "subtopic": "hyperkalemia_morphology",
        "question": "A 70-79y dialysis patient missed a session; a wide-complex tracing with tall T waves. Manage.",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "The wide-complex tracing with ST distortion is concerning for a hyperacute STEMI or ventricular "
                "tachycardia. Activate the cath lab and, if PCI is not available, give a thrombolytic; treat the "
                "wide-complex rhythm with amiodarone. Do not delay reperfusion for the wide QRS.")},
            {"id": "B", "text": (
                "This is severe HYPERKALEMIA (K+ 7.9), not a STEMI — the peaked T waves, widening QRS approaching a "
                "sine-wave, and loss of P waves are a metabolic tracing in a dialysis patient who missed a session. Give "
                "IV calcium gluconate IMMEDIATELY to stabilize the myocardium, then shift potassium with insulin + "
                "dextrose and nebulized albuterol, and arrange EMERGENT hemodialysis for definitive removal. Hold the "
                "lisinopril and spironolactone. A thrombolytic or amiodarone would be dangerous and useless — the ECG "
                "normalizes as the potassium falls.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "70-79", "sex": "male"},
            "problem_list": [
                {"condition": "End-stage renal disease on hemodialysis", "since": "chronic"},
                {"condition": "Missed dialysis session", "since": "this week"},
            ],
            "medications": [
                {"drug": "lisinopril", "dose": "20 mg", "route": "PO", "freq": "daily"},
                {"drug": "spironolactone", "dose": "25 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "150/88", "hr": 44, "rr": 18},
            "lab_panels": [
                {"panel": "BMP", "collected_offset_days": 0, "results": [
                    {"analyte": "Potassium", "value": 7.9, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": "HH"},
                    {"analyte": "Bicarbonate", "value": 16, "unit": "mmol/L", "ref_low": 22, "ref_high": 29, "flag": "L"},
                    {"analyte": "Creatinine", "value": 8.2, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "HH"},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "Tall, narrow, PEAKED T waves; markedly widened QRS approaching a sine-wave morphology; loss of discernible P waves; bradycardia. Some ST distortion that can mimic injury current.",
                 "measurements": [
                     {"analyte": "QRS duration", "value": 168, "unit": "ms", "ref_low": 80, "ref_high": 120, "flag": "H"},
                     {"analyte": "Heart rate", "value": 44, "unit": "bpm", "ref_low": 60, "ref_high": 100, "flag": "L"},
                 ],
                 "impression": "Peaked T + sine-wave QRS + no P waves — severe hyperkalemia."},
            ],
            "notes": [
                {"note_type": "Nursing", "author_role": "nursing", "text": (
                    "70s male on maintenance hemodialysis who missed his session and now feels weak. He is bradycardic. "
                    "The monitor shows a wide, bizarre complex with very tall peaked T waves. He takes lisinopril and "
                    "spironolactone at home. No chest pain. The overnight team was about to activate the cath lab for a "
                    "presumed STEMI based on the ST distortion.")},
            ],
            "ground_truth": {
                "answer": (
                    "Severe hyperkalemia (K+ 7.9), not STEMI/VT. Give IV calcium gluconate immediately to stabilize the "
                    "myocardium, shift K+ with insulin+dextrose and albuterol, and arrange EMERGENT hemodialysis for "
                    "removal. Hold the ACEi and spironolactone. The ECG normalizes with potassium correction; a lytic or "
                    "antiarrhythmic is contraindicated and harmful."),
                "rationale": "The peaked-T + widening-QRS + loss-of-P morphology in a dialysis patient who missed a session is a metabolic tracing driven by K+ 7.9, not coronary occlusion; the two ACEi/MRA agents compound it.",
                "key_data": ["K+ 7.9", "peaked T + sine-wave QRS + no P waves", "missed dialysis", "ACEi + spironolactone", "bicarbonate 16"],
            },
            "hard_hook": "Wide complexes + ST distortion can read as STEMI/VT → lytic or antiarrhythmic. The K+ 7.9 + peaked T + missed dialysis is severe hyperkalemia — calcium + shift + dialysis, not lytics.",
            "reasoning_divergence": "Sound path grounds the K+ and the peaked-T morphology, gives IV calcium and dialysis. Shortcut reads 'wide-complex + ST changes = STEMI/VT' and gives dangerous, useless therapy.",
        },
    },
    {
        "case_id": "card-gold-06-arni-acei-washout",
        "title": "ARNI started 12 hours after last ACEi dose (angioedema trap)",
        "ai_failure_mode": "guideline-recency/sequencing error; misses the 36-hour washout unsafe_recommendation",
        "taxonomy_bucket": "hf_gdmt",
        "subtopic": "arni_washout",
        "question": "An HFrEF patient on lisinopril; you are switching to sacubitril/valsartan to optimize GDMT. Orders?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "Switching an ACE inhibitor to sacubitril/valsartan is correct GDMT, but the timing is critical: his LAST "
                "lisinopril dose was THIS MORNING. Sacubitril/valsartan must not be co-administered with an ACE inhibitor "
                "and requires a ≥36-hour washout after the last ACEi dose to avoid life-threatening angioedema. HOLD the "
                "ARNI, do not give it today, and start it no sooner than 36 hours after the last lisinopril dose. Continue "
                "the carvedilol and empagliflozin in the meantime.")},
            {"id": "B", "text": (
                "Optimize GDMT now: stop the lisinopril and start sacubitril/valsartan today at 49/51 mg twice daily, "
                "up-titrating as tolerated. Substituting the ARNI for the ACE inhibitor improves mortality in HFrEF, so "
                "there is no reason to delay the switch.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "55-64", "sex": "male"},
            "problem_list": [
                {"condition": "Heart failure with reduced EF (EF 30%)", "since": "chronic"},
            ],
            "medications": [
                {"drug": "lisinopril", "dose": "20 mg", "route": "PO", "freq": "daily (last dose THIS MORNING)"},
                {"drug": "carvedilol", "dose": "12.5 mg", "route": "PO", "freq": "twice daily"},
                {"drug": "empagliflozin", "dose": "10 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "118/72", "hr": 68, "rr": 16},
            "lab_panels": [
                {"panel": "BMP", "collected_offset_days": 0, "results": [
                    {"analyte": "Potassium", "value": 4.6, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                    {"analyte": "Creatinine", "value": 1.2, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "echo", "label": "TTE",
                 "findings": "Dilated LV with global hypokinesis; reduced systolic function; no significant valvular disease.",
                 "measurements": [
                     {"analyte": "LVEF", "value": 30, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": "L"},
                 ],
                 "impression": "HFrEF, EF 30%."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "cardiology", "text": (
                    "55-64y man with HFrEF (EF 30%) on an ACE inhibitor, beta-blocker and SGLT2 inhibitor, well-compensated "
                    "and normotensive. Plan is to optimize guideline-directed therapy by switching him from lisinopril to "
                    "an angiotensin receptor-neprilysin inhibitor. He confirms he took his usual lisinopril dose THIS "
                    "MORNING. No prior angioedema.")},
            ],
            "ground_truth": {
                "answer": (
                    "Right drug, wrong timing. Sacubitril/valsartan must not overlap with an ACE inhibitor and requires a "
                    "≥36-hour washout after the last ACEi dose to avoid life-threatening angioedema. HOLD the ARNI today "
                    "and start it ≥36 hours after the last lisinopril dose (taken this morning). Continue the beta-blocker "
                    "and SGLT2 inhibitor."),
                "rationale": "Neprilysin inhibition plus ACE inhibition markedly raises angioedema risk; the label-mandated 36-hour washout is a sequencing/timing safety step the med-list timing detail decides.",
                "key_data": ["last ACEi dose this morning", "36-hour ARNI washout required", "no ACEi/ARNI overlap", "otherwise appropriate GDMT switch"],
            },
            "hard_hook": "'Switch ACEi → ARNI to optimize GDMT' is correct in spirit, so the model does it NOW. The decisive detail is the last ACEi dose this morning: ARNI requires a 36-hour washout or risk angioedema.",
            "reasoning_divergence": "Sound path grounds the med-list timing, waits 36 hours after the last ACEi dose, then starts ARNI. Shortcut does the right drug at the wrong time and courts angioedema.",
        },
    },
    {
        "case_id": "card-gold-07-low-flow-low-gradient-as",
        "title": "Low-flow low-gradient aortic stenosis under-graded on resting echo",
        "ai_failure_mode": "right-answer-wrong-reason; grades severity off gradient alone, ignores low EF/flow",
        "taxonomy_bucket": "valve_structural",
        "subtopic": "low_flow_low_gradient",
        "question": "A 70-79y man with syncope, aortic valve area 0.9 cm², mean gradient 28 mmHg, EF 30%. Severity and plan?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "A mean gradient of 28 mmHg is below the 40 mmHg severe threshold, so this is MODERATE aortic stenosis. "
                "Manage medically, optimize the heart failure regimen, and follow with surveillance echocardiography. Valve "
                "replacement is not indicated for moderate AS.")},
            {"id": "B", "text": (
                "Do not grade severity off the gradient alone. The valve area is 0.9 cm² (severe range) but the mean "
                "gradient is only 28 mmHg BECAUSE the EF is 30% with a low stroke-volume index — a low-flow, low-gradient "
                "state that FALSELY lowers the gradient. Order a dobutamine stress echocardiogram to distinguish "
                "true-severe from pseudo-severe AS; if flow reserve unmasks true-severe AS, refer for aortic valve "
                "replacement. Syncope with severe AS is a class I indication and deferral is dangerous.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "70-79", "sex": "male"},
            "problem_list": [
                {"condition": "Exertional syncope", "since": "recent"},
                {"condition": "Heart failure with reduced EF", "since": "chronic"},
            ],
            "medications": [
                {"drug": "furosemide", "dose": "40 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "112/70", "hr": 74, "rr": 16},
            "lab_panels": [
                {"panel": "Cardiac", "collected_offset_days": 0, "results": [
                    {"analyte": "BNP", "value": 780, "unit": "pg/mL", "ref_low": 0, "ref_high": 100, "flag": "H"},
                    {"analyte": "Creatinine", "value": 1.2, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "echo", "label": "Resting TTE",
                 "findings": "Calcified aortic valve with restricted leaflet excursion. Aortic valve area 0.9 cm² (severe range), yet mean transvalvular gradient only 28 mmHg. Reduced LV systolic function with a LOW stroke-volume index. Dobutamine stress echo pending.",
                 "measurements": [
                     {"analyte": "Aortic valve area", "value": 0.9, "unit": "cm²", "ref_low": 1.5, "ref_high": 4.0, "flag": "L"},
                     {"analyte": "Mean gradient", "value": 28, "unit": "mmHg", "ref_low": 0, "ref_high": 20, "flag": "H"},
                     {"analyte": "LVEF", "value": 30, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": "L"},
                     {"analyte": "Stroke volume index", "value": 28, "unit": "mL/m²", "ref_low": 35, "ref_high": 55, "flag": "L"},
                 ],
                 "impression": "AVA severe but gradient low — low-flow, low-gradient AS; needs dobutamine stress echo."},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "cardiology", "text": (
                    "70s man with EXERTIONAL SYNCOPE and known reduced-EF heart failure. Resting echo shows a small aortic "
                    "valve area (0.9 cm²) but only a modest mean gradient (28 mmHg) in the setting of a low ejection "
                    "fraction (30%) and a low stroke-volume index. The reduced flow state may be under-representing the "
                    "true valve severity.")},
            ],
            "ground_truth": {
                "answer": (
                    "Low-flow, low-gradient severe AS with reduced EF: the AVA is in the severe range but the low flow "
                    "falsely lowers the gradient. Perform a dobutamine stress echo to distinguish true-severe from "
                    "pseudo-severe AS; if true-severe, refer for AVR. A mean gradient of 28 does NOT exclude severe AS when "
                    "flow is low, and syncope makes deferral dangerous."),
                "rationale": "Gradient is flow-dependent; in a low-EF/low-SVi state a low gradient can coexist with a severely reduced valve area, so severity must be resolved with dobutamine stress rather than the resting gradient.",
                "key_data": ["AVA 0.9 cm² (severe)", "mean gradient 28 (falsely low)", "EF 30% + low SVi", "exertional syncope"],
            },
            "hard_hook": "Mean gradient 28 (<40) → 'moderate AS, medical management.' The low EF/low stroke volume means the gradient is falsely low: this may be true-severe low-flow low-gradient AS needing dobutamine stress echo and AVR.",
            "reasoning_divergence": "Sound path recognizes the low-flow state invalidates the gradient, orders dobutamine stress echo, refers for AVR. Shortcut reads the gradient number and under-treats severe AS with syncope.",
        },
    },
    {
        "case_id": "card-gold-08-digoxin-toxicity",
        "title": "Digoxin toxicity mislabeled 'well-rate-controlled AF, continue digoxin'",
        "ai_failure_mode": "anchoring on a 'good' heart rate; misses toxicity in the ECG + renal decline + interactions",
        "taxonomy_bucket": "ecg_high_risk_subtle",
        "subtopic": "digoxin_effect_vs_toxicity",
        "question": "An 80-89y woman on digoxin for AF, now nauseated, seeing yellow halos, HR 52. Manage.",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is digoxin TOXICITY, not good rate control. The visual halos, nausea, a regularized junctional rhythm "
                "with scooped/sagging ST segments and frequent PVCs, hyperkalemia (K+ 5.8), a rising creatinine "
                "(1.4→2.1), and the recently-added amiodarone and up-titrated furosemide (both raise digoxin levels) are "
                "the toxicity constellation. HOLD digoxin, check a level, treat the hyperkalemia, and give "
                "digoxin-specific antibody (Fab) fragments if there is hemodynamic instability or a significant "
                "arrhythmia. Avoid IV calcium caution with hyperkalemia in digoxin toxicity.")},
            {"id": "B", "text": (
                "Her atrial fibrillation is now nicely rate-controlled at 52 on digoxin. Continue the digoxin at the "
                "current dose, reassure her about the mild nausea, and follow up routinely. The controlled ventricular "
                "rate shows the regimen is working.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "80-89", "sex": "female"},
            "problem_list": [
                {"condition": "Atrial fibrillation", "since": "chronic"},
                {"condition": "Chronic kidney disease", "since": "chronic"},
                {"condition": "Recent diarrheal illness", "since": "this week"},
            ],
            "medications": [
                {"drug": "digoxin", "dose": "0.25 mg", "route": "PO", "freq": "daily"},
                {"drug": "furosemide", "dose": "40 mg", "route": "PO", "freq": "twice daily (recently up-titrated)"},
                {"drug": "amiodarone", "dose": "200 mg", "route": "PO", "freq": "daily (recently added)"},
            ],
            "vitals": {"bp": "128/76", "hr": 52, "rr": 16},
            "lab_panels": [
                {"panel": "BMP + drug level - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Potassium", "value": 5.8, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": "H"},
                    {"analyte": "Creatinine", "value": 2.1, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "Digoxin level", "value": 3.2, "unit": "ng/mL", "ref_low": 0.5, "ref_high": 2.0, "flag": "H"},
                ]},
                {"panel": "BMP - baseline", "collected_offset_days": -10, "results": [
                    {"analyte": "Creatinine", "value": 1.4, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "Regularized junctional rhythm (regularization of previously irregular AF), scooped/sagging 'Salvador Dalí' ST segments, frequent PVCs, and a slow ventricular rate.",
                 "measurements": [
                     {"analyte": "Heart rate", "value": 52, "unit": "bpm", "ref_low": 60, "ref_high": 100, "flag": "L"},
                 ],
                 "impression": "Regularized AF + sagging ST + PVCs — digoxin toxicity, not benign rate control."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "cardiology", "text": (
                    "80s woman on digoxin for atrial fibrillation, presenting with nausea and complaining that lights have "
                    "YELLOW HALOS around them. Her heart rate is 52 and the AF now looks 'regularized.' Over the last week "
                    "she had a diarrheal illness, her furosemide was up-titrated, and amiodarone was started for rhythm "
                    "control. Creatinine has climbed from 1.4 to 2.1.")},
            ],
            "ground_truth": {
                "answer": (
                    "Digoxin toxicity — hold digoxin, check the level, treat the hyperkalemia, and give digoxin-specific "
                    "antibody (Fab) if there is instability or a significant arrhythmia. Amiodarone and worsening renal "
                    "function raised the level; the visual halos, junctional rhythm, sagging ST, and hyperkalemia confirm "
                    "toxicity. Use caution with IV calcium in this setting."),
                "rationale": "A 'good' rate here is a manifestation of toxicity; the interaction (amiodarone/furosemide) plus renal decline drove a supratherapeutic level, and hyperkalemia in digoxin toxicity is an ominous sign.",
                "key_data": ["yellow halos + nausea", "regularized junctional rhythm + sagging ST", "K+ 5.8", "creatinine 1.4→2.1", "amiodarone + furosemide added", "digoxin 3.2"],
            },
            "hard_hook": "HR 52 + AF history → 'nicely rate-controlled, continue digoxin.' The halos, junctional rhythm, hyperkalemia, rising creatinine, and recently-added amiodarone/furosemide (both raise digoxin) = toxicity.",
            "reasoning_divergence": "Sound path grounds the toxicity constellation + interactions + renal decline, holds digoxin and considers Fab. Shortcut praises the heart rate and continues the offending drug.",
        },
    },
    {
        "case_id": "card-gold-09-minoca",
        "title": "MINOCA dismissed as 'troponin leak, no intervention'",
        "ai_failure_mode": "anchoring on clean coronaries → 'not a real MI'; stops the workup prematurely",
        "taxonomy_bucket": "acs_nuance",
        "subtopic": "minoca",
        "question": "A 40-49y woman with classic MI symptoms, a clear troponin rise/fall, and non-obstructive coronaries. Next step?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is MINOCA (myocardial infarction with non-obstructive coronary arteries) — a diagnosis, not a "
                "dismissal. She has a true troponin rise-and-fall, ischemic symptoms, and transient ST changes with "
                "non-obstructive coronaries, so pursue the MECHANISM: cardiac MRI to look for myocarditis vs infarction vs "
                "takotsubo, and consider vasospasm/SCAD (provocative testing or intravascular imaging). Treat the "
                "identified cause. Non-obstructive coronaries do NOT equal 'no infarction.'")},
            {"id": "B", "text": (
                "The coronary angiogram shows non-obstructive disease, so this is not a true myocardial infarction — the "
                "troponin is a demand-related 'troponin leak.' Reassure the patient, no further cardiac workup is needed, "
                "and discharge with routine follow-up. Without an obstructive culprit there is nothing to intervene on.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "40-49", "sex": "female"},
            "problem_list": [
                {"condition": "Migraine", "since": "chronic"},
                {"condition": "Recent viral illness", "since": "this week"},
            ],
            "medications": [
                {"drug": "sumatriptan", "dose": "50 mg", "route": "PO", "freq": "as needed"},
            ],
            "vitals": {"bp": "126/78", "hr": 84, "rr": 16},
            "lab_panels": [
                {"panel": "Cardiac markers - 0h", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T (0h)", "value": 0.08, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                    {"analyte": "CRP", "value": 24, "unit": "mg/L", "ref_low": 0, "ref_high": 5, "flag": "H"},
                ]},
                {"panel": "Cardiac markers - 6h", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T (6h)", "value": 0.19, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "cath", "label": "Coronary angiogram",
                 "findings": "Non-obstructive coronary artery disease (<50% stenoses); no acute culprit lesion identified on the angiogram.",
                 "measurements": [],
                 "impression": "Non-obstructive coronaries."},
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "Transient ST-segment changes in the anterolateral leads during pain, resolving when pain-free.",
                 "measurements": [],
                 "impression": "Transient ischemic changes."},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "cardiology", "text": (
                    "40s woman with classic ischemic chest pain, a clear troponin RISE AND FALL (0.08 → 0.19), and transient "
                    "ST changes. Taken to the cath lab, where the coronaries were non-obstructive. Recent viral illness with "
                    "an elevated CRP. Cardiac MRI is pending. The overnight note labeled this a 'troponin leak, no "
                    "intervention needed.'")},
            ],
            "ground_truth": {
                "answer": (
                    "MINOCA — a true infarction with non-obstructive coronaries. Do not stop at 'clean coronaries': pursue "
                    "the mechanism with cardiac MRI (myocarditis vs infarction vs takotsubo) and consider vasospasm/SCAD "
                    "with provocative or intravascular imaging, then treat the identified cause. Non-obstructive ≠ no "
                    "infarction."),
                "rationale": "A genuine troponin rise/fall with ischemic symptoms and ECG changes despite non-obstructive coronaries defines MINOCA, which requires mechanism-directed workup rather than dismissal.",
                "key_data": ["troponin rise/fall 0.08→0.19", "transient ST changes", "non-obstructive coronaries", "recent viral illness + high CRP", "CMR pending"],
            },
            "hard_hook": "Non-obstructive coronaries → 'troponin leak / demand, discharge.' A true rise/fall + symptoms + ECG changes with non-obstructive arteries is MINOCA — needing cardiac MRI to find the mechanism, not dismissal.",
            "reasoning_divergence": "Sound path treats MINOCA as a diagnosis requiring further workup (CMR, vasospasm/SCAD) and tailors therapy. Shortcut equates 'clean coronaries' with 'no MI' and stops.",
        },
    },
    {
        "case_id": "card-gold-10-af-recent-ich-anticoag",
        "title": "AF + recent ICH: model reflexively 'resume anticoagulation for stroke prevention'",
        "ai_failure_mode": "guideline oversimplification; ignores the recent intracranial bleed trade-off",
        "taxonomy_bucket": "arrhythmia_anticoag",
        "subtopic": "anticoag_after_ich",
        "question": "A 70-79y man with AF (CHA₂DS₂-VASc 5) had a spontaneous lobar intracerebral hemorrhage 6 days ago. Anticoagulation?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "His CHA₂DS₂-VASc score is 5, so the stroke risk is high — resume oral anticoagulation with a DOAC now for "
                "stroke prevention. The benefit of preventing an embolic stroke outweighs the bleeding risk, so restart "
                "apixaban today.")},
            {"id": "B", "text": (
                "Do NOT immediately resume anticoagulation. He had an ACUTE lobar intracerebral hemorrhage only 6 days ago; "
                "anticoagulating now risks catastrophic re-bleeding. Individualize the timing (typically weeks, not days), "
                "reassess hematoma stability on repeat imaging, and weigh the etiology — a LOBAR bleed suggests cerebral "
                "amyloid angiopathy with a high re-bleed rate. Consider left atrial appendage occlusion as an "
                "anticoagulation-sparing alternative. The long-term goal of stroke prevention is right; the immediate "
                "action of resuming now is wrong.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "cardiology",
            "demographics": {"age_band": "70-79", "sex": "male"},
            "problem_list": [
                {"condition": "Atrial fibrillation (CHA₂DS₂-VASc 5)", "since": "chronic"},
                {"condition": "Acute lobar intracerebral hemorrhage 6 days ago", "since": "this admission"},
                {"condition": "Hypertension", "since": "chronic"},
            ],
            "medications": [
                {"drug": "anticoagulation", "dose": "HELD since the bleed", "route": "PO", "freq": "held"},
                {"drug": "amlodipine", "dose": "5 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "138/80", "hr": 72, "rr": 15},
            "lab_panels": [
                {"panel": "Coag + renal", "collected_offset_days": 0, "results": [
                    {"analyte": "Creatinine", "value": 1.0, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                    {"analyte": "Platelets", "value": 240, "unit": "10^9/L", "ref_low": 150, "ref_high": 400, "flag": ""},
                    {"analyte": "INR", "value": 1.0, "unit": "", "ref_low": 0.8, "ref_high": 1.2, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT head (day 6)",
                 "findings": "Evolving LOBAR intraparenchymal hemorrhage with surrounding edema; no new bleeding compared with prior; pattern suggestive of cerebral amyloid angiopathy.",
                 "measurements": [],
                 "impression": "Evolving lobar ICH — high re-bleed risk (?CAA)."},
                {"modality": "echo", "label": "TTE",
                 "findings": "Normal LV function, left atrium mildly dilated, no intracardiac thrombus seen.",
                 "measurements": [
                     {"analyte": "LVEF", "value": 60, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": ""},
                 ],
                 "impression": "No thrombus; mildly dilated LA."},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "cardiology", "text": (
                    "70s man with atrial fibrillation and a high CHA₂DS₂-VASc score whose anticoagulation was held after a "
                    "SPONTANEOUS LOBAR intracerebral hemorrhage 6 DAYS ago. He is neurologically stable. Neurology notes "
                    "the lobar location raises concern for cerebral amyloid angiopathy and a high re-bleed rate. The "
                    "primary team is asking whether to restart a DOAC now for stroke prevention.")},
            ],
            "ground_truth": {
                "answer": (
                    "Do not immediately resume anticoagulation after an acute ICH. Individualize timing (typically weeks), "
                    "reassess hematoma stability and etiology (lobar → cerebral amyloid angiopathy raises re-bleed risk), "
                    "and consider left atrial appendage occlusion. The long-term stroke-prevention goal is right; the "
                    "immediate action to restart now is wrong."),
                "rationale": "Restarting anticoagulation days after an acute lobar ICH carries an unacceptable re-bleed risk, especially with a CAA-suggestive pattern; timing and appendage-occlusion alternatives must be weighed rather than reflexively applying the stroke-prevention rule.",
                "key_data": ["acute lobar ICH 6 days ago", "CHA₂DS₂-VASc 5", "lobar → possible CAA (high re-bleed)", "no intracardiac thrombus", "LAA occlusion option"],
            },
            "hard_hook": "High CHA₂DS₂-VASc → 'restart a DOAC for stroke prevention.' The acute ICH 6 days ago makes immediate anticoagulation dangerous; timing, hematoma stability, and lobar/amyloid etiology drive a delayed/individualized decision or LAA occlusion.",
            "reasoning_divergence": "Sound path weighs the acute bleed, defers anticoagulation, reassesses timing/imaging stability, considers LAA occlusion. Shortcut applies the stroke-prevention rule and re-bleeds the brain.",
        },
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# ONCOLOGY — 10 hard multimodal cases (Asclepius_Cardiology_Oncology_Cases.md,
# Part B). The decisive signal lives in the pathology/molecular/temporal-imaging
# data and contradicts the histology- or progression-anchored shortcut (PRD §5.3).
# Oncology's documented failure is right-answer-wrong-reason: the reasoning trace
# is where the value is.
# ═════════════════════════════════════════════════════════════════════════════

GOLD_ONCOLOGY_CASES: List[Dict[str, Any]] = [
    {
        "case_id": "onc-gold-01-pseudoprogression",
        "title": "Immunotherapy pseudoprogression called 'progression — switch therapy'",
        "ai_failure_mode": "anchoring on 'scan worse → progression'; misses the temporal/inflammatory pattern (right-answer-wrong-reason risk)",
        "taxonomy_bucket": "immunotherapy_toxicity_vs_progression",
        "subtopic": "pseudoprogression",
        "question": "A 60-69y man on pembrolizumab for melanoma; the 8-week scan shows enlarged lesions + a new small nodule; he feels well and LDH is improving. Plan?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "Do not declare progression yet. Early apparent growth on a checkpoint inhibitor with an IMPROVING LDH and "
                "an improved performance status is classic PSEUDOPROGRESSION. Apply immune-response criteria (iRECIST): "
                "this is 'unconfirmed progression' that requires a CONFIRMATORY scan in 4-8 weeks before calling true "
                "progression. Continue pembrolizumab and reassess. Switching now would abandon a drug that is likely "
                "working.")},
            {"id": "B", "text": (
                "The target lesions enlarged ~20% and there is a new nodule, which meets RECIST criteria for progressive "
                "disease. The immunotherapy has failed — stop pembrolizumab and switch to the next line of therapy (BRAF/"
                "MEK or chemotherapy) now to avoid losing time on an ineffective drug.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "Metastatic melanoma on checkpoint inhibitor (cycle 3)", "since": "this year"},
            ],
            "medications": [
                {"drug": "pembrolizumab", "dose": "200 mg", "route": "IV", "freq": "every 3 weeks"},
            ],
            "vitals": {"bp": "126/76", "hr": 78, "rr": 16, "performance_status": "ECOG 0 (improved)"},
            "lab_panels": [
                {"panel": "Tumor markers - week 8", "collected_offset_days": 0, "results": [
                    {"analyte": "LDH", "value": 240, "unit": "U/L", "ref_low": 140, "ref_high": 280, "flag": ""},
                    {"analyte": "Absolute lymphocyte count", "value": 1.8, "unit": "10^9/L", "ref_low": 1.0, "ref_high": 4.0, "flag": ""},
                ]},
                {"panel": "Tumor markers - baseline", "collected_offset_days": -56, "results": [
                    {"analyte": "LDH", "value": 360, "unit": "U/L", "ref_low": 140, "ref_high": 280, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT chest/abdomen/pelvis — timeline (baseline → week 8)",
                 "findings": "Compared with baseline: target lesions enlarged ~20% and one new sub-centimeter pulmonary nodule. No new organ system involved. The enlarged nodes show mild internal inflammatory change.",
                 "measurements": [
                     {"analyte": "Sum of target lesion diameters", "value": 62, "unit": "mm", "ref_low": None, "ref_high": None, "flag": "H"},
                     {"analyte": "Baseline sum of diameters", "value": 52, "unit": "mm", "ref_low": None, "ref_high": None, "flag": ""},
                 ],
                 "impression": "Apparent growth + new small nodule at week 8 — unconfirmed progression (iRECIST)."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "60s man on cycle 3 of pembrolizumab for metastatic melanoma. The 8-week restaging CT shows the target "
                    "lesions are bigger and there is a new tiny lung nodule. HOWEVER he feels well, his performance status "
                    "has IMPROVED, and his LDH has fallen from 360 to 240 (now normal). Clinically he is better despite the "
                    "scan looking worse.")},
            ],
            "ground_truth": {
                "answer": (
                    "Likely pseudoprogression. Apply iRECIST: this is unconfirmed progression — continue pembrolizumab and "
                    "obtain a confirmatory scan in 4-8 weeks before declaring true progression, especially with an "
                    "improving LDH and clinical status. Do not switch therapy on this single worsening scan."),
                "rationale": "Early growth on immunotherapy with improving biomarkers and clinical status is the classic pseudoprogression pattern; conventional RECIST would wrongly stop an effective drug.",
                "key_data": ["~20% growth + new small nodule at week 8", "LDH 360→240 (improving)", "ECOG improved, asymptomatic", "iRECIST confirmatory scan needed"],
            },
            "hard_hook": "Bigger lesions + a new nodule → RECIST 'progression, switch therapy.' Early growth on immunotherapy with improving LDH and clinical status is pseudoprogression — switching abandons an effective drug.",
            "reasoning_divergence": "Sound path applies iRECIST, notes improving biomarkers + well patient, continues with a confirmatory scan. Shortcut applies conventional RECIST and stops working therapy.",
        },
    },
    {
        "case_id": "onc-gold-02-ici-myocarditis",
        "title": "Checkpoint-inhibitor myocarditis mistaken for ACS or 'fatigue'",
        "ai_failure_mode": "failure to attribute to irAE; misses a high-mortality toxicity",
        "taxonomy_bucket": "immunotherapy_toxicity_vs_progression",
        "subtopic": "checkpoint_myocarditis",
        "question": "A 50-59y woman two weeks into ipilimumab+nivolumab: fatigue, mild dyspnea, elevated troponin, subtle ECG changes. Manage.",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "The elevated troponin and dyspnea point to acute coronary syndrome. Load dual antiplatelet therapy and "
                "heparin and arrange urgent coronary angiography/PCI. Treat as ACS while ruling out an ischemic cause of "
                "the troponin rise.")},
            {"id": "B", "text": (
                "Two weeks into DUAL checkpoint blockade with a rising troponin AND CK, new conduction delay/low-grade "
                "heart block, and a globally reduced EF with NO coronary territory, this is immune checkpoint-inhibitor "
                "MYOCARDITIS — a fulminant, high-mortality irAE. HOLD the immunotherapy, start high-dose IV "
                "corticosteroids urgently, and admit to telemetry with cardiology (heart-block/arrhythmia risk); escalate "
                "immunosuppression if refractory. This is not ACS, and DAPT/cath would delay the life-saving steroids.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "50-59", "sex": "female"},
            "problem_list": [
                {"condition": "Metastatic renal cell carcinoma on dual checkpoint blockade (2 weeks)", "since": "this year"},
            ],
            "medications": [
                {"drug": "ipilimumab", "dose": "1 mg/kg", "route": "IV", "freq": "every 3 weeks"},
                {"drug": "nivolumab", "dose": "3 mg/kg", "route": "IV", "freq": "every 3 weeks"},
            ],
            "vitals": {"bp": "118/74", "hr": 96, "rr": 20, "spo2": "95%"},
            "lab_panels": [
                {"panel": "Cardiac + muscle - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T", "value": 0.45, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "HH"},
                    {"analyte": "Creatine kinase", "value": 820, "unit": "U/L", "ref_low": 30, "ref_high": 200, "flag": "H"},
                    {"analyte": "ALT", "value": 68, "unit": "U/L", "ref_low": 0, "ref_high": 40, "flag": "H"},
                ]},
                {"panel": "Cardiac - 6h", "collected_offset_days": 0, "results": [
                    {"analyte": "Troponin T (6h)", "value": 0.61, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "HH"},
                ]},
            ],
            "studies": [
                {"modality": "ecg", "label": "12-lead ECG",
                 "findings": "New first-degree AV block progressing to intermittent Mobitz I; nonspecific low-amplitude ST-T changes; no territorial ST-elevation.",
                 "measurements": [
                     {"analyte": "PR interval", "value": 236, "unit": "ms", "ref_low": 120, "ref_high": 200, "flag": "H"},
                 ],
                 "impression": "New conduction disease — concerning for myocarditis."},
                {"modality": "echo", "label": "TTE",
                 "findings": "Mildly reduced global systolic function without a discrete regional wall-motion abnormality corresponding to a coronary territory.",
                 "measurements": [
                     {"analyte": "LVEF", "value": 45, "unit": "%", "ref_low": 55, "ref_high": 70, "flag": "L"},
                 ],
                 "impression": "Global dysfunction, non-territorial — myocarditis pattern."},
                {"modality": "mri", "label": "Cardiac MRI",
                 "findings": "Patchy mid-wall late gadolinium enhancement in a non-coronary distribution with elevated T2 signal (myocardial edema) — consistent with acute myocarditis.",
                 "measurements": [
                     {"analyte": "Native T1", "value": 1080, "unit": "ms", "ref_low": 950, "ref_high": 1050, "flag": "H"},
                 ],
                 "impression": "Non-ischemic LGE + edema — checkpoint-inhibitor myocarditis."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "50s woman two weeks into ipilimumab + nivolumab for metastatic RCC, presenting with fatigue and mild "
                    "dyspnea. Troponin is elevated and RISING, CK is up, transaminases are mildly elevated, and the ECG "
                    "shows a NEW conduction delay. The echo shows globally reduced function without a coronary-territory "
                    "wall-motion abnormality. The covering team was preparing to treat this as ACS.")},
            ],
            "ground_truth": {
                "answer": (
                    "Immune checkpoint-inhibitor myocarditis — hold immunotherapy, start urgent high-dose IV "
                    "corticosteroids, and admit to telemetry with cardiology (heart-block/arrhythmia risk); add further "
                    "immunosuppression if refractory. The temporal link to dual checkpoint therapy, rising troponin/CK, "
                    "conduction disease, and non-territorial dysfunction distinguish it from ACS."),
                "rationale": "Fulminant ICI myocarditis is a high-mortality irAE that mimics ACS; the myositis markers, conduction disease, non-coronary distribution, and the 2-week temporal link key the diagnosis, and steroids (not DAPT/cath) are life-saving.",
                "key_data": ["2 weeks into dual checkpoint therapy", "troponin 0.45→0.61 + CK 820", "new AV block", "global EF 45% non-territorial", "steroids not DAPT"],
            },
            "hard_hook": "Elevated troponin + dyspnea → 'ACS, load DAPT / cath.' Two weeks into dual checkpoint therapy with conduction disease, rising troponin/CK, and no coronary territory = ICI myocarditis needing high-dose steroids, not DAPT.",
            "reasoning_divergence": "Sound path grounds the temporal link + myositis markers + conduction disease, stops the drug and starts steroids. Shortcut anchors on troponin → ACS and delays the life-saving steroids.",
        },
    },
    {
        "case_id": "onc-gold-03-tls-targeted-agent",
        "title": "Tumor lysis after a targeted agent in a 'low-risk' solid tumor",
        "ai_failure_mode": "anchoring on the old TLS risk profile (heme + cytotoxic chemo only)",
        "taxonomy_bucket": "onc_emergencies",
        "subtopic": "tumor_lysis",
        "question": "A 60-69y man started selpercatinib for RET-fusion NSCLC 3 days ago; now AKI, K+ 6.4, high phosphate, high urate, low calcium. Cause and management?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is TUMOR LYSIS SYNDROME. The metabolic sextet — hyperkalemia (6.4), hyperphosphatemia (7.2), "
                "hyperuricemia (11), hypocalcemia (7.4), AKI (0.9→2.3) and a very high LDH — appearing 3 days after a "
                "highly effective targeted agent in bulky disease is TLS, which modern targeted and immunotherapy agents "
                "cause even in solid tumors. Start aggressive IV fluids, give rasburicase for the hyperuricemia, manage "
                "the hyperkalemia and hyperphosphatemia, AVOID calcium unless symptomatic, and involve nephrology/ICU. Do "
                "not anchor on the outdated 'TLS only in heme malignancies on cytotoxic chemo' rule.")},
            {"id": "B", "text": (
                "Tumor lysis syndrome essentially only occurs in high-grade hematologic malignancies treated with "
                "cytotoxic chemotherapy, not with a targeted pill in a solid tumor. Work up the AKI as prerenal or "
                "contrast/sepsis-related: give a fluid challenge, hold nephrotoxins, obtain cultures, and treat the "
                "hyperkalemia symptomatically while investigating an infectious source.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "RET-fusion NSCLC, high tumor burden", "since": "this year"},
                {"condition": "Started selpercatinib 3 days ago", "since": "this week"},
            ],
            "medications": [
                {"drug": "selpercatinib", "dose": "160 mg", "route": "PO", "freq": "twice daily (started 3 days ago)"},
            ],
            "vitals": {"bp": "128/80", "hr": 92, "rr": 18, "urine_output": "declining"},
            "lab_panels": [
                {"panel": "Metabolic panel - today", "collected_offset_days": 0, "results": [
                    {"analyte": "Potassium", "value": 6.4, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.0, "flag": "H"},
                    {"analyte": "Phosphate", "value": 7.2, "unit": "mg/dL", "ref_low": 2.5, "ref_high": 4.5, "flag": "H"},
                    {"analyte": "Uric acid", "value": 11.0, "unit": "mg/dL", "ref_low": 3.5, "ref_high": 7.2, "flag": "H"},
                    {"analyte": "Corrected calcium", "value": 7.4, "unit": "mg/dL", "ref_low": 8.5, "ref_high": 10.2, "flag": "L"},
                    {"analyte": "Creatinine", "value": 2.3, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                    {"analyte": "LDH", "value": 720, "unit": "U/L", "ref_low": 140, "ref_high": 280, "flag": "HH"},
                ]},
                {"panel": "Baseline - 3 days ago", "collected_offset_days": -3, "results": [
                    {"analyte": "Creatinine", "value": 0.9, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT chest/abdomen (staging)",
                 "findings": "Bulky mediastinal and hilar nodal disease with hepatic metastases — high tumor burden. No hydronephrosis.",
                 "measurements": [],
                 "impression": "High tumor burden — TLS risk with a rapidly effective agent."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "60s man with high-burden RET-fusion NSCLC who started selpercatinib 3 days ago and now has AKI "
                    "(creatinine 0.9→2.3), hyperkalemia, hyperphosphatemia, hyperuricemia, hypocalcemia and a very high "
                    "LDH. He is making less urine. The primary team was investigating this as sepsis or contrast "
                    "nephropathy, reasoning that 'tumor lysis doesn't happen with a targeted pill in a solid tumor.'")},
            ],
            "ground_truth": {
                "answer": (
                    "Tumor lysis syndrome from a rapid response to targeted therapy. Give aggressive IV hydration, "
                    "rasburicase for hyperuricemia, treat hyperkalemia and hyperphosphatemia, avoid calcium unless "
                    "symptomatic, and involve nephrology/ICU. Modern targeted/immuno agents cause TLS even in solid "
                    "tumors — the old 'heme + cytotoxic only' risk profile is outdated."),
                "rationale": "The full metabolic sextet 3 days after starting a highly effective agent in bulky disease is TLS regardless of drug class; anchoring on the outdated risk profile misses a treatable emergency.",
                "key_data": ["↑K 6.4, ↑phos 7.2, ↑urate 11, ↓Ca 7.4", "AKI 0.9→2.3, LDH 720", "selpercatinib day 3", "high tumor burden"],
            },
            "hard_hook": "'TLS doesn't happen with a targeted pill in a solid tumor' → the model looks for sepsis/contrast AKI. The metabolic sextet 3 days after a highly effective targeted agent in bulky disease = TLS, now documented with targeted/immuno agents.",
            "reasoning_divergence": "Sound path recognizes TLS regardless of agent class, gives fluids + rasburicase + electrolyte management. Shortcut anchors on the outdated risk profile and misses a treatable emergency.",
        },
    },
    {
        "case_id": "onc-gold-04-egfr-over-pdl1",
        "title": "Treating by histology/PD-L1 when the NGS panel changes the drug",
        "ai_failure_mode": "molecular-over-histology miss; right cancer, wrong therapy (right-answer-wrong-reason)",
        "taxonomy_bucket": "molecular_therapy_selection",
        "subtopic": "pd_l1_vs_driver",
        "question": "A 60-69y never-smoker woman with lung adenocarcinoma; choosing first-line therapy. Path/NGS attached.",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "The PD-L1 tumor proportion score is 60% (high), so first-line single-agent pembrolizumab is indicated. "
                "High PD-L1 predicts a strong checkpoint-inhibitor response, so start immunotherapy now.")},
            {"id": "B", "text": (
                "The EGFR exon-19 deletion OVERRIDES the PD-L1 score. EGFR-mutant NSCLC responds poorly to first-line "
                "immunotherapy, and giving immunotherapy before an EGFR TKI raises the risk of pneumonitis when osimertinib "
                "is later started. The correct first-line therapy is OSIMERTINIB. Actionable driver mutations supersede "
                "PD-L1 for first-line selection despite the loud 60% number.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "female"},
            "problem_list": [
                {"condition": "Lung adenocarcinoma, never-smoker", "since": "this year"},
            ],
            "medications": [
                {"drug": "supportive care only", "dose": "—", "route": "PO", "freq": "as needed"},
            ],
            "vitals": {"bp": "124/76", "hr": 74, "rr": 15, "performance_status": "ECOG 1"},
            "lab_panels": [
                {"panel": "CBC + chemistry", "collected_offset_days": 0, "results": [
                    {"analyte": "Hemoglobin", "value": 11.8, "unit": "g/dL", "ref_low": 12.0, "ref_high": 15.5, "flag": "L"},
                    {"analyte": "Albumin", "value": 3.8, "unit": "g/dL", "ref_low": 3.5, "ref_high": 5.0, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "pathology", "label": "Core biopsy — histology/IHC",
                 "findings": "Adenocarcinoma of the lung, TTF-1 positive, napsin-A positive. PD-L1 immunohistochemistry: tumor proportion score 60%.",
                 "measurements": [
                     {"analyte": "PD-L1 TPS", "value": 60, "unit": "%", "ref_low": None, "ref_high": None, "flag": "H"},
                 ],
                 "impression": "Lung adenocarcinoma, PD-L1 high."},
                {"modality": "molecular", "label": "NGS panel",
                 "findings": "EGFR exon 19 deletion detected (activating driver). ALK/ROS1/BRAF/KRAS/MET wild-type. TMB low.",
                 "measurements": [
                     {"analyte": "EGFR exon 19 del VAF", "value": 34, "unit": "%", "ref_low": None, "ref_high": None, "flag": "H"},
                 ],
                 "impression": "EGFR exon-19-deletion NSCLC — actionable driver."},
            ],
            "notes": [
                {"note_type": "Consult", "author_role": "oncology", "text": (
                    "60s never-smoker woman with newly diagnosed lung adenocarcinoma. The pathology reports a PD-L1 tumor "
                    "proportion score of 60%, and the note says 'PD-L1 high, considering pembrolizumab.' The NGS panel — "
                    "which returned after the PD-L1 result — shows an EGFR exon 19 deletion. We are choosing first-line "
                    "systemic therapy.")},
            ],
            "ground_truth": {
                "answer": (
                    "EGFR exon-19-deletion NSCLC → first-line OSIMERTINIB, not immunotherapy, despite PD-L1 60%. Actionable "
                    "driver mutations supersede PD-L1 for first-line selection, and first-line immunotherapy in EGFR-mutant "
                    "disease is less effective and raises pneumonitis risk when a TKI follows."),
                "rationale": "The NGS driver changes the drug; PD-L1 high is a distractor because EGFR-mutant tumors respond poorly to first-line checkpoint inhibitors.",
                "key_data": ["EGFR exon 19 deletion on NGS", "PD-L1 60% (distractor)", "never-smoker adenocarcinoma", "osimertinib first-line"],
            },
            "hard_hook": "PD-L1 60% → 'high PD-L1, give a checkpoint inhibitor.' The EGFR exon-19 deletion overrides PD-L1: EGFR-mutant NSCLC responds poorly to first-line immunotherapy; the correct first-line is osimertinib.",
            "reasoning_divergence": "Sound path grounds the NGS result, recognizes the driver takes precedence over PD-L1, gives osimertinib. Shortcut anchors on the loud PD-L1 number and gives the wrong, potentially harmful first-line.",
        },
    },
    {
        "case_id": "onc-gold-05-t790m-resistance",
        "title": "EGFR-TKI resistance: model repeats the drug instead of testing for T790M",
        "ai_failure_mode": "failure to seek the resistance-mechanism data; sequencing error",
        "taxonomy_bucket": "molecular_therapy_selection",
        "subtopic": "t790m_resistance",
        "question": "A 70-79y man with EGFR-mutant NSCLC progressing on erlotinib after 14 months. Next line?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "He has progressed on a first-generation EGFR TKI after a good run, so switch to platinum-doublet "
                "chemotherapy as the next line. Alternatively, escalate the erlotinib dose to overcome resistance. Either "
                "way, move on from the TKI empirically.")},
            {"id": "B", "text": (
                "Before choosing the next line, obtain the RESISTANCE MECHANISM: a repeat tissue biopsy or a plasma "
                "(liquid) NGS to test for the EGFR T790M mutation. If T790M is positive — the most common resistance "
                "mechanism — the correct therapy is OSIMERTINIB, not chemotherapy. Only if T790M is negative do you "
                "consider chemotherapy. Skipping the resistance test and defaulting to chemo or dose escalation is a "
                "sequencing error that denies an effective targeted option.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "70-79", "sex": "male"},
            "problem_list": [
                {"condition": "EGFR exon-19 NSCLC, progressing on erlotinib (14 months)", "since": "this year"},
            ],
            "medications": [
                {"drug": "erlotinib", "dose": "150 mg", "route": "PO", "freq": "daily"},
            ],
            "vitals": {"bp": "130/78", "hr": 80, "rr": 16, "performance_status": "ECOG 1"},
            "lab_panels": [
                {"panel": "CBC + chemistry", "collected_offset_days": 0, "results": [
                    {"analyte": "Hemoglobin", "value": 11.4, "unit": "g/dL", "ref_low": 13.5, "ref_high": 17.5, "flag": "L"},
                    {"analyte": "Creatinine", "value": 1.0, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT chest — timeline",
                 "findings": "New growth of pulmonary and nodal lesions compared with the prior scan after 14 months of response on erlotinib — acquired resistance.",
                 "measurements": [
                     {"analyte": "Largest lesion diameter", "value": 34, "unit": "mm", "ref_low": None, "ref_high": None, "flag": "H"},
                 ],
                 "impression": "Progression on first-gen TKI — obtain resistance testing."},
                {"modality": "molecular", "label": "Repeat biopsy / liquid NGS",
                 "findings": "Pending — sent for EGFR T790M and broad resistance panel (tissue + plasma).",
                 "measurements": [],
                 "impression": "Resistance mechanism pending (T790M?)."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "70s man with EGFR exon-19 NSCLC who responded to erlotinib for 14 months and has now PROGRESSED on "
                    "imaging. He tolerated the TKI well. The key question is the resistance mechanism; a repeat biopsy and "
                    "a plasma liquid biopsy for T790M have been sent but are still pending. The covering note suggested "
                    "'progressed on TKI → start chemotherapy.'")},
            ],
            "ground_truth": {
                "answer": (
                    "Test for the T790M resistance mutation (repeat tissue biopsy or plasma liquid biopsy). If T790M-"
                    "positive → osimertinib. Do not switch to chemotherapy or escalate the TKI dose without checking the "
                    "resistance mechanism first."),
                "rationale": "T790M is the most common acquired-resistance mechanism to first-gen EGFR TKIs and is specifically treatable with osimertinib; empirically defaulting to chemo skips the decisive molecular test.",
                "key_data": ["progression after 14 months on erlotinib", "resistance testing pending", "T790M+ → osimertinib", "no chemo without the mechanism"],
            },
            "hard_hook": "'Progressed on a TKI → switch to chemo,' or 'increase the TKI dose.' The decisive missing step is testing for T790M, which if present indicates osimertinib rather than chemotherapy.",
            "reasoning_divergence": "Sound path orders resistance testing and, if T790M+, gives osimertinib; only if negative considers chemo. Shortcut skips the mechanism test and defaults to chemo or dose escalation.",
        },
    },
    {
        "case_id": "onc-gold-06-febrile-neutropenia",
        "title": "Febrile neutropenia where the model waits for the culture",
        "ai_failure_mode": "dangerous under-urgency; withholds empiric antibiotics pending data",
        "taxonomy_bucket": "onc_emergencies",
        "subtopic": "febrile_neutropenia",
        "question": "A 50-59y woman, 10 days post-chemo, temp 38.5°C, ANC 400, looks well. Immediate step?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is FEBRILE NEUTROPENIA — a time-critical emergency. Draw blood cultures and then IMMEDIATELY start "
                "empiric broad-spectrum antipseudomonal antibiotics (cefepime or piperacillin-tazobactam) within the first "
                "hour, before any source is identified. Risk-stratify (MASCC), but do NOT wait for imaging or culture "
                "results — a delay of even a well-appearing neutropenic fever can become fatal sepsis quickly.")},
            {"id": "B", "text": (
                "She is hemodynamically stable and well-appearing with no localizing source, so obtain blood and urine "
                "cultures and a chest X-ray first, and start antibiotics once a source is identified or the cultures return "
                "positive. Avoid unnecessary broad-spectrum antibiotics if there is no clear infection.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "50-59", "sex": "female"},
            "problem_list": [
                {"condition": "Breast cancer, 10 days post-cytotoxic chemotherapy", "since": "this year"},
            ],
            "medications": [
                {"drug": "pegfilgrastim", "dose": "6 mg", "route": "SC", "freq": "once per cycle (given)"},
            ],
            "vitals": {"temp_c": 38.5, "hr": 104, "bp": "108/64", "rr": 18, "spo2": "97%"},
            "lab_panels": [
                {"panel": "CBC + differential", "collected_offset_days": 0, "results": [
                    {"analyte": "Absolute neutrophil count", "value": 400, "unit": "cells/µL", "ref_low": 1500, "ref_high": 8000, "flag": "LL"},
                    {"analyte": "White blood cells", "value": 0.9, "unit": "10^9/L", "ref_low": 4.0, "ref_high": 11.0, "flag": "L"},
                    {"analyte": "Lactate", "value": 2.2, "unit": "mmol/L", "ref_low": 0.5, "ref_high": 2.0, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT chest (low-dose, on arrival)",
                 "findings": "No focal consolidation or abscess identified; no clear source of infection on imaging.",
                 "measurements": [],
                 "impression": "No localizing source — does not exclude occult infection."},
            ],
            "notes": [
                {"note_type": "ED", "author_role": "emergency medicine", "text": (
                    "50s woman 10 days after cytotoxic chemotherapy for breast cancer, presenting with a single fever to "
                    "38.5°C. She is mildly tachycardic but hemodynamically STABLE and looks well, with no localizing "
                    "symptoms or obvious source. The ANC is 400 (severely neutropenic). The team is inclined to 'culture "
                    "and observe' because she looks well.")},
            ],
            "ground_truth": {
                "answer": (
                    "Draw cultures and then give empiric broad-spectrum antipseudomonal antibiotics (cefepime or "
                    "piperacillin-tazobactam) within 1 hour — do NOT delay for imaging or culture results. Neutropenic "
                    "fever is a time-critical emergency even in a well-appearing patient; risk-stratify with MASCC but "
                    "treat first."),
                "rationale": "The golden-hour antibiotic rule for febrile neutropenia is independent of appearance or an identified source; delay to await data is the dangerous error.",
                "key_data": ["ANC 400 + fever 38.5", "well-appearing but neutropenic", "empiric antibiotics within 1 hour", "do not wait for cultures/imaging"],
            },
            "hard_hook": "'Well-appearing, no source → get cultures/imaging, start antibiotics once we localize.' Febrile neutropenia is an emergency: empiric antipseudomonal antibiotics within ~1 hour, before any source is found.",
            "reasoning_divergence": "Sound path draws cultures and immediately starts empiric antibiotics, risk-stratifies, does not wait. Shortcut waits for a source or culture result and loses the golden hour.",
        },
    },
    {
        "case_id": "onc-gold-07-siadh-sclc-overcorrection",
        "title": "SIADH from small-cell lung cancer over-corrected with hypertonic saline",
        "ai_failure_mode": "overtreatment / osmotic demyelination; misses the paraneoplastic driver + correction limit",
        "taxonomy_bucket": "paraneoplastic",
        "subtopic": "siadh",
        "question": "A 60-69y smoker, Na 116, confused, with a hilar mass. Manage the sodium.",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is euvolemic hyponatremia from SIADH — a paraneoplastic syndrome of small-cell lung cancer (low serum "
                "osmolality, inappropriately concentrated urine, high urine sodium, normal cortisol/TSH). CAP the correction "
                "at ≤6-8 mmol/L per 24 hours; use small hypertonic-saline boluses ONLY for severe symptoms (seizures/coma) "
                "with strict limits, fluid-restrict, and treat the underlying malignancy. Over-rapid correction risks "
                "osmotic demyelination.")},
            {"id": "B", "text": (
                "A sodium of 116 with confusion is severe symptomatic hyponatremia — run 3% hypertonic saline to bring the "
                "sodium back to normal over the next 24 hours to reverse the neurologic symptoms. Normalizing the sodium "
                "quickly is the priority.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "New hilar lung mass (small-cell suspected)", "since": "this month"},
                {"condition": "Heavy tobacco use", "since": "chronic"},
            ],
            "medications": [
                {"drug": "no diuretics; supportive care", "dose": "—", "route": "PO", "freq": "as needed"},
            ],
            "vitals": {"bp": "128/78", "hr": 82, "rr": 16, "volume": "clinically euvolemic"},
            "lab_panels": [
                {"panel": "Sodium + osmolality", "collected_offset_days": 0, "results": [
                    {"analyte": "Sodium", "value": 116, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"},
                    {"analyte": "Serum osmolality", "value": 248, "unit": "mOsm/kg", "ref_low": 275, "ref_high": 295, "flag": "L"},
                    {"analyte": "Urine osmolality", "value": 320, "unit": "mOsm/kg", "ref_low": None, "ref_high": None, "flag": "H"},
                    {"analyte": "Urine sodium", "value": 58, "unit": "mmol/L", "ref_low": None, "ref_high": None, "flag": "H"},
                    {"analyte": "Morning cortisol", "value": 16, "unit": "µg/dL", "ref_low": 5, "ref_high": 25, "flag": ""},
                    {"analyte": "TSH", "value": 2.1, "unit": "mIU/L", "ref_low": 0.4, "ref_high": 4.0, "flag": ""},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT chest",
                 "findings": "Hilar mass with bulky mediastinal adenopathy; appearance and rapid course suggestive of small-cell lung carcinoma. Biopsy pending.",
                 "measurements": [],
                 "impression": "Hilar mass — likely small-cell; paraneoplastic SIADH."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "60s smoker with a new hilar mass, admitted confused with a sodium of 116. He is clinically EUVOLEMIC "
                    "with a low serum osmolality, an inappropriately concentrated urine (Uosm 320) and a high urine sodium; "
                    "cortisol and TSH are normal — the picture of SIADH, most likely paraneoplastic from a small-cell "
                    "primary. The admitting team wants to 'run hypertonic saline to normalize the sodium.'")},
            ],
            "ground_truth": {
                "answer": (
                    "SIADH from small-cell lung cancer. Correct the sodium by ≤6-8 mmol/L per 24 h (cautious hypertonic "
                    "saline ONLY if seizing/severely symptomatic, with strict limits), fluid-restrict, and treat the "
                    "malignancy. Over-rapid correction causes osmotic demyelination — normalizing the sodium fast is the "
                    "danger, not the goal."),
                "rationale": "Chronic euvolemic hyponatremia from paraneoplastic SIADH must be corrected slowly; the trap is aggressive normalization causing osmotic demyelination syndrome.",
                "key_data": ["Na 116, euvolemic", "Uosm 320 + urine Na 58", "normal cortisol/TSH", "hilar mass (small-cell)", "cap correction ≤6-8 mmol/L/24h"],
            },
            "hard_hook": "Na 116 + confusion → 'severe hyponatremia, run hypertonic saline to normal.' Euvolemic hyponatremia with concentrated urine = SIADH; the danger is over-rapid correction → osmotic demyelination.",
            "reasoning_divergence": "Sound path caps correction, uses hypertonic saline only for severe symptoms with strict limits, fluid-restricts, works up the small-cell primary. Shortcut chases a normal sodium fast and demyelinates the pons.",
        },
    },
    {
        "case_id": "onc-gold-08-hhm-pthrp",
        "title": "Hypercalcemia of malignancy treated as primary hyperparathyroidism",
        "ai_failure_mode": "anchoring on 'high calcium = check PTH → parathyroid'; misses PTHrP (right-answer-wrong-reason)",
        "taxonomy_bucket": "paraneoplastic",
        "subtopic": "pthrp_hypercalcemia",
        "question": "A 60-69y man with squamous lung cancer, calcium 13.8, confusion. Cause and treatment?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is humoral hypercalcemia of malignancy (HHM). The corrected calcium is 13.8 with a SUPPRESSED PTH and "
                "an elevated PTHrP in a patient with squamous NSCLC — PTHrP-mediated, not parathyroid. Treat with aggressive "
                "IV normal saline, an IV bisphosphonate (or denosumab if renal impairment), and treat the tumor. The "
                "suppressed PTH excludes primary hyperparathyroidism, so parathyroidectomy is wrong.")},
            {"id": "B", "text": (
                "A calcium of 13.8 indicates hyperparathyroidism. Check the PTH and refer to endocrine surgery for "
                "parathyroidectomy to remove the overactive gland; give some IV fluids while arranging the operation. "
                "Surgical cure of the parathyroid adenoma will correct the calcium.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "Squamous cell lung carcinoma", "since": "this year"},
            ],
            "medications": [
                {"drug": "as-needed analgesia; supportive care", "dose": "—", "route": "PO", "freq": "as needed"},
            ],
            "vitals": {"bp": "118/72", "hr": 96, "rr": 16, "volume": "dehydrated"},
            "lab_panels": [
                {"panel": "Calcium panel", "collected_offset_days": 0, "results": [
                    {"analyte": "Corrected calcium", "value": 13.8, "unit": "mg/dL", "ref_low": 8.5, "ref_high": 10.2, "flag": "HH"},
                    {"analyte": "PTH (intact)", "value": 6, "unit": "pg/mL", "ref_low": 15, "ref_high": 65, "flag": "L"},
                    {"analyte": "Phosphate", "value": 2.2, "unit": "mg/dL", "ref_low": 2.5, "ref_high": 4.5, "flag": "L"},
                    {"analyte": "PTHrP", "value": 8.4, "unit": "pmol/L", "ref_low": 0, "ref_high": 2.0, "flag": "H"},
                    {"analyte": "Creatinine", "value": 1.6, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "ct", "label": "CT chest",
                 "findings": "Bulky cavitating squamous primary in the right upper lobe with mediastinal adenopathy.",
                 "measurements": [],
                 "impression": "Bulky squamous NSCLC — humoral hypercalcemia."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "60s man with squamous NSCLC, admitted with polyuria, confusion and constipation. Corrected calcium is "
                    "13.8. The intact PTH is SUPPRESSED (6) and the PTHrP is elevated; phosphate is low and the creatinine "
                    "is up from dehydration. The covering team anchored on 'high calcium = hyperparathyroidism' and asked "
                    "for a surgery referral.")},
            ],
            "ground_truth": {
                "answer": (
                    "Humoral hypercalcemia of malignancy (PTHrP-mediated). Give aggressive IV normal saline, an IV "
                    "bisphosphonate (or denosumab if renal impairment), and treat the tumor. The SUPPRESSED PTH with "
                    "elevated PTHrP excludes primary hyperparathyroidism — parathyroidectomy is the wrong organ."),
                "rationale": "A suppressed PTH with elevated PTHrP in a squamous cancer defines HHM; anchoring on 'hypercalcemia = parathyroid' pursues the wrong organ and delays the right therapy.",
                "key_data": ["corrected calcium 13.8", "PTH suppressed (6)", "PTHrP elevated", "squamous NSCLC", "IV saline + bisphosphonate/denosumab"],
            },
            "hard_hook": "High calcium → 'hyperparathyroidism, refer to surgery.' The suppressed PTH + elevated PTHrP in a squamous cancer = humoral hypercalcemia of malignancy; treatment is IV fluids + bisphosphonate/denosumab, not parathyroidectomy.",
            "reasoning_divergence": "Sound path grounds the suppressed PTH and PTHrP, diagnoses HHM, hydrates and gives a bisphosphonate/denosumab. Shortcut anchors on 'hypercalcemia = parathyroid' and pursues the wrong organ.",
        },
    },
    {
        "case_id": "onc-gold-09-cord-compression",
        "title": "Metastatic spinal cord compression: model schedules an outpatient MRI",
        "ai_failure_mode": "under-urgency; misses the emergency window for steroids + definitive treatment",
        "taxonomy_bucket": "onc_emergencies",
        "subtopic": "cord_compression",
        "question": "A 60-69y man with prostate cancer, progressive back pain, new leg weakness and urinary hesitancy. Plan?",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is metastatic epidural spinal cord compression — a neuro-oncologic EMERGENCY. Give immediate high-dose "
                "dexamethasone now and obtain emergent radiation-oncology and neurosurgery consultation for definitive "
                "treatment (decompression/radiation). The whole-spine MRI already confirms cord compression at T8; do not "
                "defer, because ambulatory recovery depends on treating before paralysis is established. Hours matter.")},
            {"id": "B", "text": (
                "Back pain in a cancer patient is common. Arrange an outpatient MRI and routine oncology follow-up, start "
                "analgesia and physical therapy, and reassess in clinic. If the weakness progresses, escalate then.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "male"},
            "problem_list": [
                {"condition": "Metastatic castration-resistant prostate cancer", "since": "this year"},
            ],
            "medications": [
                {"drug": "leuprolide", "dose": "22.5 mg", "route": "SC", "freq": "every 3 months"},
            ],
            "vitals": {"bp": "132/80", "hr": 82, "rr": 16},
            "lab_panels": [
                {"panel": "Labs", "collected_offset_days": 0, "results": [
                    {"analyte": "PSA", "value": 148, "unit": "ng/mL", "ref_low": 0, "ref_high": 4, "flag": "HH"},
                    {"analyte": "Alkaline phosphatase", "value": 320, "unit": "U/L", "ref_low": 40, "ref_high": 130, "flag": "H"},
                ]},
            ],
            "studies": [
                {"modality": "mri", "label": "Whole-spine MRI",
                 "findings": "Epidural metastatic deposit at T8 with cord compression and early cord signal change. Multiple additional vertebral metastases.",
                 "measurements": [],
                 "impression": "Metastatic epidural spinal cord compression at T8 — emergency."},
                {"modality": "pet", "label": "Bone scan",
                 "findings": "Diffuse skeletal metastatic uptake ('superscan' pattern).",
                 "measurements": [],
                 "impression": "Diffuse bone metastases."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "60s man with metastatic prostate cancer and several weeks of progressive band-like mid-thoracic back "
                    "pain, now with NEW bilateral leg weakness, new urinary hesitancy and reduced saddle sensation. The "
                    "whole-spine MRI shows epidural cord compression at T8. The covering note suggested arranging an "
                    "'outpatient MRI and oncology follow-up.'")},
            ],
            "ground_truth": {
                "answer": (
                    "Metastatic epidural spinal cord compression — immediate high-dose dexamethasone plus emergent "
                    "radiation-oncology/neurosurgery consultation; do NOT defer. Recovery of ambulation depends on treating "
                    "before paralysis is established — hours matter."),
                "rationale": "Progressive weakness plus sphincter symptoms with MRI-confirmed cord compression is a time-critical emergency; outpatient management risks irreversible paraplegia.",
                "key_data": ["MRI cord compression at T8", "new bilateral leg weakness", "urinary hesitancy + saddle sensory loss", "immediate dexamethasone + emergent RT/neurosurgery"],
            },
            "hard_hook": "'Back pain in a cancer patient → arrange imaging and outpatient follow-up.' Progressive weakness + sphincter symptoms + cord compression is a neuro-oncologic emergency: immediate steroids + emergent radiation/surgery.",
            "reasoning_divergence": "Sound path grounds the neuro deficit + MRI, gives immediate dexamethasone and emergent consults. Shortcut treats it as routine pain and lets the patient become paraplegic.",
        },
    },
    {
        "case_id": "onc-gold-10-msi-high-immunotherapy",
        "title": "MSI-high tumor denied immunotherapy after chemo 'failure'",
        "ai_failure_mode": "failure to ground the biomarker; withholds the effective therapy",
        "taxonomy_bucket": "molecular_therapy_selection",
        "subtopic": "msi_high_tmb",
        "question": "A 60-69y woman with metastatic colorectal cancer progressing after two chemo lines. Molecular attached. Next option?",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "She has progressed through two chemotherapy regimens and the NGS shows no RAS/BRAF actionable driver, so "
                "there is no targeted option left — move to best supportive care or a clinical trial. Without a targetable "
                "mutation, further disease-directed therapy is unlikely to help.")},
            {"id": "B", "text": (
                "The tumor is MSI-HIGH / mismatch-repair deficient (dMMR) — the decisive, actionable biomarker. dMMR/MSI-"
                "high metastatic colorectal cancer responds markedly to CHECKPOINT IMMUNOTHERAPY (e.g. pembrolizumab), "
                "independent of RAS/BRAF status. Do not equate 'no driver mutation' with 'no options': MMR/MSI status IS "
                "the actionable marker, and immunotherapy is a highly effective next therapy here.")},
        ],
        "case": {
            "case_source": "synthetic", "specialty": "oncology",
            "demographics": {"age_band": "60-69", "sex": "female"},
            "problem_list": [
                {"condition": "Metastatic colorectal cancer, progressed on two chemo regimens", "since": "this year"},
            ],
            "medications": [
                {"drug": "supportive care (between lines)", "dose": "—", "route": "PO", "freq": "as needed"},
            ],
            "vitals": {"bp": "122/74", "hr": 78, "rr": 16, "performance_status": "ECOG 1"},
            "lab_panels": [
                {"panel": "Labs", "collected_offset_days": 0, "results": [
                    {"analyte": "CEA", "value": 88, "unit": "ng/mL", "ref_low": 0, "ref_high": 5, "flag": "H"},
                    {"analyte": "Hemoglobin", "value": 10.6, "unit": "g/dL", "ref_low": 12.0, "ref_high": 15.5, "flag": "L"},
                ]},
            ],
            "studies": [
                {"modality": "molecular", "label": "NGS + MMR/MSI panel",
                 "findings": "Mismatch-repair DEFICIENT (loss of MLH1/PMS2 on IHC), MSI-HIGH by PCR. KRAS/NRAS/BRAF wild-type; no other actionable driver reported. TMB high.",
                 "measurements": [
                     {"analyte": "MSI status", "value": "MSI-high", "unit": "", "ref_low": None, "ref_high": None, "flag": "H"},
                     {"analyte": "Tumor mutational burden", "value": 42, "unit": "mut/Mb", "ref_low": 0, "ref_high": 10, "flag": "H"},
                 ],
                 "impression": "dMMR/MSI-high — checkpoint-inhibitor responsive."},
                {"modality": "pathology", "label": "Archival tumor block IHC",
                 "findings": "Poorly differentiated adenocarcinoma; MMR IHC with loss of MLH1 and PMS2 nuclear staining.",
                 "measurements": [],
                 "impression": "dMMR by IHC."},
            ],
            "notes": [
                {"note_type": "Progress", "author_role": "oncology", "text": (
                    "60s woman with metastatic colorectal cancer that has progressed after two lines of chemotherapy. The "
                    "molecular report shows the tumor is MSI-HIGH / mismatch-repair deficient with a high TMB and no RAS/"
                    "BRAF driver. The covering note read 'out of standard chemo options, no targetable driver → best "
                    "supportive care,' overlooking the MMR status.")},
            ],
            "ground_truth": {
                "answer": (
                    "MSI-high/dMMR metastatic CRC → checkpoint-inhibitor immunotherapy (e.g. pembrolizumab), which is "
                    "highly effective here. MMR/MSI status is an actionable biomarker independent of RAS/BRAF — 'no driver "
                    "mutation' does not mean 'no options.'"),
                "rationale": "dMMR/MSI-high status predicts marked benefit from checkpoint inhibitors regardless of RAS/BRAF; fixating on the absence of a classic driver misses the decisive biomarker.",
                "key_data": ["MSI-high / dMMR (MLH1+PMS2 loss)", "high TMB", "RAS/BRAF wild-type (distractor)", "pembrolizumab highly effective"],
            },
            "hard_hook": "'Progressed on chemo, no targetable driver → best supportive care / trial.' The MSI-high/dMMR status is the decisive biomarker: these tumors respond markedly to checkpoint immunotherapy, which the shortcut ignores.",
            "reasoning_divergence": "Sound path grounds the MSI-high result and gives a checkpoint inhibitor. Shortcut fixates on 'no driver mutation' and misses that MMR status IS the actionable marker.",
        },
    },
]


# ─── Combined gold registry (specialty-agnostic serving + seedmaker few-shots) ──
GOLD_CASE_SETS: Dict[str, List[Dict[str, Any]]] = {
    "nephrology": GOLD_NEPHROLOGY_CASES,
    "cardiology": GOLD_CARDIOLOGY_CASES,
    "oncology": GOLD_ONCOLOGY_CASES,
}


def all_gold_cases() -> List[Dict[str, Any]]:
    """Every authored gold case across specialties (nephrology + cardiology +
    oncology). ``load_gold_cases`` / ``_validated`` iterate this, so a new specialty's
    gold set is picked up by adding it to ``GOLD_CASE_SETS`` — no other change."""
    out: List[Dict[str, Any]] = []
    for cases in GOLD_CASE_SETS.values():
        out.extend(cases)
    return out


def _validated() -> List[Dict[str, Any]]:
    """Fail fast (at import) if a case does not clear the real content gate or is
    missing its A/B pair — a broken seed must never ship silently."""
    from asclepius.cases import assert_multimodal_content, MultimodalContentError

    ok: List[Dict[str, Any]] = []
    for entry in all_gold_cases():
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


def fewshot_cases(k: int = 2, start: int = 0, *, specialty: str = "nephrology") -> List[Dict[str, Any]]:
    """Return ``k`` gold ``{question, case}`` exemplars (public case only — the
    answer key is stripped) to inject as few-shot into the case-gen prompt. ``start``
    rotates the window so calls don't always show the same cases.

    ``specialty`` selects WHICH gold set to few-shot from so the Seedmaker copies
    the right modality shape (cardiology → ECG/echo studies; oncology → pathology/
    molecular studies). Falls back to nephrology if the specialty has no gold set."""
    from asclepius.cases import public_case

    pool = GOLD_CASE_SETS.get((specialty or "").strip().lower()) or GOLD_NEPHROLOGY_CASES
    n = len(pool)
    if n == 0 or k <= 0:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(min(k, n)):
        entry = pool[(start + i) % n]
        out.append({"question": entry["question"], "case": public_case(entry["case"])})
    return out


def fewshot_prompt_block(k: int = 2, start: int = 0, *, specialty: str = "nephrology") -> str:
    """A ready-to-inject text block of ``k`` full exemplars for the case-gen user
    message. Empty string when no cases are available."""
    ex = fewshot_cases(k=k, start=start, specialty=specialty)
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

    ``specialty`` filters WHICH gold cases load (nephrology, cardiology, or
    oncology): a mismatched specialty loads nothing rather than mis-tagging cases
    under it, so ``POST /generation/<other>/load-gold`` is a correct no-op. The
    case's own specialty is authoritative, never the path param."""
    from asclepius.cases import render_case_prompt, case_type_signature

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
        # A declared, unmeasured difficulty (PRD §9): these authored exemplars ARE
        # the frontier-failure bar, but they must still clear the empirical gate on
        # LIVE frontier models before being sold as "measured hard." Until measured,
        # ``measured=False`` prevents an over-claim; the serving gate only enforces a
        # measured floor when ``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY`` is on.
        empirical = {
            "value": None,          # frontier-model failure rate — set by live measurement
            "declared": 0.8,        # authored bar (§9): engineered to break frontier models
            "measured": False,
            "both_axes": True,      # counts wrong-answer OR wrong-reasoning (§9)
            "note": "authored frontier-failure exemplar; awaits live grade-real-models measurement (PRD §9)",
        }
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
                "taxonomy_bucket": entry.get("taxonomy_bucket"),
                "subtopic": entry.get("subtopic"),
                "case_type": case_type_signature(case),
                "empirical_difficulty": empirical,
                "intended_flawed_id": entry["intended_flawed_id"],
                "modality": "multimodal",
                "case_source": case.get("case_source", "synthetic"),
            },
            created_by="system:gold_seed",
        )
        loaded += 1
        task_ids.append(tid)
    return {"loaded": loaded, "skipped": skipped, "total": len(eligible), "task_ids": task_ids}
