# PRD — Intra-Op Reassessment (Switch-to-Post-Op Tier Recompute)

| Field | Value |
|---|---|
| Feature | Intra-Op Reassessment |
| Document version | 1.0 (final) |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-05-08 |
| Primary user | Operating **surgeon** (reviews, may edit, and **locks** after RN handoff) |
| Secondary users | **RN care coordinator** (owns draft until marked ready for review), **NP/PA** (read-only on triage surfaces). |
| Implementation target | Next.js 14 App Router + TypeScript + Tailwind + shadcn/ui + Prisma/Postgres (PRD style); LLM-based PDF extractor for operative notes |
| Audience | Cursor / engineering implementers |
| Depends on | Initial Pre-Op Triage PRD v1.0 (`initial-triage-v1.md`); Pre-Op Re-Tiering PRD v1.0 (`preop-retier-v1.md`); existing episode lifecycle (transition from pre-op → intra-op → post-op) |
| Supersedes | Existing `Intraop Reassessment PRD v0.1` provided in chat |

> **Updated 2026-05-10 (Triage Suite pass 4):** The workflow changed from a single-surgeon draft-and-lock model to **RN care coordinator drafts → surgeon reviews and locks**. Status `READY_FOR_LOCK` is replaced by **`READY_FOR_SURGEON_REVIEW`**. See §3.2–§3.3, §7, and §10.

---

## 0. Reading order and conventions

- TIER_3 = highest risk (preserved across all triage PRDs in this repo).
- The intra-op reassessment is **upward-only** by design — it never downgrades the patient's tier. The resolution rule is "most conservative wins" between the current tier (pre-op or pre-op re-tiered) and the intra-op-proposed tier.
- **Pass 4:** The **RN care coordinator** drafts the form (manual fill, PDF extraction, autosave). The **surgeon** reviews once the RN marks **`READY_FOR_SURGEON_REVIEW`**, may edit, and is the only role that may **lock**. NP/PA may view but cannot write triage endpoints.
- All times are wall-clock; OR duration is the only computed time field on the form.
- The form is created server-side at the moment the episode records `OR_ENDED` (timestamp captured by the OR scheduling integration or by a manual surgeon action). It is opened by the surgeon clicking **Switch to post-op** on the episode page; the click opens the form in whatever state it is in (empty, partially auto-populated by AIMS, or partially filled).

---

## 1. Scope

**In scope.**

1. The "Switch to post-op" entry point and the modal/page it opens.
2. The intra-op form: 11 required universal fields (your list) plus an optional extended set sourced from the existing intra-op PRD where clinically meaningful for tier scoring.
3. Two ingestion paths into the form:
   - **PDF operative note upload + AI extraction** — uploaded file is parsed by an LLM extractor that emits a `Partial<IntraopForm>` with per-field confidence scores; surgeon reviews and confirms each extracted field before lock.
   - **Manual fill** — surgeon types into the form directly.
4. The intra-op delta algorithm: hard upgrades (any one → TIER_3), soft upgrades (each adds one step; ≥2 aggregate to TIER_3), procedure-family-specific contributors.
5. The resolution rule: `resolveFinalTier(currentTier, proposedTier)` returns the most conservative (higher rank) of the two.
6. Form lifecycle: **NEW → IN_PROGRESS → READY_FOR_SURGEON_REVIEW → LOCKED**, with **REOPENED** as an intermediate editing state; admin **or** the **locking surgeon** may reopen (see §7.3).
7. Conservative default: if the form is not locked within 24h of `OR_ENDED`, the system auto-applies a one-step tier upgrade with reason "Intra-op data unavailable; conservative default applied." If the surgeon locks later, real data overrides.
8. Audit trail (`IntraOpReassessmentEvent`), API contracts, Prisma schema, file structure, edge cases, build order.

**Out of scope.**

- The PACU RN finalizer multi-contributor flow described in the existing PRD v0.1 (replaced by single-surgeon model per your edit).
- Production EHR/AIMS integration plumbing (we specify the `IntraopAutoPopulator` interface and a stub; the real integration is a separate workstream).
- The post-op re-tiering pipeline that begins after intra-op reassessment fires (its own PRD).
- Anesthesia documentation tooling beyond the fields we capture.
- OR scheduling / case logistics.

---

## 2. Why this exists (1 paragraph)

The pre-op tier captures who walked in and how engaged they were heading into surgery. The post-op signals will tell us how recovery unfolds. Between them is the surgery itself — the single largest perturbation of physiology in the episode and one of the strongest predictors of 30-day complications. A pre-op TIER_1 patient who has a 4-hour case with 800ml EBL, sustained hypotension, and a vasopressor requirement is not the same patient at PACU as they were on the operating table — and the system that drives post-op cadence and threshold sensitivity needs to know that *before* the first home vital arrives 12 hours later. This feature closes that gap. On **Switch to post-op**, the intra-op form is created (**NEW**). The **RN care coordinator** fills the structured summary (manual entry and/or PDF extraction with review), marks the draft **ready for surgeon review** when all required fields validate, and the **surgeon** opens the **Forms awaiting your review** queue (or deep-links from an alert), may edit, confirms, and **locks**; the system then re-runs tier computation, writes the new tier with full audit, and moves the episode to post-op.

---

## 3. The Switch-to-post-op flow

### 3.1 Trigger

On the episode page, after `OR_ENDED` is recorded, a primary CTA labeled **"Switch to post-op"** appears. Two server-side preconditions before the button enables:

- `episode.status === 'INTRA_OP'`
- `episode.orEndedAt !== null`

Clicking the button opens the intra-op form. If the form does not yet exist, it is created server-side with status `NEW` (with auto-populator stub run) and opened. If it exists in any pre-LOCK state, it opens at that state.

### 3.2 Two ingestion paths (RN-led draft)

While the form is in **NEW** / **IN_PROGRESS** / **REOPENED**, the **RN care coordinator** drives data entry. The form surfaces the same two affordances:

1. **Upload operative note (PDF)** — RN or surgeon (when in surgeon-review state) may upload; extraction jobs and confidence pills behave as before.
2. **Fill manually** — RN enters the 11 required fields; autosave is debounced.

After Pass 4, **automatic transition to “ready to lock” on field completion is removed**. The RN explicitly calls **`POST .../mark-ready-for-review`** once validation passes (`validate_required_fields` + OR duration consistency). That sets **`READY_FOR_SURGEON_REVIEW`**, stamps `draft_completed_by` / `draft_completed_at`, writes an **`escalations`** row (`trigger_type=intraop:ready_for_review`, `tier=2`) for the surgeon queue, and logs **`INTRAOP_FORM_READY_FOR_REVIEW`**.

The RN may **`POST .../recall`** while in **`READY_FOR_SURGEON_REVIEW`** to return to **`IN_PROGRESS`** (clears draft-completion metadata, logs recall + escalation `intraop:draft_recalled`).

The two paths remain **not mutually exclusive**: PDF extraction and manual edits share one form state with per-field origins.

### 3.3 Lock behavior (surgeon only)

