# Archangel Episode OS — Foundry/AIP submission kit

Everything needed to build the TEAM episode platform on the Foundry Ontology via
the AI FDE agent, scoped to a single 4-minute hero demo.

## What to upload to Foundry
Ship the single archive **`archangel_foundry_submission.zip`** (built by
`make_zip.sh`). Unzip into one Foundry folder, preserving the layout in `INDEX.md`:
- **`INDEX.md`** — manifest + read order + join keys + enum dictionary (agent reads first).
- **`data/*.csv`** — 16 object-shaped CSVs, ~300 notional TEAM episodes (no PHI),
  deterministic (`generate_dataset.py`, seed 20260613). Hero episode = `EP-0009`.
- **`instructions/00…`** — ontology↔repo reconciliation + data dictionary.
- **`instructions/01_BUILD_ORDER.md`** — dependency-ordered build, mapped to PRD §9.
- **`instructions/02_PIPELINE_BUILDER_TRANSFORMS.md`** — CSV→Ontology transform logic.
- **`instructions/03_AI_FDE_PROMPT.md`** — the prompt to paste into AI FDE.

## Order of operations
Enable AIP + Global Branching → unzip `archangel_foundry_submission.zip` into one
folder → open AI FDE on branch `risk-lab`, point it at the folder → paste the prompt
from `instructions/03_AI_FDE_PROMPT.md` (it tells the agent to read `INDEX.md` first).

## Rebuild the zip
```
cd foundry_submission && python3 generate_dataset.py && bash make_zip.sh
```

## Regenerate the data
```
cd foundry_submission && python3 generate_dataset.py   # → data/*.csv + data/_manifest.json
```

Provenance of the cohort: repo seed shapes (`triage_demo_seed.py`, `sample_ehrs/`),
repo enums (`triage/types.py`, `triage/postop/types.py`, `telehealth/gcodes.py`), the
challenge PMC notes / ICD-10 extract as the note-ingestion corpus, and Synthea-style
cost/claims streams for the CostEvent rollup. See file `00` §B for the full mapping.
