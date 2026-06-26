# Asclepius — Data Curation & Build-Optimization Prompt

> **What this file is.** A standalone prompt to paste into Cursor / Claude Code **alongside** `asclepius-expert-evaluation-portal-v1.md`. The PRD says *what to build*; this prompt says *how to make the emitted data as valuable and as sellable as possible*. Everything here is grounded in what real buyers of medical AI training data (a hungry medical AI lab first; Mercor / Surge / Scale / frontier labs next) actually require in 2026. When this prompt and the PRD conflict on data shape or quality, **this prompt wins** — but never at the cost of the ≤3-min evaluator flow.

---

## 0. Mission (read first)

You are optimizing **Product #3 — the Asclepius Expert Evaluation Portal** — our first revenue product. It needs **no PHI, no consent apparatus, no compliance lag**: a credentialed specialist logs in, sees a clinical prompt with two AI answers, picks the better one and says why, or writes the ideal answer or step-by-step reasoning. The output is **expert reasoning + verified labels** — sellable immediately.

**Go-to-market reality you are building for:**
- **First buyer:** a small, hungry AI medical lab that pays on delivery. They want clean JSONL in *their* eval format, fast, with provenance they can trust.
- **Next channels:** Mercor (30k+ pre-vetted experts), Surge AI, Scale AI, and frontier labs — who buy at volume but hold a **higher quality and provenance bar**.
- **Our moat:** credentialed-specialist provenance + **guideline-grounded reasoning**. That is the whole premium. Commodity labelers cannot produce it; we can, anchored by our nephrologist and clinician network (3–5 clinicians to start).

**Your job in the build:** make every submission turn into the **maximum amount of correctly-formatted, independently-verifiable, premium training signal**, packaged so a buyer can ingest it with zero rework. Optimize for *value density per expert-minute*, not volume.

---

## 1. What the market actually pays for (build to these, exactly)

Diligent market research (2026) says buyers reward five things. Bake each into the build.

### 1.1 Canonical formats, matched byte-for-byte
Do **not** invent schemas. Buyers ingest these and reject anything that needs reshaping:
- **Preference (reward model / RLHF / DPO):** Anthropic `hh-rlhf`-style JSONL — one object per line with `chosen` and `rejected`. Provide both a "chat" variant (`messages` array with roles) and a "flat" variant (`prompt`/`chosen`/`rejected` strings); make the variant a config flag.
- **SFT / instruction tuning:** `{prompt, completion}` (a.k.a. `instruction`/`response`) from the ideal/revised answer.
- **Reasoning trace (process reward model):** **PRM800K-style** — an ordered list of steps, each step independently labeled (`good` / `neutral` / `bad`, plus an optional numeric `step_reward`). This is the highest-value format; treat per-step labels as first-class, not an afterthought.
- Emit **one submission → potentially multiple records** (a "both inadequate" with steps yields an SFT record *and* a reasoning-trace record). Maximize records per expert-minute without duplicating signal.

### 1.2 Guideline-grounded verification = the medical premium
This is the single biggest differentiator and the reason a doctor-rate hour is worth it (research: Med-PRM, MedPRMBench, 2026 — every reasoning step verified against clinical guidelines/literature).
- Add an **optional evidence anchor** to: the "why it's better" rationale, each error tag on the rejected answer, and **each reasoning step**. An evidence anchor = `{citation_text, source_type (guideline|primary_literature|expert_consensus|other), identifier (e.g., KDIGO 2024, PMID, DOI, guideline section)}`.
- Keep it **optional and additive** so it never slows the lightest path — but make capturing it one keystroke (a "cite" affordance per field).
- Records carrying evidence anchors get a `grounded: true` flag and are exportable as a **premium tier** the exporter can filter to. This is what we upsell to frontier labs.

### 1.3 Inter-annotator agreement, computed and surfaced
Buyers ask for it; the industry threshold is **Cohen's κ > 0.7** (substantial agreement).
- Support a **double-labeled subset** (route a configurable % of tasks to two evaluators).
- Compute **Cohen's κ** on the verdict (A/B/both) and, where feasible, agreement on error-tag sets (Jaccard) and on revision overlap.
- Store per-pair and aggregate scores; surface the aggregate in `quality_report.md`. Flag low-agreement tasks for re-review rather than silently exporting them.

