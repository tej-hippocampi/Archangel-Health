# Triage Tracking — PRD Suite Index

This directory holds the four PRDs that, together, specify the entire tier-tracking system for a TEAM episode from patient upload through Day 30 post-op. They are designed to compose: each one writes a different slice of the same `Episode.tier` field through the same audit table using the same conventions. This README is the integration map. Read it first, then dive into the individual PRDs.

| PRD | Phase | Owns | File |
|---|---|---|---|
| Initial Pre-Op Triage v1.0 | Upload → first tier | `Episode.initialTier`, `Episode.tier` (first write) | `initial-triage-v1.md` |
| Pre-Op Re-Tiering v1.0 | Upload → surgery (T-N hours) | Re-tier `Episode.tier` from intake / PAM / surveys / engagement | `preop-retier-v1.md` |
| Intra-Op Reassessment v1.0 | OR end → "Switch to post-op" click | Re-tier `Episode.tier` from intra-op data; transition episode → POST_OP | `intraop-reassessment-v1.md` |
| Post-Op Scoring v1.0 | D1 → D30 | Re-tier `Episode.tier` from daily check-ins, surveys, engagement, adherence, wound photos | `postop-scoring-v1.md` |

**Contributing-signal PRDs** (do not own `Episode.tier`; they emit signals consumed by the re-tier engines above):

| PRD | Phase | Feeds | File |
|---|---|---|---|
| Teach-Back Comprehension v1.0 | Pre-op (post-video) + Post-op (post-video) | Comprehension signals into Pre-Op Re-Tier and Post-Op Re-Tier (incl. two new hard escalators — see §8.4) | `teachback-v1.md` |

The existing `Triage Tracking PRD v0.1` (alert lifecycle, RN queue, priority scoring, patient self-flag, autonomous escalation, audit) is **reused unchanged** by all four PRDs above. The four PRDs in this directory **supersede** §4 (initial tier assignment) and §5 (dynamic re-tiering) of v0.1, and **leave §6–§13 intact** as the alert + queue + audit substrate they all write into.

---

## 1. The tier evolves; the field doesn't change

There is exactly one live tier per episode: `Episode.tier`. Every PRD in this suite writes that field. Snapshots are persisted at phase boundaries to preserve the basis for downstream guards:

| Field | Set by | Immutable after |
|---|---|---|
| `Episode.initialTier` | Initial Pre-Op Triage | Initial assignment (never overwritten) |
| `Episode.initialTierWasHardEscalator` | Initial Pre-Op Triage | Initial assignment |
| `Episode.tier` (live) | All four PRDs in sequence | — (live, evolves) |
| `Episode.postIntraOpTier` | Intra-Op Reassessment | The lock that fires reassessment |
| `Episode.tierLastChanged` / `tierLastChangedBy` | All writers | — (updated on every change) |

Snapshots act as **floors and guards** for downstream stages:

- The pre-op re-tier reads `initialTier` + `initialTierWasHardEscalator` to enforce its sticky-hard guard on downgrades.
- The intra-op reassessment uses "most conservative wins" against the *current* tier (which may already reflect pre-op re-tiering) — never downgrades.
- The post-op re-tier reads `postIntraOpTier` as a floor it cannot drop below.

---

## 2. Patient journey timeline

