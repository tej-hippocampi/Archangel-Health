"""V4 real de-identified cases — the three built from the partner bundles.

Sibling of :mod:`asclepius.gold_cases`, and deliberately shaped like it, but the
provenance is the opposite: gold cases are hand-AUTHORED exemplars
(``case_source: synthetic``); these are real decision points lifted out of real
de-identified charts (``case_source: real_deid``), so every number below is
transcribed from the partner bundle's own ``labs/lab_results.csv`` and notes.

Three cases (V4 Cases & Promotion PRD §3), each chosen because the obvious move
is wrong and the correct move is defensible from the data present:

  * ``v4-hep-001`` — bilirubin lag after successful biliary drainage (patient-1)
  * ``v4-neph-001`` — volume-responsive AKI in cirrhosis, and an over-transfusion
    (patient-3)
  * ``v4-card-001`` — troponin elevation in acute cerebrovascular event (patient-4)

Four rules govern this file, and the first two are why it exists at all:

1. **Nothing here is invented.** A value appears only if the PRD measured it
   against the source CSV. Where the PRD names a post-decision fact (the
   2023-06-03 stent occlusion, the 2024-01-09 creatinine), it lives in
   ``ground_truth.evidence`` — never as a visible panel, because a panel dated
   after the decision point IS the answer. See ``_HELD_OUT_NOTE`` below.
2. **The corrupted bicarbonate is not used.** patient-4 carries three conflicting
   bicarbonates on 2025-01-23, one of them 1.7 mmol/L — not survivable, an OCR
   artifact, and now dropped by ``real_cases.implausible_value``. Case C is built
   on the 2024-12 admission, and every value in it is verified.
3. **The schema is the real one.** The PRD sketched these dicts with
   ``problem_list[].label``, ``studies[].kind`` and ``studies[].offset_days``;
   ``ClinicalCase`` uses ``condition``, ``modality`` and ``collected_offset_days``
   and is ``extra="forbid"``, so the sketch would have raised on every case. The
   shapes below are validated at import (``_validated``).
4. **A gap in the source data is NAMED, never dressed up.** Where a real chart
   cannot satisfy a content requirement — patient-4's bundle carries no ECG, and
   a cardiology case would normally need one — the case ships and the gap is
   reported (``V4_STUDY_GAPS``), because the alternatives are fabricating the
   artifact inside a ``real_deid`` record or relabelling the case into a
   specialty that does not describe it. A case that cannot be made honest at all
   is HELD instead (``V4_HOLDS``, currently empty).

Offsets are relative days against each case's own decision point (day 0), which
is what ``ClinicalCase`` carries — the calendar dates in the tables below are
documentation of the source rows, and die here.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.v4_cases")

V4_CASE_SOURCE = "real_deid"

#: Why a post-decision value is never a visible panel. Referenced from each case.
_HELD_OUT_NOTE = (
    "Recorded after the decision point and therefore held out of the visible case: "
    "it is the outcome, and showing it would answer the question it is meant to test."
)


# ═══════════════════════════════════════════════════════════════════════════════
# Case A — v4-hep-001 · Bilirubin lag after successful biliary drainage
# ═══════════════════════════════════════════════════════════════════════════════
# patient-1, M 39. Source rows, ``labs/lab_results.csv`` (calendar dates are the
# partner's shifted dates and are documentation only — the case carries offsets):
#
#   date         Bili(T)   ALP   GGT    ALT   Plt      offset
#   2023-03-16    15.04 H  666 H 1358 H 237 H  48 L      -19
#   2023-03-17      —      659   1361   253    61 L      -18
#   2023-03-20      —       —      —     —     60 L      -15   (amylase 106 H)
#   2023-03-31    17.43 H  259 H  237 H  56 H  83 L       -4
#   2023-04-04    17.77 H  289 H  123 H  32    100 L       0   ← decision point
#
# The trap: bilirubin has RISEN 15.04 → 17.77 across nineteen days, so the obvious
# read is failed drainage needing urgent repeat ERCP. The enzymes say otherwise.

CASE_A: Dict[str, Any] = {
    "case_id": "v4-hep-001",
    "case_source": V4_CASE_SOURCE,
    "specialty": "hepatology",
    "demographics": {"age_band": "35-44", "sex": "male"},
    "problem_list": [
        {"condition": "Chronic portal vein thrombosis with cavernous transformation",
         "since": "chronic", "collected_offset_days": -19},
        {"condition": "Noncirrhotic portal hypertension",
         "since": "chronic", "collected_offset_days": -19},
        {"condition": "Portal biliopathy with recurrent obstructive jaundice",
         "since": "chronic", "collected_offset_days": -19},
        {"condition": "Choledocholithiasis with common bile duct stricture; serial ERCP stenting",
         "since": "chronic", "collected_offset_days": -19},
        {"condition": "Thrombocytopenia secondary to hypersplenism",
         "since": "chronic", "collected_offset_days": -19},
        {"condition": "Post-ERCP acute pancreatitis", "collected_offset_days": -15},
    ],
    "medications": [
        {"drug": "Ursodeoxycholic acid", "dose": "300 mg", "route": "oral",
         "freq": "three times daily", "collected_offset_days": -19},
        {"drug": "Propranolol", "dose": "20 mg", "route": "oral",
         "freq": "twice daily", "collected_offset_days": -19},
        {"drug": "Pantoprazole", "dose": "40 mg", "route": "oral",
         "freq": "once daily", "collected_offset_days": -19},
    ],
    "lab_panels": [
        {"panel": "Liver function + full blood count", "collected_offset_days": -19,
         "results": [
             {"analyte": "Bilirubin (total)", "value": 15.04, "unit": "mg/dL",
              "ref_low": 0.2, "ref_high": 1.2, "flag": "H"},
             {"analyte": "Alkaline phosphatase", "value": 666, "unit": "U/L",
              "ref_low": 40, "ref_high": 129, "flag": "H"},
             {"analyte": "Gamma-glutamyl transferase", "value": 1358, "unit": "U/L",
              "ref_low": 8, "ref_high": 61, "flag": "H"},
             {"analyte": "Alanine aminotransferase", "value": 237, "unit": "U/L",
              "ref_low": 7, "ref_high": 55, "flag": "H"},
             {"analyte": "Platelet count", "value": 48, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
         ]},
        {"panel": "Liver function + full blood count", "collected_offset_days": -18,
         "results": [
             {"analyte": "Alkaline phosphatase", "value": 659, "unit": "U/L",
              "ref_low": 40, "ref_high": 129, "flag": "H"},
             {"analyte": "Gamma-glutamyl transferase", "value": 1361, "unit": "U/L",
              "ref_low": 8, "ref_high": 61, "flag": "H"},
             {"analyte": "Alanine aminotransferase", "value": 253, "unit": "U/L",
              "ref_low": 7, "ref_high": 55, "flag": "H"},
             {"analyte": "Platelet count", "value": 61, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
         ]},
        {"panel": "Pancreatic enzymes + full blood count", "collected_offset_days": -15,
         "results": [
             {"analyte": "Amylase", "value": 106, "unit": "U/L",
              "ref_low": 25, "ref_high": 125, "flag": "H"},
             {"analyte": "Platelet count", "value": 60, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
         ]},
        {"panel": "Liver function + full blood count", "collected_offset_days": -4,
         "results": [
             {"analyte": "Bilirubin (total)", "value": 17.43, "unit": "mg/dL",
              "ref_low": 0.2, "ref_high": 1.2, "flag": "H"},
             {"analyte": "Alkaline phosphatase", "value": 259, "unit": "U/L",
              "ref_low": 40, "ref_high": 129, "flag": "H"},
             {"analyte": "Gamma-glutamyl transferase", "value": 237, "unit": "U/L",
              "ref_low": 8, "ref_high": 61, "flag": "H"},
             {"analyte": "Alanine aminotransferase", "value": 56, "unit": "U/L",
              "ref_low": 7, "ref_high": 55, "flag": "H"},
             {"analyte": "Platelet count", "value": 83, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
         ]},
        {"panel": "Liver function + full blood count", "collected_offset_days": 0,
         "results": [
             {"analyte": "Bilirubin (total)", "value": 17.77, "unit": "mg/dL",
              "ref_low": 0.2, "ref_high": 1.2, "flag": "H"},
             {"analyte": "Alkaline phosphatase", "value": 289, "unit": "U/L",
              "ref_low": 40, "ref_high": 129, "flag": "H"},
             {"analyte": "Gamma-glutamyl transferase", "value": 123, "unit": "U/L",
              "ref_low": 8, "ref_high": 61, "flag": "H"},
             {"analyte": "Alanine aminotransferase", "value": 32, "unit": "U/L",
              "ref_low": 7, "ref_high": 55, "flag": ""},
             {"analyte": "Platelet count", "value": 100, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
         ]},
    ],
    "studies": [
        {"modality": "other", "label": "ERCP procedure report",
         "collected_offset_days": -19,
         "findings": ("Common bile duct cannulated. Distal CBD stricture traversed; "
                      "plastic biliary stent placed across the stricture with brisk "
                      "flow of bile on withdrawal. Cavernous transformation of the "
                      "portal vein noted at the hilum."),
         "impression": "Successful biliary decompression across a distal CBD stricture."},
        {"modality": "other", "label": "Abdominal ultrasound",
         "collected_offset_days": -15,
         "findings": ("Biliary stent in situ, correctly positioned. Intrahepatic ducts "
                      "no longer dilated compared with the pre-procedure study. Bulky "
                      "oedematous pancreas with peripancreatic fluid. Splenomegaly with "
                      "portal cavernoma."),
         "impression": ("Decompressed biliary tree with a patent stent; interval "
                        "appearances of acute pancreatitis.")},
    ],
    "notes": [
        {"note_type": "Progress", "author_role": "hepatology",
         "collected_offset_days": -15,
         "text": ("Day 3 post-ERCP. Epigastric pain radiating to the back with vomiting "
                  "overnight. Amylase 106, mildly above the upper reference limit; "
                  "ultrasound shows a bulky oedematous pancreas with peripancreatic "
                  "fluid. Impression: post-ERCP acute pancreatitis, mild. Kept nil by "
                  "mouth with intravenous fluids and analgesia. Biliary stent is in a "
                  "good position and the intrahepatic ducts have decompressed. Platelets "
                  "60, in keeping with his known hypersplenism; no bleeding.")},
        {"note_type": "Discharge Summary", "author_role": "hepatology",
         "collected_offset_days": 0,
         "text": ("Nineteen days since ERCP and plastic stent placement for portal "
                  "biliopathy with a distal CBD stricture. His pancreatitis settled "
                  "conservatively over five days and he has been eating normally since. "
                  "Clinically he is brighter, itch has resolved and his stools have "
                  "darkened. He remains visibly jaundiced.\n\n"
                  "Bloods today: total bilirubin 17.77, up from 15.04 before the "
                  "procedure and 17.43 four days ago. Alkaline phosphatase 289, GGT 123 "
                  "(1358 pre-procedure), ALT 32 (237 pre-procedure). Platelets 100, the "
                  "best they have been this admission.\n\n"
                  "The team is asked whether the rising bilirubin means the stent has "
                  "failed and whether he should go back for repeat ERCP before "
                  "discharge. Of note he developed post-ERCP pancreatitis after the "
                  "index procedure.")},
    ],
    "vitals": {"temperature_c": "36.8", "heart_rate": "78", "bp": "112/68",
               "collected_offset_days": 0},
    "source_refs": [
        {"title": "Partner de-identified EHR export — patient-1 (lab_results.csv, "
                  "clinical-notes, ERCP and ultrasound reports)",
         "identifier": "partner-deid-export-v1/patient-1",
         "source_type": "benchmark", "role": "chart"},
    ],
    "declared_difficulty": "hard",
    "required_modalities": ["labs", "clinical notes", "ERCP procedure report"],
    "study_findings_policy": "visible",
    "ground_truth": {
        "answer": ("Continue current management and discharge with outpatient follow-up. "
                   "Do not re-intervene on the bilirubin alone."),
        "rationale": (
            "Drainage worked. GGT fell 1361 → 123 U/L (an eleven-fold drop) and ALT "
            "253 → 32 U/L; those are the enzymes that track biliary obstruction and "
            "hepatocellular injury, and both have essentially normalised. Conjugated "
            "bilirubin binds covalently to albumin as delta bilirubin and clears with "
            "albumin's half-life of roughly 17-20 days, so it lags enzyme "
            "normalisation by weeks and can rise transiently after decompression. The "
            "enzyme trajectory is the evidence; the bilirubin is the echo. Repeat ERCP "
            "would expose a patient who has already demonstrated post-ERCP "
            "pancreatitis, and whose platelets are 100 from hypersplenism, to a second "
            "avoidable procedural insult."),
        "key_data": [
            "GGT 1358 → 1361 → 237 → 123 U/L across the nineteen days",
            "ALT 237 → 253 → 56 → 32 U/L, now within the reference range",
            "ALP 666 → 289 U/L",
            "Bilirubin 15.04 → 17.43 → 17.77 mg/dL — the only value moving the wrong way",
            "Post-ERCP pancreatitis on day 3 (amylase 106 H) — a demonstrated susceptibility",
            "Platelets 48 → 100 from hypersplenism",
        ],
        "evidence": {
            "held_out": _HELD_OUT_NOTE,
            "subsequent_course": (
                "Two months later (day +60) GGT is 983 and ALP 723, both risen from 123 "
                "and 289, with bilirubin 5.50 — LOWER than at this decision point. That "
                "is real stent occlusion, and it looks completely different: the "
                "enzymes lead and the bilirubin follows. The contrast is what makes "
                "this decision point interpretable."),
        },
    },
    "hard_hook": "Bilirubin rises while every obstruction enzyme normalises.",
    "reasoning_divergence": (
        "Models anchor on the single rising number and recommend re-intervention, "
        "ignoring the enzyme trajectory that answers the question."),
}

QUESTION_A = (
    "Nineteen days after ERCP stenting for portal biliopathy, total bilirubin has "
    "risen from 15.04 to 17.77 mg/dL. What is your next step, and what in this "
    "chart supports it?"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Case B — v4-neph-001 · Volume-responsive AKI in cirrhosis, and an over-transfusion
# ═══════════════════════════════════════════════════════════════════════════════
# patient-3, F 76. Source rows, ``labs/lab_results.csv``:
#
#   date        Hgb   Plt  Creat   K    Alb  CRP     offset
#   2024-01-06   5.4   44   2.2   5.4   2.8  16.9      -2
#   2024-01-07   7.6   60    —     —     —    —        -1
#   2024-01-08  11.1   35   1.6   5.2    —    —         0   ← decision point
#   2024-01-09  10.2   46   1.5   5.5    —    —        +1   HELD OUT
#
# Routes to NEPHROLOGY because the promoted question is AKI differentiation in
# cirrhosis (pre-renal vs hepatorenal), which is a nephrology decision. The
# over-transfusion rides along as a second scored dimension.

CASE_B: Dict[str, Any] = {
    "case_id": "v4-neph-001",
    "case_source": V4_CASE_SOURCE,
    "specialty": "nephrology",
    "demographics": {"age_band": "75-84", "sex": "female"},
    "problem_list": [
        {"condition": "Chronic liver disease with portal hypertension",
         "since": "chronic", "collected_offset_days": -2},
        {"condition": "Ascites", "since": "chronic", "collected_offset_days": -2},
        {"condition": "Splenomegaly with hypersplenism", "since": "chronic",
         "collected_offset_days": -2},
        {"condition": "Thrombocytopenia", "since": "chronic", "collected_offset_days": -2},
        {"condition": "Severe anaemia on admission, transfused", "collected_offset_days": -2},
        {"condition": "Acute kidney injury", "collected_offset_days": -2},
    ],
    "medications": [
        {"drug": "Packed red cells — transfusion episode", "dose": "see nursing record",
         "route": "intravenous", "collected_offset_days": -2},
        {"drug": "Spironolactone", "dose": "100 mg", "route": "oral",
         "freq": "once daily — withheld on admission", "collected_offset_days": -2},
        {"drug": "Furosemide", "dose": "40 mg", "route": "oral",
         "freq": "once daily — withheld on admission", "collected_offset_days": -2},
        {"drug": "Pantoprazole", "dose": "40 mg", "route": "intravenous",
         "freq": "twice daily", "collected_offset_days": -2},
    ],
    "lab_panels": [
        {"panel": "Admission panel", "collected_offset_days": -2,
         "results": [
             {"analyte": "Haemoglobin", "value": 5.4, "unit": "g/dL",
              "ref_low": 12.0, "ref_high": 15.5, "flag": "L"},
             {"analyte": "Platelet count", "value": 44, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
             {"analyte": "Creatinine", "value": 2.2, "unit": "mg/dL",
              "ref_low": 0.5, "ref_high": 1.1, "flag": "H"},
             {"analyte": "Potassium", "value": 5.4, "unit": "mmol/L",
              "ref_low": 3.5, "ref_high": 5.1, "flag": "H"},
             {"analyte": "Albumin", "value": 2.8, "unit": "g/dL",
              "ref_low": 3.5, "ref_high": 5.0, "flag": "L"},
             {"analyte": "C-reactive protein", "value": 16.9, "unit": "mg/L",
              "ref_low": 0, "ref_high": 5, "flag": "H"},
         ]},
        {"panel": "Full blood count", "collected_offset_days": -1,
         "results": [
             {"analyte": "Haemoglobin", "value": 7.6, "unit": "g/dL",
              "ref_low": 12.0, "ref_high": 15.5, "flag": "L"},
             {"analyte": "Platelet count", "value": 60, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
         ]},
        {"panel": "Full blood count + renal panel", "collected_offset_days": 0,
         "results": [
             {"analyte": "Haemoglobin", "value": 11.1, "unit": "g/dL",
              "ref_low": 12.0, "ref_high": 15.5, "flag": "L"},
             {"analyte": "Platelet count", "value": 35, "unit": "10^3/uL",
              "ref_low": 150, "ref_high": 400, "flag": "L"},
             {"analyte": "Creatinine", "value": 1.6, "unit": "mg/dL",
              "ref_low": 0.5, "ref_high": 1.1, "flag": "H"},
             {"analyte": "Potassium", "value": 5.2, "unit": "mmol/L",
              "ref_low": 3.5, "ref_high": 5.1, "flag": "H"},
         ]},
    ],
    "studies": [
        {"modality": "other", "label": "Abdominal ultrasound with dopplers",
         "collected_offset_days": -2,
         "findings": ("Coarse, nodular liver with a shrunken right lobe. Moderate "
                      "ascites. Splenomegaly at 16 cm. Patent portal vein with "
                      "hepatopetal flow. Kidneys of normal size with preserved "
                      "corticomedullary differentiation and no hydronephrosis."),
         "impression": ("Chronic liver disease with portal hypertension and ascites. "
                        "Structurally normal kidneys.")},
    ],
    "notes": [
        {"note_type": "H&P", "author_role": "medicine",
         "collected_offset_days": -2,
         "text": ("Admitted with two days of black stool, light-headedness and one "
                  "episode of near-syncope. Known chronic liver disease with ascites "
                  "and splenomegaly. On arrival pale and clammy, heart rate 108, blood "
                  "pressure 96/54 lying. Abdomen distended with shifting dullness, no "
                  "tenderness, no asterixis, alert and orientated.\n\n"
                  "Haemoglobin 5.4, platelets 44, creatinine 2.2 with potassium 5.4, "
                  "albumin 2.8, CRP 16.9. Baseline creatinine from clinic six weeks ago "
                  "was 0.9.\n\n"
                  "Plan: transfuse, hold diuretics, intravenous pantoprazole, urgent "
                  "gastroscopy, hourly urine output. Renal team asked to advise on the "
                  "acute kidney injury.")},
        {"note_type": "Progress", "author_role": "medicine",
         "collected_offset_days": 0,
         "text": ("Forty-eight hours in. Haemodynamically settled — heart rate 82, "
                  "blood pressure 118/70. No further melaena. Passing good volumes of "
                  "urine.\n\n"
                  "Haemoglobin has come up 5.4 → 7.6 → 11.1 over the two days on the "
                  "transfusion programme. Platelets 35 today. Creatinine has fallen "
                  "2.2 → 1.6 and potassium 5.4 → 5.2.\n\n"
                  "The night team have written that the anaemia is 'now corrected' and "
                  "asked whether to continue the transfusion programme as prescribed. "
                  "The admitting note carries a query of hepatorenal syndrome and a "
                  "question about starting terlipressin. Renal review requested for both.")},
    ],
    "vitals": {"heart_rate": "82", "bp": "118/70", "temperature_c": "36.6",
               "collected_offset_days": 0},
    "source_refs": [
        {"title": "Partner de-identified EHR export — patient-3 (lab_results.csv, "
                  "clinical-notes, abdominal ultrasound report)",
         "identifier": "partner-deid-export-v1/patient-3",
         "source_type": "benchmark", "role": "chart"},
    ],
    "declared_difficulty": "hard",
    "required_modalities": ["labs", "clinical notes"],
    "study_findings_policy": "visible",
    "ground_truth": {
        "answer": ("Stop transfusing — the haemoglobin target is 7-8 g/dL and it has "
                   "been exceeded. The acute kidney injury is pre-renal and "
                   "volume-responsive, not hepatorenal; do not start terlipressin."),
        "rationale": (
            "Restrictive transfusion is standard in acute upper gastrointestinal "
            "bleeding in chronic liver disease: raising haemoglobin beyond 7-8 g/dL "
            "raises portal pressure and increases rebleeding and mortality. Reaching "
            "11.1 g/dL overshoots that target by roughly three units' worth in exactly "
            "the population where overshooting causes harm, and the falling platelet "
            "count alongside it is consistent with dilution rather than recovery. "
            "Separately, a creatinine falling 2.2 → 1.6 mg/dL with volume repletion "
            "and diuretic withdrawal EXCLUDES hepatorenal syndrome, which is defined "
            "by non-response to volume expansion. This is pre-renal azotaemia and it "
            "is already being treated correctly; a splanchnic vasoconstrictor would "
            "treat a diagnosis the chart has ruled out."),
        "key_data": [
            "Haemoglobin 5.4 → 7.6 → 11.1 g/dL in 48 hours — past the 7-8 g/dL target",
            "Platelets 44 → 60 → 35 in a patient with portal hypertension",
            "Creatinine 2.2 → 1.6 mg/dL with volume, off diuretics",
            "Potassium 5.4 → 5.2 mmol/L",
            "Ultrasound: structurally normal kidneys, no obstruction",
            "Baseline creatinine 0.9 mg/dL six weeks earlier",
        ],
        "evidence": {
            "held_out": _HELD_OUT_NOTE,
            "subsequent_course": (
                "The following day (day +1) creatinine is 1.5 mg/dL and haemoglobin "
                "10.2 g/dL, confirming both readings: the renal trajectory continued "
                "to respond to volume, and the haemoglobin drifted back down without "
                "further transfusion."),
        },
    },
    "hard_hook": "A correction that looks like success is the error.",
    "reasoning_divergence": (
        "Models congratulate the haemoglobin recovery and miss that the target was "
        "exceeded in exactly the population where exceeding it causes harm — and "
        "reach for terlipressin for an AKI the trajectory has already excluded."),
}

QUESTION_B = (
    "Haemoglobin has risen from 5.4 to 11.1 g/dL over 48 hours in a patient with "
    "chronic liver disease, ascites and platelets of 35. Creatinine has fallen from "
    "2.2 to 1.6 mg/dL. Assess the management."
)


# ═══════════════════════════════════════════════════════════════════════════════
# Case C — v4-card-001 · Troponin elevation in acute cerebrovascular event
# ═══════════════════════════════════════════════════════════════════════════════
# patient-4, M 45. Source rows, ``labs/lab_results.csv``:
#
#   date        HCO3   K     Na     Urea Creat Trop   Amylase Lipase  offset
#   2024-12-19   8.4 L 5.84H 131.3L  35   0.97   —      —      —        -1
#   2024-12-20  20.0   4.0   137.0    —    —    0.855H 146 H  242 H      0  ← decision
#   2024-12-22  21.4   3.4 L 134.0L  16   0.47L  —      —      —        +2  HELD OUT
#
# Routes to CARDIOLOGY: the question is how to interpret and act on a troponin,
# which is a cardiology decision even though the confounder is neurological.
#
# ── NO TRACING, AND THAT IS THE HONEST STATE ──────────────────────────────────
# A cardiology case normally must carry ≥1 ``ecg``/``echo`` study, and for an
# AUTHORED case that gate stays hard: the generator could always produce one, so
# a missing tracing there is an authoring bug.
#
# This is a real chart, and patient-4's ECG report is not among the artifacts the
# source bundle was measured for. The two ways to satisfy a hard gate here would
# be to fabricate a tracing inside a record stamped ``case_source: real_deid``, or
# to relabel the case into a specialty that does not describe it — both worse than
# shipping a real case with a named gap. So ``_assert_specialty_studies`` treats
# the requirement as ADVISORY for real charts, this case loads, and the gap is
# reported by ``V4_STUDY_GAPS`` rather than inferred from a missing row.
#
# It also happens to be the case's own clinical point: the answer is that you do
# not act on the troponin until you have characterised it, and the ECG is one of
# the things the physician is being asked to go and get.
#
# Attaching the real report to ``CASE_C["studies"]`` closes the gap with no code
# change — the loader re-checks on every call.
_CASE_C_STUDY_GAP = (
    "no ECG/echo: patient-4's tracing is not in the source bundle. The case ships "
    "with the gap named rather than with a fabricated study. Attach the real "
    "report to CASE_C['studies'] as {'modality': 'ecg', 'label': '12-lead ECG', "
    "'collected_offset_days': 0, 'findings': <report text>} to close it."
)

CASE_C: Dict[str, Any] = {
    "case_id": "v4-card-001",
    "case_source": V4_CASE_SOURCE,
    "specialty": "cardiology",
    "demographics": {"age_band": "45-54", "sex": "male"},
    "problem_list": [
        {"condition": "Acute cerebrovascular event, subtype not yet characterised",
         "collected_offset_days": -1},
        {"condition": "Seizure disorder", "since": "chronic", "collected_offset_days": -1},
        {"condition": "Type 2 diabetes mellitus", "since": "chronic",
         "collected_offset_days": -1},
        {"condition": "Hypertension", "since": "chronic", "collected_offset_days": -1},
        {"condition": "High anion gap metabolic acidosis with hyperkalaemia, resolving",
         "collected_offset_days": -1},
    ],
    "medications": [
        {"drug": "Levetiracetam", "dose": "500 mg", "route": "intravenous",
         "freq": "twice daily", "collected_offset_days": -1},
        {"drug": "Insulin — variable rate intravenous infusion", "route": "intravenous",
         "collected_offset_days": -1},
        {"drug": "Sodium chloride 0.9% intravenous fluids", "route": "intravenous",
         "collected_offset_days": -1},
        {"drug": "Amlodipine", "dose": "5 mg", "route": "oral", "freq": "once daily",
         "collected_offset_days": -1},
    ],
    "lab_panels": [
        {"panel": "Arterial blood gas + renal panel", "collected_offset_days": -1,
         "results": [
             {"analyte": "Bicarbonate", "value": 8.4, "unit": "mmol/L",
              "ref_low": 22, "ref_high": 29, "flag": "L"},
             {"analyte": "Potassium", "value": 5.84, "unit": "mmol/L",
              "ref_low": 3.5, "ref_high": 5.1, "flag": "H"},
             {"analyte": "Sodium", "value": 131.3, "unit": "mmol/L",
              "ref_low": 136, "ref_high": 145, "flag": "L"},
             {"analyte": "Urea", "value": 35, "unit": "mg/dL",
              "ref_low": 7, "ref_high": 20, "flag": "H"},
             {"analyte": "Creatinine", "value": 0.97, "unit": "mg/dL",
              "ref_low": 0.7, "ref_high": 1.3, "flag": ""},
         ]},
        {"panel": "Cardiac and pancreatic enzymes + renal panel",
         "collected_offset_days": 0,
         "results": [
             {"analyte": "Troponin I", "value": 0.855, "unit": "ng/mL",
              "ref_low": 0, "ref_high": 0.04, "flag": "H"},
             {"analyte": "Bicarbonate", "value": 20.0, "unit": "mmol/L",
              "ref_low": 22, "ref_high": 29, "flag": "L"},
             {"analyte": "Potassium", "value": 4.0, "unit": "mmol/L",
              "ref_low": 3.5, "ref_high": 5.1, "flag": ""},
             {"analyte": "Sodium", "value": 137.0, "unit": "mmol/L",
              "ref_low": 136, "ref_high": 145, "flag": ""},
             {"analyte": "Amylase", "value": 146, "unit": "U/L",
              "ref_low": 25, "ref_high": 125, "flag": "H"},
             {"analyte": "Lipase", "value": 242, "unit": "U/L",
              "ref_low": 13, "ref_high": 60, "flag": "H"},
         ]},
    ],
    "studies": [
        # Populate with the real 12-lead ECG report to release this case —
        # see _CASE_C_MISSING_STUDY_HINT above. Nothing is invented here.
    ],
    "notes": [
        {"note_type": "H&P", "author_role": "medicine",
         "collected_offset_days": -1,
         "text": ("45-year-old man brought in after being found down at home with "
                  "right-sided weakness and expressive difficulty of unknown onset "
                  "time. Known seizure disorder, type 2 diabetes and hypertension; "
                  "adherence to medication reportedly poor for several weeks.\n\n"
                  "On arrival drowsy but rousable, GCS 13/15 (E3 V4 M6), dense right "
                  "hemiparesis with power 2/5 in the right arm and 3/5 in the right "
                  "leg. No witnessed seizure in the department.\n\n"
                  "Markedly deranged bloods: bicarbonate 8.4, potassium 5.84, sodium "
                  "131.3, urea 35 with a creatinine of 0.97. High anion gap metabolic "
                  "acidosis in a poorly controlled diabetic. Started on intravenous "
                  "fluids and a variable-rate insulin infusion; levetiracetam continued. "
                  "Urgent neuroimaging and stroke team review requested.")},
        {"note_type": "Progress", "author_role": "medicine",
         "collected_offset_days": 0,
         "text": ("Day 2. The metabolic picture has largely corrected on fluids and "
                  "insulin — bicarbonate 20.0 from 8.4, potassium 4.0 from 5.84, sodium "
                  "137. He is more alert. The right hemiparesis is unchanged.\n\n"
                  "A troponin I sent with this morning's bloods has returned at 0.855 "
                  "ng/mL against an upper reference limit of 0.04. He has not "
                  "complained of chest pain at any point and is not complaining of it "
                  "now, although his expressive difficulty limits the history. Amylase "
                  "146 and lipase 242 are also above range without abdominal signs.\n\n"
                  "The covering team have asked whether to load him with dual "
                  "antiplatelets and start therapeutic anticoagulation for a presumed "
                  "acute coronary syndrome, and whether to refer for urgent "
                  "angiography. Stroke subtype and infarct volume are not yet "
                  "established. Cardiology opinion requested.")},
    ],
    "vitals": {"heart_rate": "94", "bp": "158/88", "temperature_c": "37.1",
               "collected_offset_days": 0},
    "source_refs": [
        {"title": "Partner de-identified EHR export — patient-4 (lab_results.csv, "
                  "clinical-notes)",
         "identifier": "partner-deid-export-v1/patient-4",
         "source_type": "benchmark", "role": "chart"},
    ],
    "declared_difficulty": "hard",
    # Declares what this chart DELIVERS. "ecg" is deliberately not claimed: the
    # source bundle carries no tracing (see _CASE_C_STUDY_GAP), and declaring a
    # modality we cannot deliver is the completeness over-claim the ingest gate
    # exists to catch. Obtaining the ECG is part of the ANSWER, not the evidence.
    "required_modalities": ["labs", "clinical notes"],
    "study_findings_policy": "visible",
    "ground_truth": {
        "answer": ("Do not anticoagulate or load antiplatelets on this troponin. "
                   "Characterise first: 12-lead ECG for a dynamic ischaemic pattern, "
                   "serial troponins to establish whether there is a rise-and-fall "
                   "curve, echocardiography for regional wall motion, and a confirmed "
                   "stroke subtype with infarct volume."),
        "rationale": (
            "Troponin elevation is common in acute stroke from neurogenic myocardial "
            "injury and demand ischaemia; it correlates with stroke severity and does "
            "not by itself establish acute coronary syndrome. Antithrombotic therapy "
            "in the first 48 hours of an acute cerebrovascular event, before the "
            "stroke has been characterised as ischaemic or haemorrhagic and before "
            "infarct size is known, risks haemorrhagic transformation. A single "
            "troponin drawn during systemic derangement — bicarbonate 8.4 with a "
            "potassium of 5.84 the day before — is precisely the setting in which "
            "demand ischaemia produces a number like this, and the concurrently "
            "raised amylase and lipase without abdominal signs point the same way: "
            "a systemic insult, not one organ's diagnosis."),
        "key_data": [
            "Troponin I 0.855 ng/mL against an upper reference limit of 0.04 — a single value",
            "No chest pain at any point",
            "Bicarbonate 8.4 and potassium 5.84 the previous day, corrected within 24 hours",
            "Amylase 146 and lipase 242 raised without abdominal signs",
            "Stroke subtype and infarct volume not yet established",
        ],
        "evidence": {
            "held_out": _HELD_OUT_NOTE,
            "subsequent_course": (
                "Two days later (day +2) bicarbonate is 21.4, potassium 3.4, sodium "
                "134, urea 16 and creatinine 0.47 — the systemic derangement has fully "
                "resolved, which is the trajectory of demand ischaemia in a systemic "
                "insult rather than of an evolving coronary event."),
        },
    },
    "hard_hook": "A number that means ACS in one context and demand ischaemia in this one.",
    "reasoning_divergence": (
        "Models treat the troponin as the primary finding and reach for antithrombotics "
        "without asking what the concurrent, uncharacterised stroke does to that "
        "decision."),
}

QUESTION_C = (
    "Day 2 of admission for an acute cerebrovascular event, troponin returns at "
    "0.855 ng/mL. What do you do, and what must you know first?"
)


# ═══════════════════════════════════════════════════════════════════════════════
# The registry
# ═══════════════════════════════════════════════════════════════════════════════
# Each entry mirrors ``gold_cases``: the case, the clinical question, and the
# authored A/B preference pair a specialist annotates. ``intended_flawed_id`` names
# the SEDUCTIVE SHORTCUT — the answer a frontier model gives — so the pair is a
# preference pair and not two guesses.

V4_REAL_CASES: List[Dict[str, Any]] = [
    {
        "case_id": "v4-hep-001",
        "title": "Bilirubin lag after successful biliary drainage",
        "ai_failure_mode": ("anchoring (one rising number → drainage failed → repeat "
                            "ERCP) despite an eleven-fold GGT fall; overtreatment with "
                            "a demonstrated procedural risk"),
        "taxonomy_bucket": "biliary_obstruction",
        "subtopic": "post-ERCP complications",
        "question": QUESTION_A,
        "partner_bundle": "patient-1",
        "intended_flawed_id": "B",
        "candidate_answers": [
            {"id": "A", "text": (
                "Drainage has worked and the bilirubin is a lagging indicator, not evidence "
                "of failure. GGT has fallen 1361 to 123 U/L and ALT 253 to 32 U/L — the "
                "enzymes that track biliary obstruction and hepatocellular injury have both "
                "essentially normalised, and ALP has fallen 666 to 289. Conjugated bilirubin "
                "bound covalently to albumin (delta bilirubin) clears with albumin's 17-20 "
                "day half-life, so it lags enzyme normalisation by weeks and can rise "
                "transiently after decompression. Continue current management and follow the "
                "enzymes as outpatient; re-image only if GGT or ALP turn back up. Repeat ERCP "
                "now would expose a man who developed post-ERCP pancreatitis three days after "
                "the index procedure, with platelets of 100 from hypersplenism, to a second "
                "avoidable pancreatitis and bleeding risk for a number that is behaving "
                "exactly as expected.")},
            {"id": "B", "text": (
                "A total bilirubin rising from 15.04 to 17.77 mg/dL nineteen days after "
                "stenting indicates that the biliary stent is not draining adequately — "
                "either it has migrated or it is occluding early, which is common in portal "
                "biliopathy where the stricture is extrinsic. He should return to ERCP for "
                "cholangiography with stent exchange or upsizing before discharge, since "
                "persistent obstructive jaundice risks secondary biliary cirrhosis and "
                "cholangitis. The falling enzymes reflect the initial decompression but the "
                "bilirubin is the functional endpoint and it is going the wrong way.")},
        ],
        "case": CASE_A,
    },
    {
        "case_id": "v4-neph-001",
        "title": "Volume-responsive AKI in cirrhosis, and an over-transfusion",
        "ai_failure_mode": ("congratulating a corrected number (Hgb 5.4 → 11.1) in the one "
                            "population where exceeding the target causes harm; treating the "
                            "HRS label rather than the volume response"),
        "taxonomy_bucket": "aki_critical_care",
        "subtopic": "hepatorenal",
        "question": QUESTION_B,
        "partner_bundle": "patient-3",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "This is a successful resuscitation. The haemoglobin has been restored from "
                "5.4 to 11.1 g/dL, she is haemodynamically stable, and the creatinine has "
                "improved from 2.2 to 1.6 mg/dL with the improvement in perfusion. Continue "
                "the transfusion programme to complete correction and maintain the "
                "haemoglobin, keep the diuretics held, and because the acute kidney injury in "
                "a cirrhotic with ascites is most likely hepatorenal syndrome, start "
                "terlipressin with albumin to consolidate the renal recovery. Repeat the "
                "gastroscopy if there is any further melaena.")},
            {"id": "B", "text": (
                "Two things are wrong here. First, she has been over-transfused: restrictive "
                "transfusion targeting 7-8 g/dL is standard in acute upper gastrointestinal "
                "bleeding in chronic liver disease because raising the haemoglobin further "
                "raises portal pressure and increases rebleeding and mortality. She passed "
                "the target at 7.6 and is now 11.1 — stop transfusing and do not give further "
                "units in the absence of active bleeding. Second, the acute kidney injury is "
                "pre-renal, not hepatorenal: a creatinine falling 2.2 to 1.6 mg/dL with "
                "volume repletion and diuretic withdrawal excludes HRS-AKI, which is defined "
                "by non-response to volume expansion. Do not start terlipressin. Continue "
                "volume repletion judiciously, keep the diuretics held, monitor the potassium, "
                "and treat the bleeding source.")},
        ],
        "case": CASE_B,
    },
    {
        "case_id": "v4-card-001",
        "title": "Troponin elevation in acute cerebrovascular event",
        "ai_failure_mode": ("treating a single troponin as the primary finding and reaching "
                            "for antithrombotics without asking what an uncharacterised "
                            "concurrent stroke does to that decision; catastrophic "
                            "unsafe_recommendation (haemorrhagic transformation)"),
        "taxonomy_bucket": "acs_nuance",
        "subtopic": "troponin_interpretation",
        "question": QUESTION_C,
        "partner_bundle": "patient-4",
        "intended_flawed_id": "A",
        "candidate_answers": [
            {"id": "A", "text": (
                "A troponin I of 0.855 ng/mL against an upper reference limit of 0.04 is a "
                "twenty-fold elevation and, in a 45-year-old diabetic hypertensive, "
                "represents an acute coronary syndrome until proven otherwise — the absence "
                "of chest pain is expected in diabetic and neurologically impaired patients. "
                "Load with aspirin and a P2Y12 inhibitor, start therapeutic anticoagulation, "
                "and refer for urgent invasive angiography. Delaying revascularisation in "
                "NSTEMI worsens outcome, and the concurrent stroke makes cardioembolic "
                "disease more likely rather than less, which strengthens the case for "
                "anticoagulation.")},
            {"id": "B", "text": (
                "Do not give antithrombotics on this troponin. Troponin rises in acute stroke "
                "from neurogenic myocardial injury and demand ischaemia; it is common, it "
                "tracks stroke severity, and it does not by itself establish ACS. This is a "
                "single value with no chest pain, drawn the day after a bicarbonate of 8.4 "
                "with a potassium of 5.84 — a systemic derangement that has corrected within "
                "24 hours — and with amylase and lipase also raised without abdominal signs, "
                "which points to a systemic insult rather than one organ's diagnosis. "
                "Antithrombotic therapy in the first 48 hours of an acute cerebrovascular "
                "event, before the stroke is characterised as ischaemic or haemorrhagic and "
                "before infarct volume is known, risks haemorrhagic transformation. "
                "Characterise first: 12-lead ECG for a dynamic ischaemic pattern, serial "
                "troponins for a rise-and-fall curve, echocardiography for regional wall "
                "motion, and confirmed stroke subtype with infarct volume. Then decide.")},
        ],
        "case": CASE_C,
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# Validation + holds
# ═══════════════════════════════════════════════════════════════════════════════

def all_v4_cases() -> List[Dict[str, Any]]:
    return list(V4_REAL_CASES)


def _structural_errors(entry: Dict[str, Any]) -> List[str]:
    """Errors that make an entry unusable regardless of clinical content: a shape
    ``ClinicalCase`` would reject, or a missing A/B pair. These are BUGS in this
    file and must fail loudly at import — never be silently held."""
    from pydantic import ValidationError

    from asclepius.cases import ClinicalCase

    errors: List[str] = []
    cid = entry.get("case_id", "<no-id>")
    try:
        # ``extra="forbid"`` all the way down, so this catches the PRD's sketched
        # ``label`` / ``kind`` / ``offset_days`` keys and anything like them.
        ClinicalCase(**entry["case"])
    except ValidationError as exc:
        errors.append(f"{cid}: does not validate as a ClinicalCase: {exc}")
    if entry["case"].get("case_source") != V4_CASE_SOURCE:
        errors.append(f"{cid}: case_source must be {V4_CASE_SOURCE!r} — these are real charts")
    cands = entry.get("candidate_answers") or []
    if len(cands) != 2 or entry.get("intended_flawed_id") not in ("A", "B"):
        errors.append(f"{cid}: missing a valid A/B preference pair")
    if not (entry.get("question") or "").strip():
        errors.append(f"{cid}: missing a clinical question")
    return errors


def _content_hold(entry: Dict[str, Any]) -> Optional[str]:
    """The reason this case cannot be served yet, or None.

    Distinct from ``_structural_errors`` on purpose. A structural error is our bug
    and raises. A content hold is a statement about the DATA — "this cardiology
    case has no ECG" — and the honest response to it is to hold the case and say
    so, not to fabricate the missing artifact or to relax the gate that noticed."""
    from asclepius.cases import MultimodalContentError, assert_multimodal_content

    try:
        assert_multimodal_content(entry["case"])
    except MultimodalContentError as exc:
        hint = _HOLD_HINTS.get(entry["case_id"])
        return f"{exc}" + (f" — {hint}" if hint else "")
    return None


_HOLD_HINTS: Dict[str, str] = {}

#: ``{case_id: reason}`` — a case that SHIPS but is missing a study its specialty
#: would normally require, because the real chart did not contain one. Reported by
#: ``load_v4_cases`` so the gap is read, not inferred from a study list nobody
#: opened. Distinct from ``V4_HOLDS``: a gap ships, a hold does not.
_STUDY_GAPS: Dict[str, str] = {
    "v4-card-001": _CASE_C_STUDY_GAP,
}


def V4_STUDY_GAPS() -> Dict[str, str]:
    """``{case_id: reason}`` for every case shipping without a study its specialty
    would normally require. Recomputed, so attaching the real study clears it."""
    from asclepius.cases import missing_specialty_studies

    out: Dict[str, str] = {}
    for entry in _validated():
        case = entry["case"]
        if missing_specialty_studies(case["specialty"], case.get("studies") or []):
            out[entry["case_id"]] = _STUDY_GAPS.get(
                entry["case_id"], f"{case['specialty']} case ships without the study "
                                  "modality its specialty would normally require")
    return out


@functools.lru_cache(maxsize=1)
def _validated() -> List[Dict[str, Any]]:
    """Every entry, structurally validated. Raises on a bug in this file.

    Cached because the set is static module data and this runs on the serving
    path. Content HOLDS are deliberately NOT cached here — they are re-evaluated
    on every ``load_v4_cases`` call, so attaching a missing study releases the
    case without a restart."""
    errors: List[str] = []
    seen = set()
    for entry in all_v4_cases():
        errors.extend(_structural_errors(entry))
        if entry["case_id"] in seen:
            errors.append(f"duplicate case_id: {entry['case_id']}")
        seen.add(entry["case_id"])
        if entry["case_id"] != entry["case"].get("case_id"):
            errors.append(f"{entry['case_id']}: entry/case case_id disagree")
    if errors:
        raise ValueError("V4 real cases are malformed (" + str(len(errors))
                         + " error(s)): " + "; ".join(errors[:5]))
    return list(V4_REAL_CASES)


def V4_HOLDS() -> Dict[str, str]:
    """``{case_id: reason}`` for every case currently held out of the queue.

    Empty is the goal state. A non-empty entry is a promise NOT kept and is
    surfaced by ``load_v4_cases`` and by the admin endpoint rather than logged
    into a void — a case silently missing from the queue is the failure mode this
    whole PRD is about."""
    return {e["case_id"]: r for e in _validated()
            if (r := _content_hold(e)) is not None}


# ═══════════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════════
#: Independent labels per V4 case (V4 PRD §4): one labeller plus two independent
#: for Cohen's kappa. NOT 60 — visibility and paid labels are different things,
#: and at $75/case ``max_labels=60`` is $4,500 for one case.
# ═══════════════════════════════════════════════════════════════════════════════
# The partner bundles themselves (Longitudinal E2E PRD §2.2)
# ═══════════════════════════════════════════════════════════════════════════════
# The three cases above were hand-authored FROM patient-1, -3 and -4; the charts
# they were read out of never entered the ingestion pipeline, which is why the
# Longitudinal batch read ``0 trajectories · 0 points`` for as long as it did.
# The bundles now live under ``asclepius/fixtures/patient_bundles/`` and go in
# through the real partner door (``patient_fixtures.ingest_committed_bundles``).
#
# **The specialty map is here, not there, on purpose.** None of the four bundles
# carries a ``manifest.json``, so ingestion would resolve every one of them to
# ``general`` — a wrong specialty routes a case to the wrong pool and mislabels it
# in the export, invisibly, which is the exact failure ``specialty not set`` exists
# to prevent. The knowledge of what each chart IS was already encoded in this file
# by the hand-authored cases; putting the map anywhere else would create a second
# place to be wrong about it. ``patient-2`` has no authored case above and is named
# here from its own README (serial tumour markers and serial radiology).
#
# For a HOSPITAL upload the fix is not this map — it is Box 1's inline specialty
# editor (``POST /uploads/{id}/specialty``). This map covers exactly the four
# committed fixtures and nothing else.
FIXTURE_BUNDLE_SPECIALTIES: Dict[str, str] = {
    "patient-1": "hepatology",
    "patient-2": "oncology",
    "patient-3": "nephrology",
    "patient-4": "cardiology",
}


V4_DEFAULT_MAX_LABELS = 3


def v4_task_id(case_id: str) -> str:
    """The stable task id for a V4 case. Stable so loading is idempotent."""
    return "v4real-" + case_id


def load_v4_cases(
    store: Any, *, specialty: Optional[str] = None,
    max_labels: int = V4_DEFAULT_MAX_LABELS,
    open_to_all_specialties: bool = False,
    reconcile_visibility: bool = False,
) -> Dict[str, Any]:
    """Insert the V4 real cases as ready-to-serve ``partner_ehr`` tasks, idempotently.

    Mirrors ``gold_cases.load_gold_cases`` and needs NO LLM: each case ships with
    its authored A/B preference pair, so it is a complete V4 task the moment it
    lands. Returns ``{loaded, skipped, held, total, task_ids, holds}``.

    ``specialty`` filters WHICH cases load; the case's own specialty is
    authoritative and is what the task is tagged with, never the argument — a
    mismatched argument loads nothing rather than mislabelling a chart.

    ``open_to_all_specialties`` widens VISIBILITY only (V4 PRD §4). It does not
    touch ``max_labels`` and therefore does not change what we pay.

    ``reconcile_visibility`` also applies that flag to tasks that ALREADY EXIST
    (counted as ``revisited``). Off by default and deliberately so — this splits
    two jobs that look like one:

      * *create what is missing*, which is safe to run on any request and is what
        the ``/tasks/next`` backstop does;
      * *change who can see the existing cases*, which is an *operator* decision
        and belongs to boot and to the explicit admin route only.

    Without the split, a physician drawing a case would silently rewrite the
    visibility of the whole real corpus as a side effect of asking for work. With
    it off, the seed is idempotent on task id exactly as before; with it on, a
    deployed database picks up a changed setting instead of staying on whatever it
    was first created with.
    """
    from asclepius.cases import case_type_signature, render_case_prompt

    want = (specialty or "").strip().lower()
    eligible = [e for e in _validated()
                if not want or e["case"].get("specialty") == want]

    loaded, skipped, task_ids, revisited = 0, 0, [], 0
    holds: Dict[str, str] = {}
    for entry in eligible:
        # Re-checked per call, not cached: attaching a missing study to a held case
        # must release it without a process restart.
        hold = _content_hold(entry)
        if hold is not None:
            holds[entry["case_id"]] = hold
            log.warning("V4 case %s is HELD and will not be served: %s",
                        entry["case_id"], hold)
            continue
        tid = v4_task_id(entry["case_id"])
        if store.get_task(tid):
            skipped += 1
            # A deployed database already holds these three tasks, so a seed that
            # only ever set this flag at INSERT would apply a changed setting to a
            # fresh install and to nothing else — correct in a test, wrong in
            # production, and an approved physician looking at an empty queue while
            # the cases sit in the table. VISIBILITY only (see the store method),
            # and only when the caller asked for it (see the docstring).
            if reconcile_visibility and store.set_task_open_to_all_specialties(
                    tid, bool(open_to_all_specialties)):
                revisited += 1
            continue
        case = entry["case"]
        prompt = render_case_prompt(case, entry["question"])
        # A DECLARED, unmeasured difficulty. These are real decision points chosen
        # because the obvious move is wrong, but "hard" is a claim until frontier
        # models have actually failed them — which is step 3 of the pipeline and
        # happens through ``grade-real-models``, not here. ``measured=False`` keeps
        # the claim honest; the serving gate enforces a measured floor only when
        # ``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY`` is on.
        empirical = {
            "value": None,
            "declared": 0.8,
            "measured": False,
            "both_axes": True,
            "note": ("real de-identified decision point; awaits live "
                     "grade-real-models measurement before any 'hard' claim ships "
                     "(PRD §9, V4 PRD §3.1)"),
        }
        store.insert_task(
            task_id=tid,
            prompt=prompt,
            specialty=case["specialty"],
            difficulty="hard",
            capture_reasoning=True,
            source="partner_ehr",
            candidate_answers=list(entry["candidate_answers"]),
            case=case,
            max_labels=max(1, int(max_labels or 1)),
            generation={
                "mode": "v4_real_seed",
                "case_id": entry["case_id"],
                "title": entry.get("title"),
                "ai_failure_mode": entry.get("ai_failure_mode"),
                "taxonomy_bucket": entry.get("taxonomy_bucket"),
                "subtopic": entry.get("subtopic"),
                "partner_bundle": entry.get("partner_bundle"),
                "case_type": case_type_signature(case),
                "empirical_difficulty": empirical,
                "intended_flawed_id": entry["intended_flawed_id"],
                "modality": "multimodal",
                "case_source": V4_CASE_SOURCE,
                "question": entry["question"],
            },
            created_by="system:v4_real_seed",
            open_to_all_specialties=bool(open_to_all_specialties),
        )
        loaded += 1
        task_ids.append(tid)
    return {"loaded": loaded, "skipped": skipped, "held": len(holds),
            "total": len(eligible), "task_ids": task_ids, "holds": holds,
            # How many ALREADY-PRESENT tasks had their visibility corrected on this
            # call. Non-zero means a deployed queue was just widened (or narrowed)
            # in place — worth a boot log line, because it changes who sees what.
            "revisited": revisited,
            # A case that SHIPS with a named gap. Not a hold — it is in the queue —
            # but an operator should read it rather than discover an empty study list.
            "study_gaps": {cid: why for cid, why in V4_STUDY_GAPS().items()
                           if not want or cid in {e["case_id"] for e in eligible}}}