### 1.4 Provenance is now regulatory, not optional
Research: FDA Jan-2025 credibility framework, FDA/EMA Jan-2026 joint principles, EU AI Act, GDPR — all require documenting **who** annotated, **which guideline version** was in effect, **when**, and **what changed since**.
- Every record carries: hashed annotator id, **credential string** (board cert + specialty + years), confidence, taxonomy version, app/config version (mirror `APP_AI_CONFIG_VERSION`), task `source` (`lab_supplied` vs `internal_prompt_bank`), and full status-change timestamps.
- Ship a **Datasheets-for-Datasets-style `datasheet.md`** (Gebru et al. standard) with every export: motivation, composition, collection process, annotator credentials in aggregate, preprocessing, recommended uses, and **limitations**. Plus `data_dictionary.md` (every field defined) and `quality_report.md` (counts, κ, QA pass rate, time-floor flags, grounded %).
- Add a per-record **rights attestation**: `license`, `contains_phi: false` (asserted + scanned), and `ip_cleared: true`. Buyers' legal teams need this to purchase.

### 1.5 No contamination, no duplicates, no junk
Labs reject data that overlaps public benchmarks or repeats itself.
- **Dedup** on a normalized hash of (prompt + candidate texts); flag near-duplicates.
- **Contamination check:** flag prompts that look lifted from known public benchmarks (MedQA, MedMCQA, PubMedQA, MMLU-med) — at minimum a substring/shingle check, logged in the quality report.
- **Time-floor & effort checks** (too-fast submissions flagged), empty-field checks, and the **LLM critic** consistency check (`call_llm(role="asclepius_critic")`) catching verdict↔rationale↔chosen-answer contradictions.

---

## 2. The buyer's schema defines "optimal" — make export configurable

The first buyer's eval format *is* the spec. Do **not** hardcode field names into the writer.
- Implement export as a **field-mapping layer** in `backend/asclepius/export.py`: an internal canonical record → a buyer profile (a small JSON/py mapping of `our_field → their_field`, plus which record types and which filters). Ship a `default` profile and make adding a buyer profile a ~10-line change.
- Support export **filters**: by specialty, difficulty, record type, date range, `grounded` tier, confidence floor, and min agreement score.
- Validate every emitted line against the target profile's JSON Schema **before** writing; a batch with any invalid line fails loudly (no partial silent exports).

---

## 3. Optimize the expert's time (value density)

Premium data comes from deep reasoning, not speed (research: AfterQuery and reasoning-task buyers reward quality/depth). But our throughput depends on the ≤3-min flow. Reconcile both:
- **Lightest path stays sacred:** pick a side → optional one-line why → submit. Everything premium (evidence anchors, step labels, from-scratch reasoning) is **progressive disclosure** — one click to expand, never blocking submit.
- **Pre-fill from the critic:** when the LLM critic is confident the rationale is thin or contradicts the verdict, prompt the expert inline *before* submit (cheap quality lift, no extra screen).
- **Capture `time_spent_sec` honestly** (resume after refresh) — it is both a quality signal and the basis for paying clinicians by effort.
- **Reasoning-trace tasks are the money tasks:** make the step editor fast (add step, tab to label, optional cite). Prioritize routing reasoning-capable specialists to `capture_reasoning: true` tasks.

---

## 4. Concrete build directives (apply on top of the PRD)

When implementing the PRD phases, additionally do all of the following:

