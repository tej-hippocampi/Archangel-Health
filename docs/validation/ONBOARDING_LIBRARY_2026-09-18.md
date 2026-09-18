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

## OpenAI generation and remaining release blockers

[Real-model build 35389308065](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35389308065)
was stopped after Anthropic returned HTTP 400: the account credit balance was too
low to access the API. Completed passing material was retained. The user restored
credits. The workflow now uses OpenAI (`gpt-5`) for authorship and retains both
Anthropic and OpenAI independent blinded solves and clinical reviews. This is a
CI library-build override; production and paid-generation model defaults are
unchanged. The switch passed 134 targeted tests and independent review.

[Pathology probe 35398241075](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35398241075)
made real model calls without billing errors, but neither case passed all clinical
gates. Blind-review confidence was below the unchanged 0.90 threshold. No pathology
artifact was accepted. The subsequent
[43-specialty OpenAI build](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35399238129)
was stopped after the content audit found repeated answer leaks; rerun missing
pairs using the corrected prompts and full diagnostics. Completed outputs were
retained, and their originals remain available for investigation.

The independent audit rejected a newly generated family medicine examination
because its visible note disclosed the intended management plan. The artifact
was quarantined outside the release bank, despite both automated approvals. Author
and reviewer prompts now explicitly reject answer leaks and incomplete "less bad"
care. CI diagnostics capture both reviewers' results and the exact screened input;
rejected artifacts cannot load through the release-file namespace. Whole authored
prose, including titles and claim statements, is scanned before retention.

The expanded content audit excluded nine artifacts across the first and OpenAI
builds: internal medicine practice, family medicine practice/examination,
endocrinology practice/examination, cardiology examination, nephrology practice,
rheumatology examination and infectious diseases practice. This leaves **8/86**
release candidates. These are new, undeployed PR artifacts; existing production
batches and submitted assessments have not been changed. Originals are retained
in CI and outside the release directory. Unknown claim fields now fail validation
rather than escaping the identifier scan. Independent code re-audit: **8 focused
tests passed**, both reviewers still required, and acceptance thresholds unchanged.

Latest local diagnostic/privacy regression: **120 passed, 1 deselected**; the separately
reported 86-case completeness failure remains a release blocker. Standard CI on
`297346e` passed all checks except the incomplete-library gate in both backend
and keyless shard 3, plus a sandbox-copy live-file hash assertion in backend
shard 3. All seven sandbox-copy tests passed in an isolated local reproduction;
that intermittent CI failure still needs resolution before merge. Public evidence
preflight retrieved at least two eligible references for all 75 missing topics;
retrieval availability does not establish clinical support for an authored claim.

The sandbox-copy failure was subsequently reproduced with forced garbage
collection during the request: closing the final fixture writer checkpoints the
WAL and changes bytes without changing records. The test now pins a connection
across both snapshots and still hashes the database plus WAL. **7 tests passed**
with forced collection; the independent auditor injected a committed live-fixture
write and confirmed the byte assertion still fails. Production code is unchanged.
The auditor cleared this test-only fix for push; fresh CI remains required.

[Corrected OpenAI build 35400997410](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35400997410)
runs all 43 specialties from `ac289a0`, reusing the eight audited cases.

This PR remains a draft. Neither pathology case is clinically released yet,
despite the image viewer and provenance paths passing software tests. Do not
merge until all 86 artifacts pass review and CI, and production preservation
requirements are verified.
