# PRD — Post-Op Scoring & Re-Tiering

| Field | Value |
|---|---|
| Feature | Post-Op Scoring (signal sources + tier re-tiering) |
| Document version | 1.0 (final) |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-05-08 |
| Primary user | RN care coordinator (consumes resulting tier in queue, reviews wound photos) |
| Secondary users | Patient (daily check-ins, surveys, med adherence pings, wound photo upload, video, self-flag), NP / PA, Surgeon (read), Clinical Operations Lead (audit) |
| Implementation target | Next.js 14 App Router + TypeScript + Tailwind + shadcn/ui + Prisma/Postgres (PRD style) |
| Audience | Cursor / engineering implementers |
| Depends on | Initial Pre-Op Triage v1.0 (`initial-triage-v1.md`); Pre-Op Re-Tiering v1.0 (`preop-retier-v1.md`); Intra-Op Reassessment v1.0 (`intraop-reassessment-v1.md`); Triage Tracking PRD v0.1 (alert lifecycle, RN queue, priority scoring, patient self-flag — all reused as-is) |
| Supersedes | None — this is the first post-op scoring PRD |

---

## 0. Reading order and conventions

- TIER_3 = highest risk (preserved across all triage PRDs).
- Post-op tier movement is **upward-only**, mirroring Triage Tracking PRD §5.3. Downgrades require explicit RN action with reason and respect a 24h cooldown after a self-flag (existing rule, unchanged).
- The "floor" of post-op tier is the tier in effect immediately after intra-op reassessment. Re-tier rebuilds from that floor + current post-op signal state.
- **Explicitly excluded from this PRD per author guidance:** RPM device readings of any kind, and Care Companion engagement (whether used and what was said). Both have been removed from the signal set defined in the existing Triage Tracking PRD §6; their thresholds and weights are deactivated. A future PRD will restore RPM and Care Companion contributors with revised logic.
- The existing Triage Tracking PRD's alert lifecycle (§7), priority scoring (§8), RN queue (§9), action handlers (§10), autonomous escalation (§11), patient self-flag flow (§12), and audit (§13) are **reused unchanged**. This PRD adds new *signal sources* and a new *post-op re-tier algorithm* that produces alerts and tier changes consumed by those existing systems.

---

## 1. Scope

**In scope.**

1. The seven post-op signal sources called out by the author:
   - Daily symptom check-in (questions, scoring, completion tracking).
   - Day 7 / Day 14 / Day 30 surveys (questions, scoring, completion tracking).
   - Diagnosis / treatment / red-flag video viewing (multi-session counts).
   - Medication adherence ping (response + non-response).
   - Wound photo submission (engagement only; photo content is **not** scored — see §8 for the parallel nurse-review training pipeline).
   - Patient self-flag (referenced; spec lives in Triage Tracking PRD §12, unchanged).
2. The post-op re-tier algorithm: hard escalators, soft contributors, idempotent recompute from the post-intra-op floor.
3. Cadence: synchronous on every signal commit, nightly batch at 02:00 local, and at D7 / D14 / D30 checkpoint crons.
4. Wound photo nurse-review pipeline: structured RN judgment (problematic / unproblematic + reason) writing into a labeled training-data store. Photo content is not used for tiering in v1.
5. Persisted episode state, audit events, API contracts, file structure, edge cases, build order.

**Out of scope.**

- Anything RPM-related (vitals stream, missed-readings alerts, vitals-derived signals).
- Anything Care-Companion-related (usage tracking, transcript ingestion, conversation-based signals).
- The alert lifecycle, queue, action handlers, priority scoring (existing in Triage Tracking PRD).
- Telehealth visit booking flow (Triage Tracking PRD §10.2).
- Discharge from monitoring at D30 (separate workflow).

---

## 2. Why this exists (1 paragraph)

The post-op window is where outcomes are made or lost. The 30 days following discharge contain the readmissions, the missed infections, the silent decompensations, and the engagement collapses that the entire TEAM model is designed to prevent. The system needs a tier that updates as those signals arrive — not a static label set at discharge — and the signals that update it must reflect what the patient is actually doing and feeling at home, not what the chart said when they walked in. This PRD specifies the seven signal sources we are using (excluding RPM and Care Companion in v1 per explicit author guidance), how each is captured, scored, and committed, and how they feed an idempotent tier-recompute that respects the post-intra-op floor and feeds the existing alert/queue/escalation pipeline.

---

## 3. Signal sources (overview)

| # | Source | Section | Used by tier? | Used by alerts? |
|---|---|---|---|---|
| 1 | Daily symptom check-in (content + completion) | §4 | Yes | Yes |
| 2 | Day 7 / 14 / 30 surveys (content + completion) | §5 | Yes | Yes |
| 3 | Diagnosis/treatment/red-flag video views | §6 | Yes (engagement) | No |
| 4 | Medication adherence ping (response + non-response) | §7 | Yes | Yes (low adherence) |
| 5 | Wound photo submission (binary only) | §8 | Yes (engagement only) | Yes (lost-engagement) |
| 6 | Wound photo content via nurse review | §8 | **No (v1)** | No (training data only) |
| 7 | Patient self-flag | §9 | Yes (hard escalator → TIER_3) | Yes |

The existing Triage Tracking PRD §11 autonomous escalations (stale alerts, lost-contact-Tier3, after-hours rules) continue to operate against these signals exactly as specified.

---

## 4. Daily symptom check-in

Sent every 24 hours from D1 through D30 inclusive (30 sends per episode). Delivery via the existing patient-app push + SMS fallback channels.

### 4.1 Questions (10 items)

The check-in is intentionally short to maximize completion. Items 5, 8, and 10 trigger structured red-flag handling.

