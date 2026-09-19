# Specialty onboarding revision and completion

## Scope and preservation

PR #167 repairs rejected synthetic onboarding drafts using their retained evidence
and specific review findings. Author-only revision inputs contain synthetic cases,
public references and review diagnostics. They contain no applicant/CV data or
health-system partner records. All real-model calls run in isolated CI using
GitHub Secrets. No database migration, deployed-store write, email or payment is
part of this change. Existing 41 committed bundles are frozen byte-for-byte;
original trial/rejected artifacts remain retained outside the release bank.

The revision runner skips independently audited trial passes to bound spending,
but these inputs confer no publication authority. Both fresh reviewers independently
solve revised cases, see the actual public payload, and recheck safety, source
entailment, contraindications and independence. Prior feedback is author-only.
Rejection preserves diagnostics; provider failure aborts paid work; no trial
writes tasks, submissions, records or the onboarding bank.

## Verification and release

Before inventory: `output/specialty-onboarding/revision-before-bundles.json`
contains SHA-256 hashes for all 41 existing bundles. Compare this frozen inventory
after additions; no existing file may disappear or change. The previous
same-dataset read-only bundle and SQLite backup/restore regressions remain
applicable. No production restore is attempted. Production backup/alert coverage
has not been newly verified; this work does not claim it has.

Independent content audits of the first 18 trial passes cleared 16 and held two
(palliative answer cue and unrelated ENT evidence). Held cases require fresh
revision and review. Publication remains blocked until clinical, artifact-audit,
coverage and software checks pass. No trial marker may be relabelled to imply
cross-provider or physician review that did not happen. Exact release evidence
and final audits will be appended before merge.

Rollback: revert application changes without removing stored draws, submissions,
ready database cases or paid batches. Keep accepted material and artifact history;
do not overwrite submitted assessment identities.
