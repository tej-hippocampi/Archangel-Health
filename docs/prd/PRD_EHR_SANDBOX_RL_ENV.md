# PRD: Nephrology EHR Sandbox (ENV-EHR)

**An RL environment built from real de-identified outpatient records, graded against what the treating doctor did, with physician review where the chart cannot decide**

- **Status:** Draft v1, ready for build.
- **Date:** 26 September 2026.
- **Owner:** Tej (product).
- **Builder:** Claude Code.
- **Auditor:** a fresh-context agent, per `AGENTS.md`.
- **Scope:** extends the existing ENV track (`backend/asclepius/environments/`) and the existing zip ingestion door. It does not change the single-turn portal.

---

## 0. How to use this document (read first, Claude Code)

1. Read `AGENTS.md`, then this PRD in full, then the files cited in §4. Citations are `file:line` and were checked on the date above. If one has drifted, fix the PRD, never the code (`AGENTS.md`, "How to work here").
2. Build **one milestone at a time** (§12). Each milestone ends with exit checks. Stop and report after each one: the files you created, the tests you added with pass counts, and every `# EHR-QUESTION:` comment you left.
3. **Never weaken an invariant in §5 to make a test pass.** If one looks impossible, stop and ask.
4. **Third-party software.** §7 lists every outside package and container, with the exact install command and whether it is required or optional.
   - Install only what the current milestone needs.
   - Guard optional imports so the suite still passes without them.
   - Never vendor a third-party repo into this tree.
   - Nothing in the default test suite may need Docker or network access.
5. **No real data in the repo or in tests.** Fixtures are synthetic. Build them with the Synthea commands in §7, or hand-write them. Real partner zips only ever come through the ingestion door.
6. Tests run keyless (`ASCLEPIUS_LLM_PROVIDER=fake`, see `AGENTS.md`, "Sandbox facts"). Every LLM call added here needs a `purpose` string so `ai/fake_llm.py` can answer it deterministically.

---

## 1. What we are building, in one picture

```
 Omics-de-identified .zip from the nephrology EHR (PDF worksheets, C-CDA XML, CSV, text)
        │
        ▼  POST /api/asclepius/partner/uploads   (existing door; new C-CDA + PDF adapters)
 ingest_cases  (one de-identified ClinicalCase per patient, relative day offsets, quarantine on doubt)
        │
        ▼  chart_builder   (NEW)
 ehr_charts: one FHIR R4 chart per patient
    + worksheet extraction → past orders, med changes and assessments per visit
        │
        ▼  visit_compiler  (NEW)   one visit = one task
 ehr_tasks: for visit k, the sandbox shows everything BEFORE visit k (plus decoy patients).
            Visit k's worksheet is SEALED as the answer key.
            Visit k+1 is SEALED as the outcome.
        │
        ▼  FhirSandbox + tools  (NEW)   the agent searches, calculates, orders, writes a note, finishes
 ehr_rollouts: trajectory + the sandbox's end state (every write the agent made)
        │
        ▼  checkpoint grader  (NEW)
 4 checkpoints per visit: RETRIEVE · REASON · ACT · DOCUMENT   (+ safety hard-fail gate)
        │
        ├── ACT or REASON disagrees with the doctor ─────────┐
        ├── visit k+1 shows a bad outcome (doctor may be wrong) ├─► ehr_reviews → routed to a
        └── 10% random sample of rubric-graded notes ─────────┘   network nephrologist ($25)
        │                                                        (2nd reviewer if unsure)
        ▼
 final reward per rollout · internal eval report per model · lab package (Docker + OpenEnv)
```

**The thesis:** the treating doctor's next move is the answer key, and the next visit grades the doctor. Where the agent and the doctor disagree, or the next visit says the doctor may have been wrong, a nephrologist decides. That review is the product nobody else can build without a physician network.

---

## 2. Decisions already made (Tej, 26 Sep 2026)

| # | Decision | Answer |
|---|---|---|
| D1 | Build on | **Extend** `backend/asclepius/environments/` and the existing ingestion door. No new service. |
| D2 | What is in the zip | **Mixed or unknown.** Handle PDF (text and scanned), C-CDA/CCD XML, CSV and plain text. Detect the format per file. |
| D3 | How the agent works in the EHR | **API tools first.** FHIR-shaped tool calls, the MedAgentBench/PhysicianBench style. A clickable UI (Medplum) is a later milestone and optional. |
| D4 | When a physician reviews | **(a)** The agent disagrees with the doctor. **(b)** The next visit suggests the doctor's action was wrong. **(c)** A random sample of rubric-graded free text, per D5. |
| D5 | Free text | **Structured tools, a rubric, and sampling.** Code grades structured actions. Free-text notes are graded by an LLM against a physician-written rubric. 10% of those gradings go to a physician to keep the grader honest. |
| D6 | One task | **One visit = one task.** The chart is shown up to visit k. The agent makes visit k's decisions. The answer key is visit k's worksheet. Visit k+1 is the outcome. |
| D7 | De-identification | **Omics de-identifies before upload.** The pipeline re-scans and fails closed. Raw PHI is out of scope. |
| D8 | Delivery | **Internal eval first**, then a lab package: a Docker image with a standard reset/step interface (OpenEnv). |
| D9 | Reviewers per disagreement | **One, plus a second when unsure.** A second reviewer is added when the first marks low confidence, says the doctor was wrong, or flags harm. A third breaks a tie. |
| D10 | Review pay | **$25 per completed review.** |
| D11 | Agent writes the visit note | **Yes.** It is the DOCUMENT checkpoint. |
| D12 | Decoy patients | **Yes, from v1.** Every episode loads 2 to 3 look-alike patients from the same practice. |


---

## 3. Research basis: how sandbox environments are built, and where EHR agents break

This section explains *why* the design looks the way it does. Every design rule in §5 and §8 traces back to a row here. Sources were read 26 Sep 2026. Numbers come from each paper's own abstract or tables.

### 3.1 What labs expect an environment to be

| Pattern | Where it comes from | What we adopt |
|---|---|---|
| Gym-style `reset` / `step` / `state` server in a Docker container, with tools exposed (increasingly over MCP) | OpenEnv, the Meta-PyTorch + Hugging Face standard now governed by a multi-lab committee: https://github.com/huggingface/OpenEnv . Epoch's survey of what labs buy: "typically delivered as Docker containers" made of actions, context and tasks: https://epoch.ai/gradient-updates/state-of-rl-envs | `ClinicalEnv` already mirrors Gymnasium (`asclepius/environments/env.py:30`). Add an OpenEnv-compatible HTTP server and a Docker image (M6). |
| Environment as a pip module with `load_environment()` and a weighted rubric | Prime Intellect `verifiers`: https://docs.primeintellect.ai/verifiers/environments | Ship a thin `verifiers` adapter in the lab package (M6). |
| Task JSONL plus a grader, separate from the tool server | NVIDIA NeMo Gym: https://github.com/NVIDIA-NeMo/Gym . OpenAI Agent RFT has the customer host tool and grader endpoints, with a per-rollout id so tool state stays per trajectory: https://developers.openai.com/api/docs/guides/graders | Tasks are rows in `ehr_tasks`, exported as JSONL. Every sandbox session is keyed by `rollout_id`. |
| **Grade the end state, not the transcript** | τ-bench compares the final DB to a gold DB: https://arxiv.org/pdf/2406.12045 . AppWorld runs state-based unit tests and also checks "collateral damage": https://arxiv.org/abs/2407.18901 . MedAgentBench issue #10 reports that write tasks are graded from transcript text, never from FHIR server state (unconfirmed by maintainers): https://github.com/stanfordmlgroup/MedAgentBench/issues/10 | The ACT checkpoint reads the sandbox's write log (`ehr_rollouts.writes_json`), never the agent's prose (invariant I4). |
| **Checkpoint scoring** with partial credit | TheAgentCompany: `0.5·(checkpoints passed / total) + 0.5·full_success`: https://arxiv.org/pdf/2412.14161v1 . PhysicianBench: 670 checkpoints over 100 tasks, graded pass/partial/fail: https://arxiv.org/html/2605.02240v1 | Four checkpoints per visit (§9). Report both the partial score and full success. |
| **A fresh environment per task** | PhysicianBench runs a fresh HAPI FHIR container per task and tears it down after grading. MedAgentBench ships HAPI as `jyxsu6/medagentbench`: https://github.com/stanfordmlgroup/medagentbench | The in-process `FhirSandbox` uses a frozen snapshot plus a per-rollout write overlay. That gives the same isolation at microsecond reset cost. HAPI is kept as the parity reference (§7, M6). |
| **The "do nothing" trap** | "World Feedback for Clinical Agents" (MAB-v3): an agent could pass 41.7% of tasks by doing nothing, GRPO training collapsed into not acting, and a `datetime.now()` bug broke the grader. Fixes: a frozen clock, a 1:1 balance of action and no-action cases, and shaped penalties for unwanted writes and for finishing without using tools: https://arxiv.org/html/2607.01470 | Invariants I3 (frozen clock) and I5 (do-nothing baseline must score low). Task balance is enforced at compile time (§8.5). |
| **Expert audit of every task** | τ²-Bench-Verified fixed broken, ambiguous and wrong-ID tasks: https://github.com/amazon-agi/tau2-bench-verified . WebArena-Verified dropped LLM judges for structural comparison: https://github.com/ServiceNow/webarena-verified | An answer-key audit gate before any task is `ready` (§8.4), plus physician review of disagreements. |
| **Reliability metric** | τ-bench `pass^k`. PhysicianBench: the best pass@1 is 46%, but only 28% pass all 3 runs | The eval report shows pass@1 and pass^3 (§11). |
| Contamination control | Held-out splits and canary strings: HealthBench Professional https://arxiv.org/html/2604.27470v1 , SWE-Bench Pro https://scale.com/blog/swe-bench-pro | Every task gets `split ∈ {train, dev, heldout}`. `heldout` never leaves our servers (I11). Canary string in every exported file. |

### 3.2 Where agents break inside an EHR (and the task that targets it)

Ranked by how often each failure happens times how badly it hurts the patient. The nephrology-specific rows are what our records can test that public benchmarks cannot.

| # | Failure | Evidence | Our design response |
|---|---|---|---|
| F1 | **Omission**: roughly right, but misses the recheck, dose, follow-up or safety step | PhysicianBench: clinical reasoning causes 50.4% of failures, and most are "incomplete reasoning" or a missed detail, not an outright wrong answer. An NHS medication-review study got 100% sensitivity but only 46.9% of patients handled fully correctly; 86% of failures were contextual: https://arxiv.org/html/2512.21127v1 | The ACT checkpoint scores each key item separately. A missed item is a miss, not partial credit (§9.3). |
| F2 | **Trends, thresholds and time windows** | ART benchmark: 100% retrieval, but 28–64% on aggregating a window and 32–38% on thresholds, e.g. "no recent potassium found" when one existed: https://web3.arxiv.org/pdf/2601.08988 . ClinTraceBench (includes CKD): 41.6–55.9% with a compressed history versus 67–71% with the full history: https://arxiv.org/html/2609.01111 | eGFR slope, a 40% decline and 3-month persistence all require reading many visits. Labs are paginated, and evidence sits in middle visits. Probe tasks P1–P3 (§8.6). |
| F3 | **Write gap**: says the action in text but never places the order | MedAgentBench: 85.33% on query tasks versus 54.00% on action tasks for the best model: https://arxiv.org/html/2501.14654v2 . PhysicianBench names renal dose adjustment as a common "output gap" | The DOCUMENT checkpoint cross-checks the note against the orders actually written. Anything in the plan but not ordered is flagged `output_gap` (§9.4). |
| F4 | **Wrong search space, codes or links** | FHIR-AgentBench: the best retrieval precision is 0.35 and recall 0.68. Errors include querying the wrong resource type, not following references, and matching display text instead of codes: https://arxiv.org/html/2509.19319v1 . EHR-ChatQA: 51.9% of consistent failures are brand-vs-generic or synonym linking: https://arxiv.org/html/2509.23415v2 | Real worksheet meds are free text with brand names. Several LOINC codes are used for creatinine. Decisive values sit in DocumentReference worksheets, not only Observations. |
| F5 | **Dosing and calculation** | MedCalc-Bench: GPT-4 scores 50.91%: https://arxiv.org/html/2406.12036v1 . Renal dose adjustment: GPT-5 61.7% accuracy with 54.8% specificity, i.e. it over-adjusts: https://pubmed.ncbi.nlm.nih.gov/41509008/ (abstract only). Potassium dosing: 45% accuracy at 100% stated confidence: https://www.medrxiv.org/content/10.64898/2026.06.02.26354762v1 | The `calculate` tool gives exact formulas. Grading checks the agent's dose against the key with a tolerance. Include cases where "no change" is correct, to penalise over-adjusting (§8.5). |
| F6 | **Format and protocol errors** | MedAgentBench: Gemini 2.0 Flash made invalid actions in 54% of cases. v2: structured tools plus a finish tool lifted the score from 69.67% to 91%: https://psb.stanford.edu/psb-online/proceedings/psb26/chen_eric.pdf | Named, schema-validated tools. Writes are validated server-side and return an OperationOutcome the agent must handle (§11.2). |
| F7 | **Stopping early, running out of budget, or looping** | AgentClinic: accuracy fell from 52% to 25% when turns were cut from 20 to 10: https://arxiv.org/pdf/2405.07960 . MedCUA-Bench: 53.5% of closed-model failures are exploration timeouts: https://arxiv.org/html/2606.03203v1 | Budget of 40 tool calls. `finish_visit` is required. Truncation is recorded as its own failure tag. |
| F8 | **Inconsistent across runs** | PhysicianBench pass@1 46% versus pass-all-3 28%. EHR-ChatQA shows gaps of up to 58.7 points | Report pass^3. |
| F9 | **Hallucinated numbers in notes** | 1.47% of sentences hallucinated, 44% of them major, concentrated in the Plan section: https://www.nature.com/articles/s41746-025-01670-7 | Every number in the agent's note is checked against the chart (§9.4). |
| F10 | **Calibration and escalation** | Potassium dosing at 100% confidence. No direct benchmark of "failure to escalate" was found | `flag_urgent` tool. Safety rules hard-fail a missed escalation when K ≥ 6.0 or eGFR falls fast (§9.5). |
| F11 | **Wrong patient** | MedCUA-Bench scores patient-identity violations as critical. Little other direct evidence | Decoy patients in every episode (D12). Any write to a decoy is a hard fail (I9). |
| F12 | **Out-of-date guidelines** | KDIGO 2024 CKD guideline and 2026 anemia guideline: https://kdigo.org/wp-content/uploads/2026/01/KDIGO-2026-Anemia-in-CKD-Guideline.pdf . This is our inference; no study measures it | Rubric criteria cite the current KDIGO version. Reviewers can mark "doctor followed an older standard". |

**What this means for nephrology.** The highest-yield targets are F2 (trends), F1 (omission after starting an ACEi/ARB, SGLT2 inhibitor or MRA), F3 plus F5 (renal dosing that must actually be ordered), F4 (worksheet med reconciliation) and F10 (hyperkalemia, rapid eGFR decline, dialysis or transplant thresholds). PhysicianBench's nephrology/urology slice scores only 29–33%, so there is headroom.

### 3.3 Closest comparables

- **PhysicianBench** (Stanford, per-task HAPI container, 21 specialties, e-consult records).
- **MedAgentBench v1/v2** (HAPI, 300 + 300 tasks, mostly inpatient-style single-step tasks).
- **EHRGym** (OpenEnv, Synthea patients, an Epic-like UI): https://github.com/adtserapio/EHRGym
- **Collinear SimLab** lists an EMR environment (unverified beyond a directory listing).

**Where we differ:**

1. Real longitudinal *outpatient* charts.
2. An answer key that is the treating doctor's own next move, with the next visit as outcome.
3. Physician adjudication of every disagreement.


---

## 4. What already exists (reuse it; do not rebuild it)

### 4.1 Ingestion (the zip door)