The **Lock & switch to post-op** action is **surgeon-only** and enabled only when **`status === READY_FOR_SURGEON_REVIEW`** (all required fields still validated server-side). The surgeon may **`PATCH`** the form while in that state to refine values before locking.

On confirm:

1. Form state → **`LOCKED`**.
2. `applyIntraopReassessment(episodeId)` runs synchronously.
3. Episode tier updates if the resolved tier differs.
4. Episode `status` transitions to **`POST_OP`**.

### 3.3a (obsolete) Previous single-surgeon auto-ready model

Older drafts described auto-advance to `READY_FOR_LOCK` when fields completed; **Pass 4 replaces that** with explicit RN **`mark-ready-for-review`** and renames the state to **`READY_FOR_SURGEON_REVIEW`**.

### 3.4 Acceptance criteria

- **AC-3.1** "Switch to post-op" button enables exactly when `OR_ENDED` is recorded and form is not already LOCKED.
- **AC-3.2** Clicking creates the form (if needed) and opens it within 1.5s.
- **AC-3.3** PDF upload starts an extraction job and fields begin populating within 8s for an average 2-page operative note.
- **AC-3.4** Each extracted field shows a confidence pill; LOW-confidence fields require explicit confirmation before lock-eligibility.
- **AC-3.5** Manual edits override extracted values; both values are preserved in audit.
- **AC-3.6** Lock confirms tier change; episode transitions to POST_OP only after a successful reassessment write.
- **AC-3.7** A locked form is read-only; **admin (`X-Admin-Token`) or the locking surgeon (Bearer)** may reopen (§7.3).

---

## 4. Form fields

### 4.1 The 11 required universal fields (your list)

Required to lock the form. Each accepts a value from any of three origins (manual, AIMS auto-pop, PDF extraction) with origin metadata persisted per write.

| # | Field | Component | Notes |
|---|---|---|---|
| 1 | Documented intra-operative complication | `<RadioGroup>` Yes / No | If Yes, expands optional complication-type multiselect (§4.2.4). |
| 2 | Estimated blood loss (mL) | `<NumberInput>` | Min 0, max 10000. |
| 3 | Transfusion volume (total units) | `<NumberInput>` | Aggregate units across components. If >0, expands optional component breakdown (§4.2.3). |
| 4 | Conversion (MIS → open) | `<RadioGroup>` Yes / No / N/A | If Yes, expands optional reason textarea (§4.2.5). |
| 5 | Sustained intra-operative hypotension | `<RadioGroup>` Yes / No | "Sustained" = MAP <65 for >10 min cumulative. Tooltip restates definition. |
| 6 | Vasopressor requirement | `<Select>` None / Brief (≤30 min) / Sustained (>30 min) | |
| 7 | Significant arrhythmia | `<RadioGroup>` Yes / No | Definition: required intervention (rate/rhythm control, shock, pacing). |
| 8 | OR duration (HH:MM) | `<DurationInput>` | Auto-populated from `orStartAt` and `orEndAt` if both present; otherwise editable directly. |
| 9 | Difficult airway encountered | `<RadioGroup>` Yes / No | |
| 10 | Net fluid balance (mL) | `<NumberInput>` (signed) | Auto-populated from `fluidIn − fluidOut` if both present; otherwise editable directly. |
| 11 | Anesthesia type | `<Select>` General / Regional / MAC / Combined | |

### 4.2 Extended fields (optional; auto-populated when available)

These are not required to lock but sharpen the algorithm when present. The PDF extractor and AIMS auto-populator both attempt to fill them; the surgeon may also fill any of them manually.

#### 4.2.1 OR timestamps (drives field #8)

- `orStartAt` — `<DateTimePicker>`; auto-pop from OR system or PDF.
- `orEndAt` — `<DateTimePicker>`; auto-pop from OR system or PDF.

If both present, OR duration is computed and locked to display-only; if either is missing, OR duration is directly editable.

#### 4.2.2 ASA class (auto-pop from AIMS; PDF often contains it)

- `<Select>` 1, 2, 3, 4, 5, 1E, 2E, 3E, 4E, 5E.

#### 4.2.3 Transfusion component breakdown (drives field #3 if all subsumed)

- `prbcUnits`, `plateletUnits`, `ffpUnits`, `cryoUnits` — each `<NumberInput>`.
- If all four are filled, `transfusionTotalUnits` is computed as the sum and locks.
- If only the aggregate field #3 is filled, components are blank; algorithm uses the aggregate.

#### 4.2.4 Complication subtype (when field #1 = Yes)

