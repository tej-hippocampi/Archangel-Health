---
name: ehr-extraction
description: Extract structured clinical fields from raw EHR text (discharge summaries, pre-op notes, op notes) in this repo. Use when working on the EHR ingestion pipeline, extraction schemas, grounding checks, or any Claude-driven clinical NLP extraction.
---

# EHR Structured Extraction

Convert messy EHR text into the structured JSON that drives the patient
dashboard, battlecards, triage, and the Digital Care Companion.

## Core rules (non-negotiable)

1. **Zero hallucinations.** Extract only what is explicitly stated. Absent
   field → `null`, never a guess. This is the contract downstream grounding
   checks enforce.
2. **Schema is code, not prose.** The canonical field list lives in
   `backend/pipeline/extract.py` (`EXTRACTION_PROMPT`) — patient/procedure
   identity, medications (name/dose/frequency/route/status), red flags vs
   normal symptoms, wound care, follow-up, surgical metadata
   (surgeon/site/laterality/facility), `note_type`, and
   `missing_critical_data`. Read it before changing anything; the dashboard
   and avatar prompt builder consume these exact keys.
3. **Identity is never extracted-over.** `patient_id` / `patient_name` from
   ingest metadata always win (`ExtractionLayer.extract`).
4. **Tool-use for new extractors.** New extraction calls should follow the
   forced tool-use pattern (`tool_choice={"type": "tool", ...}`) used in
   `backend/eligibility/extract.py` and
   `backend/triage/intraop/extractor_llm.py`, with per-field self-rated
   confidence (HIGH/MED/LOW/NOT_FOUND) — not free-text JSON.

## Pipeline context

`ingest → extract → grounding check → classify → generate` —
see `backend/pipeline/`. Extraction output must survive
`grounding_check.py` (every claim grounds to source text) before synthesis;
extractions that would fail grounding are bugs in the extractor, not the
judge.

## Key files

| Concern | File |
|---|---|
| System + field-schema prompt | `backend/pipeline/extract.py` |
| Grounding audit (LLM judge) | `backend/pipeline/grounding_check.py` |
| Intra-op note extractor (tool-use exemplar) | `backend/triage/intraop/extractor_llm.py` |
| LLM wrapper + model registry | `backend/ai/llm_client.py`, `backend/ai/model_config.py` |
| Prompt registry / versioning | `backend/prompts/registry.py` (`ehr_extract`) |

When the extraction prompt or schema changes, bump the `ehr_extract` version
in `backend/prompts/registry.py` and run
`cd backend && python3 -m pytest tests/ -q -k "grounding or extract"`.
