# 00 — Ontology Reconciliation + Data Dictionary

Source of truth for properties = the `Archangel-Health` repo Pydantic schemas.
This file maps every Ontology object type to the repo type/endpoint it ports
from, flags what is **net-new** (invented in the PRD with no code basis), and
documents the CSVs in `../data/`. Citations are `file:line` in the repo.

## A. Object-type reconciliation (PRD §4.1 vs repo)

| Ontology object | Repo basis (file:line) | Verdict | Notes |
|---|---|---|---|
| **Patient** | `triage_demo_seed.py:48+`, `team_store` patient rows; `health_system_id` is real (tenant) | **REUSE** | `preferred_language`/`needs_interpreter` come from `SocialHistoryInput.primary_language/needs_interpreter` (`triage/types.py:149-150`), not a Patient field today — fold them onto Patient. |
| **SurgicalEpisode** *(central)* | `ProcedureInput` (`triage/types.py:53-61`): `cpt_code`, `anchor_procedure_family`, `scheduled_date`. Eligibility `track`/episode is **NOT** in code. | **PARTIAL / MOSTLY NEW** | `anchor_cpt`, `procedure_family` = REUSE. `target_price`, `spend_to_date`, `margin_remaining`, `track`, `window_end`, `cqs_inputs`, `episode_status` are **NET-NEW** (no cost/episode econ in repo — confirmed exhaustively, §C). Keep — this is the porting thesis. |
| **TierAssessment** | `TierAssignment` (`triage/types.py:29-39`): `tier`,`score`,`reasons`,`model_version`,`tuning_version`; `TierReason` (`:21-26`): `kind∈{HARD,BASE,SOFT}`,`code`,`label`,`weight` | **REUSE (exact)** | PRD says `tuning_version` baseline=1; **repo live is `TUNING_VERSION=2`, `MODEL_VERSION="postop-retier@1.1.0"`** (`postop/tuning.py:19-20`). Data uses the real versions. |
| **RiskFlag** | Closest repo analog: `DailyCheckinScored.red_flags`/`wound_concern` (`postop/types.py:77-82`) + grounding evidence (`pipeline/grounding_gate.py`). No first-class `RiskFlag` table. | **PARTIAL NEW** | `evidence` (source span) is the *right* addition — it mirrors the grounding gate's PASS/BLOCK/REVIEW evidence discipline. `generated_by` = AIP Logic run id (new). Keep. |
| **ActiveProblem** | `ActiveProblem` (`triage/types.py:66-71`): `icd10`,`description`,`status∈{ACTIVE,RESOLVED,CHRONIC}` | **REUSE (exact)** | |
| **Medication** | `Medication` (`triage/types.py:88-95`): `rxnorm_code`,`name`,`dose`,`route`,`frequency` | **REUSE (exact)** | |
| **Allergy** | `Allergy` (`triage/types.py:104-115`) | **REUSE** | Not needed for hero flow → Phase 2. |
| **DailyCheckin** | `DailyCheckinAnswers`+`DailyCheckinScored` (`postop/types.py:52-82`) — all item enums exact | **REUSE (exact)** | `incision_flags`/`red_flag_symptoms` are the verifiable evidence the RiskFlag cites. |
| **EngagementSignal** | `compute_rolling_med_adherence`→`MedAdherenceWindowSummary` (`scoring/med_adherence.py`), `determine_video_flags` (`scoring/video_engagement.py`), `lost_contact_status` (`scoring/lost_contact.py`) | **REUSE** | Collapsed into one signal stream object (med-adherence / video / lost-contact / chat). |
| **Escalation** | `escalations` table (`team_store.py:120-131`): `id`,`patient_id`,`tier(INT)`,`trigger_type`,`message`,`resolved(0/1)`,`created_at`,`consent`,`conversation_snapshot`. Endpoints `GET /api/escalations`, `PATCH /api/escalations/{id}/resolved` (`main.py:2850,2903`). | **REUSE — with corrections** | PRD's `origin` is **derived in the endpoint** (`main.py:2884`), not stored. PRD's `resolved_by`/owner: **there is NO assignment/owner column in the table** — adding `assigned_to` is net-new (fine, but flag it). `tier` is an INT in code; model as enum on the object. |
| **Intervention** | No repo object. Telehealth encounter (`routers/telehealth.py:224+`) is the closest write. | **NEW (justified)** | The unifying write across VOICE/TELEHEALTH/HOME_HEALTH/EMAIL. Keep. |
| **ClaimLine** | `gcodes.py:16-29` ladder + `create_telehealth_claim` (`routers/telehealth.py:519-525`): `hcpcs_code`,`type_of_bill="13X"`,`revenue_code="0780"`,`pos`,`ride_alone`. | **REUSE (exact)** | Ride-alone is real (`enforce_ride_alone`, `gcodes.py:66-68`). |
| **CostEvent** | **Nothing in code.** | **NET-NEW** | Entire episode-economics layer is invented in the PRD. This is the point of the port; just be honest on camera. |
| **RiskModelVersion** | `postop/tuning.py` weights/hard-escalators/thresholds exist as **module constants**, not as a versioned object. | **PARTIAL NEW** | Promote the constants into a first-class object so they can be branched. `status∈{DRAFT,CANDIDATE,PROMOTED}` is new. |
| **ReconciliationReport** | Nothing in code. | **NET-NEW** | Depends on CostEvent + CQS. Phase-1 only as the closing automation. |
| **CareTeamMember** | `auth_roles.py:3-31`: roles = `system_admin`,`surgeon`,`rn_coordinator`,`np_pa`,`patient`. `WRITE_CLINICAL={surgeon,rn_coordinator}`; `np_pa` read-only. | **REUSE — with corrections** | **PRD invents `medical director` and `VBC exec`** — they are NOT in `auth_roles`. Repo's `np_pa` (read-only) is missing from the PRD. Decision: add `medical_director` + `vbc_exec` as net-new roles (needed for Feature A promote-gate + Screen B), keep `np_pa` as read-only. Documented, not silently mapped. |

