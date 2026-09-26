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

The pair covers interpretation of genital injury evidence and retention of
reported neurological symptoms in nonfatal-strangulation documentation. Authorship
and independent reviews receive the specialty scope; pathology/histology/microscopy studies are rejected. Missing or
rejected material remains unavailable instead of selecting an unrelated case.

An unfinished wrong-specialty draw can be replaced once the correct case is
ready. Previous case IDs and drafts are retained, attempt 1 stays attempt 1,
and submitted exams remain immutable. Stale practice writes after correction
are rejected, matching the existing exam protection.

## Validation

- Actual PDF parsing and production TypeScript prefill verified locally.
- 235 final forensic, release-library, CV-to-case and harness tests passed with no
  exclusions. This includes the complete curriculum, the released forensic pair
  and 35 new regressions for national titles, nearby specialties, microscopy
  rejection, stale writes, missing-case behavior, replacement, submitted-exam
  retention and restoration.
- 71 evidence tests passed after the source/topic refinements. Earlier focused
  suites covered existing CV parsing, credentialing, promotion, self-service,
  specialty routing, examination, community scope and tutorial behavior.
- 109 frontend checks passed. Route table unchanged at 691 routes; no dangling
  imports across 779 files. PRD line references corrected and harness audit passes.
- Same populated SQLite dataset compared before/after replacement, preserving
  every identity and all fields except the expected tutorial-state transition.
  SQLite backup API and an isolated restored copy matched the original inventory.
- Full release-library check passes all 88 entries. SQL preservation gate and
  diff check pass. Fresh final-commit CI remains required before merging.

## Independent reviews and clinical build status

Code auditor /root/audit_forensic_routing independently reran 35 forensic and
105 library tests, identified combined-title recognition and modality-normalization
issues, and confirmed those fixes. Evidence audit verified additive topic pins
and preserved prior configurations. The initial code audit found no remaining blocker for draft preparation and
held release pending the clinical artifact review documented below.

First isolated real-model run 36209707471 rejected the practice case for lack of
adequate source support. Its exam passed both providers but independent artifact
auditor /root/audit_forensic_cases held it for incomplete source support and an
implausibly absolute distractor. It was not imported into the released bank.

Second run 36210185377 used the verified topic-specific references but both cases
failed all three attempts. Practice drafts added unsupported documentation and
injury-timing claims. Exam drafts failed required review metadata or confidence
gates. No artifact from this run was imported. Authoring was narrowed to directly
supported decisions without weakening any review gate. No automated review,
source pin or independent agent review is represented as physician ratification.

Third run 36211253261 passed both cases on their first attempts using the original
two-provider evidence-review protocol. Runtime validation of both exact downloaded
documents passed. Independent auditor /root/audit_forensic_cases cleared both:
source hashes and quotations verified, two substantive references per decision,
correct keys, plausible distractors, withheld private fields, distinct decisions
and appropriate clinical scope. No material unresolved findings. The report is
`backend/asclepius/onboarding_material/audits/audit-forensic-medicine-2026-09-26.json`;
its SHA256 is `2bd48b8921c265948494992895a68d6c3bb1bbca50df8e663ddd938e42ffe3e0`.

The accepted documents were copied byte-for-byte into the release bundle.
`build_onboarding_library.py --check` passes all 88 cases / 44 specialties.
All 86 preexisting case documents and 201 checked case/asset/CV-fixture files
remain byte-identical to origin/main at `6b72423037d00d13b5aa036be4a392950ceeaf03`.

Final code/import auditor /root/audit_forensic_routing independently confirmed
both imports and all document/entry/blind hashes, the exact audit-report hash,
88/88 coverage and preservation of all 201 original files. Its final focused run
passed 36 forensic and full-coverage tests with no exclusions. No blocking
findings remain for PR readiness; final-commit CI is still required.

## Final review correction

The premerge review identified a deterministic validation gap: a study using the
generic modality clinical examination could hide microscopic tissue content in
its label, findings or impression. The guard now inspects all four fields for
specific histology, microscopy, H&E and tissue-slide markers. Generic pathology
wording in ordinary CT/neurological findings and labels remains allowed. Twelve
additional regressions cover the bypass and these legitimate record-review cases.

The final targeted forensic/library/evidence/harness run passed 272 tests with no
exclusions. Independent reviewer /root/final_merge_review confirmed the bypass is
closed, the false positives are avoided, and both unchanged clinical artifacts
still match their audit hashes and validate. Fresh CI on this correction is
required before the user-authorized merge.

A subsequent reviewer update identified absent-test wording as a false positive.
The guard now removes only explicit statements that a tissue test was not
performed or supplied before checking the remaining text. It still rejects
negative tissue results and mixed descriptions containing actual microscopy.
Twenty-three further regression parameters cover absence across descriptive
fields and negative-result/mixed-study rejection. The targeted run now passes
295 tests. Independent reviewer /root/final_merge_review checked 558 combinations
of absence wording, field placement and positive/negative tissue evidence; all
matched the intended outcomes. Clinical artifacts remain byte-identical and
runtime-valid. No remaining blocker in the patch; fresh CI still gates merge.

## Production status

Release preparation is tracked in PR179. No production account write, merge, deploy or email has occurred.
A scoped correction plan is prepared for the affected account: preserve the
original declaration in an audit record, correct its current specialty after
release, and let the normal draw path replace the unsubmitted exam without
consuming an attempt. Re-read the account before applying; abort on concurrent
changes or a newly submitted assessment.
