# Sample EHRs for TEAM eligibility checker testing

Five synthetic single-patient EHR summaries (all PHI is fictional) for manual
upload testing of the TEAM eligibility pipeline. Each file targets one verdict
path through the six checks (`backend/eligibility/evaluate.py`).

| File | Patient | Anchor procedure | Surgery date | Expected verdict | Check exercised |
|---|---|---|---|---|---|
| `01_okafor_eligible_clean.txt` | Margaret Okafor | LEJR (TKA) | 2026-07-14 | ELIGIBLE | All 6 PASS; CKD stage 3a distractor must NOT trip `not_esrd_basis` |
| `02_brennan_ineligible_medicare_advantage.txt` | Harold Brennan | CABG | 2026-07-22 | INELIGIBLE | `not_ma` FAIL — intake says "Medicare" but H1036 contract = MA (conflict-resolution rule) |
| `03_castillo_ineligible_medicare_secondary.txt` | Diane Castillo | SPINAL_FUSION | 2026-08-05 | INELIGIBLE | `medicare_primary` FAIL — working-aged MSP, EGHP primary |
| `04_whitfield_ineligible_esrd_basis.txt` | Samuel Whitfield | HIP_FEMUR | 2026-06-25 | INELIGIBLE | `not_esrd_basis` FAIL — under-65, entitlement code C (true ESRD basis, unlike case 01) |
| `05_delgado_blocked_unknown_partB.txt` | Rosa Delgado | MAJOR_BOWEL | 2026-07-30 | BLOCKED_UNKNOWN | `partB_active` UNKNOWN — Part B verification pending, all other checks PASS |

All MBIs match the pipeline's MBI regex. Surgery dates are in the future
relative to June 2026; coverage effective dates precede them.