```
              ┌─────────────┐
   T-∞        │   UPLOAD    │   Initial Pre-Op Triage v1.0
              │             │   • Reads chart (six categories)
              │             │   • Writes Episode.initialTier + Episode.tier
              │             │   • Records hard-escalator flag for sticky guard
              └──────┬──────┘
                     │
       ┌─────────────┼─────────────────────────────────────┐
       │             │  Pre-Op Re-Tiering v1.0              │
       │             │  • Intake form completion + content  │
       │             │  • PAM-style proxy (LOW/MOD/HIGH)    │
       │             │  • Surveys T-96 / T-48 / T-24        │
       │             │  • Pre-op video & battle-card views  │
       │             │  • Idempotent recompute on signal +  │
       │             │    cron at T-96/T-72/T-48/T-24/T-0   │
       │             │  • Writes Episode.tier               │
       │             │  • Sticky-hard guard on downgrades   │
       └─────────────┼─────────────────────────────────────┘
                     │
              ┌──────┴──────┐
   T-0        │   SURGERY   │
              └──────┬──────┘
                     │
              ┌──────┴──────┐
   OR end     │  PACU       │   Intra-Op Reassessment v1.0
              │             │   • Surgeon clicks "Switch to post-op"
              │             │   • PDF op-note upload + AI extraction
              │             │     OR manual fill of 11 required fields
              │             │   • Lock fires reassessment (single tx)
              │             │   • resolveFinalTier(current, proposed)
              │             │   • Writes Episode.tier (upward-only)
              │             │   • Snapshots Episode.postIntraOpTier
              │             │   • Transitions episode → POST_OP
              │             │   • Conservative default at +24h if unlocked
              └──────┬──────┘
                     │
       ┌─────────────┴─────────────────────────────────────┐
       │  Post-Op Scoring v1.0                              │
       │  • Daily symptom check-in (D1–D30)                 │
       │  • Day 7 / Day 14 / Day 30 surveys                 │
       │  • Diagnosis-treatment + red-flag videos           │
       │  • Med adherence ping (daily 19:00 local)          │
       │  • Wound photo submission (engagement only)        │
       │  • Wound photo nurse review → training data        │
       │  • Patient self-flag (existing Triage Tracking §12)│
       │  • Idempotent recompute on signal + nightly + D7/  │
       │    D14/D30 checkpoints                             │
       │  • Upward-only; floor = postIntraOpTier            │
       │  • RPM and Care Companion DISABLED in v1           │
       └─────────────┬─────────────────────────────────────┘
                     │
              ┌──────┴──────┐
   D30        │   CLOSE     │   Episode closes after 6h grace
              └─────────────┘
```

---

## 3. Conventions held across all four PRDs

These are the constants that bind the system together. Cursor should treat any deviation in a single PRD as a bug.

### 3.1 Tier values

```ts
type Tier = 'TIER_1' | 'TIER_2' | 'TIER_3';
const TIER_RANK: Record<Tier, number> = { TIER_1: 1, TIER_2: 2, TIER_3: 3 };
```

**TIER_3 is the highest risk.** This is preserved across every PRD. Every weight, color (red border = TIER_3, amber = TIER_2, neutral = TIER_1), priority base score (`+20` for TIER_3), and downstream consumer assumes this ordering. Any inversion would require a synchronous rewrite of all four PRDs and the existing Triage Tracking PRD §8.

### 3.2 Idempotent recompute

Every re-tier function (pre-op re-tier, intra-op reassessment, post-op re-tier) **rebuilds from a snapshot floor + current signal state on every call**. There is no event-by-event mutation of `Episode.tier`. This guarantees:

- Out-of-order signals produce the same final tier as in-order signals.
- Retried calls are safe.
- Audit logs capture inputs that produced outputs (not deltas applied in sequence).

### 3.3 Direction rules

| Stage | Can upgrade? | Can downgrade? | Mechanism |
|---|---|---|---|
| Initial Pre-Op Triage | N/A — single assignment | N/A — coordinator override only | Hard escalators short-circuit; weighted soft score otherwise |
| Pre-Op Re-Tiering | Yes (algorithmic) | Yes, by ≤1 step from `initialTier`, **subject to sticky guard**. Cannot go below `initialTier` if `initialTierWasHardEscalator=true` | Signed delta |
| Intra-Op Reassessment | Yes (algorithmic) | **No** — `resolveFinalTier(current, proposed) = max(rank)` | Most-conservative-wins |
| Post-Op Re-Tiering | Yes (algorithmic) | **No, algorithmically.** RN action with reason can downgrade per Triage Tracking PRD §5.4 (24h cooldown after self-flag + completed contact) | Unsigned positive-only delta with cap |

The asymmetry is intentional and documented in each PRD: false-low tiering is dangerous, false-high tiering is just slightly more labor.

### 3.4 Sticky hard guard

Set on the episode by Initial Pre-Op Triage when a hard escalator triggers. Read by Pre-Op Re-Tiering before any algorithmic downgrade. Not used by intra-op or post-op (both upward-only). Coordinator overrides do not flip the flag — the boolean reflects the *original algorithmic basis*, not the override.

### 3.5 Audit pattern

Every state-altering call writes a `TriageEvent` (existing Triage Tracking PRD §13) **plus** a re-tier-specific snapshot row when applicable:

| Stage | Snapshot row table |
|---|---|
| Initial Pre-Op Triage | (no snapshot table — fields on `Episode` are the snapshot) |
| Pre-Op Re-Tiering | `PreOpReTierEvent` |
| Intra-Op Reassessment | `IntraOpReassessmentEvent` (also writes `IntraopForm` and `IntraopExtraction` rows) |
| Post-Op Re-Tiering | `PostOpReTierEvent` |

