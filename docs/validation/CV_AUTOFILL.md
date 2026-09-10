# PDF-to-physician-fields validation

Implemented on top of main `7ee60e6`, retaining the newer upload-attempt lifecycle and stable row IDs. Parser version is `cv-parse-2`.

## Results

- 100 synthetic digital PDFs: **820 → 1,300 correct field values out of 1,300** compared with unchanged main. Complete documents: **0 → 100** across the 13 checked fields.
- Corpus extraction plus actual TypeScript mapping: about **0.8 seconds total** locally (digital PDFs only).
- Affected backend suite: **561 passed, 2 real OCR cases skipped locally** because this Mac lacks Tesseract. OCR execution is mandatory in `.github/workflows/cv-autofill.yml`.
- Frontend mapper: **11 tests passed**, covering manual values and deliberately cleared fields, reload/replacement, additional licenses, future dates and unknown board validity.
- Production frontend build passed. A local browser harness rendered the real credential component with a production parse and checked name, training institution/year, employer and license values on desktop/mobile; no JavaScript errors. This is a component check, not a live Railway deployment test.
- Data inventory: no local application database, zero tables/IDs; no schema change. No production data was modified.

## Changes

Read PDF pages independently, recognize a sustained central column gutter, and OCR low-text pages even when another page already has text. PDF processing is bounded to 30 pages; OCR image size and per-call timeouts are bounded. Existing deployment OCR binaries remain required.

Handle Unicode/titled names, labelled license numbers, negative certification/status claims, inline/split/undated training and lowercase current-employment dates. Preserve legacy scoring fields while using only explicit active-practice years for physician autofill. Labelled mobile, practice city and clinical focus now reach their correct controls.

Persist CV suggestions separately from review chips. Replacement uploads update unchanged suggestions and clear absent ones while preserving manual edits, including deliberately cleared fields, across reloads. Additional licenses appear as editable/removable rows. Board validity and other attestations remain unanswered for the doctor to confirm.

## Limits

The synthetic development corpus is not evidence of 100% accuracy on real CVs. Scanned recognition remains gated on the required CI OCR tests. Unknown or unsupported facts stay empty; registry verification and signed attestations are separate. No LLM service, deployment, or main-branch merge was performed.

## Independent audit

Auditor confirmed all reported parser and rebase regressions resolved, persisted suggestions/manual edit protection, row IDs, future training guard and additional-license removal. Final verdict: **no outstanding actionable findings**. Auditor verified 11 mapper tests and `git diff --check`; real OCR awaited CI.