1. **Pain right now (0–10 NRS).** Numeric input.
2. **Compared to yesterday, your pain is:** `<RadioGroup>` Better / Same / Worse.
3. **Have you had a fever (≥100.4°F / 38.0°C) in the last 24 hours?** `<RadioGroup>` Yes (measured) / Yes (felt feverish, didn't measure) / No.
4. **Has your incision changed in the last 24 hours?** `<RadioGroup>` Looks the same / Looks better / Looks worse.
5. **Are any of these true about your incision today?** `<MultiSelect>` New redness spreading / New drainage (any color) / Opening or gaping / Bad smell / Increased pain at incision / None of these.
6. **Nausea or vomiting in the last 24 hours?** `<RadioGroup>` None / Mild (no vomiting) / Moderate / Severe (multiple episodes).
7. **Are you eating and drinking close to normally?** `<RadioGroup>` Yes / Some / Almost nothing.
8. **Have you experienced any of these in the last 24 hours?** `<MultiSelect>` Chest pain / Sudden trouble breathing / Sudden weakness on one side / Severe or new bleeding / Confusion or sudden mental change / Calf swelling, redness, or pain in one leg / Severe headache / Fainting or near-fainting / None of these.
9. **Did you walk around today as your team instructed?** `<RadioGroup>` Yes / Some / No.
10. **How worried are you about your recovery today?** `<RadioGroup>` Not at all / A little / Moderately / Very / Extremely.

A free-text "anything else?" field is always present, optional. Submission requires answers to items 1–10 (multi-selects can be "None of these").

### 4.2 Scoring

```ts
// /lib/triage/postop/daily-checkin-scoring.ts
export interface DailyCheckinScored {
  rawTotal: number;                   // 0..100
  tier: 'GREEN' | 'ORANGE' | 'RED';
  redFlags: string[];                 // surfaced from items 5, 8 only
  newRedFlagSymptom: boolean;         // item 8 hit
  woundConcern: boolean;              // item 5 hit
  painNrs: number;                    // item 1
  painTrajectory: 'BETTER' | 'SAME' | 'WORSE';
  itemScores: Record<string, number>;
}
```

Per-item scoring is configurable in `tuning.json`. Default mapping:

| Item | Output | Default weight |
|---|---|---|
| 1. Pain NRS | 100 − (NRS × 10) → 0..100 | 20% of total |
| 2. Pain trajectory | Better=100 / Same=70 / Worse=20 | 10% |
| 3. Fever | No=100 / Yes(felt)=40 / Yes(measured)=0 | 15% |
| 4. Incision change | Better=100 / Same=85 / Worse=10 | 5% (item 5 dominates wound) |
| 5. Incision flags | None=100 / any single chip=20 / multiple=0 | 15% |
| 6. Nausea | None=100 / Mild=70 / Moderate=40 / Severe=10 | 5% |
| 7. Eating/drinking | Yes=100 / Some=60 / Almost nothing=20 | 5% |
| 8. Red-flag symptoms | None=100 / any single chip=0 (and triggers `NEW_RED_FLAG_SYMPTOM` event) | 15% |
| 9. Walking | Yes=100 / Some=60 / No=20 | 5% |
| 10. Worry level | Not at all=100 / A little=80 / Moderately=50 / Very=20 / Extremely=0 | 5% |

`rawTotal` = weighted sum. Tier mapping (defaults; tunable):

| Tier | Threshold | Behavior |
|---|---|---|
| GREEN | rawTotal ≥ 85 AND no red-flag chips on items 5 or 8 | No alert raised; normal cadence |
| ORANGE | 70 ≤ rawTotal < 85, OR any single item-5 chip without item-8 hits | Alert at OPEN priority per existing alert weights |
| RED | rawTotal < 70 OR any item-8 hit OR multiple item-5 chips | High-priority alert; potential hard escalator (see §10) |

Three special outputs are emitted *regardless* of tier:

- `NEW_RED_FLAG_SYMPTOM` event (item 8 hit) → triggers existing Triage Tracking PRD §6 auto-call + page rule.
- `WOUND_CONCERN` event (item 5 hit) → consumed by post-op re-tier (§10).
- `PAIN_TRAJECTORY_ABNORMAL` event (item 2 = Worse AND item 1 ≥ expected curve threshold for episode-day) → consumed by re-tier and existing alert pipeline.

### 4.3 Completion tracking

Each send creates a `DailyCheckinSend` row. Each submission creates a `DailyCheckinResponse` row. A check-in is considered "completed" if a response is recorded within 36 hours of send (gives patient until next send + 12h grace).

- Missed: response not received within 36h → mark `completed=false`, surface as a soft contributor in re-tier.
- Consecutive misses: tracked in `Episode.dailyCheckinMissedStreak` for cron-driven escalation (see §10 hard escalators).

### 4.4 Acceptance criteria

- **AC-4.1** Send fires every 24h between D1 and D30; idempotent if process restarts.
- **AC-4.2** Submission with items 1–10 produces a deterministic `DailyCheckinScored` payload.
- **AC-4.3** Item 8 hit produces a `NEW_RED_FLAG_SYMPTOM` event regardless of total score.
- **AC-4.4** Item 5 hit produces a `WOUND_CONCERN` event regardless of total score.
- **AC-4.5** Missed check-ins (no response in 36h) increment a per-episode missed streak.

---

## 5. Day 7 / Day 14 / Day 30 surveys

Distinct from the daily check-in. Longer, focused on trajectory and recovery milestones. Sent on each respective episode day at 09:00 local; window closes at 48h (longer window because surveys are heavier; missing them is a stronger signal of disengagement).

### 5.1 Common structure

Each survey is grouped into four sections. Section content adapts by procedure family where indicated.

| Section | Contents |
|---|---|
| A. Pain & symptoms | NRS, pain interference (4 PROMIS-aligned items), continued red-flag screen |
| B. Function | Procedure-specific PROM (KOOS Jr / HOOS Jr for LEJR; ODI for spinal; abridged DASH for upper extremity if applicable; SF-12 PCS otherwise) — abridged 5–8 items |
| C. Engagement & adherence | Medication adherence (8-item Morisky-style); PT/exercise adherence; appointments attended |
| D. Recovery confidence | Single 0–10 readiness item ("How well do you feel your recovery is going right now?") + free-text comment |

Each section emits a 0–100 sub-score; the survey emits a weighted total and a tier (GREEN / ORANGE / RED).

### 5.2 Day-specific emphasis

| Day | Focus | Default weights (A/B/C/D) | Tier thresholds (G/O/R) |
|---|---|---|---|
| Day 7 | Acute recovery; infection vigilance still high | A:40 / B:20 / C:25 / D:15 | G ≥85 / O 70–84 / R <70 |
| Day 14 | Function returning; medication taper begins | A:30 / B:35 / C:20 / D:15 | G ≥85 / O 72–84 / R <72 |
| Day 30 | Return-to-baseline; discharge from monitoring | A:20 / B:45 / C:15 / D:20 | G ≥80 / O 65–79 / R <65 |

Day 30 is more lenient on the GREEN threshold because most patients have residual symptoms at 30 days and we don't want to penalize normal-curve recovery.

### 5.3 Red-flag passthrough

Item-level red-flag triggers in Section A (any chest pain, severe SOB, calf pain with swelling, etc.) propagate as `NEW_RED_FLAG_SYMPTOM` events identically to daily check-in §4.2.

### 5.4 Completion tracking

- Window: 48h from send.
- Missed (no submission within 48h): emit `SURVEY_DAY_X_MISSED` event; close the window; the next survey send proceeds on its schedule.
- Late submissions (after window close but before discharge from monitoring): accepted but tier not retroactively updated for the missed-window penalty already applied in re-tier.

### 5.5 Acceptance criteria

- **AC-5.1** Each survey is sent at the designated day at 09:00 local with a 48h response window.
- **AC-5.2** Submission produces a deterministic per-section score, total score, and tier.
- **AC-5.3** Procedure-family-specific Section B items load correctly per `episode.anchorProcedureFamily`.
- **AC-5.4** Item-level red flags propagate as `NEW_RED_FLAG_SYMPTOM` events.
- **AC-5.5** Missed window emits `SURVEY_DAY_X_MISSED` exactly once.

---

## 6. Diagnosis / treatment / red-flag video

Two videos delivered post-op:

1. **Diagnosis & treatment video** — explains what was done, the expected recovery curve, and what "normal" looks like. Recommended viewing window: D1–D5.
2. **Red-flag video** — explains warning signs that warrant urgent contact (chest pain, SOB, calf swelling, wound changes, fever, mental status change). Recommended viewing window: D1–D2 (early viewing matters most).

Both videos are stored alongside the existing pre-op video infrastructure in the patient app.

### 6.1 Multi-session view tracking

Mirroring the pre-op re-tier engagement spec:

- Emit `postop_video_played` on every distinct play session (≥60s gap between events defines a new session).
- Emit `postop_video_completed` when playback reaches ≥90% of duration.
- Persist `{ video_kind: 'DIAGNOSIS_TREATMENT' | 'RED_FLAG', source: 'postop_page', session_id, playback_position, playback_rate, duration }`.
- Reader helpers: `countPostopVideoSessions(patientId, kind)`, `lastPostopVideoSessionAt(patientId, kind)`.

### 6.2 Use in re-tier

Engagement contributors (tunable in §15):

- `RED_FLAG_VIDEO_VIEWED_BY_D2`: −2 (reward for early viewing of the most safety-critical video)
- `RED_FLAG_VIDEO_NOT_VIEWED_BY_D5`: +2
- `DIAGNOSIS_TREATMENT_VIDEO_VIEWED_BY_D5`: −1
- `DIAGNOSIS_TREATMENT_VIDEO_VIEWED_3_OR_MORE_BY_D14`: −1 (additional)
- `DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14`: +1

### 6.3 Acceptance criteria

- **AC-6.1** Multi-session events deduplicate across rapid scrubs but separate at ≥60s gaps.
- **AC-6.2** Each video emits a single `postop_video_completed` per session at ≥90%.
- **AC-6.3** View-count helpers return correct counts under concurrent inserts.
- **AC-6.4** Re-tier reads contributor flags against current timestamps relative to `episode.dischargeAt`.

---

## 7. Medication adherence ping

Daily push at 19:00 local from D1 through D30: "Did you take your medications today as prescribed?" Four response options:

| Option | Interpretation |
|---|---|
| Yes | Reported full adherence |
| Partial | Took some but not all / missed a dose |
| No | Did not take as prescribed |
| Reply later | Defers; if no reply by 23:00 local, treated as non-response |

### 7.1 Capture

`MedAdherencePing` and `MedAdherenceResponse` rows. A ping is "responded" if any response is recorded by 23:00 local on the send day (4-hour window). After that, the ping is `MISSED_NON_RESPONSE`.

### 7.2 Re-tier contributors

Computed over a rolling 7-day window for stability:

- `MED_ADHERENCE_HIGH`: ≥6 of last 7 days = Yes → −1 (engagement reward)
- `MED_ADHERENCE_LOW`: <5 of last 7 days = Yes (combining Partial/No/non-response as "not Yes") → +2
- `MED_ADHERENCE_NON_RESPONSE_STREAK`: ≥3 consecutive days of non-response → +2 (additional, signals disengagement specifically)

These contributors collapse the existing Triage Tracking PRD §6 `MED_ADHERENCE_LOW` signal type with a more nuanced 7-day rolling rule.

### 7.3 Alert generation

Independently of re-tier, the existing Triage Tracking alert pipeline still raises an `ALERT_RAISED` of reason `MED_ADHERENCE_LOW` when the threshold trips at the patient's current tier. This PRD does not change that flow; it only writes the underlying signal events the alert pipeline reads.

### 7.4 Acceptance criteria

- **AC-7.1** Daily ping fires at 19:00 local on every day from D1–D30.
- **AC-7.2** Each response stamps a row; non-response is a row with `MISSED_NON_RESPONSE` after 23:00 local.
- **AC-7.3** Rolling 7-day adherence calculation is correct under DST transitions and timezone changes.
- **AC-7.4** Alert pipeline raises `MED_ADHERENCE_LOW` per existing Triage Tracking PRD weights.

---

## 8. Wound photo (dual-purpose pipeline)

Per author guidance, wound photo content is **not used in tiering or alerting in v1**. We track only whether the patient submitted a photo. In parallel, we build the data plumbing that lets nurses label submitted photos so that a future PRD can incorporate wound state into tiering with real training data.

### 8.1 Patient-side: photo upload

- A "Wound photos" surface in the patient app accepts JPEG / HEIC / PNG up to 12 MB.
- Each submission writes a `WoundPhoto` row with `patientId`, `episodeId`, `photoBlobUrl`, `submittedAt`, and an optional patient note.
- Submission emits a `WOUND_PHOTO_SUBMITTED` event consumed by:
  - The post-op re-tier as an engagement signal.
  - The RN queue, which surfaces the new photo on the patient detail page (Triage Tracking PRD §9.4 Column B; we add a "new photo, awaiting review" badge).

### 8.2 Re-tier contributors (engagement only)

- `WOUND_PHOTO_SUBMITTED_BY_D5`: −1 (engagement reward, early)
- `WOUND_PHOTO_SUBMITTED_BY_D10`: −1 (additional)
- `WOUND_PHOTO_NOT_SUBMITTED_BY_D7`: +1
- `WOUND_PHOTO_NOT_SUBMITTED_BY_D14`: +2

These weights value early photo submission as engagement evidence; they do **not** read the photo's content.

### 8.3 Nurse review pipeline (training data)

When an RN opens a wound photo on the patient detail page, a structured review form is presented adjacent to the image:

| Field | Component | Required |
|---|---|---|
| Is this wound photo problematic? | `<RadioGroup>` Yes / No / Unable to assess | Yes |
| If Yes, what is the concern? | `<MultiSelect>` Erythema spreading / Drainage (purulent) / Drainage (serosanguinous excessive) / Dehiscence / Approximation loss / Hematoma / Eschar / Necrosis / Surrounding skin changes / Other | Conditional on Yes |
| If "Unable to assess," why? | `<MultiSelect>` Photo blurry / Lighting poor / Wound not visible / Cropping incorrect / Other | Conditional on Unable |
| Severity (when Yes) | `<RadioGroup>` Low / Medium / High | Conditional on Yes |
| Action taken | `<MultiSelect>` Patient called / Visit scheduled / Surgeon escalated / Asked patient to retake / No action needed | Yes |
| Free-text explanation | `<Textarea>` (min 30 chars) | Yes |
| Confidence in assessment | `<Slider>` 0–100 | Yes |

On submit, a `WoundPhotoReview` row is written with all fields, the reviewing RN's `userId`, and a snapshot of the patient's clinical context at review time (procedure family, days post-op, current tier, last vitals if available). The original photo, the structured judgment, and the explanation form a labeled training record.

### 8.4 Training-data store

- `WoundPhotoReview` rows are the labeled dataset.
- A nightly export job writes a redacted, de-identified parquet snapshot of the dataset to a designated bucket (`wound-photo-training/<yyyy-mm-dd>.parquet`) with patient identifiers replaced by stable salted hashes.
- Photo blobs are referenced by URL; the export does not copy photo binaries (they live in the existing object store).
- The export job is opt-in per institution and gated by Clinical Operations Lead approval.

### 8.5 What v2 will look like (informational, not in scope)

A future PRD will introduce a wound-state classifier trained on this dataset. When that lands:
- A new signal `WOUND_CLASSIFIER_OUTPUT` will emit alongside `WOUND_PHOTO_SUBMITTED`.
- Tiering contributors will be added that use the classifier's output (high-confidence problematic photo → hard escalator → TIER_3, etc.).
- Until then, this PRD's pipeline produces the labeled data. No model is deployed in v1.

### 8.6 Acceptance criteria

- **AC-8.1** Patient submits a JPEG/HEIC/PNG up to 12 MB; row + event written within 3s.
- **AC-8.2** Re-tier reads only the binary "submitted" signal; no field references photo content.
- **AC-8.3** RN review form requires Yes/No/Unable, severity (when Yes), action, explanation ≥30 chars.
- **AC-8.4** Each review writes a `WoundPhotoReview` row with reviewing RN, timestamp, full structured payload, and clinical context snapshot.
- **AC-8.5** Nightly redacted export writes parquet successfully or surfaces a clear failure to the data ops queue.
- **AC-8.6** Photo blobs are never duplicated into the training export; URLs only.

---

## 9. Patient self-flag

Spec is owned by the existing Triage Tracking PRD §12. This PRD does not modify it. The relevant facts for post-op scoring:

- Self-flag creates an alert with reason `PATIENT_SELF_FLAG`, weight 100 (Triage Tracking §8 weights table).
- Self-flag triggers immediate hard upgrade to TIER_3 (Triage Tracking §5.2 hard upgrade triggers).
- 24h cooldown + completed call/visit prerequisite to downgrade (Triage Tracking §5.4).

The post-op re-tier algorithm (§10) defers to the existing rule: any active self-flag → TIER_3.

---

## 10. The post-op re-tier algorithm

### 10.1 Idempotent recompute, post-intra-op floor

Like the pre-op re-tier, post-op re-tier rebuilds `Episode.tier` from a floor + current signal state on every call. The floor is the post-intra-op tier — the value of `Episode.tier` immediately after `applyIntraopReassessment` ran, snapshotted in `Episode.postIntraOpTier` for cheap lookup.

```ts
// /lib/triage/postop/postop-retier.ts
export function reTierPostOp(state: PostOpReTierInput): PostOpReTierResult {
  const reasons: ReTierReason[] = [];

  // Stage 0 — hard escalators (any one → TIER_3)
  for (const hard of evaluatePostOpHardEscalators(state)) {
    reasons.push(hard);
    return finalize(state.postIntraOpTier, 'TIER_3', reasons, state);
  }

  // Stage 1 — sum unsigned soft delta (post-op is upward-only)
  const delta = computePostOpDelta(state, reasons);

  // Stage 2 — apply delta to floor
  const targetTier = applyDeltaUpwardOnly(state.postIntraOpTier, delta);

  return finalize(state.postIntraOpTier, targetTier, reasons, state);
}
```

### 10.2 Hard escalators (any one → TIER_3)

| Hard escalator | Source | Definition |
|---|---|---|
| `PATIENT_SELF_FLAG_ACTIVE` | Self-flag flow | Any unresolved self-flag (per Triage Tracking §12) |
| `NEW_RED_FLAG_SYMPTOM` | Daily check-in item 8, or any survey Section A red-flag | Any chest pain, severe SOB, sudden weakness, severe bleeding, mental status change, calf swelling/redness, fainting, severe headache |
| `LOST_CONTACT_TIER3` | Computed | Tier-3 patient with zero responses (check-in, ping, photo, survey) for 24h consecutive — same as Triage Tracking §11 |
| `LOST_CONTACT_GENERAL` | Computed | Any patient with zero responses for 72h consecutive (newly added) |
| `DAY_X_SURVEY_RED_AND_RED_FLAG` | D7 or D14 survey | Survey total tier RED AND any item-level red-flag chip (compounded — total alone is soft, total+red-flag is hard) |
| `MULTIPLE_INCISION_FLAGS` | Daily check-in item 5 | ≥2 chips on the same submission OR any single chip on 3 consecutive days |

### 10.3 Soft contributors (each adds points; total maps to tier upgrade)

Post-op uses an unsigned positive-only delta — all contributors push upward. The floor cannot be undercut.

```ts
// /lib/triage/postop/postop-retier.weights.ts
export const POSTOP_WEIGHTS = {
  // Daily check-in
  CHECKIN_TIER_RED:                          +3,   // per day
  CHECKIN_TIER_ORANGE:                       +1,
  CHECKIN_MISSED:                            +1,   // per day, capped at 7-day window contribution of +5
  CHECKIN_MISSED_STREAK_3:                   +2,   // additional, when streak ≥3
  WOUND_CONCERN_FROM_CHECKIN:                +2,   // item 5 single chip
  PAIN_TRAJECTORY_WORSE:                     +1,   // item 2 = Worse and item 1 above expected curve

  // Day surveys
  SURVEY_DAY_7_RED:                          +3,
  SURVEY_DAY_7_ORANGE:                       +1,
  SURVEY_DAY_7_MISSED:                       +2,
  SURVEY_DAY_14_RED:                         +3,
  SURVEY_DAY_14_ORANGE:                      +1,
  SURVEY_DAY_14_MISSED:                      +2,
  SURVEY_DAY_30_RED:                         +2,   // less weight late in episode
  SURVEY_DAY_30_ORANGE:                      +1,
  SURVEY_DAY_30_MISSED:                      +1,

  // Engagement — videos
  RED_FLAG_VIDEO_VIEWED_BY_D2:               -2,   // signed; capped to 0 in unsigned sum
  RED_FLAG_VIDEO_NOT_VIEWED_BY_D5:           +2,
  DIAGNOSIS_TREATMENT_VIDEO_VIEWED_BY_D5:    -1,
  DIAGNOSIS_TREATMENT_VIDEO_VIEWED_3_PLUS_BY_D14: -1,
  DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14: +1,

  // Med adherence (rolling 7-day)
  MED_ADHERENCE_HIGH:                        -1,
  MED_ADHERENCE_LOW:                         +2,
  MED_ADHERENCE_NON_RESPONSE_STREAK_3:       +2,

  // Wound photo (engagement only)
  WOUND_PHOTO_SUBMITTED_BY_D5:               -1,
  WOUND_PHOTO_SUBMITTED_BY_D10:              -1,
  WOUND_PHOTO_NOT_SUBMITTED_BY_D7:           +1,
  WOUND_PHOTO_NOT_SUBMITTED_BY_D14:          +2,
} as const;
```

The "negative" weights above (engagement rewards) are tracked in the algorithm but **clamped to 0** in the post-op delta (post-op cannot downgrade below the floor). They appear in the audit log so the team can see how positive engagement is offsetting negative contributors, but they cannot, by themselves, drop the tier.

### 10.4 Delta → tier mapping

```ts
// /lib/triage/postop/postop-retier.mapping.ts
export function applyDeltaUpwardOnly(floor: Tier, delta: number): Tier {
  if (delta >= 6)  return upgradeBy(floor, 2);
  if (delta >= 3)  return upgradeBy(floor, 1);
  return floor;
}
```

Symmetric thresholds with pre-op re-tier (≥+3 = +1 step, ≥+6 = +2 steps), but no downgrade arm.

### 10.5 Worked examples

**A. Floor T1, clean recovery.** D7 green, D14 green, all daily check-ins green, both videos viewed, med adherence high, wound photo submitted by D5. Negative weights present but clamped. delta = 0. Tier = T1.

**B. Floor T1, missed engagement.** D7 missed (+2), D14 orange (+1), 4 daily check-ins missed (+4 capped at +5), red-flag video not viewed by D5 (+2), wound photo not submitted by D7 (+1). Total delta ≈ +10 (capped). Mapping: ≥+6 → upgrade by 2 → TIER_3.

**C. Floor T2, item 5 single chip.** Day 5 daily check-in shows "new redness spreading" (+2 from `WOUND_CONCERN_FROM_CHECKIN`). delta = +2. Mapping: <+3 → no change. Tier stays T2. The wound concern still raises an *alert* via the existing Triage Tracking pipeline; tier just doesn't move yet.

**D. Floor T1, hard escalator from compounded item-5.** Three consecutive daily check-ins each show "new redness spreading." Hard escalator `MULTIPLE_INCISION_FLAGS` (≥1 chip on 3 consecutive days) → TIER_3 regardless of soft delta.

**E. Floor T3, bad recovery.** Already at T3; soft delta cannot push higher. Hard escalators may fire but tier stays T3.

**F. Floor T1, lost contact 72h.** No responses for 72h consecutive → `LOST_CONTACT_GENERAL` hard escalator → TIER_3.

**G. Floor T1, self-flag at D6.** `PATIENT_SELF_FLAG_ACTIVE` hard escalator → TIER_3 (also drives existing alert pipeline). On resolution + cooldown, RN can downgrade per Triage Tracking §5.4 (this is the only post-op downgrade path; algorithm itself never downgrades).

These seven cases are encoded as fixtures in `/lib/triage/postop/__tests__/postop-retier.fixtures.ts`.

### 10.6 Cadence

- **Synchronously on every signal commit** — daily check-in submission, survey submission, video event, med adherence response or non-response, wound photo submission, self-flag.
- **Nightly batch** at 02:00 local — every active episode; catches missed signals (missed check-in, missed survey window, lost-contact computations).
- **Day checkpoints** — D1 (initial post-op floor confirm), D7, D14, D30 — extra cron run to ensure survey-related contributors register exactly at boundary.
- **On RN action** — when an RN resolves an alert or downgrades tier, re-tier recomputes synchronously.

---

## 11. Integration with existing alert / queue / priority systems

This PRD does not modify Triage Tracking PRD §6–§13. It only:

- Removes RPM-derived signals from the active set in `tuning.json` (set `enabled: false`; entries preserved for v2 restoration).
- Removes Care-Companion-derived signals likewise.
- Adds new signal types: `DAILY_CHECKIN_RED`, `DAILY_CHECKIN_ORANGE`, `DAILY_CHECKIN_MISSED`, `WOUND_CONCERN`, `WOUND_PHOTO_SUBMITTED`, `MED_ADHERENCE_LOW` (already existed; semantics updated to 7-day rolling), `MED_ADHERENCE_NON_RESPONSE_STREAK`, `SURVEY_DAY_X_RED`, `SURVEY_DAY_X_ORANGE`, `SURVEY_DAY_X_MISSED`, `LOST_CONTACT_GENERAL`, `MULTIPLE_INCISION_FLAGS`.
- Each new signal type has a default weight in the existing Triage Tracking PRD §8 priority scoring formula (defaults below; tunable).

```ts
// Additions to Triage Tracking PRD §8 WEIGHTS map
const POSTOP_ALERT_WEIGHTS: Partial<Record<AlertReason, number>> = {
  DAILY_CHECKIN_RED:                  60,
  DAILY_CHECKIN_ORANGE:               25,
  DAILY_CHECKIN_MISSED:               15,
  WOUND_CONCERN:                      45,
  MULTIPLE_INCISION_FLAGS:            85,
  SURVEY_DAY_7_RED:                   55,
  SURVEY_DAY_14_RED:                  50,
  SURVEY_DAY_30_RED:                  35,
  SURVEY_DAY_X_MISSED:                20,
  MED_ADHERENCE_NON_RESPONSE_STREAK:  30,
  LOST_CONTACT_GENERAL:               75,
};
```

Self-flag and `NEW_RED_FLAG_SYMPTOM` retain their existing weights of 100 from Triage Tracking PRD.

---

## 12. UI surfaces

### 12.1 Patient app

- **Daily check-in card** — appears at the top of the patient home from D1–D30; submission disappears the card until next day.
- **Survey card** — appears on D7 / D14 / D30 home screens; tapping launches the survey.
- **Diagnosis & treatment video** + **Red-flag video** — surfaced on home screen with "Watch now" CTAs; both have replay affordances.
- **Medication adherence ping** — push notification at 19:00; in-app card with the four options also visible all day.
- **Wound photo upload** — dedicated section accessible from home; instructions + camera prompt; submission confirmation.
- **Self-flag** — already-existing always-visible "Something doesn't feel right" button (Triage Tracking §12).

The patient app **never** shows tier or score values.

### 12.2 RN coordinator queue

Queue tier card adds:

- A small "post-op re-tiered" indicator when current tier > `postIntraOpTier`.
- A new Column B (signals & evidence) section: most recent daily check-in summary, last submitted wound photo thumbnail with a "Review" CTA, current 7-day med-adherence rate.
- Wound photo review form (§8.3) opens inline next to the image.

### 12.3 Acceptance criteria

- **AC-12.1** Patient surfaces never display tier or score values.
- **AC-12.2** Daily check-in card disappears after submission and reappears next day.
- **AC-12.3** Wound photo "Review" CTA opens the structured review form within 1.5s.
- **AC-12.4** Survey cards launch the procedure-family-correct Section B.
- **AC-12.5** All UI states meet WCAG 2.1 AA.

---

## 13. Data model (Prisma)

```prisma
model Episode {
  // ... existing
  postIntraOpTier            Tier?      // floor for post-op re-tier
  dailyCheckinMissedStreak   Int        @default(0)
  postOpReTierLastRunAt      DateTime?
  postOpReTierLastDelta      Int?
  postOpReTierTopReasons     Json?
  postOpReTierVersion        String?
  postOpReTierTuningVersion  Int?

  dailyCheckinSends          DailyCheckinSend[]
  dailyCheckinResponses      DailyCheckinResponse[]
  dayXSurveys                DayXSurvey[]
  medAdherencePings          MedAdherencePing[]
  medAdherenceResponses      MedAdherenceResponse[]
  woundPhotos                WoundPhoto[]
  woundPhotoReviews          WoundPhotoReview[]
  postOpVideoEvents          PostOpVideoEvent[]
  postOpReTierEvents         PostOpReTierEvent[]
}

model DailyCheckinSend {
  id          String   @id @default(cuid())
  episodeId   String
  episodeDay  Int
  sentAt      DateTime
  channel     String   // 'PUSH' | 'SMS'
  episode     Episode  @relation(fields: [episodeId], references: [id])
  @@index([episodeId, episodeDay])
}

model DailyCheckinResponse {
  id            String   @id @default(cuid())
  episodeId     String
  episodeDay    Int
  submittedAt   DateTime
  answers       Json     // 10 items + free text
  rawTotal      Float
  tier          String   // 'GREEN' | 'ORANGE' | 'RED'
  redFlags      Json     // string[]
  newRedFlag    Boolean
  woundConcern  Boolean
  painNrs       Int
  painTrajectory String  // 'BETTER' | 'SAME' | 'WORSE'
  itemScores    Json
  episode       Episode  @relation(fields: [episodeId], references: [id])
  @@index([episodeId, episodeDay])
}

model DayXSurvey {
  id          String   @id @default(cuid())
  episodeId   String
  day         Int                       // 7 | 14 | 30
  sentAt      DateTime
  submittedAt DateTime?
  status      String                    // 'PENDING' | 'COMPLETED' | 'MISSED'
  sectionScores Json?                   // {A,B,C,D}
  totalScore  Float?
  tier        String?                   // 'GREEN' | 'ORANGE' | 'RED'
  redFlags    Json?
  rawAnswers  Json?
  episode     Episode  @relation(fields: [episodeId], references: [id])
  @@unique([episodeId, day])
}

model PostOpVideoEvent {
  id           String   @id @default(cuid())
  episodeId    String
  videoKind    String                   // 'DIAGNOSIS_TREATMENT' | 'RED_FLAG'
  eventType    String                   // 'PLAYED' | 'COMPLETED'
  sessionId    String
  occurredAt   DateTime
  payload      Json
  episode      Episode  @relation(fields: [episodeId], references: [id])
  @@index([episodeId, videoKind, occurredAt])
}

model MedAdherencePing {
  id          String   @id @default(cuid())
  episodeId   String
  episodeDay  Int
  sentAt      DateTime
  episode     Episode  @relation(fields: [episodeId], references: [id])
  @@index([episodeId, episodeDay])
}

model MedAdherenceResponse {
  id          String   @id @default(cuid())
  episodeId   String
  episodeDay  Int
  respondedAt DateTime?
  response    String                    // 'YES' | 'PARTIAL' | 'NO' | 'REPLY_LATER' | 'MISSED_NON_RESPONSE'
  episode     Episode  @relation(fields: [episodeId], references: [id])
  @@unique([episodeId, episodeDay])
}

model WoundPhoto {
  id            String   @id @default(cuid())
  episodeId     String
  patientId     String
  photoBlobUrl  String
  patientNote   String?
  submittedAt   DateTime @default(now())
  episode       Episode  @relation(fields: [episodeId], references: [id])
  reviews       WoundPhotoReview[]
  @@index([episodeId, submittedAt])
}

model WoundPhotoReview {
  id                String   @id @default(cuid())
  woundPhotoId      String
  reviewedBy        String                   // userId
  reviewedAt        DateTime @default(now())
  isProblematic     String                   // 'YES' | 'NO' | 'UNABLE_TO_ASSESS'
  concernTypes      Json?                    // when YES
  unableReasons     Json?                    // when UNABLE
  severity          String?                  // 'LOW' | 'MEDIUM' | 'HIGH', when YES
  actionTaken       Json                     // string[]
  explanation       String                   // ≥30 chars
  confidence        Int                      // 0..100
  clinicalContext   Json                     // snapshot at review time
  woundPhoto        WoundPhoto @relation(fields: [woundPhotoId], references: [id])
  @@index([reviewedAt])
}

model PostOpReTierEvent {
  id                  String   @id @default(cuid())
  episodeId           String
  triggeredBy         String                  // 'SIGNAL:<type>' | 'CHECKPOINT:<dayX>' | 'NIGHTLY' | 'MANUAL:<userId>'
  inputsSnapshot      Json
  postIntraOpTier     Tier
  computedDelta       Int
  computedTier        Tier
  tierBefore          Tier
  tierAfter           Tier
  changed             Boolean
  reasons             Json
  modelVersion        String
  tuningVersion       Int
  createdAt           DateTime @default(now())
  episode             Episode  @relation(fields: [episodeId], references: [id])
  @@index([episodeId, createdAt])
}

// Triage event additions (reused enum)
enum TriageEventType {
  // ... existing
  POSTOP_DAILY_CHECKIN_SUBMITTED
  POSTOP_DAILY_CHECKIN_MISSED
  POSTOP_SURVEY_SUBMITTED
  POSTOP_SURVEY_MISSED
  POSTOP_VIDEO_VIEW
  POSTOP_MED_ADHERENCE_RESPONSE
  POSTOP_MED_ADHERENCE_NON_RESPONSE
  POSTOP_WOUND_PHOTO_SUBMITTED
  POSTOP_WOUND_PHOTO_REVIEWED
  POSTOP_RETIER_RECOMPUTED_NO_CHANGE
  POSTOP_RETIER_TIER_UPDATED
  POSTOP_RETIER_HARD_ESCALATOR_FIRED
}

// AlertReason additions (reused enum)
enum AlertReason {
  // ... existing
  DAILY_CHECKIN_RED
  DAILY_CHECKIN_ORANGE
  DAILY_CHECKIN_MISSED
  WOUND_CONCERN
  MULTIPLE_INCISION_FLAGS
  SURVEY_DAY_7_RED
  SURVEY_DAY_14_RED
  SURVEY_DAY_30_RED
  SURVEY_DAY_X_MISSED
  MED_ADHERENCE_NON_RESPONSE_STREAK
  LOST_CONTACT_GENERAL
}
```

---

## 14. API contracts

### 14.1 Daily check-in

- **`POST /api/episodes/:episodeId/daily-checkin`** — submit response. Request: `{ answers: DailyCheckinAnswers }`. Response 201: `{ scored: DailyCheckinScored, alertsRaised: AlertId[], retierTriggered: boolean }`.
- **`GET /api/episodes/:episodeId/daily-checkin/today`** — fetch today's send + response (or null).

### 14.2 Day surveys

- **`POST /api/episodes/:episodeId/day-survey/:day`** — submit (`day ∈ {7,14,30}`).
- **`GET /api/episodes/:episodeId/day-survey/:day`** — fetch.

### 14.3 Med adherence ping

- **`POST /api/episodes/:episodeId/med-adherence/today`** — submit response or non-response trigger from cron.

### 14.4 Wound photo

- **`POST /api/episodes/:episodeId/wound-photo`** — multipart upload. Response: `{ photo: WoundPhoto }`.
- **`GET /api/episodes/:episodeId/wound-photos`** — list with thumbnails.
- **`POST /api/wound-photos/:woundPhotoId/review`** — RN review. Request: `WoundPhotoReviewInput`. Response: `{ review: WoundPhotoReview }`.

### 14.5 Video events

- **`POST /api/events/postop-video`** — `{ episodeId, videoKind, sessionId, eventType, payload }`.

### 14.6 Re-tier

- **`POST /api/triage/postop-retier/compute`** — pure compute preview.
- **`POST /api/episodes/:episodeId/postop-retier/run`** — persist; called by signal handlers, crons, and the "Recompute now" UI.

### 14.7 Tuning

- **`GET /api/triage/tuning/postop/current`** — current config.
- **`POST /api/triage/tuning/postop`** — admin deploy.

---

## 15. Tuning config

`tuning.json` gains a `postop` block.

```json
{
  "postop": {
    "version": 1,
    "modelVersion": "postop-retier@1.0.0",
    "weights": { "...": "see §10.3 POSTOP_WEIGHTS" },
    "deltaThresholds": { "upgrade1Min": 3, "upgrade2Min": 6 },
    "checkin": {
      "windowHours": 36,
      "tierThresholds": { "greenMin": 85, "orangeMin": 70 },
      "itemWeights": { "...": "see §4.2" },
      "missedStreakHardEscalatorDays": 0
    },
    "surveys": {
      "windowHours": 48,
      "thresholds": {
        "day7":  { "greenMin": 85, "orangeMin": 70 },
        "day14": { "greenMin": 85, "orangeMin": 72 },
        "day30": { "greenMin": 80, "orangeMin": 65 }
      },
      "sectionWeights": {
        "day7":  [40, 20, 25, 15],
        "day14": [30, 35, 20, 15],
        "day30": [20, 45, 15, 20]
      }
    },
    "medAdherence": {
      "rollingWindowDays": 7,
      "highMinYes": 6,
      "lowMaxYes": 4,
      "nonResponseStreakDays": 3,
      "pingTimeLocal": "19:00",
      "responseWindowEndLocal": "23:00"
    },
    "videos": {
      "redFlagEarlyDay": 2,
      "redFlagMissedDay": 5,
      "diagnosisTreatmentEarlyDay": 5,
      "diagnosisTreatmentMissedDay": 14,
      "diagnosisTreatmentMultiviewMin": 3
    },
    "woundPhoto": {
      "earlyDay": 5,
      "midDay": 10,
      "missedSoftDay": 7,
      "missedHardDay": 14,
      "maxFileMb": 12,
      "acceptedMimeTypes": ["image/jpeg", "image/png", "image/heic"]
    },
    "lostContact": {
      "tier3Hours": 24,
      "generalHours": 72
    },
    "rpmEnabled": false,
    "careCompanionEnabled": false
  }
}
```

---

## 16. Component / file structure

```
/app
  /api
    /episodes/[episodeId]
      /daily-checkin/route.ts
      /daily-checkin/today/route.ts
      /day-survey/[day]/route.ts
      /med-adherence/today/route.ts
      /wound-photo/route.ts
      /wound-photos/route.ts
      /postop-retier/run/route.ts
    /wound-photos/[woundPhotoId]/review/route.ts
    /events/postop-video/route.ts
    /triage
      /postop-retier/compute/route.ts
      /tuning/postop/current/route.ts
      /tuning/postop/route.ts

/components
  /postop
    DailyCheckinCard.tsx
    DailyCheckinForm.tsx
    DaySurveyCard.tsx
    DaySurveyForm.tsx
    MedAdherenceCard.tsx
    PostOpVideoCard.tsx
    WoundPhotoUpload.tsx
    WoundPhotoReviewForm.tsx
    PostOpReTierBadge.tsx
    PostOpReTierReasonsList.tsx
    PostOpReTierHistoryTable.tsx

/lib
  /triage
    /postop
      postop-retier.ts
      postop-retier.weights.ts
      postop-retier.mapping.ts
      postop-retier.hard.ts
      postop-retier.delta.ts
      postop-retier.cadence.ts
      daily-checkin-scoring.ts
      day-survey-scoring.ts
      med-adherence-rolling.ts
      lost-contact-detector.ts
      video-engagement.ts
      wound-photo-engagement.ts

/jobs
  postop-daily-checkin-send.ts                # cron, every 24h
  postop-survey-send.ts                       # cron, D7/14/30
  postop-med-adherence-ping.ts                # cron, daily 19:00 local
  postop-med-adherence-non-response.ts        # cron, daily 23:00 local
  postop-retier-nightly.ts                    # cron, 02:00 local
  postop-checkin-missed-watcher.ts            # cron, every 30 min, marks misses
  postop-survey-missed-watcher.ts             # cron, hourly
  postop-lost-contact-watcher.ts              # cron, hourly
  wound-photo-training-export.ts              # cron, nightly de-identified parquet

/__tests__
  postop-retier.spec.ts
  postop-retier.fixtures.ts                   # Examples A–G
  daily-checkin-scoring.spec.ts
  day-survey-scoring.spec.ts
  med-adherence-rolling.spec.ts
  wound-photo-engagement.spec.ts
  postop-hard-escalators.spec.ts
```

---

## 17. Edge cases (enumerated)

1. **Patient submits the daily check-in twice in the same day.** Most-recent wins; both rows preserved; only the most recent is used by re-tier.
2. **Patient submits a check-in with item 8 hit AND item 5 hit AND total RED.** All three signals fire. Hard escalator path takes precedence; tier → TIER_3.
3. **Survey late submission (between hour 49 and hour 72).** Accepted, but the missed-window contributor already fired in the prior re-tier; the late submission writes the score and emits its own `POSTOP_SURVEY_SUBMITTED` event but does not retroactively un-apply the missed penalty.
4. **Wound photo submitted but never reviewed.** Engagement contributor fires (it's a binary on submission). RN queue surfaces "awaiting review" badge; cron-driven escalation if photo unreviewed for 48h surfaces it as a queue task.
5. **Two RNs review the same wound photo.** Both reviews stored. Latest review's structured judgment is the active interpretation; older review preserved in audit and in the training set as a labeled disagreement.
6. **Patient submits a non-image file.** Rejected at endpoint with 415; no row written.
7. **Patient is on hospice / DNR change / palliative pivot mid-episode.** Episode flagged `CARE_GOAL_CHANGED`; re-tier respects the flag by suppressing engagement-missed-penalty contributors but continues to fire safety hard escalators (self-flag, red-flag symptom).
8. **Episode end (D30 reached).** Cron transitions episode to `CLOSED` after a 6h grace; re-tier becomes inert.
9. **Time-zone jumps mid-episode.** All cron sends use the patient's most recent `homeTimeZone` value; daily check-in send sequence numbering remains monotonic (no skipped or duplicate days).
10. **Non-response on the med adherence ping at exactly 23:00 local (boundary).** Treated as `MISSED_NON_RESPONSE` strictly after 23:00.
11. **Patient reaches D30 with all `MISSED` surveys.** D30 missed contributor fires (+1, low weight on D30). Tier already likely upgraded by D7/D14 missed contributors; this is consistent.
12. **Lost contact 72h triggers hard escalator at exactly the same moment as patient submits a check-in.** Race: hard escalator latched in the hourly cron; patient submission triggers a synchronous re-tier that recomputes — the escalator no longer fires (no longer 72h silent), but the prior hard fire is preserved in audit.
13. **Episode is `INTERRUPTED` (admitted to another hospital).** Re-tier inert; existing Triage Tracking PRD §17.12 logic applies.
14. **Wound photo training export run fails (bucket unavailable).** Failure surfaces in the data ops queue; existing rows continue to accumulate; export retries next night.
15. **Concurrent re-tier calls from a check-in submit and a video event arriving milliseconds apart.** Postgres advisory lock per episodeId serializes; both `PostOpReTierEvent` rows write; tier deterministic.
16. **Tuning config swap mid-episode.** New computations use new tuning version atomically; in-flight re-tier completes with prior version; UI shows version stamp.
17. **Patient self-flags during a scheduled telehealth visit.** Existing Triage Tracking PRD §17.15 applies — flag recorded but does not create a new alert.
18. **Day 7 survey submitted on Day 8 via late link.** Accepted; the survey row updates from MISSED to COMPLETED; soft contributor previously fired remains in audit.

---

## 18. Build order

1. **Schema + migrations** — episode additions, all new tables, enum values.
2. **`daily-checkin-scoring.ts`** with full unit-test coverage, including the 10-item scoring matrix.
3. **`day-survey-scoring.ts`** with per-day section weights and tier thresholds.
4. **`med-adherence-rolling.ts`** with rolling 7-day computations, DST tests.
5. **`video-engagement.ts`** and **`wound-photo-engagement.ts`** computing contributor flags from event streams.
6. **`postop-retier.hard.ts`** — hard escalator evaluator (self-flag, NEW_RED_FLAG_SYMPTOM, lost-contact, multiple-incision-flags, day-X-survey-red-and-red-flag).
7. **`postop-retier.delta.ts`** + **`postop-retier.mapping.ts`** + **`postop-retier.ts`** — pure compute orchestrator.
8. **Worked-example fixtures and unit tests** — Examples A–G + all §17 edge cases.
9. **Send crons** — daily check-in send, survey send, med adherence ping send.
10. **Missed-window watchers** — daily check-in 36h, survey 48h, med adherence 23:00, lost-contact 24/72h.
11. **Submission endpoints** — daily check-in, day surveys, med adherence response, wound photo upload, post-op video event.
12. **Wound photo nurse-review form** + endpoint.
13. **Post-op re-tier `run` endpoint** with advisory-lock serialization and event persistence.
14. **Nightly post-op re-tier batch** — every active episode at 02:00 local.
15. **Wound photo training-data export job** — nightly de-identified parquet.
16. **Tuning config block** + APIs.
17. **Patient app surfaces** — daily check-in card, survey card, med adherence card, wound photo upload, post-op video card.
18. **RN queue extensions** — post-op re-tier badge, signals & evidence updates, wound photo review inline.
19. **Synthetic load test** — 50 simulated post-op episodes spanning 30 days each, varied engagement profiles; assert deterministic re-tier outputs and queue stability.

---

## 19. Acceptance criteria summary

- [ ] Daily check-in sends every 24h D1–D30; submission produces deterministic scoring.
- [ ] Item 5 / item 8 emit `WOUND_CONCERN` / `NEW_RED_FLAG_SYMPTOM` events regardless of total tier.
- [ ] D7 / D14 / D30 surveys send at 09:00 local with 48h windows; per-day section weights applied; submission produces deterministic scoring.
- [ ] Multi-session video tracking emits per-session events for both videos; reader counts correct.
- [ ] Med adherence ping fires daily 19:00 local; non-response after 23:00 produces a row; rolling 7-day computation correct including DST.
- [ ] Wound photo upload accepts JPEG/HEIC/PNG ≤12 MB; submission triggers re-tier engagement contributor.
- [ ] Photo content is **not** read by re-tier in v1.
- [ ] Wound photo nurse-review form requires structured fields and explanation ≥30 chars; writes labeled training row.
- [ ] Nightly de-identified training export writes parquet successfully.
- [ ] Hard escalators (self-flag, red-flag symptom, lost-contact, multiple-incision-flags, survey-red-and-red-flag) force TIER_3 regardless of soft delta.
- [ ] Soft delta sums with the §10.3 weights; mapping ≥+3 = +1 step, ≥+6 = +2 steps.
- [ ] Post-op re-tier never lowers tier below `postIntraOpTier`.
- [ ] Re-tier runs synchronously on signal commits, in nightly cron, and on D7/D14/D30 checkpoint crons.
- [ ] Every re-tier writes a `PostOpReTierEvent` regardless of whether tier changed.
- [ ] Patient app never shows tier or score values.
- [ ] RPM and Care Companion contributors are disabled in v1 tuning.
- [ ] Tuning swap atomic; in-flight re-tiers use the version they loaded.
- [ ] All worked examples and edge cases covered by tests.
- [ ] All UI states meet WCAG 2.1 AA.

---

## 20. References (clinical anchors)

- ERAS Society guidelines on post-discharge monitoring — basis for daily symptom check-in red-flag screen and pain trajectory tracking.
- AHRQ "Re-Engineered Discharge (RED) Toolkit" — basis for medication adherence ping cadence and daily symptom screen items.
- CDC SSI surveillance criteria — basis for wound concern items and the structured RN review concern types.
- PROMIS-29 / PROMIS Global-10 — anchor for survey Section A pain interference items.
- KOOS Jr / HOOS Jr / Oswestry Disability Index (ODI) — anchors for procedure-family-specific Section B function items.
- Morisky Medication Adherence Scale (MMAS-8) — anchor for Day-survey Section C medication adherence items (used as proxy; not a literal MMAS reproduction since MMAS is licensed).
- Patient Self-Reported Outcomes literature on post-discharge "self-flag" risk signaling — supports the always-visible self-flag affordance and 100-weight escalation rule.

---

## 21. Glossary

- **Floor** — `Episode.postIntraOpTier`, the tier in effect immediately after intra-op reassessment; post-op re-tier cannot drop below this.
- **Hard escalator** — a single signal that forces TIER_3 regardless of soft delta.
- **Soft contributor** — a signal that adds points to the unsigned post-op delta; mapped to upgrade steps.
- **Rolling 7-day adherence** — the proportion of "Yes" responses to the med adherence ping over the most recent 7 days; computation tolerates DST and time-zone changes.
- **Lost contact** — silent for ≥24h (Tier 3 patients) or ≥72h (any) across all signal channels.
- **Multiple incision flags** — ≥2 chips on item 5 of a single check-in OR any single chip on 3 consecutive check-ins.
- **Engagement reward** — a negative weight in `POSTOP_WEIGHTS`; tracked for audit but clamped to 0 in the unsigned post-op delta sum.
- **Patient self-flag** — owned by Triage Tracking PRD §12; this PRD references but does not modify it.

---

*End of PRD v1.0 — Post-Op Scoring & Re-Tiering*
