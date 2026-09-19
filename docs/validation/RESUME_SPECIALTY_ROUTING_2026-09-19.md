# Resume-to-case routing verification

## Finding and corrected behavior

PR167 is running in production at commit
`333896355790e89dd58b7e1cd6fe90ffb61c0ff5`. Its full 86-case library is present.
The remaining reproduced defects occur before case selection: CV display parsing
could override an ambiguous or pediatric specialty, a reference physician's
credentials could become the applicant's field, old training could outrank
current work, and a terminal callback could temporarily expose an old CV parse.

The parser now makes one evidence-attributed specialty decision across the
applicant's document. Explicit declarations and current clinical work take
priority; conflicting or unsupported explicit fields need a physician selection.
Employment dates bind within their entry. Reference/publication sections and
negated or aspirational claims do not establish clinical identity. Display text
comes from the same decision. Manual physician choices remain authoritative.

Terminal CV progress is published with its matching result. Failed replacements
clear only untouched suggestions; reloading cannot restore them or skip Review
after the specialty changed. No existing applicant is automatically reparsed or
given a new eligibility decision. A specific historical account must be inspected
before claiming its saved specialty or submitted assessment is corrected.

## Validation

- Focused CV/upload/routing/library suite: **511 passed**, **2 skipped** locally.
  The two real OCR fixtures require system binaries and are mandatory in the CV
  extraction CI workflow. Both 100-PDF corpus tests ran locally, including actual
  HTTP upload/storage/status and production TypeScript autofill.
- Credentialing, examination, self-service onboarding, harness, sharding and CV
  lifecycle suite: **252 passed** (overlaps lifecycle coverage above).
- Frontend suite: **72 passed**, including rendered form recovery, focus/edit
  stability, and production autofill behavior.
- All **43** specialties traverse parsed CV → production TypeScript form →
  account provisioning → both real bundled case endpoints. No fixture cases or
  live model calls substitute for the released library in these checks.
- Stale unfinished practice/exam assignments are replaced with matching cases;
  their original IDs remain in history. Submitted exam preservation and stale
  submission rejection continue to pass.
- Route baseline unchanged: **676 routes**. Dangling-import check clear.
- Data SQL guard and merge readiness clear; zero commits behind frozen main.
- **218** immutable files unchanged, including **86** case bundles and **102**
  synthetic PDFs. SQLite same-dataset inventory/backup comparisons pass.

## Independent final audit

Fresh reviewer `audit_resume_routing_fix` reported no remaining P1/P2 findings
after the builder corrected its concrete reproductions. The auditor independently
ran **264 backend** and **29 frontend** tests, all passing, and verified all 218
immutable files against the frozen base. It confirmed date attribution, unknown
specialty conflicts, reference-section exclusion, failed replacement/reload
cleanup, manual-choice precedence and privacy-minimized evidence metadata.

These results validate software behavior and fixture preservation. They do not
establish perfect extraction of arbitrary CV layouts, physician verification,
clinical ratification of cases, or production backup/restore coverage. Unsupported
wording and uncertainty deliberately require the physician to confirm their field.
The data-safety change record documents the open operational release gate.

## CI clock regression

The first CI run passed the CV/OCR, browser, form and visual checks but exposed
two existing webinar tests that seeded September 2 events and queried them using
the real current date. Both failures reproduced locally. The shared webinar test
fixture now uses the same fixed clock as its seeded scenarios. All original
assertions remain; no community production behavior changed. The complete file
passes **35 tests**, independently rerun and cleared by the fresh auditor.
