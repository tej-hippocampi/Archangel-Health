# Archangel-Health

CareGuide - Patient Surgical Video Platform

## Running locally

```bash
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Doctor sign-in is at <http://localhost:8000/doctor/sign-in> (or open the site root — it redirects there). After login, the roster is at <http://localhost:8000/doctor/app>. The demo patient dashboard
is at <http://localhost:8000/patient/maria_001>, and the FastAPI Swagger UI is at
<http://localhost:8000/docs>.

## TEAM eligibility (Track A)

The doctor portal includes a CMS Transforming Episode Accountability Model (TEAM)
eligibility determination flow that runs Medicare eligibility documents through a
parse → extract → evaluate pipeline and emits live SSE progress.

### What's needed

- `ANTHROPIC_API_KEY` in `backend/.env` — required for live extraction.
- `tesseract` binary on the host for OCR fallback on image-only PDFs:

  ```bash
  brew install tesseract poppler
  ```

  Without `tesseract` (or `poppler`, used by `pdf2image` to rasterize PDF pages),
  the parser still works for text-extractable PDFs; image-only PDFs surface a
  graceful "OCR unavailable" message.
- `UPLOAD_DIR` env var (defaults to `/tmp/elysium-eligibility`). Uploaded
  documents land under `<UPLOAD_DIR>/eligibility/{patientId}/{uuid}.{ext}` with
  mode `0o600`.

### Endpoints (PRD §8)

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/api/eligibility-draft-patient`              | Allocate a draft patient row before file upload |
| `POST`   | `/api/eligibility-documents`                  | Multipart upload (X12 271, PDF, CSV) |
| `DELETE` | `/api/eligibility-documents/{id}`             | Hard-delete an uploaded document |
| `POST`   | `/api/eligibility-checks`                     | Start the parse → extract → evaluate pipeline |
| `GET`    | `/api/eligibility-checks/{id}`                | Full check record |
| `GET`    | `/api/eligibility-checks/{id}/stream`         | SSE progress (event: status / result / error) |
| `POST`   | `/api/eligibility-checks/{id}/override`       | Override a single field with an audited reason |
| `POST`   | `/api/eligibility-checks/{id}/rerun`          | Re-run extraction (preserves overrides) |
| `POST`   | `/api/eligibility-checks/{id}/finalize`       | `SAVE_AS_TEAM` or `SAVE_AS_STANDARD` |
| `POST`   | `/api/eligibility-batches`                    | Group / bundled upload with identity fan-out |
| `GET`    | `/api/eligibility-batches/{id}`               | Batch summary |
| `GET`    | `/api/eligibility-batches/{id}/stream`        | SSE for live batch progress |
| `GET`    | `/admin/audit/eligibility?limit=500`          | Audit log viewer (requires Bearer token) |

The doctor portal also exposes companion notes endpoints used by the Track B
detail-view redesign:

| Method | Path |
|---|---|
| `GET`  | `/api/patient/{id}/postop-notes` |
| `POST` | `/api/patient/{id}/postop-notes/confirm` |
| `GET`  | `/api/patient/{id}/preop-notes` |
| `POST` | `/api/patient/{id}/preop-notes/confirm` |

### File-format limits (PRD §4.2.2)

| Format | Max size | Detected via |
|---|---|---|
| X12 271 | 5 MB    | `ISA*` envelope prefix or `.x12/.271/.edi` ext |
| PDF     | 25 MB   | `%PDF` magic bytes |
| CSV     | 10 MB   | `.csv/.tsv` ext + delimiter sniffing |
| Other   | 25 MB   | fallback (any text-extractable format) |

Password-protected PDFs are rejected at upload with HTTP 422.

### Running tests

```bash
cd backend && python3 -m pytest tests/ -q
```

The `tests/test_eligibility_validation_set.py` module provides 50 deterministic
fixtures covering the 6 TEAM checks (Part A, Part B, MA, MSP, ESRD, UMWA) and
edge cases (term-equals-surgery, mixed unknowns, etc.).