| Piece | Where | Reuse how |
|---|---|---|
| Partner zip upload through a token link: caps, magic bytes, SHA-256, encrypted quarantine write | `partner_upload` at `routers/asclepius.py:6890` | Unchanged. The nephrology zip arrives here. |
| Orchestrator: unpack → classify → adapters → one case per patient → timeline → de-id verify → `ingest_cases` row | `process_upload` at `asclepius/ingestion.py:1800` | Its logic is unchanged. Two new formats flow through it, and the helpers it calls get the edits listed in §8.1 (including the `_classify` call-site head size). |
| File classifier | `_classify` at `asclepius/ingestion.py:851`, called at `asclepius/ingestion.py:1127` with only a 512-byte `head` and a 200-character `text_head` | **Edit**: add `ccda` and `pdf_doc` (§8.1). **Also edit the call site** to pass a 4,096-byte head for `.xml` entries only, because a C-CDA root can sit behind an XML declaration and stylesheet instruction. |
| Fragment merge | `_merge_fragments` at `asclepius/ingestion.py:1175` (its per-key copy loop) | **Edit**: copy the four new collections (§6.1). Without this they are silently dropped. |
| Patient-key minting | `_patient_key_and_source` at `asclepius/ingestion.py:1690` (filename prefix `<patient>__`, else `default`); `_KEY_SOURCE_PRECEDENCE` at `asclepius/ingestion.py:1725`; `unify_patient_keys` at `asclepius/ingestion.py:1729`; the HL7 adapter appends a hashed key to `_patient_keys` at `asclepius/adapters/hl7v2.py:127` | **Edit**: add `ccda` and `pdf_doc` to the precedence tuple. The C-CDA adapter mints a hashed key the same way the HL7 adapter does (§8.1). |
| Adapter registry | `FORMATS` at `asclepius/case_formats.py:204` | **Edit**: register `ccda` and `pdf_doc`. |
| Existing adapters (FHIR R4, HL7v2, lab CSV, note text) | `asclepius/adapters/__init__.py` | Unchanged. New adapters follow the same `parse(raw, *, specialty, manifest)` contract and the same "never copy an identifier" rule. |
| Date → relative-offset conversion | `normalize_timeline` at `asclepius/timeline.py:628` converts each collection **by name**. Structured date keys are declared in `_STRUCTURED_DATE_KEYS` at `asclepius/timeline.py:482`; they feed only the anchor pool and date-order inference. Free-text fields to rewrite are in `_FREE_TEXT_FIELDS` at `asclepius/timeline.py:443` | **Edit all three**: add a conversion block for each new collection inside `normalize_timeline`, add their date keys, and add their free-text fields (§6.1). Adding keys alone converts nothing, and leftover raw dates make `deidentify()` reject the case. |
| Residual-PHI verifier | `verify_deid` at `asclepius/deid_verify.py:179` | Called again at the chart-build boundary and at every sandbox read (I7). |
| Encounter segmentation (a new encounter after a gap of more than 7 days) | `segment_longitudinal_record` at `asclepius/real_cases.py:250` | Used to group same-day items. A visit is keyed to the day of a worksheet (§8.2). |
| Temporal split for one decision point | `build_encounter_case` at `asclepius/real_cases.py:1362`, and `assert_temporal_split` at `asclepius/real_cases.py:2175` | This is the pattern the visit compiler follows. Reuse `assert_temporal_split` on the visible slice. |
| Encrypted sealed answer keys | `sealed_ground_truth` table at `asclepius/store.py:1190`. Crypto helpers `encrypt_field` at `field_crypto.py:71` and `decrypt_field` at `field_crypto.py:87` | Visit keys and outcomes are stored the same way: encrypted, read only by the grader and the review surface. |

### 4.2 The environment track

| Piece | Where | Reuse how |
|---|---|---|
| Gymnasium-style env: `reset` / `step` / `verify`, seeded, with an action cap | `ClinicalEnv` at `asclepius/environments/env.py:30`; `reset` at `asclepius/environments/env.py:87`; `step` at `asclepius/environments/env.py:150` | The new `EhrVisitEnv` **implements the same interface but does not subclass it**. `ClinicalEnv.__init__` rejects tools outside `_TOOL_TABLE` (`_validate_action_space`, `asclepius/environments/env.py:78`), and `step` assumes `EHRState`. Copy the trajectory, step-reward and truncation mechanics. |
| In-memory chart state with a fail-closed temporal gate and PHI re-scrub | `EHRState` at `asclepius/environments/state.py:48`; gate `_item_visible` at `asclepius/environments/state.py:110` | Kept for the old templates. `FhirSandbox` enforces the same rule on FHIR resources (I1), and its tests copy the old gate's fail-closed cases. |
| Tool table (13 tools; actions are built as FHIR but **never stored**) | `_TOOL_TABLE` at `asclepius/environments/tools.py:151`; `ToolRegistry` at `asclepius/environments/tools.py:214` | Unchanged. The new tools live in `ehr_sandbox/tools.py`, and their writes **are** stored (I4). |
| Verifier: deterministic checks, critical-negative hard gate, rubric (RULER) path | `score` at `asclepius/environments/verify.py:525`; reward-hacking probe `probe_hackability` at `asclepius/environments/verify.py:727` | Reuse the composition idea and the hackability probe. The checkpoint grader is new (§9). |
| Compile a case into an env spec | `compile_environment` at `asclepius/environments/compile_env.py:177` | Reference only. Visit compilation has its own module. |
| Rollout driver (JSON-in-text action protocol) | `rollout` at `asclepius/environments/rollout.py:68` | **Unchanged.** The new harness (`ehr_sandbox/harness.py`) adds native tool calling through `call_llm` (`ai/llm_client.py:503`) and imports `_extract_json` for the JSON fallback. |
| Physician annotation of env runs, assignment-gated | `save_annotation` at `asclepius/environments/service.py:161`; `_require_annotation_assignment` at `routers/asclepius_env.py:44` | The review flow copies this gate pattern with its own table and routes. |
| Env HTTP surface | `router` at `routers/asclepius_env.py:37` (`/api/asclepius/environments`) | New routes go on a **new** router, `routers/asclepius_ehr_sandbox.py` (§11). |
| `env_runs` table | `asclepius/store.py:1263` | **Not reused.** The EHR sandbox has its own tables (§6.2). `env_runs` stays byte-identical. |

### 4.3 People, routing and money

| Piece | Where | Reuse how |
|---|---|---|
| Assignments table: idempotent on (task, user, role) | `upsert_assignment` at `asclepius/store.py:5362`; `has_assignment` at `asclepius/case_access.py:81`; `assignment_required` at `asclepius/case_access.py:23` | **Not reused for reviews.** Review rows there would count toward portal allocation load (`open_assignment_counts`, `asclepius/store.py:5424`) and show in the admin assignment list as rows with no task. Copying the case-access gate would also let the open-pool flag or a mock user skip the assignment check, which breaks I12. Reviews get their own `ehr_review_assignments` table (§6.2) and their own gate (§11.6). |
| Physician eligibility for real data and specialty match | `_eligible_to_review` at `asclepius/allocation.py:183` (no agreement check). `Physician` objects are built only in the admin router, at `routers/asclepius_admin.py:3763`. Agreement currency: `require_current_agreement` at `routers/asclepius.py:3655` uses `physician_agreement.gate_enabled` and `resignature_reason` | `reviews.load_candidates(store)` copies the admin construction as a thin helper (with a comment pointing to the original; never import a router). It then applies `_eligible_to_review`, the agreement check, and I12. |
| Email one member | `notify_person` at `notifications.py:244` | Review offers and reminders. |
| Ledger row, un-double-payable through `UNIQUE(kind, ref_id)` | `insert_earning` at `asclepius/store.py:16301`; kinds start at `KIND_TASK` (`asclepius/payments.py:144`); display labels in `_KIND_LABELS` at `asclepius/payments.py:2085` | New kind `KIND_EHR_REVIEW = "ehr_review"` plus a `_KIND_LABELS` entry ("EHR review"). `ref_id` is the `ehr_review_assignments` id. $25. |

### 4.4 The gaps this PRD fills

1. **No C-CDA or PDF adapter.** Office Ally exports exactly these formats (§7.3).
2. **No visit, order or med-change model.** `ClinicalCase` (`asclepius/cases.py:345`) holds labs, notes, problems, meds and studies, but not "what the doctor ordered at this visit".
3. **Agent actions are never written anywhere.** `ToolRegistry.execute` returns a resource and forgets it. You cannot grade an end state that was never stored.
4. **No search semantics.** No LOINC codes, date ranges, pagination or multiple patients.
5. **No review routing for disagreements.**
6. **No lab-ready package.**

---

## 5. Invariants (design rules the build must never break)

Every invariant has at least one test in §13, named `test_ehr_I<n>_...`.

- **I1: Time is sealed on the server.** The sandbox for visit k holds only resources dated at or before the visit-k decision instant, minus anything authored *at* visit k (§8.2). Anything with unknown timing is withheld (fail closed, as in `asclepius/environments/state.py:110`). The visit-k answer key and every visit-k+1 item are **absent from the sandbox process**, not filtered at read time. The sandbox is built from a pre-sliced snapshot, so no code path can reach the future.
- **I2: The answer key never reaches the agent.** It is not in the prompt, not in any tool result, not in `info`, and not in the exported task row. It is stored encrypted (`field_crypto`) and decrypted only inside the grader and the review view.
- **I3: The clock is frozen.** `now` inside a rollout is the task's `decision_instant`. No code under `ehr_sandbox/` may call `datetime.now()`, `date.today()` or `time.time()` for clinical logic. There is a lint test for this. Real wall-clock time is used only for audit timestamps, through the single helper `ehr_sandbox.constants.audit_now()`, which wraps `store._utcnow_iso`.
- **I4: Writes are graded from sandbox state, never from prose.** ACT reads the rollout's write overlay. A write the server rejected (invalid resource) does not exist. Text saying "I would order X" earns nothing on ACT.
- **I5: Doing nothing must fail.** For every task set, the scripted `noop` agent (reads the chart, calls `finish_visit`, writes nothing) must score a mean reward ≤ 0.15. The `oracle` agent (replays the doctor's key as tool calls) must score ≥ 0.90. Both are checked in CI on the synthetic fixture set, and again at the first real-data run (after M5). A task set that fails either check cannot be marked `ready`. The oracle check excludes `doctor_flagged` tasks (the doctor's own key trips a safety rule, §9.5) until their review resolves.
- **I6: Determinism.** The same task, seed and action sequence give byte-identical observations, writes and deterministic checkpoint scores. Search results have a stable sort order (tie-break on resource id).
- **I7: No PHI anywhere.** The input is Omics-de-identified (D7), but the pipeline still:
  - runs `verify_deid` on every chart at build time, quarantining on any finding;
  - re-scrubs every free-text tool result at read time, withholding on a finding;
  - never stores or emits calendar dates from the source. Sandbox dates are *synthetic* dates: a fixed anchor plus the relative offset (§8.3).
- **I8: Decoys are real but unlinkable.** Decoy patients are other de-identified patients from the same upload, time-sliced the same way. Every patient in an episode gets a synthetic display name, synthetic MRN and synthetic birth date (§8.3). These are generated per task from a seed and never derived from source data.
- **I9: A write to the wrong patient is a hard fail.** Any create or update whose `subject` is not the target patient sets `hard_fail = true` with the reason `wrong_patient`.
- **I10: Safety rules hard-fail regardless of the answer key.** A triggered safety rule (§9.5) zeroes the reward, even if the doctor did the same thing. In that case a review is always created (trigger `safety`).
- **I11: The held-out split never leaves.** `split = 'heldout'` tasks are never included in an export or Docker image. The lab package contains `train` and `dev` only.
- **I12: Reviewers are independent.** A reviewer is never:
  - a clinician from the source practice;
  - the same person twice on one review;
  - someone who can see the other reviewer's verdict before submitting.

  The source practice's clinicians are recorded on the upload as an exclusion list (§6.2, `ehr_source_exclusions`). Review routes check for a live `ehr_review_assignments` row for this user on **every** request. There is no open-pool, mock-user or admin-impersonation bypass.
