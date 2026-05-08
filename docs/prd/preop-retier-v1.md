# PRD — Pre-Op Re-Tiering (Intake, PAM, Surveys, Engagement)

| Field | Value |
|---|---|
| Feature | Pre-Op Re-Tiering |
| Document version | 1.0 (final) |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-05-08 |
| Primary user | RN care coordinator (consumes updated tier in queue) |
| Secondary users | Patient (intake form, surveys, video, battle-card), NP / PA, Surgeon (read) |
| Implementation target | Next.js 14 App Router + TypeScript + Tailwind + shadcn/ui + Prisma/Postgres (PRD style); existing Python/FastAPI/SQLite scoring code is the behavioral reference and is reused where indicated |
| Audience | Cursor / engineering implementers |
| Depends on | Initial Pre-Op Triage PRD v1.0 (`initial-triage-v1.md`); intake form (existing in `backend/intake_section_chat.py`, `backend/intake_form_parser.py`); pre-op surveys (existing in `backend/preop_survey.py`); pre-op video (existing in `frontend/pre-op.js`); battle-card (existing in `frontend/pre-op.html` rendering `preop_resource.battlecard_html`) |
| Supersedes | None — this is the first re-tiering PRD; future post-op re-tiering will be a separate document |

---

## 0. Reading order and conventions

- TIER_3 = highest risk (preserved from initial-triage PRD).
- All times are expressed relative to surgery start in **hours-until-surgery (T-N)**: T-96 = 96 hours pre-op, T-0 = surgery start.
- "Initial tier" means the value of `Episode.initialTier` set by the initial-triage feature at upload. "Current tier" means `Episode.tier`, the live tier that drives the queue and cadence. Re-tiering writes the latter; never the former.
- The existing Python codebase scores surveys and computes a transient per-window survey tier (green/orange/red). This PRD **does not replace that scoring**; it consumes its output as one input among several and routes everything into a persisted `Episode.tier`.

---

## 1. Scope

**In scope.**

1. The five re-tier signal sources: intake form completion + content, PAM-style proxy assessment, T-96 / T-48 / T-24 surveys (scores AND completion), pre-op video views (count), battle-card views (count).
2. The re-tier algorithm — a delta from `initialTier` driven by upgrade/downgrade soft factors, plus re-tier-specific hard escalators.
3. Sticky-hard-escalator guard preventing downgrade below an initial-tier hard escalator.
4. Specification of the PAM-style proxy (13 items, 4-point scale, LOW/MOD/HIGH binning) embedded in the intake form interview — instrument design plus scoring.
5. View tracking for video (multi-view counts, not just first-play) and for battle-card (currently untracked).
6. Cadence: re-tier runs synchronously on every signal commit AND at scheduled checkpoints T-96, T-72, T-48, T-24, T-0.
7. Idempotent recompute model: every re-tier call rebuilds the tier from `initialTier` + currently-known signals (no compounding).
8. Persisted episode state, audit events, API contracts, tuning config, UI affordances on the existing patient and coordinator surfaces.

**Out of scope.**

- The initial pre-op tier (handled by `initial-triage-v1.md`).
- The clinical content of the intake form (already specified by the existing intake feature; we consume its outputs only).
- Survey question content and scoring logic (already specified in `backend/preop_survey.py`; we consume its outputs only).
- Telehealth visit feature, RPM signals, post-op re-tiering, intra-op reassessment.
- Re-running the initial-tier model when intake reveals new comorbidities — surfaced as a coordinator advisory in v1; not auto-triggered.

---

## 2. Why this exists (1 paragraph)

The initial tier is the floor of risk derived from the chart at upload. Between upload and surgery, the system gathers a second class of signal — the patient's own engagement, activation, and self-reported readiness — that the chart cannot capture. A patient with chart-clean labs but LOW PAM activation, no completed intake, and zero video views is materially different from one with the same chart and HIGH PAM, completed intake, and three video views. Pre-op re-tiering closes that gap. It produces the live `Episode.tier` that the coordinator queue and downstream automations key off of, and it does so deterministically, on every signal, with full auditability and the ability to downgrade or upgrade as evidence accumulates.

---

## 3. Inputs

The five sources, plus the initial tier as the anchor.

| Source | Provides | Already in repo? |
|---|---|---|
| Initial tier + reason kind | Tier floor; whether a hard escalator triggered initial assignment | Yes (PRD v1.0) |
| Intake form | Completion status (per-section), interview-discovered social/safety facts, surfaced disclosures | Partially — intake form/interview exist; new event types and disclosure-flag derivation must be added |
| PAM-style proxy (new) | 13-item activation score (0–100), level (LOW/MOD/HIGH) | No — to be built |
| Pre-op surveys (T-96 / T-48 / T-24) | Per-window: score (0–100), survey tier (green/orange/red), critical red flags, completion status | Yes — `backend/preop_survey.py` |
| Pre-op video | Cumulative view count, first-view timestamp, last-view timestamp | Partial — first play is logged; multi-view tracking must be added |
| Battle-card | Cumulative view count, first-view timestamp, last-view timestamp | No — must be added |

The re-tier algorithm reads exactly these. It does not reach back into chart data; chart-derived risk is captured upstream by the initial tier.

---

## 4. PAM-style proxy specification

The Patient Activation Measure (PAM-13) is a copyrighted instrument. We do not reproduce it. Instead, we specify a **proxy** — 13 items in the same activation-confidence frame, focused on the surgical episode, embedded as a structured block inside the existing intake interview (`backend/intake_section_chat.py`).

