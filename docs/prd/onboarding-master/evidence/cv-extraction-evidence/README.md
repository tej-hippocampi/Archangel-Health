# Reproduce the CV investigation

Run from the repository root; set ARCHANGEL_REPO to use another checkout. Use Node 24 for probe_frontend.cjs and Python 3 with the project parser dependencies plus reportlab and Pillow for PDF fixtures.

- `python3 docs/prd/cv-extraction-evidence/probe_extraction.py`
- `node docs/prd/cv-extraction-evidence/probe_frontend.cjs`
- `python3 docs/prd/cv-extraction-evidence/probe_worker.py`

These diagnostic scripts write results beside themselves, replacing the packaged observations when rerun. Preserve the original evidence before rerunning. They test local functions, generated documents, and an in-memory worker store; no production account is needed. A completed script is not a passing acceptance suite: inspect each JSON assertion.

The Mike Blum CV is synthetic. Expected_Extraction describes the observed baseline parser output, including current limitations; the PRD defines improved expectations. Source hashes identify the investigated snapshot. Text parsing is date-sensitive, so years inferred from dates can change on a later run. The image-only PDF failed locally because OCR was unavailable; deployment OCR health is unverified. The mixed-content defect also has independent source evidence.

live_observation.json contains the previously observed business fields and UI comparison with onboarding tokens omitted. No production writes or race tests were performed.
