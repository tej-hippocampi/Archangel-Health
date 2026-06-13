# 01 — Concrete Build Order for the AI FDE (dependency-ordered)

Maps 1:1 to PRD §9 milestones. Each step names the exact Foundry surface.
Cut line for the 4-min demo is in `03_AI_FDE_PROMPT.md` §Phase-2. Everything
below the **PHASE-1 LINE** is required for the hero flow; everything under
**PHASE-2** is vision and is explicitly out of the build.

Legend: 🟦 Ontology · 🟩 Function · 🟨 AIP Logic · 🟧 Action · 🟪 Workshop · ⬛ Automate · 🟫 Pipeline Builder

---

## STEP 0 — Branch (PRD §5 Feature A governance frame) — milestone 5 prep
- Create Foundry branch `risk-lab` (Global Branching). All RiskModelVersion edits
  and the replay happen here; merge-to-main = the promote. *Branching covers
  Ontology Manager + Workshop + Pipeline Builder, so this is the unifying branch.*

## STEP 1 — Land the cohort 🟫 — **milestone 1**
- Pipeline Builder pipeline `archangel_ingest` (logic in `02_PIPELINE_BUILDER_TRANSFORMS.md`).
- Ingest the 16 CSVs in `../data/`; output Ontology objects + links below.
- Set `episode_status` and `window_end` from the CSV (already computed).

## STEP 2 — Object types 🟦 — **milestone 1** (dependency order)
1. `CareTeamMember` (no deps) — roles `rn_coordinator | surgeon | medical_director | vbc_exec | np_pa` (repo `auth_roles.py` + 2 net-new per recon §A).
2. `RiskModelVersion` (no deps) — `tuning_version(int, PK)`, `status∈{DRAFT,CANDIDATE,PROMOTED}`, `weights(json)`, `hard_thresholds(json)`, `created_by`, `created_at`.
3. `Patient` — `patient_id(PK)`, `mrn`, `age`, `gender`, `preferred_language`, `needs_interpreter`, `health_system_id`, `lives_alone`, `has_reliable_caregiver`.
4. `SurgicalEpisode` *(central)* — `episode_id(PK)`, `patient_id(FK)`, `anchor_cpt`, `procedure_family(enum)`, `track(int)`, `admit_date`, `discharge_date`, `window_end`, `target_price(double)`, `current_tier(enum)`, `episode_status(enum OPEN/CLOSED)`, `cqs_score(double)`, `cqs_hwr_input`, `cqs_psi90_input`, `cqs_propm_input`, `readmitted_label(bool)`. Derived props added in STEP 6.
5. `ActiveProblem`, `Medication` — child rows keyed by `episode_id`.
6. `DailyCheckin` — full repo item set (`postop/types.py`), `scored_tier(enum)`, `incision_flags`, `red_flag_symptoms`, `free_text` (evidence source).
7. `EngagementSignal`, `TierAssessment`, `RiskFlag`, `Escalation`, `Intervention`, `ClaimLine`, `CostEvent`, `ReconciliationReport`.

## STEP 3 — Link types 🟦 — **milestone 1** (PRD §4.2)
- `Patient —has→ SurgicalEpisode` (1:n)
- `SurgicalEpisode —includes→ ActiveProblem | Medication` (1:n)
- `SurgicalEpisode —has_checkin→ DailyCheckin | EngagementSignal` (1:n)
- `SurgicalEpisode —assessed_by→ TierAssessment —scored_with→ RiskModelVersion`
- `SurgicalEpisode —cites→ RiskFlag` (RiskFlag —evidenced_by→ DailyCheckin)
- `SurgicalEpisode —raised→ Escalation —resolved_by→ Intervention —produces→ ClaimLine`
- `SurgicalEpisode —accrues→ CostEvent`  ← drives the rollup
- `SurgicalEpisode —closes_into→ ReconciliationReport —routed_to→ CareTeamMember`
- `CareTeamMember —owns→ Patient | Escalation`

## STEP 4 — Functions 🟩 — **milestone 2**
- `scoreDailyCheckin(answers) → {tier, red_flags, wound_concern, new_red_flag_symptom}`
  — TS/Python port of `score_daily_checkin` (`scoring/daily_checkin.py:84-138`). Pure.
- `reTierPostOp(snapshot, modelVersion) → {proposed_tier, delta, hard_escalator_fired, reasons[]}`
  — port of `re_tier_post_op` (`postop/algo.py:23-69`); **takes RiskModelVersion as an arg**
  so it can be replayed against any candidate. Carries `POSTOP_DELTA_CAP=12`, thresholds 3/6,
  the 8 hard escalators (`postop/tuning.py`).
- `replayRiskModel(modelVersion) → RiskModelComparison` *(Feature A engine — see STEP 8)*.
- `computeMargin(episode) → double` — `target_price − Σ linked CostEvent.amount` (STEP 6).