### 4.1 The 13 proxy items

Each item is rated on a 4-point scale (1 = Strongly Disagree, 2 = Disagree, 3 = Agree, 4 = Strongly Agree) with an optional N/A. N/A items are excluded from scoring.

1. I am the person most responsible for managing my health before surgery.
2. I know what each of my pre-op instructions is for.
3. I am confident I can follow my pre-op instructions correctly.
4. I know which of my medications to hold and which to take on the morning of surgery.
5. I know who to contact if I have a problem before surgery.
6. I am confident I can describe a problem to my care team if one comes up.
7. I understand what will happen during recovery in the first week after surgery.
8. I am confident I can manage my pain medication safely after surgery.
9. I know how to recognize signs of a wound infection.
10. I am confident I can complete the daily check-ins after surgery.
11. I have arranged the help I will need at home after surgery.
12. I am confident I can stick to my recovery plan even when it is hard.
13. I know what questions to ask my surgeon if I am uncertain.

### 4.2 Scoring

```ts
// /lib/triage/pam-proxy.ts
export interface PamResponse {
  itemIndex: number;        // 1..13
  value: 1 | 2 | 3 | 4 | 'N_A';
}

export interface PamResult {
  rawSum: number;             // sum of 1..4 over non-N_A items
  itemsScored: number;        // count of non-N_A items
  rawAverage: number;         // rawSum / itemsScored, in [1, 4]
  activationScore: number;    // 0..100, see formula
  level: 'LOW' | 'MODERATE' | 'HIGH';
  isComplete: boolean;        // itemsScored >= 10 (allow up to 3 N_A)
}

// Linear rescale of average 1..4 into 0..100, then bin against PAM-13 standard cutoffs.
// Cutoffs anchored on published PAM-13 short form thresholds (47.0 / 55.1 / 67.0).
export function scorePam(responses: PamResponse[]): PamResult {
  const scored = responses.filter(r => r.value !== 'N_A') as Array<Required<PamResponse>>;
  const itemsScored = scored.length;
  if (itemsScored < 10) {
    return { rawSum: 0, itemsScored, rawAverage: 0, activationScore: 0, level: 'LOW', isComplete: false };
  }
  const rawSum = scored.reduce((acc, r) => acc + (r.value as number), 0);
  const rawAverage = rawSum / itemsScored;
  const activationScore = Math.round(((rawAverage - 1) / 3) * 100 * 10) / 10;
  const level: 'LOW' | 'MODERATE' | 'HIGH' =
    activationScore <= 55.1 ? 'LOW'
    : activationScore <= 67.0 ? 'MODERATE'
    : 'HIGH';
  return { rawSum, itemsScored, rawAverage, activationScore, level, isComplete: true };
}
```

### 4.3 Embedding in the intake interview

The PAM block is presented as section **3.5** in the intake interview flow (between Medical History and Surgical & Anesthesia History), labeled to the patient as "Your readiness for surgery." All 13 items rendered together with a progress indicator. Patient cannot proceed past the section without answering at least 10 items (3 N/A allowed).

The intake form parser (`backend/intake_form_parser.py`) is extended to recognize the PAM block and persist responses; the result of `scorePam(...)` is stored on a new `PamAssessment` row keyed to the episode.

### 4.4 Acceptance criteria for the PAM proxy

- **AC-4.1** Section renders with all 13 items on a single screen with a 4-point scale + N/A.
- **AC-4.2** Patient cannot submit the section unless at least 10 items are answered with non-N/A values.
- **AC-4.3** `scorePam` produces deterministic output for any input set; covered by unit tests including all-1s, all-4s, mixed, with 0/1/2/3 N/As.
- **AC-4.4** Activation score and level are stored with the response set and are consumed by the re-tier algorithm.
- **AC-4.5** Re-completing the PAM proxy (e.g., patient retakes) versions the assessment; the most recent complete version is used by re-tier.

---

## 5. The re-tier model

### 5.1 Idempotent recompute

Every re-tier call **rebuilds** the tier from `initialTier` + the current state of all five signal sources. There is no event-by-event mutation of the tier. This eliminates ordering bugs, makes the audit log clean (each event captures the inputs that produced the output), and makes recomputes safe to retry.

```ts
// /lib/triage/preop-retier.ts
export function reTierPreOp(state: PreOpReTierInput): PreOpReTierResult {
  const reasons: ReTierReason[] = [];

  // Stage 0 — re-tier-specific hard escalators (TIER_3 regardless of initial)
  for (const hard of evaluateReTierHardEscalators(state)) {
    reasons.push(hard);
    return finalize(state.initialTier, 'TIER_3', reasons, state);
  }

  // Stage 1 — sum signed soft delta
  const delta = computePreOpDelta(state, reasons);

  // Stage 2 — apply delta to initialTier with sticky guard
  const targetTier = applyDeltaWithGuard(state.initialTier, state.initialTierWasHardEscalator, delta);

  return finalize(state.initialTier, targetTier, reasons, state);
}
```

### 5.2 Re-tier hard escalators

If any are true, re-tier sets the tier to TIER_3 regardless of `initialTier` or delta. These are hazards revealed by signals after upload, and they always dominate.