Every snapshot row carries:
- The full input snapshot at compute time
- The model version (`<feature>@<semver>`)
- The tuning config version
- The triggering reason (signal type, checkpoint, manual recompute, conservative default)
- Both `tierBefore` and `tierAfter`

This makes every tier change reproducible from the snapshot alone, even if upstream chart data, signal events, or tuning are subsequently mutated.

**Pass 4 — intra-op handoff:** When an RN marks an intra-op draft **`READY_FOR_SURGEON_REVIEW`**, the CareGuide implementation also inserts an **`escalations`** row (`tier=2`, `trigger_type=intraop:ready_for_review`) so the draft appears in the shared alerts queue; a matching **`INTRAOP_FORM_READY_FOR_REVIEW`** event is logged. Recall of a draft uses `intraop:draft_recalled`. Fine-grained **role gates** for all four triage routers are documented in `backend/auth_roles.py` and the pass-4 changelog.

### 3.6 Tuning config

A single `tuning.json` file holds **all** weights, thresholds, and switches across all four PRDs. Loaded by `/lib/triage/tuning.ts` (introduced in Initial Pre-Op Triage), reloaded on file change, version-tracked via `TuningConfig` rows.

```json
{
  "initialTier":   { "version": 1, "modelVersion": "initial-tier@1.0.0",   ... },
  "preopRetier":   { "version": 1, "modelVersion": "preop-retier@1.0.0",   ... },
  "intraop":       { "version": 1, "modelVersion": "intraop-delta@1.0.0",  ... },
  "postop":        { "version": 1, "modelVersion": "postop-retier@1.0.0",  ... }
}
```

Each PRD computes its result and stamps the result row with the version that produced it. Tuning swaps mid-episode are atomic per-PRD and never mutate in-flight computations.

### 3.7 What v1 explicitly does **not** include

| Not in scope | Where it will land | Why excluded from v1 |
|---|---|---|
| RPM device readings (vitals stream, missed-readings alerts) | Future PRD | Author guidance: scope discipline; existing v0.1 §6 entries kept as `enabled: false` for v2 reactivation |
| Care Companion engagement (usage, transcripts) | Future PRD | Author guidance; revised logic to be defined |
| Wound photo content classifier (model-driven tiering of wound state) | Future PRD; v1 lays the labeled-data pipeline | Insufficient training data; nurse-review pipeline in Post-Op §8 builds it |
| Auto-rerun of Initial Pre-Op Triage when intake reveals new comorbidities | Manual coordinator advisory in v1 (`CLINICAL_BASELINE_REVIEW_NEEDED`) | Complex side effects; deferred until manual flow proves out |
| Post-op surveys beyond D7/D14/D30 | — | Out of scope; cadence frozen for v1 |
| Multi-surgeon TEAM pods (multiple operating surgeons per 4-seat pod) | Future PRD | Pass 4 caps the pod at **one surgeon (director)** + **one RN coordinator** + **two NP/PA**; additional surgeon roles are out of scope. |
| Dedicated “clinical operations lead” / anesthesia-provider seats in the TEAM role matrix | Removed from v1 prose | Pass 4 consolidates clinical staff to **surgeon**, **rn_coordinator**, **np_pa** (+ **system_admin** for tuning); intra-op draft is **RN-owned** until surgeon lock. |

---

## 4. Signal-source → consumer matrix

