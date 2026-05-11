# PRD — Initial Pre-Op Triage (Patient Upload → Tier Assignment)

| Field | Value |
|---|---|
| Feature | Initial Pre-Op Triage |
| Document version | 1.0 (final) |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-05-08 |
| Primary user | RN care coordinator (uploads patient, reviews tier) |
| Secondary users | NP / PA, Surgeon (read), Clinical Operations Lead (override audit) |
| Implementation | Next.js 14 App Router + TypeScript + Tailwind + shadcn/ui + Prisma/Postgres |
| Audience | Cursor / engineering implementers |
| Depends on | TEAM eligibility check (assumed correctly executed; only TEAM-eligible episodes proceed to triage) |
| Supersedes | §4 ("Initial tier assignment") of `Triage Tracking PRD v0.1` |

---

## 0. Reading order and conventions

This PRD covers **only** the initial pre-op tier assigned at patient upload. Re-tiering during the pre-op window (intake form, surveys, video engagement, PAM proxy), intra-op reassessment, and post-op signal-driven re-tiering are separate PRDs in this directory and are out of scope here.

**Tier numbering convention (preserved from Triage Tracking PRD v0.1):**

| Tier | Risk profile | Cadence intent |
|---|---|---|
| `TIER_1` | **Low** pre-op risk | Standard touchpoints |
| `TIER_2` | **Moderate** pre-op risk | Enhanced touchpoints |
| `TIER_3` | **High** pre-op risk | High-touch (daily early, then every 2–3 days) |

> **Note on labeling.** The author's review notes inverted the numbering (Tier 1 = high). We deliberately keep `TIER_3 = highest` because every downstream system (priority scoring `+20` for Tier 3, red border styling, intra-op hard-upgrade targets, self-flag auto-upgrade target, queue color coding) is keyed to that convention. Inverting would require a synchronous rewrite of seven other modules; we treat the inversion as a labeling slip and keep the existing convention. This is called out explicitly so the team can revisit if the inversion was intentional.

---

## 1. Scope

**In scope.**

1. The six clinical input categories surfaced when an RN uploads a TEAM-eligible patient: Procedure Type, Active Problems / Medical History, Current Medications, Allergies, Social History, Recent Labs and Studies.
2. The deterministic pre-op risk model that consumes those inputs and emits a `TIER_1 / TIER_2 / TIER_3` assignment with itemized reasons.
3. The patient upload review screen — display of inputs, computed tier, rationale, accept/override controls.
4. Override flow (coordinator chooses a different tier with required reason).
5. Persisted episode state, audit events, and API contracts.
6. Tunable weights/thresholds via versioned `tuning.json`.

**Explicitly out of scope (other PRDs).**

- Patient activation (PAM proxy assessment) — captured in the intake form and used in **pre-op re-tiering**, not initial assignment.
- Intake form completion status, survey scores (T-96 / T-48 / T-24), video engagement, battle-card views — all **re-tiering** inputs.
- Intra-op reassessment, post-op signal-driven re-tiering, alert lifecycle, RN queue, priority scoring.
- TEAM eligibility determination — assumed handled upstream; this feature only runs for episodes whose eligibility verdict is `ELIGIBLE`.
- EHR ingestion plumbing — this PRD specifies the *shape* of the six input categories and consumes them via an `IntakeIngestProvider` interface; the production EHR adapter is its own work.

---

## 2. Why this exists (1 paragraph)

A patient uploaded to the system needs a tier *before* anyone touches them — the tier governs which scheduled touchpoints get auto-created on the calendar, which threshold sensitivities apply if signals fire later, and how the patient is colored in the RN queue. The initial tier is the floor of risk for the episode: post-op signals can move it up, but it sets the baseline. This PRD specifies exactly how that floor is computed from chart data available at the moment of upload — no patient-facing inputs required, no manual scoring sheet, no judgment-only call. The output is itemized so the coordinator can audit the reasoning, override if their clinical judgment differs, and so the team can retune weights as outcomes accumulate.

---

## 3. Clinical foundation

The model is anchored on the **ACS-NSQIP Surgical Risk Calculator** input variables (`https://riskcalculator.facs.org`), the published peer-reviewed model that ingests roughly 20 patient-level factors plus a CPT code and produces calibrated 30-day complication probabilities. We do not call the ACS-NSQIP API or replicate its statistical model — we use its input variable set as the canonical list of pre-op chart-derivable risk factors, then map each variable into our tiering decision in a way that is auditable, fast, and tunable.

The six clinical categories the coordinator sees on upload were chosen because together they enclose the ACS-NSQIP input set without requiring patient interview:

| ACS-NSQIP variable | Sourced from |
|---|---|
| Procedure (CPT) | Procedure Type |
| Functional status | Active Problems / Medical History (e.g., "ambulates with walker", "wheelchair-bound", "ADL dependence") |
| Emergency case | Procedure Type (procedure metadata) |
| ASA-PS proxy | Computed from comorbidity load (Active Problems) |
| Steroid use for chronic condition | Current Medications |
| Ascites within 30 days | Active Problems / Recent Labs and Studies |
| Systemic sepsis within 48h | Active Problems / Recent Labs (WBC, lactate) |
| Ventilator dependent | Active Problems |
| Disseminated cancer | Active Problems |
| Diabetes (none / oral / insulin) | Active Problems + Current Medications |
| Hypertension requiring medication | Active Problems + Current Medications |
| Congestive heart failure within 30 days | Active Problems |
| Dyspnea | Active Problems |
| Current smoker within 1 year | Social History |
| History of severe COPD | Active Problems |
| Dialysis | Active Problems + Current Medications |
| Acute renal failure | Active Problems + Recent Labs (creatinine/eGFR trend) |
| BMI | Active Problems / vitals (if present in record) |