| Hard escalator | Source | Rationale |
|---|---|---|
| `INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER` | Intake interview surfaces (`livesAlone=true` AND `hasReliableCaregiver=false`) when chart did not | Same gravity as the initial-tier hard escalator; promotes regardless. |
| `INTAKE_DISCLOSURE_HOUSING_INSTABILITY` | Intake reveals unstable housing or homelessness | Same as initial-tier hard. |
| `INTAKE_DISCLOSURE_FOOD_INSECURITY` | Intake reveals food insecurity | Same as initial-tier hard. |
| `INTAKE_DISCLOSURE_TRANSPORTATION_BARRIER_DAY_OF` | Intake reveals no ride / no responsible adult on day of surgery | Likely-cancellation event; clinically high-touch. |
| `SURVEY_RED_FLAG_CRITICAL` | Any T-48 or T-24 survey response that the existing scorer marks `red=true` on a critical item (NPO violation, active red-flag symptom screen, no ride/caregiver) | Mirrors existing red-flag semantics in `preop_survey.py`. |
| `PAM_LEVEL_LOW_AT_T_24` | PAM proxy completed and result is LOW with no further opportunity to remediate (T-24 reached) | Low activation at the end of the pre-op window has well-documented post-op risk; matches the original Triage Tracking PRD's `LOW activation` hard escalator. |

### 5.3 Soft delta (signed) — upgrade and downgrade contributors

Every contributor adds a signed integer to a running `delta`. Positive = upgrade pressure, negative = downgrade pressure. Defaults below; all weights live in `tuning.json` (§11) with an effective version stamped on each computation.

```ts
// /lib/triage/preop-retier.weights.ts
export const PREOP_RETIER_WEIGHTS = {
  // PAM (highest weight class per author guidance)
  PAM_LEVEL_LOW:                       +5,   // (re-tier hard at T-24; soft elsewhere)
  PAM_LEVEL_MODERATE:                  +1,
  PAM_LEVEL_HIGH:                      -3,
  PAM_NOT_COMPLETED_BY_T_72:           +2,
  PAM_NOT_COMPLETED_BY_T_24:           +3,   // additional, on top of T-72 penalty

  // Intake form completion
  INTAKE_NOT_STARTED_BY_T_96:          +2,
  INTAKE_NOT_STARTED_BY_T_72:          +3,   // replaces the +2 (not additive)
  INTAKE_STARTED_NOT_COMPLETE_BY_T_48: +2,
  INTAKE_NOT_COMPLETE_BY_T_24:         +4,
  INTAKE_COMPLETE:                     -1,   // reward for engagement + completeness

  // Per-window survey tier (mapped from existing scorer output)
  SURVEY_T_96_RED:                     +3,
  SURVEY_T_96_ORANGE:                  +1,
  SURVEY_T_96_GREEN:                    0,
  SURVEY_T_96_MISSED:                  +2,   // window closed without response

  SURVEY_T_48_RED:                     +3,
  SURVEY_T_48_ORANGE:                  +1,
  SURVEY_T_48_GREEN:                    0,
  SURVEY_T_48_MISSED:                  +2,

  SURVEY_T_24_RED:                     +3,
  SURVEY_T_24_ORANGE:                  +1,
  SURVEY_T_24_GREEN:                    0,
  SURVEY_T_24_MISSED:                  +2,

  // Engagement — pre-op video
  VIDEO_VIEWED_AT_LEAST_ONCE_BY_T_72:  -1,
  VIDEO_VIEWED_3_OR_MORE_BY_T_48:      -1,   // additional
  VIDEO_NOT_VIEWED_BY_T_48:            +1,
  VIDEO_NOT_VIEWED_BY_T_24:            +2,   // replaces the +1 (not additive)

  // Engagement — battle-card
  BATTLECARD_VIEWED_AT_LEAST_ONCE_BY_T_48: -1,
  BATTLECARD_NOT_VIEWED_BY_T_24:            +1,

  // Cumulative engagement reward (caps to discourage gaming)
  ENGAGEMENT_FULLY_COMPLETE_BY_T_24:        -1,
  // = INTAKE_COMPLETE && PAM HIGH or MODERATE && all 3 surveys submitted (any tier)
  // && video viewed >= 1 && battle-card viewed >= 1
} as const;
```

Three rules govern combination:

1. **Mutual exclusion within a category.** "Not started by T-72" replaces "not started by T-96" rather than stacking. Same for video "not viewed by T-24" replacing "not viewed by T-48". Implemented by deriving the *current state* from event streams and emitting a single contributor per category at re-tier time.
2. **No double-counting across categories.** Surveys and PAM are separate; intake completion is separate from intake-disclosure hard escalators.
3. **Caps.** Total upgrade contribution from the soft sum is capped at +12 (irrelevant once a hard escalator fires; relevant for tier mapping when multiple negative engagement signals stack).

### 5.4 Delta → tier mapping

```ts
// /lib/triage/preop-retier.mapping.ts
export function applyDeltaWithGuard(
  initial: Tier,
  initialWasHard: boolean,
  delta: number
): Tier {
  // Upgrades
  if (delta >= 6)  return upgrade(initial, 2);
  if (delta >= 3)  return upgrade(initial, 1);

  // Downgrades — guarded
  if (delta <= -3 && !initialWasHard) return downgrade(initial, 1);

  // No change
  return initial;
}
```

The asymmetry is intentional. Upgrades require less evidence than downgrades because false-low is dangerous and false-high is just slightly more labor — same asymmetry the post-op PRD enforces. Hard-escalator initial tiers are sticky and cannot be downgraded; soft-scored initial tiers can drop by at most one step regardless of how positive the delta is.

### 5.5 Worked examples

