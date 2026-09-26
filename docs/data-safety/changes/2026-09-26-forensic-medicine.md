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
exports remain unchanged. The release bundle adds two exact independently audited case documents only.
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

Release preparation: two-provider clinical build 36211253261 passed both cases on
the first attempt after rejected earlier drafts were excluded. Independent
artifact auditor /root/audit_forensic_cases cleared the exact pair; report and
hashes are retained in onboarding_material/audits/audit-forensic-medicine-2026-09-26.json.
All 88 curriculum cases validate. The 86 existing case files and 201 checked
case/asset/CV-fixture files match origin/main 6b72423037d00d13b5aa036be4a392950ceeaf03
byte-for-byte. All prior source pins and authoring feedback remain unchanged;
only two new topic configurations and feedback entries are added.

Deployment/account repair remains separate from these isolated checks. Live
backup/restore and scoped account preconditions must be verified at that boundary.
No production write or deploy performed. Full results are recorded in
docs/validation/FORENSIC_MEDICINE_2026-09-26.md.

Final premerge hardening extends the study scope check from modality to visible
label, findings and impression. No case document or stored record is rewritten.
Twelve added regressions and the 272-test targeted suite pass; independent review
confirmed restricted tissue studies are rejected and legitimate clinical/genetic
record review remains allowed.

The final absence-wording correction masks only explicit statements that a test
was not performed/supplied. Actual tissue results remain excluded, including
negative results and mixed text. Twenty-three further regression parameters and
the 295-test targeted suite pass. No clinical artifact or stored data is changed.

Final marker coverage includes biopsy sections/slides, immunohistochemistry and
cytopathology, preserving explicit absence handling and ordinary forensic DNA
specimen reports. The final targeted run passes 309 tests with no case-file or
stored-data changes.

Adjective test names now receive the same explicit-absence handling as their noun
forms. Twenty-four paired regression cases and the 333-test targeted suite pass;
independent review checked 1,152 boundary combinations. Both clinical documents
remain unchanged and runtime-valid. No stored data is changed by this correction.

Residual biopsy/tissue results naming a tissue diagnosis are now rejected even
without repeated microscopy terms. Same-clause result matching preserves DNA,
unrelated history and ordinary injury wording. The exact suite passes 338 tests;
independent review passes 252 further probes. No artifact or stored data changes.
