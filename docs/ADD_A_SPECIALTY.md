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

## Multimodal (structured-case) archetypes — V3

A specialty can also produce **multimodal cases** (Synthetic Multimodal Cases
PRD): a structured clinical case (lab panels + notes + meds/problems/vitals) the
specialist reasons *across*, not a one-line prompt. This is pure config too — the
case generator, case judge (Stage 3c), value multiplier, and export filters are
all specialty-agnostic.

### Add `multimodal_archetypes[]` to the seed corpus
Each archetype seeds one case the generator varies:
- `topic` (**must match a taxonomy bucket id**), `subtopic`, `why_hard`.
- `multimodal` — the case shape hint: `{panels[], notes[], hard_hook, ...}`
  (e.g. `panels: ["BMP", "urine studies"]`, `hard_hook: "urine osm decides"`).
- The generator authors a PHI-free case with a fixed **held-out answer key**
  (`ground_truth`) + a shortcut path (`reasoning_divergence`); `public_case`
  strips the key before it is blinded to an evaluator or shipped.

Generation gates every case through the case judge — coherence,
`ground_truth_determinable`, `multimodal_necessity`, and
`reasoning_divergence_potential` floors (env-tunable) — before it becomes a task.
Multimodal tasks are always `difficulty=hard`, always capture the reasoning
trace, and carry a **1.35× value multiplier** (`ASCLEPIUS_VALUE_MULTIMODAL_MULT`).

Invariants (enforced, not optional): age **bands** only; lab timing is
**relative** (`collected_offset_days`), never a date.

### Structured `studies` — the multimodal modalities (Specialty Hyper-Personalization PRD §3)
Cardiology reasoning lives in the **ECG/echo/cath**; oncology in **pathology/
imaging/molecular**. `ClinicalCase.studies: List[Study]` carries these as a
**structured findings report** + numeric `measurements` (EF %, gradient, SUVmax,
molecular VAF) — never a raw waveform/pixel image in V3 synthetic (a raw image is a
V4-only asset, see below). `assert_multimodal_content` is **strengthened per
specialty**: a cardiology case must carry ≥1 `ecg`/`echo` study; an oncology case
≥1 of `pathology`/imaging(`ct`/`mri`/`pet`)/`molecular`; nephrology is unchanged
(labs-driven). Add the specialty's construction rule via
`prompts.specialty_case_gen_rules` (PRD §4.3/§5.3) — one code path, specialty is
config, never a fork.

### The empirical-difficulty gate (PRD §9 — the prime directive)
A case ships only if it **breaks frontier models** — wrong ground truth OR wrong
reasoning. `asclepius/empirical_difficulty.py` runs the case through the live
frontier baseline models and scores BOTH axes; `empirical_difficulty` +
`difficulty_measured` are first-class task columns. Measurement is OFF by default
(`ASCLEPIUS_MEASURE_EMPIRICAL_DIFFICULTY`); authored/declared seeds carry a declared
value with `measured=False`. Turn `ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY` on to
enforce a live-measured floor at serving time (prod posture).

**Evaluator view.** A multimodal task renders a structured case panel whose tabs
are **data-driven from a per-specialty `SPECIALTY_UI` config** (cardiology: ECG ·
Echo/Imaging · Labs · EHR · Meds; oncology: Pathology · Imaging timeline ·
Molecular · Labs · EHR · Meds; nephrology unchanged) — one render path, never a
per-specialty fork. Specialty chips are deterministic: nephrology **green**,
cardiology **orange**, oncology **pink** (no blue). The clinician can flag a case
as internally inconsistent (`case_incoherent`) — the human counterpart to the
case-judge coherence gate.

### Real de-identified cases (`real_deid`) — the ingest seam
`case_source` is `synthetic` (generated) or `real_deid` (parsed from a real,
de-identified export). Real cases come in through `case_formats.ingest_real_deid`:
a format adapter (`lab_csv` / `fhir_r4` / `hl7v2`) maps the export to a
`ClinicalCase`, then `deidentify()` enforces the Safe-Harbor bar (age banding,
residual-identifier scan, relative-offset check) before the case is stamped.
The adapters are a wired seam (`CaseFormatNotImplemented` until a parser lands);
every downstream path already handles `real_deid` with no change.

### V4 real images (V4 Image Embedding PRD) — `docs/asclepius/V4_IMAGE_EMBEDDING.md`
A **V4** (`real_deid`) case can attach a real de-identified image (ECG strip,
echo/CT/PET still, pathology region) to a `Study` via a `StudyAsset` reference. The
bytes live in a content-addressed asset store (`ASCLEPIUS_ASSET_STORE`), never in
`asclepius.db`. Ingest accepts **PNG/JPEG/PDF only**, enforces size/dim caps,
renders PDF→raster, strips EXIF/technical metadata, hashes, and dedupes. Both
frontier A/B answers are produced by **vision models reading the identical image**
(the `prompt_hash` includes the image `sha256`). Images never touch V1/V2/V3.

### Export
Filter a batch by `modality` (`text` | `multimodal`) and `case_source`
(`synthetic` | `real_deid`). The held-out answer key ships only on an explicit
`include_answer_key` benchmark export (under `answer_key`, never raw
`ground_truth`).

## Checklist
1. Author `seed_corpus/<specialty>.v1.json` (a clinician + the LLM co-author the archetypes/rubric).
2. (Optional) add `multimodal_archetypes[]` for V3 structured cases.
3. Add the taxonomy + `SpecialtyConfig(enabled=True)`.
4. (Optional) add `citations/<specialty>.v1.json`.
5. Run the suite: `pytest backend/tests -k asclepius`.
6. Have a specialist ratify the corpus (`ratified: true`) before selling its data.

No other code changes are required.
