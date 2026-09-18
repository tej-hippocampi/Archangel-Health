# Specialty onboarding library — draft PR validation

## Independent audit

Fresh-context auditor `audit_library_164` reviewed the library, generation/review
gates, image access, blinding, specialty routing, workflow and preservation paths.

Resolved and independently confirmed:

- P1: provisional applicants could draw but not load pathology images. Drawn
  onboarding assets now work; ordinary assets retain their full-access gate.
- P1: a misnamed artifact could claim another specialty's coverage. Requested
  filename and document identity must now agree.
- P2: real-model smoke could reuse a release case without making model calls.
  Smoke bypasses bundled material; deliberate batch reuse is separately reported.
- P2: admin reviewers could not view the applicant's pathology pixels. Admins
  can now access the two pinned onboarding references.

Auditor validation: **54 passed, 1 deselected**, adversarial swapped-filename
rejected, cached-case smoke reached fresh generation, diff whitespace clean.
No remaining code blockers found for draft PR / isolated CI generation.

## Builder validation before real generation

**512 passed, 1 deselected** across specialty-library routing, prior specialty
cases, tutorials, credentialing/exam access, examination-owed gate, practice gate,
CV extraction, onboarding, image embedding, case schema and buyer-response tests.
The deselected test is the explicit 86-case release completeness gate: artifacts
must be generated and committed before it can pass. Fake model results are never
accepted as release material.

Merge readiness: zero commits behind current main, no merge conflicts. Data-change
guard passed. Route baseline unchanged (676 routes). All 17 preservation PRD
citations passed. Public PubMed retrieval returned eight eligible references for
each of the six pathology/dermatology/neurology curriculum topics.

## Remaining release evidence

Actual case artifacts and their real-model review reports, full 86-case coverage,
CI/browser results and current production preservation checks must be recorded
before release. This report does not claim generated clinical content is ready
or that production backups and alerting are verified.

## Artifact audit and hardening

The first real build exposed two defects missed by automated review: author-added
answer-key fields survived selective blinding, and neonatal/pediatric cases were
accepted for adult nephrology/oncology. Five artifacts were excluded and retained
in the original CI build for investigation. They are not in the release bank.

The solver now receives an explicit field allowlist; entry and candidate objects
reject unexpected fields. Curriculum patient-age scopes are enforced. PubMed
retrieval uses bounded pages to avoid oversized guideline responses. Pathology
assistance sees the same pinned pixels as the applicant without held-out fields.

Independent re-audit confirmed these fixes: **20 targeted tests passed**. All
**11 retained artifacts** passed strict validation, filename identity and source
hash checks. No new blockers were found for updating the draft PR. This is not
approval for clinical release.

Latest builder regression run: **491 passed, 2 skipped, 1 deselected**. The
separately executed completeness test failed as expected: **11/86 ready, 75
missing**. Seven browser journeys passed, including both pathology image viewers,
dermatology/neurology examination submission, optional practice and preparation
recovery. Desktop/mobile screenshots were visually inspected. These UI tests use
synthetic fixtures and do not count as clinical artifact validation.

## Current generation blocker

[Real-model build 35389308065](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35389308065)
was stopped after Anthropic returned HTTP 400: the account credit balance was too
low to access the API. Completed passing material was retained. Restoring credits
for the account behind GitHub's `ANTHROPIC_API_KEY` is necessary before resuming.
The build reuses unchanged, validated release files and generates only missing
entries; clinical acceptance thresholds remain unchanged.

This PR remains a draft. Neither pathology case is clinically released yet,
despite the image viewer and provenance paths passing software tests. Do not
merge until all 86 artifacts pass review and CI, and production preservation
requirements are verified.