### What I flag as INVENTED (no code basis) — keep only if defended on camera
- All episode economics: `target_price`, `spend_to_date`, `margin_remaining`, `CostEvent`, `ReconciliationReport`, CQS math. (Core to the thesis — defensible.)
- `track` (1/2/3) — a TEAM/CMS concept, not in repo; needed for downside math.
- Roles `medical_director`, `vbc_exec`.
- `Escalation.assigned_to`/owner and `Intervention` as a first-class object.

### What the CODE has that the PRD UNDER-models (worth adding)
1. **`np_pa` read-only role** (`auth_roles.py`) — your Action role-rules must include a read-only seat or you misrepresent the real RBAC.
2. **Grounding verdict `PASS/BLOCK/REVIEW`** (`grounding_gate.py`) — RiskFlag should carry a `grounding_verdict`, not just `evidence`; that is the actual safety gate in the repo and the strongest "not-an-LLM-assertion" talking point.
3. **TEAM eligibility verdict object** — the 6 checks (`partA_active, partB_active, not_ma, medicare_primary, not_esrd_basis, not_umwa`; `evaluate.py:69-86`), `overall∈{ELIGIBLE,INELIGIBLE,BLOCKED_UNKNOWN}`, finalize `SAVE_AS_TEAM|SAVE_AS_STANDARD` (`eligibility.py:714-750`). The PRD's SurgicalEpisode assumes eligibility already decided; model an `EligibilityDetermination` object (Phase 2) so the episode's TEAM-vs-standard status is itself an audited artifact.
4. **`care_companion` semantic-escalation resolution** (`scoring/care_companion.py`) — the re-tier's `care_companion_*` inputs (`postop/types.py:235-239`) depend on open chat-semantic escalations. Carried as EngagementSignal/Escalation `trigger_type LIKE 'chat:semantic%'`.

## B. Data dictionary (`../data/*.csv` → Ontology)

| CSV | → Object type | Primary key | Key links |
|---|---|---|---|
| `patients.csv` (300) | Patient | `patient_id` | — |
| `surgical_episodes.csv` (300) | SurgicalEpisode | `episode_id` | →Patient (`patient_id`), →intake_note |
| `intake_notes.csv` (300) | (input to AIP Logic) | `note_id` | →episode |
| `active_problems.csv` (907) | ActiveProblem | (`episode_id`,`icd10`) | →episode |
| `medications.csv` (769) | Medication | (`episode_id`,`rxnorm_code`) | →episode |
| `daily_checkins.csv` (8 265) | DailyCheckin | `checkin_id` | →episode |
| `engagement_signals.csv` (1 876) | EngagementSignal | (`episode_id`,`date`) | →episode |
| `cost_events.csv` (792) | CostEvent | `cost_event_id` | →episode (drives rollup) |
| `tier_assessments.csv` (600) | TierAssessment | `assessment_id` | →episode, →RiskModelVersion |
| `risk_flags.csv` (113) | RiskFlag | `flag_id` | →episode, cites DailyCheckin |
| `escalations.csv` (113) | Escalation | `escalation_id` | →episode, →Intervention |
| `interventions.csv` | Intervention | `intervention_id` | →episode, →Escalation, →ClaimLine |
| `claim_lines.csv` | ClaimLine | `claim_id` | →Intervention |
| `reconciliation_reports.csv` (222) | ReconciliationReport | `report_id` | →episode, →routed CareTeamMember |
| `risk_model_versions.csv` (2) | RiskModelVersion | `tuning_version` | scores episodes |
| `care_team_members.csv` (5) | CareTeamMember | `member_id` | owns Patient/Escalation |

**Enum fidelity** (verbatim from repo): `procedure_family∈{LEJR,CABG,SPINAL_FUSION,HIP_FEMUR_FRACTURE,MAJOR_BOWEL}` (`triage/types.py:44-50`); `current_tier∈{TIER_1,TIER_2,TIER_3}`; `scored_tier∈{GREEN,ORANGE,RED}`; incision/red-flag chip codes per `postop/types.py:30-49`; G-codes per `gcodes.py`.

## C. Hard finding: episode economics is 100% net-new
Exhaustive repo search found **no** `target_price`, `margin`, `spend`, `CostEvent`, or cost field anywhere. The only money-shaped code is the telehealth G-code biller (`gcodes.py`, `routers/telehealth.py`) which emits CMS-837-shaped **claim drafts**, never transmitted. Feature B is therefore a genuine net-new capability the Foundry port unlocks — frame it that way, do not imply the app already computes margin.

## D. Hero episode for the demo
`EP-0009` — Patricia Clark, 63F, **Spanish** (routes the voice agent), **lives alone**, **TIER_3 LEJR**, target $21,800. Day-12 DailyCheckin scores **RED** with evidence *"Pain getting worse and I see yellow drainage on the bandage"* + `BAD_SMELL|OPENING_OR_GAPING`. Escalation → `PlaceVoiceCall` (Spanish) → `DispatchHomeHealth` (CostEvent $168 + ride-alone G0667 ClaimLine) → no readmission → episode CLOSES under target → ReconciliationReport (SAVED $4,576, CQS 0.745) routes to VBC_EXEC. The $168 dispatch visibly defended a ~$12k readmission.
