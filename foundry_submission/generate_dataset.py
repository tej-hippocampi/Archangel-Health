#!/usr/bin/env python3
"""
Archangel Episode OS — notional TEAM cohort generator (PRD §7).

No PHI. Deterministic (seeded). Emits CSVs that land 1:1 into the Foundry
Ontology object types in instructions/ONTOLOGY_SPEC.md. Enums are copied
verbatim from the repo so Pipeline Builder mapping is a straight column->property:

  - ProcedureFamily / Tier / TierReasonKind ...... backend/triage/types.py
  - DailyCheckin* / RedFlagSymptom / IncisionFlag  backend/triage/postop/types.py
  - G-code ladder + ride-alone ................... backend/telehealth/gcodes.py

Run:  python3 generate_dataset.py
Out:  ./data/*.csv  (+ data/_manifest.json)
"""
from __future__ import annotations

import csv
import json
import os
import random
from datetime import date, timedelta

random.seed(20260613)  # demo date — reproducible cohort

OUT = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT, exist_ok=True)

# ── Repo-faithful enums ──────────────────────────────────────────────────────
FAMILIES = ["LEJR", "HIP_FEMUR_FRACTURE", "SPINAL_FUSION", "CABG", "MAJOR_BOWEL"]
TIERS = ["TIER_1", "TIER_2", "TIER_3"]
CHECKIN_TIER = ["GREEN", "ORANGE", "RED"]
INCISION_FLAGS = ["NEW_REDNESS_SPREADING", "NEW_DRAINAGE", "OPENING_OR_GAPING",
                  "BAD_SMELL", "INCREASED_PAIN_AT_INCISION"]
RED_FLAGS = ["CHEST_PAIN", "SUDDEN_TROUBLE_BREATHING", "SUDDEN_WEAKNESS_ONE_SIDE",
             "SEVERE_OR_NEW_BLEEDING", "CONFUSION_MENTAL_CHANGE",
             "CALF_SWELLING_OR_PAIN", "SEVERE_HEADACHE", "FAINTING_OR_NEAR_FAINTING"]

# Anchor CPT + notional CMS-style TEAM target price per family (regionally blended).
FAMILY_META = {
    #                anchor_cpt   target$   count   readmit%   base_surg%ofTarget
    "LEJR":              ("27447", 21800, 130, 0.11, 0.74),
    "HIP_FEMUR_FRACTURE":("27236", 28400,  50, 0.20, 0.70),
    "SPINAL_FUSION":     ("22612", 34200,  45, 0.17, 0.72),
    "CABG":              ("33533", 48600,  35, 0.22, 0.76),
    "MAJOR_BOWEL":       ("44140", 38100,  40, 0.19, 0.71),
}

# ICD-10 problem pools per family (anchor + plausible comorbidities).
ICD_POOL = {
    "LEJR":  [("M17.11", "Unilateral primary osteoarthritis, right knee"),
              ("M16.11", "Unilateral primary osteoarthritis, right hip"),
              ("E11.9", "Type 2 diabetes mellitus without complications"),
              ("I10", "Essential hypertension"), ("E66.9", "Obesity, unspecified")],
    "HIP_FEMUR_FRACTURE": [("S72.001A", "Fracture of unspecified part of neck of right femur"),
              ("M81.0", "Age-related osteoporosis"), ("I10", "Essential hypertension"),
              ("F03.90", "Unspecified dementia"), ("E11.9", "Type 2 diabetes mellitus")],
    "SPINAL_FUSION": [("M48.06", "Spinal stenosis, lumbar region"),
              ("M51.36", "Other intervertebral disc degeneration, lumbar"),
              ("E11.9", "Type 2 diabetes mellitus"), ("F17.210", "Nicotine dependence, cigarettes"),
              ("I10", "Essential hypertension")],
    "CABG": [("I25.10", "Atherosclerotic heart disease of native coronary artery"),
              ("I50.22", "Chronic systolic congestive heart failure"),
              ("E11.9", "Type 2 diabetes mellitus"), ("N18.3", "Chronic kidney disease, stage 3"),
              ("J44.9", "COPD, unspecified")],
    "MAJOR_BOWEL": [("C18.9", "Malignant neoplasm of colon, unspecified"),
              ("K57.92", "Diverticulitis of intestine, part unspecified"),
              ("E11.9", "Type 2 diabetes mellitus"), ("D64.9", "Anemia, unspecified"),
              ("I10", "Essential hypertension")],
}

