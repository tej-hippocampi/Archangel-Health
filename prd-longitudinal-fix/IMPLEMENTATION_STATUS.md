# Longitudinal patient-4 — accepted implementation

## Design and invariants

Branch `fix/longitudinal-patient4`, based on main `7ee60e6`. The user approved
reconciling the original acceptance criteria to measured source evidence and
holding narrative-incomplete points for review. Production remains unchanged.

Implemented: versioned ingestion reports and re-ingest controls; atomic retry
supersession preserving specialty/history; task/retry exclusion; conservative
answer-key reconciliation; specialty declaration controls; header/prefix note
deduplication scoped to the same day; panel-text exclusion from visible notes and
density; role-based budgets (16 trajectory notes, 10 static notes); discharge
rebasing, withholding, later reveal and bounded answer keys; unsupported-timing
omission; zero-model-call explanation.

No ingestion-case DELETE remains, including startup recovery. A promoted upload
cannot be retried. Task insertion and retry use the same transactional exclusion.
Existing cases, answer keys and task links remain available for audit.

## Accepted evidence and readiness contract

| Chart | Encounters | Density points | Potential outcome pairs | Review-held | Ready before model gates |
|---|---:|---:|---:|---:|---:|
| patient-1 | 22 | 9 | 8 | 8 | 1 |
| patient-2 | 12 | 2 | 1 | 1 | 0; quarantined |
| patient-3 | 5 | 4 | 3 | 3 | 1 |
| patient-4 | 8 | 3 | 2 | 2 | 1 |

The date and density thresholds are unchanged. Patient-4's remaining early sheet
is only 1,021 days before its earliest structured item, below the 1,095-day rule.
It therefore remains a nonqualifying encounter. Patient-4 has 167 curated notes,
157 panels and 7 curated documents withheld for unsupported timing.

Longitudinal generation requires a model-visible clinical narrative from the
current encounter. Reports, discharge summaries, prior-encounter notes, hidden
notes and identifiable order/nursing forms do not satisfy this document-role
check. This is a conservative readiness rule, not an assertion that a note is
clinically complete. No text is fabricated or retrospectively relabeled.

A held point has `review_required=true`, a reason code and an explanation, and
`generatable=false`. It receives no question-authoring call or generated task.
Explicit encounter selection and disabling the density filter cannot clear the
hold. Earlier points depending on a held successor are also held: the system
cannot grade against one successor and reveal a different one. The unaffected
suffix can still be generated. Static generation retains its existing policy.

Patient-4's stable offsets remain −406 / −372 / +1; current encounter ordinals
are 1 / 2 / 7. The first two remain held. Only the terminal point can become an
unrouted, single-label task; it has no subsequent generated outcome. The JSON
reference preserves historical ordinals and marks authored examples as reference
material, distinct from generation-ready tasks.

Reviewers can inspect held points in the admin plan. Automatic runs persist
counts and reasons, including when every point is held. Completed upload rows
retain read-only chart-plan review without creating duplicate tasks. Correct
source timing/types or provide additional contemporaneous source material, then
re-plan. Existing promoted work requires a corrected new upload, not mutation of
its historical raw blob or a retry that would overwrite task provenance.

## Tests

The original baseline had 11 passing patient-focused tests. After literal curation
rules, four old acceptance checks failed; the user then approved the reconciled
counts above. Front-door assertions now pin those counts and the readiness split.

Regression coverage includes encrypted ingest/retry, both retry/task race
orderings, stale sealed-key recovery, real patient-4 offsets and bounded reveal,
custom encounter gaps, narrative roles and chronology, predecessor dependencies,
explicit selection, density overrides, automatic runs (partial and all-held),
and executed admin DOM controls. The final validation result is recorded below.

Local data inventory contains no production database. Before/after checks show
no local ids lost; this is not a claim to have audited production records.

## Out of scope / do not touch

No deployment, production re-ingestion, external messages, live model spending,
physician routing or fixture relocation. Synced sources and unrelated onboarding
work are untouched. The PRD's clinical candidate answers are never hardcoded into
runtime generation.

## Independent audit

Fresh-context audit identified and drove fixes for retry/task races, stale sealed
keys, custom-gap discharge leakage, role classification, outcome-note prioritization,
held-successor bridging, all-held automatic reporting, and trajectory-mode loss
in admin controls. Final confirmation and validation are recorded below.

## Final validation

**397 regression tests passed** with fake model legs, including six executed
admin DOM tests. No acceptance test remains failing or skipped to conceal the
changed yields. JavaScript syntax, whitespace checks, dangling-import scan
(678 Python files), current PRD citation checks and the before/after data
inventory also passed. Full production/live-model behavior was not exercised.

Final independent audit confirmed the hold propagation, all-held automatic
reporting, trajectory-preserving replans and read-only completed-upload review.
No remaining actionable defect was identified. The accepted source limitation
remains explicit: patient-4's first two points require additional contemporaneous
evidence before they can be generated.
