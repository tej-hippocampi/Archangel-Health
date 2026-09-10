# Synthetic physician CV corpus

100 PDFs, 10 synthetic physician profiles across 10 layout/content variants. All are invented; no physician, patient or registry lookup data is used. Each PDF has independently authored expected form values in `manifest.json` (13 fields, 1,300 comparisons). Variants cover Unicode/titled names, multiple degrees, ten specialties, one/two columns, multiple pages, late identity headers, split/inline training, lowercase dates, references, and negated certification/license claims.

`evaluate.py` extracts real PDF bytes with the production parser, executes the production TypeScript mapper and credential defaults in Node, and compares final values. Only generated row IDs are excluded from comparisons. `test_cv_upload_corpus.py` additionally sends all 100 through the actual upload, asset storage, background parse and status endpoints before executing the mapper. No fake LLM output is used.

Run from the backend directory:

```sh
python tests/cv_corpus/evaluate.py /tmp/cv-results.json
python -m pytest tests/test_cv_autofill_regressions.py tests/test_cv_upload_corpus.py -q
```

Node 24 is required for TypeScript stripping. Fixtures are committed, so generation libraries are not needed in CI. Regenerate with `python tests/cv_corpus/generate.py` (ReportLab); generate the two supplementary scanned PDFs with `python tests/cv_corpus/scan_fixtures.py` (ReportLab and PyMuPDF; test tooling only).

`main-baseline.json` evaluates unchanged main `7ee60e6`: 820/1,300 fields correct. `final.json`: 1,300/1,300. Earlier `baseline.json` and `iteration-*.json` record the development loop against the older local checkout before the newer onboarding changes were fetched. They are historical, not the final comparison baseline.

`scan.pdf` and `mixed-scan.pdf` are additional image-only and text-cover-plus-scan fixtures. Real OCR tests require Tesseract and Poppler. They explicitly fail rather than skip when `CV_REQUIRE_OCR=1`; the dedicated GitHub workflow sets that flag. They are not counted as passing within the 100 digital PDFs.

This is a development regression corpus, not an independent estimate of accuracy on real CVs. It does not establish universal handwriting, multilingual OCR, arbitrary tables, every medical qualification, or every physician field. Unknown information remains unanswered. Tests outside the corpus check corruption/page limits, personal-vs-reference contact details, manual edits, reload/replacement, additional licenses and future training dates.
