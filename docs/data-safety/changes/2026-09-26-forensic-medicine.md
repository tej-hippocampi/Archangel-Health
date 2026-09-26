# Forensic Medicine onboarding correction — 2026-09-26

## Scope and cause

Read-only production diagnosis verified that the affected applicant's CV result
had no specialty, the saved declaration was Forensic Pathologist, and the user
profile was pathology. An unsubmitted first examination was the bundled pathology
skin-histology case. The record does not prove who supplied the declaration.
Forensic Medicine / Legal Medicine was missing from both the recognition
vocabulary and the 43-specialty curriculum. Substring normalization reduced
Forensic Pathologist to Pathology. There is no generic missing-case pathology
fallback: the wrong canonical identity selected the wrong curriculum.

The supplied CV consistently documents Forensic Medicine qualification and
German specialist recognition, clinical forensic examinations, medicolegal
reports and forensic genetics. The CV itself is private reference material and
is not copied into the repository, a model prompt, or a clinical case.

## Changes and invariants

Add independent Forensic Medicine identity, English/German/Serbian aliases,
explicit author/reviewer scope, and two separate medicolegal curriculum topics.
Forensic Pathology and Forensic Genetics remain distinct. Recognized forensic titles and an immediately preceding forensic qualifier
retain their scope instead of becoming the unqualified parent. Previously confirmed declarations
remain authoritative; this patch does not mass-relabel existing physicians.

All original case JSON, images, source evidence, CV uploads, credential forms,
submitted assessments, signatures, tasks, submissions, records, earnings and
exports remain unchanged. The proposed release adds reviewed material only.
No migration, deletion, account edit, email, external upload or paid-queue
capability is part of the code change. Old drafts and completed exams are retained.
A separately authorized account correction is needed for the known saved
Forensic Pathologist declaration. Case identity must be ready before replacement.

## Stores and verification

Runtime scope: Asclepius users.tutorial_json when an unfinished draw is replaced;
existing onboarding_case_bank generation if a reviewed case is unavailable.
No schema change. Team/community/objects/encryption keys unchanged. Adding clinical
vocabulary can add a community channel under the existing synchronization policy;
it does not delete messages or change permissions.

Regression tests use populated isolated SQLite fixtures, snapshot the same dataset
before/after replacement, allow only users.tutorial_json to change, assert all old
case identities survive, preserve submitted exams, keep attempt 1, and reject stale
practice/exam writes. SQLite backup API includes committed WAL; an isolated restore
is compared to the before inventory. These fixtures are not production backup proof.
Missing/rejected clinical material must remain unavailable instead of returning a
pathology case. The full library coverage gate remains required before release.

Release status: in progress. Real case generation, independent review and final
checks are recorded in subsequent revisions. No production write or deploy performed.
