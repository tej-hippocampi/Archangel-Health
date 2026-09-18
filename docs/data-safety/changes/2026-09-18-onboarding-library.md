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

Fresh-context review and CI results will be recorded in PR #164 before release.