MED_POOL = {
    "LEJR":  [("855332", "Warfarin", "5 mg", "PO", "daily"),
              ("1191", "Aspirin", "81 mg", "PO", "daily"),
              ("197696", "Oxycodone-acetaminophen", "5-325 mg", "PO", "q6h PRN")],
    "HIP_FEMUR_FRACTURE": [("11289", "Enoxaparin", "40 mg", "SC", "daily"),
              ("42463", "Calcium-vitamin D", "600 mg", "PO", "BID"),
              ("197696", "Oxycodone-acetaminophen", "5-325 mg", "PO", "q6h PRN")],
    "SPINAL_FUSION": [("197696", "Oxycodone-acetaminophen", "5-325 mg", "PO", "q6h PRN"),
              ("6470", "Gabapentin", "300 mg", "PO", "TID"),
              ("8640", "Cyclobenzaprine", "10 mg", "PO", "TID PRN")],
    "CABG": [("29046", "Metoprolol", "25 mg", "PO", "BID"),
              ("83367", "Atorvastatin", "40 mg", "PO", "daily"),
              ("1191", "Aspirin", "81 mg", "PO", "daily"),
              ("4603", "Furosemide", "20 mg", "PO", "daily")],
    "MAJOR_BOWEL": [("11289", "Enoxaparin", "40 mg", "SC", "daily"),
              ("197696", "Oxycodone-acetaminophen", "5-325 mg", "PO", "q6h PRN"),
              ("8123", "Ondansetron", "4 mg", "PO", "q8h PRN")],
}

LANGS = ["English"] * 7 + ["Spanish"] * 2 + ["Vietnamese", "Mandarin", "Haitian Creole"]
FIRST = ["James","Mary","Robert","Patricia","John","Jennifer","Michael","Linda","David",
         "Maria","Carlos","Elena","Thomas","Dorothy","Frank","Rosa","Henry","Grace","Walter","Ana"]
LAST = ["Smith","Johnson","Williams","Alvarez","Brown","Nguyen","Garcia","Miller","Davis",
        "Martinez","Wilson","Anderson","Thomas","Lee","Patel","Tran","Robinson","Clark","Lewis","Hall"]

HEALTH_SYSTEM = "hs_mercy_general"  # single-tenant demo


def daterange_admit():
    # Discharges spread Apr–May 2026 so windows close inside the demo "today" (2026-06-13).
    start = date(2026, 4, 1)
    return start + timedelta(days=random.randint(0, 55))


