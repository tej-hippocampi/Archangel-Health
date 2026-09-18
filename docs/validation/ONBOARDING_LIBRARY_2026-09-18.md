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