**A. Tier 1 stays Tier 1.** Initial T1 (soft), HIGH PAM, intake complete, T-96 green, T-48 green, video viewed 2×, battle-card viewed 1×.
delta = (-3) + (-1) + 0 + 0 + (-1) + (-1) = -6. delta ≤ -3 → downgrade by 1, but T1 is the floor → stays T1.

**B. Tier 2 → Tier 1 (downgrade allowed).** Initial T2 (soft), HIGH PAM, intake complete, T-96 green, T-48 green, T-24 green, video viewed 4×, battle-card viewed 1×, fully-complete reward.
delta = (-3) + (-1) + 0 + 0 + 0 + (-1) + (-1) + (-1) + (-1) = -8 (capped); mapping: ≤ -3 → downgrade. initialWasHard=false → T2 → T1.

**C. Tier 2 stays Tier 2 (sticky hard).** Initial T3 by hard escalator (e.g., dialysis). Same engagement as Example B. delta = -8 (would downgrade), but `initialTierWasHardEscalator=true` → guard blocks → stays T3.

**D. Tier 1 → Tier 2 (intake never completed).** Initial T1, intake not started by T-72 (+3), PAM not completed by T-72 (+2), T-96 missed (+2), video not viewed by T-48 (+1).
delta = +8 → upgrade by 2 → TIER_3.

**E. Tier 1 → Tier 3 hard (re-tier escalator).** Initial T1. Intake completes; interview discloses patient lives alone with no caregiver. Re-tier hard escalator `INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER` → TIER_3.

**F. T-24 red survey.** Initial T1. Patient submits T-24 with active fever symptom (red flag). `SURVEY_RED_FLAG_CRITICAL` hard escalator → TIER_3 regardless of other engagement.

These six cases are encoded as fixtures in `/lib/triage/__tests__/preop-retier.fixtures.ts` and asserted by unit tests.

### 5.6 Cadence

Re-tier runs:

