# ENV-EHR synthetic fixtures

Every clinical value and worksheet here is hand-authored. No partner chart,
identifier, patient image, or real physician decision was used. This follows
the PRD's allowance for handwritten fixtures in §0. These are engineering
fixtures, not clinically validated evaluation tasks or Synthea-generated data.

`mixed_patients.zip` has 12 patients, 3–8 visits each: four C-CDA charts, four
text PDFs, two image-only PDFs, and two CSV/text charts. CSV companions keep
the PDF patients identifiable even when OCR is unavailable; unreadable scans
must still trigger the existing blocking incomplete-upload review. The
per-file manifest contains no global `patient_key`.

`unmapped_patients.zip` has three distinct C-CDA patients and three PDFs with
no mapping or patient prefixes. It exercises refusal to guess patient identity.

`cases/` holds the equivalent relative-time ClinicalCase fixtures for unit
tests. It is not a shortcut around front-door ingestion acceptance tests.
`fixture_manifest.json` records archive hashes, formats, and patient counts.

Regenerate using `python tests/fixtures/ehr_sandbox/generate.py` from backend
(generation only: ReportLab and Pillow). ZIP timestamps and PDF metadata are
fixed. The default suite reads committed files and does not invoke the generator.