Two additions beyond ACS-NSQIP, both grounded in surgical-readmission literature and in the existing Triage Tracking PRD's tier definitions:

1. **Social determinants** — lives alone without caregiver, housing instability, food insecurity, transportation barrier. Sourced from Social History. Strong predictors of 30-day readmission and adherence regardless of clinical risk.
2. **Pharmacological complexity** — anticoagulant/antiplatelet therapy, immunosuppressants, polypharmacy ≥10, chronic opioid use. Sourced from Current Medications. Drives perioperative management complexity.

The strict six categories you listed are the **only** clinical inputs. Patient demographics (age, sex) are non-clinical record fields present at upload; we use age as an ACS-NSQIP-aligned soft factor (≥75) and ignore sex for tiering purposes. **PAM activation, intake-form responses, surveys, and engagement metrics are deliberately excluded** — they belong to re-tiering.

---

## 4. The six input categories (canonical schemas)

Each category arrives as a structured object on patient upload. The shapes below are what the algorithm consumes; the upstream `IntakeIngestProvider` is responsible for normalizing whatever the EHR returns into these shapes.

### 4.1 Procedure Type

```ts
interface ProcedureInput {
  cptCode: string;                            // e.g., "27447" (TKA)
  anchorProcedureFamily: ProcedureFamily;     // CMS TEAM family
  scheduledDate: string;                      // ISO date
  isEmergency: boolean;                       // ACS-NSQIP "Emergency case"
  bilateral?: boolean;
  laterality?: 'LEFT' | 'RIGHT' | 'BILATERAL' | 'N_A';
  approach?: 'OPEN' | 'MIS' | 'ROBOTIC' | 'UNKNOWN';
  notes?: string;
}

type ProcedureFamily =
  | 'LEJR'                  // Lower Extremity Joint Replacement (hip/knee)
  | 'CABG'                  // Coronary Artery Bypass Graft
  | 'SPINAL_FUSION'
  | 'HIP_FEMUR_FRACTURE'
  | 'MAJOR_BOWEL';
```

### 4.2 Active Problems / Medical History

```ts
interface ActiveProblemsInput {
  problems: ActiveProblem[];
  functionalStatus: 'INDEPENDENT' | 'PARTIALLY_DEPENDENT' | 'TOTALLY_DEPENDENT' | 'UNKNOWN';
  bmi?: number;
  asaClassIfDocumented?: 1 | 2 | 3 | 4 | 5;   // Optional pre-op pre-anesthesia visit
}

interface ActiveProblem {
  icd10: string;
  description: string;
  status: 'ACTIVE' | 'RESOLVED' | 'CHRONIC';
  onsetDate?: string;
  severityNote?: string;
}
```

The algorithm maps ICD-10 codes to clinical flags via a versioned mapping table (`/lib/triage/icd10-flags.ts`). Flags emitted include: `CHF_RECENT`, `CAD`, `DYSPNEA_AT_REST_OR_MIN_EXERTION`, `SEVERE_COPD`, `CKD_STAGE`, `DIALYSIS_DEPENDENT`, `DIABETES_TYPE` (none / oral / insulin), `HTN_REQUIRING_MEDS`, `DISSEMINATED_CANCER`, `ASCITES_30D`, `SEPSIS_48H`, `VENTILATOR_DEPENDENT`, `PRIOR_30D_READMISSION`, `OBSTRUCTIVE_SLEEP_APNEA`, `STROKE_HISTORY`, `BLEEDING_DIATHESIS`.

### 4.3 Current Medications

```ts
interface MedicationsInput {
  medications: Medication[];
}

interface Medication {
  rxnormCode?: string;
  name: string;
  dose?: string;
  route?: string;
  frequency?: string;
  startDate?: string;
  indication?: string;
}
```

The algorithm derives flags via `/lib/triage/med-flags.ts`: `ANTICOAGULANT_THERAPEUTIC` (warfarin, DOACs, therapeutic LMWH), `DUAL_ANTIPLATELET`, `INSULIN_DEPENDENT_DM`, `CHRONIC_STEROIDS` (>20 mg prednisone equiv. >30 d, or any dose >90 d), `IMMUNOSUPPRESSANTS` (e.g., tacrolimus, MMF, biologics), `CHRONIC_OPIOIDS` (>90 days continuous), `POLYPHARMACY_HIGH` (≥10 distinct active meds), `BETA_BLOCKER_ON_BOARD`, `STATIN_ON_BOARD`, `DIURETIC_LOOP`, `INHALED_BRONCHODILATOR_DAILY`.

### 4.4 Allergies

```ts
interface AllergiesInput {
  allergies: Allergy[];
}

interface Allergy {
  substance: string;
  reactionType: 'ANAPHYLAXIS' | 'RASH' | 'GI' | 'ANGIOEDEMA' | 'OTHER' | 'UNKNOWN';
  severity?: 'MILD' | 'MODERATE' | 'SEVERE';
  notes?: string;
}
```

Allergies do not directly drive tier in most cases but contribute via two narrow rules:
- Any **anaphylaxis** to anesthetic agents, contrast, latex, or perioperative-relevant antibiotics → +1 soft point ("Severe allergy with perioperative implications").
- Any allergy/intolerance to standard surgical antibiotic prophylaxis (e.g., cephalosporin allergy with documented anaphylaxis) → flagged in UI for surgeon review but does not by itself change tier (well-managed by anesthesia/surgery teams).

### 4.5 Social History