- **Synchronously on every signal commit:** intake section completed, intake submitted, PAM submitted, survey submitted, video play event, battle-card view event.
- **At scheduled checkpoints** (cron in `backend/preop_survey.py` schedule extended): T-96, T-72, T-48, T-24, T-0. Catches the *absence* of signals (missed survey, intake not started by T-72, etc.) which has no event to trigger on.
- **On manual coordinator request** (button in the queue's tier card).

Re-tier writes only when the computed tier differs from the current tier; otherwise it writes a `PREOP_RETIER_RECOMPUTED_NO_CHANGE` audit event with the inputs (so we have a record of having looked) and exits.

---

## 6. Engagement tracking specifications

### 6.1 Pre-op video — multi-view tracking

The existing implementation in `frontend/pre-op.js` logs `preop_video_watched` once on first play. This PRD upgrades it.

- Emit `preop_video_played` on every distinct play session (gap between events ≥ 60s defines a new session).
- Emit `preop_video_completed` when playback reaches ≥ 90% of duration.
- Persist event payload `{ source: 'preop_page', session_id, playback_position, playback_rate, duration }`.
- Add `event_logs` table query helpers: `countDistinctVideoSessions(patientId)`, `lastVideoSessionAt(patientId)`.
- The re-tier reader treats "view count" as `countDistinctVideoSessions` (not raw play events).

### 6.2 Battle-card — view tracking (new)

Currently the battle-card is a static HTML render with no telemetry.

- Add `battlecard_viewed` event emitted client-side on first scroll past 25% of card height OR on a 5-second visible-on-screen dwell, whichever comes first.
- Subsequent views require a 30-minute gap (deduplicate accidental refreshes).
- Persist `{ source: 'preop_page', dwell_ms, scroll_depth_pct }`.
- Reader: `countBattlecardViews(patientId)`, `lastBattlecardViewAt(patientId)`.

### 6.3 Why count, not just boolean

The author requested view counts. In re-tier weights, counts feed the `VIDEO_VIEWED_3_OR_MORE_BY_T_48` contributor — a mid-engagement marker that one view does not satisfy. Counts also feed coordinator surfaces ("watched 4 times — high engagement") and downstream analytics for tuning.

### 6.4 Acceptance criteria for engagement tracking

- **AC-6.1** Multiple video sessions in a single day produce distinct events when separated by ≥ 60s; rapid scrubs do not.
- **AC-6.2** A 95%-watched event records `preop_video_completed` exactly once per session.
- **AC-6.3** Battle-card view records exactly once per 30-minute window per patient.
- **AC-6.4** Reader helpers return correct counts under concurrent inserts (transactional read).

---

## 7. Integration with the rest of the system

### 7.1 Initial tier remains immutable

This feature **never** writes `Episode.initialTier`, `Episode.initialTierReasons`, `Episode.initialTierScore`, or any other initial-tier snapshot field. It writes `Episode.tier`, `Episode.tierLastChanged`, `Episode.tierLastChangedBy='SYSTEM:PREOP_RETIER'`, plus a re-tier-specific snapshot row (`PreOpReTierResult`) and a `TriageEvent`.

### 7.2 Sticky-hard guard wiring

A boolean `initialTierWasHardEscalator` is required. The initial-triage feature already records reason `kind: 'HARD' | 'SOFT' | 'BASE'` per contributing reason; the boolean is `reasons.some(r => r.kind === 'HARD')` and is materialized as a column for cheap lookup.

### 7.3 Surfacing in the coordinator queue

The queue PRD already renders tier with a small override badge. We add:

- A small **"re-tiered"** badge on the tier card when current tier differs from initial tier, with a tooltip showing the top three reasons.
- A "Last re-tier: 12 min ago" timestamp.
- A "Recompute now" affordance behind the kebab menu.

### 7.4 Surfacing in the patient-facing app

No clinical reasons or tier numbers are surfaced to the patient. Engagement nudges remain in their existing form (intake reminder, survey reminder, video reminder), but the patient app does not display "you are Tier 2."

### 7.5 Coordinator advisory when intake reveals new clinical facts

When the intake form parser detects a clinical fact that was not in the chart (new active problem, new med, allergy class change), the system writes a `CLINICAL_BASELINE_REVIEW_NEEDED` advisory on the episode. This does NOT auto-rerun the initial-tier model in v1. The coordinator sees an advisory card on the queue with "Intake disclosed new comorbidity — review initial tier?" and a manual "rerun initial tier" action that triggers `assignInitialTier` with the augmented inputs. New "rerun initial tier" is captured as a separate `INITIAL_TIER_REASSIGNED` event distinct from this PRD's events.

---

## 8. Data model (Prisma)

```prisma
model Episode {
  // ... existing fields, including initialTier* from initial-triage-v1.md

  // Initial-tier hard-escalator flag for the sticky guard
  initialTierWasHardEscalator  Boolean   @default(false)

  // Latest re-tier snapshot (denormalized for queue performance)
  preOpReTierLastRunAt         DateTime?
  preOpReTierLastDelta         Int?
  preOpReTierLastTier          Tier?
  preOpReTierTopReasons        Json?     // string[] (top 3, for queue tooltip)
  preOpReTierVersion           String?   // e.g., 'preop-retier@1.0.0'
  preOpReTierTuningVersion     Int?

  // Relations
  pamAssessments               PamAssessment[]
  preOpReTierEvents            PreOpReTierEvent[]
}

model PamAssessment {
  id                String   @id @default(cuid())
  episodeId         String
  responses         Json     // PamResponse[]
  rawSum            Int
  itemsScored       Int
  rawAverage        Float
  activationScore   Float
  level             PamLevel
  isComplete        Boolean
  completedAt       DateTime?
  createdAt         DateTime @default(now())
  episode           Episode  @relation(fields: [episodeId], references: [id])

  @@index([episodeId, createdAt])
}

enum PamLevel { LOW MODERATE HIGH }

model PreOpReTierEvent {
  id                  String   @id @default(cuid())
  episodeId           String
  triggeredBy         String   // 'SIGNAL:<type>' | 'CHECKPOINT:<T-N>' | 'MANUAL:<userId>'
  inputsSnapshot      Json     // PreOpReTierInput, frozen at compute time
  initialTier         Tier
  initialTierWasHard  Boolean
  computedDelta       Int
  computedTier        Tier
  tierBefore          Tier
  tierAfter           Tier
  changed             Boolean
  reasons             Json     // ReTierReason[]
  modelVersion        String
  tuningVersion       Int
  createdAt           DateTime @default(now())

  episode             Episode  @relation(fields: [episodeId], references: [id])
  @@index([episodeId, createdAt])
}

// Reused: existing TriageEventType enum gains:
enum TriageEventType {
  // ... existing
  PREOP_RETIER_RECOMPUTED_NO_CHANGE
  PREOP_RETIER_TIER_UPDATED
  PREOP_RETIER_HARD_ESCALATOR_FIRED
  PREOP_RETIER_DOWNGRADE_BLOCKED_STICKY
  PAM_PROXY_COMPLETED
  CLINICAL_BASELINE_REVIEW_NEEDED
  PREOP_VIDEO_VIEW
  BATTLECARD_VIEW
}
```

A `PreOpReTierEvent` is written on every re-tier call, regardless of whether the tier changed. This provides a tight audit trail of "we looked, here's what we saw, here's what we did."

---

## 9. API contracts

### 9.1 Compute (preview)

**`POST /api/triage/preop-retier/compute`** — pure compute; used by the coordinator's "Recompute now" affordance and by the dev tooling.

```ts
// Request
{
  episodeId: string,
  // Optional override of inputs for what-if previews
  overrideInputs?: Partial<PreOpReTierInput>
}
// Response 200
{
  initialTier: Tier,
  initialTierWasHard: boolean,
  delta: number,
  computedTier: Tier,
  reasons: ReTierReason[],
  inputsSnapshot: PreOpReTierInput,
  modelVersion: string,
  tuningVersion: number
}
```

### 9.2 Apply (persist)

**`POST /api/episodes/:episodeId/preop-retier/run`** — runs the algorithm and persists the result. Called by:
- Signal-handler webhooks (intake submitted, survey submitted, etc.)
- Checkpoint cron jobs
- The "Recompute now" UI action

```ts
// Request
{
  triggeredBy: 'SIGNAL:<type>' | 'CHECKPOINT:T-96' | 'CHECKPOINT:T-72' | 'CHECKPOINT:T-48' | 'CHECKPOINT:T-24' | 'CHECKPOINT:T-0' | 'MANUAL'
}
// Response 200
{
  episode: Episode,
  reTierEvent: PreOpReTierEvent,
  changed: boolean
}
```

### 9.3 PAM submission

**`POST /api/episodes/:episodeId/pam`** — persist PAM proxy responses (called by the intake submit handler when section 3.5 is finalized).

```ts
// Request
{ responses: PamResponse[] }
// Response 201
{ assessment: PamAssessment }
// Side-effect: triggers a synchronous re-tier
```

### 9.4 Engagement events

**`POST /api/events/preop-video`**

```ts
{ episodeId: string, sessionId: string, durationSec: number, completedSession: boolean }
```

**`POST /api/events/battlecard`**

```ts
{ episodeId: string, dwellMs: number, scrollDepthPct: number }
```

Both write to `event_logs`. The video endpoint dedups within a 60s window by `sessionId`; the battle-card endpoint dedups within a 30-minute window by `episodeId`. Both trigger a re-tier when the event is durably persisted.

### 9.5 Tuning

**`GET /api/triage/tuning/preop-retier/current`** — current weights, thresholds, model version.

**`POST /api/triage/tuning/preop-retier`** — admin-only, deploy new tuning config (creates a `TuningConfig` row, bumps `version`).

---

## 10. UI surfaces

### 10.1 Coordinator queue tier card (additions to existing card)

```
┌──── COMPUTED TIER ──────────────────────────────────────────────┐
│  ▮ TIER 2  (was Tier 1 at upload)        re-tier @ 12 min ago    │
│                                                                   │
│  Top reasons:                                                     │
│   • PAM activation MODERATE  (+1)                                 │
│   • T-48 survey orange       (+1)                                 │
│   • Video not viewed by T-48 (+1)                                 │
│   • Intake complete          (-1)                                 │
│   • Net delta: +2 (no change threshold)                           │
│                                                                   │
│  [ ⟲ Recompute now ]   [ ⓘ View full re-tier history ]            │
└─────────────────────────────────────────────────────────────────┘
```

Stays consistent with the override and accept controls owned by the initial-tier review screen — those still surface, but for a *re-tiered* tier the "auto-assigned tier" is shown as a secondary chip rather than the headline.

### 10.2 Patient-facing app — engagement nudges

Surface unchanged; we deliberately do not show tier or scoring to the patient. The intake reminder, survey reminder, video reminder, and battle-card prompt continue to fire from existing schedule logic. The PAM section is added inside the existing intake interview as section 3.5 with neutral language ("Your readiness for surgery").

### 10.3 Acceptance criteria

- **AC-10.1** Tier card renders within 2s of opening a patient detail when a re-tier snapshot exists.
- **AC-10.2** Re-tier badge appears only when `tier !== initialTier`.
- **AC-10.3** "Recompute now" returns a new tier card render within 2s and writes a `PreOpReTierEvent`.
- **AC-10.4** Re-tier history view lists all `PreOpReTierEvent` rows in reverse chronological order with delta, reasons, and tier transitions.
- **AC-10.5** Patient-facing surfaces never display tier or score values.
- **AC-10.6** All UI states meet WCAG 2.1 AA.

---

## 11. Tuning config

`tuning.json` gains a `preopRetier` block. Loaded by `/lib/triage/tuning.ts` (existing module from initial-triage PRD), reloaded on file change.

```json
{
  "preopRetier": {
    "version": 1,
    "modelVersion": "preop-retier@1.0.0",
    "weights": { "...": "see §5.3 PREOP_RETIER_WEIGHTS" },
    "delta": { "upgrade1Min": 3, "upgrade2Min": 6, "downgrade1Max": -3 },
    "softCap": 12,
    "checkpointHours": [96, 72, 48, 24, 0],
    "videoSessionGapSec": 60,
    "videoCompletionPct": 90,
    "battleCardDedupMinutes": 30,
    "pamCutoffs": { "low": 55.1, "moderate": 67.0 },
    "stickyHardGuard": true
  }
}
```

Every change mints a new `TuningConfig` row; computed re-tiers are stamped with the version that produced them.

---

## 12. Component / file structure

```
/app
  /triage
    [episodeId]
      retier-history/page.tsx        # full re-tier event log per episode

  /api
    /triage
      /preop-retier
        /compute/route.ts            # POST §9.1
    /episodes
      /[episodeId]
        /preop-retier
          /run/route.ts              # POST §9.2
        /pam/route.ts                # POST §9.3
    /events
      /preop-video/route.ts          # POST §9.4
      /battlecard/route.ts           # POST §9.4

/components
  /preop-retier
    ReTierBadge.tsx                  # the small "re-tiered" chip
    ReTierReasonsList.tsx
    ReTierHistoryTable.tsx
    RecomputeNowButton.tsx
  /pam
    PamSection.tsx                   # the 13-item block embedded in intake
    PamScaleInput.tsx

/lib
  /triage
    preop-retier.ts                  # main entry — reTierPreOp()
    preop-retier.weights.ts
    preop-retier.mapping.ts          # applyDeltaWithGuard, upgrade/downgrade
    preop-retier.hard.ts             # evaluateReTierHardEscalators()
    preop-retier.delta.ts            # computePreOpDelta()
    preop-retier.cadence.ts          # checkpoint scheduling
    pam-proxy.ts                     # scorePam()
    intake-disclosures.ts            # extracts re-tier-relevant facts from intake form_data_json

/jobs
  preop-retier-checkpoint.ts         # cron: T-96/T-72/T-48/T-24/T-0 sweeps

/__tests__
  preop-retier.spec.ts
  preop-retier.fixtures.ts           # Examples A–F
  pam-proxy.spec.ts
  intake-disclosures.spec.ts
  preop-retier.cadence.spec.ts
  weights.invariants.spec.ts         # mutual-exclusion, no-double-count, cap

/backend  (existing Python; minimal additions)
  preop_survey.py                    # extend to publish per-window tier events to /api/events/preop-survey-result
  intake_section_chat.py             # add PAM section (3.5) handling
  intake_form_parser.py              # parse PAM block + extract disclosure flags
```

The Python additions are minimal: emit events into the existing `event_logs` table that the Next.js side reads; the actual re-tier algorithm lives on the Next.js/Prisma side as specified.

---

## 13. Edge cases (enumerated)

1. **Patient submits PAM, then resubmits later with different responses.** Both stored; latest complete wins for re-tier. Audit shows both.
2. **Intake interview surfaces a hard escalator (lives alone, no caregiver) at T-90, then patient secures a caregiver and updates intake at T-30.** Both events recorded; re-tier at T-30 reads the *latest* intake state and clears the disclosure. Tier may downgrade subject to the sticky guard. The earlier hard fire is preserved in audit.
3. **Survey scoring code (existing `preop_survey.py`) marks a window red on a non-critical item.** That maps to `SURVEY_T_xx_RED` (+3) only — does NOT trigger the `SURVEY_RED_FLAG_CRITICAL` hard escalator. The hard escalator requires the existing scorer's `red=true` on a critical-flagged item.
4. **Video viewed 0× by T-48 and then 4× at T-30.** Mutual-exclusion rule applies: the latest state at re-tier time is "viewed ≥ 3 by T-48" — but T-48 has already passed, so the contributor is `VIDEO_VIEWED_AT_LEAST_ONCE_BY_T_72` if the *first* view happened before T-72; otherwise just `VIDEO_VIEWED_AT_LEAST_ONCE` (no time-window contributor) and the missed-by-T-48 contributor still applies. Tuning specifies that engagement contributors evaluate against actual timestamps, not "as of now."
5. **Battle-card viewed but not video.** Treated independently; battle-card contributor fires, video missed-by contributors still fire.
6. **PAM partially completed (8 of 13 items, 5 N/A).** `isComplete=false` because `itemsScored < 10`. Re-tier treats PAM as not completed — applies the not-completed-by penalty. Patient is prompted to finish on next intake visit.
7. **PAM result is exactly 55.1 or 67.0.** Boundary handling: `score <= 55.1` is LOW; `55.1 < score <= 67.0` is MODERATE; `score > 67.0` is HIGH.
8. **Surgery rescheduled** (e.g., delayed by 7 days). The `episode.surgeryDate` change re-anchors all checkpoint times. Already-fired checkpoint events remain in audit; future checkpoints recompute against the new date. The system does NOT retroactively un-fire missed-window contributors that fired against the old date.
9. **Surgery cancelled.** Episode closed; re-tier becomes inert. Existing audit preserved.
10. **Patient marked as not requiring intake (rare; e.g., sub-acute add-on).** Intake-related contributors short-circuit to 0; surveys and engagement still apply.
11. **Concurrent re-tier calls** (simultaneous signal arrivals). Postgres advisory lock keyed to `episodeId` serializes re-tiers. Last winner's snapshot is authoritative; both `PreOpReTierEvent` rows persisted.
12. **Tuning config update mid-window.** New computations use new tuning version; in-flight re-tier uses whatever it loaded at start. Tier card shows tuning version stamp.
13. **Initial tier was overridden by the coordinator.** The override is the effective initial tier; `initialTierWasHardEscalator` reflects the *original* algorithmic basis (not the override). If the coordinator overrode an algorithmic Tier 3 (hard) down to Tier 2, the sticky guard still applies because the underlying clinical condition (e.g., dialysis) hasn't changed — re-tier cannot downgrade below Tier 2 (which is the override floor in this case). Document this decision; alternative interpretations are reasonable but more complex.
14. **`SURVEY_RED_FLAG_CRITICAL` fires at T-48 and patient resolves it before T-24** (e.g., NPO violation reported at T-48, patient confirms resolution at T-24). Re-tier hard fires at T-48. At T-24, the snapshot reads the latest survey state (no current red flag) — but the hard at T-48 is preserved in audit; the live tier may downgrade subject to the guard. This is the correct behavior: the hard escalator dominates *while it's true*, and the recompute model lets it lift cleanly once it isn't.
15. **Patient never installs the app.** No video, no battle-card, no surveys submitted, intake interview happens via clinic touchpoint. All engagement-missed contributors fire; tier likely upgrades. This is the intended behavior.
16. **Race: two simultaneous PAM submissions.** Latest write wins by `createdAt`; both rows persist. Re-tier reads the latest *complete* result.
17. **Coordinator triggers "Recompute now" with no new signals since last re-tier.** A new `PreOpReTierEvent` writes with `changed=false`; tier unchanged; audit reflects the explicit recompute.

---

## 14. Build order

1. **Schema** — Episode additions (`initialTierWasHardEscalator`, `preOpReTierLast*`), new tables (`PamAssessment`, `PreOpReTierEvent`), new enum values.
2. **`pam-proxy.ts`** with full unit-test coverage of `scorePam`.
3. **PAM section UI** (`PamSection.tsx`, `PamScaleInput.tsx`) and intake interview integration (section 3.5).
4. **Intake form parser additions** — parse PAM block, extract disclosure flags, write `PamAssessment`.
5. **`intake-disclosures.ts`** — derive re-tier hard-escalator flags from intake `form_data_json`.
6. **Engagement event endpoints** — `/api/events/preop-video`, `/api/events/battlecard`; client emitters in `frontend/pre-op.js`.
7. **Engagement readers** — `countDistinctVideoSessions`, `countBattlecardViews`, etc., reading from `event_logs`.
8. **`preop-retier.weights.ts` and `preop-retier.delta.ts`** — pure soft-delta computation with mutual-exclusion and cap rules.
9. **`preop-retier.hard.ts`** — re-tier hard escalator evaluator.
10. **`preop-retier.mapping.ts`** — `applyDeltaWithGuard`, including sticky logic.
11. **`preop-retier.ts`** — main `reTierPreOp` orchestrator.
12. **Worked-example fixtures and unit tests** — Examples A–F plus §13 edge cases.
13. **`/api/episodes/:episodeId/preop-retier/run` endpoint** — Postgres advisory lock per episode, persists snapshot + event.
14. **`/api/triage/preop-retier/compute` endpoint** — pure compute, no persist.
15. **PAM endpoint** — `/api/episodes/:id/pam`; on success triggers re-tier.
16. **Signal-handler webhooks** — intake submit, survey submit, video event, battle-card event all call the run endpoint.
17. **Checkpoint cron** — `preop-retier-checkpoint.ts` walks all active episodes at T-96/T-72/T-48/T-24/T-0 and triggers re-tier.
18. **Tuning config** — extend `tuning.json` with the `preopRetier` block; tuning APIs.
19. **Coordinator queue UI** — `ReTierBadge`, `ReTierReasonsList`, `RecomputeNowButton`, history page.
20. **Synthetic load test** — 50 patients across all 6 worked-example shapes; verify deterministic outputs and queue stability.

---

## 15. Acceptance criteria summary

- [ ] PAM-style proxy renders inside intake interview as section 3.5; requires ≥10 non-N/A items to submit.
- [ ] `scorePam` produces deterministic activation scores and LOW/MOD/HIGH levels matching §4.2 cutoffs.
- [ ] Re-tier algorithm rebuilds from `initialTier` + signals on every call (idempotent).
- [ ] Re-tier hard escalators force TIER_3 regardless of initial.
- [ ] Soft delta sums with mutual-exclusion within categories and cap at ±12.
- [ ] Delta thresholds: ≥+6 = upgrade 2 steps, ≥+3 = upgrade 1, ≤−3 = downgrade 1 (subject to guard).
- [ ] Sticky guard blocks downgrade below `initialTier` when `initialTierWasHardEscalator` is true.
- [ ] Video tracking emits per-session events and supports view counts.
- [ ] Battle-card view tracking exists and dedupes within 30 minutes.
- [ ] Re-tier runs synchronously on signal commit AND at T-96 / T-72 / T-48 / T-24 / T-0 cron checkpoints.
- [ ] Every re-tier writes a `PreOpReTierEvent` regardless of whether tier changed.
- [ ] Tier card shows re-tier badge, top reasons, last-run timestamp, and recompute action.
- [ ] Patient-facing surfaces never display tier, score, or activation level.
- [ ] Tuning config swap does not corrupt in-flight re-tiers; new compute uses new version atomically.
- [ ] All worked examples (§5.5) and edge cases (§13) covered by tests.
- [ ] WCAG 2.1 AA on all new UI.

---

## 16. References (clinical anchors)

- Hibbard JH, Mahoney ER, Stockard J, Tusler M. *Development and testing of a short form of the Patient Activation Measure.* Health Services Research, 2005. — Foundation for the 13-item structure and the 4-point activation framing. We use the published cutoffs (47.0 / 55.1 / 67.0) for level binning while keeping the items as a surgery-focused proxy that does not reproduce the licensed instrument.
- Hibbard JH, Greene J. *What the evidence shows about patient activation: better health outcomes and care experiences.* Health Affairs, 2013. — Evidence base for the magnitude of activation as a risk modifier; supports the high weight assigned to PAM in the soft delta.
- Khuri SF et al., NSQIP foundational publications — supports the engagement-and-self-management dimension of perioperative risk that the chart cannot capture.
- Existing in-repo behavioral references:
  - `backend/preop_survey.py` — survey scoring rules, red-flag semantics, window cutoffs.
  - `backend/intake_section_chat.py` and `backend/intake_form_parser.py` — intake interview structure and form data shape.
  - `frontend/pre-op.js` — current pre-op video and battle-card render path; basis for the engagement-tracking additions.

---

## 17. Glossary

- **Re-tier** — recomputation of `Episode.tier` after the initial assignment, driven by pre-op signals.
- **Soft delta** — signed integer summed from upgrade/downgrade contributors; mapped to a tier change relative to the initial tier.
- **Sticky hard guard** — rule that prevents downgrade below `initialTier` when the initial tier was set by a hard escalator.
- **Re-tier hard escalator** — condition that forces TIER_3 regardless of soft delta or initial tier.
- **Idempotent recompute** — every re-tier rebuilds from initial + current signal state; no event-by-event mutation.
- **PAM-style proxy** — 13-item, 4-point activation instrument modeled on PAM-13, embedded in the intake interview.
- **Engagement counts** — distinct session counts for the pre-op video and view counts for the battle-card.
- **Checkpoint** — scheduled T-96 / T-72 / T-48 / T-24 / T-0 cron run that captures absence-of-signal upgrades.
- **Mutual exclusion within a category** — only the most-specific contributor in a category fires (e.g., "not started by T-72" replaces, not stacks with, "not started by T-96").

---

*End of PRD v1.0 — Pre-Op Re-Tiering*