| Signal source | Initial Pre-Op | Pre-Op Re-Tier | Intra-Op | Post-Op Re-Tier | Existing Alert Pipeline |
|---|---|---|---|---|---|
| Procedure type | ✓ | — | ✓ (P90 lookup) | — | — |
| Active problems / chart | ✓ | — | — | — | — |
| Current medications | ✓ | — | — | — | — |
| Allergies | ✓ | — | — | — | — |
| Social history (chart) | ✓ | — | — | — | — |
| Recent labs / studies | ✓ | — | — | — | — |
| Intake form completion + disclosures | — | ✓ | — | — | — |
| PAM proxy (in intake) | — | ✓ (high weight) | — | — | — |
| Pre-op surveys T-96 / T-48 / T-24 | — | ✓ | — | — | — |
| Pre-op video, battle-card views | — | ✓ | — | — | — |
| Pre-op teach-back comprehension (post-loop) | — | ✓ (med-hold post-loop fail = hard escalator) | — | — | — |
| Intra-op form (locked) | — | — | ✓ | — | — |
| PDF op-note + extraction | — | — | ✓ | — | — |
| Daily symptom check-in | — | — | — | ✓ | ✓ (`DAILY_CHECKIN_RED`, `WOUND_CONCERN`, `NEW_RED_FLAG_SYMPTOM`) |
| Day 7 / 14 / 30 surveys | — | — | — | ✓ | ✓ (`SURVEY_DAY_X_RED`, `SURVEY_DAY_X_MISSED`) |
| Diagnosis/treatment + red-flag videos | — | — | — | ✓ | — |
| Post-op teach-back comprehension (post-loop) | — | — | — | ✓ (red-flag post-loop fail = hard escalator) | — |
| Med adherence ping | — | — | — | ✓ | ✓ (`MED_ADHERENCE_LOW`, `MED_ADHERENCE_NON_RESPONSE_STREAK`) |
| Wound photo submission (binary) | — | — | — | ✓ | ✓ (lost-engagement) |
| Wound photo content (nurse-reviewed) | — | — | — | **No (v1)** | — |
| Patient self-flag (existing) | — | — | — | ✓ (hard escalator) | ✓ (existing `PATIENT_SELF_FLAG`, weight 100) |
| RPM vitals | — | — | — | **DISABLED v1** | **DISABLED v1** |
| Care Companion engagement | — | — | — | **DISABLED v1** | — |

---

## 5. Cross-PRD data model

A single `Episode` row carries fields contributed by all four PRDs. The full picture:

```prisma
model Episode {
  // Identity
  id, patientId, status                                  // existing

  // Initial Pre-Op Triage
  initialTier                          Tier
  initialTierAssignedAt                DateTime
  initialTierAssignedBy                String
  initialTierScore                     Int?
  initialTierReasons                   Json
  initialTierModelVersion              String
  initialTierTuningVersion             Int
  initialTierInputsSnapshot            Json
  initialTierWasOverridden             Boolean   @default(false)
  initialTierOverrideReason            String?
  initialTierOverrideBy                String?
  initialTierOverrideAt                DateTime?
  initialTierWasHardEscalator          Boolean   @default(false)        // sticky-guard input

  // Live tier (written by all four)
  tier                                 Tier
  tierLastChanged                      DateTime
  tierLastChangedBy                    String

  // Pre-Op Re-Tiering snapshot (denormalized for queue)
  preOpReTierLastRunAt                 DateTime?
  preOpReTierLastDelta                 Int?
  preOpReTierLastTier                  Tier?
  preOpReTierTopReasons                Json?
  preOpReTierVersion                   String?
  preOpReTierTuningVersion             Int?

  // Intra-Op Reassessment
  intraopFormId                        String?   @unique
  postIntraOpTier                      Tier?                            // floor for post-op re-tier

  // Post-Op Re-Tiering snapshot
  dailyCheckinMissedStreak             Int       @default(0)
  postOpReTierLastRunAt                DateTime?
  postOpReTierLastDelta                Int?
  postOpReTierTopReasons               Json?
  postOpReTierVersion                  String?
  postOpReTierTuningVersion            Int?

  // Relations contributed by each PRD
  triageEvents                         TriageEvent[]
  preOpReTierEvents                    PreOpReTierEvent[]
  pamAssessments                       PamAssessment[]
  intraopForm                          IntraopForm?
  intraOpReassessments                 IntraOpReassessmentEvent[]
  dailyCheckinSends                    DailyCheckinSend[]
  dailyCheckinResponses                DailyCheckinResponse[]
  dayXSurveys                          DayXSurvey[]
  medAdherencePings                    MedAdherencePing[]
  medAdherenceResponses                MedAdherenceResponse[]
  woundPhotos                          WoundPhoto[]
  woundPhotoReviews                    WoundPhotoReview[]
  postOpVideoEvents                    PostOpVideoEvent[]
  postOpReTierEvents                   PostOpReTierEvent[]
}
```

Shared cross-PRD enums (extended additively):

