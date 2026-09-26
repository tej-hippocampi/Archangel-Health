# ENV-EHR implementation status

Implemented on `feat/nephrology-ehr-sandbox`, from main `6b72423`.
The delivered scope is the M0–M6 software workflow in the existing Archangel
Health application. The input PRD's embedded M0-only kickoff is reference text;
the user's request was to build the PRD. M7 computer-use UI remains out of scope.

This is an engineering implementation with synthetic validation. It is not a
claim that real patient records, clinical grading policy, or a production release
have been approved.

## Design and delivered behavior

| Milestone | Implementation |
|---|---|
| M0 — foundation | Twelve additive realm-scoped EHR tables; optional ClinicalCase collections; deterministic mixed-format fixtures. Legacy case serialization remains byte-compatible when new fields are absent. |
| M1 — intake | Secure C-CDA parsing, text-layer PDF worksheets, optional local OCR, shared manifest patient mapping, collection merging, relative dates, and residual-identifier checks. Ambiguous identity, unreadable scans, and invalid source dates fail closed or remain explicitly unknown. |
| M2 — charts | Validated FHIR resources, quote-grounded structured worksheet extraction, medication reconciliation, immutable source provenance, encrypted reference keys, and unchanged-input rebuild detection. |
| M3 — runtime | Pre-visit slicing, synthetic identities and same-split decoys, key-audit and balance gates, frozen snapshots with isolated write overlays, 24 tools, nine calculator operations, and deterministic reset/step/state. |
| M4 — evaluation | Native tool and JSON harnesses, fake provider and scripted agents, four checkpoints, eight safety-rule families, five outcome signals, probes, versioned rubric support, and aggregate evaluation reports. |
| M5 — review | Assignment-only physician views, blinded comparison, key/rubric/safety/outcome/reference audits, independent escalation, source exclusions, expiry and quotas, append-only corrections, cached alternatives, reward recomputation, and exactly-once review earnings. Admin console supports build, compile, evaluation, review monitoring, and export. |
| M6 — delivery | Train/dev buyer packages, separate agent and authenticated replay-grader services, encrypted keys with separate key delivery, manifests and hashes, buyer attestations and provenance, Docker contexts, HTTP reset/step/state, Verifiers adapter, and forty-query HAPI comparison. |

`main.py` has only the two planned integration edits: router mounting and the
review-expiry maintenance call. Existing environment tables and implementations
are not repurposed. Existing PRDs were updated only where shared code moved their
citations.

## Test evidence

Evidence files live outside the repository under workspace `output/ehr-sandbox/`.

- Latest focused Python suite: **212 passed**, covering ingestion, charts,
  runtime, grading, workflow, delivery, and independent-audit regressions
  (`ehr-final.log`).
- Chromium UI tests: **2 passed**. The actual admin module boots and compiles;
  the physician form records its verdict before requesting later outcomes.
  Screenshots are in `screenshots/`.
- CKD-EPI tests include all sixteen published Tufts 2021 implementation vectors
  for both creatinine and creatinine–cystatin C equations. Calculator formula,
  unit, missing-input and boundary checks are also covered.
- Package tests run vendored runtime code without the application on its import
  path; reject heldout exports and missing attestations; verify manifest hashes,
  access control, replay grading, and absent plaintext keys in the agent context.
- Local data inventory: **82 original tables → 94 tables**, no missing original
  IDs or changed field hashes. Restoring the pre-change SQLite backup reproduces
  the 82-table baseline. See `EHR_DATA_SAFETY_2026-09-26.md`.

- Full application run: **8,584 passed, 3 skipped, 1 failed**. The one failure was
  an old admin-tab-count assertion (six rather than seven). It was corrected and
  the failed browser test passed on rerun. This is a full run plus a targeted
  correction, not a claim of a second uninterrupted full-suite run.
- Agent/grader Docker smoke passed, including five actions and finish; the
  private grader returned oracle reward **1.0**. HAPI returned identical IDs for
  **40/40 queries**.
- Independent auditor confirmed all reported fixes and no remaining blockers in
  the checked scope; final confirmation ran **51 focused tests**. See
  `EHR_AUDIT_2026-09-26.md`.
- Boot, route-baseline, dangling-import, PRD citation, JavaScript syntax, and CI
  shard-discovery checks passed. The route inventory contains 710 routes.

## Implementation decisions and limits

The fixtures are handwritten and explicitly synthetic, as allowed by PRD §0;
no Synthea generation or real-record provenance is claimed. Repeated worksheet
boilerplate can trip the deliberately strict eight-token leakage gate. Such
visits are excluded, with reasons; the implementation never rewrites source
records to make them pass. Integration fixtures use distinct narratives and
explicitly labeled synthetic audit waivers to exercise the downstream workflow.

The clinical note rubric is an empty, versioned placeholder pending two actual
nephrologists' approval. Until then, reports identify deterministic documentation
checks. Titration equivalence similarly requires a table approved by two
physicians; the default matcher uses the specified ±25% tolerance. Approval
identities and clinical content have not been fabricated. Label-based renal dose
rules abstain where necessary indication or regimen context is missing, instead
of inventing universal renal dose ceilings.

Outcome review is per visit, so it presents one anonymous proposed reference
plan. Disagreement review presents two anonymous plans with the same formatting
and medication-list context. An additional `dose_sample` trigger implements the
PRD's 10% P4 reference audit: disputed reference labels require independent
confirmation, and invalid probes are excluded with their rewards withdrawn.

The portable grader uses deterministic documentation checks and exported,
resolved physician grading policy. Approved LLM rubric judging is available in
the control plane; portable delivery does not silently call a model provider.
The HTTP service implements the documented reset/step/state protocol. The
Verifiers adapter targets `verifiers>=0.1.8,<0.2`; actual external SDK/client and
real-provider integration remain environment-specific checks, not claimed by
fake-provider tests.

OCR is optional locally and fails closed when unavailable; production scanned
intake requires OCRmyPDF/Tesseract. Build/evaluation jobs run in the application
process with persisted events, but do not automatically resume after a process
restart; an operator can retry from retained inputs.

## Release and clinical acceptance

Before a real-data release, complete the PRD's operations acceptance: physician
rubric authorship and calibration, review of extracted keys and dose policy,
first real-practice batch with no-op/oracle and balance checks, source-clinician
exclusions, jurisdiction/contract review, and actual provider smoke tests in the
approved CI environment. Inventory mounted stores and blobs, make off-volume
backups, and verify restore and encryption-key recovery before deployment.

The local implementation does not send email, move money, alter real records,
merge to main, or deploy production. Review earnings in tests are synthetic.