```ts
interface SocialHistoryInput {
  smokingStatus: 'NEVER' | 'FORMER' | 'CURRENT' | 'UNKNOWN';
  packYears?: number;
  alcoholUse: 'NONE' | 'OCCASIONAL' | 'MODERATE' | 'HEAVY' | 'AT_RISK_OR_AUDIT_POSITIVE' | 'UNKNOWN';
  substanceUse: SubstanceUse[];
  livesAlone: boolean | null;
  hasReliableCaregiver: boolean | null;
  housingStatus: 'STABLE' | 'UNSTABLE' | 'HOMELESS' | 'UNKNOWN';
  foodSecurity: 'SECURE' | 'INSECURE' | 'UNKNOWN';
  transportationBarrier: boolean | null;
  employmentStatus?: 'EMPLOYED' | 'UNEMPLOYED' | 'RETIRED' | 'DISABLED' | 'UNKNOWN';
  primaryLanguage?: string;
  needsInterpreter?: boolean;
  age: number;                                // demographic, included as ACS-NSQIP soft factor
}

interface SubstanceUse {
  substance: 'OPIOIDS' | 'STIMULANTS' | 'CANNABIS' | 'OTHER';
  status: 'ACTIVE' | 'IN_RECOVERY' | 'PRIOR' | 'UNKNOWN';
}
```

### 4.6 Recent Labs and Studies

```ts
interface RecentLabsInput {
  labs: LabResult[];
  studies: StudyResult[];
}

interface LabResult {
  loinc?: string;
  name: string;                               // 'Hemoglobin', 'Albumin', 'Creatinine', 'eGFR', 'HbA1c', 'INR', 'Platelets', 'BNP', 'Lactate', 'WBC'
  value: number;
  unit: string;
  drawnAt: string;
  referenceRange?: string;
  isAbnormal?: boolean;
}

interface StudyResult {
  type: 'ECHO' | 'ECG' | 'PFT' | 'CXR' | 'STRESS_TEST' | 'CARDIAC_CATH' | 'OTHER';
  performedAt: string;
  summary: string;
  ejectionFraction?: number;                  // % if ECHO
  significantFindings?: string[];
}
```

Lab flags emitted by `/lib/triage/lab-flags.ts`:

| Flag | Default trigger |
|---|---|
| `ANEMIA_PREOP` | Hb <12 g/dL (women) or <13 g/dL (men) — NSQIP threshold |
| `ANEMIA_SEVERE` | Hb <10 g/dL |
| `HYPOALBUMINEMIA` | Albumin <3.5 g/dL |
| `MALNUTRITION_SEVERE` | Albumin <3.0 g/dL |
| `RENAL_IMPAIRMENT` | eGFR <60 |
| `RENAL_IMPAIRMENT_SEVERE` | eGFR <30 OR creatinine ≥2.0 |
| `GLYCEMIC_DYSCONTROL` | HbA1c >8.0% |
| `GLYCEMIC_DYSCONTROL_SEVERE` | HbA1c >9.5% |
| `COAGULOPATHY` | INR >1.5 (off therapeutic anticoagulation) |
| `THROMBOCYTOPENIA` | Platelets <100k |
| `THROMBOCYTOPENIA_SEVERE` | Platelets <50k |
| `BNP_ELEVATED` | BNP >400 pg/mL or NT-proBNP >1800 pg/mL |
| `LACTATE_ELEVATED` | Lactate >2 mmol/L |
| `LOW_EJECTION_FRACTION` | EF <40% on echo |
| `LOW_EJECTION_FRACTION_SEVERE` | EF <30% |

All thresholds live in `tuning.json` and are versioned (see §10).

---

## 5. The risk model

The model is a **two-stage decision**: hard escalators (any one → Tier 3, short-circuit) followed by a weighted soft-factor score (mapped to tier).

### 5.1 Hard escalators (any one ⇒ Tier 3)

If any condition below is true, the patient is Tier 3 and no further scoring is needed. The reason is recorded.

| Hard escalator | Source | Rationale |
|---|---|---|
| `EMERGENCY_CASE = true` | Procedure | Emergent surgery has materially higher 30-day complication rates across all NSQIP-tracked outcomes. |
| `SEPSIS_48H` | Problems / Labs | Active perioperative sepsis is a top-tier mortality driver. |
| `VENTILATOR_DEPENDENT` | Problems | Pre-op ventilator dependence indicates respiratory compromise. |
| `DISSEMINATED_CANCER` | Problems | NSQIP-validated independent risk factor. |
| `DIALYSIS_DEPENDENT` | Problems / Meds | ESRD on dialysis has 2–3× complication odds. |
| `CHF_RECENT` (within 30 d) | Problems | Recent decompensation predicts perioperative cardiac events. |
| `LOW_EJECTION_FRACTION_SEVERE` (EF <30%) | Studies | Severe systolic dysfunction. |
| `ASCITES_30D` | Problems / Labs | Hepatic decompensation marker. |
| `FUNCTIONAL_STATUS = TOTALLY_DEPENDENT` | Problems | NSQIP-validated mortality driver; also signals discharge-disposition complexity. |
| `PRIOR_30D_READMISSION` | Problems | Strong recurrence predictor. |
| `HOUSING_INSTABILITY` (`HOMELESS` or `UNSTABLE`) | Social | Adherence and follow-up infeasible without stable housing. |
| `FOOD_INSECURITY` | Social | Linked to readmission and post-op infection. |
| `LIVES_ALONE_NO_CAREGIVER` (`livesAlone=true` AND `hasReliableCaregiver=false`) | Social | Discharge safety and adherence failure mode. |
| `EF_OR_NSQIP_PROCEDURE_HARD` | Procedure × Studies | Procedure-family-specific gating (see §5.4). |

### 5.2 Soft factors (weighted)

If no hard escalator triggers, sum weighted soft factors. Weights live in `tuning.json` (§10) and are exposed below at default values.