1. **Schemas (`backend/asclepius/schemas.py`):** add `evidence_anchor` to rationale, each error tag, and each reasoning step; add `grounded`, `license`, `ip_cleared`, `agreement_score`, `taxonomy_version`, `config_version` to the canonical record.
2. **Packaging (`packaging.py`):** emit all three canonical formats in both buyer-ready variants; one submission may yield multiple records; never drop captured signal.
3. **Validation (`validation.py`):** schema-valid + non-empty + time-floor + PHI scan + dedup + contamination check + license/attestation present. Any failure → QA queue with a machine-readable reason.
4. **Agreement (`store.py` + a small `agreement.py`):** double-label routing %, Cohen's κ computation, per-task and aggregate storage.
5. **Critic (`critic.py`):** consistency check via `call_llm(role="asclepius_critic")`; also an optional **grounding check** that, when evidence anchors exist, sanity-checks that the citation supports the claim (reuse the existing grounding-judge pattern if present).
6. **Export (`export.py`):** buyer-profile field mapper + filters + per-line schema validation + `datasheet.md` / `data_dictionary.md` / `quality_report.md` generation + provenance log entry + manifest (`batch.json` with counts, hashes, profile, filters used).
7. **Quality report must include:** total records by type, grounded %, Cohen's κ (aggregate + by specialty), QA pass rate, time-floor/too-fast flag count, dedup/contamination flag count, contributor breakdown (credential mix, hours, counts).
8. **Buyer profiles directory:** `backend/asclepius/buyer_profiles/` with `default.json` and a documented template; first-buyer profile added when their format lands.
9. **Keep PHI defense on** even though prompts are synthetic/de-identified: run the PHI scan, route any hit to QA, and gate any LLM call through the existing subprocessor BAA path.
10. **Tests (`backend/tests/test_asclepius_*.py`):** golden-file tests that a known submission produces byte-stable canonical JSONL; a κ computation test; a contamination/dedup test; an export-schema-validation test; a "lightest path still ≤ N fields" guard.

---

## 5. Definition of "best possible data" (acceptance, data-side)

A batch is buyer-ready when:
- Every line validates against the target buyer profile's schema; the batch ships with `datasheet.md`, `data_dictionary.md`, `quality_report.md`, and a `batch.json` manifest with content hashes.
- Every record carries credential + provenance + license/attestation; nothing reaches `export_ready` without auto-validation **and** the QA gate.
- Aggregate inter-annotator agreement is reported (κ), and low-agreement records are excluded or flagged.
- The **grounded (evidence-anchored) premium tier** is separable and reported.
- Zero PHI (scanned), deduped, and contamination-checked against public medical benchmarks.
- Reformatting for a new buyer is a config change (a new profile), not a code change.

---

## 6. One-paragraph version (if you only read one thing)

Build Asclepius so that the instant a credentialed specialist submits, we emit the **maximum** correctly-formatted training signal — `hh-rlhf` preference pairs, `{prompt, completion}` SFT, and **PRM800K-style per-step-labeled reasoning traces** — each optionally **grounded in a cited clinical guideline** (our medical premium), each carrying **expert credential + full provenance + license attestation**, each **deduped, contamination-checked, PHI-scanned**, with **Cohen's κ inter-annotator agreement** computed on a double-labeled subset and surfaced in a **Datasheets-for-Datasets-style datasheet + data dictionary + quality report**, exported through a **per-buyer configurable field-mapper** so the first lab — and later Mercor/Surge/frontier labs — ingest it with zero rework. Keep the evaluator's lightest path ≤3 minutes; make every premium capture one optional keystroke.

---

### Sources (market research informing this prompt)
- Anthropic `hh-rlhf` (canonical preference JSONL): https://github.com/anthropics/hh-rlhf
- Med-PRM — stepwise, guideline-verified process rewards: https://www.researchgate.net/publication/392717044_Med-PRM_Medical_Reasoning_Models_with_Stepwise_Guideline-verified_Process_Rewards
- MedPRMBench — process reward models in medical reasoning: https://arxiv.org/html/2604.17282
- PRMBench — fine-grained process-reward benchmark: https://arxiv.org/pdf/2501.03124
- RLHF platforms in biotech (Scale vs Labelbox vs in-house): https://intuitionlabs.ai/articles/rlhf-platforms-biotech-comparison
- Evolution of data labeling — Sama, Scale, Surge, Mercor: https://medium.com/@ishisinghal/the-evolution-of-data-labelling-sama-scale-ai-surge-ai-and-mercor-8a8d69514336
- Mercor — AI model training platforms / expert network: https://www.mercor.com/resources/experts/ai-model-training-platforms/
- Surge AI — RLHF & preference data: https://surgehq.ai/
- OpenTrain — RLHF & preference data raters: https://www.opentrain.ai/solutions/rlhf-and-preference-data/
- Data annotation best practices for LLM training (2026): https://neuwark.com/blog/data-annotation-best-practices-llm-training-2026
- Data labeling governance & quality (inter-annotator agreement): https://atlan.com/know/data-labeling-best-practices-llms/