- **I13: The key is fixed at task creation; reviews can only correct it on the record.** The original extracted key is immutable. A review verdict of `doctor_wrong` writes a `key_correction` row. The effective key is the original plus corrections, and both are kept and exported.
- **I14: Money flows only through the ledger.** A review earning is written only through `store.insert_earning` with `kind = 'ehr_review'` and `ref_id = <review assignment id>`. There is exactly one row per completed review assignment.
- **I15: US-sourced data does not go to countries of concern.** An export or package build of US-sourced data (`source_country = 'US'`, set on the upload) must carry three admin-entered attestations, and is refused unless all three pass:
  1. `buyer_jurisdiction` (ISO-2), not on `EHR_EXPORT_BLOCKED_JURISDICTIONS` (default `CN, HK, MO, RU, IR, KP, CU, VE`, the DOJ Data Security Program's countries of concern, 28 CFR Part 202);
  2. `buyer_not_covered_person = true`. Under the DSP, covered-person status turns on ownership, control and residency, not only the buyer's address, so a US or UK entity majority-owned from a country of concern is still blocked;
  3. for any non-US buyer, `onward_transfer_clause = true`, confirming the contract bars onward transfer to countries of concern (the DSP's required contract term for such transactions).

  The DSP covers bulk US health data **even when de-identified**. The "bulk" threshold is a legal question, so this gate applies to every US-sourced package regardless of size. It is a floor, not legal advice. Counsel owns the list and the attestation wording.
- **I16: Never delete data.** Follow `docs/data-safety/POLICY.md`: no `DELETE` in migrations, add columns instead, and snapshot before and after with `scripts/data_inventory.py`.


---

## 6. Data model

### 6.1 Additions to `ClinicalCase` (additive and optional; old cases are unaffected)

Add these to `asclepius/cases.py`. Every new list defaults to empty, and every new item carries `collected_offset_days` so the existing temporal gates apply. Every new model sets `model_config = ConfigDict(extra="forbid")`, like `ClinicalCase` itself. Without that, leftover raw date keys would be silently dropped instead of failing loudly.

```python
class EncounterItem(BaseModel):          # one visit
    model_config = ConfigDict(extra="forbid")
    encounter_ref: str                     # opaque, per-case ("enc-007"); never a source id
    visit_type: str = "office"             # office | telehealth | procedure | hospital | other
    collected_offset_days: Optional[int] = None
    note_ids: List[str] = []               # ClinicalNote.note_id of worksheets for this visit

class OrderItem(BaseModel):              # something the doctor ordered at a visit
    model_config = ConfigDict(extra="forbid")
    kind: str                              # lab | imaging | referral | procedure | follow_up | other
    code: Optional[str] = None             # LOINC for labs; CPT/SNOMED kept internal only (§7.4)
    code_system: Optional[str] = None
    text: str = ""
    encounter_ref: Optional[str] = None
    due_offset_days: Optional[int] = None  # follow-up / due date relative to the visit
    collected_offset_days: Optional[int] = None
    source: str = "structured"             # structured | worksheet_extracted
    source_span: Optional[str] = None      # quoted snippet (de-identified) it came from

class MedicationEvent(BaseModel):        # a change, not a list entry
    model_config = ConfigDict(extra="forbid")
    action: str                            # start | stop | increase | decrease | hold | continue | switch
    drug: str                              # as written
    rxnorm_ingredient: Optional[str] = None
    dose: Optional[str] = None             # "10 mg"
    route: Optional[str] = None
    freq: Optional[str] = None
    reason: Optional[str] = None
    encounter_ref: Optional[str] = None
    collected_offset_days: Optional[int] = None
    source: str = "structured"
    source_span: Optional[str] = None

class AllergyItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    substance: str
    reaction: Optional[str] = None
    collected_offset_days: Optional[int] = None

# on ClinicalCase:
encounters: List[EncounterItem] = []
orders: List[OrderItem] = []
medication_events: List[MedicationEvent] = []
allergies: List[AllergyItem] = []

# on existing models (optional, additive):
ClinicalNote.note_id: Optional[str] = None          # "note-<n>", assigned in _merge_fragments, stable per case
MedicationItem.status: Optional[str] = None          # active | stopped | completed | unknown
MedicationItem.start_offset_days: Optional[int] = None
MedicationItem.stop_offset_days: Optional[int] = None
```

**Wiring. All required in M1; without these the new data never survives ingestion:**

1. `_merge_fragments` (`asclepius/ingestion.py:1175`), in its per-key copy loop: add `encounters`, `orders`, `medication_events` and `allergies` to the copied keys, and assign `note_id` in merge order.
2. `normalize_timeline` (`asclepius/timeline.py:628`): add a conversion block for each new collection and for `MedicationItem` raw `started_at` / `stopped_at`. Each block converts raw date strings to offsets and **deletes** the raw key, the same way the existing lab-panel block does.
3. `_STRUCTURED_DATE_KEYS` (`asclepius/timeline.py:482`): add `("encounters", ("collected_at", "start"))`, `("orders", ("collected_at", "authored_on", "due_at"))`, `("medication_events", ("collected_at", "authored_on"))`, `("allergies", ("recorded_at", "collected_at"))`, and add `started_at`, `stopped_at` to `medications`.
4. `_FREE_TEXT_FIELDS` (`asclepius/timeline.py:443`): add `("orders", ("text", "source_span"))`, `("medication_events", ("drug", "dose", "reason", "source_span"))` and `("allergies", ("substance", "reaction"))`, so dates inside them are rewritten.
5. `deidentify()` already walks every string recursively (`_case_text_fields`, `asclepius/case_formats.py:87`). No change is needed there, but add a test proving the new fields are scanned.

`LabResult` already has a `loinc` field (`asclepius/cases.py:106`) and is used for normalisation.

### 6.2 New tables (all in the Asclepius store; realm-scoped like every other store call)

Write the DDL in `asclepius/store.py`, following the existing `CREATE TABLE IF NOT EXISTS` pattern. **No changes to existing tables.**

```sql
-- One FHIR chart per ingested patient (the canonical, de-identified chart)
CREATE TABLE IF NOT EXISTS ehr_charts (
  chart_id        TEXT PRIMARY KEY,          -- 'chart-' + 12 hex
  ingest_case_id  TEXT NOT NULL,             -- ingest_cases.ingest_case_id
  upload_id       TEXT NOT NULL,
  specialty       TEXT NOT NULL DEFAULT 'nephrology',
  source_country  TEXT NOT NULL DEFAULT 'US',-- I15
  n_visits        INTEGER NOT NULL DEFAULT 0,
  resources_json  TEXT NOT NULL,             -- FHIR R4 resources, relative offsets (no dates)
  extraction_json TEXT,                      -- worksheet-extraction report (confidence, spans)
  status          TEXT NOT NULL DEFAULT 'built',   -- built | quarantined | superseded
  chart_hash      TEXT NOT NULL,             -- sha256 of resources_json
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

-- One row per visit that can become a task
CREATE TABLE IF NOT EXISTS ehr_visits (
  visit_id        TEXT PRIMARY KEY,          -- 'visit-' + 12 hex
  chart_id        TEXT NOT NULL,
  encounter_ref   TEXT NOT NULL,
  visit_index     INTEGER NOT NULL,          -- 0..n-1 in chart order
  offset_days     INTEGER NOT NULL,          -- decision day = the visit worksheet's offset (§8.2)
  has_next_visit  INTEGER NOT NULL DEFAULT 0,
  key_enc         TEXT,                      -- encrypted VisitKey JSON (I2)
  outcome_enc     TEXT,                      -- encrypted OutcomeWindow JSON (I2)
  key_confidence  REAL,                      -- min item confidence from extraction
  key_conflict    INTEGER NOT NULL DEFAULT 0,-- worksheet vs structured med-list disagree (§8.2 step 4)
  key_audit       TEXT NOT NULL DEFAULT 'pending', -- pending | not_sampled | passed | failed | waived
  doctor_flagged  INTEGER NOT NULL DEFAULT 0,-- the key itself trips a safety rule (§9.5)
  status          TEXT NOT NULL DEFAULT 'candidate', -- candidate | ready | excluded
  exclusion_reason TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE (chart_id, encounter_ref)
);

-- A runnable task (one visit + decoys + instruction + budget + split)
CREATE TABLE IF NOT EXISTS ehr_tasks (
  task_id         TEXT PRIMARY KEY,          -- 'ehrt-' + 12 hex
  visit_id        TEXT NOT NULL,
  task_kind       TEXT NOT NULL DEFAULT 'visit',  -- visit | probe_trend | probe_retrieval | probe_dose
  env_version     TEXT NOT NULL,             -- e.g. 'neph-ehr-1.1.0'
  split           TEXT NOT NULL,             -- train | dev | heldout  (I11)
  seed            INTEGER NOT NULL,
  snapshot_json   TEXT NOT NULL,             -- the pre-sliced sandbox snapshot (target + decoys), synthetic dates
  instruction     TEXT NOT NULL,
  budget_tool_calls INTEGER NOT NULL DEFAULT 40,
  tags_json       TEXT,                      -- failure modes targeted (F1..F12), action/no-action class
  probe_key_enc   TEXT,                      -- encrypted key for probe tasks
  status          TEXT NOT NULL DEFAULT 'ready',  -- ready | retired
  created_at      TEXT NOT NULL
);

-- One agent episode on one task
CREATE TABLE IF NOT EXISTS ehr_rollouts (
  rollout_id      TEXT PRIMARY KEY,          -- 'ehrr-' + 12 hex
  task_id         TEXT NOT NULL,
  model           TEXT,
  provider        TEXT,
  harness         TEXT NOT NULL DEFAULT 'native_tools', -- native_tools | json_protocol | scripted:noop | scripted:oracle | external
  run_group       TEXT,                      -- groups k repeats for pass^k
  trajectory_json TEXT NOT NULL,
  access_log_json TEXT NOT NULL,             -- every read: resource ids returned
  writes_json     TEXT NOT NULL,             -- accepted writes (the end state, I4)
  rejected_writes_json TEXT,                 -- writes the server refused, with OperationOutcome
  terminated_by   TEXT,                      -- finish_visit | budget | error
  provisional_reward REAL,
  final_reward    REAL,                      -- set when all reviews resolve
  hard_fail       INTEGER NOT NULL DEFAULT 0,
  hard_fail_reason TEXT,
  status          TEXT NOT NULL DEFAULT 'graded', -- graded | awaiting_review | final
  created_at      TEXT NOT NULL,
  updated_at      TEXT
);

-- One row per checkpoint per rollout
CREATE TABLE IF NOT EXISTS ehr_checkpoints (
  checkpoint_id   TEXT PRIMARY KEY,
  rollout_id      TEXT NOT NULL,
  kind            TEXT NOT NULL,             -- retrieve | reason | act | document | safety
  score           REAL,                      -- 0..1, NULL when not gradable
  verdict         TEXT NOT NULL,             -- pass | partial | fail | not_gradable | pending_review
  items_json      TEXT NOT NULL,             -- per-item matches/misses/extras with evidence
  grader          TEXT NOT NULL,             -- code | rubric_llm | physician
  UNIQUE (rollout_id, kind)
);

-- A physician review. Rollout-scoped (disagreement, safety, rubric_sample) or
-- visit-scoped (outcome_flag, key_audit). Items are grouped inside.
CREATE TABLE IF NOT EXISTS ehr_reviews (
  review_id       TEXT PRIMARY KEY,          -- 'ehrrev-' + 12 hex
  scope           TEXT NOT NULL,             -- rollout | visit
  rollout_id      TEXT,                      -- NULL for visit-scoped reviews
  visit_id        TEXT NOT NULL,
  trigger         TEXT NOT NULL,             -- disagreement | outcome_flag | safety | rubric_sample | key_audit
  dedupe_key      TEXT NOT NULL UNIQUE,      -- rollout scope: rollout_id|trigger ; visit scope: visit_id|trigger
  items_json      TEXT NOT NULL,             -- the disputed items (blinded Plan A / Plan B)
  blind_order     TEXT NOT NULL,             -- 'a_is_doctor' | 'a_is_agent' (seeded, never sent to the reviewer)
  status          TEXT NOT NULL DEFAULT 'open', -- open | needs_second | needs_tiebreak | resolved | unresolved | cancelled
  reason          TEXT,                      -- e.g. no_eligible_reviewer, daily_cap
  resolution_json TEXT,                      -- per-item final verdicts
  created_at      TEXT NOT NULL,
  resolved_at     TEXT
);

-- Which rollouts are waiting on which reviews (a visit-scoped review can hold many rollouts)
CREATE TABLE IF NOT EXISTS ehr_review_rollouts (
  review_id       TEXT NOT NULL,
  rollout_id      TEXT NOT NULL,
  item_ids_json   TEXT NOT NULL,             -- the rollout's items this review decides
  PRIMARY KEY (review_id, rollout_id)
);

-- Review work offered to one physician (NOT the shared assignments table; see §4.3)
CREATE TABLE IF NOT EXISTS ehr_review_assignments (
  review_assignment_id TEXT PRIMARY KEY,     -- 'ehra-' + 12 hex; the earning ref_id (I14)
  review_id       TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  round           INTEGER NOT NULL,          -- 1, 2, or 3 (tie-break)
  status          TEXT NOT NULL DEFAULT 'offered', -- offered | claimed | submitted | expired | revoked
  offered_at      TEXT NOT NULL,
  due_at          TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  UNIQUE (review_id, user_id)
);

-- Each reviewer's independent submission
CREATE TABLE IF NOT EXISTS ehr_review_verdicts (
  verdict_id      TEXT PRIMARY KEY,
  review_id       TEXT NOT NULL,
  review_assignment_id TEXT NOT NULL UNIQUE, -- ehr_review_assignments id (I14 ref_id)
  reviewer_user_id TEXT NOT NULL,
  round           INTEGER NOT NULL,          -- 1, 2, or 3 (tie-break)
  verdict_json    TEXT NOT NULL,
  confidence      TEXT NOT NULL,             -- high | low
  seconds_spent   INTEGER,
  submitted_at    TEXT NOT NULL,
  UNIQUE (review_id, reviewer_user_id)
);

-- Reuse verdicts across rollouts: same visit + same normalized disputed item → same answer
CREATE TABLE IF NOT EXISTS ehr_verdict_cache (
  cache_key       TEXT PRIMARY KEY,          -- sha256(visit_id + normalized item signature)
  visit_id        TEXT NOT NULL,
  item_signature  TEXT NOT NULL,
  verdict         TEXT NOT NULL,             -- valid_alternative | agent_wrong | doctor_wrong | harmful
  source_review_id TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

-- Corrections to a visit key made by resolved reviews (I13)
CREATE TABLE IF NOT EXISTS ehr_key_corrections (
  correction_id   TEXT PRIMARY KEY,
  visit_id        TEXT NOT NULL,
  review_id       TEXT NOT NULL,
  correction_json TEXT NOT NULL,             -- {item_id, from, to, reason}
  created_at      TEXT NOT NULL
);

-- Clinicians who may never review charts from a given upload (I12)
CREATE TABLE IF NOT EXISTS ehr_source_exclusions (
  upload_id       TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  reason          TEXT NOT NULL,             -- source_practice_clinician | family | other
  PRIMARY KEY (upload_id, user_id)
);
```

Extend `scripts/data_inventory.py` so it lists the ids of every new table, and add the `ehr_*` tables to whatever table list `scripts/data_change_guard.py` checks (I16).

### 6.3 The VisitKey (what the doctor did at visit k; sealed)

```json
{
  "visit_id": "visit-3f2a…",
  "assessments": [
    {"item_id": "a1", "icd10": "N18.4", "text": "CKD stage 4", "status": "active",
     "source_span": "CKD IV, eGFR 22 today", "confidence": 0.93}
  ],
  "med_changes": [
    {"item_id": "m1", "action": "decrease", "rxnorm_ingredient": "29046", "drug": "lisinopril",
     "from_dose": "20 mg daily", "to_dose": "10 mg daily", "reason": "K 5.6",
     "source_span": "decrease lisinopril to 10", "confidence": 0.88}
  ],
  "med_continues": ["amlodipine", "furosemide"],
  "orders": [
    {"item_id": "o1", "kind": "lab", "loinc_group": "BMP", "codes": ["2823-3","2160-0"],
     "timing_days": 7, "source_span": "BMP 1 wk", "confidence": 0.95}
  ],
  "referrals": [
    {"item_id": "r1", "specialty": "vascular_surgery", "reason": "AVF planning",
     "source_span": "refer vasc for fistula", "confidence": 0.9}
  ],
  "follow_up": {"item_id": "f1", "interval_days": 28, "tolerance_days": 7, "confidence": 0.97},
  "escalation": null,
  "counseling": ["low potassium diet", "avoid NSAIDs"],
  "no_change_categories": ["diuretic"],
  "evidence_refs": ["Observation/k-2026-…", "Observation/egfr-…", "DocumentReference/ws-prev"],
  "extraction": {"model": "…", "prompt_id": "ehr_worksheet_extract_v1", "min_confidence": 0.88}
}
```

The `OutcomeWindow` (sealed) holds visit k+1's labs, vitals, worksheet assessment text and med events, plus the outcome signals computed by `outcome_rules.py` (§9.6).

---

## 7. Third-party software (tell the builder exactly what to install)

**Rules:**

- **Required** means the milestone cannot pass without it.
- **Optional** means guard the import: `try: import x except ImportError: x = None`, and skip that test with `pytest.importorskip`.
- Add Python packages to `backend/requirements.txt` with a pinned major version, and add a line to `.env.example` for each new environment variable.
- Containers are never started by the default test suite.

### 7.1 Python packages

| Package | Why | Milestone | Required? | Install |
|---|---|---|---|---|
| `lxml` | Parse C-CDA/CCD XML (Office Ally "CCD", "Transition of Care" and "EHI CCD" batch exports) | M1 | Required | `pip install "lxml>=6.1.3,<7"` |
| `pypdf` | PDF text layer extraction. **Already pinned** (`pypdf==6.19.0` in `backend/requirements.txt`); do not re-pin | M1 | Required | already installed |
| `pdfplumber` | Table-aware text extraction for lab tables in PDFs | M1 | Optional (improves lab tables) | `pip install "pdfplumber>=0.11"` |
| `ocrmypdf` + system `tesseract-ocr` | OCR for scanned worksheets with no text layer | M1 | Optional in dev, **required in production** for scanned uploads | `apt-get install -y ocrmypdf tesseract-ocr` then `pip install "ocrmypdf>=16"` |
| `fhir.resources` | Validate the FHIR resources we build and the agent writes. Use `fhir.resources.R4B` (R4B ≈ R4 for the resources we use) | M2 | Required | `pip install "fhir.resources>=8,<9"` |
| `presidio-analyzer` | Stronger residual-PHI scan (already an optional backend of `deid_verify`) | M1 | Optional | `pip install presidio-analyzer && python -m spacy download en_core_web_lg` |
| `openenv-core` | OpenEnv server/client types for the lab package | M6 | Optional (package build only) | `pip install openenv-core` (check the name on PyPI at build time; the repo is https://github.com/huggingface/OpenEnv) |
| `verifiers` | Prime Intellect adapter in the lab package | M6 | Optional (package build only) | `pip install verifiers` |
| `gymnasium` | `as_gym()` wrapper already supported | n/a | Optional | `pip install gymnasium` |

### 7.2 Containers and tools (none run in the default suite)

| Tool | Why | Milestone | Required? | Command |
|---|---|---|---|---|
| **HAPI FHIR JPA** (Apache-2.0) | Parity reference: the same snapshot and queries must return the same ids from HAPI and from `FhirSandbox` | M6 (CI job `ehr-parity`, `workflow_dispatch` only) | Optional | `docker run -d -p 8080:8080 -e hapi.fhir.fhir_version=R4 hapiproject/hapi:latest` → base URL `http://localhost:8080/fhir` |
| **Synthea** (Apache-2.0, JDK 17+) | Synthetic CKD patients for fixtures and demos, never mixed with real data | M0 | Required for fixture generation (one-time; commit the generated fixture JSON, not Synthea) | `git clone https://github.com/synthetichealth/synthea && cd synthea && ./run_synthea -p 200 -s 42 --exporter.fhir.export=true --exporter.ccda.export=true`, then keep the patients whose conditions include CKD (a chronic kidney disease condition). Check `src/main/resources/modules` for a kidney module and pass it with `-m <name>` if present |
| **Microsoft FHIR-Converter** (MIT) | Optional cross-check of our C-CDA parser on real exports | M1 (manual QA only) | Optional | `docker run -d -p 8081:8080 mcr.microsoft.com/healthcareapis/fhir-converter:latest` (check the current tag) |
| **Medplum** (Apache-2.0) | A clickable EHR UI over the same FHIR data, for computer-use agents | M7 (later, optional) | Optional | `curl -o docker-compose.yml https://raw.githubusercontent.com/medplum/medplum/main/docker-compose.full-stack.yml && docker compose up -d` (UI :3000, API :8103) |
| **OpenEMR** (GPL-3.0) | **Do not use in v1.** Copyleft applies if we ship a modified copy to labs, and FHIR needs OAuth even locally. Listed so nobody picks it by default | n/a | No | n/a |
| **Aidbox** | **Do not use.** Its free licence forbids real healthcare data | n/a | No | n/a |

### 7.3 What the zip is likely to contain (Office Ally EHR 24/7)

Office Ally's documented export options:

- **Chart Download** (PDF, or a password-protected ZIP of PDFs): https://support.officeally.com/ehr-24-7-articles/exporting-patient-encounters-and-documents-from-ehr-24-7
- **Document Center batches** in **CCD, Transition of Care and EHI CCD** (C-CDA XML): https://support.officeally.com/ehr-24-7-articles/using-the-document-center
- A FHIR R4 API that only vendor admins can register for.

No clinical CSV export is documented. **Operational ask for Tej (not a code task):** request the **EHI CCD batch export** in addition to the PDFs. Structured meds, problems, results and encounters from C-CDA need no OCR and far less LLM extraction. Keep the PDFs for the visit worksheets.

### 7.4 Terminology (free; no licence fees for our use)

- **LOINC:** free, with an attribution notice in the lab package README. https://loinc.org/kb/license
- **RxNorm:** the RxNav API needs no licence, but is limited to 20 req/s per IP. Cache results in `ehr_sandbox/terminology_cache.json`, which is committed and synthetic-safe. For volume, run RxNav-in-a-Box. **No network calls from tests:** the committed cache must cover every fixture drug.
- **ICD-10-CM:** free CDC files.
- **SNOMED CT:** US use is free through UMLS, but distribution outside the US needs SNOMED International registration. **Do not put SNOMED codes in lab packages.** Use ICD-10-CM and LOINC only.
- **CPT (AMA):** licensed. **Never put CPT descriptors in exported packages.** Codes may be kept internally.


---

## 8. Architecture: from zip to task

New package: `backend/asclepius/ehr_sandbox/`.

```
ehr_sandbox/
  __init__.py
  constants.py         # env version, anchor date, budgets, weights, blocked jurisdictions
  chart_builder.py     # ingest case → FHIR chart (+ worksheet extraction)       M2
  worksheet_extract.py # LLM structured extraction from a de-identified worksheet  M2
  terminology.py       # LOINC groups, RxNorm ingredient lookup (cache-first), ICD-10 helpers  M2
  visit_compiler.py    # chart → ehr_visits → ehr_tasks (slice, seal, decoys, split)  M3
  identities.py        # synthetic names / MRNs / birth dates (seeded)            M3
  sandbox.py           # FhirSandbox: snapshot + overlay, search, validate, audit log  M3
  tools.py             # the tool registry (schemas + handlers over FhirSandbox)  M3
  calculators.py       # eGFR, CrCl, KFRE, UACR category, CKD stage, unit conversion  M3
  env.py               # EhrVisitEnv (same interface as ClinicalEnv; not a subclass)  M3
  harness.py           # native-tool-calling rollout + scripted noop/oracle agents  M4
  grader.py            # the four checkpoints + safety gate + reward composition  M4
  safety_rules.py      # deterministic nephrology hard-fail rules                  M4
  outcome_rules.py     # visit k+1 signals that the doctor may have been wrong     M4
  rubric.py            # the visit-note rubric + LLM judge call                    M4
  reviews.py           # triggers, reviewer selection, verdict resolution, cache   M5
  report.py            # eval report per model (pass@1, pass^k, checkpoint breakdown, failure tags)  M4
  package.py           # lab package: Docker context, OpenEnv manifest, verifiers adapter  M6
  server.py            # FastAPI app: OpenEnv reset/step/state + FHIR REST facade  M6
backend/asclepius/adapters/ccda.py      M1
backend/asclepius/adapters/pdf_doc.py   M1
backend/routers/asclepius_ehr_sandbox.py  M3–M5
frontend/asclepius/ehr/review.js          M5
frontend/asclepius/ehr/admin.js           M5
```

### 8.1 Ingestion (M1): two new adapters

**`adapters/ccda.py`** parses C-CDA R2.1 / CCD XML with `lxml`. It maps these sections by template OID, falling back to LOINC section codes:

| C-CDA section | LOINC section code | → fragment |
|---|---|---|
| Problems | 11450-4 | `problem_list` (+ ICD-10 code in the text as `condition`, e.g. "CKD stage 4 (N18.4)") |
| Medications | 10160-0 | `medications` (drug, dose, route, freq, `status`, and start/stop dates as raw `started_at` / `stopped_at`) |
| Results | 30954-2 | `lab_panels` (organizer → panel; observation → `LabResult` with `loinc`, value, unit, ref range, raw `collected_at`) |
| Vital signs | 8716-3 | a `lab_panels` entry named "Vitals" per timepoint (per-timepoint vitals are modelled as panels, as `_public_vitals` at `asclepius/environments/state.py:269` requires for gating) |
| Encounters | 46240-8 | `encounters` (raw date → offset later; `encounter_ref` = `enc-<n>` in date order) |
| Plan of treatment / care plan | 18776-5 | `orders` with `kind` from the entry type (observation → lab, act → other, encounter → follow_up) |
| Allergies | 48765-2 | `allergies` |
| Notes / assessment | 51848-0, 11488-4 | `notes` (note_type from section title) |

**Rules:**

- **Mint the patient key first, then drop identifiers.** Before discarding anything, hash `recordTarget/patientRole/id` (root + extension) into `_patient_keys` as `ccda-<sha256[:12]>`, exactly as the HL7 adapter appends PID-3 to `_patient_keys` (`asclepius/adapters/hl7v2.py:127`). Then **drop** `recordTarget` (the patient header), `author`, `custodian`, `legalAuthenticator`, every `addr`, `telecom` and `name`, and every `id/@extension`. This is the same rule as `asclepius/adapters/__init__.py`.
- **Medication status.** Read the medication entry's `statusCode` and `effectiveTime` low/high into `MedicationItem.status` and raw `started_at` / `stopped_at` (§6.1). The med-list diff in §8.2 depends on them.
- **Never** read narrative `<text>` blocks for structured fields. Read them only for notes.
- A document with no recognisable sections returns an empty fragment with `_unparsed_reason`, and ingestion routes it to review. It never passes silently.

**`adapters/pdf_doc.py`** turns one PDF into one or more `notes` fragments:

1. Extract the text layer with `pypdf`. If a page yields fewer than 40 characters, it is treated as scanned. Run OCR on it when `ocrmypdf` is available. Otherwise mark the page `ocr_unavailable`, and the whole entry routes to review (it never becomes a silently empty note).
2. **Split multi-visit PDFs.** Office Ally chart downloads can concatenate encounters. Split on page-level "Date of Service" / "DOS" / "Visit Date" headers. Each segment becomes one note with raw `collected_at` from that header. This reuses the date-header parsing rule in `asclepius/adapters/note_text.py`, which also refuses non-date literals.
3. Set `note_type` to `"Visit worksheet"` when the segment has worksheet markers ("Assessment", "Plan", "A/P", "Impression"). Otherwise use `"Lab report"` (reference-range tables), `"Correspondence"` or `"Other"`.
4. Parse lab tables inside "Lab report" segments into `lab_panels` when `pdfplumber` is available. When it is not, keep them as notes only; the chart builder can still extract values from text in M2.
5. **Never store page images as assets.** An image-only page that could not be OCR'd was never screened for names or headers. It stays only inside the encrypted raw upload in quarantine. The entry is reported as unparsed (next paragraph).

**Unparsed files.** A PDF whose pages cannot be read (scanned, no OCR installed) counts as an incomplete file. The existing orchestrator then raises a **blocking** `incomplete_upload` review on the upload's cases (`_raise_review` with `incomplete_upload`, `asclepius/ingestion.py:2100`), and no chart builds until an admin resolves it. That is correct behaviour and must not be weakened. The admin either re-uploads with OCR enabled or clears the review with a written reason through the existing route `POST /ingestion/cases/{ingest_case_id}/review/clear` (`clear_case_review`, `routers/asclepius.py:7442`).

**Edits to existing files in M1 (the complete list; nothing else in ingestion changes):**

1. `_classify` (`asclepius/ingestion.py:851`): add `ccda` for a `.xml` whose head contains `<ClinicalDocument` or `urn:hl7-org:v3`, and `pdf_doc` for `.pdf` or `%PDF` magic. Place both **before** the final `unsupported` return.
2. The `_classify` call site (`asclepius/ingestion.py:1127`): pass `data[:4096]` and a matching `text_head` **only when the name ends in `.xml`**. Every other entry keeps today's 512-byte and 200-character heads.
3. `FORMATS` (`asclepius/case_formats.py:204`) and `asclepius/adapters/__init__.py`: register and import both adapters.
4. `_merge_fragments`, `normalize_timeline`, `_STRUCTURED_DATE_KEYS` and `_FREE_TEXT_FIELDS`: the wiring in §6.1.
5. `_KEY_SOURCE_PRECEDENCE` (`asclepius/ingestion.py:1725`): insert `ccda` after `hl7v2`, and `pdf_doc` after `note_text`.

**Patient grouping.** Today's key minting (`_patient_key_and_source`, `asclepius/ingestion.py:1690`) uses only a `<patient>__` filename prefix or a single manifest key, and otherwise puts everything in `default`. An Office Ally zip of many patients would therefore **merge several patients into one case**. That is the worst possible failure here, because it would also let one patient straddle train and heldout. Rules:

**How it is built (inside the adapters; `_patient_key_and_source` is not edited).** The orchestrator already passes each adapter the manifest with the entry's `filename` added (`entry_manifest`, `asclepius/ingestion.py:1948`). An adapter exception becomes `parse_failed`, which raises `incomplete_upload`. So:

- **Both adapters, first:** if `manifest["files"][filename]` exists (`{"files": {"<path in zip>": "<patient label>"}}`), append `manifest-<label>` to `_patient_keys`. A C-CDA and that patient's PDFs then share one key, and `unify_patient_keys` sees a single patient.
- **C-CDA, otherwise:** append the hashed `patientRole/id` (above).
- **PDF, otherwise:** append `prefix-<patient>` from a `<patient>__` filename prefix. With neither a manifest entry nor a prefix, `pdf_doc` **raises** `PatientUnmapped`. The PDF is held, never guessed, and the upload goes to review as above. Omics is asked to supply the manifest or the prefixes (§15 Q2).
- **The manifest must not set `patient_key`** for a multi-patient zip. That field overrides every per-file key (`asclepius/ingestion.py:1712`) and would merge everyone into one case. `pdf_doc` and `ccda` raise when they see both `patient_key` and a `files` map, and the fixture manifests never set it.
- **A C-CDA and PDFs for the same person with different keys** (no manifest): `unify_patient_keys` (`asclepius/ingestion.py:1729`) either splits them or raises `ambiguous_patient_identity`. That blocking review is the correct outcome.
- **Test:** an M1 test uploads 3 patients' C-CDAs and PDFs with no manifest, and asserts that nothing merges silently.

### 8.2 Chart building (M2): `chart_builder.build_chart(ingest_case_id)`

Input: one `ingest_cases` row with status `ingested`. Output: one `ehr_charts` row.

1. **Load** the de-identified `ClinicalCase` and run `verify_deid` (I7). Any finding sets the chart to `quarantined` with a masked reason.
2. **Identify visits.** A visit is keyed to the **day of a worksheet**: each distinct `collected_offset_days` carrying a `note_type = "Visit worksheet"` note is one visit, and `d_k` is that offset. Do not use a segmentation cluster's start day, because one cluster can span several days. `segment_longitudinal_record` (`asclepius/real_cases.py:250`), called with `min_gap_days=1`, is used only to attach same-day items (labs, the C-CDA encounter) to the visit. Other encounters (lab-only draws, phone notes) stay in the chart as history but are not tasks.
3. **Extract every worksheet** with `worksheet_extract.extract(note_text, context)`:
   - one `call_llm(role="ehr_extract", purpose="ehr_worksheet_extract", ...)` per worksheet;
   - JSON schema output: assessments, med_changes, med_continues, orders, referrals, follow_up, escalation, counseling;
   - each item carries a verbatim `source_span` (≤ 160 chars) and a `confidence` in [0, 1].

   **Span check:** any item whose `source_span` is not a substring of the worksheet text (after whitespace normalisation) is dropped and counted as `span_mismatch`. This stops the extractor inventing answer-key items.
4. **Reconcile with structure.** When C-CDA structured meds exist with `status`, `start_offset_days` and `stop_offset_days` (§6.1) around the visit, compute the **med-list diff** (start/stop/dose change by RxNorm ingredient) and merge it with the extracted `med_changes`:
   - an item in both sources gets `confidence = max(...)`;
   - a structured-only change is added with `source = "structured"`;
   - a conflict (the worksheet says increase, the list says decrease) sets `conflict = true` and forces a key audit (§8.4).
5. **Normalise codes** with `terminology.py`:
   - drug text → RxNorm ingredient (cache-first, with a brand → generic table);
   - lab names → LOINC plus a `loinc_group` (BMP, CMP, renal panel, CBC, UACR, PTH, phosphorus, 25-OH vitamin D, iron studies, lipid, A1c, urinalysis);
   - assessment text → ICD-10-CM where the worksheet states it.

   Unmapped items keep their text and `code = null`. They are never dropped.
6. **Emit FHIR R4 resources** with **relative** time. Each resource carries `extension[offsetDays]` and no absolute date yet:

| Source | FHIR resource |
|---|---|
| patient (age band, sex) | `Patient` (no name, no identifier yet; §8.3 adds synthetic ones per task) |
| `encounters` | `Encounter` (status finished, class AMB) |
| lab panels | `Observation` (category laboratory, LOINC code, `valueQuantity` with UCUM unit, `referenceRange`, `interpretation`) grouped by a `DiagnosticReport` per panel |
| vitals panels | `Observation` (category vital-signs; BP as a component observation, LOINC 85354-9) |
| `medications` + `medication_events` | `MedicationRequest` history: one resource per regimen span. `status` is active, stopped or completed. `priorPrescription` links dose changes. `dosageInstruction` is structured when parseable |
| `problem_list` + extracted assessments | `Condition` (clinicalStatus, ICD-10-CM coding when known) |
| `orders` (past visits only) | `ServiceRequest` (lab, imaging, referral) and `Appointment` (follow-up) |
| worksheets and other notes | `DocumentReference` (type "Visit worksheet" etc., `content.attachment.data` = the de-identified text, base64) |
| `allergies` | `AllergyIntolerance` |

7. Validate every resource with `fhir.resources.R4B`. **A chart with any invalid resource is quarantined, not repaired silently.**
8. Write `ehr_charts` with `chart_hash = sha256(resources_json)`. Re-building an unchanged case is a no-op, because the hash is equal.

### 8.3 Visit compiling (M3): `visit_compiler.compile_chart(chart_id)`

For each visit k in the chart:

1. **Decision instant.** Visit k's day offset `d_k`, at 09:00 local synthetic time.
2. **Slice (the most leak-sensitive step; I1).** The snapshot contains every resource with `offsetDays < d_k`, plus these same-day items:
   - lab and vital Observations with `offsetDays = d_k` that were **not** ordered at visit k. These are pre-visit draws, which are ordered at an earlier visit or have no order link;
   - the `Encounter` for visit k itself, with `status = in-progress`. It is **stripped** of `reasonCode`, `type` text, `diagnosis` and `serviceType`, which would state why the patient came.

   **Excluded:**
   - every visit-k worksheet and note;
   - every order, med event and Condition authored at visit k;
   - everything after `d_k`;
   - anything with unknown offset.

   **Status is recomputed as of `d_k`, not copied.** A resource created before visit k can carry a *later* status that reveals the answer. Rules:
   - `MedicationRequest`: `active` if it started before `d_k` and has no stop before `d_k`. The stop date, `stopped` status and any stop reason authored at or after `d_k` are removed.
   - `Condition`: `clinicalStatus` and `abatement` are taken from the last assertion before `d_k`.
   - `ServiceRequest`: `completed` only if a result exists before `d_k`, else `active`.
   - `Appointment`: `booked` if it falls on or after `d_k`.
   - Any reference (`priorPrescription`, `basedOn`, `replaces`) pointing to an excluded resource is removed.

   **Two leak checks, both required:**
   1. `assert_fhir_slice(snapshot, d_k)`, new in `visit_compiler`. It walks every resource and every date, status and reference field, and fails on anything dated at or after `d_k` that is not in the allowed same-day list, and on any status that disagrees with the as-of rules.
   2. The text check: **no run of 8 or more consecutive tokens from the visit-k worksheet appears anywhere in the snapshot**. This reuses the idea of `assert_no_answer_leakage`, `asclepius/ingestion.py:1619`.

   `assert_temporal_split` (`asclepius/real_cases.py:2175`) only inspects the older ClinicalCase collections. Run it too, over the ClinicalCase view of the slice, as a third check, but do not rely on it alone.
3. **Seal.** Store the VisitKey (from visit k's extraction) as `key_enc` and the OutcomeWindow (visit k+1) as `outcome_enc`. `has_next_visit = 0` when there is no k+1. Such a visit can still be a task (key-graded), but it can never trigger an outcome review.
4. **Synthetic time.** Convert every `offsetDays` to a date: `ANCHOR + (offset − d_k) days`, where `ANCHOR = 2031-03-03` (a Monday, deliberately in the future, so no real date can collide). The decision instant is therefore always `2031-03-03T09:00:00-08:00`. Intervals are exact. The real calendar is never present (I7).
5. **Synthetic identities (I8).** `identities.py`, seeded by `task seed`, generates for the target and each decoy:
   - a name, from committed lists of 400 given names and 400 family names that are common in US census data, and never taken from the source;
   - an 8-digit MRN;
   - a birth date consistent with the age band.

   Decoys are chosen to be **confusable** (F11):
   - decoy 1 shares the family name;
   - decoy 2 shares the birth year and sex;
   - decoy 3, when the practice has 20 or more charts, shares a CKD stage.
6. **Decoys (D12).** Pick 2 or 3 other charts from the **same upload**, time-sliced at their own visit with the closest offset. They are reset onto the same anchor so all "today" dates line up. Decoy charts never contribute answer keys to this task.
7. **Instruction** (the only natural-language prompt, identical across tasks except names):
   > "You are the nephrologist at an outpatient clinic. Today is Monday 3 March 2031. You are seeing **{name}** (MRN {mrn}, DOB {dob}) for a scheduled follow-up visit. Use the EHR tools to review the chart, make today's clinical decisions, place any orders, and write today's visit note. Only act on this patient. Call `finish_visit` when you are done."

   The instruction never states the reason for the visit, the diagnosis, or any value.
8. **Split.** `split` is a deterministic hash of `chart_id` (never `task_id`), so every visit of one patient lands in the same split. Defaults: 70% train, 15% dev, 15% heldout. Decoys come only from charts in the same split.

### 8.4 Key audit gate (M3; before a visit can be `ready`)

A visit becomes `ready` only when **all** of these hold:

- `key_confidence ≥ EHR_KEY_MIN_CONFIDENCE` (default 0.80) and `key_conflict = 0`, **or** a physician key audit has `passed` (which clears both);
- the key has at least one gradable item (an assessment, med change, order, referral or follow-up), **or** it is explicitly a "no change" visit: every med continued, follow-up only. A "no change" key **with no follow-up** has nothing ACT can grade, so it is `excluded` with reason `no_gradable_action`;
- the slice passed both leak checks;
- `key_audit ∈ {passed, waived, not_sampled}`. A visit outside the audit sample gets `not_sampled` when it is compiled; the rest stay `pending` until audited.

**`key_audit`** is a **physician QA pass on the extraction, not on the doctor.** It applies to:

- the first 20 visits of every new upload;
- a 5% random sample after that;
- every visit below the confidence threshold.

These audits are an ops task. They go through the same review routing (§10) with `trigger = 'key_audit'`, at $25, and show the worksheet next to the extracted key. Admins can set `waived` with a reason, which is logged.

**Key items no tool can express** (for example a `procedure` or `other` order with no code group) are marked `not_gradable` in the key and excluded from every denominator. They are listed in the compile report, so the tool set can be extended if they are common.

### 8.5 Task balance (anti-"do nothing", I5)

After compiling, each split must satisfy:

- **action vs no-action:** 40–60% of visit tasks contain at least one med change, order or referral in the key;
- **escalation:** at least 3% of tasks have an escalation or urgent item when the data supports it. If the practice data has fewer, report it. **Never fabricate.**

When a split is out of range, `visit_compiler` downsamples the majority class, never the minority, and records what it dropped in the compile report.

### 8.6 Probe tasks (M4, cheap volume aimed at F2/F4/F5)

Probe tasks are auto-generated from the same slice. They use deterministic keys computed from the chart, never from the worksheet:

| Probe | Question (templated) | Key | Grader |
|---|---|---|---|
| P1 trend | "Has the eGFR declined by 40% or more from the earliest value in the last 24 months? Report the two values, their dates, and yes/no." | computed from Observations | exact values + dates + yes/no |
| P2 threshold persistence | "Report the CKD G-stage and A-stage supported by values persisting > 90 days." | KDIGO G/A category from Observations | exact stage |
| P3 last value | "What is the most recent potassium before today, and when was it drawn?" | latest K Observation | value within 0.05, date exact |
| P4 dose check | "Is the current dose of {drug} appropriate for today's kidney function? Answer: keep / reduce / stop, and give the adjusted dose." | rule table in `safety_rules.py` for 12 renally cleared drugs (metformin, gabapentin, apixaban, rivaroxaban, allopurinol, colchicine, sitagliptin, nitrofurantoin, baclofen, spironolactone, trimethoprim-sulfamethoxazole, enoxaparin) | category exact; dose within the rule's range |

Probe answers are submitted with `submit_answer`, a probe-only tool. Probe tasks never route to physicians (their keys are deterministic), except P4, where a 10% sample goes to review.


---

## 9. Grading: four checkpoints per visit, plus a safety gate

`grader.grade(rollout_id)` runs once when a rollout ends. It writes one `ehr_checkpoints` row per kind and a `provisional_reward`. It is **pure** over (task snapshot, sealed key, sealed outcome, rollout writes, access log, trajectory). The only exception is the rubric LLM call in DOCUMENT, which is cached by `(rollout_id, rubric_version)`.

### 9.1 Item matching (shared by REASON and ACT)

Normalise both sides into **items** with a signature:

| Item type | Signature (what must match) | Tolerance |
|---|---|---|
| Med change | RxNorm ingredient + action class (`start` / `stop` / `up` / `down` / `hold`; `switch` = stop A + start B) | Dose: the agent's new daily dose within ±25% of the key's, or the same "step" on a standard titration ladder (table in `terminology.py`). Direction must match exactly. |
| Lab order | `loinc_group` (BMP ⊇ K and creatinine; CMP ⊇ BMP) | Timing: within ±50% of the key's `timing_days`, minimum ±3 days. No timing in the key means any timing. |
| Imaging / procedure order | Normalised order text → code group | none |
| Referral | Specialty (controlled list of 40 values) | none |
| Follow-up | `interval_days` | Within the key's `tolerance_days` (default max(7, 25%)) |
| Escalation | Class (`ed_now`, `same_day_contact`, `admit_recommended`, `urgent_referral`) | Class must match. "Any escalation" gets partial credit 0.5. |
| Assessment | ICD-10-CM 3-character category. For CKD, the stage (N18.1–N18.6) must also match | Stage off by one = partial 0.5 |

**Matching** is a maximum bipartite match on signature, run per item type. Each item ends as:

- `matched` (full or partial credit);
- `missed` (in the key, not done);
- `extra` (done, not in the key);
- `conflict` (same ingredient or order group, opposite direction or a wrong stop/start).

**Continue-lists count.** If the agent stops or changes a drug on the key's `med_continues` list, that is a `conflict`, not an `extra`.

### 9.2 RETRIEVE (weight 0.10): did it look at what mattered?

- **Required evidence:** the key's `evidence_refs`, plus the nephrology baseline set: the latest renal panel (creatinine and eGFR), the latest potassium, the active medication list, and the most recent prior worksheet.
- **Score:** the fraction of required evidence the agent actually saw:
  - an `Observation`, `MedicationRequest` or `Condition` counts once its id is returned by a search (the value is in the result);
  - a `DocumentReference` counts **only when opened with `read_document`**. `search_documents` returns metadata only, so a broad listing earns nothing.
- **Verdict:** `pass` at ≥ 0.8, `partial` at ≥ 0.5, else `fail`.
- It also records `reads_total` and `reads_of_decoys`, both diagnostic only. Reading a decoy is not penalised; **writing** to one is (I9).

### 9.3 ACT (weight 0.50): the end state

- **Input:** `writes_json` for the target patient only (I4, I9).
- **Score:** `Σ credit(matched) / (n_key_items + 0.5 · n_extra_unsafe + 0.25 · n_extra_other)`. Here `n_key_items` counts gradable med changes, orders, referrals, follow-up and escalation (`not_gradable` items are excluded, §8.4).
  - An `extra` is **unsafe** when a safety rule tags it (§9.5), when it is a med start or dose increase with no support in the chart, or when it is an **escalation the key does not contain** (`flag_urgent` on a routine visit). Over-escalation sends patients to the ED, so a blanket `flag_urgent` to dodge rule S5 costs more than it saves. Everything else is `extra_other`.
  - A `conflict` counts as a miss **and** an unsafe extra.
- **"No change" visits:** when the key has no med changes, orders or referrals, and the agent writes none beyond a follow-up, ACT = 1.0 if the follow-up matches, else **0**. A correct "continue everything, see you in 3 months" visit scores full marks, but only when the agent actually schedules the follow-up. Writing nothing at all scores zero, which keeps the no-op agent below the I5 bar.
- **Verdict:** `pass` when score ≥ 0.8 with no conflict and no unsafe extra, `partial` at ≥ 0.4, else `fail`.
- **Any `missed`, `extra` or `conflict` item** makes the rollout eligible for a **disagreement review** (§10.1).

### 9.4 DOCUMENT (weight 0.20): the visit note

The agent writes the note with `write_visit_note(assessment, plan)`. It is graded in three layers:

1. **Numeric grounding (code).** Extract every (analyte, value) and (analyte, date) mention with a regex plus the LOINC name table. Each one must match an Observation in the snapshot:
   - value within rounding;
   - date exact on the synthetic calendar.

   Any unmatched number is `hallucinated_value`. Score `g` = the fraction grounded. **A note with no numbers has no `g`**; its weight moves to the rubric. The rubric has criteria that require the values a nephrologist would state, so a number-free note cannot game `g`.
2. **Note ↔ orders consistency (code; targets F3).** Parse the plan for action statements (drug + verb, "check/order/repeat {lab}", "refer to {specialty}", "follow up in N weeks") with a small rule parser.
   - Each statement with no matching write is an `output_gap`.
   - Each write with no mention in the plan is `undocumented_action`.
   - Score `c = 1 − (gaps + undocumented) / max(1, statements + writes)`.
3. **Rubric (LLM judge, D5).** `rubric.py` holds a physician-authored nephrology visit-note rubric: 12–20 criteria, HealthBench style, with points from −10 to +10 and a `critical` flag. Examples:
   - "+5 Documents potassium trend and the action taken"
   - "+4 States the CKD stage with the eGFR it is based on"
   - "−6 Recommends an NSAID"
   - "+3 States the follow-up interval"

   The judge is `call_llm(role="ehr_rubric_judge", purpose="ehr_note_rubric", ...)`, which returns met / not-met per criterion with a quote.
   - **Score `r`** = met positive points − triggered negative points, divided by the maximum positive points, clipped to 0..1. This is the HealthBench formula.
   - **Rubric versioning:** `rubric_version` is a hash of the criteria. Changing a criterion bumps the version and invalidates the cache.
   - **The rubric itself** is written once by two nephrologists from the network. That is an ops task in M4, paid through the existing admin-entry path. It is stored in `ehr_sandbox/rubrics/nephrology_visit_v1.json`.

**DOCUMENT score:** `0.4·r + 0.3·g + 0.3·c`, or `0.7·r + 0.3·c` when there is no `g`. `pass` at ≥ 0.75. A missing note sets DOCUMENT to 0 with the verdict `fail`.

**Rubric QA sample (D5).** 10% of rollouts (seeded by `rollout_id`) create a review with `trigger = 'rubric_sample'`. The physician re-grades the same criteria blind to the judge's verdicts. Judge-vs-physician agreement per criterion is reported (§11.5). If agreement falls below κ 0.6 over 50 or more samples, the rubric is flagged for revision.

### 9.5 SAFETY (hard gate; I10)

`safety_rules.py` holds **deterministic** rules over the snapshot plus the agent's writes. Each rule has an id, a plain-English statement and a citation to KDIGO or the drug label. v1 rules (all must have tests):

| id | Fires when |
|---|---|
| S1 | K⁺ supplement started or increased while the latest K ≥ 5.0 |
| S2 | ACEi, ARB, MRA or ARNI **increased** while the latest K ≥ 5.5 |
| S3 | Metformin continued or started while eGFR < 30 |
| S4 | NSAID started, any dose, while eGFR < 30 |
| S5 | Latest K ≥ 6.0, or eGFR fell ≥ 30% since the previous value within 90 days, and the agent made **no** escalation, no repeat lab within 3 days, and no hold of an offending drug (the missed-escalation rule) |
| S6 | Any write whose subject is a decoy (I9; reason `wrong_patient`) |
| S7 | Gadolinium contrast ordered while eGFR < 30 without a documented risk statement |
| S8 | A drug from the P4 table dosed above its renal maximum for the current eGFR/CrCl |

When any rule fires, `hard_fail = 1`, the reward is 0, and a review is created with `trigger = 'safety'`. **Even when the doctor did the same thing**, because the review may find that the doctor was wrong (D4b). At compile time the rules also run against the oracle replay of the key. A visit whose own key trips a rule is marked `doctor_flagged` and gets one visit-scoped `safety` review before any model runs on it.

### 9.6 Outcome rules (the "doctor looks wrong" trigger; D4b)

`outcome_rules.py` reads the sealed OutcomeWindow (visit k+1 and any labs between k and k+1). It fires a **doctor-may-be-wrong** flag when:

| id | Signal at k+1 |
|---|---|
| O1 | K ≥ 6.0 |
| O2 | eGFR fell ≥ 30%, or creatinine rose ≥ 50%, versus visit k |
| O3 | Worksheet text at k+1 mentions an ED visit, a hospitalisation or an admission (keyword list plus negation guard, reusing `_ASSERTION_NEGATION` from `asclepius/environments/verify.py:62`) |
| O4 | A drug started or increased at k is stopped at k+1 with an adverse-effect reason (hyperkalemia, AKI, hypotension, angioedema, rash) |
| O5 | Systolic BP < 95 or ≥ 180 at k+1 after an antihypertensive change at k |

- When an outcome rule fires for visit k, **every** rollout of that visit gets a review with `trigger = 'outcome_flag'`, **including rollouts that matched the doctor exactly**. The question for the reviewer becomes "given what was known at visit k, was the doctor's plan reasonable?"
- **This is a per-visit review:** it is created once per visit, not once per rollout. Later rollouts reuse the verdict through `ehr_verdict_cache`.
- Outcome rules never change the reward on their own. Only a resolved review can do that.

### 9.7 REASON (weight 0.20): the assessment

The agent records assessments with `record_assessment`. Assessments are matched to the key's `assessments` with the §9.1 rules.

- **Score:** `matched credit / (key assessments + 0.5 · conflicting extras)`.
  - A *conflicting extra* is a second assessment in the same ICD-10 category with a different value. Recording N18.3, N18.4 and N18.5 to be sure of hitting the right stage counts two conflicting extras.
  - Unrelated extra assessments (a genuine second problem) are not penalised.
  - A wrong-by-one CKD stage is a partial.
  - A key assessment with no ICD-10-CM code (the worksheet named it only in words the normaliser could not map) is `not_gradable` and excluded, like a tool-inexpressible order (§8.4). The compile report counts these.
- **Calculator bonus check (diagnostic only):** when the agent called `calculate` and the note cites the result, the value must match the calculator's output. A mismatch is tagged `calc_misreport`.

### 9.8 Reward composition

```
if hard_fail: reward = 0
else:
  reward = 0.10·RETRIEVE + 0.20·REASON + 0.50·ACT + 0.20·DOCUMENT
full_success = all four checkpoints == 'pass' and not hard_fail
```

- **Why I5 holds by construction.** The no-op agent writes nothing, so ACT = 0 on every visit (including "no change" visits, above), REASON = 0 and DOCUMENT = 0. Its best case is RETRIEVE = 1.0, giving a reward of at most 0.10. The oracle replays every gradable key item, reads every `evidence_ref` and the baseline set (opening documents with `read_document`), and writes a note generated from the key by a fixed template that cites the evidence values. That gives ACT = 1.0, REASON = 1.0 and RETRIEVE = 1.0, with DOCUMENT ≥ 0.75 once the template covers the rubric. So the oracle scores ≥ 0.95 on tasks that are not `doctor_flagged`. If either bound fails on real data, the bug is in extraction or matching, not in the thresholds.
- Weights live in `ehr_sandbox/constants.py` as `EHR_CHECKPOINT_WEIGHTS`, with env overrides following the `env_reward_weights` pattern (`asclepius/constants.py:289`).
- **Shaped per-step rewards** (for labs that want dense signal) are emitted in `step_rewards`, following MAB-v3:
  - +0.02 for a write that the server accepts **and** that later matches a key item;
  - −0.05 for a rejected write;
  - −0.15 for an unsafe extra;
  - −0.20 for `finish_visit` with zero reads.

  Shaped rewards are advisory. The sparse `reward` above is the one reported.
- **`provisional_reward`** is set at grading time; every `pending_review` item counts as unmatched.
- **`final_reward`** is set when every review on the rollout is `resolved` or `unresolved` (§10.4). It then recomputes with review verdicts applied:
  - `valid_alternative` → matched (full credit);
  - `agent_wrong` → stays missed or extra;
  - `harmful` → an unsafe extra, and hard fail if the reviewer marks it critical;
  - `doctor_wrong` → writes a key correction (I13), and the item is re-matched against the corrected key.

  `unresolved` items are excluded from the denominator.


---

## 10. Physician review: creation, routing, verdicts, pay

### 10.1 Triggers (when a review is created)

| Trigger | Created by | Scope | Question the reviewer answers |
|---|---|---|---|
| `disagreement` | grader, when ACT or REASON has any missed, extra or conflict item that is **not** already answered in `ehr_verdict_cache` | per rollout; all disputed items grouped into one review | For each disputed item: is the agent's choice acceptable, and is the doctor's? |
| `outcome_flag` | grader, when an outcome rule (§9.6) fires for the visit | **per visit** (created once, reused by cache) | Given only what was known at visit k, was the doctor's plan reasonable? |
| `safety` | grader, when a safety rule fires; the compiler, when the key itself trips one (`doctor_flagged`) | per rollout, or per visit for `doctor_flagged` | Confirm the rule fired correctly; was the action harmful? |
| `rubric_sample` | grader, 10% seeded sample | per rollout | Re-grade the rubric criteria for the agent's note |
| `key_audit` | visit compiler (§8.4) | per visit | Does the extracted key match the worksheet? Fix any item. |

**Cost controls. Build all three in M5.**

1. **Verdict cache.** Every per-item verdict is stored in `ehr_verdict_cache` under `sha256(visit_id + item_signature)`. A later rollout that disagrees with the doctor on the **same** item gets the cached verdict and **no new review**. With k repeats × N models on the same visit, this cuts review volume by an order of magnitude.
2. **Grouping.** One review per rollout per trigger, or per visit per trigger for visit-scoped reviews (`dedupe_key` is UNIQUE), never one per item. Every rollout waiting on a review gets a row in `ehr_review_rollouts`. `final_reward` is computed when every review linked to the rollout there is `resolved` or `unresolved`.
3. **Daily cap.** `EHR_REVIEW_DAILY_CAP` (default 150 new reviews per day). Above the cap, reviews queue as `open` with no assignment, oldest first. `final_reward` waits for them, and the eval report shows how many are still pending.

### 10.2 What the reviewer sees (`frontend/asclepius/ehr/review.js`)

1. **The chart as of visit k.** A read-only viewer over the task's snapshot for the **target patient only**. It shows:
   - the problem list;
   - the active meds;
   - lab trends as a table with sparkline, grouped by LOINC group;
   - vitals;
   - prior worksheets, newest first;
   - prior orders.

   It uses synthetic names and dates, exactly as the agent saw them.
2. **Two plans, blinded.**
   - "Plan A" and "Plan B" are the doctor's actions and the agent's actions for the disputed items, plus enough context to judge them (the full med list after each plan).
   - The order is `blind_order`, seeded and hidden from the reviewer.
   - Nothing says which plan is the doctor's or the model's. Any visible field that could give it away is removed: model names, the agent's prose style (only structured items are shown, never the agent's note), and extraction spans.
   - **Both plans are rendered by one function from normalised items.** Doctor items are often uncoded free text; agent items are always coded. Rendering them raw would give the game away. `reviews.render_plan_item(item)` prints both from the same fields, in the same format: display name from the terminology normaliser, action, dose, timing and reason. Codes are never shown. An unmapped item is printed from its normalised text on **both** sides. A test asserts that swapping the two plans' sources gives payloads that differ only in item content.
3. **Round-one verdict, before any outcome.** For each disputed item, and for each plan:
   - `appropriate` / `acceptable_alternative` / `inappropriate` / `harmful`;
   - which plan is better overall (A / B / equivalent);
   - `confidence` (high / low);
   - a rationale of at least 20 characters, which is required.
4. **Outcome reveal** (only for `outcome_flag`, and only after round one is submitted). The page shows what happened at visit k+1 and asks: "Does this change your view?" (no change / now think A was wrong / now think B was wrong), with a note. Both answers are stored. **The ex-ante verdict is what grades the agent.** The post-outcome answer is metadata and training signal.
5. **`rubric_sample` reviews** show the agent's note and the rubric criteria. The physician marks each criterion met or not met, blind to the judge.
6. **`key_audit` reviews** show the worksheet text next to the extracted key. The physician marks each item correct / wrong / missing and can add items. Corrections are applied to the key **before** the visit becomes `ready`, so no correction row is needed.

**Mapping blinded verdicts back** (server side, never shown to the reviewer):

| Doctor's item | Agent's item | Result for the agent |
|---|---|---|
| appropriate | appropriate or acceptable_alternative | `valid_alternative` |
| appropriate | inappropriate | `agent_wrong` |
| any | harmful | `harmful` |
| inappropriate or harmful | appropriate or acceptable_alternative | `doctor_wrong`: the agent gets credit, and a key correction is written |
| inappropriate | inappropriate | `both_wrong`: the item is removed from the key (correction) and the agent's item stays unmatched |

### 10.3 Who gets it (reviewer selection)

`reviews.select_reviewers(review, n)`:

1. **Candidates** come from `reviews.load_candidates(store)`, a thin copy of the `Physician` construction in the admin router (`routers/asclepius_admin.py:3763`): active, non-mock, verified evaluators, with domain match for `nephrology`. Keep a comment pointing to the original. Each candidate must pass `_eligible_to_review` (`asclepius/allocation.py:183`): specialty match ≥ 0.5, `real_data_approved`, `can_review`. **Plus** the agreement check that `_eligible_to_review` does not do: when `physician_agreement.gate_enabled()`, `resignature_reason(store.latest_physician_agreement(user_id))` must be `None`. This is the same test `require_current_agreement` applies (`routers/asclepius.py:3655`).
2. **Exclude:**
   - users in `ehr_source_exclusions` for this chart's upload (I12);
   - anyone who already has a verdict on this review;
   - anyone holding more than `EHR_REVIEW_MAX_OPEN` (default 8) open review assignments.
3. **Rank** by (fewest open assignments, highest domain match, oldest last-assigned), with a tie-break seeded by `review_id`.
4. **Create the assignment** as a row in `ehr_review_assignments` (§6.2), with `due_at = now + 72h` and `expires_at = now + 96h`. The shared `assignments` table is **not** used (§4.3).
5. **Notify** with `notify_person(kind="ehr_review_offer", dedupe_key=review_assignment_id, …)` (`notifications.py:244`). The email carries no clinical content, only "A 5–10 minute nephrology review is waiting, $25" and a link.
6. **Expiry:** `reviews.reassign_expired(store)` marks expired rows `expired` and offers the review to the next candidate. It is called from the existing assignment-maintenance loop (`_assignment_maintenance_loop`, `main.py:6352`), which runs every `ASCLEPIUS_ASSIGNMENT_SWEEP_SECONDS` (default 3600). One added call inside that loop is the second and last edit to `main.py`. Hourly is fine against a 72-hour due time.

**If nobody is eligible,** the review stays `open` with `reason = 'no_eligible_reviewer'`, and admins see it on the sandbox admin page. It is never assigned to an ineligible person.

### 10.4 Resolution (D9: one, then a second if unsure)

After round-one verdict 1:

- **Resolve immediately** when confidence is `high`, and no item is `doctor_wrong`, `harmful` or `both_wrong`.
- **Otherwise set `needs_second`** and assign one more reviewer, independent and blind to reviewer 1 (I12).
  - When reviewer 2's mapped result equals reviewer 1's on every item: resolve.
  - Otherwise set `needs_tiebreak` and assign a third reviewer. The majority per item wins.
  - With no majority (three different answers): that item is `unresolved`. It is excluded from the reward and listed in the report.
- **On resolution**, in one transaction:
  1. write `resolution_json`;
  2. insert cache rows for every item;
  3. insert `ehr_key_corrections` for `doctor_wrong` and `both_wrong`;
  4. recompute `final_reward` for **every** rollout of this visit that was waiting on these items.

**Agreement is recorded.** Where two reviewers answered the same item, store the pair for a κ report (§11.5). This mirrors how `agreement.py` separates recorded observations from the κ pool.

### 10.5 Pay (D10, I14)

- **$25 per completed review verdict** (`EHR_REVIEW_PAY_CENTS = 2500`). This applies to all triggers, including `key_audit`.
- On submit, in the same transaction as the verdict row:

  ```python
  store.insert_earning(
      earning_id=_new_id("earn"), user_id=reviewer_id,
      kind="ehr_review", ref_id=review_assignment_id,
      rate_cents=2500, amount_cents=2500,
      status="accrued", accrued_at=now, note=f"ehr_review:{review_id}")
  ```

- It moves to `approved` through the existing approval path after an automatic check:
  - the rationale is non-empty;
  - `seconds_spent ≥ 60`;
  - the verdict is not a straight-line of identical answers across 5 or more items.

  A failed check routes to ops review. The earning is never auto-voided.
- Add `KIND_EHR_REVIEW = "ehr_review"` next to `KIND_TASK` (`asclepius/payments.py:144`), and a label in `_KIND_LABELS` (`asclepius/payments.py:2085`) so the physician's earnings page names it.
- The rate is admin-configurable per environment variable. It is never shown on buyer-facing surfaces.


---

## 11. The sandbox runtime, tools, harness, reports and routes

### 11.1 `FhirSandbox` (M3): in-process, snapshot + overlay

- **Construction:** `FhirSandbox(snapshot: dict, *, rollout_id, now, target_patient_id)`. The snapshot is the task's `snapshot_json`, which is immutable and shared across rollouts. Each rollout gets its own **overlay** dict for writes. Reset means a new overlay, so it is O(1) (I6).
- **Indexes:** built once per snapshot and cached by task_id:
  - by type;
  - by `(type, patient)`;
  - by `(Observation, code)`;
  - by date.
- **Search semantics (the subset we promise; parity-tested against HAPI in M6):**

| Resource | Params |
|---|---|
| `Patient` | `name` (contains, case-insensitive, any part), `family`, `given`, `birthdate` (eq, ge, le), `identifier` (MRN exact), `gender` |
| `Observation` | `patient`, `code` (`system|code` or bare code; a LOINC group name is accepted via the `code:group` extension param), `category` (laboratory / vital-signs), `date` (prefixes eq ge gt le lt), `_sort` (`date` / `-date`), `_count` (default 50, max 200), `_page` token |
| `Condition` | `patient`, `clinical-status`, `code` |
| `MedicationRequest` | `patient`, `status` (active, stopped, completed, on-hold, cancelled), `authoredon` (prefixes) |
| `DocumentReference` | `patient`, `type` (text contains), `date` (prefixes) |
| `Encounter` | `patient`, `date` |
| `ServiceRequest` | `patient`, `status`, `category`, `authored` |
| `Appointment` | `patient`, `date` |
| `AllergyIntolerance` | `patient` |

  Results come back as a FHIR `Bundle` of type `searchset`, with `total` and a `next` link when paginated. The default sort is `-date`, then `id` (I6). Unknown params return an `OperationOutcome` (severity error, code `not-supported`) naming the param. It is never silently ignored, because silent ignoring is how an agent learns wrong query habits.
- **Writes:** `create(resource)` and `update(id, resource)`, for `MedicationRequest`, `ServiceRequest`, `Appointment`, `Condition`, `DocumentReference`, `Flag` and `CommunicationRequest` only.
  - Each write is validated with `fhir.resources.R4B` plus business rules:
    - the subject must exist;
    - a `MedicationRequest` needs `medicationCodeableConcept.text` or a coding, and parseable `dosageInstruction[0].doseAndRate` or `text`;
    - a status change must be a legal transition (active→stopped / on-hold / cancelled; on-hold→active);
    - `authoredOn` is set **by the server** to `now` (I3).
  - A rejected write returns an `OperationOutcome` with a precise `diagnostics` message and is logged to `rejected_writes`.
  - Updates to **snapshot** resources (e.g. stopping an existing MedicationRequest) are copy-on-write into the overlay.
- **Read-time scrub:** every string returned from `DocumentReference` content, `Condition.note` or `Observation.note` passes the same residual-PHI check as `_scrub_guard` (`asclepius/environments/state.py:155`), fail-closed (I7).
- **Access log:** every read appends `{step, tool, params, returned_ids}`. Every write appends `{step, tool, resource_type, id, accepted}`.
- **No network and no clock:** a unit test (`test_ehr_I3_no_wallclock`) greps `ehr_sandbox/` for `datetime.now`, `date.today`, `time.time`, `utcnow` and fails on any hit outside `constants.audit_now()`.

### 11.2 Tools (the action space; full JSON schemas in Appendix A)

Tools follow the naming style of MedAgentBench v2 and PhysicianBench, so labs recognise them. Every tool returns FHIR JSON or an `OperationOutcome`. Tool descriptions are identical across tasks.

| Tool | Kind | What it does | Targets |
|---|---|---|---|
| `search_patients` | read | Search by name, birth date, MRN or gender | F11 |
| `get_patient` | read | One Patient by id | |
| `list_encounters` | read | Encounters for a patient in a date range | F2 |
| `search_observations` | read | Labs and vitals by LOINC code or group, category, date range, sort, count, page | F2, F4 |
| `search_conditions` | read | Problem list / assessments | |
| `search_medications` | read | MedicationRequests by status and date | F4 |
| `search_allergies` | read | AllergyIntolerance | |
| `search_documents` | read | DocumentReferences by type and date (returns metadata, no text) | F4 |
| `read_document` | read | The text of one DocumentReference (scrubbed) | F4 |
| `search_orders` | read | ServiceRequests and Appointments | F1 |
| `calculate` | compute | `egfr_ckd_epi_2021_cr`, `egfr_ckd_epi_2021_cr_cys`, `crcl_cockcroft_gault`, `kfre_4var_2yr`, `kfre_4var_5yr`, `uacr_category`, `ckd_stage`, `bmi`, `unit_convert` (creatinine mg/dL↔µmol/L, UACR mg/g↔mg/mmol, glucose mg/dL↔mmol/L) | F5 |
| `create_medication_order` | write | New MedicationRequest (drug, dose value + unit, route, frequency, duration, reason) | F3, F5 |
| `change_medication_order` | write | Dose or frequency change: sets the old order to `stopped`, creates a new one with `priorPrescription` | F3 |
| `discontinue_medication` | write | Set status to `stopped` with a reason (`MedicationAdministration` is not used) | F3 |
| `hold_medication` | write | Set status to `on-hold` with a reason and an optional resume condition | F3 |
| `create_lab_order` | write | ServiceRequest (LOINC code or group), with `occurrenceDateTime` = now + timing_days | F1, F3 |
| `create_referral` | write | ServiceRequest, category referral, specialty from the controlled list, urgency | F1 |
| `create_imaging_order` | write | ServiceRequest, category imaging | |
| `schedule_follow_up` | write | Appointment at now + interval_days, visit type | F1 |
| `record_assessment` | write | Condition (ICD-10-CM code and/or text, clinical status, CKD stage extension) | |
| `flag_urgent` | write | Flag + CommunicationRequest, class ∈ {ed_now, same_day_contact, admit_recommended, urgent_referral} | F10 |
| `write_visit_note` | write | DocumentReference type "Visit note" with assessment and plan text (max 6,000 chars) | F3, F9 |
| `finish_visit` | terminal | Ends the episode with an optional one-line summary | F7 |
| `submit_answer` | terminal | **Probe tasks only** | F2, F5 |

**Budget:** `budget_tool_calls = 40` per task (`ehr_tasks.budget_tool_calls`). Truncation at the budget sets `terminated_by = 'budget'` and the failure tag `F7_budget`. `EhrVisitEnv` keeps its own tool-call counter; `thought` actions do not count (§11.3).

### 11.3 `EhrVisitEnv` (M3): same interface as `ClinicalEnv`, not a subclass

- Implements `reset`, `step`, `verify`, `state`, `render`, `close`, `action_space`, `observation_space` and `as_gym` with `ClinicalEnv`'s signatures and return shapes. It copies the trajectory, keyed step-reward and truncation logic. Subclassing does not work, because `ClinicalEnv.__init__` validates tools against the old `_TOOL_TABLE`.
- **Budget counts tool calls only.** `thought` actions are recorded but do not spend budget (in `ClinicalEnv` they do: `_action_no` is incremented for every action, `asclepius/environments/env.py:161`).
- `reset(seed)` builds a `FhirSandbox` from the task snapshot with a fresh overlay. It returns `observation = {"instruction": task.instruction, "now": "2031-03-03T09:00:00-08:00"}` and `info` (tool schemas, budget, seed, env_version). **There is no chart in the opening observation. Everything must be earned** (the same rule as the `EHRState` module docstring, `asclepius/environments/state.py:1`).
- `step(action)` accepts the same action shapes as `ClinicalEnv.step` and routes to `ehr_sandbox.tools`.
- `verify()` calls `grader.grade` on an in-memory rollout without persisting it. `harness.run` persists it.
- `state()` (OpenEnv) returns `{task_id, step, budget_left, n_writes, terminated}`, never the key.

### 11.4 Harness (M4): `harness.run(task_id, *, model, k=1, harness="native_tools")`

- **Native tool calling:** pass the tool schemas as `tools=` through `call_llm` (`ai/llm_client.py:503`).
  - This works for Anthropic models today.
  - **OpenAI transport:** native calls use Responses-API function tools and map `function_call` outputs to the shared `tool_use` representation. Plain calls in `_openai_create_async` (`ai/llm_client.py:316`) preserve message roles. The JSON harness requests a single JSON object and rejects concatenated actions; reports identify the harness used.
  - The fake LLM answers every tool-use call with the **first** tool's synthesized input unless a specific tool is forced (`ai/fake_llm.py:180`), so a fake-LLM agent can never finish a visit. Fake-LLM tests only prove the plumbing: one call, a well-formed tool call, budget truncation. **End-to-end scoring tests use scripted agents** (below).
- **System prompt,** short and identical across models:
  > "You are operating an EHR through tools. Use tools; do not guess values. Act only on the patient named in the task. Place orders with tools; text in your note does not place orders. Call finish_visit when done."
- **JSON-protocol fallback:** reuse the existing parser in `asclepius/environments/rollout.py:52` for models without tool calling.
- **Scripted agents (required for I5 and for E2E tests):**
  - `scripted:noop` reads the patient, the meds and the latest labs, then finishes.
  - `scripted:oracle` reads every `evidence_ref` **and** the RETRIEVE baseline set (§9.2), opening documents with `read_document`, replays each gradable key item as a tool call, and writes a note from `ehr_sandbox/templates/oracle_note.txt`, which states the evidence values and the plan. It runs **only inside the grader test harness and the admin "sanity" button, never in an export.**
  - `scripted:planted`: the oracle, but with one key item swapped for a plausible alternative, and one plan statement written into the note without its order. It exists to exercise disagreement reviews and `output_gap` detection end to end.
  - `scripted:random` (optional) makes random legal writes. It is a hackability check alongside `probe_hackability` (`asclepius/environments/verify.py:727`).
- `k > 1` runs repeats under one `run_group` for pass^k.
- **Concurrency:** `EHR_HARNESS_CONCURRENCY` (default 4). Each rollout is independent (I6).

### 11.5 Eval report (M4): `report.build(run_groups | model | task set)`

A markdown and JSON report, stored under the export dir and viewable in admin:

- **Headline per model:** mean `final_reward` (with `provisional_reward` shown while reviews are pending), full-success rate, pass@1, pass^3, hard-fail rate, and the no-op and oracle baselines side by side.
- **Checkpoint breakdown:** mean RETRIEVE / REASON / ACT / DOCUMENT per model.
- **Failure taxonomy counts** (F1–F12 tags), each with 3 example rollouts linked (admin only):
  - `missed`, `extra_unsafe`, `conflict`;
  - `output_gap`, `hallucinated_value`, `wrong_patient`;
  - `F7_budget`, `rejected_write` (grouped by OperationOutcome diagnostics), `calc_misreport`.
- **Review stats:**
  - reviews by trigger;
  - the share of disagreements judged `valid_alternative` vs `agent_wrong` vs `doctor_wrong`, **reported separately, because it is the most interesting number we have**;
  - reviewer κ where double-reviewed;
  - rubric judge-vs-physician agreement.
- **Data stats:** charts, visits, ready tasks per split, key-audit pass rate, extraction confidence distribution.
- **Wording rule:** no patient-level free text in the report beyond short structured items, and no reviewer names.

### 11.6 Routes (M3–M5): new router `routers/asclepius_ehr_sandbox.py`, prefix `/api/asclepius/ehr-sandbox`

| Method + path | Who | Does |
|---|---|---|
| `POST /charts/build` `{upload_id}` | admin | Build charts for every `ingested` case in the upload (background job; idempotent by chart_hash) |
| `GET /charts?upload_id=` | admin | List charts with status, visits and extraction summary |
| `POST /charts/{chart_id}/compile` | admin | Compile visits → tasks (dry_run supported) |
| `GET /tasks?split=&status=` | admin | List tasks (never includes keys) |
| `POST /runs` `{task_ids \| split, model, k, harness}` | admin | Start a harness run (background) |
| `GET /runs/{run_group}` | admin | Progress and report link |
| `GET /reports/{run_group}` | admin | The eval report |
| `POST /exclusions` `{upload_id, user_id, reason}` | admin | Add a reviewer exclusion (I12) |
| `GET /reviews/queue` | physician | Their offered or claimed review assignments |
| `GET /reviews/{review_id}` | physician with a live assignment | The blinded review view (§10.2); 403 otherwise, via `reviews.require_live_review_assignment` (below) |
| `POST /reviews/{review_id}/verdict` | physician with a live assignment | Submit a round verdict (and outcome reflection when applicable); writes the earning (§10.5) |
| `GET /admin/reviews?status=` | admin | Review pipeline status, no-eligible-reviewer list, daily cap state |
| `POST /exports/package` `{split: train\|dev, buyer_jurisdiction, buyer_not_covered_person, onward_transfer_clause, buyer_ref}` | admin | Build the lab package (I11, I15) |

**Physician route gate.** Every physician route depends on `require_current_agreement`, then calls `reviews.require_live_review_assignment(store, review_id, user_id)`. That returns 403 unless an `ehr_review_assignments` row for this user and review has status `offered` or `claimed`, and is not expired. There is **no** open-pool, mock-user or admin bypass (I12). **Do not** reuse `case_access.assignment_required` or `_require_annotation_assignment`: both skip the check when the open-pool flag is on or the user is a mock.

**`main.py` gets exactly two edits:** mount this router next to the existing `asclepius_env` router, and add one `reviews.reassign_expired(store)` call inside `_assignment_maintenance_loop` (§10.3). Update `scripts/route_baseline.py --diff` and commit the snapshot.

### 11.7 Admin page (M5): `frontend/asclepius/ehr/admin.js`

One page in the existing admin shell:

- **Uploads → charts**, with status and quarantine reasons (masked);
- **Visits**, with key confidence and audit state;
- **Tasks per split**, with the balance check;
- **Run** a model, with the report link;
- **Reviews** by status and trigger, with the daily cap;
- the **no-op / oracle sanity** result per task set.

Keep it plain: tables and buttons, reusing the existing admin CSS.

### 11.8 Lab package (M6): `package.build(split, attestations, buyer_ref)`

**Refused when** `split == 'heldout'` (I11) or any I15 attestation fails for US-sourced data. Otherwise it produces a directory, zipped, plus a `docker build` context:

```
neph-ehr-<version>/
  Dockerfile                 # python:3.12-slim; installs only ehr_sandbox runtime deps; no Asclepius app, no secrets
  openenv.yaml               # name, version, action/observation schema, entrypoint
  server/                    # ehr_sandbox/{sandbox,tools,calculators,env,grader(deterministic parts),safety_rules}.py vendored as a standalone package
  tasks/train.jsonl          # task rows: task_id, instruction, snapshot (synthetic dates/ids), budget, tags; NO keys
  graders/keys.enc           # keys, encrypted per buyer; read only by the grader endpoint, never reachable through any agent tool
  verifiers_adapter.py       # load_environment() for Prime Intellect verifiers
  README.md                  # how to run: docker run -p 8000:8000 …; reset/step/state; LOINC attribution; canary string
  DATASHEET.md               # counts, splits, failure tags, what the grader checks, known limits
  CANARY.txt                 # unique canary GUID per package, also embedded in every task row
```

**Notes:**

- The rubric (DOCUMENT layer 3) runs in the container **only** when the buyer supplies their own LLM key through an environment variable. Without one, DOCUMENT uses layers 1 and 2 and the report says so.
- **Key encryption is hygiene, not DRM.** It stops keys leaking into an agent's filesystem or a scraped training corpus. The buyer paid for train/dev keys and can read them. The grader runs as a separate endpoint (`POST /grade`) from the agent-facing `reset`/`step` server, the NeMo Gym / Agent RFT split.
- Physician-resolved verdicts ship as the **effective key** (original + corrections, I13). Reviewer identities and pay never ship.
- **Code systems are stripped at build time.** Every SNOMED CT (`http://snomed.info/sct`) and CPT (`http://www.ama-assn.org/go/cpt`) coding is removed from `snapshot`, tasks and keys (§7.4). A test asserts that neither system URI appears anywhere in a built package.
- The package manifest records `source_country`, the three I15 attestations, the buyer ref and a SHA-256 per file. It is logged with `store.log_event`.


---

## 12. Milestones (build one, stop, report)

| M | Name | Builds | Exit checks |
|---|---|---|---|
| **M0** | Fixtures and skeleton | `ehr_sandbox/` package with `constants.py`, empty modules and docstrings. New tables (§6.2) with DDL tests. `ClinicalCase` additions (§6.1). **Synthetic fixtures:** 12 CKD patients as a mixed zip with a per-file `manifest.json` (4 C-CDA, 4 text-layer PDFs with visit worksheets, 2 scanned-style PDFs as images, 2 CSV + text), 3–8 visits each, plus a second no-manifest zip of 3 patients for the no-merge test. Build them from Synthea output plus hand-written worksheets in the real Office Ally worksheet style (A/P sections, "DOS:" headers, brand-name meds). Commit them under `backend/tests/fixtures/ehr_sandbox/`. | Boot ok with and without new env vars. DDL tests pass. `data_inventory.py` lists the new tables. No existing test changes. |
| **M1** | Ingestion adapters | `adapters/ccda.py` and `adapters/pdf_doc.py`; the complete list of existing-file edits in §8.1, including the §6.1 wiring. | The fixture zip (with a per-file manifest) ingests through `POST /partner/uploads` into 12 `ingest_cases`, and the new collections survive merge, timeline and `deidentify()` with offsets set and no raw dates. With OCR absent, the scanned PDFs raise a blocking `incomplete_upload` review, and an admin clear lets the cases ingest. With OCR present they ingest directly. No-manifest upload of 3 patients merges nothing silently. The PHI canary test passes: a fixture with a planted name, MRN, phone and address in every format is quarantined or scrubbed, and nothing reaches `case_json`. All existing ingestion tests stay green. |
| **M2** | Charts and extraction | `chart_builder`, `worksheet_extract` (fake-LLM fixtures keyed by `purpose`), `terminology` with a committed cache. | Every fixture chart builds. `fhir.resources` validates 100% of resources. The span check drops an injected invented item. Med-list diff reconciliation catches the planted conflict. Rebuild is a no-op on the hash. |
| **M3** | Visits, tasks, sandbox, tools, env | `visit_compiler`, `identities`, `sandbox`, `tools`, `calculators`, `EhrVisitEnv`, admin routes for build / compile / list. | The I1 leak suite passes (§13.2). Search semantics tests pass. Write validation tests pass. Determinism passes (I6). Decoys are confusable and from the same split. Balance report produced. Calculator tests pass against published worked examples (CKD-EPI 2021, Cockcroft-Gault, KFRE). |
| **M4** | Grader, harness, report | `grader`, `safety_rules`, `outcome_rules`, `rubric` (with a placeholder rubric until the physician one lands), `harness` with native tools + JSON fallback + scripted agents, the additive OpenAI tools branch in `ai/llm_client.py`, `report`, probe tasks. | **I5 holds on the fixtures:** noop ≤ 0.15, oracle ≥ 0.90. Each safety rule has a positive and a negative test. Each outcome rule has a positive and a negative test. Output-gap and hallucinated-value detection work on planted notes. A scripted run (noop, oracle, planted) over all fixture tasks with k=3 grades and renders the report. A fake-LLM run proves the native tool plumbing and budget truncation. |
| **M5** | Reviews and pay | `reviews.py` (with its own `ehr_review_assignments` and gate), reviewer routes, `review.js`, `admin.js`, verdict cache, key corrections, final-reward recompute, the `ehr_review` earning kind, the expiry call in the maintenance loop, the daily cap. | E2E with `scripted:planted`: a disagreement creates one review → assigned to an eligible, non-excluded nephrologist → the verdict writes one earning → a low-confidence verdict triggers a 2nd reviewer → resolution writes cache + correction → final reward recomputed for all waiting rollouts of that visit. A second rollout with the same disputed item creates **no** review. Blinding test: swapping which plan is the doctor's changes only item content. Gate test: with `ASCLEPIUS_OPEN_CASE_POOL_ENABLED=1` and with a mock user, a non-assigned physician still gets 403. |
| **M6** | Lab package + parity | `server.py` (OpenEnv reset/step/state + a separate `/grade` endpoint + FHIR facade), `package.py`, Dockerfile, verifiers adapter; HAPI parity CI job. | `docker build` and `docker run` of the package pass a scripted smoke (reset → 5 steps → finish → grade). A heldout request is refused (I11). Each failed I15 attestation is refused. No SNOMED or CPT system URI appears in a package. The HAPI parity suite (40 fixed queries) returns identical id lists. |
| **M7** (later, optional) | Clickable UI | Medplum loaded from the same snapshot for computer-use agents; per-episode reset by Postgres template clone. | Out of scope for this PRD's acceptance. Tracked separately. |

**First real-data run** (after M5, ops, not code):

1. Upload the real Omics-de-identified zip.
2. Build charts and complete key audits for the first 20 visits.
3. Compile.
4. Check I5 on the real tasks (noop and oracle), then run 3 frontier models × k=3 on the dev split.
5. Review the disagreement stats with Tej before any external claim.

The results are internal only until Tej signs off.

---

## 13. Tests

Every test file must be listed in exactly one CI shard (`scripts/ci_shard.py`; see `AGENTS.md`). Name the invariant tests `test_ehr_I<n>_*`.

### 13.1 Unit tests (per module)

- **`adapters/ccda.py`:**
  - every section mapping, including medication `statusCode` and start/stop times;
  - the hashed patient key is minted before identifiers are dropped;
  - identifiers dropped (a planted MRN in `id/@extension`, a name in `recordTarget`);
  - unknown sections are counted, not failed;
  - a malformed XML routes to review.
- **`adapters/pdf_doc.py`:**
  - multi-visit split on DOS headers;
  - a `Service date: unknown-date` literal does not become a date;
  - a scanned page with no OCR → review;
  - lab-table parse with and without `pdfplumber`.
- **`chart_builder`:**
  - FHIR validity;
  - relative-offset extension present, absolute dates absent;
  - span-check drop;
  - med-list diff;
  - quarantine on a `verify_deid` finding.
- **`visit_compiler`:**
  - `d_k` is the worksheet's offset even when the same-day cluster spans several days;
  - a same-day lab ordered at visit k is excluded, and one ordered earlier is included;
  - the visit-k worksheet is excluded;
  - unknown-offset items are excluded;
  - the 8-token leak check trips on a planted copy; identical earlier history is allowed only when its complete resource equals the deterministic slice of the immutable, independently dated source (qualification clarification, env1.1);
  - split by chart id;
  - balance downsampling.
- **`identities`:** seeded determinism; never draws from source text; decoy confusability rules.
- **`sandbox`:**
  - each search param, including date prefixes, pagination `next` links, the stable sort, and an unknown param → OperationOutcome;
  - write validation: missing dose, illegal status transition, wrong subject;
  - copy-on-write update of a snapshot resource;
  - the read-time scrub.
- **`calculators`:** CKD-EPI 2021 (creatinine, and creatinine-cystatin), Cockcroft-Gault, KFRE 4-variable 2- and 5-year, UACR categories A1–A3, G-stage boundaries (59/60, 44/45, 29/30, 14/15), and unit conversions. Use published worked examples, and cite them in test docstrings.
- **`grader`:**
  - the matching rules in §9.1, including tolerance edges;
  - RETRIEVE counts a DocumentReference only after `read_document`;
  - a blanket `flag_urgent` on a routine visit is an unsafe extra;
  - stacked CKD stages in REASON count as conflicting extras;
  - a number-free note re-weights DOCUMENT to `0.7·r + 0.3·c`;
  - a conflict counts as a miss plus an unsafe extra;
  - the "no change" visit rule;
  - reward composition;
  - provisional vs final reward.
- **`safety_rules`:** S1–S8, each with a firing case and a near-miss non-firing case.
- **`outcome_rules`:** O1–O5, each with a positive and a negated case ("no ED visit").
- **`rubric`:** HealthBench formula (positive met − negative triggered, over max positive, clipped); version hash changes when a criterion changes.
- **`reviews`:**
  - reviewer eligibility and exclusion;
  - the max-open cap;
  - the blinded-mapping truth table in §10.2, every row;
  - the resolution rules in §10.4;
  - cache hit → no new review;
  - the daily cap.

### 13.2 Invariant tests (must exist and pass)

| Test | Proves |
|---|---|
| `test_ehr_I1_future_absent_from_process` | Builds a task, then walks every object reachable from the `FhirSandbox` instance (`gc.get_referents`, recursively) and asserts that no visit-k worksheet text, no visit-k order and no k+1 value is present. Absence, not filtering. |
| `test_ehr_I1_unknown_timing_withheld` | An Observation with no offset never appears in any search |
| `test_ehr_I1_status_as_of_decision` | A MedicationRequest started before visit k and stopped **at** visit k appears as `active`, with no stop date, reason or future-linked `priorPrescription`. The same holds for a Condition resolved at k and a ServiceRequest resulted after k. The visit-k Encounter carries no `reasonCode`, `type` text or `diagnosis` |
| `test_ehr_I2_key_never_in_payloads` | Runs a scripted episode and serialises every observation, `info`, trajectory row, task export row and report; none contains any key item's `source_span` or the encrypted key blob |
| `test_ehr_I3_no_wallclock` | The grep test in §11.1; plus `authoredOn` on every write equals the task's `now` |
| `test_ehr_I4_prose_earns_nothing` | An agent that writes a perfect plan in its note but places no orders scores ACT = 0 and gets an `output_gap` for every item |
| `test_ehr_I5_noop_and_oracle_baselines` | On the fixture task set: noop mean ≤ 0.15, oracle mean ≥ 0.90 |
| `test_ehr_I6_deterministic_replay` | Same task + seed + actions → byte-identical observations, writes and deterministic checkpoint scores, 3 runs |
| `test_ehr_I7_phi_canary` | Planted identifiers in every input format never reach `ehr_charts.resources_json`, any tool result, or any export |
| `test_ehr_I7_no_source_dates` | No ISO date from the fixture source files appears in any snapshot. All dates are anchor-based |
| `test_ehr_I8_identities_synthetic` | Every name, MRN and birth date in a snapshot comes from the committed lists or generator. A planted source name is never used |
| `test_ehr_I9_wrong_patient_hard_fail` | A single write to a decoy → `hard_fail`, reward 0, reason `wrong_patient` |
| `test_ehr_I10_safety_overrides_key` | When the key itself contains a rule-triggering action and the agent copies it: hard fail and a `safety` review created |
| `test_ehr_I11_heldout_never_packaged` | `package.build(split='heldout')` raises. A train package contains no heldout task ids |
| `test_ehr_I12_reviewer_independence` | An excluded source clinician is never selected. The same reviewer is never on one review twice. Reviewer 2's payload has no field from reviewer 1's verdict. The review routes return 403 to a non-assigned physician even with the open-pool flag on or a mock user |
| `test_ehr_I13_key_corrections_append_only` | `doctor_wrong` writes a correction row. The original `key_enc` is unchanged. The effective key = original + corrections |
| `test_ehr_I14_one_earning_per_review` | Double submit → one earnings row (`UNIQUE(kind, ref_id)`); kind `ehr_review`, amount 2500 |
| `test_ehr_I15_jurisdiction_block` | A US-sourced package to `CN` is refused. To `GB` with both other attestations true it is allowed. A missing jurisdiction, `buyer_not_covered_person = false`, or a missing onward-transfer clause for a non-US buyer is refused |
| `test_ehr_I16_no_deletes` | `data_change_guard.py` passes; new tables are covered by the inventory |

### 13.3 End-to-end (fake LLM, sandbox realm)

`tests/test_ehr_sandbox_e2e.py`:

1. Upload the fixture zip.
2. Process, build charts, compile.
3. Run `scripted:noop`, `scripted:oracle` and `scripted:planted`.
4. Grade.
5. Reviews created.
6. Fake physicians submit verdicts through the HTTP routes.
7. Final rewards recomputed.
8. Report rendered.
9. Package built for `dev` and `GB`.

**Asserts:**

- the counts at every stage;
- one earning per verdict;
- the cache prevents a duplicate review;
- no key or PHI in any response body captured during the run.

### 13.4 Existing suites that must stay green

`test_asclepius_environments.py`, `test_asclepius_env_isolation.py`, `test_asclepius_ingestion.py` and every `test_asclepius_longitudinal*.py`, plus the full suite, the route baseline (only additions) and `check_dangling_imports.py`.


---

## 14. Do not touch, and non-goals

### 14.1 Do not touch

**The complete list of existing files this PRD edits.** Anything not listed here is import-only:

| File | Edit | Section |
|---|---|---|
| `asclepius/ingestion.py` | `_classify` (2 formats); the `.xml` head size at the call site; `_merge_fragments` (4 collections + `note_id`); `_KEY_SOURCE_PRECEDENCE` | §8.1, §6.1 |
| `asclepius/case_formats.py` | `FORMATS` registration | §8.1 |
| `asclepius/adapters/__init__.py` | import the 2 adapters | §8.1 |
| `asclepius/timeline.py` | `normalize_timeline` conversion blocks; `_STRUCTURED_DATE_KEYS`; `_FREE_TEXT_FIELDS` | §6.1 |
| `asclepius/cases.py` | new models and optional fields | §6.1 |
| `asclepius/store.py` | new `CREATE TABLE` blocks and their accessor methods only | §6.2 |
| `asclepius/payments.py` | `KIND_EHR_REVIEW` and its `_KIND_LABELS` entry | §10.5 |
| `ai/llm_client.py` | additive OpenAI tools branch | §11.4 |
| `backend/main.py` | mount the router; one call in `_assignment_maintenance_loop` | §11.6 |
| `scripts/data_inventory.py`, `scripts/data_change_guard.py` | cover the `ehr_*` tables | §6.2 |
| `backend/requirements.txt`, `.env.example` | new packages and variables | §7, Appendix C |

**Do not change:**

- **The existing environment track.** `ClinicalEnv`, `EHRState`, `_TOOL_TABLE`, `verify.score`, `compile_environment`, `rollout.py` and the `env_runs` table stay byte-identical. `EhrVisitEnv` re-implements the interface; the harness imports `_extract_json`.
- **Existing ingestion behaviour** for FHIR, HL7v2, CSV, text and DICOM, including the blocking `incomplete_upload` and `ambiguous_patient_identity` reviews.
- **The single-turn portal, longitudinal walks and their gates:** `routers/asclepius.py` (no edits), `real_cases.py` (import only), `routing.py`, `agreement.py`, `case_access.py`.
- **Existing tables.** No column changes to `tasks`, `submissions`, `records`, `earnings`, `assignments`, `exports`, `ingest_cases`, `sealed_ground_truth` or `env_runs`. Reviews use their own tables, not `assignments`.
- **`asclepius/auth.py`, `credentials.py`, `payout.py`, `allocation.py`, `physician_agreement.py` and `routers/asclepius_admin.py`:** import only, or a thin commented copy (§10.3).
- **The community plane.** No posts. Notifications go through `notify_person` only.

### 14.2 Non-goals for v1

1. A clickable EHR UI or computer-use agents (M7, later).
2. Raw PHI ingestion or de-identification by us (D7). The pipeline verifies; it does not de-identify.
3. Inpatient or ED workflows, and multi-specialty charts. v1 is outpatient nephrology. The design is specialty-agnostic, but rubrics, safety rules and probes are nephrology-only.
4. Imaging pixel data as agent context. Cardiology DICOM from the other practice is a separate track.
5. Live multi-turn patient simulation (τ-bench-style user simulator). The agent works the chart, not a talking patient.
6. Training models ourselves. We run evals and ship environments; post-training is a separate project.
7. Automated buyer billing, self-serve lab signup, or hosting labs' rollouts at scale. v1 hosting is for heldout evals we run ourselves.
8. SNOMED or CPT descriptors in any export (§7.4).
9. Using the answer key or reviews to grade the **treating doctor** as a person. Reviews judge decisions, and no report ranks or names source clinicians.

---

## 15. Open questions for Tej (defaults chosen so the build is not blocked)

| # | Question | Default in this PRD |
|---|---|---|
| Q1 | **Legal prerequisites, not code items.** (a) Omics de-identifies on the practice's behalf, so there must be a BAA covering that step (practice → Omics directly, or practice → Archangel → Omics as subcontractor). (b) Archangel needs a data licence or data-use agreement from the practice that permits building and selling environments from the de-identified output. Receiving Safe-Harbor de-identified data does not itself need a BAA. Are (a) and (b) signed? | The build proceeds on synthetic fixtures. Real upload waits for a yes on both. |
| Q2 | Can you get the Office Ally **EHI CCD batch export** as well as PDFs? And can Omics ship a `manifest.json` mapping every file to a patient label (or prefix filenames `<patient>__`)? | The pipeline handles PDFs alone, but without a per-file patient mapping every PDF is held for review (§8.1). C-CDA makes keys far more reliable. |
| Q3 | Who writes the nephrology visit-note rubric? | Two network nephrologists, paid via admin entry. A placeholder rubric is used until then. |
| Q4 | Which clinicians must be on the exclusion list (source practice, family members)? | Every clinician at the source practice, entered by admin at upload. |
| Q5 | Train / dev / heldout split. | 70 / 15 / 15 by patient. |
| Q6 | Which models run in the first internal eval? | Three frontier models × k=3 on dev, configured by env var. |
| Q7 | Daily review cap. | 150 per day. |
| Q8 | Is the synthetic anchor date (Monday 3 March 2031) fine for labs? | Yes. It prevents any real calendar date appearing. |
| Q9 | Blocked-jurisdiction list for US-sourced data (I15). | CN, HK, MO, RU, IR, KP, CU, VE. Counsel to confirm. |
| Q10 | Pricing of the environment for labs. | Not in code. §3.3 comparables and Epoch's $200–$2,000 per task range inform ops. |

---

## Appendix A: Tool schemas (Anthropic `input_schema` shape; map `input_schema` → `parameters` for OpenAI)

```json
[
 {"name":"search_patients","description":"Search patients by name, birth date, MRN or gender. Returns a FHIR Bundle of Patient.",
  "input_schema":{"type":"object","properties":{
    "name":{"type":"string","description":"any part of the name, case-insensitive"},
    "birthdate":{"type":"string","description":"YYYY-MM-DD, optionally prefixed ge/le/eq"},
    "identifier":{"type":"string","description":"MRN, exact"},
    "gender":{"type":"string","enum":["female","male","other","unknown"]}},"required":[]}},
 {"name":"get_patient","description":"Read one Patient by id.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"}},"required":["patient_id"]}},
 {"name":"list_encounters","description":"List encounters for a patient, newest first.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "date_from":{"type":"string"},"date_to":{"type":"string"}},"required":["patient_id"]}},
 {"name":"search_observations","description":"Search labs or vitals. Use a LOINC code (system|code or bare) or a group name (e.g. 'BMP','renal','UACR','CBC','iron','PTH'). Paginated.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "code":{"type":"string"},"group":{"type":"string"},
    "category":{"type":"string","enum":["laboratory","vital-signs"]},
    "date_from":{"type":"string"},"date_to":{"type":"string"},
    "sort":{"type":"string","enum":["date","-date"],"default":"-date"},
    "count":{"type":"integer","minimum":1,"maximum":200,"default":50},
    "page_token":{"type":"string"}},"required":["patient_id"]}},
 {"name":"search_conditions","description":"Problem list and recorded assessments.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "clinical_status":{"type":"string","enum":["active","inactive","resolved"]}},"required":["patient_id"]}},
 {"name":"search_medications","description":"MedicationRequests (current and past regimens).",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "status":{"type":"string","enum":["active","on-hold","stopped","completed","cancelled","all"],"default":"active"},
    "date_from":{"type":"string"},"date_to":{"type":"string"}},"required":["patient_id"]}},
 {"name":"search_allergies","description":"AllergyIntolerance for a patient.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"}},"required":["patient_id"]}},
 {"name":"search_documents","description":"List clinical documents (visit worksheets, lab reports, letters). Returns metadata only; use read_document for text.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "type":{"type":"string"},"date_from":{"type":"string"},"date_to":{"type":"string"},
    "count":{"type":"integer","default":20}},"required":["patient_id"]}},
 {"name":"read_document","description":"Return the text of one document.",
  "input_schema":{"type":"object","properties":{"document_id":{"type":"string"}},"required":["document_id"]}},
 {"name":"search_orders","description":"Past and pending orders (labs, imaging, referrals) and appointments.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "kind":{"type":"string","enum":["lab","imaging","referral","appointment","all"],"default":"all"}},"required":["patient_id"]}},
 {"name":"calculate","description":"Deterministic clinical calculators.",
  "input_schema":{"type":"object","properties":{
    "formula":{"type":"string","enum":["egfr_ckd_epi_2021_cr","egfr_ckd_epi_2021_cr_cys","crcl_cockcroft_gault","kfre_4var_2yr","kfre_4var_5yr","uacr_category","ckd_stage","bmi","unit_convert"]},
    "inputs":{"type":"object","description":"formula-specific; e.g. {creatinine_mg_dl, age, sex} or {value, from_unit, to_unit, analyte}"}},
    "required":["formula","inputs"]}},
 {"name":"create_medication_order","description":"Start a new medication. Creates an active MedicationRequest.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "drug":{"type":"string"},"dose_value":{"type":"number"},"dose_unit":{"type":"string"},
    "route":{"type":"string"},"frequency":{"type":"string","description":"e.g. 'once daily', 'BID'"},
    "duration_days":{"type":"integer"},"reason":{"type":"string"}},
    "required":["patient_id","drug","dose_value","dose_unit","route","frequency"]}},
 {"name":"change_medication_order","description":"Change dose or frequency of an active medication. Stops the old order and creates a new one linked to it.",
  "input_schema":{"type":"object","properties":{"medication_request_id":{"type":"string"},
    "dose_value":{"type":"number"},"dose_unit":{"type":"string"},"frequency":{"type":"string"},"reason":{"type":"string"}},
    "required":["medication_request_id","reason"]}},
 {"name":"discontinue_medication","description":"Stop an active medication.",
  "input_schema":{"type":"object","properties":{"medication_request_id":{"type":"string"},"reason":{"type":"string"}},
    "required":["medication_request_id","reason"]}},
 {"name":"hold_medication","description":"Put an active medication on hold.",
  "input_schema":{"type":"object","properties":{"medication_request_id":{"type":"string"},"reason":{"type":"string"},
    "resume_condition":{"type":"string"}},"required":["medication_request_id","reason"]}},
 {"name":"create_lab_order","description":"Order a lab test or panel.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "code":{"type":"string","description":"LOINC code or group name"},"timing_days":{"type":"integer","description":"days from today, 0 = today"},
    "reason":{"type":"string"}},"required":["patient_id","code"]}},
 {"name":"create_referral","description":"Refer to another specialty.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "specialty":{"type":"string","description":"controlled list, e.g. vascular_surgery, transplant_nephrology, cardiology, urology, dietitian"},
    "urgency":{"type":"string","enum":["routine","urgent"]},"reason":{"type":"string"}},
    "required":["patient_id","specialty","reason"]}},
 {"name":"create_imaging_order","description":"Order imaging.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "study":{"type":"string","description":"e.g. 'renal ultrasound'"},"contrast":{"type":"boolean","default":false},
    "reason":{"type":"string"}},"required":["patient_id","study","reason"]}},
 {"name":"schedule_follow_up","description":"Schedule the next visit.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "interval_days":{"type":"integer","minimum":1},"visit_type":{"type":"string","enum":["office","telehealth"]}},
    "required":["patient_id","interval_days"]}},
 {"name":"record_assessment","description":"Record a diagnosis or assessment for today.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "icd10":{"type":"string"},"text":{"type":"string"},
    "clinical_status":{"type":"string","enum":["active","resolved"],"default":"active"},
    "ckd_stage":{"type":"string","enum":["G1","G2","G3a","G3b","G4","G5"]}},"required":["patient_id","text"]}},
 {"name":"flag_urgent","description":"Escalate: the patient needs urgent action beyond a routine order.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "action":{"type":"string","enum":["ed_now","same_day_contact","admit_recommended","urgent_referral"]},
    "reason":{"type":"string"}},"required":["patient_id","action","reason"]}},
 {"name":"write_visit_note","description":"Write today's visit note. Orders are NOT placed by text; use the order tools.",
  "input_schema":{"type":"object","properties":{"patient_id":{"type":"string"},
    "assessment":{"type":"string"},"plan":{"type":"string"}},"required":["patient_id","assessment","plan"]}},
 {"name":"finish_visit","description":"End the visit. Call once, last.",
  "input_schema":{"type":"object","properties":{"summary":{"type":"string"}},"required":[]}},
 {"name":"submit_answer","description":"Probe tasks only: submit the answer.",
  "input_schema":{"type":"object","properties":{"answer":{"type":"object"}},"required":["answer"]}}
]
```

## Appendix B: One task row as exported (train/dev only; no key)

```json
{"task_id":"ehrt-9c1e…","env_version":"neph-ehr-1.1.0","task_kind":"visit","split":"dev","seed":417,
 "instruction":"You are the nephrologist at an outpatient clinic. Today is Monday 3 March 2031. You are seeing Dana Whitaker (MRN 40318822, DOB 1957-06-14) for a scheduled follow-up visit. Use the EHR tools to review the chart, make today's clinical decisions, place any orders, and write today's visit note. Only act on this patient. Call finish_visit when you are done.",
 "now":"2031-03-03T09:00:00-08:00","budget_tool_calls":40,
 "tags":["F1","F2","F3","action"],
 "snapshot":{"resourceType":"Bundle","type":"collection","entry":["… target + 3 decoys, synthetic dates …"]},
 "canary":"NEPH-EHR-CANARY-7b0f…"}
```

## Appendix C: New environment variables (add to `.env.example`)

| Var | Default | Meaning |
|---|---|---|
| `EHR_ENV_VERSION` | `neph-ehr-1.1.0` | Stamped on tasks and packages |
| `EHR_KEY_MIN_CONFIDENCE` | `0.80` | Key audit threshold (§8.4) |
| `EHR_BUDGET_TOOL_CALLS` | `40` | Default per-task budget |
| `EHR_CHECKPOINT_WEIGHTS` | `0.10,0.20,0.50,0.20` | RETRIEVE, REASON, ACT, DOCUMENT |
| `EHR_RUBRIC_SAMPLE_RATE` | `0.10` | Rubric QA sample (D5) |
| `EHR_REVIEW_PAY_CENTS` | `2500` | D10 |
| `EHR_REVIEW_DAILY_CAP` | `150` | §10.1 |
| `EHR_REVIEW_MAX_OPEN` | `8` | Per-reviewer open cap |
| `EHR_REVIEW_DUE_HOURS` | `72` | Assignment due |
| `EHR_HARNESS_CONCURRENCY` | `4` | Parallel rollouts |
| `EHR_EXPORT_BLOCKED_JURISDICTIONS` | `CN,HK,MO,RU,IR,KP,CU,VE` | I15 |
| `EHR_OCR_ENABLED` | `auto` | `auto` uses ocrmypdf if installed |

## Appendix D: Kickoff prompt to paste into Claude Code

> Read `AGENTS.md`, then `docs/prd/PRD_EHR_SANDBOX_RL_ENV.md` in full, then every file cited in §4. Build **M0 only**.
>
> - Create the `ehr_sandbox` package skeleton, the §6.2 DDL, the §6.1 `ClinicalCase` additions, and the synthetic fixture zip described in M0. Install nothing that M0 does not need. Synthea is run once to produce fixtures; commit the JSON/XML/PDF outputs, not Synthea.
> - Write the M0 tests and the I16 test.
> - Run the M0 exit checks. Stop and report: files created, tests added with pass counts, the route-baseline diff (should be empty for M0), and any `# EHR-QUESTION:` comments.
> - Do not start M1 until I confirm.
> - Never weaken an invariant in §5 to make a test pass. If one seems impossible, stop and ask.



## Implementation record — 26 September 2026

The M0–M6 software implementation and acceptance evidence are recorded in
[EHR_IMPLEMENTATION_STATUS.md](EHR_IMPLEMENTATION_STATUS.md). That document
also records explicit limits: handwritten synthetic fixtures, clinical rubric
and titration-table authorship pending two nephrologists, context-dependent
renal-dose abstentions, a deterministic portable documentation grader, optional
local OCR, and production/real-data acceptance still to be performed.

Two review clarifications were necessary in the implementation. Per-visit
outcome review rates one anonymous proposed reference plan before outcomes are
revealed; per-rollout disagreement review compares two anonymous plans. P4's
specified 10% reference audit has its own `dose_sample` trigger, so a disputed
probe reference can be withdrawn without rewriting the visit key. Final report
metrics exclude pending and withdrawn references, and rubric agreement is
stratified by version, judge provider/model, and criterion.
