---
name: team-eligibility-review
description: Review Medicare TEAM eligibility determinations for surgical episodes. Use when working on eligibility extraction, the 6 TEAM checks, verdict rationale, overrides, or finalization (SAVE_AS_TEAM / SAVE_AS_STANDARD) in this repo. Follows a waypoint/rubric workflow adapted from Anthropic's prior-auth-review skill.
---

# TEAM Eligibility Review

Determine whether a Medicare patient qualifies for the CMS Transforming Episode
Accountability Model (TEAM) for a scheduled anchor procedure, with auditable,
per-criterion rationale.

This skill mirrors the production pipeline in this repo. The model never decides
eligibility free-form: extraction is structured tool-use, and verdict logic is
deterministic Python. Your job when using this skill is to follow the same
waypoints and keep the two in sync.

## Waypoint 1 — Structured extraction

Extract the six eligibility dimensions from the patient's documents (X12 271,
PDF eligibility reports, CSV exports, free-text notes).

- Canonical extraction prompt: `backend/prompts/eligibility.py`
  (`ELIGIBILITY_SYSTEM_PROMPT`) — X12 segment signals (EB03 codes, REF*18 MA
  contract prefixes H/R/E), date arithmetic, and conflict-resolution rules live
  there. Do not paraphrase those rules from memory; read the file.
- Tool schema: `EXTRACT_TOOL` in `backend/eligibility/extract.py`. Every field
  carries a verbatim `sourceExcerpt` (≤200 chars) — `"(not present in
  documents)"` for UNKNOWN. Never fabricate excerpts.
- Be conservative: UNKNOWN over a guess, always.

## Waypoint 2 — Deterministic evaluation against the rubric

Never re-derive verdicts with the LLM. Run the deterministic evaluator:

- `backend/eligibility/evaluate.py` — `evaluate()` maps extraction → per-check
  PASS/FAIL/UNKNOWN; `overall_verdict()` maps to
  ELIGIBLE / INELIGIBLE / BLOCKED_UNKNOWN; `apply_overrides()` merges audited
  RN overrides; `build_rationale()` produces the criterion → evidence →
  reasoning → recommended-action entries shown on the review/override screen.
- The rubric (criteria, verdict mapping, recommended actions) is documented in
  [references/rubric.md](references/rubric.md). If you change `evaluate.py`,
  update the rubric file in the same change, and vice versa.

## Waypoint 3 — Review, override, finalize

- FAIL/UNKNOWN checks are overridable by clinical staff with a mandatory
  reason; overrides are audited (`store.append_audit`) and re-rendered with an
  `override` annotation in the rationale.
- `SAVE_AS_TEAM` is only permitted when the overall verdict is ELIGIBLE.
  A human (RN coordinator / surgeon) always makes the final call — never
  auto-finalize.

## Key files

| Concern | File |
|---|---|
| Extraction prompts | `backend/prompts/eligibility.py` |
| Extraction tool schemas | `backend/eligibility/extract.py` |
| Verdict logic + rationale rubric | `backend/eligibility/evaluate.py` |
| Pipeline (parse → extract → evaluate → SSE) | `backend/eligibility/pipeline.py` |
| HTTP surface (checks, override, finalize) | `backend/routers/eligibility.py` |
| Review/override UI | `frontend/doctor.html` (`renderDetermineResult`) |
| Tests | `backend/tests/test_eligibility_*.py` |

Run tests with `cd backend && python3 -m pytest tests/ -q -k eligibility`.
