---
name: surgical-risk-triage
description: Work on the ACS-NSQIP-anchored surgical risk tiering system (TIER_1/2/3) — initial pre-op tiering, intra-op reassessment from operative notes, post-op daily scoring, and re-tiering. Use for any change to triage scoring, tier semantics, escalators, or the triage prompts.
---

# Surgical Risk Triage (3-tier model)

Risk-tier TEAM surgical episodes across the perioperative timeline:
**TIER_1** (low) / **TIER_2** (moderate) / **TIER_3** (high), anchored on
ACS-NSQIP variables. Tiering is deterministic and itemized; the LLM's only
job is extracting clinical facts from documents.

## Architecture invariants

1. **Two-stage initial tier** (`backend/triage/initial_tier.py`, PRD §5.5):
   - Stage 1: any *hard escalator* → TIER_3 immediately (score `None`, single
     HARD reason).
   - Stage 2: `PROCEDURE_BASE[family] + Σ soft-factor weights` → score →
     tier via `SCORE_TO_TIER` thresholds.
   All weights/thresholds live in `backend/triage/tuning.py` — never inline
   constants in the algorithm.
2. **Every tier is explained.** Output is a `TierAssignment` with itemized
   `TierReason` entries (HARD/BASE/SOFT, code, label, weight) plus
   `model_version`/`tuning_version` — these feed the audit log and the
   clinician-facing explanation (`backend/routers/triage_explain.py`).
   Any new factor must produce a reason entry.
3. **LLM extracts, Python decides.** Intra-op reassessment extracts operative
   note fields via forced tool-use with per-field confidence
   (`backend/triage/intraop/extractor_llm.py` — EBL, transfusions,
   conversion, hypotension, plus procedure-family fields for LEJR / CABG /
   SPINAL_FUSION / HIP_FEMUR_FRACTURE / MAJOR_BOWEL). Null + NOT_FOUND beats
   a guess; `conservative_default.py` governs what happens when extraction
   is missing or low-confidence.
4. **Tier changes are events.** Re-tiering (intake, PAM proxy, surveys,
   post-op check-ins) goes through delta/apply modules
   (`intraop/delta.py`, `intraop/apply.py`, `postop/`, `preop_retier/`) so
   transitions are audited — never mutate a tier directly.
5. **Patients never see tiers.** Tier/score values are clinician-facing only;
   the patient dashboard must not display them (PRD §13).

## Key files

| Concern | File |
|---|---|
| Initial tier algorithm | `backend/triage/initial_tier.py` |
| Weights, thresholds, labels | `backend/triage/tuning.py` |
| Input/output schemas | `backend/triage/types.py` |
| Flag derivation (ICD-10, labs, meds, social) | `backend/triage/derive_flags.py`, `*_flags.py` |
| Intra-op extractor prompt + tool schema | `backend/triage/intraop/extractor_llm.py` |
| Re-tier / apply / delta flows | `backend/triage/intraop/`, `backend/triage/postop/`, `backend/triage/preop_retier/` |
| Prompt registry entry | `backend/prompts/registry.py` (`intraop_extract`) |

Run `cd backend && python3 -m pytest tests/ -q -k "triage or intraop or postop or initial_tier"`
after any change; the suite includes synthetic-load and cohesion tests that
catch tuning regressions.
