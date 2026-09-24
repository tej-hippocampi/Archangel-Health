# International physician onboarding continuity — 24 September 2026

## Problem and expected behavior

Applicants outside the US reported US-only credential defaults after CV upload
and an unexplained disabled Continue button. PR #176 fixed the selectable
Outside-the-US registration option, but left downstream defaults and review
transitions uncovered. International applicants must be able to select their
country, correct CV suggestions, resume a saved application, and submit without
US credentials. Password and email verification requirements remain enforced.

## Scope and source of truth

The React onboarding wizard, credential controls and signup registry routing are
changed. No schema migration, production data rewrite, upload storage change,
email-provider change, secret/key change or background reprocessing is included.
The existing team `asclepius_people.credentials_json` stores the reviewed form;
new registrationsByCountry is an additive map preserving identifiers and registry
extras while switching jurisdictions. Country of practice, licensure and degree
are independent. CV assets, hashes, parse attempts and original extraction
results remain server-owned and preserved by the existing credential-save API.

Old unsigned application drafts with an explicit Outside-US answer and implicit
US country defaults are presented as unanswered when they contain no NPI or US
licence-number evidence. Explicit manual country choices are retained. The raw
saved draft is not rewritten on load. Null string answers hydrate as unanswered.
CV qualifications absent from an option catalogue remain visible and editable.

An absent country question retains legacy US routing. An explicit blank modern
licensure answer routes to document review under the existing unknown-region
code ZZ, rather than querying NPPES or guessing licensure from practice country.
No country or qualification change approves a physician or bypasses review.

## Preservation and recovery

The regression test `test_new_international_verification_preserves_existing_rows_and_originals`
takes before/after inventories from the same frozen local Asclepius fixture using
`scripts.data_inventory.snapshot/compare`, checks existing row fields and a
nonempty original-evidence tree, and restores a SQLite backup made via its backup
API into an isolated file. New applicant records may be added; prior identities,
field hashes and file hashes must remain identical. Browser regressions save and
reload credential drafts, switch registration jurisdictions and retain the CV
asset hash through final submission. This verifies fixture scope only.

Production live/sandbox stores, object storage and backups are not modified by
this change. No claim of a new production restore drill is made. Rollback is a
code revert; retain all original CV data and the additive credential map. Never
restore a database over production to undo a UI deployment. Existing account
records and accepted applications are not migrated.

## Failure and retry coverage

Tests cover desktop/mobile signup through submission, CV upload and manual
entry, saving and reloading, country switches and switch-back, different practice
and licensing countries, custom and international qualifications, legacy nulls,
configuration HTTP failure and malformed metadata, invalid advisory regexes,
password validation, preserved OTP gates, and unknown-country document review.
Existing CV lifecycle coverage exercises replacement/failure/retry behavior.
Storage commit boundaries are unchanged; this change adds no new blob writes.

## Review and release evidence

Independent auditor review and exact validation results are recorded in the PR
before release. Application changes require the existing frontend, backend,
browser, data guard and route checks. Tests cannot guarantee every future edge
case; the added regressions exercise the reported failures in existing CI jobs.

Local verification: 814 affected backend/harness checks, 91 frontend tests, four
Chrome applications through submission, and the 100-PDF / 1,300-field corpus
passed. The independent auditor confirmed no remaining P1/P2 findings and
separately checked the responsive grid at seven widths. Mobile controls are
asserted inside the viewport. Production rollout remains a separate release step.
