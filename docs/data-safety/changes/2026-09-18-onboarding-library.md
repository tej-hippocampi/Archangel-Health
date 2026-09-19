# Prepared specialty onboarding library

## Scope and invariants

Updates PR #164 with 43 named specialties and two distinct onboarding topics each.
Published entries are backend-only immutable JSON; pathology images are pinned,
public-domain, visually inspected and stripped of metadata. Source provenance is
recorded in `backend/asclepius/onboarding_material/image_sources.json`. No partner
record, uploaded CV, physician identity or production database is used to build
cases. Independent real-model review runs only in isolated GitHub Actions stores.

Read-only bundle lookup supplements the existing realm-scoped onboarding bank.
Existing ready database entries always win. Missing entries never route to another
specialty. All existing gold batches, assignments, submissions, earnings, files
and submitted exams are preserved. The separate exam/practice draw state follows
the preservation rules in the September 17 change record. No new migration or
data deletion is introduced here. Partner asset authorization remains unchanged;
onboarding images require the requesting physician's actual drawn case stamp.

## Validation and failure checks

The prior 43 focused onboarding tests passed before these edits, including the
same-dataset before/after inventory and isolated SQLite backup/restore checks.
The new matrix tests every requested specialty through both draw endpoints.
Additional checks cover offline bundle reads without database writes, preserved
gold batches/ready rows, changed content, fake reviews, single-provider reviews,
wrong blind answers, missing pixel review, denied cross-user image access and
image metadata. Both clinical reviewers receive exactly the served image bytes;
the blinded solve excludes interpretation captions and answer keys.

Clinical approval and software correctness are separate gates. The release
coverage test fails until all 86 real-reviewed entries are present. Rejected
author/reviewer output is never published. Partial successful builds are retained
as artifacts and can be retried without discarding them. Production backup/restore
and alert coverage remain separate release checks under POLICY.md; passing a
synthetic fixture does not establish production protection.

## Rollback

Revert application code without deleting the onboarding bank, existing case
files, draws or exams. Keep the deployed material version with the release.
Immutable bundled IDs must not be reused for new content; publish a new version
instead. Do not restore over a live database or modify submitted assessments.

## Independent review and results

Fresh-context re-audit confirmed strict solver blinding, age scope, pinned image
assistance and bounded retrieval: 20 targeted tests passed. Eleven real-reviewed
case artifacts initially passed structural validation; five rejected artifacts
remain outside the release bank and available in the original CI build. Credits
were restored and the CI builder switched to OpenAI authorship with both-provider
review unchanged. The subsequent content audit found nine answer-leaking or
leading artifacts across the two builds; they are quarantined outside the bank,
with originals preserved. Eight new, undeployed release candidates remain after
collecting and auditing passing companions. No production batch was changed.
The full 86-case coverage gate remains failing. Latest software checks: 491
regressions passed, 2 skipped, plus
7 browser journeys passed. See `docs/validation/ONBOARDING_LIBRARY_2026-09-18.md`.

The CI-only rejected-review diagnostic writer runs after whole-entry identifier
screening, uses a separate filename namespace, and cannot publish a case. It
awaits both provider outcomes and retains input/source/image hashes for failure
investigation. Production does not write these files. Additional regression:
120 passed, 1 release-completeness test deselected; independent code re-audit:
8 passed. Release thresholds, immutable published IDs and paid-inventory
isolation are unchanged.

The subsequent source audit found unsupported recommendations despite provider
approval. Eight additional artifacts were quarantined; seven independently
source-audited documents remain. Their complete original bytes are represented
by canonical document hashes in `legacy_evidence_audits.json`. Any change to a
legacy case, source or review invalidates that exception. All new generation
requires exact source excerpts, checked against retrieved text, plus the existing
two-provider clinical review. No production records or submitted cases were
rewritten. Source-protocol validation: 154 builder tests and 126 independent tests
passed (the unfinished 86-case release gate remains separately failing).

Follow-up:9/86 independently audited artifacts retained. Added reference retrieval
and passage-provenance checks only; no migration or mutation of production data.
Existing batches, submitted exams and reviewed documents are preserved. New
rejected draft artifacts are retained outside the release bank. Full clinical
coverage and production backup/alert verification still block release.

Recovery build: 40/86 independently audited, undeployed artifacts retained. Both
new geriatrics drafts and a colorectal examination were quarantined with original
bytes preserved. No existing committed document or production row changed.
Reference/provenance improvements and an author-only metadata guard address the
observed defects; source support, independent clinical review and the full 86-case
gate remain required. The 153 focused software tests include read-only bundle
inventory and preserved-draw checks; the known completeness failure is tracked
separately. These checks do not verify live backup or alert coverage.