- `Tier` — 3 values, never changes
- `TriageEventType` — each PRD adds its own values (see each PRD's §13/§9 schema section)
- `AlertReason` — each PRD adds its own values (consumed by existing Triage Tracking §6 alert pipeline)

---

## 6. Build order across the suite

The ordering below is the order Cursor should implement. Each phase's tests must pass before the next begins.

### Phase 0 — Foundation

1. `Episode` schema with all cross-PRD fields above (introduce them all up front to avoid migration churn).
2. `TriageEvent` table (existing Triage Tracking §13 — reused).
3. `TuningConfig` table + `tuning.json` loader (`/lib/triage/tuning.ts`).
4. Tier and shared enums (`Tier`, `TriageEventType`, `AlertReason`) declared with all values from all four PRDs.

### Phase 1 — Initial Pre-Op Triage

Build per `initial-triage-v1.md` §13 (build order). Outputs: working upload review screen, persisted initial tier, override flow, audit events.

### Phase 2 — Pre-Op Re-Tiering

Build per `preop-retier-v1.md` §14 (build order). Outputs: PAM proxy embedded in intake, multi-session video tracking, battle-card view tracking, idempotent re-tier on signal + checkpoints.

### Phase 3 — Intra-Op Reassessment

Build per `intraop-reassessment-v1.md` §14 (build order). Outputs: Switch-to-post-op flow, PDF op-note extraction (mock then LLM), single-transaction lock + reassessment + episode → POST_OP.

### Phase 4 — Post-Op Scoring

Build per `postop-scoring-v1.md` §18 (build order). Outputs: daily check-in pipeline, D7/14/30 surveys, video tracking, med adherence ping, wound photo upload + nurse review pipeline + nightly de-identified training export, idempotent post-op re-tier.

Each phase depends only on its predecessors. Phases 2/3/4 do not modify earlier phases' code; they only consume snapshot fields and write `Episode.tier`.

---

## 7. Reviewer's checklist (use this for the "beautiful pass")

A targeted checklist for Cursor to verify the four PRDs implement consistently:

### 7.1 Conventions

- [ ] `TIER_3` is highest-risk in every weight table, color rule, comment, and worked example across all four PRDs.
- [ ] Every re-tier function is idempotent (rebuilds from snapshot + signals; no event-by-event mutation).
- [ ] Direction rules respected: pre-op re-tier can downgrade with sticky guard; intra-op and post-op never downgrade algorithmically.
- [ ] Sticky-hard guard wired: `Episode.initialTierWasHardEscalator` set in Phase 1, read in Phase 2, ignored in Phases 3/4.
- [ ] Every state change writes a `TriageEvent`; every re-tier writes a snapshot row regardless of whether tier changed.

### 7.2 Audit reproducibility

- [ ] Every snapshot row carries `modelVersion`, `tuningVersion`, full input snapshot, before/after tier, triggering reason.
- [ ] Tuning swap during a re-tier preserves the version that was used by the in-flight computation.
- [ ] Coordinator overrides preserve both the auto-assigned and final tier.
- [ ] Tier downgrades by RN action carry actor + reason ≥30 chars.

### 7.3 Surfacing

- [ ] Tier values, scores, and activation levels are **never** displayed to the patient.
- [ ] Coordinator queue tier card shows: current tier; chip for "re-tiered" if `tier !== initialTier`; chip for "post-op re-tiered" if `tier !== postIntraOpTier`; top reasons with weights; last-run timestamp; "Recompute now" affordance.
- [ ] All UI states meet WCAG 2.1 AA across every PRD.

### 7.4 Cadence

- [ ] Pre-op re-tier runs on signal AND at T-96 / T-72 / T-48 / T-24 / T-0.
- [ ] Intra-op reassessment fires on lock (sync) AND on conservative-default cron at OR-end + 24h.
- [ ] Post-op re-tier runs on signal AND nightly 02:00 local AND at D7 / D14 / D30 checkpoints.
- [ ] Concurrent re-tier calls per episode are serialized via Postgres advisory lock.

### 7.5 Excluded paths

- [ ] No RPM signal types fire in v1 (existing `MISSED_READINGS_24H`, `MISSED_READINGS_48H`, `TEMP_SUSTAINED`, `TACHYCARDIA`, `HYPOTENSION`, `SPO2_LOW` from Triage Tracking PRD §6 are gated `enabled: false`).
- [ ] No Care Companion engagement contributors fire.
- [ ] Wound photo content does not feed the post-op delta in v1.
- [ ] `CLINICAL_BASELINE_REVIEW_NEEDED` advisory does not auto-rerun initial tier; it requires explicit coordinator action.

### 7.6 Schema hygiene

- [ ] All cross-PRD enum values added in Phase 0 (no per-feature ALTER TABLE in later phases).
- [ ] Snapshot rows are append-only — no UPDATE on `*ReTierEvent`, `*ReassessmentEvent`, `TriageEvent`, `WoundPhotoReview` rows after creation.
- [ ] Episode mutations write through a single `Episode.updateTier(...)` routine (no scattered `prisma.episode.update({ tier })` calls).

### 7.7 Tests

- [ ] Worked examples in each PRD's §5 / §10 are encoded as fixture-based unit tests.
- [ ] Edge cases enumerated in each PRD have at least one test each.
- [ ] Synthetic load test exists per phase (e.g., 100 patients for initial; 50 for re-tier; 30 for intra-op; 50 for post-op).
- [ ] Tier distribution from synthetic load is within published targets for each phase.

---

## 8. Quick reference

### 8.1 Tier numbering

```ts
type Tier = 'TIER_1' | 'TIER_2' | 'TIER_3';
// TIER_1 = lowest risk    (Standard cadence)
// TIER_2 = moderate risk  (Enhanced cadence)
// TIER_3 = highest risk   (High-touch cadence)
```

### 8.2 Re-tier delta thresholds (consistent across PRDs)

```ts
// Pre-Op Re-Tier (signed delta)
if (delta >= 6)  return upgrade(initial, 2);
if (delta >= 3)  return upgrade(initial, 1);
if (delta <= -3 && !initialWasHard) return downgrade(initial, 1);
return initial;

// Intra-Op (no delta; hard or step-up logic; resolve via max-rank)
proposedTier = hardUpgrade ? TIER_3
             : stepUpgrades >= 2 ? TIER_3
             : stepUpgrades === 1 ? stepUp(current, 1)
             : current;
finalTier = max(current, proposedTier);

// Post-Op Re-Tier (unsigned positive-only delta, cap 12)
if (delta >= 6)  return upgrade(floor, 2);
if (delta >= 3)  return upgrade(floor, 1);
return floor;
```

### 8.3 Cadence summary

```
Pre-Op Re-Tier:    on signal + cron at T-96, T-72, T-48, T-24, T-0
Intra-Op:          on lock (sync) + conservative-default cron at OR-end + 24h
Post-Op Re-Tier:   on signal + nightly 02:00 local + cron at D7, D14, D30
```

### 8.4 Hard escalator inventory

| Stage | Hard escalators (any → TIER_3) |
|---|---|
| Initial Pre-Op Triage | Emergency case; sepsis 48h; ventilator; disseminated cancer; dialysis; CHF recent; severe low EF; ascites; functional status totally dependent; prior 30-d readmission; housing instability; food insecurity; lives alone with no caregiver |
| Pre-Op Re-Tier | Intake disclosure of lives-alone-no-caregiver; intake disclosure of housing instability; intake disclosure of food insecurity; intake disclosure of transportation barrier day-of; survey red-flag-critical; PAM LOW at T-24; **teach-back medication-hold post-loop failure** (`TEACHBACK_FAILED_MED_HOLD_POSTLOOP`) |
| Intra-Op Reassessment | Documented complication; spinal dural tear; bowel contamination class 4; CABG mechanical-support bypass weaning; procedure aborted |
| Post-Op Re-Tier | Patient self-flag active; new red-flag symptom (chest pain, severe SOB, calf swelling, etc.); lost contact 24h (Tier 3) or 72h (general); multiple incision flags (≥2 chips/day or chip on 3 consecutive days); D7/D14 survey RED + red-flag chip; **teach-back red-flag post-loop failure** (`TEACHBACK_FAILED_RED_FLAG_POSTLOOP`) |

### 8.5 Module / file roots

```
/lib/triage/
  tuning.ts                                  # shared loader
  initial-tier.*                             # Initial Pre-Op Triage
  preop-retier.*                             # Pre-Op Re-Tiering
  pam-proxy.ts
  intake-disclosures.ts
  intraop-delta.*                            # Intra-Op Reassessment
  intraop-extractor.*
  intraop-apply.ts
  postop/
    postop-retier.*                          # Post-Op Re-Tiering
    daily-checkin-scoring.ts
    day-survey-scoring.ts
    med-adherence-rolling.ts
    lost-contact-detector.ts
    video-engagement.ts
    wound-photo-engagement.ts
```

---

*Last updated: 2026-05-08. If you change any convention here (tier numbering, sticky-guard behavior, idempotent recompute, audit pattern, tuning structure), update all four PRDs in lockstep.*
