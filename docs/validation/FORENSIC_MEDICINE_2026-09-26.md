# Forensic Medicine onboarding correction

## Observed cause

Read-only inspection of the current production deployment found:

- The affected CV parse was successful overall but its specialty status was missing.
- The saved application declared Forensic Pathologist; the profile stored pathology.
- The first exam was in progress and unsubmitted, stamped with the pathology skin-histology task.
- The production case bank had no forensic/legal-medicine entries; the shipped
  43-specialty curriculum likewise had no Forensic Medicine pair.

The immediate cause was specialty normalization: matching the word pathologist
inside Forensic Pathologist discarded its qualifier and selected the general
pathology curriculum. Missing recognition of the CV's actual field and missing
Forensic Medicine cases were separate contributing gaps. Saved records do not
establish how the incorrect form declaration was first entered.

## Corrected behavior

The actual private PDF now resolves to forensic medicine with role/training
provenance. Passing that parse through the real TypeScript autofill code yields
Forensic Medicine / Legal Medicine. The private PDF and its text are not added to
the repository or sent to clinical generation. Regression CVs are synthetic.

English, German and Serbian titles are recognized. Forensic Pathology and
Forensic Genetics retain separate identities; these changes do not grant clinical
credentials or enable paid case generation. A physician-confirmed specialty still
wins, so already incorrect declarations need a deliberate account correction.
The form explains that an international physician need not choose a US equivalent.

The proposed pair covers injury documentation/interpretation and medicolegal
review of nonfatal strangulation. Authorship and independent reviews receive the
specialty scope; pathology/histology/microscopy studies are rejected. Missing or
rejected material remains unavailable instead of selecting an unrelated case.

An unfinished wrong-specialty draw can be replaced once the correct case is
ready. Previous case IDs and drafts are retained, attempt 1 stays attempt 1,
and submitted exams remain immutable. Stale practice writes after correction
are rejected, matching the existing exam protection.

## Validation so far

- Actual PDF parsing and production TypeScript prefill verified locally.
- 35 forensic regressions passed, including national titles, nearby specialties,
  actual-layout synthetic CV, microscopy rejection, missing-case behavior, stale
  writes, replacement, idempotent draws, submitted-exam retention and restoration.
- 393 existing CV, evidence, library, specialty-routing and examination tests passed;
  two local OCR checks skipped, with OCR enforced by CI. The missing-content
  coverage gate is intentionally not counted as a pass before bundle import.
- 260 additional CV-to-case, credentialing, promotion and self-service tests passed;
  new forensic release-case parameter is not counted before bundle import.
- 133 evidence, scope, community and review-clarity tests passed; 25 onboarding and
  tutorial tests passed. Counts overlap and must not be summed as unique tests.
- 109 frontend checks passed; route table unchanged at 691 routes; no dangling
  imports across 779 files; SQL preservation and merge-readiness gates clear.
- Same populated SQLite dataset compared before/after replacement, preserving
  every identity and all fields except the expected tutorial-state transition.
  SQLite backup API and an isolated restored copy matched the original inventory.

## Independent reviews and clinical build status

Code auditor /root/audit_forensic_routing independently reran 35 forensic and
105 library tests, identified combined-title recognition and modality-normalization
issues, and confirmed those fixes. Evidence audit verified additive topic pins
and preserved prior configurations. No remaining blocking code findings for draft
preparation; actual reviewed cases remain a release gate.

First isolated real-model run 36209707471 rejected the practice case for lack of
adequate source support. Its exam passed both providers but independent artifact
auditor /root/audit_forensic_cases held it for incomplete source support and an
implausibly absolute distractor. It was not imported into the released bank.

Second run 36210185377 used the verified topic-specific references but both cases
failed all three attempts. Practice drafts added unsupported documentation and
injury-timing claims. Exam drafts failed required review metadata or confidence
gates. No artifact from this run was imported. Authoring is being narrowed to
the directly supported decisions, without weakening any review gate. No automated
review, source pin or independent agent review is represented as physician
ratification. Final accepted artifacts and full coverage checks are recorded
below when completed.

## Production status

PR179 is a draft. No production account write, merge, deploy or email has occurred.
A scoped correction plan is prepared for the affected account: preserve the
original declaration in an audit record, correct its current specialty after
release, and let the normal draw path replace the unsubmitted exam without
consuming an attempt. Re-read the account before applying; abort on concurrent
changes or a newly submitted assessment.
