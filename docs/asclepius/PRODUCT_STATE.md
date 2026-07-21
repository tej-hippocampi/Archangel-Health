# Asclepius — Product State

**Current as of 2026-07-21** (reflects `main` at merge commit `e8c9727`, PR #41).
This is the in-repo reference for what Asclepius is and what ships today. Everything
below is **on `main`** — there is no unmerged delta.

---

## 1 — What Asclepius is

Asclepius is the **expert clinical-evaluation portal** inside Archangel Health.
Board-certified specialists compare two candidate answers to a hard clinical case,
pick the better one, correct the worse one, and capture their reasoning — producing
**preference pairs, ideal answers, reasoning traces, and scoring rubrics** that a
model lab buys as training / eval data.

**Stack:** FastAPI + SQLite (`AsclepiusStore`) + Pydantic v2 backend; a vanilla-JS
single-page app frontend (an `h()` hyperscript helper, a global `state.draft`, no
build step). LLM calls go through one audited multi-provider client
(`ai/llm_client.py`).

### The four portal versions (tiers)

| Version | Name | Data | A/B answer source |
|---|---|---|---|
| **V1** | classic | synthetic text prompts | existing candidates |
| **V2** | assisted-legacy | synthetic, model-assisted pre-labeling | existing candidates |
| **V3** | seamless multimodal | **synthetic** structured cases (labs + EHR notes + meds) | **two-frontier** (OpenAI ↔ Anthropic) |
| **V4** | real de-identified | **real** de-identified patient cases (V4 wall: `case_source='real_deid'` ⇔ v4) | Anthropic-only by default (BAA gate) |

> **Guardrail (invariant):** every V3/V4 feature gates on
> `portal_version ∈ {v3,v4}`, `case_source`, or `multimodal` — **never** on
> `isAssisted()` (which also matches V2). **V1 and V2 are byte-for-byte unchanged**,
> with an explicit regression test (`test_v1_v2_submit_unaffected_by_critical_negative_gate`).

---

## 2 — Backend map (`backend/asclepius/`)

### Core pipeline & data
| File | Responsibility |
|---|---|
| `store.py` | SQLite data layer (`AsclepiusStore`): tasks, submissions, baseline runs, exports, generation jobs, model failures, events. Schema + idempotent migrations. `ab_slot_balance()`, `ab_fallback_rate()`, `open_multimodal_count()`. |
| `schemas.py` | Pydantic request/response models (`SubmissionIn`, `RubricCriterion`, `FailureTag`, `PromptReview`, `IndependentAnswer`, …). |
| `constants.py` | Enumerations + tunable thresholds + env-config helpers (axes, tiers, gate floors, two-frontier + fallback-ladder + rubric-probe config). |
| `pipeline.py` | The submit pipeline: validate → package → critic → grounding → agreement → store; runs the rubric grader probes (V3/V4, gated). |
| `packaging.py` | Turns a submission into standalone training records (preference / ideal_answer / reasoning_trace / rubric). |
| `validation.py` | PHI scan, contamination, dedupe, too-fast checks. |
| `agreement.py` | Inter-rater agreement (Cohen's κ) scoring. |
| `value.py` | Per-record marginal value + Value-per-Time; **quality-scaled rubric marginal** (FIX-5.1). |

### Case generation & multimodal (V3)
| File | Responsibility |
|---|---|
| `generation.py` | **Seedmaker** — auto-generates hard tasks (text or multimodal) from the seed corpus, quality-gated; **case-novelty anti-duplication gate**. |
| `cases.py` | `ClinicalCase` schema, `render_case_prompt`, `assert_multimodal_content`, `public_case` (answer-key stripping). |
| `critic.py` | LLM roles: prompt-gen, candidate-gen, prompt/hardness/case judges, `generate_case`. |
| `corpus.py` | Curated seed corpus + hard-case archetypes. |
| `gold_cases.py` | The **10 ratified gold nephrology multimodal cases** (hand-authored, no-LLM seeds) + `load_gold_cases`. |
| `case_formats.py`, `specialties.py` | Case rendering helpers; specialty registry (nephrology enabled). |

### Real-data ingestion (V4)
| File | Responsibility |
|---|---|
| `ingestion.py`, `ingest_notify.py` | Real EHR ingestion pipeline + partner notifications. |
| `deid_verify.py` | De-identification verification. |
| `adapters/` | `fhir_r4.py`, `hl7v2.py`, `lab_csv.py`, `note_text.py` — source-format adapters. |
| `timeline.py` | Multi-timepoint clinical timeline assembly. |

### Frontier capture, rubric, taxonomy, export
| File | Responsibility |
|---|---|
| `baselines.py` | **Two-frontier A/B**: answers a case COLD with one OpenAI + one Anthropic model to the *identical* prompt (`assemble_ab_pair` + fallback ladder). |
| `rubric.py` | Auto-seeds + normalizes the tiered, HealthBench-shaped rubric; concreteness (`is_specific_text`), grounding, completeness/premium, failure coverage, **core-axis nudge** (FIX-7). |
| `grader_eval.py` | Package-time grader meta-eval: `grader_validity`, `grader_reliability`, `hackability` probes (degrade to `skipped` without an LLM key). |
| `failure_taxonomy.py` | **Model-Failure Taxonomy** (§D): `FAILURE_MODES` vocab, provider attribution, small-N suppression, κ label-agreement, holdout split. |
| `export.py` | Buyer export bundles: JSONL + `grader_prompt.txt` + `score.py` + manifest; **eval-pack SKU** (FIX-5.2); taxonomy artifacts. |
| `citations.py`, `stt.py`, `profiles.py`, `credentials.py`, `auth.py` | Evidence anchors + library; dictation cleanup; buyer profiles; credentials; portal auth. |

### Routers, frontend, config
- `routers/asclepius.py` — main API (auth, tasks, submissions, generation + `topup` + `load-gold`, `grade-real-models`, exports, `/stats` with `ab_slot_balance` + `ab_fallback` health).
- `routers/asclepius_provider.py` — data-provider portal.
- `frontend/asclepius/asclepius.js` + `.css` — the SPA (admin + evaluator + QA), incl. the §C/§D capture UI.
- `seed_corpus/`, `citations/`, `buyer_profiles/` — data/config.
- **~30 `test_asclepius_*.py` suites.** Full backend suite: **1690 passed, 1 skipped**.

---

## 3 — The two-frontier / rubric-rigor / taxonomy build (shipped in PR #41)

Implements the **Two-Model / Tiered-Rubric / Novelty PRD**, the **Tier-1 Bug-Fix PRD
(§A–§E)**, its **Rubric Rigor companion (§C, FIX-1…FIX-8)**, and the **Model-Failure
Taxonomy export (§D)** — all V3/V4-scoped.

### Two-Frontier A/B (§A)
Every V3/V4 A/B pair is **one OpenAI + one Anthropic** answer to the *same* rendered
case + system prompt (shared `prompt_hash`; a divergent pair is discarded, never
shown to a physician). Slot assignment is truly random (`SystemRandom`, soft
drift-correction clamped `[0.15,0.85]` — never a learnable A,B,A,B alternation). The
physician sees neither provider nor model (payload is an allowlist of `{id,text}`).

**Fallback ladder (§A3):** two-frontier is the strong default (concurrent, each
provider retried once) → on a genuine single-provider failure it reverts to the old
Anthropic-only method (`legacy_fallback`) → a sustained high fallback rate is an
incident (`needs_baseline` + admin alert). Never a silent single-model pair, never a
gold stand-in.

**V4 BAA gate (§A7):** `ASCLEPIUS_TWO_FRONTIER_V4` defaults **off** — V4 real
de-identified cases stay Anthropic-only (BAA-covered). V3 synthetic (PHI-free) is
always two-frontier.

### V3 generation (§B)
Semantic **case-novelty gate** (`case_near_duplicate`, default 0.90) drops re-skins
before judge budget is spent. **Load-vs-generate split** + continuous supply
(`POST /generation/{specialty}/topup` fills the open pool to a target, reporting
drop-reason counts).

### Tiered + rigorous rubric (§B + §C)
Criticality **tiers** (critical |8-10| / important |4-7| / helpful |1-3|), **≥1
critical negative** required on V3/V4, and a **critical-negative hard-fail** in the
shipped grader. Rubric Rigor fixes:
- **FIX-1** concrete, machine-checkable criteria (`specific`, de-truncation, key-data seeding).
- **FIX-2** package-time grader **validity + reliability** meta-eval.
- **FIX-3** per-criterion **evidence anchors** (grounded).
- **FIX-4** completeness / **premium** gate (≥5 criteria, ≥3 axes, all key criteria specific).
- **FIX-5.1** **quality-scaled rubric marginal**: base $60 × grounded (1.4) × validated (1.5) × premium (1.3), capped $200 — a fully-loaded reusable grader ≈ $164, not a flat $25.
- **FIX-5.2** **eval-pack SKU**: rubric records + `grader_prompt.txt` + `score.py` + `validity_report.json` + `EVAL_PACK.md` ship as a standalone, **re-licensable-per-model-version**, recurring line — reported separately from the one-time data sale in the manifest (`eval_pack`) and datasheet.
- **FIX-6** de-duplicated `value_rubric_marginal` (§E-1).
- **FIX-7** **axis-coverage nudge**: advisory (never a gate) — a rubric missing safety/accuracy/reasoning gets a suggestion + an axis histogram in the card.
- **FIX-8** failure-surface coverage + **hackability** probe (padded-hollow must not beat terse-correct).

### Model-Failure Taxonomy export (§D)
Controlled `FAILURE_MODES` vocabulary; physician-verified `FailureTag` capture (gated
on V3/V4 real-model pairs with a critical-negative rubric); **provider attribution**
via the A/B slot map (same-model `legacy_fallback` → `unattributed`). Export artifacts:
`model_failure_taxonomy.json` (cells × mode/axis/provider/difficulty, small-N
suppressed), `TAXONOMY.md`, a disjoint scored-eval holdout + `score_failuremode.py`,
manifest provenance (κ label-agreement, physician count).

### Cross-cutting (§E)
`ab_source` flows to the buyer export; NULL-provider legacy rows don't break metrics
(all rollups guarded); `score.py` hard-fail has no divide-by-zero; a real-SDK
integration test pins `openai==1.99.9`; reasoning-token headroom for reasoning models
(`LLM_OPENAI_REASONING_RESERVE`, default 12000) so a small cap never yields empty
completions.

---

## 4 — Deployment (Railway)

**Required:** `OPENAI_API_KEY` **and** `ANTHROPIC_API_KEY`. With both set, two-frontier
is fully active — no other variable is needed. `ASCLEPIUS_DB_PATH` points at the
persistent SQLite volume.

**Everything else defaults correctly in code** — set a var only to override:

| Var | Default (no env) | Purpose |
|---|---|---|
| `ASCLEPIUS_AB_SOURCE` | `two_frontier` | A/B mode (on by default) |
| `ASCLEPIUS_BASELINE_MODELS` | `gpt-5,claude-opus-4-8` | the two frontier ids (validated 1 OpenAI + 1 Anthropic at startup) |
| `LLM_OPENAI_REASONING_RESERVE` | `12000` | reasoning-token headroom |
| `ASCLEPIUS_LLM_TIMEOUT_SEC` | `180` | per-call timeout |
| `ASCLEPIUS_CASE_NOVELTY_MAX` | `0.90` | novelty threshold |
| `ASCLEPIUS_MIN_CELL_N` | `5` | taxonomy small-N suppression |
| `ASCLEPIUS_MAX_FALLBACK_RATE` | `0.20` | fallback-ladder incident threshold |
| **`ASCLEPIUS_TWO_FRONTIER_V4`** | **off** | **Compliance — leave unset.** Two-frontier sends the case to OpenAI (not BAA-covered). Do NOT enable for V4 real de-identified data until legal signs off. |

**Compliance note:** V3 cases are synthetic (PHI-free), so two-frontier to OpenAI is
safe. V4 real de-identified cases stay Anthropic-only while `ASCLEPIUS_TWO_FRONTIER_V4`
is off.
