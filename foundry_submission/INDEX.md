# INDEX — archangel_foundry_submission.zip (read me first)

This zip contains everything to build **Archangel Episode OS** on Foundry. Unzip
into a single Foundry folder; the relative paths below are stable so the AI FDE
agent and Pipeline Builder can resolve every reference.

```
archangel_foundry_submission/
├── INDEX.md                      ← this file (manifest + read order)
├── README.md                     ← upload order + how to regenerate data
├── instructions/
│   ├── 00_RECONCILIATION_AND_DATA_DICTIONARY.md   ← property names, enums, data dictionary, hero episode
│   ├── 01_BUILD_ORDER.md                          ← AUTHORITATIVE build spec (objects/links/actions/functions/modules)
│   ├── 02_PIPELINE_BUILDER_TRANSFORMS.md          ← CSV → Ontology transform logic
│   └── 03_AI_FDE_PROMPT.md                         ← PROMPT to paste into AI FDE
└── data/                          ← 16 object-shaped CSVs (no PHI) + manifest
    ├── _manifest.json             ← row counts + _hero_episode_id (EP-0009)
    ├── patients.csv               (300)   → Patient
    ├── surgical_episodes.csv      (300)   → SurgicalEpisode  [CENTRAL]
    ├── intake_notes.csv           (300)   → AIP Logic input (noteToRiskFlags)
    ├── active_problems.csv        (907)   → ActiveProblem
    ├── medications.csv            (769)   → Medication
    ├── daily_checkins.csv         (8265)  → DailyCheckin
    ├── engagement_signals.csv     (1876)  → EngagementSignal
    ├── cost_events.csv            (792)   → CostEvent       [drives margin rollup]
    ├── tier_assessments.csv       (600)   → TierAssessment
    ├── risk_flags.csv             (113)   → RiskFlag
    ├── escalations.csv            (113)   → Escalation
    ├── interventions.csv          (2)     → Intervention
    ├── claim_lines.csv            (1)     → ClaimLine
    ├── reconciliation_reports.csv (222)   → ReconciliationReport
    ├── risk_model_versions.csv    (2)     → RiskModelVersion (PROMOTED v2, CANDIDATE v3)
    └── care_team_members.csv      (5)     → CareTeamMember
```

## Read order for the AI FDE agent
1. `instructions/03_AI_FDE_PROMPT.md` — the instruction set (paste this as your prompt).
2. `instructions/01_BUILD_ORDER.md` — authoritative for object/link/action shape + order.
3. `instructions/00_RECONCILIATION_AND_DATA_DICTIONARY.md` — exact property names, enums,
   the file→object map (§B), and the hero episode acceptance test (§D).
4. `instructions/02_PIPELINE_BUILDER_TRANSFORMS.md` — only when wiring Pipeline Builder.

## Join keys (every CSV resolves to SurgicalEpisode)
- `patients.csv.patient_id` ↔ `surgical_episodes.csv.patient_id`
- every other CSV carries `episode_id` ↔ `surgical_episodes.csv.episode_id`
- `tier_assessments.csv.tuning_version` ↔ `risk_model_versions.csv.tuning_version`
- `claim_lines.csv.intervention_id` ↔ `interventions.csv.intervention_id`
- `reconciliation_reports.csv.routed_to` (role) ↔ `care_team_members.csv.role`

## Enum dictionary (verbatim from the repo — enforce on ingest)
- `procedure_family` ∈ {LEJR, CABG, SPINAL_FUSION, HIP_FEMUR_FRACTURE, MAJOR_BOWEL}
- `current_tier` ∈ {TIER_1, TIER_2, TIER_3}
- `scored_tier` (DailyCheckin) ∈ {GREEN, ORANGE, RED}
- `episode_status` ∈ {OPEN, CLOSED}
- `risk_model_versions.status` ∈ {DRAFT, CANDIDATE, PROMOTED}
- `interventions.channel` ∈ {VOICE, TELEHEALTH, HOME_HEALTH, EMAIL}
- `cost_events.category` ∈ {SURGERY, DRUG, ED_VISIT, READMISSION, HOME_HEALTH, TELEHEALTH, SNF, OUTPATIENT}
- pipe-delimited arrays: `daily_checkins.incision_flags`, `daily_checkins.red_flag_symptoms` (split on `|`)
- G-codes (`claim_lines.hcpcs_gcode`): NEW G0660–G0664 / ESTABLISHED G0665–G0668; `type_of_bill=13X`, `revenue_code=0780`, ride-alone = one line item only.

## Hero episode (acceptance test): `EP-0009`
Patricia Clark, TIER_3 LEJR, Spanish, lives alone, target $21,800. Day-12 RED
check-in → escalation → PlaceVoiceCall → DispatchHomeHealth ($168 CostEvent +
ride-alone G0667 ClaimLine) → margin updates live → closes under target →
ReconciliationReport (SAVED $4,576, CQS 0.745) routed to vbc_exec. Detail in `00` §D.
