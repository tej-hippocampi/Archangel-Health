# Add a specialty to the Asclepius hard-case engine

Onboarding a new specialty (e.g. cardiology) is **pure config** — no pipeline
changes. The Seedmaker, hardness judge, hard-only serving, and value model are
specialty-agnostic and read the config below. `cardiology` ships as a worked
example (`seed_corpus/cardiology.v1.json` + a `SpecialtyConfig`).

## Three additions

### 1. A seed corpus: `backend/asclepius/seed_corpus/<specialty>.v1.json`
Top-level keys:
- `version`, `specialty`, `ratified`, `review_status` — provenance (stamped onto every generated record).
- `items[]` — seed vignettes. Each item needs: `seed_id`, `specialty`, `topic`
  (**must match a taxonomy bucket id**), `subtopic`, `difficulty`
  (`easy|medium|hard`), `prompt`, `ai_failure_mode`, `why_high_value`,
  `reference_basis`, `reference_type` (`guideline|review|primary_literature|expert_consensus`),
  `capture_reasoning_recommended`, `tags[]`. No PHI; no verbatim benchmark/exam text.
- **Hard-Case Engine config** (WS2):
  - `failure_domains[]` — `{name, weight, why}`: the model-weak areas (given to the hardness judge as context).
  - `hard_case_archetypes[]` — `{topic, failure_domain, why_hard, axes[]}`: seeded hard scenarios the generator varies.
  - `hardness_rubric[]` — the checklist a candidate prompt must satisfy.

### 2. A taxonomy + registry entry: `backend/asclepius/specialties.py`
Add a `TaxonomyBucket` list (bucket `id`s must match the corpus `topic`s) and a
`SpecialtyConfig(name, seed_corpus, taxonomy, enabled=True)` in
`SPECIALTY_REGISTRY`.

### 3. (Optional) a citation library: `backend/asclepius/citations/<specialty>.v1.json`
Curated `{title, section, source_type, identifier, url, snippet, keywords}`
entries so the WS3 one-click citation chip works for the specialty. Absent → the
chip degrades to `skipped` (doctors type citations manually).

## How the engine uses it
- **Generation** scores each candidate with the hardness judge (rubric +
  `failure_domains` context); below `ASCLEPIUS_HARDNESS_MIN` (default 0.7) it is
  dropped as `below_hardness_floor`, otherwise stamped `difficulty=hard` +
  hardness provenance.
- **Serving**: the V3 queue (`GET /tasks/next?portal_version=v3`) serves only
  `difficulty=hard` tasks. Clinicians can flag a served prompt **"not actually
  hard"** (prompt-review verdict `not_hard`), which routes it out and feeds back
  for hardness recalibration.

## Checklist
1. Author `seed_corpus/<specialty>.v1.json` (a clinician + the LLM co-author the archetypes/rubric).
2. Add the taxonomy + `SpecialtyConfig(enabled=True)`.
3. (Optional) add `citations/<specialty>.v1.json`.
4. Run the suite: `pytest backend/tests -k asclepius`.
5. Have a specialist ratify the corpus (`ratified: true`) before selling its data.

No other code changes are required.
