# 03 — Master prompt for the Foundry AIP "AI FDE" agent

> **How to use:** Everything ships in `archangel_foundry_submission.zip`. Unzip it into
> one Foundry folder so the layout in `INDEX.md` is preserved (`instructions/` and
> `data/` siblings). In Foundry, open AI FDE (AIP enabled; Global Branching enabled —
> required for the Ontology edits and the Risk Model Lab), point the session at that
> folder, then paste the prompt below. AI FDE works in **modes** (data integration →
> ontology editing → app building) and **skills**; this prompt drives it through them in
> order. Run it on a branch named `risk-lab`. Build the **Phase-1** section only.

---

## PROMPT — paste verbatim into AI FDE

You are the AI FDE building **Archangel Episode OS**: a post-discharge accountability
platform for CMS **TEAM** surgical episodes (LEJR, hip/femur fracture, spinal fusion,
CABG, major bowel), ported onto the Foundry Ontology from an existing, validated
FastAPI product. **Do not invent clinical logic** — port the supplied schemas and
ladders exactly. Your inputs are in this folder (unzipped from
`archangel_foundry_submission.zip`); start by reading `INDEX.md` at the folder root —
it lists every file, its path, its target object type, the join keys, and the enum
dictionary. The 16 source CSVs are in `data/`; the specs are in `instructions/`. Treat
`instructions/01_BUILD_ORDER.md` as authoritative for object/link/action/function/module
shape and dependency order, and `instructions/00_RECONCILIATION_AND_DATA_DICTIONARY.md`
for exact property names, enums, the file→object map (§B), and the hero acceptance test
(§D). Use `instructions/02_PIPELINE_BUILDER_TRANSFORMS.md` when wiring ingestion. Work on
branch `risk-lab`. Build **only the Phase-1 scope** below; stub nothing outside it.

**Mode 1 — Data integration.** Create Pipeline Builder pipeline `archangel_ingest`
implementing `02_PIPELINE_BUILDER_TRANSFORMS`. Land all 16 CSVs as Ontology objects
with the links in `01_BUILD_ORDER` STEP 3. Enforce enums verbatim from the repo:
`procedure_family∈{LEJR,CABG,SPINAL_FUSION,HIP_FEMUR_FRACTURE,MAJOR_BOWEL}`,
`current_tier∈{TIER_1,TIER_2,TIER_3}`, `scored_tier∈{GREEN,ORANGE,RED}`, incision/
red-flag chip codes and G-codes exactly as listed. Cast types as specified.

**Mode 2 — Ontology editing.** Create the object types, properties, and link types in
`01_BUILD_ORDER` STEP 2–3 and STEP 6, with `SurgicalEpisode` as the central object.
Add derived properties `spend_to_date = Σ linked CostEvent.amount` and
`margin_remaining = target_price − spend_to_date` (and `margin_at_risk = margin_remaining < 0`).
If live derived-aggregation is constrained on this tenant, materialize via the
`episode_spend` transform AND recompute inside the cost-writing Actions, so margin
updates the instant a nurse acts.

**Functions.** Port two pure Functions: `scoreDailyCheckin` (from `score_daily_checkin`)
and `reTierPostOp(snapshot, riskModelVersion)` (from `re_tier_post_op` — it MUST accept
a RiskModelVersion argument; carry the delta cap = 12, upgrade thresholds 3/6, and the
eight hard escalators). Then `replayRiskModel(candidate)` which runs `reTierPostOp`
across all SurgicalEpisodes (or the labeled-readmit subset), compares proposed tiering
to `readmitted_label`, and writes ONE `RiskModelComparison` object with
`caught_earlier`, `false_escalations_added`, `sensitivity`, `specificity`, and
`alarm_burden_per_nurse_per_week` versus the PROMOTED version. **Do not implement the
replay as a Scenario** — Scenarios show forked object states, not metrics, and
function-backed action batches cap at 20; the comparison must be a Function that writes
an object. Use the `risk-lab` branch purely for provenance/promotion.

**AIP Logic.** Create `noteToRiskFlags`: from a SurgicalEpisode's intake note + latest
DailyCheckin, emit `RiskFlag` objects each carrying an exact `evidence` source span and
a `grounding_verdict∈{PASS,BLOCK,REVIEW}`. Grounding-gate it: on BLOCK, regenerate once,
then mark REVIEW. This is the guarantee that a flag is verifiable, not an LLM assertion.

