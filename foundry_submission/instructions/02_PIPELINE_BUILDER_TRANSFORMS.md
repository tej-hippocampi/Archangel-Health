# 02 — Pipeline Builder transform logic (CSV → Ontology)

One pipeline `archangel_ingest`. Inputs = the 16 CSVs in `../data/`. Build on the
`risk-lab` branch (Branching covers Pipeline Builder). Each transform below is a
node; "→ Ontology output" means "Add an Ontology output" mapping columns to
object properties (no reshaping needed — CSVs are already object-shaped).

## Direct object loads (column → property, 1:1)
- `patients.csv` → **Patient** (PK `patient_id`). Cast `lives_alone`,`needs_interpreter`,`has_reliable_caregiver` to boolean.
- `surgical_episodes.csv` → **SurgicalEpisode** (PK `episode_id`). Cast `target_price`→double, `track`→int, `readmitted_label`→bool, dates→date, `cqs_*`→double.
- `active_problems.csv` → **ActiveProblem** (surrogate PK = hash(`episode_id`+`icd10`)).
- `medications.csv` → **Medication** (surrogate PK = hash(`episode_id`+`rxnorm_code`)).
- `daily_checkins.csv` → **DailyCheckin** (PK `checkin_id`). Split pipe-delimited `incision_flags`,`red_flag_symptoms` into string arrays.
- `engagement_signals.csv` → **EngagementSignal** (PK = hash(`episode_id`+`date`)).
- `cost_events.csv` → **CostEvent** (PK `cost_event_id`). Cast `amount`→double, `is_readmission`→bool.
- `tier_assessments.csv` → **TierAssessment** (PK `assessment_id`). Parse `reasons` JSON→struct array.
- `risk_flags.csv` → **RiskFlag** (PK `flag_id`). Add `grounding_verdict='PASS'` literal for seeded rows (AIP Logic sets it live).
- `escalations.csv` → **Escalation** (PK `escalation_id`). Cast `resolved`→bool. Note: `assigned_to` is net-new (recon §A).
- `interventions.csv` → **Intervention** (PK `intervention_id`).
- `claim_lines.csv` → **ClaimLine** (PK `claim_id`). Cast `duration_min`→int, `ride_alone_ok`→bool.
- `reconciliation_reports.csv` → **ReconciliationReport** (PK `report_id`). Cast `target_price`,`actual_spend`,`delta`,`projected_payment`,`cqs_score`→numeric.
- `risk_model_versions.csv` → **RiskModelVersion** (PK `tuning_version`). Keep `weights_json`,`hard_thresholds_json` as strings (parsed by Functions).
- `care_team_members.csv` → **CareTeamMember** (PK `member_id`).

## Link wiring (in the Ontology output config)
Resolve foreign keys to links:
- Patient.`patient_id` ← SurgicalEpisode.`patient_id`  ⇒ `has`
- SurgicalEpisode.`episode_id` ← every child table's `episode_id` ⇒ `includes`/`has_checkin`/`accrues`/`raised`/`cites`/`assessed_by`/`closes_into`
- TierAssessment.`tuning_version` (from `model_version`/`tuning_version`) ← RiskModelVersion ⇒ `scored_with`
- Escalation.`escalation_id` ← Intervention.(derive: link interventions whose `intervention_id` shares episode + within window) ⇒ `resolved_by`; Intervention.`intervention_id` ← ClaimLine.`intervention_id` ⇒ `produces`
- ReconciliationReport.`routed_to` (role string) ← CareTeamMember.`role` ⇒ `routed_to`

## Derived-property materialization (Feature B fallback)
If live Ontology derived-aggregation is constrained on the free tier, add a
transform `episode_spend`:
```
cost_events
  → GROUP BY episode_id  → spend_to_date = SUM(amount)
  → JOIN surgical_episodes ON episode_id
  → margin_remaining = target_price - spend_to_date
  → margin_at_risk = margin_remaining < 0
  → write back onto SurgicalEpisode (incremental)
```
Schedule incrementally; ALSO recompute inside `DispatchHomeHealth`/`StartTelehealthVisit`
Action side-effects so the nurse sees margin move live without waiting for the batch.

## Notional 837/835 feed (PRD §7, optional realism)
`cost_events.csv` already encodes the 837/835-shaped stream (category, amount,
is_readmission). If you want Pipeline Builder's notional-dataset generation for a
denser claims feed, fan `cost_events` out to one row per service line keyed to
`claim_lines` (type_of_bill `13X`, revenue_code `0780`) — not required for the hero flow.