```ts
// /lib/triage/initial-tier.weights.ts
export const SOFT_WEIGHTS = {
  // Functional & demographic
  FUNCTIONAL_PARTIALLY_DEPENDENT:      3,
  AGE_75_PLUS:                         1,
  BMI_OVER_40:                         1,
  BMI_UNDER_18_5:                      2,

  // Cardiac
  CAD:                                 2,
  CHF_HISTORY_NOT_RECENT:              2,
  LOW_EJECTION_FRACTION:               2,    // 30–40%
  HTN_REQUIRING_MEDS:                  1,

  // Pulmonary
  SEVERE_COPD:                         3,
  DYSPNEA_AT_REST_OR_MIN_EXERTION:     2,
  OBSTRUCTIVE_SLEEP_APNEA:             1,
  CURRENT_SMOKER:                      2,    // within 1 year
  CURRENT_SMOKER_HEAVY:                1,    // additional, if pack-years >20

  // Renal
  RENAL_IMPAIRMENT:                    2,    // eGFR <60
  RENAL_IMPAIRMENT_SEVERE:             3,    // eGFR <30 (and not on dialysis — dialysis is hard)

  // Endocrine
  DIABETES_INSULIN_DEPENDENT:          2,
  DIABETES_ORAL:                       1,
  GLYCEMIC_DYSCONTROL:                 1,    // HbA1c >8
  GLYCEMIC_DYSCONTROL_SEVERE:          2,    // additional, HbA1c >9.5

  // Hematologic / nutrition
  ANEMIA_PREOP:                        1,
  ANEMIA_SEVERE:                       2,
  HYPOALBUMINEMIA:                     2,    // <3.5
  MALNUTRITION_SEVERE:                 3,    // additional, <3.0
  COAGULOPATHY:                        2,
  THROMBOCYTOPENIA:                    1,
  THROMBOCYTOPENIA_SEVERE:             3,    // additional, <50k

  // Neuro
  STROKE_HISTORY:                      1,
  COGNITIVE_IMPAIRMENT:                2,

  // Pharmacological complexity
  ANTICOAGULANT_THERAPEUTIC:           1,
  DUAL_ANTIPLATELET:                   1,
  CHRONIC_STEROIDS:                    2,
  IMMUNOSUPPRESSANTS:                  2,
  CHRONIC_OPIOIDS:                     1,
  POLYPHARMACY_HIGH:                   1,    // ≥10 active

  // Social
  TRANSPORTATION_BARRIER:              1,
  AT_RISK_ALCOHOL_OR_AUDIT_POS:        2,
  ACTIVE_SUBSTANCE_USE:                2,
  NEEDS_INTERPRETER:                   1,

  // Allergy
  PERIOP_ANAPHYLAXIS_HISTORY:          1,

  // Procedure-family base risk (always applied — see §5.4)
  PROCEDURE_BASE:                      0,    // resolved per family
} as const;
```

### 5.3 Score → tier mapping

```ts
// /lib/triage/initial-tier.ts
export function scoreToTier(score: number): Tier {
  if (score >= 8) return 'TIER_3';
  if (score >= 4) return 'TIER_2';
  return 'TIER_1';
}
```

Defaults are deliberately conservative — TIER_2 starts at score 4 (e.g., a single insulin-dependent diabetic with eGFR<60 and current smoker would land Tier 2). Thresholds are tunable.

### 5.4 Procedure-family base risk

Every procedure carries a base score added before the soft factors are summed. This anchors the tier in the inherent risk of the operation itself — a perfectly healthy patient undergoing CABG is not the same as a perfectly healthy patient undergoing TKA.

```ts
// /lib/triage/procedure-base.ts
export const PROCEDURE_BASE: Record<ProcedureFamily, number> = {
  LEJR:                 0,    // elective hip/knee — relatively low procedural risk in healthy patients
  SPINAL_FUSION:        2,    // variable; assume moderate base
  MAJOR_BOWEL:          3,    // higher inherent SSI/anastomotic leak risk
  CABG:                 4,    // high base; almost always Tier 2 minimum
  HIP_FEMUR_FRACTURE:   3,    // typically emergent in elderly
};

// Procedure-family hard escalators (see §5.1)
export const PROCEDURE_HARD_RULES: Array<(p: ProcedureInput, s: StudiesSummary) => string | null> = [
  (p, _s) => p.isEmergency ? 'EMERGENCY_CASE' : null,
  (p, s) => (p.anchorProcedureFamily === 'CABG' && s.lowEf30) ? 'CABG_WITH_EF_UNDER_30' : null,
];
```

### 5.5 The full algorithm (pseudocode)

```ts
// /lib/triage/initial-tier.ts
export function assignInitialTier(input: InitialTierInput): TierAssignment {
  const flags = deriveFlags(input);                        // {hard: string[], soft: Array<{flag, weight}>}
  const reasons: TierReason[] = [];

  // Stage 1 — hard escalators
  for (const hard of flags.hard) {
    return {
      tier: 'TIER_3',
      score: null,
      reasons: [{ kind: 'HARD', code: hard, label: HARD_LABELS[hard] }],
      modelVersion: MODEL_VERSION,
      tuningVersion: input.tuningVersion,
    };
  }

  // Stage 2 — weighted soft score
  let score = PROCEDURE_BASE[input.procedure.anchorProcedureFamily];
  reasons.push({
    kind: 'BASE',
    code: 'PROCEDURE_BASE',
    label: `${input.procedure.anchorProcedureFamily} base risk`,
    weight: score,
  });

  for (const { flag, weight } of flags.soft) {
    score += weight;
    reasons.push({ kind: 'SOFT', code: flag, label: SOFT_LABELS[flag], weight });
  }

  const tier = scoreToTier(score);
  return { tier, score, reasons, modelVersion: MODEL_VERSION, tuningVersion: input.tuningVersion };
}
```

