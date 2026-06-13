# Archangel Episode OS — Foundry/AIP submission kit

Everything needed to build the TEAM episode platform on the Foundry Ontology via
the AI FDE agent, scoped to a single 4-minute hero demo.

## What to upload to Foundry
1. **`data/*.csv`** — 16 object-shaped CSVs, ~300 notional TEAM episodes (no PHI),
   deterministic (`generate_dataset.py`, seed 20260613). Hero episode = `EP-0009`.
2. **`instructions/00_RECONCILIATION_AND_DATA_DICTIONARY.md`** — ontology↔repo
   reconciliation (what to reuse / what's net-new) + the data dictionary.
3. **`instructions/01_BUILD_ORDER.md`** — dependency-ordered build, mapped to PRD §9.
4. **`instructions/02_PIPELINE_BUILDER_TRANSFORMS.md`** — CSV→Ontology transform logic.
5. **`instructions/03_AI_FDE_PROMPT.md`** — the prompt to paste into AI FDE.

## Order of operations
Enable AIP + Global Branching → upload the CSVs + the four instruction files →
open AI FDE on branch `risk-lab` → paste the prompt from file `03`.

## Regenerate the data
```
cd foundry_submission && python3 generate_dataset.py   # → data/*.csv + data/_manifest.json
```

Provenance of the cohort: repo seed shapes (`triage_demo_seed.py`, `sample_ehrs/`),
repo enums (`triage/types.py`, `triage/postop/types.py`, `telehealth/gcodes.py`), the
challenge PMC notes / ICD-10 extract as the note-ingestion corpus, and Synthea-style
cost/claims streams for the CostEvent rollup. See file `00` §B for the full mapping.
