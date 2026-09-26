# Labeling navigation and answer revisions — 2026-09-26

Scope: the V1–V5 contributor workflow, including practice and credentialing exam
surfaces, answer presentation, new longitudinal generation prompts, and review of
submitted corrections. Editing covers the draft before final submission. Accepted
submissions remain immutable; idempotent retries return the accepted original.

## Data boundaries

The existing Asclepius store remains the source of truth in each realm. No schema
migration, task regeneration, source chart update, routing change, payment write,
record replacement or existing-submission backfill is introduced. Team/community
stores, blobs, contracts, uploads, encryption keys and email delivery are outside
this change. Tutorial reveal continues to write no independent commit.

`independent_commits` retains the first server-timestamped answer and citations.
Each submission route (normal, invalid/incoherent/not-hard, and exam) derives an
optional `independent_answer_revision` from that original and the submitted draft.
It is explicitly marked `after_model_reveal`, exposed separately to reviewers,
and packaged as context. It never replaces a blind stance or independent ideal
training target. Client claims about revision phase are discarded. Citation-only
corrections, including URL changes, are retained. Late flags screen completed
clinical prose and clear a prior validity attestation.

Model strings remain unchanged in storage and export. Matched Markdown emphasis
is formatted using text nodes in the reader; editor prefill removes presentation
markers. A formatting-only save serializes the original with `edited=false`.
Both clinical edits and the original candidate remain distinguishable.

Drafts keep separate A, B and both-inadequate branches. Switching branches restores
all authored text, citations, grading and completion state. Answer edits preserve
that work but reopen reasoning/scoring/confidence review. Source/branch guards
keep late reasoning and rubric responses from replacing a different draft.

## Evidence and restore

Local release artifacts: `output/labeling-revision-ux/` in the project workspace.
The SAME frozen SQLite baseline from the preceding longitudinal investigation was
inventoried before and after: 82 tables, 27 rows; no IDs or baselined content lost.
A new isolated database was restored through SQLite's backup API and matched the
same inventory. This small fixture establishes behavior, not a production backup.

A scoped production query opened the deployed database in read-only/query-only
mode and collected hashes of the reported chart, its seven derived points and
original model answers, plus assignment/submission identifiers. No clinical prose
or credentials were exported. A temporary SSH key was registered and removed in
a `finally` block; the connection audit records successful removal. Compare this
baseline after deployment, along with served code hashes and public health.

No new durable store or key dependency is created. Existing production backup and
key recovery arrangements remain in force; this release does not claim a new
full-store/off-volume restore drill. Rollback is a code redeployment. Keep the
additive correction payloads and accepted rows; do not erase them on rollback.

## Failure and regression checks

- All five labeling versions: reveal → back → edit → refresh → next, verdict
  round trips, invalid flag after reveal, failed flag and successful retry.
- Full longitudinal grading, outcome rendering, next assignment navigation,
  offline/timeouts, and recovery after refresh.
- Every completed step reopens; a changed final answer cannot bypass the required
  review by jumping directly to confidence. Slow reveal preserves the authoritative
  original even if the local input changes while the request is pending.
- Forced legacy re-splits, back navigation during a split, verdict changes during
  grading generation, citation-only changes, duplicate submissions, and discarded
  forged revision provenance.
- Safe formatted text and raw numeric values, source offsets for error/diff
  highlights, literal HTML rendered inert, unchanged baseline outputs, reviewer
  visibility and source links, and no spurious physician correction record.
- Longitudinal-only style instructions and hashes of actual generation inputs;
  no hard output truncation or retrospective rewriting of model answers.

Validation before final patch review: 521 focused backend/client/harness tests
and 97 full physician-browser/visual tests passed. Final targeted tests cover
subsequent serializer and reviewer fixes. Independent auditor `audit_labeling_revision`
reviewed the implementation with fresh context; findings were fixed and returned
for confirmation. Final CI and deployment evidence are attached to the PR/release
artifacts. No real physician evaluation or operational test email is submitted.