Where `deriveFlags(input)` orchestrates the per-category flag derivers (`procedure-flags.ts`, `icd10-flags.ts`, `med-flags.ts`, `allergy-flags.ts`, `social-flags.ts`, `lab-flags.ts`), each pure and unit-tested in isolation.

### 5.6 Worked examples

**Example A — Tier 1.** 62-year-old man, elective TKA (LEJR, base 0). Active problems: HTN on lisinopril (HTN_REQUIRING_MEDS, +1). Meds: lisinopril, atorvastatin (no flags). Allergies: NKDA. Social: never smoker, lives with spouse, stable housing. Labs: Hb 14.2, eGFR 78, HbA1c 5.6, albumin 4.1. **Score = 0 + 1 = 1 → TIER_1.** Reason: "HTN requiring medication."

**Example B — Tier 2.** 71-year-old woman, elective THA (LEJR, base 0). Active problems: T2DM on insulin (insulin DM +2), CAD s/p PCI 2 years ago (+2), OSA on CPAP (+1). Meds: insulin, metformin, aspirin, atorvastatin, metoprolol, lisinopril, sertraline, gabapentin, vitamin D, omeprazole (10 active → polypharmacy +1). Social: former smoker, lives with spouse. Labs: Hb 11.8 (anemia +1), eGFR 56 (renal impairment +2), HbA1c 7.4, albumin 3.8. Demographic: age 71. **Score = 0 + 2 + 2 + 1 + 1 + 1 + 2 = 9 → TIER_3.** (Note: this lands Tier 3, not Tier 2 — that is the model working correctly. NSQIP-style, this patient *is* high risk.)

**Example C — Tier 2 cleanly.** 58-year-old man, elective laparoscopic sigmoid colectomy (MAJOR_BOWEL, base 3). Active problems: HTN (+1), former smoker (no flag — former, not current). Meds: lisinopril, ASA 81 (no DAPT). Social: lives alone but has reliable caregiver, stable housing. Labs: all normal. Age 58. **Score = 3 + 1 = 4 → TIER_2.** Reason: procedure-base + HTN.

**Example D — Tier 3 hard.** 68-year-old man, **emergent** open repair of perforated diverticulitis. **Hard escalator: EMERGENCY_CASE → TIER_3.** Score not computed; reason logged.

**Example E — Tier 3 hard from social.** 64-year-old woman, elective TKA. No comorbidities. Social: lives alone, no reliable caregiver. **Hard escalator: LIVES_ALONE_NO_CAREGIVER → TIER_3.**

These examples are encoded as fixtures in `/lib/triage/__tests__/initial-tier.fixtures.ts` and asserted by unit tests.

---

## 6. The patient upload review screen

The screen the RN sees the moment a patient is uploaded and TEAM-eligibility passes. It is the only UI surface this PRD owns.

### 6.1 Layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Patient: Maria Cruz, F 71      MBI: 1AB-CD2-EF34       Surgeon: Dr. Patel   │
│ Procedure: TKA (right) — scheduled 2026-05-22 (T-14)                        │
│                                                                              │
│ ┌──── COMPUTED PRE-OP TIER ────────────────────────────────────────────┐    │
│ │  ▮ TIER 2 — Moderate pre-op risk           Score: 5 / model v1.0     │    │
│ │                                                                       │    │
│ │  Why this tier:                                                       │    │
│ │   • Procedure base (LEJR): +0                                         │    │
│ │   • Insulin-dependent diabetes: +2                                    │    │
│ │   • Renal impairment (eGFR 56): +2                                    │    │
│ │   • Polypharmacy (10 active meds): +1                                 │    │
│ │                                                                       │    │
│ │  [ ✓ Accept tier ]  [ ▾ Override ]                                    │    │
│ └──────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│ ┌──── INPUTS USED ──────────────────────────────────────────────────────┐   │
│ │ ┌────────────┐ ┌────────────┐ ┌────────────┐                          │   │
│ │ │ Procedure  │ │ Active     │ │ Current    │                          │   │
│ │ │  Type      │ │ Problems   │ │ Medications│                          │   │
│ │ └────────────┘ └────────────┘ └────────────┘                          │   │
│ │ ┌────────────┐ ┌────────────┐ ┌────────────┐                          │   │
│ │ │ Allergies  │ │ Social     │ │ Recent Labs│                          │   │
│ │ │            │ │ History    │ │ & Studies  │                          │   │
│ │ └────────────┘ └────────────┘ └────────────┘                          │   │
│ │  (each card expandable, shows raw fields the algorithm consumed)      │   │
│ └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│ ┌──── MISSING DATA WARNINGS ────────────────────────────────────────────┐   │
│ │  ⚠ Albumin not in chart within last 90 days. Tier may understate     │   │
│ │    nutritional risk. [Order labs] [Acknowledge]                       │   │
│ └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Behavior

- **Tier card** uses the same color convention as the queue: red border for Tier 3, amber for Tier 2, neutral for Tier 1.
- **Reasons list** itemizes every contributing flag with its weight. Hard escalators are shown with a 🛑 icon; soft factors with a +N pill.
- **Inputs cards** are read-only summaries of the six categories; clicking expands to show the raw structured fields the algorithm consumed (drug names, ICD-10 codes, lab values with reference ranges).
- **Missing data warnings** surface when key inputs are absent or stale (e.g., labs older than 90 days, no albumin, no eGFR, no recent ECHO for cardiac procedures). Each warning has a contextual action.
- **Accept** persists the computed tier as the episode's tier and writes an `INITIAL_TIER_ASSIGNED` event.
- **Override** opens a modal with: target tier dropdown (TIER_1 / TIER_2 / TIER_3), required reason text (≥30 chars), and an "Acknowledge that override deviates from algorithm" checkbox. Both the auto-assigned tier and the override are persisted; tier becomes the override; an `INITIAL_TIER_OVERRIDDEN` event captures actor, timestamp, before, after, reason.