`<MultiSelect>` chips (at least one required if field #1 = Yes):

- Vascular injury (arterial or venous)
- Visceral / organ injury
- Dural tear (CNS leak)
- Nerve injury
- Cardiac event (ischemia, arrest, tamponade)
- Pulmonary event (pneumothorax, embolism)
- Anesthesia complication (aspiration, allergic reaction, awareness)
- Equipment failure with clinical impact
- Other (free text, required if selected)

Plus an optional free-text description (`<Textarea>`, min 20 chars when present).

#### 4.2.5 Conversion reason (when field #4 = Yes)

- `<Textarea>`, min 20 chars when present.

#### 4.2.6 Hypoxia event

- `<RadioGroup>` Yes / No — "Yes" = SpO2 <90% sustained intra-op. Soft contributor in §5.

#### 4.2.7 Procedure-family-specific extension fields

Loaded conditionally based on `episode.anchorProcedureFamily` and used by the algorithm in §5.4. All are auto-populable from the PDF or AIMS; none are *required* to lock the form.

**LEJR (lower extremity joint replacement).**

| Field | Component |
|---|---|
| Joint | `<RadioGroup>` Hip / Knee |
| Side | `<RadioGroup>` Left / Right / Bilateral |
| Fixation type | `<Select>` Cemented / Cementless / Hybrid |
| Prosthesis manufacturer & model | `<Combobox>` |
| Component sizes | `<Textarea>` |
| Intra-operative fracture | `<RadioGroup>` Yes / No |
| → Fracture location | `<Select>` Femoral / Acetabular / Tibial / Other |

**CABG.**

| Field | Component |
|---|---|
| Number of grafts | `<NumberInput>` 1–6 |
| Pump strategy | `<RadioGroup>` On-pump / Off-pump |
| Aortic cross-clamp time (min) | `<NumberInput>` |
| Cardiopulmonary bypass time (min) | `<NumberInput>` |
| Aortic manipulation | `<RadioGroup>` Yes / No |
| Grafts used | `<MultiSelect>` LIMA / RIMA / SVG / Radial / Other |
| Successful weaning from bypass | `<RadioGroup>` Yes / Difficult / Required mechanical support |

**Spinal fusion.**

| Field | Component |
|---|---|
| Approach | `<Select>` Anterior / Posterior / Combined / Lateral |
| Number of levels fused | `<NumberInput>` 1–10 |
| Levels (e.g., L4-L5) | `<TagInput>` |
| Instrumentation | `<RadioGroup>` Yes / No |
| Bone graft source | `<Select>` Autograft / Allograft / Synthetic / Combined |
| Dural tear | `<RadioGroup>` Yes / No |
| Neuromonitoring used | `<RadioGroup>` Yes / No |
| → Significant neuromonitoring changes | `<RadioGroup>` Yes / No |

**Hip / femur fracture.**

| Field | Component |
|---|---|
| Fracture pattern | `<Select>` Intracapsular / Intertrochanteric / Subtrochanteric / Femoral shaft |
| Fixation method | `<Select>` Dynamic Hip Screw / Intramedullary nail / Hemiarthroplasty / Total hip / ORIF other |
| Time-to-OR from admission (hours) | `<NumberInput>` |
| Weight-bearing status post-op | `<Select>` Full / Partial / Toe-touch / Non-weight-bearing |

**Major bowel.**

| Field | Component |
|---|---|
| Procedure type | `<Select>` Partial colectomy / Total colectomy / Small bowel resection / Other |
| Approach | `<Select>` Open / Laparoscopic / Robotic |
| Anastomosis performed | `<RadioGroup>` Yes / No |
| → Anastomosis location | `<Select>` Ileocolic / Colocolic / Coloanal / Ileal pouch-anal |
| Ostomy created | `<RadioGroup>` Yes / No |
| Wound contamination class | `<Select>` 1 (Clean) / 2 (Clean-contaminated) / 3 (Contaminated) / 4 (Dirty-infected) |

### 4.3 Per-field origin tracking

Every field write records:

```ts
interface FieldOrigin {
  origin: 'MANUAL' | 'AUTO_POP_AIMS' | 'AUTO_POP_PDF' | 'RN_DRAFT' | 'SURGEON_REVIEWED';
  source?: string;                  // 'aims:case-id-12345', 'pdf:upload-id-abc', 'manual'
  confidence?: number;              // 0..1, only for AUTO_POP_PDF
  populatedAt: string;              // ISO timestamp
  confirmedBy?: string;              // userId of surgeon who confirmed; required when origin != MANUAL and confidence < 0.85
  confirmedAt?: string;
  originalValue?: any;               // when surgeon edits an auto-populated field, the auto value is preserved
}
```

Origin metadata is stored in `IntraopForm.fieldOrigins` (Json). It is queryable for QA and for the "review LOW-confidence extractions" affordance.

---

## 5. The intra-op delta algorithm

The algorithm consumes the locked form and produces a `proposedTier` plus reasons. The episode's final tier is then resolved as the most conservative of `currentTier` (the tier in effect immediately before the reassessment, which may already reflect pre-op re-tiering) and `proposedTier`.

### 5.1 Pseudocode

```ts
// /lib/triage/intraop-delta.ts
type Tier = 'TIER_1' | 'TIER_2' | 'TIER_3';
const TIER_RANK: Record<Tier, number> = { TIER_1: 1, TIER_2: 2, TIER_3: 3 };

interface IntraopDeltaResult {
  proposedTier: Tier;
  hardUpgradeApplied: boolean;
  upgradeSteps: number;
  reasons: IntraopReason[];
}

export function computeIntraopDelta(
  form: LockedIntraopForm,
  procedureFamily: ProcedureFamily,
  hospitalProcedureStats: ProcedureStats,
  preOrCurrentTier: Tier
): IntraopDeltaResult {
  const reasons: IntraopReason[] = [];
  let hardUpgrade = false;
  let stepUpgrades = 0;

  // ── HARD UPGRADES (any one → TIER_3) ─────────────────────
  if (form.documentedComplication === true) {
    hardUpgrade = true;
    reasons.push(hard('Intra-operative complication documented', form.complicationTypes));
  }
  if (procedureFamily === 'SPINAL_FUSION' && form.duralTear === true) {
    hardUpgrade = true; reasons.push(hard('Dural tear'));
  }
  if (procedureFamily === 'MAJOR_BOWEL' && form.contaminationClass === 4) {
    hardUpgrade = true; reasons.push(hard('Wound contamination class 4 (dirty-infected)'));
  }
  if (procedureFamily === 'CABG' && form.weaningFromBypass === 'REQUIRED_MECHANICAL_SUPPORT') {
    hardUpgrade = true; reasons.push(hard('Required mechanical support to wean from bypass'));
  }
  if (form.proceduralAborted === true) {
    hardUpgrade = true; reasons.push(hard('Procedure aborted'));
  }

  // ── SOFT UPGRADES (each adds one step) ───────────────────
  if (form.ebl > 500) {
    stepUpgrades++; reasons.push(soft(`EBL ${form.ebl}ml exceeds 500ml threshold`));
  }
  const totalUnits =
    form.transfusionTotalUnits ??
    ((form.prbcUnits ?? 0) + (form.plateletUnits ?? 0) + (form.ffpUnits ?? 0) + (form.cryoUnits ?? 0));
  if (totalUnits >= 2) {
    stepUpgrades++; reasons.push(soft(`Transfused ${totalUnits} total units`));
  }
  if (form.conversion === true) {
    stepUpgrades++; reasons.push(soft('Converted from minimally invasive to open'));
  }
  if (form.sustainedHypotension === true) {
    stepUpgrades++; reasons.push(soft('Sustained intra-operative hypotension (MAP <65, >10 min)'));
  }
  if (form.vasopressorRequirement === 'SUSTAINED') {
    stepUpgrades++; reasons.push(soft('Sustained vasopressor requirement (>30 min)'));
  }
  if (form.hypoxiaEvent === true) {
    stepUpgrades++; reasons.push(soft('Intra-operative hypoxia event (SpO2 <90% sustained)'));
  }
  if (form.significantArrhythmia === true) {
    stepUpgrades++; reasons.push(soft('Significant arrhythmia requiring intervention'));
  }
  if (form.difficultAirway === true) {
    stepUpgrades++; reasons.push(soft('Difficult airway encountered'));
  }
  // OR time vs hospital P90 for procedure
  const p90 = hospitalProcedureStats.orDurationP90Minutes;
  if (form.orDurationMinutes > p90) {
    stepUpgrades++; reasons.push(soft(`OR time ${form.orDurationMinutes}min exceeds P90 (${p90}min) for ${procedureFamily}`));
  }

  // Procedure-family-specific soft upgrades
  if (procedureFamily === 'CABG') {
    if (form.aorticCrossClampMinutes && form.aorticCrossClampMinutes > 90) {
      stepUpgrades++; reasons.push(soft(`Cross-clamp time ${form.aorticCrossClampMinutes}min`));
    }
    if (form.cpbTimeMinutes && form.cpbTimeMinutes > 120) {
      stepUpgrades++; reasons.push(soft(`CPB time ${form.cpbTimeMinutes}min`));
    }
  }
  if (procedureFamily === 'SPINAL_FUSION') {
    if (form.numberOfLevelsFused && form.numberOfLevelsFused >= 4) {
      stepUpgrades++; reasons.push(soft(`${form.numberOfLevelsFused}-level fusion`));
    }
    if (form.neuromonitoringChanges === true) {
      stepUpgrades++; reasons.push(soft('Significant neuromonitoring changes'));
    }
  }
  if (procedureFamily === 'LEJR' && form.intraoperativeFracture === true) {
    stepUpgrades++; reasons.push(soft(`Intra-operative ${form.fractureLocation ?? ''} fracture`));
  }
  if (procedureFamily === 'MAJOR_BOWEL' && form.contaminationClass === 3) {
    stepUpgrades++; reasons.push(soft('Wound contamination class 3 (contaminated)'));
  }
  if (procedureFamily === 'HIP_FEMUR_FRACTURE' && form.timeToOrHours && form.timeToOrHours > 48) {
    stepUpgrades++; reasons.push(soft(`Time-to-OR ${form.timeToOrHours}h exceeds 48h threshold`));
  }

  // ── COMPUTE PROPOSED TIER ────────────────────────────────
  let proposedTier: Tier;
  if (hardUpgrade) {
    proposedTier = 'TIER_3';
  } else if (stepUpgrades >= 2) {
    proposedTier = 'TIER_3';                                     // 2+ soft upgrades aggregate
  } else if (stepUpgrades === 1) {
    proposedTier = stepUp(preOrCurrentTier, 1);
  } else {
    proposedTier = preOrCurrentTier;
    reasons.push(info('No intra-operative risk factors identified'));
  }

  return { proposedTier, hardUpgradeApplied: hardUpgrade, upgradeSteps: stepUpgrades, reasons };
}

function stepUp(t: Tier, n: number): Tier {
  const targetRank = Math.min(3, TIER_RANK[t] + n);
  return targetRank === 3 ? 'TIER_3' : targetRank === 2 ? 'TIER_2' : 'TIER_1';
}

// Final tier resolution — most conservative wins (upward-only)
export function resolveFinalTier(currentTier: Tier, intraOpProposedTier: Tier): Tier {
  return TIER_RANK[currentTier] >= TIER_RANK[intraOpProposedTier] ? currentTier : intraOpProposedTier;
}
```

### 5.2 Why this shape

- **Hard vs. soft.** Documented complications, dural tears, contamination class 4, mechanical-support bypass weaning, and procedural abort each independently predict 30-day complication rates that warrant TIER_3 cadence regardless of what else happened. Soft upgrades each add one step; two stacked soft upgrades aggregate to TIER_3 because compound intra-op stressors are non-linearly worse than single ones.
- **OR time is hospital-relative.** Absolute OR time is meaningless across surgeons and shops. Per-hospital, per-procedure P90 is the right benchmark. For prototype, ship with national benchmark P90s drawn from public NSQIP data; once 50+ cases are observed at a hospital, switch to observed P90.
- **Most conservative wins.** Pre-op TIER_3 isn't downgraded by an uneventful surgery; pre-op TIER_1 *is* upgraded by a hard intra-op event. Matches the upward-only stance of post-op re-tiering.
- **Conservative default for missing data.** When the form isn't locked within 24h of OR end, apply a 1-step upgrade with a clear reason. Better to over-monitor than to assume a clean case happened.
- **Idempotent.** Like the pre-op re-tier, the algorithm reads the form snapshot and current tier and produces a deterministic output. Reopen → re-lock fires a *new* reassessment event; the previous reassessment is preserved in audit.

### 5.3 Worked examples

**A. Pre-op TIER_1 → final TIER_3 (hard).** Pre-op TIER_1; form: documented complication = vascular injury. `proposedTier = TIER_3`. `resolveFinalTier(TIER_1, TIER_3) = TIER_3`.

**B. Pre-op TIER_1 → final TIER_2 (one soft).** Pre-op TIER_1; form: EBL = 600ml, no other risks. 1 soft upgrade. `proposedTier = TIER_2`. Final = TIER_2.

**C. Pre-op TIER_1 → final TIER_3 (two softs aggregate).** Pre-op TIER_1; form: EBL = 600ml AND 3-unit transfusion. 2 soft upgrades. `proposedTier = TIER_3`. Final = TIER_3.

**D. Pre-op TIER_3 → final TIER_3 (uneventful).** Pre-op TIER_3 (e.g., dialysis hard escalator); form: completely uneventful. `proposedTier = TIER_3` (pre-op rolled forward). Final = TIER_3.

**E. Spinal fusion with dural tear.** Pre-op TIER_2; form: dural tear = Yes. Hard upgrade. `proposedTier = TIER_3`. Final = TIER_3.

**F. Major bowel, contamination class 3 + EBL 300ml.** Pre-op TIER_2; form: contamination class 3 (1 soft) + EBL 300 (no contributor). `proposedTier = stepUp(TIER_2, 1) = TIER_3`. Final = TIER_3.

**G. Form not locked within 24h.** Pre-op TIER_1; OR ended 25h ago; no lock. Conservative default fires: `proposedTier = stepUp(TIER_1, 1) = TIER_2`. Final = TIER_2 with reason `INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT`. If surgeon locks at 30h with an uneventful form, a new reassessment fires: `proposedTier = TIER_1`, but `resolveFinalTier(TIER_2, TIER_1) = TIER_2`. The conservative default is preserved as the floor; explicit admin REOPEN with reason can be used in rare cases to revisit.

These seven cases are encoded as fixtures in `/lib/triage/__tests__/intraop-delta.fixtures.ts` and asserted by unit tests.

### 5.4 Acceptance criteria for the algorithm

- **AC-5.1** Documented complication → `proposedTier = TIER_3` regardless of other fields.
- **AC-5.2** EBL=600ml + zero other risks → `proposedTier = TIER_2` (from TIER_1).
- **AC-5.3** EBL=600ml AND 3-unit transfusion → `proposedTier = TIER_3` (2 soft aggregate).
- **AC-5.4** Pre-op TIER_3 + uneventful form → final TIER_3.
- **AC-5.5** Spinal fusion + dural tear → TIER_3.
- **AC-5.6** Major bowel + contamination class 3 → TIER_3 (1 soft from class 3 from pre-op TIER_2).
- **AC-5.7** Form not locked within 24h of OR end → 1-step conservative default with reason "Intra-op data unavailable; conservative default applied."
- **AC-5.8** Late form completion after conservative default fires a new reassessment; `resolveFinalTier` keeps the higher of the two; both events preserved in audit.

---

## 6. PDF operative-note extraction

### 6.1 Interface

```ts
// /lib/triage/intraop-extractor.ts
export interface IntraopExtractor {
  extract(input: { episodeId: string; pdfBlobUrl: string }): Promise<IntraopExtraction>;
}

export interface IntraopExtraction {
  fields: Partial<IntraopForm>;
  fieldConfidences: Record<keyof IntraopForm, number>;          // 0..1 per field
  rawText: string;                                                // OCR'd text for audit
  modelVersion: string;                                           // e.g., 'intraop-extractor@1.0.0'
  promptVersion: string;
  extractedAt: string;
  warnings: string[];                                             // e.g., 'EBL not found'
}
```

### 6.2 Production extractor

`LlmIntraopExtractor` calls Claude (the existing in-repo model usage) with:

- A structured system prompt instructing it to extract ONLY the fields enumerated in `IntraopForm`, return a JSON object matching the schema, and provide a confidence per field.
- The PDF rendered to text (via the existing PDF parsing path used elsewhere in the repo).
- A few-shot example of an ideal extraction for each procedure family.

Confidence is derived heuristically: model is asked to self-rate `HIGH/MED/LOW` per field, mapped to 0.95 / 0.75 / 0.50; fields not found in text return undefined with confidence 0.

### 6.3 Stub extractor for prototype

`MockIntraopExtractor` returns a deterministic partial payload based on the episode's procedure family. Used in dev and CI.

### 6.4 Surgeon review UI

The form's right rail shows extraction status:

```
EXTRACTION
─────────────────────────────
Source: op_note_2026-05-22.pdf
Model:  intraop-extractor@1.0.0
Status: ✓ Complete (12 fields populated, 2 LOW confidence)

LOW confidence — please review:
  • OR duration         (0.52)  [confirm] [edit]
  • Contamination class (0.48)  [confirm] [edit]
```

Each field on the form shows the confidence pill inline. LOW-confidence fields display with a yellow border and require explicit confirm or edit before lock-eligibility.

### 6.5 Acceptance criteria for extraction

- **AC-6.1** PDF upload of an average 2-page operative note returns extracted fields within 8s P95.
- **AC-6.2** Each field has a confidence in `[0, 1]`; UI shows the pill.
- **AC-6.3** LOW-confidence fields cannot count toward lock-eligibility until confirmed.
- **AC-6.4** Surgeon edits to extracted values preserve the original extracted value in `fieldOrigins.originalValue`.
- **AC-6.5** Extraction failures (timeout, model error, unparseable PDF) leave the form openable for manual fill with a clear inline error.
- **AC-6.6** Raw extracted text is preserved on `IntraopExtraction` for audit and quality review.

---

## 7. Form lifecycle

### 7.1 States (Pass 4)

```
[NEW]                  ← created when episode records OR_ENDED / switch-to-postop
   │
   │ RN opens / edits (or REOPENED re-enters here as IN_PROGRESS after PATCH)
   ▼
[IN_PROGRESS]          ← RN draft; autosave; PDF + manual paths
   │
   │ RN POST /mark-ready-for-review (validated)
   ▼
[READY_FOR_SURGEON_REVIEW]
   │                    ← surgeon may PATCH edits, then POST /lock
   │ RN may POST /recall → back to IN_PROGRESS
   │
   │ surgeon POST /lock
   ▼
[LOCKED]               ← reassessment fires; episode → POST_OP
   │
   │ admin X-Admin-Token OR locking surgeon Bearer POST /reopen
   ▼
[REOPENED]             ← RN resumes draft → mark-ready → surgeon lock again
```

`READY_FOR_LOCK` is **deprecated**; existing rows migrate to `READY_FOR_SURGEON_REVIEW`.

### 7.2 Autosave

Every field edit autosaves with a 500ms debounce. The form survives accidental tab close. Surgeon can leave and return without losing work.

### 7.3 Reopen

**Admin:** `POST .../reopen` with valid **`X-Admin-Token`**.

**Locking surgeon:** same endpoint with **Bearer only** (no admin token); server matches JWT email to `surgeon_locked_by`.

On reopen:

- `IntraopForm.status = REOPENED`.
- Audit event **`INTRAOP_FORM_REOPENED`** (and existing event log rows).
- RN may resume **`IN_PROGRESS`** via subsequent **`PATCH`**; cycle repeats through **`READY_FOR_SURGEON_REVIEW`** and surgeon **lock**.

### 7.4 Conservative default

A cron job (`intraop-overdue-watcher`) runs every 15 minutes. For every episode with `orEndedAt` ≥ 24h ago and form status not LOCKED:

1. If a `CONSERVATIVE_DEFAULT_APPLIED` flag is not yet set, apply a 1-step tier upgrade.
2. Write `INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT` event with reason "Intra-op data unavailable; conservative default applied."
3. Notify the assigned RN and on-call surgeon.
4. Set `IntraopForm.conservativeDefaultAppliedAt`.

The form remains openable. When the surgeon does eventually lock it, the late lock fires a normal reassessment; resolution remains "most conservative wins," so the conservative default's tier persists unless the lock proposes an even higher tier. Admin REOPEN can be used in rare cases (e.g., genuinely benign case erroneously upgraded by the default) to revisit.

### 7.5 Acceptance criteria for lifecycle

- **AC-7.1** Autosave fires within 1s of last keystroke; survives tab close.
- **AC-7.2** Lock requires all 11 required fields with confirmed origins.
- **AC-7.3** Lock button shows the proposed tier change in a confirm modal before applying.
- **AC-7.4** Reopen accepts **admin token** OR **locking surgeon** session (implementation-defined).
- **AC-7.5** Conservative default fires exactly once per episode; subsequent late locks do not double-apply.
- **AC-7.6** Episode `status` transitions to POST_OP atomically with the LOCK + reassessment write (single transaction).

---

## 8. Integration with the rest of the system

### 8.1 Tier write path

`applyIntraopReassessment(episodeId)` is the only function that writes the intra-op tier change. It is called:

- Synchronously when the form locks (within the LOCK transaction).
- Synchronously by the conservative-default cron when 24h elapses with no lock.
- Synchronously when an admin REOPEN → re-lock cycle completes.

It does:

1. Loads the locked form (or constructs a "no-data" form for the conservative-default path).
2. Loads the *current* episode tier (which may already reflect pre-op re-tiering).
3. Calls `computeIntraopDelta(...)`.
4. Calls `resolveFinalTier(currentTier, proposedTier)`.
5. If the resolved tier ≠ current, calls `Episode.updateTier(...)` (the same routine used by initial-tier and pre-op re-tier) with reason "Intra-op reassessment."
6. Writes `IntraOpReassessmentEvent` with the form snapshot, computed delta, reasons, and final tier.
7. Writes a `TriageEvent` of type `INTRAOP_REASSESSMENT_APPLIED`.
8. Transitions episode status from INTRA_OP to POST_OP if not already.

### 8.2 Pre-op re-tier sticky guard does not apply

The pre-op re-tier's sticky-hard-escalator guard prevents *downgrade* below the initial tier. The intra-op reassessment is upward-only by construction; the guard is irrelevant. The intra-op result simply replaces the current tier when higher.

### 8.3 Coordinator queue surfacing

The queue tier card (already extended in pre-op re-tier PRD) gains one more chip when an intra-op reassessment has fired:

```
▮ TIER 3   (pre-op T2 → intra-op reassessed)   reassessed @ 14:32
  Top reasons: Documented complication (vascular injury); EBL 800ml
  [ ⓘ View intra-op form ]   [ ⓘ View reassessment history ]
```

---

## 9. Data model (Prisma)

```prisma
model IntraopForm {
  id                          String   @id @default(cuid())
  episodeId                   String   @unique

  status                      IntraopFormStatus  // NEW | IN_PROGRESS | READY_FOR_SURGEON_REVIEW | LOCKED | REOPENED

  // OR times
  orStartAt                   DateTime?
  orEndAt                     DateTime?
  orDurationMinutes           Int?     // computed when both present, else manually editable
  asaClass                    String?

  // The 11 required universal fields (your list)
  documentedComplication      Boolean?
  ebl                         Int?
  transfusionTotalUnits       Int?
  conversion                  String?  // 'YES' | 'NO' | 'N_A'
  sustainedHypotension        Boolean?
  vasopressorRequirement      String?  // 'NONE' | 'BRIEF' | 'SUSTAINED'
  significantArrhythmia       Boolean?
  difficultAirway             Boolean?
  netFluidBalance             Int?
  anesthesiaType              String?  // 'GENERAL' | 'REGIONAL' | 'MAC' | 'COMBINED'

  // Extended optional fields
  prbcUnits                   Int?
  plateletUnits               Int?
  ffpUnits                    Int?
  cryoUnits                   Int?
  fluidIn                     Int?
  fluidOut                    Int?
  conversionReason            String?
  hypoxiaEvent                Boolean?
  complicationTypes           Json?    // string[]
  complicationDescription     String?
  proceduralAborted           Boolean? @default(false)
  proceduralAbortedReason     String?

  // Procedure-family-specific (single JSON blob; shape per family)
  procedureSpecific           Json?

  // Origin tracking per field
  fieldOrigins                Json     // Record<fieldName, FieldOrigin>

  // PDF upload + extraction
  pdfBlobUrl                  String?
  extractionId                String?
  extraction                  IntraopExtraction? @relation(fields: [extractionId], references: [id])

  // Lock metadata
  surgeonLockedBy             String?
  surgeonLockedAt             DateTime?
  conservativeDefaultAppliedAt DateTime?

  createdAt                   DateTime @default(now())
  updatedAt                   DateTime @updatedAt

  episode                     Episode  @relation(fields: [episodeId], references: [id])
  reassessments               IntraOpReassessmentEvent[]
}

model IntraopExtraction {
  id              String   @id @default(cuid())
  episodeId       String
  pdfBlobUrl      String
  rawText         String
  fields          Json     // Partial<IntraopForm>
  fieldConfidences Json    // Record<field, number>
  modelVersion    String
  promptVersion   String
  warnings        Json     // string[]
  status          ExtractionStatus  // PENDING | RUNNING | COMPLETE | FAILED
  errorMessage    String?
  startedAt       DateTime @default(now())
  completedAt     DateTime?
  intraopForm     IntraopForm[]
}

model IntraOpReassessmentEvent {
  id                          String   @id @default(cuid())
  episodeId                   String
  intraopFormId               String

  formSnapshot                Json                            // full form at moment of lock; immutable
  preOrCurrentTier            Tier                            // tier in effect immediately before reassessment
  proposedTier                Tier
  finalTier                   Tier                            // resolveFinalTier output
  hardUpgradeApplied          Boolean
  upgradeSteps                Int
  reasons                     Json                            // IntraopReason[]
  isConservativeDefault       Boolean  @default(false)

  modelVersion                String                          // e.g., 'intraop-delta@1.0.0'
  tuningVersion               Int

  triggeredBy                 String                          // userId or 'SYSTEM:CONSERVATIVE_DEFAULT' or 'ADMIN_REOPEN_RELOCK'
  triggeredAt                 DateTime @default(now())

  intraopForm                 IntraopForm @relation(fields: [intraopFormId], references: [id])
  episode                     Episode    @relation(fields: [episodeId], references: [id])

  @@index([episodeId, triggeredAt])
}

enum IntraopFormStatus { NEW IN_PROGRESS READY_FOR_SURGEON_REVIEW LOCKED REOPENED }
enum ExtractionStatus { PENDING RUNNING COMPLETE FAILED }

// Reused: existing TriageEventType enum gains
enum TriageEventType {
  // ... existing
  INTRAOP_FORM_CREATED
  INTRAOP_FORM_FIELD_UPDATED
  INTRAOP_FORM_LOCKED
  INTRAOP_FORM_REOPENED
  INTRAOP_REASSESSMENT_APPLIED
  INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT
  INTRAOP_PDF_UPLOADED
  INTRAOP_EXTRACTION_COMPLETED
  INTRAOP_EXTRACTION_FAILED
}

// Reused: existing AlertReason enum gains
enum AlertReason {
  // ... existing
  INTRAOP_FORM_OVERDUE
}
```

Add to `Episode`:

```prisma
model Episode {
  // ... existing
  intraopFormId     String?  @unique
  intraopForm       IntraopForm?
  intraOpReassessments IntraOpReassessmentEvent[]
}
```

---

## 10. API contracts

### 10.1 Form lifecycle

**`POST /api/episodes/:episodeId/intraop-form`** — create form (idempotent). Called by the OR-end hook and on the first "Switch to post-op" click as a fallback.

```ts
// Response 201 (or 200 if already exists in non-LOCKED state)
{ form: IntraopForm }
```

**`GET /api/episodes/:episodeId/intraop-form`** — fetch current form + extraction status.

```ts
// Response 200
{ form: IntraopForm, extraction: IntraopExtraction | null, requiredFieldsRemaining: string[] }
```

**`PATCH /api/episodes/:episodeId/intraop-form`** — partial update (autosave). **Role + status gates:** RN may edit in **NEW / IN_PROGRESS / REOPENED**; **surgeon** may edit only in **READY_FOR_SURGEON_REVIEW**; **LOCKED** → `409`.

```ts
// Request
{ fields: Partial<IntraopForm>, origin: FieldOrigin }
// Response 200
{ form: IntraopForm, missing: string[] }
```

**`POST /api/episodes/:episodeId/intraop-form/mark-ready-for-review`** — **RN coordinator only.** Validates required fields + OR duration; sets **READY_FOR_SURGEON_REVIEW**, draft metadata, **`escalations`** row `intraop:ready_for_review`, event **`INTRAOP_FORM_READY_FOR_REVIEW`**.

**`POST /api/episodes/:episodeId/intraop-form/recall`** — **RN coordinator only** while **READY_FOR_SURGEON_REVIEW** → **IN_PROGRESS**; escalation `intraop:draft_recalled`, event **`INTRAOP_FORM_RECALLED`**.

**`GET /api/intraop-forms?status=READY_FOR_SURGEON_REVIEW`** — **Surgeon only** (tenant-scoped). Powers **Forms awaiting your review** on the clinician dashboard.

**`POST /api/episodes/:episodeId/intraop-form/lock`** — **Surgeon only.** Requires **`READY_FOR_SURGEON_REVIEW`**. Lock + run reassessment + transition to POST_OP.

```ts
// Request: {} (uses session userId; verifies role)
// Response 200
{ form: IntraopForm, reassessment: IntraOpReassessmentEvent, episode: Episode }
// Errors
//   422 — required fields missing or unconfirmed (returns list)
//   409 — wrong status (e.g. still IN_PROGRESS) or already locked
```

**`POST /api/episodes/:episodeId/intraop-form/reopen`** — **`X-Admin-Token`** **or** locking **surgeon** Bearer (email must match `surgeon_locked_by`).

```ts
// Response 200
{ form: IntraopForm }
```

### 10.2 PDF upload + extraction

**`POST /api/episodes/:episodeId/intraop-form/pdf`** — multipart upload.

```ts
// Form field: "file" (PDF, max 25 MB)
// Response 202
{ extractionId: string, status: 'RUNNING' }
```

**`GET /api/intraop-extractions/:extractionId`** — poll extraction status (SSE alternative supported).

```ts
// Response 200
{ extraction: IntraopExtraction }
```

### 10.3 Tuning

**`GET /api/triage/tuning/intraop/current`** — current weights, P90 stats, model versions.

**`POST /api/triage/tuning/intraop`** — admin-only deploy.

---

## 11. Tuning config

`tuning.json` gains an `intraop` block.

```json
{
  "intraop": {
    "version": 1,
    "modelVersion": "intraop-delta@1.0.0",
    "softThresholds": {
      "eblMl": 500,
      "transfusionUnits": 2,
      "vasopressorSustainedMin": 30,
      "spo2HypoxiaThresholdPct": 90,
      "mapHypotensionThreshold": 65,
      "mapHypotensionMinDuration": 10,
      "cabgCrossClampMinutes": 90,
      "cabgCpbMinutes": 120,
      "spinalLevelsAggregate": 4,
      "hipFemurTimeToOrHours": 48
    },
    "procedureP90Minutes": {
      "LEJR": 120,
      "CABG": 270,
      "SPINAL_FUSION": 240,
      "HIP_FEMUR_FRACTURE": 150,
      "MAJOR_BOWEL": 210
    },
    "conservativeDefault": {
      "thresholdHoursAfterOrEnd": 24,
      "upgradeSteps": 1
    },
    "extraction": {
      "modelVersion": "intraop-extractor@1.0.0",
      "promptVersion": "v1",
      "lowConfidenceThreshold": 0.65,
      "midConfidenceThreshold": 0.85,
      "timeoutSec": 30,
      "maxPdfSizeMb": 25
    }
  }
}
```

Procedure P90s ship with national-benchmark defaults; replace with hospital-specific observed P90s once 50+ cases per family are recorded. Tuning version stamped on every reassessment for reproducibility.

---

## 12. Component / file structure

**Implementation note (CareGuide / this repo):** The production surface is **FastAPI** routers under `backend/routers/intraop.py`, persistence in **`backend/team_store.py`** (`intraop_forms` table), and static UIs **`frontend/intraop-form.html`** + **`frontend/doctor.html`** (review queue). The tree below remains a logical Next.js reference from v1.0 authoring.

### 12.1 Permissions (Pass 4 summary)

| Action | surgeon | rn_coordinator | np_pa |
|--------|---------|----------------|-------|
| GET form, preview, history, extractions | ✓ | ✓ | ✓ (read) |
| POST create form, PATCH (draft states) | — | ✓ | — |
| PATCH in READY_FOR_SURGEON_REVIEW | ✓ | — | — |
| mark-ready-for-review | — | ✓ | — |
| recall | — | ✓ | — |
| lock | ✓ | — | — |
| reopen (locking surgeon or admin) | ✓ (if locker) | — | — |
| PDF upload | ✓ | ✓ | — |

```
/app
  /episodes
    /[episodeId]
      /switch-to-postop/page.tsx           # Switch-to-post-op entry; renders the form
  /api
    /episodes/[episodeId]
      /intraop-form
        /route.ts                           # GET, POST, PATCH
        /lock/route.ts
        /reopen/route.ts
        /pdf/route.ts                       # multipart PDF upload
    /intraop-extractions
      /[extractionId]/route.ts              # poll + SSE
    /triage
      /tuning/intraop/current/route.ts
      /tuning/intraop/route.ts

/components
  /intraop
    SwitchToPostOpButton.tsx                # primary CTA on episode page
    IntraopForm.tsx                         # main form
    UniversalFieldsSection.tsx              # the 11 required fields
    ExtendedFieldsSection.tsx
    ProcedureFamilyExtension.tsx            # switches on episode.procedureFamily
    PdfUploadDropzone.tsx
    ExtractionStatusPanel.tsx               # right-rail status + LOW-confidence list
    ConfidencePill.tsx
    OriginIndicator.tsx                     # green dot for AUTO_POP_AIMS, amber for AUTO_POP_PDF, etc.
    LockConfirmModal.tsx
    ReopenModal.tsx
    ReassessmentHistoryTable.tsx

/lib
  /triage
    intraop-delta.ts                        # computeIntraopDelta, resolveFinalTier
    intraop-delta.weights.ts
    intraop-extractor.ts                    # IntraopExtractor interface
    intraop-extractor.llm.ts                # production LLM impl
    intraop-extractor.mock.ts               # stub
    intraop-form-validation.ts              # Zod schema for IntraopForm
    intraop-overdue-watcher.ts              # cron: 24h conservative default
    intraop-apply.ts                        # applyIntraopReassessment orchestration

/jobs
  intraop-overdue-watcher.ts                # cron entry

/__tests__
  intraop-delta.spec.ts
  intraop-delta.fixtures.ts                 # Examples A–G
  intraop-extractor.spec.ts                 # mock extractor + LOW-confidence handling
  intraop-form-validation.spec.ts
  intraop-apply.spec.ts                     # end-to-end: lock → reassessment → episode tier write
  intraop-overdue-watcher.spec.ts
```

---

## 13. Edge cases (enumerated)

1. **PDF unparseable / extraction fails.** Form opens for full manual fill; clear inline error in the right rail; surgeon can retry upload or fill manually.
2. **PDF extracts most fields but EBL is missing.** EBL field is empty with a warning chip; lock disabled until EBL is filled.
3. **Surgeon overrides an AUTO_POP_PDF value.** Original extracted value preserved in `fieldOrigins.originalValue`; new value stored with origin = MANUAL; field origin metadata logs both.
4. **Surgeon uploads a PDF after manually filling fields.** Extraction populates only the *empty* fields by default; for fields already filled, extracted values are surfaced as a "diff suggestions" panel rather than overwriting.
5. **Two PDFs uploaded.** Latest replaces previous; a new `IntraopExtraction` row is created; old extraction preserved for audit.
6. **Conservative default fires, then surgeon locks an uneventful form.** New reassessment fires `proposedTier = preOpTier` (or current); `resolveFinalTier` keeps the conservative-default tier (most conservative wins); admin REOPEN with reason can be used to revisit.
7. **Procedure aborted.** Special toggle (`proceduralAborted=true`); hard upgrade fires regardless of other fields; reason "Procedure aborted."
8. **Patient leaves OR alive but in extremis.** `proceduralAborted=false` but documented complication is true; hard upgrade applies. If a separate "emergency back-to-bedside" event happened and is documented in complications, it satisfies the hard rule.
9. **Surgery converted from TEAM-eligible to non-TEAM intra-op** (rare; e.g., LEJR converted to non-TEAM procedure). Form completes for documentation; episode flagged `INTERRUPTED_TEAM_INELIGIBILITY`; downstream triage tracking ceases for this episode but the reassessment event is still written.
10. **OR time exactly equal to P90.** Strict greater-than; no contributor.
11. **Missing hospital P90 (new procedure family at hospital).** Falls back to national benchmark (in tuning); flag for QA so observed data accumulates.
12. **Two simultaneous LOCK attempts.** DB-level transaction with row-level lock on `IntraopForm`; loser sees a clear error.
13. **Form opens before OR_ENDED is recorded.** "Switch to post-op" button is disabled with tooltip "OR end time not yet recorded; please record OR end first."
14. **Extraction returns HIGH-confidence value that the surgeon disagrees with on review.** Surgeon edits; both values preserved; extraction confidence-vs-correction rate is tracked in QA telemetry as a tuning signal.
15. **Surgeon attempts to lock with a LOW-confidence unconfirmed field.** Lock blocked; UI lists which fields require confirmation.
16. **Network drops mid-autosave.** Local draft preserved; reconnect retries; user sees "Saving…" → "Offline — draft preserved" indicator.
17. **Reopen → re-lock cycle.** Each lock fires a new reassessment event. The episode tier is re-resolved against the *current* tier (which may include the prior reassessment's effect). Audit chain shows all reassessments.
18. **Pre-op re-tier set the patient to TIER_3 with sticky hard guard, intra-op proposes TIER_1.** `resolveFinalTier(TIER_3, TIER_1) = TIER_3`. Intra-op never downgrades.
19. **Form locked, but post-op signal arrives moments later proposing further upgrade.** Post-op re-tiering operates independently; the tier may move up further. Both events are recorded.
20. **Upload of a non-PDF or PDF >25 MB.** Rejected with 415 / 413; clear inline error.

---

## 14. Build order

1. **Schema + migrations.** `IntraopForm`, `IntraopExtraction`, `IntraOpReassessmentEvent`, new enum values, `Episode` additions.
2. **Zod schema for `IntraopForm`** with origin metadata.
3. **`intraop-delta.ts`** — `computeIntraopDelta`, `resolveFinalTier`, plus `intraop-delta.weights.ts`. Full unit-test coverage of Examples A–G.
4. **`intraop-extractor.ts` interface and `MockIntraopExtractor`.** Stub for prototype.
5. **`LlmIntraopExtractor`.** Production extractor calling the in-repo Claude pathway with structured prompt, JSON schema response, per-field confidence rating.
6. **PDF upload endpoint + object storage wiring.** Multipart, size cap, MIME check, virus scan (existing infra).
7. **Extraction job runner.** Async; writes `IntraopExtraction` row; updates form fields with origin = AUTO_POP_PDF.
8. **`applyIntraopReassessment` orchestration** in `intraop-apply.ts`. Single-transaction LOCK → reassess → episode tier update → status transition.
9. **Form lifecycle endpoints** — create, GET, PATCH (autosave), lock, reopen.
10. **Switch-to-post-op page and form UI** — `IntraopForm`, `UniversalFieldsSection`, `ExtendedFieldsSection`, `ProcedureFamilyExtension`, `PdfUploadDropzone`, `ExtractionStatusPanel`, `LockConfirmModal`, `ConfidencePill`, `OriginIndicator`.
11. **Right-rail live preview.** Calls `/api/triage/preop-retier/compute` analog (`/api/triage/intraop/preview`) to show the proposed tier preview as fields fill.
12. **`intraop-overdue-watcher` cron.** Conservative-default behavior + alert generation (`INTRAOP_FORM_OVERDUE`).
13. **Reassessment history view.** `ReassessmentHistoryTable` listing all `IntraOpReassessmentEvent` rows for an episode in reverse chronological order with reasons and tier deltas.
14. **Tuning config block + APIs.**
15. **Synthetic load test.** 30 episodes across all five procedure families with mixed PDF + manual paths; assert deterministic tier outputs and queue stability.

---

## 15. Acceptance criteria summary

- [ ] "Switch to post-op" button enables exactly when `OR_ENDED` is recorded.
- [ ] Clicking creates the form (if needed) and opens it in ≤ 1.5s.
- [ ] PDF upload of an average operative note returns extracted fields in ≤ 8s P95 with per-field confidence.
- [ ] All 11 required fields lockable from any combination of MANUAL / AUTO_POP_AIMS / AUTO_POP_PDF origins.
- [ ] LOW-confidence fields require explicit confirmation before lock-eligibility.
- [ ] `computeIntraopDelta` matches all 7 worked examples in unit tests.
- [ ] `resolveFinalTier` returns the most conservative of (current, proposed); intra-op never downgrades.
- [ ] Lock fires reassessment, updates episode tier (if changed), and transitions episode to POST_OP atomically.
- [ ] Conservative default fires exactly once per episode at 24h post OR end with no lock.
- [ ] Reopen is admin-only; reopen → re-lock fires a new reassessment event preserving prior in audit.
- [ ] Every reassessment writes a `IntraOpReassessmentEvent` row with form snapshot, reasons, model version, tuning version.
- [ ] Origin metadata is preserved per field, including `originalValue` when surgeon edits an auto-populated value.
- [ ] Tuning swap mid-OR doesn't disrupt in-flight forms; new locks use new tuning version atomically.
- [ ] All UI states meet WCAG 2.1 AA.

---

## 16. References (clinical anchors)

- Bilimoria KY et al., *Development and Evaluation of the Universal ACS NSQIP Surgical Risk Calculator*, J Am Coll Surg, 2013 — basis for the input variable set and outcome predictors that the soft factor list mirrors.
- ACS-NSQIP Operative Risk Variables documentation — EBL, transfusion, vasopressor, hypotension, hypoxia, OR duration as predictors of 30-day complications.
- STS Adult Cardiac Surgery Database documentation — basis for CABG-specific contributors (cross-clamp time, CPB time, mechanical support).
- North American Spine Society guidelines on spinal-fusion intra-operative complications — basis for dural tear and neuromonitoring change as TIER_3 escalators.
- CDC NHSN wound classification (1–4) — basis for the major-bowel contamination-class contributor.
- AAHKS / AAOS perioperative best-practice statements — basis for LEJR intra-operative fracture as a soft contributor.
- AHA / ACC perioperative cardiovascular evaluation guidelines — supports vasopressor and sustained hypotension as risk contributors.

Concrete thresholds in `tuning.json` are encoded from these references and are reviewed quarterly against observed outcomes.

---

## 17. Glossary

- **Switch to post-op** — the surgeon-facing CTA that opens the intra-op form and, after lock, transitions the episode into the post-op phase.
- **Hard upgrade** — single condition that promotes the patient to TIER_3 regardless of other fields.
- **Soft upgrade** — condition that adds one tier step; ≥2 soft upgrades aggregate to TIER_3.
- **Most conservative wins** — `resolveFinalTier` rule: the higher-rank tier of (current, proposed) is the final tier.
- **Conservative default** — system-applied 1-step upgrade when the form is not locked within 24h of OR end.
- **Origin** — per-field metadata recording whether a value was filled MANUAL / AUTO_POP_AIMS / AUTO_POP_PDF, with confidence and confirmation timestamps.
- **Confidence pill** — the HIGH / MED / LOW indicator on each PDF-extracted field; LOW requires explicit confirmation before lock.
- **P90** — 90th percentile (used for procedure OR-time benchmarking).

---

*End of PRD v1.0 — Intra-Op Reassessment*
