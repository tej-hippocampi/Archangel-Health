# PRD — Asclepius Auto-Generation Engine (Nephrology Seed Corpus + Synthetic Prompt/Response Generator)

**Feature codename:** Asclepius Seedmaker (nephrology v1)
**Parent product:** Asclepius — Expert Evaluation Portal (Product #3)
**Status:** v0.1 — adds the auto-generation half of the "two-source" task model
**Code home:** extends `backend/asclepius/` (no new top-level area)
**Last updated:** June 2026

---

## 0. Context — where this fits in what's already built

Asclepius already supports **two ways** a task (prompt + two candidate AI answers) can enter the system:

| Source | Status today | Endpoint(s) |
| --- | --- | --- |
| **Mode B — lab-supplied** (a buyer uploads their own prompts and/or AI responses to be graded) | ✅ **Already built and solid** | `POST /api/asclepius/tasks`, `POST /api/asclepius/tasks/upload-file`, `buyer_requests` + `POST /api/asclepius/buyer-requests/{id}/batch` |
| **Mode A — internal auto-generation** (no lab content → the system generates prompts + AI responses) | ⚠️ **Stub only** — `generate_candidates()` makes 2 answers for a *single hand-typed prompt*; `INTERNAL_PROMPT_BANK` has just **6** hardcoded nephrology prompts | `POST /api/asclepius/tasks/generate` |

**This PRD builds out Mode A** into a real, scalable, nephrology-specific generation engine so the portal can produce its own sellable seed dataset when a lab hasn't supplied content — the "seed-then-expand" GTM from the data-optimization prompt (§2.5). It deliberately reuses the existing primitives rather than adding a parallel system:

- `ai.llm_client.call_llm` (BAA-covered, audit-logged) + `ai/model_config.py` model roles
- `asclepius.critic.generate_candidates()` and the `asclepius_candidate_gen` prompt (extended, not replaced)
- `asclepius.store.insert_task(...)` (tasks land in the same queue → same packaging/validation/QA/export pipeline)
- `asclepius.validation.contamination_hits()` + `compute_dedupe_hash()` (reused for novelty/contamination gating)
- task provenance fields already present: `source = "internal_prompt_bank"`, `buyer_request_id`

**Scope guard:** nephrology **only** for v1. The design is a per-specialty registry so a future release can let a doctor pick a specialty and reuse this exact backend (see §8, §15).

---

## 1. Summary

Asclepius Seedmaker is an automated pipeline that manufactures **high-value nephrology training tasks** with no human prompt authoring:

1. Ship a **curated reference corpus of 100 elite nephrology prompts** — clinical scenarios chosen specifically because a current top-tier LLM is *likely to answer confidently but imperfectly*, so that a nephrologist's revision/critique of the AI answer becomes premium training signal.
2. Use those 100 as **few-shot exemplars + a coverage taxonomy** to have the current Claude model **synthesize new, novel nephrology prompts** across the same difficulty profile, at volume.
3. For each generated prompt, **auto-generate two candidate AI answers** from the current Claude model (reusing/extending `generate_candidates`).
4. **Quality-gate** every generated item (novelty, contamination, on-specialty, error-likelihood / "revision value" score) before it becomes a task.
5. Land accepted items as ordinary tasks (`source=internal_prompt_bank`) in the existing evaluator queue, so the rest of the proven pipeline (blinded A/B → revise → package → validate → QA → export) is unchanged.

**The economic thesis:** the product's value is the **delta** between a confident AI answer and a credentialed nephrologist's corrected answer. So the generator is explicitly optimized to produce prompts and answers that *maximize that delta* — not to produce easy prompts the AI already nails (those yield near-zero revision value).

---

## 2. Goals & Non-Goals

### Goals (v1)
- A curated, versioned **100-prompt nephrology seed corpus**, each item tagged (topic, subtopic, difficulty, *why AI is likely to err*, source-type), synthetic / no-PHI, contamination-safe.
- An **admin one-click generator**: "Generate N nephrology tasks" → N validated tasks (prompt + 2 candidate answers) in the queue.
- Generation is **grounded in the seed corpus** (few-shot + taxonomy coverage), not free-floating.
- Every generated task is **novel** (deduped vs. seeds + prior generations) and **contamination-checked** (not lifted from public benchmarks).
- An **error-likelihood / revision-value gate** so low-value (AI-already-correct) prompts are dropped.
- Full **provenance**: each generated task records its seed exemplars, generation model + config version, and `source=internal_prompt_bank`.
- **Nephrology-scoped**, behind a specialty registry that makes future specialties a config addition.
- Wire it into the existing **buyer-request "spec-only" path** so "no lab prompts → auto-generate to spec" works end to end.

### Non-Goals (v1)
- No specialties other than nephrology (registry is built, but only nephrology is registered).
- No doctor self-serve specialty picker yet (future; §15).
- No autonomous publishing — generated tasks still flow through the **human evaluator + QA gate** before export. The generator never produces "ground-truth" answers; it produces *candidates to be judged*.
- No live web retrieval/RAG at generation time in v1 (the seed corpus is the curated knowledge anchor; live retrieval is a future enhancement, §15).
- No change to Mode B (lab-supplied) — it's already built; this is additive.

---

## 3. The value thesis — why "hard, current, nuanced" prompts (research-backed)

Recent (2025–2026) evidence shapes the curation bar:

- LLMs achieve **superhuman multiple-choice scores yet show fragile real-world clinical reasoning, systematic overconfidence, and sensitivity to prompt wording** — i.e., they are *confidently wrong* on realistic open-ended cases. (Medical-errors-in-LLMs synthetic-transcript study; "Beyond MedQA".)
- Nephrology is **dosing- and protocol-heavy** (eGFR-indexed drug dosing, dialysis prescriptions, ESA/IV-iron rules, electrolyte-correction *rates*) where general LLMs "hallucinate and deviate from evidence-based protocols." (LLMs-in-nephrology systematic review; AnemiaCare HD.)
- **Guidelines moved recently** (KDIGO 2024 CKD; KDIGO 2025 IgAN/IgAV; SGLT2i / finerenone / GLP-1 in CKD; hyperkalemia insulin-dextrose dosing debates). Models with older training cutoffs lag current standard-of-care — a reliable source of *confidently outdated* answers.

**Implication for the generator:** target the zones above. A good Seedmaker prompt is one where (a) a current LLM will produce a fluent, plausible answer, (b) that answer has a realistic chance of a clinically meaningful error/omission, and (c) a nephrologist's correction is specific and teachable. Easy recall questions are explicitly *out* — they produce low-delta, low-value data.

---

## 4. Users & trigger points

1. **Admin / operator** — primary user. Triggers generation from the Asclepius admin UI ("Generate nephrology tasks", choose count, difficulty mix, `capture_reasoning`, `grounding_mode`).
2. **Buyer-request "spec-only" path** — when a `buyer_request` has constraints but **no uploaded prompts**, `POST /buyer-requests/{id}/batch` invokes this generator (it currently calls `generate_candidates` over the 6-prompt bank; it will call the new engine instead).
3. **Evaluators** — unchanged; generated tasks appear in their queue identically to lab-supplied ones (blinded; `generator_model` never shown).

---

## 5. The nephrology seed corpus (the 100 reference prompts)

### 5.1 What it is
A curated, versioned set of **100 elite nephrology prompts** that anchor all generation. Stored as data, not code, so it can be reviewed and bumped:

`backend/asclepius/seed_corpus/nephrology.v1.json`

### 5.2 Per-prompt schema
```json
{
  "seed_id": "neph-seed-0007",
  "specialty": "nephrology",
  "topic": "dialysis_prescription",
  "subtopic": "hyperkalemia_dialysate_K",
  "difficulty": "hard",
  "prompt": "72yo on thrice-weekly hemodialysis presents pre-session with K+ 6.4 and peaked T-waves... How do you adjust the dialysate potassium and bridge medically before the run?",
  "ai_failure_mode": "over-aggressive dialysate K+ (e.g. 1.0 mEq/L) causing arrhythmia risk; wrong sequencing of membrane stabilization vs shift vs removal",
  "why_high_value": "requires nuanced sequencing + safety tradeoff; current models often pick an unsafe rapid-correction path",
  "reference_basis": "KDIGO/▢ hyperkalemia-in-HD practice points (paraphrased, not quoted)",
  "reference_type": "guideline",
  "capture_reasoning_recommended": true,
  "tags": ["dosing", "safety", "electrolytes", "current_guideline"]
}
```
> No verbatim text from any copyrighted benchmark/exam. Prompts are **original synthetic vignettes** written to exercise a topic; `reference_basis` cites the *concept* source (guideline/review), not copied question text. No PHI.

### 5.3 Coverage taxonomy (the 100 are distributed across these — buckets, not exhaustive)
1. **Renal drug dosing & contraindications by eGFR** (metformin threshold, gabapentinoids, DOACs, SGLT2i initiation thresholds, contrast, NSAIDs, antibiotics renal adjustment).
2. **Dialysis prescription & adequacy** (dialysate K⁺/Ca²⁺/bicarb, Kt/V, ultrafiltration rate, anemia ESA/IV-iron dosing, mineral-bone disease).
3. **Electrolyte & acid-base correction *rates* and safety** (hyponatremia & osmotic demyelination, hyperkalemia treatment selection/dosing, hypercalcemia, mixed acid-base).
4. **Recently-updated standard-of-care** (KDIGO 2024 CKD; KDIGO 2025 IgAN/IgAV; finerenone, SGLT2i, GLP-1 RA in CKD/diabetic kidney disease) — *AI-cutoff-lag* zone.
5. **Transplant nephrology** (tacrolimus dosing & interactions, rejection workup, BK/CMV, immunosuppression in infection/pregnancy).
6. **Glomerular & autoimmune disease** (lupus nephritis, ANCA vasculitis, IgAN treatment thresholds, nephrotic management).
7. **AKI & critical care nephrology** (CRRT vs IHD, contrast-associated AKI, hepatorenal, rhabdomyolysis).
8. **Special populations & tradeoff-heavy judgment calls** (pregnancy + CKD, elderly/frailty dialysis-vs-conservative, pediatric dosing, goals-of-care, conflicting-guideline situations).

Each bucket carries a target count + minimum difficulty so the corpus (and downstream generation) can't collapse onto easy recall.

### 5.4 How the 100 are produced (curation methodology)
A **one-time, reviewed build step** (script `scripts/build_nephrology_seed_corpus.py`, output committed as the JSON above):
1. **Source distillation** — from current nephrology **clinical-practice guidelines and high-quality review papers / top-lab eval discussions** (KDIGO 2024/2025, KDOQI commentary, major reviews on LLMs-in-nephrology failure modes). Extract *topics and known AI failure modes*, never copyrighted question text.
2. **Draft synthesis** — the strongest current Claude model drafts original vignettes per taxonomy bucket, each annotated with `ai_failure_mode` + `why_high_value`.
3. **Hardness filter** — run each draft through the **error-likelihood scorer** (§7.3); keep only those predicted to elicit a flawed AI answer.
4. **Contamination + dedupe** — `contamination_hits()` against public benchmarks + intra-corpus dedupe.
5. **Human ratification** — the nephrologist (anchor practice) reviews/edits/approves the final 100. *The committed corpus is the human-approved artifact*; the script is reproducible scaffolding, not an autopilot.

> The 100-prompt corpus is **versioned** (`nephrology.v1`); bumping it (v2, …) is a reviewed PR. The version is stamped on every generated task's provenance.

---

## 6. Two-source task model (where this plugs in)

```
                         ┌─────────────── lab supplies prompts/responses? ───────────────┐
                         │ YES (Mode B, already built)            NO (Mode A, this PRD)   │
                         ▼                                          ▼
        tasks / upload-file / buyer-request          ┌───────────────────────────────────┐
        (grade exactly what they sent)               │  Asclepius Seedmaker (nephrology)  │
                         │                            │  seed corpus → prompt-gen →        │
                         │                            │  candidate-gen → quality gate      │
                         ▼                            └───────────────────────────────────┘
                  store.insert_task(...)   ◄──────────────────────────┘  (source=internal_prompt_bank)
                         │
                         ▼
        existing queue → blinded A/B eval → package → validate → QA gate → export   (UNCHANGED)
```

---

## 7. The generation pipeline (new: `backend/asclepius/generation.py`)

A single orchestrator, `generate_nephrology_tasks(n, *, difficulty_mix, capture_reasoning, grounding_mode, buyer_request_id=None)`, returns created task ids. Internally four stages:

### 7.1 Prompt generation — `asclepius_prompt_gen` (new model role + prompt)
- New role in `ai/model_config.py`: `"asclepius_prompt_gen"` (default to the **strongest current Claude model**, e.g. `claude-opus-4-8`; env override `MODEL_ASCLEPIUS_PROMPT_GEN`). Quality matters most here.
- New system prompt `ASCLEPIUS_PROMPT_GEN_SYSTEM` in `asclepius/prompts.py`, registered in `backend/prompts/registry.py`.
- Few-shot: sample **K seed prompts** (default 6–8) from the target taxonomy bucket(s), plus the bucket's `ai_failure_mode` hints, and instruct the model to produce **new, distinct** nephrology vignettes in the same hard/nuanced/current profile — explicitly *not* paraphrases of the seeds.
- Round-robin / weighted across taxonomy buckets so a batch covers the spectrum (no collapse onto one topic).
- Output schema (strict JSON): `[{prompt, topic, subtopic, difficulty, ai_failure_mode, capture_reasoning_recommended}]`.

### 7.2 Candidate-answer generation — extend existing `asclepius_candidate_gen`
- Reuse `generate_candidates(prompt, specialty="nephrology")`. **Enhancement:** make the two answers intentionally span a quality gap so the comparison and revision are informative — one **strong** answer and one **plausibly-flawed** answer keyed to the prompt's `ai_failure_mode` (e.g., the unsafe dosing path). This is a prompt change to `ASCLEPIUS_CANDIDATE_GEN_SYSTEM`, optionally accepting the `ai_failure_mode` hint.
- Keep candidate generation on the **current production model** (e.g. `claude-sonnet-4-6`) on purpose — the thesis is "*current AI might make some errors*," and using a non-maximal model makes realistic, revisable errors more likely. Model is env-swappable (`MODEL_ASCLEPIUS_CANDIDATE_GEN`) to tune the error rate.
- `generator_model` is stored server-side, **never** surfaced to the blinded evaluator.

### 7.3 Quality / error-likelihood gate — `asclepius_prompt_judge` (new role + prompt)
A judge scores each (prompt, candidate answers) before it becomes a task:
- **`error_likelihood`** (0–1): will a current model plausibly err on this? (low → drop)
- **`revision_value`** (0–1): would a nephrologist's correction be specific & teachable? (low → drop)
- **`on_specialty`** (bool): truly nephrology?
- **`safety_ok`** (bool): not a harmful/disallowed request; synthetic; no PHI.
- Thresholds via env (`ASCLEPIUS_GEN_MIN_ERROR_LIKELIHOOD`, `ASCLEPIUS_GEN_MIN_REVISION_VALUE`, defaults 0.5). Below threshold → discarded (logged, counted).
- Runs through `call_llm` (audited). Degrades gracefully: if no LLM key, generation is **disabled** with a clear error (we will not emit ungated synthetic tasks).

### 7.4 Novelty, contamination, dedupe, scope gates (reuse existing)
- `validation.contamination_hits(prompt)` → drop if it matches a public-benchmark fingerprint.
- `validation.compute_dedupe_hash(...)` vs. the seed corpus **and** previously-generated tasks → drop near-duplicates.
- **On-specialty guard**: hard `specialty="nephrology"` on every created task; judge `on_specialty=false` → drop.
- Accepted items → `store.insert_task(..., source="internal_prompt_bank", buyer_request_id=...)` with generation provenance (below). Loop until `n` accepted or a max-attempts ceiling (log shortfall — never silently under-deliver).

---

## 8. Specialty scoping & the future-proof registry

v1 is nephrology-only, but built as a registry so future specialties are pure config:

`backend/asclepius/specialties.py`
```python
SPECIALTY_REGISTRY = {
  "nephrology": SpecialtyConfig(
    seed_corpus="seed_corpus/nephrology.v1.json",
    taxonomy=NEPHROLOGY_TAXONOMY,         # buckets + target counts + min difficulty
    enabled=True,
  ),
  # future: "cardiology": SpecialtyConfig(..., enabled=False), ...
}
```
- The generator looks up the config by specialty; **only `nephrology` is `enabled=True` in v1.** A request for any other specialty returns `400 specialty_not_enabled`.
- This is the seam the future doctor-self-serve feature (§15) plugs into: add a seed corpus + taxonomy, flip `enabled`, done — no pipeline changes.

---

## 9. Data model & provenance

### 9.1 Generation provenance stamped on every generated task
Extend the task record (and carry into packaged-record provenance) with a `generation` block:
```json
"generation": {
  "engine": "asclepius_seedmaker",
  "specialty": "nephrology",
  "seed_corpus_version": "nephrology.v1",
  "seed_exemplars": ["neph-seed-0007", "neph-seed-0042"],
  "taxonomy_bucket": "dialysis_prescription",
  "prompt_gen_model": "claude-opus-4-8",
  "candidate_gen_model": "claude-sonnet-4-6",
  "judge": {"error_likelihood": 0.78, "revision_value": 0.71},
  "config_version": "<APP_AI_CONFIG_VERSION>",
  "generated_at": "2026-06-26T...Z"
}
```
- This makes buyer-facing provenance honest: a record's prompt was **synthetically generated** (not lab-supplied), traceable to the corpus version + models used. Surface it in the datasheet (a "synthetic prompt provenance" note) so buyers know exactly what they're getting.
- New lightweight table `generation_jobs` (id, specialty, requested_n, accepted, dropped_by_reason, params_json, created_by, created_at) for the admin dashboard + auditing.

### 9.2 Storage
- Seed corpus: committed JSON (read-only at runtime; loaded + cached).
- `generation_jobs`: new table in `asclepius.db` (same `store.py` pattern).
- Generated tasks: existing `tasks` table (no schema change beyond the `generation` block living in the task payload/JSON; add a `generation_json` column or fold into existing JSON per current conventions).

---

## 10. API (new endpoints, admin-gated, `/api/asclepius`)

| Method & path | Purpose |
| --- | --- |
| `POST /generation/nephrology` | Body `{count, difficulty_mix?, capture_reasoning?, grounding_mode?, buyer_request_id?}` → runs the engine, returns `{job_id, created:[task_id...], accepted, dropped:{reason:count}}`. |
| `GET /generation/jobs` | List generation jobs (dashboard + audit). |
| `GET /generation/seed-corpus` | Return corpus metadata (version, counts by bucket) — not for evaluators; admin visibility. |
| `GET /specialties` | List specialties + `enabled` flag (drives future UI; v1 returns nephrology enabled, others disabled). |

- `POST /buyer-requests/{id}/batch` (existing) is updated: when `source` resolves to internal/no uploads, it calls `POST /generation/nephrology` internally instead of the 6-prompt loop, stamping `buyer_request_id`.
- All gated by `require_admin`. All generation calls audited via `call_llm`.

---

## 11. Config / env / model roles

Add to `ai/model_config.py` `MODEL_REGISTRY`:
```python
"asclepius_prompt_gen":   {"model": "claude-opus-4-8",   "temperature": 0.7, "max_tokens": 2000},   # diverse, novel prompts
"asclepius_prompt_judge": {"model": "claude-opus-4-8",   "temperature": 0.0, "max_tokens": 800},    # strict scoring
# asclepius_candidate_gen already exists (current model; intentionally not max)
```
Add to `.env.example`:
```
# Asclepius auto-generation (nephrology v1)
# MODEL_ASCLEPIUS_PROMPT_GEN=claude-opus-4-8
# MODEL_ASCLEPIUS_PROMPT_JUDGE=claude-opus-4-8
ASCLEPIUS_GEN_MIN_ERROR_LIKELIHOOD=0.5
ASCLEPIUS_GEN_MIN_REVISION_VALUE=0.5
ASCLEPIUS_GEN_MAX_ATTEMPTS_PER_TASK=4      # generation attempts before giving up on one slot
ASCLEPIUS_GEN_FEWSHOT_K=6                  # seed exemplars sampled per generation call
```
> "Current Claude model" is always expressed via the model registry + env override, never hardcoded in logic — so swapping to the latest model is a one-line config change.

---

## 12. Quality, safety & guardrails
- **Doctor-in-the-loop is non-negotiable.** The engine produces *candidates to be judged*, never ground-truth answers. Generated tasks still pass through the human evaluator + QA gate before `export_ready`.
- **No PHI** — synthetic vignettes only; the existing PHI scan still runs on every submission.
- **Contamination-safe** — corpus is original text; generated prompts are dedup/contamination-checked; nothing is lifted from MedQA/PubMedQA/MMLU-med.
- **No fabricated citations injected into data** — the generator does **not** attach evidence anchors; anchors are added later by the nephrologist (the human is the source of grounding, per the data-optimization prompt). Generation must never auto-stamp `grounded=true`.
- **Hallucination tolerance is the point** for candidate answers (we *want* realistic flaws) — but the judge enforces `safety_ok` so a flawed answer is "suboptimal," not "dangerous-and-presented-as-a-trap that could leak into export without review."
- **Cost control** — generation is batched, attempt-capped, and logged with per-job token usage (via the `call_llm` audit record).

## 13. Build phases
1. **Corpus** — taxonomy + `scripts/build_nephrology_seed_corpus.py`; produce, hardness-filter, dedupe; **nephrologist ratifies**; commit `nephrology.v1.json` (100 items).
2. **Generation core** — `generation.py` orchestrator; `asclepius_prompt_gen` + `asclepius_prompt_judge` roles/prompts (registered); extend `candidate_gen` for the strong/flawed pair; wire novelty/contamination/scope gates.
3. **Persistence & provenance** — `generation_jobs` table; `generation` provenance block on tasks → packaged records → datasheet note.
4. **API + admin UI** — `POST /generation/nephrology`, jobs list, seed-corpus metadata; admin "Generate nephrology tasks" panel; update buyer-request batch path.
5. **Specialty registry** — `specialties.py`; nephrology enabled; others 400.
6. **Tests** — corpus loads & is 100/valid; generator returns N accepted with provenance; gates drop contaminated/dupe/off-specialty/low-value items; specialty guard; no `grounded` auto-stamp; buyer-request spec-only path generates.

## 14. Acceptance criteria
- A committed, versioned **100-prompt nephrology corpus**, each item schema-valid, tagged, synthetic, contamination-clean, nephrologist-approved.
- Admin can request **N** nephrology tasks and receive N validated tasks (prompt + 2 candidates) in the queue, or a logged shortfall with drop reasons.
- Every generated task carries full **generation provenance** (corpus version, seed exemplars, models, judge scores) and `source=internal_prompt_bank`.
- Generated prompts are **novel** (pass dedupe vs. corpus + prior gens) and **contamination-clean**; off-specialty/low-value items are dropped, not shipped.
- The pipeline is **nephrology-only** (other specialties 400) via the registry.
- Generated tasks flow through the **unchanged** eval → QA → export pipeline; nothing reaches `export_ready` without human evaluation + QA.
- "Current Claude model" is configurable via the model registry/env (no hardcoding).

## 15. Future work (explicitly out of scope for v1)
- **Doctor specialty self-serve:** a doctor enters their specialty → the same backend generates prompts + AI responses for that specialty. Mechanically: add `seed_corpus/<specialty>.vN.json` + a taxonomy + flip `enabled=True` in `SPECIALTY_REGISTRY` — no pipeline changes. (This PRD builds the registry seam now precisely so that release is config-only.)
- **Live retrieval-augmented generation** at generation time (pull the latest guideline text to keep "current" truly current between corpus bumps).
- **Auto-refresh** the corpus when a major guideline updates (e.g., a new KDIGO release) — flag affected buckets for re-curation.
- **Difficulty/coverage analytics** feeding back into generation weighting from realized evaluator revision-value.

## 16. Open questions
- Corpus size beyond 100 per specialty, and refresh cadence (tie to guideline release cycle?).
- Should the strong/flawed candidate pairing be explicit (label which is intended-flawed, server-side) to compute an internal "did the doctor catch it" metric — a powerful QA signal — while keeping A/B blinded to the evaluator? (Recommended; low cost.)
- Judge thresholds: start at 0.5/0.5 and tune against realized revision rates with the first buyer.

## 17. Sources (research informing the curation bar)
- Medical errors in LLMs via synthetic clinical transcripts: https://www.medrxiv.org/content/10.64898/2026.03.23.26349082v1
- Beyond MedQA — real-world clinical decision-making with LLMs: https://arxiv.org/pdf/2510.20001
- Clinical applications & limitations of LLMs in nephrology (systematic review): https://pubmed.ncbi.nlm.nih.gov/41018275/
- LLMs in nephrology — applications & challenges in CKD: https://pmc.ncbi.nlm.nih.gov/articles/PMC12418797/
- Deterministic LLM framework for hemodialysis anemia (dosing protocol adherence): https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1728320/full
- Medical hallucinations in foundation models: https://arxiv.org/pdf/2503.05777
- KDIGO 2024 CKD guideline: https://kdigo.org/wp-content/uploads/2024/03/KDIGO-2024-CKD-Guideline.pdf
- KDIGO 2025 IgAN/IgAV guideline: https://www.kidney-international.org/article/S0085-2538(25)00279-0/fulltext

*End of PRD.*