### 6.3 Acceptance criteria

- **AC-6.1** GIVEN a TEAM-eligible patient is uploaded WHEN inputs include all six categories THEN the review screen renders the computed tier and itemized reasons within 2 seconds.
- **AC-6.2** GIVEN any hard escalator condition THEN the tier card shows TIER_3 with a single 🛑 reason and the score line reads "n/a — hard escalator."
- **AC-6.3** GIVEN no hard escalators and a computed soft score THEN every contributing flag (including procedure base) is shown with its individual weight, and the displayed total equals the sum.
- **AC-6.4** GIVEN missing albumin THEN a "labs incomplete" warning is shown without blocking the accept action.
- **AC-6.5** Override requires a tier selection AND ≥30-char reason; submit is disabled until both present.
- **AC-6.6** Both auto-assigned tier and final tier are visible in the audit trail after override.
- **AC-6.7** All UI states meet WCAG 2.1 AA (contrast, keyboard navigation, screen-reader labels for the tier card and the reasons list).

---

## 7. Integration with downstream systems

This feature **produces** a single thing: an `Episode` row with `tier`, `tierAssignedBy`, `initialTierReasons`, `initialTierScore`, `initialTierModelVersion`, and `initialTierTuningVersion` populated, plus an `INITIAL_TIER_ASSIGNED` audit event.

It **does not** touch:
- The Triage Queue (the queue reads `Episode.tier`; no special-case for newly-assigned episodes)
- Priority scoring (no alerts exist yet at the moment of initial assignment)
- The intra-op or post-op re-tiering pipelines (they update the same `Episode.tier` field downstream)

Downstream re-tiering modules are expected to call the same `Episode.updateTier(episodeId, newTier, reason, actor)` routine that this feature uses — the field is owned by the episode, not by the initial-tiering module. This keeps the contract clean.

---

## 8. Data model (Prisma)

This PRD adds the following fields to the existing `Episode` model and reuses the existing `TriageEvent` table.

```prisma
model Episode {
  // ... existing fields from Triage Tracking PRD

  // Initial-tiering snapshot
  initialTier                Tier
  initialTierAssignedAt      DateTime
  initialTierAssignedBy      String        // 'SYSTEM' or userId
  initialTierScore           Int?          // null when hard escalator triggered
  initialTierReasons         Json          // TierReason[] (kind, code, label, weight?)
  initialTierModelVersion    String        // e.g., 'initial-tier@1.0.0'
  initialTierTuningVersion   Int           // FK-style ref to TuningConfig.version

  // Inputs snapshot — frozen at upload time so the model is reproducible
  initialTierInputsSnapshot  Json          // InitialTierInput in full

  // Tier currently in effect (may differ from initialTier after re-tiering)
  tier                       Tier
  tierLastChanged            DateTime
  tierLastChangedBy          String

  // Override (if coordinator overrode the computed initial tier)
  initialTierWasOverridden   Boolean       @default(false)
  initialTierOverrideReason  String?
  initialTierOverrideBy      String?
  initialTierOverrideAt      DateTime?
}

// Reused from existing PRD; only relevant new event types listed
enum TriageEventType {
  // ... existing
  INITIAL_TIER_ASSIGNED
  INITIAL_TIER_OVERRIDDEN
  INITIAL_TIER_INPUTS_INCOMPLETE
}
```

The full `InitialTierInput` JSON is intentionally snapshotted on the episode (not just referenced by foreign keys to source records) so the tiering decision is reproducible even if upstream chart data changes later.

---

## 9. API contracts

### 9.1 Compute (preview, no persistence)

**`POST /api/triage/initial-tier/compute`** — pure compute; used by the upload flow to render the review screen.

```ts
// Request
{
  inputs: InitialTierInput
}
// Response 200
{
  tier: 'TIER_1' | 'TIER_2' | 'TIER_3',
  score: number | null,        // null if hard escalator triggered
  reasons: TierReason[],
  missingDataWarnings: MissingDataWarning[],
  modelVersion: string,
  tuningVersion: number
}
```

### 9.2 Assign (persist)

**`POST /api/episodes/:episodeId/initial-tier`** — persist computed tier on the episode.

```ts
// Request
{
  inputs: InitialTierInput,
  acceptedComputedTier: true
}
// Response 201
{
  episode: Episode  // with initialTier* fields populated
}
// Errors
//   409 - episode already has initialTier assigned (idempotent on identical inputs returns 200)
//   422 - inputs failed schema validation
//   428 - precondition failed: episode not TEAM-eligible
```

### 9.3 Override

**`POST /api/episodes/:episodeId/initial-tier/override`** — coordinator override.

```ts
// Request
{
  targetTier: 'TIER_1' | 'TIER_2' | 'TIER_3',
  reason: string  // min 30 chars
}
// Response 200
{
  episode: Episode  // tier replaced; initialTierWasOverridden=true
}
```

### 9.4 Tuning

**`GET /api/triage/tuning/initial-tier/current`** — current weights, thresholds, and model version. Read by the compute endpoint and surfaced in audit metadata.

**`POST /api/triage/tuning/initial-tier`** — admin-only, deploy new tuning config version (creates a `TuningConfig` row, bumps `version`).

---

## 10. Tuning config

`tuning.json` (loaded by `/lib/triage/tuning.ts`, reloaded on file change):