def write_csv(name, header, rows):
    path = os.path.join(OUT, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return name, len(rows)


# ── Accumulators ─────────────────────────────────────────────────────────────
patients, episodes, notes, problems, meds = [], [], [], [], []
checkins, signals, costs, tiers, flags = [], [], [], [], []
escalations, interventions, claims, recons = [], [], [], []

pid = 0
eid = 0


def gen_intake_note(family, age, gender, tier, icds):
    sx = {
        "LEJR": "end-stage knee osteoarthritis with refractory pain and functional decline",
        "HIP_FEMUR_FRACTURE": "a mechanical fall with displaced femoral neck fracture",
        "SPINAL_FUSION": "progressive lumbar stenosis with neurogenic claudication",
        "CABG": "multivessel coronary disease with exertional angina",
        "MAJOR_BOWEL": "an obstructing sigmoid lesion requiring resection",
    }[family]
    comorbid = ", ".join(d for _, d in icds[1:3]) or "no significant comorbidities"
    risk_line = {
        "TIER_3": "Patient lives alone with limited caregiver support and an elevated readmission profile.",
        "TIER_2": "Patient has a reliable caregiver but moderate comorbidity burden.",
        "TIER_1": "Patient is independent with strong social support and low comorbidity burden.",
    }[tier]
    return (f"{age}-year-old {gender} admitted for {sx}. "
            f"Active problems include {comorbid}. {risk_line} "
            f"Discharge plan: 30-day post-acute monitoring under the TEAM episode pathway "
            f"with daily check-ins, medication reconciliation, and red-flag symptom education.")


# Decide which episode is the on-camera protagonist (riskiest LEJR, Spanish, saved bundle).
HERO_EID = None

for family, (cpt, target, count, readmit_rate, surg_frac) in FAMILY_META.items():
    for i in range(count):
        pid += 1
        eid += 1
        patient_id = f"PT-{pid:04d}"
        episode_id = f"EP-{eid:04d}"

        age = random.randint(50, 88) if family != "MAJOR_BOWEL" else random.randint(45, 84)
        gender = random.choice(["M", "F"])
        lang = random.choice(LANGS)
        lives_alone = random.random() < 0.32
        caregiver = not lives_alone or random.random() < 0.4

        # Tier skew: higher-cost / fracture families skew sicker.
        base = {"LEJR": 0.18, "HIP_FEMUR_FRACTURE": 0.42, "SPINAL_FUSION": 0.34,
                "CABG": 0.5, "MAJOR_BOWEL": 0.44}[family]
        r = random.random()
        tier = "TIER_3" if r < base * 0.45 else ("TIER_2" if r < base * 1.2 else "TIER_1")
        if lives_alone and tier == "TIER_1" and random.random() < 0.5:
            tier = "TIER_2"

        readmitted = random.random() < readmit_rate
        # Tier_3 patients readmit more often; encode signal->outcome correlation.
        if tier == "TIER_3" and random.random() < 0.25:
            readmitted = True
        if tier == "TIER_1" and readmitted and random.random() < 0.5:
            readmitted = False

        admit = daterange_admit()
        los = random.randint(1, 4) if family in ("LEJR", "SPINAL_FUSION") else random.randint(3, 8)
        discharge = admit + timedelta(days=los)
        window_end = discharge + timedelta(days=30)
        track = random.choices([1, 2, 3], weights=[0.6, 0.25, 0.15])[0]
        status = "CLOSED" if window_end <= date(2026, 6, 13) else "OPEN"

        icds = random.sample(ICD_POOL[family], k=random.randint(2, 4))
        # Ensure anchor problem (index 0 of pool) is present for the family.
        if ICD_POOL[family][0] not in icds:
            icds[0] = ICD_POOL[family][0]

        # ── Hero selection: first lives-alone LEJR after a few rows; force the
        # on-camera profile (Tier_3, Spanish → voice-agent routing, bundle SAVED).
        global_hero = (HERO_EID is None and family == "LEJR" and lives_alone and i >= 4)
        if global_hero:
            HERO_EID = episode_id
            tier = "TIER_3"
            lang = "Spanish"
            caregiver = False
            readmitted = False  # the bundle was SAVED by the intervention
            status = "CLOSED"
            window_end = date(2026, 6, 9)
            discharge = window_end - timedelta(days=30)
            admit = discharge - timedelta(days=2)

        patients.append([patient_id, f"MRN{pid:06d}", random.choice(FIRST), random.choice(LAST),
                         age, gender, lang, str(lang != "English").lower(), HEALTH_SYSTEM,
                         str(lives_alone).lower(), str(caregiver).lower(), f"+1555{pid:07d}"])

        note_id = f"NOTE-{eid:04d}"
        notes.append([note_id, episode_id, gen_intake_note(family, age, gender, tier, icds), "synthetic"])

        # CQS inputs (PRD §8): HWR ratio, PSI-90 ratio, PRO-PM (LEJR only).
        hwr = round(random.uniform(0.82, 1.18), 3)
        psi90 = round(random.uniform(0.80, 1.20), 3)
        propm = round(random.uniform(60, 95), 1) if family == "LEJR" else ""
        # Composite quality score 0..1 (higher better) — scales reconciliation.
        cqs = round(max(0.0, min(1.0, 1.5 - 0.5 * hwr - 0.25 * psi90 + (0.0 if propm == "" else (propm-75)/400))), 3)

        episodes.append([episode_id, patient_id, cpt, family, track,
                         admit.isoformat(), discharge.isoformat(), window_end.isoformat(),
                         target, tier, status, hwr, psi90, propm, cqs,
                         int(readmitted), note_id])

        for code, desc in icds:
            problems.append([episode_id, code, desc, "ACTIVE"])
        for rx, name, dose, route, freq in random.sample(MED_POOL[family], k=random.randint(2, len(MED_POOL[family]))):
            meds.append([episode_id, rx, name, dose, route, freq])

        # ── Initial pre-op TierAssessment (port of assign_initial_tier output)
        tiers.append([f"TA-{eid:04d}-pre", episode_id, tier,
                      "" if tier == "TIER_3" else random.randint(2, 9),
                      json.dumps([{"kind": "BASE", "code": "COMORBIDITY_BURDEN", "label": "Comorbidity load", "weight": 2}]),
                      "preop", "postop-retier@1.1.0", 2, discharge.isoformat()])

        # ── 30-day daily check-in + engagement stream
        # Build a deterioration "event day" for episodes that escalate / readmit.
        escalates = readmitted or tier == "TIER_3" or random.random() < 0.18
        event_day = random.randint(4, 12) if escalates else None
        worst_tier_seen = "GREEN"
        open_escalation = False

        for d in range(0, 31):
            day_date = (discharge + timedelta(days=d)).isoformat()
            if d % 1 == 0 and random.random() < (0.12 if tier != "TIER_3" else 0.05):
                continue  # missed check-in
            near_event = event_day is not None and abs(d - event_day) <= 1
            if near_event:
                pain = random.randint(7, 9)
                traj = "WORSE"
                fever = random.choice(["YES_FELT", "YES_MEASURED"])
                inc_change = "WORSE"
                inc_flags = random.sample(INCISION_FLAGS, k=random.randint(1, 2))
                rf = random.sample(RED_FLAGS, k=1) if (family == "CABG" or random.random() < 0.4) else []
                ctier = "RED"
                free = "Pain getting worse and I see yellow drainage on the bandage." if global_hero and d == event_day else "Symptoms worse today."
            else:
                pain = max(0, random.randint(0, 5) - (1 if d > 14 else 0))
                traj = random.choice(["BETTER", "BETTER", "SAME"])
                fever = "NO"
                inc_change = random.choice(["BETTER", "SAME"])
                inc_flags = []
                rf = []
                ctier = "GREEN" if pain <= 3 else "ORANGE"
            raw = pain + (3 if fever != "NO" else 0) + 2 * len(inc_flags) + 4 * len(rf)
            checkins.append([f"CK-{eid:04d}-{d:02d}", episode_id, d, day_date, pain, traj, fever,
                             inc_change, "|".join(inc_flags), random.choice(["NONE", "MILD"]),
                             random.choice(["YES", "SOME"]),
                             "|".join(rf), random.choice(["YES", "SOME", "NO"]),
                             random.choice(["NOT_AT_ALL", "A_LITTLE", "MODERATELY"]),
                             free if near_event else "", ctier, raw])
            if CHECKIN_TIER.index(ctier) > CHECKIN_TIER.index(worst_tier_seen):
                worst_tier_seen = ctier

            # Engagement signal roughly weekly
            if d % 5 == 0:
                signals.append([episode_id, day_date,
                                round(random.uniform(0.55, 1.0) if tier != "TIER_3" else random.uniform(0.3, 0.85), 2),
                                round(random.uniform(0.4, 1.0), 2),
                                str(tier == "TIER_3" and d > 20 and random.random() < 0.3).lower(),
                                random.randint(0, 3)])

            # First RED day opens an Escalation + a RiskFlag with evidence
            if ctier == "RED" and not open_escalation:
                open_escalation = True
                esc_id = f"ESC-{eid:04d}-{d:02d}"
                trig = "checkin:red_flag" if rf else "checkin:wound_concern"
                escalations.append([esc_id, episode_id, "TIER_3", trig, "daily_checkin",
                                    "false" if (readmitted or global_hero) else "true",
                                    day_date, "RN-ONCALL"])
                fcode = rf[0] if rf else (inc_flags[0] if inc_flags else "INCREASED_PAIN_AT_INCISION")
                flags.append([f"RF-{eid:04d}-{d:02d}", episode_id, fcode,
                              fcode.replace("_", " ").title(),
                              "HARD" if rf else "BASE",
                              "HIGH" if rf else "MODERATE", "new",
                              free if near_event else f"Check-in day {d}: {fcode}",
                              "aip_logic_note_to_flag_run"])

                # ── Hero intervention chain: voice call -> home health (CostEvent + ClaimLine)
                if global_hero:
                    iv1 = f"IV-{eid:04d}-call"
                    interventions.append([iv1, episode_id, "VOICE", "completed",
                                          "Patient confirmed worsening drainage; dispatched home health.",
                                          "Spanish voice agent reached patient; red-flag confirmed.",
                                          "RN-CARMEN", day_date])
                    iv2 = f"IV-{eid:04d}-hh"
                    interventions.append([iv2, episode_id, "HOME_HEALTH", "completed",
                                          "Home-health RN assessed wound, started oral antibiotics, no ED transfer.",
                                          "", "RN-CARMEN", day_date])
                    costs.append([f"CE-{eid:04d}-hh", episode_id, day_date, "HOME_HEALTH",
                                  "Home-health skilled nursing visit", 168.0, "false"])
                    # Telehealth follow-up claim line via G-code ladder (established, 25 min -> G0667)
                    claims.append([f"CL-{eid:04d}-th", episode_id, iv1, "G0667", "10", 25,
                                   "13X", "0780", "true", "ESTABLISHED"])

        # ── Cost-event stream
        surg = round(target * surg_frac * random.uniform(0.95, 1.05))
        costs.append([f"CE-{eid:04d}-surg", episode_id, discharge.isoformat(), "SURGERY",
                      f"{family} anchor procedure {cpt}", surg, "false"])
        # Post-acute setting
        if tier != "TIER_1" and random.random() < 0.6:
            setting = random.choice(["SNF", "HOME_HEALTH"])
            amt = random.randint(3200, 9800) if setting == "SNF" else random.randint(900, 2400)
            costs.append([f"CE-{eid:04d}-pac", episode_id, (discharge + timedelta(days=2)).isoformat(),
                          setting, f"Post-acute {setting}", amt, "false"])
        # Routine outpatient/drug
        costs.append([f"CE-{eid:04d}-op", episode_id, (discharge + timedelta(days=7)).isoformat(),
                      "OUTPATIENT", "Follow-up visit + labs", random.randint(180, 650), "false"])
        # Readmission (labeled subset) — the big bundle-buster
        if readmitted:
            rday = (discharge + timedelta(days=(event_day or 9) + random.randint(1, 4)))
            ramt = random.randint(9200, 18500)
            costs.append([f"CE-{eid:04d}-readmit", episode_id, rday.isoformat(), "READMISSION",
                          "Unplanned 30-day inpatient readmission", ramt, "true"])
            costs.append([f"CE-{eid:04d}-ed", episode_id, rday.isoformat(), "ED_VISIT",
                          "Emergency department evaluation", random.randint(1100, 2600), "true"])

        # ── Post-op re-tier assessment if any deterioration
        if worst_tier_seen != "GREEN" or readmitted:
            tiers.append([f"TA-{eid:04d}-post", episode_id, "TIER_3" if (readmitted or worst_tier_seen == "RED") else "TIER_2",
                          "", json.dumps([{"kind": "HARD", "code": "NEW_RED_FLAG_SYMPTOM",
                                           "label": "New red-flag symptom", "weight": 0}]),
                          "postop", "postop-retier@1.1.0", 2, (discharge + timedelta(days=event_day or 10)).isoformat()])

        # ── Reconciliation report for CLOSED episodes
        if status == "CLOSED":
            actual = sum(c[5] for c in costs if c[1] == episode_id)
            delta = round(target - actual)  # positive = saved
            # CQS scales the reconciliation amount; Track gates downside exposure.
            track_factor = {1: 0.0, 2: 0.5, 3: 1.0}[track]
            if delta >= 0:
                projected = round(delta * cqs)
            else:
                projected = round(delta * cqs * track_factor)  # Track 1 = no PY1 downside
            outcome = "SAVED" if delta >= 0 else "BLOWN"
            routed = "VBC_EXEC" if outcome == "SAVED" else "CFO_SERVICE_LINE"
            narrative = (f"{family} episode {episode_id}: actual ${actual:,} vs target ${target:,} "
                         f"({'under' if delta>=0 else 'over'} by ${abs(delta):,}). "
                         f"CQS {cqs}; Track {track}; projected reconciliation ${projected:,}.")
            recons.append([f"RR-{eid:04d}", episode_id, target, actual, delta, cqs, projected,
                           outcome, narrative, routed, "FINALIZED"])

# ── RiskModelVersion: one PROMOTED baseline + one CANDIDATE (Feature A) ───────
# tuning_version mirrors repo: postop/tuning.py TUNING_VERSION=2 is live (PROMOTED);
# candidate is the director's proposed v3. delta cap (POSTOP_DELTA_CAP) + thresholds carried.
risk_versions = [
    [2, "PROMOTED", json.dumps({"checkin_tier_red": 3, "wound_concern": 2, "pain_worse": 2,
                                "missed_streak": 1, "med_nonadherence_3d": 1,
                                "delta_cap": 12, "upgrade_1_min": 3, "upgrade_2_min": 6}),
     json.dumps(["PATIENT_SELF_FLAG_ACTIVE","NEW_RED_FLAG_SYMPTOM","LOST_CONTACT_TIER3",
                 "LOST_CONTACT_GENERAL","DAY_X_SURVEY_RED_AND_RED_FLAG","MULTIPLE_INCISION_FLAGS",
                 "CARE_COMPANION_RED_FLAG_TIER_3","TEACHBACK_FAILED_RED_FLAG_POSTLOOP"]),
     "system", "2026-01-01"],
    [3, "CANDIDATE", json.dumps({"checkin_tier_red": 3, "wound_concern": 3, "pain_worse": 2,
                                 "missed_streak": 2, "med_nonadherence_3d": 2, "incision_flag_streak": 2,
                                 "delta_cap": 12, "upgrade_1_min": 3, "upgrade_2_min": 6}),
     json.dumps(["PATIENT_SELF_FLAG_ACTIVE","NEW_RED_FLAG_SYMPTOM","LOST_CONTACT_TIER3",
                 "LOST_CONTACT_GENERAL","DAY_X_SURVEY_RED_AND_RED_FLAG","MULTIPLE_INCISION_FLAGS",
                 "CARE_COMPANION_RED_FLAG_TIER_3","TEACHBACK_FAILED_RED_FLAG_POSTLOOP","INCISION_FLAG_STREAK_3D"]),
     "MEDICAL_DIRECTOR", "2026-06-10"],
]

care_team = [
    ["CT-RN-01", "Carmen Ruiz", "RN", HEALTH_SYSTEM],
    ["CT-RN-02", "Dion Park", "RN", HEALTH_SYSTEM],
    ["CT-SUR-01", "Dr. Alan Whitfield", "SURGEON", HEALTH_SYSTEM],
    ["CT-MD-01", "Dr. Priya Nair", "MEDICAL_DIRECTOR", HEALTH_SYSTEM],
    ["CT-VBC-01", "Tej Patel", "VBC_EXEC", HEALTH_SYSTEM],
]

# ── Write all files ──────────────────────────────────────────────────────────
manifest = dict([
    write_csv("patients.csv",
              ["patient_id","mrn","first_name","last_name","age","gender","preferred_language",
               "needs_interpreter","health_system_id","lives_alone","has_reliable_caregiver","phone"], patients),
    write_csv("surgical_episodes.csv",
              ["episode_id","patient_id","anchor_cpt","procedure_family","track","admit_date",
               "discharge_date","window_end","target_price","current_tier","episode_status",
               "cqs_hwr_input","cqs_psi90_input","cqs_propm_input","cqs_score","readmitted_label","intake_note_id"], episodes),
    write_csv("intake_notes.csv", ["note_id","episode_id","note_text","source"], notes),
    write_csv("active_problems.csv", ["episode_id","icd10","description","status"], problems),
    write_csv("medications.csv", ["episode_id","rxnorm_code","name","dose","route","frequency"], meds),
    write_csv("daily_checkins.csv",
              ["checkin_id","episode_id","day_index","date","pain_nrs","pain_trajectory","fever",
               "incision_change","incision_flags","nausea","eating_drinking","red_flag_symptoms",
               "walking","worry_level","free_text","scored_tier","raw_total"], checkins),
    write_csv("engagement_signals.csv",
              ["episode_id","date","med_adherence_score","video_engagement_score","lost_contact_flag","chat_sessions"], signals),
    write_csv("cost_events.csv",
              ["cost_event_id","episode_id","date","category","description","amount","is_readmission"], costs),
    write_csv("tier_assessments.csv",
              ["assessment_id","episode_id","tier","score","reasons","phase","model_version","tuning_version","timestamp"], tiers),
    write_csv("risk_flags.csv",
              ["flag_id","episode_id","code","label","kind","severity","status","evidence","generated_by"], flags),
    write_csv("escalations.csv",
              ["escalation_id","episode_id","tier","trigger_type","origin","resolved","created_at","assigned_to"], escalations),
    write_csv("interventions.csv",
              ["intervention_id","episode_id","channel","status","outcome","transcript_summary","owner","timestamp"], interventions),
    write_csv("claim_lines.csv",
              ["claim_id","episode_id","intervention_id","hcpcs_gcode","pos","duration_min","type_of_bill",
               "revenue_code","ride_alone_ok","patient_type"], claims),
    write_csv("reconciliation_reports.csv",
              ["report_id","episode_id","target_price","actual_spend","delta","cqs_score","projected_payment",
               "outcome","narrative","routed_to","status"], recons),
    write_csv("risk_model_versions.csv",
              ["tuning_version","status","weights_json","hard_thresholds_json","created_by","created_at"], risk_versions),
    write_csv("care_team_members.csv", ["member_id","name","role","health_system_id"], care_team),
])

manifest["_hero_episode_id"] = HERO_EID
with open(os.path.join(OUT, "_manifest.json"), "w") as f:
    json.dump(manifest, f, indent=2)

print("HERO episode:", HERO_EID)
for k, v in manifest.items():
    print(f"  {k}: {v}")