**Mode 3 — Actions (function-backed, audited, role-gated).** Create the Action types in
`01_BUILD_ORDER` STEP 7 with EXACTLY these role rules (from the repo's `auth_roles`):
writes allowed for `rn_coordinator` and `surgeon`; `np_pa` is read-only; `OverrideRiskTier`
adds `medical_director`; `ProposeRiskModelVersion`/`PromoteRiskModelVersion` are
`medical_director`-gated (promote = `medical_director` ONLY). `StartTelehealthVisit` must
map the G-code via the established/new ladder and **reject any claim with more than one
line item (ride-alone)**. `DispatchHomeHealth` creates an Intervention + a CostEvent
(+ ClaimLine) and triggers the margin recompute. Every Action writes an audit record.

**Workshop.** Build three modules over these objects:
1. **Screen A — Nurse worklist**: SurgicalEpisode list **sorted by `current_tier` first,
   `margin_remaining` only as a tie-breaker** (clinical risk is lexically prior; money
   never gates care). Each row expands to its RiskFlag-with-evidence and exposes
   `AcknowledgeEscalation`, `PlaceVoiceCall`, `StartTelehealthVisit`, `DispatchHomeHealth`
   inline.
2. **Screen B — Exec dashboard**: aggregate `margin_remaining`, projected won/lost, and
   cost drivers by complication / surgeon / post-acute setting; show routed
   ReconciliationReports.
3. **Risk Model Lab** (branch-aware): edit a candidate RiskModelVersion, run
   `replayRiskModel`, render the RiskModelComparison panel vs PROMOTED, and expose
   `PromoteRiskModelVersion`.

**Automate.** Trigger on `SurgicalEpisode.episode_status → CLOSED`: AIP Logic builds a
`ReconciliationReport` (actual vs target, scaled by `cqs_score`, downside gated by
`track`: Track 1 = no PY1 downside, Track 3 = full), and `RouteReport` routes by outcome
(SAVED → vbc_exec; BLOWN → cfo/service-line).

**Acceptance — the hero episode must work end to end:** `EP-0009` (Patricia Clark,
TIER_3 LEJR, Spanish, lives alone, target $21,800). Its Day-12 DailyCheckin scores RED
with evidence "yellow drainage on the bandage" (`BAD_SMELL|OPENING_OR_GAPING`), raising
an Escalation that surfaces at the top of Screen A. `PlaceVoiceCall` (Spanish) →
`DispatchHomeHealth` writes a $168 CostEvent + ride-alone ClaimLine; `margin_remaining`
drops by $168 live on both Screen A and Screen B. The episode closes under target and
Automate routes a SAVED ReconciliationReport ($4,576 saved, CQS 0.745) to vbc_exec.
Verify each step before declaring done.

### Phase-1 scope (BUILD)
Patient · SurgicalEpisode · ActiveProblem · Medication · DailyCheckin · EngagementSignal ·
TierAssessment · RiskFlag · Escalation · Intervention · ClaimLine · CostEvent ·
RiskModelVersion · RiskModelComparison · ReconciliationReport · CareTeamMember; the 9
Actions; Functions `scoreDailyCheckin` / `reTierPostOp` / `replayRiskModel` /
`computeMargin`; AIP Logic `noteToRiskFlags`; Screens A/B + Risk Model Lab; the close
Automate.

### Phase-2 scope (DO NOT BUILD — vision only)
Allergy object; Surgeon object-view; AIP Agent live voice (ElevenLabs/Twilio/Tavus/Daily)
— Phase-1 writes a `transcript_summary` stub instead; `EligibilityDetermination` object
(the 6 TEAM checks + SAVE_AS_TEAM/SAVE_AS_STANDARD); two-way patient messaging; real
EHR/claims transmission (837/835 are notional); wound-photo CV (already excluded in the
repo). Do not create these.

---

## The one loop to demo, and why everything else is cut

**Demo loop (≈2.5 min of the 4):** nurse opens **Screen A** → riskiest TEAM episode
(`EP-0009`) → **RiskFlag with the check-in evidence span** → `PlaceVoiceCall` → patient
reports red-flag drainage → `DispatchHomeHealth` (CostEvent + ride-alone ClaimLine) →
**`margin_remaining` updates live** on Screen B → episode later CLOSES → **Automate**
emits the reconciliation report routed to the VBC exec. Bookend with 15s of TEAM/
late-reconciliation problem framing and a 30s "under the hood: one Ontology — same
objects power the nurse's worklist, the CFO's dashboard, and the model lab; every button
is an audited Action; the risk engine is my validated code running as a Function."

**Why this loop:** it is the only path that touches all four seats (RN acts, money moves
for the CFO, the System reconciles) over the *single* object model — which is the entire
thesis — and it makes the ethics concrete (clinical tier sorts first; the $168 dispatch
visibly defends a ~$12k readmission).

**Defending the cut:** Feature A (Risk Model Lab) is the strongest *governance* story but
it is a second protagonist (medical director) and a separate cognitive frame; showing a
replay + promote inside a 4-min cut would split attention and burn the live-margin
payoff. Keep it as a **20-second "and the same Ontology lets us branch-test a new risk
model against real history before promoting it"** B-roll over the Risk Model Lab, not a
lived flow. The AIP Agent live voice, EHR write-back, and eligibility object are
infrastructure the demo asserts but does not need to execute on camera — they add risk
(latency, external deps) without advancing the single accountable loop. Everything cut is
either a different user, a different screen, or an external dependency; none of it is on
the critical path from "nurse sees risk" to "bundle saved, CFO sees it."