```json
{
  "version": 1,
  "modelVersion": "initial-tier@1.0.0",
  "softWeights": { "...": "see §5.2" },
  "procedureBase": { "LEJR": 0, "SPINAL_FUSION": 2, "MAJOR_BOWEL": 3, "CABG": 4, "HIP_FEMUR_FRACTURE": 3 },
  "scoreToTier": { "tier3Min": 8, "tier2Min": 4 },
  "labThresholds": {
    "anemiaPreopHbWomen": 12,
    "anemiaPreopHbMen": 13,
    "anemiaSevereHb": 10,
    "albuminLow": 3.5,
    "albuminMalnutrition": 3.0,
    "egfrLow": 60,
    "egfrSevere": 30,
    "creatinineSevere": 2.0,
    "hba1cElevated": 8.0,
    "hba1cSevere": 9.5,
    "inrCoagulopathy": 1.5,
    "plateletsLow": 100000,
    "plateletsSevere": 50000,
    "bnp": 400,
    "ntProBnp": 1800,
    "lactate": 2.0,
    "efLow": 40,
    "efSevere": 30
  },
  "missingDataPolicy": {
    "albuminMaxAgeDays": 90,
    "hbMaxAgeDays": 90,
    "creatinineMaxAgeDays": 60,
    "hba1cMaxAgeDays": 180,
    "echoMaxAgeDaysCardiac": 365
  }
}
```

Every change to `tuning.json` mints a new `TuningConfig` row with version+1, effective-from timestamp, and createdBy. Computed tiers are stamped with the tuning version so historical decisions remain reproducible.

---

## 11. Component / file structure

```
/app
  /upload
    page.tsx                              # Patient upload entry point
    /[episodeId]
      review/page.tsx                     # The review screen (§6)

  /api
    /triage
      /initial-tier
        /compute/route.ts                 # POST §9.1
      /tuning
        /initial-tier
          /current/route.ts               # GET §9.4
          /route.ts                       # POST §9.4 (admin)
    /episodes
      /[episodeId]
        /initial-tier
          /route.ts                       # POST §9.2
          /override/route.ts              # POST §9.3

/components
  /initial-tier
    TierCard.tsx                          # the prominent tier display
    ReasonList.tsx                        # itemized reasons with weight pills
    InputsSummary.tsx                     # six expandable category cards
    InputCategoryCard.tsx
    MissingDataWarnings.tsx
    OverrideModal.tsx
    AcceptControls.tsx

/lib
  /triage
    initial-tier.ts                       # main entry — assignInitialTier()
    initial-tier.weights.ts               # SOFT_WEIGHTS, label maps
    procedure-base.ts                     # PROCEDURE_BASE + procedure hard rules
    icd10-flags.ts                        # ICD-10 → flag mapping
    med-flags.ts                          # medication → flag mapping
    allergy-flags.ts
    social-flags.ts
    lab-flags.ts                          # lab thresholds → flags
    deriveFlags.ts                        # orchestrator over the per-category derivers
    tuning.ts                             # load + watch tuning.json
    missing-data.ts                       # detect stale/absent inputs

/types
  initial-tier.ts                         # InitialTierInput, TierAssignment, TierReason, etc.

/jobs
  (none for initial tier — purely synchronous on upload)

/__tests__
  initial-tier.spec.ts                    # unit tests against fixture worked examples
  initial-tier.fixtures.ts                # Examples A–E plus edge cases
  derive-flags.spec.ts
  lab-flags.spec.ts
  med-flags.spec.ts
  icd10-flags.spec.ts
  social-flags.spec.ts
  procedure-base.spec.ts
  override.spec.ts
```

---

## 12. Edge cases (enumerated)

1. **Patient uploaded before TEAM eligibility resolves.** Block compute call with 428; UI shows "Eligibility check in progress" and polls.
2. **Eligibility check returns INELIGIBLE.** Initial tier is not computed at all; episode is marked ineligible and exits the triage flow.
3. **No labs in chart.** Algorithm runs with no lab flags; missing-data warnings list every absent lab; tier may understate risk and the warning explicitly states this. RN can override or order labs.
4. **Labs older than threshold (e.g., albumin from 6 months ago).** Treat as missing for flag purposes; surface as missing-data warning.
5. **Conflicting data (problem list says no DM but HbA1c is 9.5).** Algorithm is problem-list-authoritative for flags but the lab still triggers `GLYCEMIC_DYSCONTROL_SEVERE` independently. Both reasons appear; RN sees the conflict in the inputs cards.
6. **Patient is on therapeutic anticoagulation AND has INR 2.5.** `COAGULOPATHY` flag is suppressed because INR elevation is expected; med flag `ANTICOAGULANT_THERAPEUTIC` fires instead.
7. **Procedure family not in the CMS TEAM five.** Reject with 422 — out of scope for triage.
8. **Bilateral procedure (e.g., bilateral TKA).** Adds a fixed +1 soft factor (defined in tuning, default 1) to acknowledge the higher logistical and physiological burden.
9. **Re-upload of an existing patient (re-eligibility, re-scheduling).** New `Episode` row created; old episode archived. Initial tiering runs fresh on the new episode.
10. **Coordinator overrides Tier 3 (hard) → Tier 1.** Allowed but flagged: override modal requires acknowledging that downgrading from a hard-escalated Tier 3 deviates significantly from the model. Both are persisted; an `INITIAL_TIER_OVERRIDDEN` event captures the deviation magnitude.
11. **Tuning config changes between compute and persist.** The persisted tier carries the tuning version that produced it. If compute used v3 and persist arrived after v4 deployed, persist re-runs compute under v4 and surfaces any tier change in the response. UI prompts RN to re-review.
12. **Inputs schema validation fails (malformed lab unit, unknown ICD-10).** Compute returns 422 with field-level errors; UI surfaces inline.
13. **Episode aborted before persistence (RN closes the tab on review screen).** Compute results are not persisted; on next visit, compute re-runs (idempotent for identical inputs).
14. **Patient identifies as a special population not represented in the model (pediatric, peripartum).** Out of scope for v1; UI rejects upload with a "not eligible for TEAM triage v1" banner.
15. **Algorithm produces TIER_1 but user has clinical concern.** Override flow handles this; the override reason is required and is a primary input to threshold tuning review.

