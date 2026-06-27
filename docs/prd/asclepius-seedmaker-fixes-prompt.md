# Cursor Handoff — Asclepius Seedmaker (Nephrology) Fixes

The Seedmaker build is strong: it compiles, the corpus is medically accurate and
contamination-clean, blinding + provenance are correct, and the new tests mock
the LLM and cover the gates. **No medical inaccuracies and no critical code bugs
were found.** The items below are gaps against the PRD and quality/correctness
refinements. Fix in priority order. Do **not** regress the parts that are already
correct (generation pipeline, provenance, intended_flawed_id blinding, gates).

---

## 🔴 P1-A — Expand the nephrology seed corpus from 24 → 100 (the headline gap)

`backend/asclepius/seed_corpus/nephrology.v1.json` currently has **24** items
(3 per bucket). The PRD and the taxonomy `target_count`s specify **100**. The
generator works with 24 (it tops up few-shot exemplars), but the corpus is the
quality anchor — a thin corpus means thinner, more repetitive generation.

Expand to **100 items** honoring the per-bucket `target_count` in
`backend/asclepius/specialties.py::NEPHROLOGY_TAXONOMY` (they sum to 100):

| bucket | target | have | add |
| --- | --- | --- | --- |
| renal_drug_dosing | 16 | 3 | +13 |
| dialysis_prescription | 14 | 3 | +11 |
| electrolyte_acid_base | 16 | 3 | +13 |
| recent_standard_of_care | 14 | 3 | +11 |
| transplant | 10 | 3 | +7 |
| glomerular_autoimmune | 12 | 3 | +9 |
| aki_critical_care | 10 | 3 | +7 |
| special_populations | 8 | 3 | +5 |

Hard requirements for every new item (match the existing 24's quality bar):
- **Original synthetic vignettes only.** Never copy or lightly reword text from
  MedQA / MedMCQA / PubMedQA / MMLU-med / board exams / any question bank. Each
  new prompt must pass `validation.contamination_hits()` (empty) and be unique.
- **Cover the `subtopics`** listed for each bucket in `specialties.py` (e.g.
  `kt_v_adequacy`, `mineral_bone_disease`, `hypercalcemia`, `nsaid`,
  `antibiotic_adjustment`, `kdigo_2024_ckd`, `kdigo_2025_igan`,
  `nephrotic_management`, `contrast_associated_aki`, `pediatric_dosing`,
  `goals_of_care`, `immunosuppression_special`, `tacrolimus_dosing`) — the
  current 24 only touch one subtopic per bucket.
- **Target genuine AI-failure zones** (the value thesis): eGFR-indexed dosing &
  contraindications, correction-*rate* safety (Na/K/Ca), recently-changed
  standard-of-care (AI cutoff-lag), and judgment tradeoffs. **No easy recall.**
- **Respect each bucket's `min_difficulty`** (transplant/aki/special_populations
  are `hard`); keep a healthy hard:medium ratio (current corpus ≈ 14:10 — keep
  ≥50% hard).
- Each item carries the full §5.2 schema (`seed_id` sequential `neph-seed-00NN`,
  `topic` = bucket id, `subtopic`, `difficulty`, `prompt`, `ai_failure_mode`,
  `why_high_value`, `reference_basis`, `reference_type`, `capture_reasoning_recommended`,
  `tags`). `reference_basis` cites the **concept** source (guideline/review),
  never copied question text. No PHI (ages/generic details only).
- Ground new items in **current** nephrology standard-of-care: KDIGO 2024 CKD,
  KDIGO 2025 IgAN/IgAV, SGLT2i (eGFR ≥20 initiation), finerenone (K thresholds),
  GLP-1 RA kidney outcomes, contrast-AKI (NAC/bicarb not beneficial), HRS-AKI
  terlipressin, ANCA/lupus modern induction, ODS correction limits, apixaban
  2-of-3 criteria, etc.

You may use `python3 scripts/build_nephrology_seed_corpus.py expand --per-bucket N
--out /tmp/nephrology.v2.draft.json` (needs `ANTHROPIC_API_KEY`) to draft
candidates through the prompt-gen + judge + contamination/dedupe gates, then
hand-curate the survivors into `nephrology.v1.json`. **Keep `ratified: false`**
(see P1-C). Do not auto-overwrite the committed corpus without review.

Then update the test that hardcodes the count:
- `backend/tests/test_asclepius_corpus.py`: change `assert len(c["items"]) == 24`
  to `== 100`, and add a test asserting each bucket meets its `target_count`
  (`by_bucket[b.id] >= bucket.target_count`).

---

## 🔴 P1-B — Default the prompt-gen & judge to the strongest model

For the "best prompts" mandate, prompt **generation** and the **error-likelihood
judge** are the quality-critical roles and should default to the strongest model.
In `backend/ai/model_config.py` they currently default to `claude-sonnet-4-6`:
```python
"asclepius_prompt_gen":   {"model": "claude-sonnet-4-6", ...},
"asclepius_prompt_judge": {"model": "claude-sonnet-4-6", ...},
```
Change both defaults to **`claude-opus-4-8`** (the `.env.example` already documents
`MODEL_ASCLEPIUS_PROMPT_GEN/JUDGE=claude-opus-4-8` overrides — make them the
default, not just an override). **Leave `asclepius_candidate_gen` on
`claude-sonnet-4-6` on purpose** — a non-maximal model produces the realistic,
revisable flaws the dataset monetizes.