## STEP 5 — AIP Logic 🟨 — **milestone 2**
- `noteToRiskFlags`: intake_note + latest DailyCheckin → `RiskFlag[]` with `evidence`
  (exact source span) and `grounding_verdict∈{PASS,BLOCK,REVIEW}`. Grounding-gated,
  mirroring `pipeline/grounding_gate.py` — on BLOCK, regenerate once then flag REVIEW.
  This is the "verifiable flag, not an LLM assertion" guarantee.

## STEP 6 — Derived properties / rollup 🟦 — **milestone 4** (Feature B)
- `SurgicalEpisode.spend_to_date` = aggregation: Σ `CostEvent.amount` over `accrues`.
- `SurgicalEpisode.margin_remaining` = `target_price − spend_to_date`.
- `SurgicalEpisode.margin_at_risk_flag` = `margin_remaining < 0`.
- Implement as Ontology derived properties backed by `computeMargin` (recompute on
  CostEvent write). Free-tier note: if live derived-aggregation is constrained,
  materialize via Pipeline Builder nightly + recompute-on-Action (see transforms doc).

## STEP 7 — Function-backed Action types 🟧 — **milestone 3** (role rules + side effects)
All replace the hand-rolled `locks.py` / `audit/middleware.py`; every Action is audited.

| Action | Role rule (repo `auth_roles`) | Side effects |
|---|---|---|
| `AcknowledgeEscalation` | `WRITE_CLINICAL` = {rn_coordinator, surgeon} | Escalation.resolved/assigned_to |
| `PlaceVoiceCall` | rn_coordinator, surgeon | create Intervention(VOICE); trigger AIP Agent voice flow (ElevenLabs/Twilio); write `transcript_summary` |
| `StartTelehealthVisit` | rn_coordinator, surgeon | create Intervention(TELEHEALTH)+ClaimLine via `map_gcode`; **`enforce_ride_alone` — reject >1 line** (`gcodes.py:66`) |
| `DispatchHomeHealth` | rn_coordinator, surgeon | create Intervention(HOME_HEALTH)+CostEvent(+ClaimLine) → triggers margin rollup |
| `SendIntervention` | rn_coordinator, surgeon | outbound email Intervention |
| `OverrideRiskTier` | surgeon, medical_director | TierAssessment override + **required reason string** (mirror eligibility override `evaluate.py:101`) |
| `ProposeRiskModelVersion` | medical_director | RiskModelVersion(status=CANDIDATE) on branch |
| `PromoteRiskModelVersion` | **medical_director only** | status→PROMOTED; merge `risk-lab` branch |
| `FinalizeReconciliationReport` / `RouteReport` | system/Automate; vbc_exec ack | finalize + route by role |

`np_pa` = read-only on all of the above (repo `ALL_CLINICAL` read, excluded from `WRITE_CLINICAL`).

## STEP 8 — Risk Model Lab replay 🟩🟪 — **milestone 5** (Feature A, rewritten)
**Do NOT rely on Scenarios to compute the comparison.** Scenarios apply Actions and
show forked object *states*; they do not compute sensitivity/specificity, and
function-backed action batches cap at 20 calls unless batched-execution is configured.
Instead:
- `replayRiskModel(candidate: RiskModelVersion)`: iterates all ~300 (or the 51-readmit
  labeled subset) SurgicalEpisodes, runs `reTierPostOp(snapshot, candidate)`, compares
  the proposed tier-over-time against `readmitted_label`, and writes ONE
  `RiskModelComparison` object: `caught_earlier`, `false_escalations_added`,
  `sensitivity`, `specificity`, `alarm_burden_per_nurse_per_week`, vs PROMOTED.
- Run it inside the `risk-lab` branch for the governance story; `PromoteRiskModelVersion`
  merges. The branch gives provenance; the Function gives the metrics. This is fully
  buildable on a free AIP Developer tenant.

## STEP 9 — Workshop modules 🟪 — **milestones 3,4,5**
- **Screen A — Nurse worklist**: SurgicalEpisode object-list, **sorted by `current_tier`
  first (clinical), `margin_remaining` as tie-breaker only** (PRD §10.2 ethics: money
  never gates care). Row → RiskFlag w/ evidence → the 4 Actions inline.
- **Screen B — Exec dashboard**: aggregations of `margin_remaining`, projected won/lost,
  cost drivers by complication / surgeon / post-acute setting; receives routed reports.
- **Risk Model Lab**: branch-aware; edit candidate RiskModelVersion → run `replayRiskModel`
  → comparison panel → `PromoteRiskModelVersion`.
- **Surgeon object view** (Phase 2 unless time).

## STEP 10 — Automate ⬛ — **milestone 6**
- Trigger: `SurgicalEpisode.episode_status → CLOSED`.
- Effect: AIP Logic builds `ReconciliationReport` (actual vs target, CQS-scaled,
  Track-gated downside), `RouteReport` routes by outcome/role.

---
### Milestone → step map (PRD §9)
1 → STEP 1-3 · 2 → STEP 4-5 · 3 → STEP 7 + Screen A · 4 → STEP 6 + Screen B ·
5 → STEP 0,8 + Risk Model Lab · 6 → STEP 10 · 7 (demo cut) → `03_AI_FDE_PROMPT.md`.