---

## 13. Build order

Each step independently testable; tests written alongside each unit.

1. **Types and schemas** — `/types/initial-tier.ts`, Zod validators for `InitialTierInput`.
2. **Tuning loader** — `/lib/triage/tuning.ts` with `tuning.json`, file watcher, version tracking, schema validation.
3. **Per-category flag derivers** — `icd10-flags.ts`, `med-flags.ts`, `allergy-flags.ts`, `social-flags.ts`, `lab-flags.ts`, `procedure-flags.ts`. Each pure, each unit-tested.
4. **Orchestrator** — `deriveFlags.ts` with hard/soft separation.
5. **Procedure base** — `procedure-base.ts` with PROCEDURE_BASE map and procedure hard rules.
6. **Main algorithm** — `initial-tier.ts` (`assignInitialTier`, `scoreToTier`).
7. **Worked-example fixtures and tests** — Examples A–E plus the §12 edge cases.
8. **Missing-data detector** — `missing-data.ts`.
9. **Prisma migration** — add `initialTier*` fields to `Episode`; add new `TriageEventType` values.
10. **Compute API** — `POST /api/triage/initial-tier/compute`.
11. **Persist + override APIs** — `POST /api/episodes/:id/initial-tier`, `POST /api/episodes/:id/initial-tier/override`.
12. **Tuning APIs** — `GET/POST /api/triage/tuning/initial-tier/*`.
13. **Review screen UI shell** — `/app/upload/[episodeId]/review/page.tsx`.
14. **TierCard, ReasonList, InputCategoryCard, MissingDataWarnings** components.
15. **OverrideModal** with required-reason validation.
16. **End-to-end test** — upload synthetic patient → compute → render → accept → episode persisted with full audit chain.
17. **Synthetic load** — 100 patients across the procedure families and risk distributions; assert tier distribution roughly matches expected (rough QA target: 35–45% Tier 1, 30–40% Tier 2, 20–30% Tier 3 across a representative cohort; outliers warrant review of weights).

---

## 14. Acceptance criteria summary

The feature is complete when:

- [ ] Upload of a TEAM-eligible patient with all six categories present yields a computed tier within 2 seconds.
- [ ] Hard escalators short-circuit to TIER_3 with a single labeled reason and `score = null`.
- [ ] Soft scoring sums procedure base + each fired flag's weight; the displayed total equals the algorithmic total exactly.
- [ ] Score thresholds map: ≥8 → TIER_3, ≥4 → TIER_2, else TIER_1 (per current tuning).
- [ ] All worked examples (§5.6) pass as unit tests.
- [ ] Override persists both auto-assigned tier and the override with reason ≥30 chars.
- [ ] Episode is stamped with `initialTierModelVersion` and `initialTierTuningVersion`.
- [ ] Inputs snapshot is stored as JSON on the episode for reproducibility.
- [ ] Missing-data warnings fire for stale or absent labs without blocking acceptance.
- [ ] Every state-changing call writes a `TriageEvent` with full metadata.
- [ ] Tuning config reload does not corrupt in-flight compute calls (atomic version swap).
- [ ] All UI meets WCAG 2.1 AA.
- [ ] 100-patient synthetic batch runs without errors and produces a defensible tier distribution.

---

## 15. References (clinical anchors)

The model is grounded in publicly available, peer-reviewed risk frameworks. We do not call external APIs at runtime; the input variable lists and threshold defaults are sourced from these references and replicated in `tuning.json`.

- ACS-NSQIP Surgical Risk Calculator — Bilimoria KY et al., *J Am Coll Surg* (2013). Input variables and the canonical pre-op chart-derivable factor list.
- ACS-NSQIP universal preoperative risk variables documentation (`https://riskcalculator.facs.org`).
- CMS TEAM Model procedure family definitions (LEJR, CABG, spinal fusion, hip/femur fracture, major bowel) — sets the five procedure families considered in v1.
- Albumin and surgical outcomes — Gibbs J et al., *Arch Surg* (1999). Threshold of 3.5/3.0 g/dL for hypoalbuminemia / severe malnutrition.
- Pre-op anemia thresholds — WHO definitions (Hb <12 women, <13 men) used by NSQIP.
- HbA1c and surgical outcomes — Underwood P et al., *Diabetes Care* (2014). >8% threshold for elevated peri-op risk.
- Functional status mortality association — Khuri SF et al., NSQIP foundational publications.
- Social determinants and 30-day readmission — multiple AHRQ briefs and Joint Commission guidance.

Concrete thresholds are encoded as defaults in `tuning.json`; clinical leadership is expected to review and recalibrate quarterly against observed outcomes.

---

## 16. Glossary

- **Tier** — patient pre-op risk classification (TIER_1 / TIER_2 / TIER_3); TIER_3 is highest risk.
- **Hard escalator** — single condition that promotes patient directly to TIER_3, short-circuiting the soft-factor scoring.
- **Soft factor** — weighted contributor; sum determines tier when no hard escalator triggers.
- **Procedure base** — fixed score added per procedure family before soft factors.
- **Tuning config** — versioned JSON of weights, thresholds, and procedure base scores; reloadable without deploy.
- **Missing-data warning** — UI-surfaced flag when a key input (e.g., albumin, eGFR) is absent or stale.
- **Override** — RN/coordinator action that replaces the computed tier with a manual selection; both values persist.
- **Inputs snapshot** — frozen JSON of the six categories at the moment of compute, stored on the episode for reproducibility.

---

*End of PRD v1.0 — Initial Pre-Op Triage*