---

## 🔴 P1-C — Provenance honesty for an unratified, synthetic corpus

The corpus is `ratified: false` / `ai_drafted_pending_clinician_review` and the
generated prompts are synthetic. Buyers must not be told otherwise.
- Add `reviewed_by` and `reviewed_at` fields to the corpus JSON (null until the
  nephrologist signs off); flip `ratified: true` only after human review.
- Ensure the **export datasheet** (`backend/asclepius/export.py`) states, when a
  batch contains `source=internal_prompt_bank` records, that prompts are
  **synthetically generated** (engine + `seed_corpus_version`) and notes whether
  the seed corpus is clinician-ratified. Do not let a synthetic-prompt batch read
  as if the prompts themselves are expert-authored. (The doctor's *answer/revision*
  is the expert signal; the *prompt* is synthetic — keep that distinction explicit.)
- Optional but recommended: block (or loudly warn on) export of
  `internal_prompt_bank` records whose `seed_corpus_version` is unratified, so we
  never ship against an unreviewed corpus by accident.

---

## 🟠 P2-A — Enforce each bucket's `min_difficulty` in generation

`generation.py` accepts a generated prompt's difficulty as-is
(`difficulty = p.get("difficulty") if in (easy,medium,hard) else "hard"`) and
never checks the bucket floor. A `medium` prompt generated for a `hard` bucket
(transplant/aki/special_populations) slips through. Enforce the floor: if a
generated prompt's difficulty is below `bucket.min_difficulty`, either drop it
(`dropped["below_min_difficulty"]`) or upgrade the stored difficulty to the
floor. Prefer dropping so the judge/quality bar stays honest.

## 🟠 P2-B — `difficulty_mix` is a silent no-op — implement or remove

`generate_tasks(difficulty_mix=...)` (and the `GenerationRequest.difficulty_mix`
field) is stored in `params` but never used to steer generation. Either:
- implement it (pass the desired mix into `run_prompt_gen` so the model targets it,
  and/or post-filter accepted items toward the ratio), or
- remove the parameter from `generation.py`, `schemas.py`, and the router so the
  API doesn't advertise a knob that does nothing.

## 🟠 P2-C — Strengthen dedupe to catch near-duplicates

Novelty dedupe in `generation.py::_prompt_hash` is an exact hash of normalized
(lowercased, whitespace-collapsed) text, so a reworded near-duplicate passes. Add
a lightweight similarity guard against the seed corpus + prior generations — e.g.
token-set Jaccard or character-shingle overlap above a threshold (≈0.8) →
`dropped["near_duplicate"]`. Keep the exact-hash fast path; add the fuzzy check
only on survivors (bounded cost).

---

## 🟡 P3 — Smaller items
- Add a generation test that a generated `medium` prompt in a `hard` bucket is
  dropped (covers P2-A), and one that near-duplicates are dropped (covers P2-C).
- `_bucket_order` weighting means a small N (e.g. 10) won't touch all 8 buckets in
  one run — acceptable, but consider logging which buckets a run covered so admins
  can balance over multiple runs.
- Consider persisting the judge `explanation` alongside the scores in the task
  `generation` block for later auditing of why items were accepted.

---

## ⚠️ Carryover — these PRE-EXISTING integration blockers still apply

The Seedmaker sits on top of the standalone Asclepius build, which is **not yet
reconciled** with the earlier admin-tab implementation committed to the branch.
The separate handoff `docs/prd/asclepius-integration-fixes-prompt.md` still must
be applied. In particular, still-open and relevant here:
- **PHI scan is a silent no-op:** `backend/asclepius/validation.py` imports
  `from gold.deid import residual_identifiers` with a fallback to `[]`, and the
  `gold` package isn't in the repo — so the PHI scan does nothing while records
  claim `contains_phi: false`. (Seedmaker's `contamination_hits` is self-contained
  and fine; the PHI gap is separate and still real.) Implement a self-contained
  regex PHI scanner in `validation.py`.
- **Collision with the older admin-tab build** on the branch (`backend/asclepius/`
  orphans `seed.py`/`buyer_profiles.py`, `routers/asclepius.py` at prefix
  `/admin/asclepius`, duplicate `main.py` wiring, the redundant `admin.html` tab)
  and the **`gold` router lines** that would break `main.py` import.

Apply `asclepius-integration-fixes-prompt.md` first (or alongside) so the whole
thing boots and the trust claims are true.

---

## Final verification
```bash
cd backend
python3 -m py_compile $(git ls-files 'asclepius/*.py' 'routers/asclepius.py')
python3 scripts/build_nephrology_seed_corpus.py            # validate: 100 items, all buckets meet target
python3 -m pytest tests/test_asclepius_corpus.py tests/test_asclepius_generation.py -q
```
Acceptance: corpus has 100 schema-valid, contamination-clean, medically-current
items meeting every bucket target; prompt-gen/judge default to opus-4-8;
min_difficulty enforced; difficulty_mix implemented-or-removed; near-dup dedupe
active; datasheet states synthetic-prompt provenance; corpus stays `ratified:false`
until the nephrologist signs off.
```
```
