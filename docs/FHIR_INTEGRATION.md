# FHIR / HL7 EHR Integration

Pulls patient data straight from an EHR's FHIR R4 server into the TEAM
eligibility pipeline, replacing manual document uploads. Feature-flagged via
`FHIR_ENABLED` (off by default — zero runtime change when disabled).

## Architecture

FHIR import is **another document source** for the existing
parse → extract → evaluate pipeline, not a parallel pipeline:

```
EHR FHIR R4 server (Epic / Cerner / HAPI sandbox)
        │  SMART Backend Services (RS384 JWT client credentials)
        ▼
backend/integrations/fhir/        config · smart_auth · client · fetch
        ▼
POST /api/fhir/import             (backend/routers/fhir_import.py)
        │  Patient + Coverage  → fhir_coverage_<id>.json  (format FHIR_JSON)
        │  DocumentReference   → attachment.pdf / .txt    (format PDF / OTHER)
        ▼
eligibility doc store (same record shape as a manual upload, + source: fhir)
        ▼
POST /api/eligibility-checks      existing pipeline, unchanged
        │  FHIR_JSON parsed by eligibility/parse_fhir.py (deterministic, no LLM)
        │  fetched PDFs reuse eligibility/parse_pdf.py
        ▼
extract → 6 TEAM checks → verdict / override / finalize / audit
```

Key modules:

| Path | Purpose |
|---|---|
| `backend/integrations/fhir/config.py` | env-driven settings + validation |
| `backend/integrations/fhir/smart_auth.py` | SMART Backend Services token client (RS384 assertion, discovery, caching) |
| `backend/integrations/fhir/client.py` | thin async R4 client: read / search+pagination / Binary, OperationOutcome-aware errors |
| `backend/integrations/fhir/fetch.py` | patient search, Patient+Coverage bundle, DocumentReference attachment resolution |
| `backend/eligibility/parse_fhir.py` | FHIR Bundle JSON → extractor text (renders every coding; no interpretation) |
| `backend/routers/fhir_import.py` | `/api/fhir/*` endpoints |

## Endpoints

All require an authenticated staff Bearer token (stricter than uploads —
anonymous/demo access never reaches an external EHR). Tenant scoping on the
local patient follows the same `_assert_patient_access` rule as eligibility.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/fhir/status?probe=1` | config validation + optional `/metadata` connectivity probe |
| GET | `/api/fhir/patients?identifier=&name=&birthdate=` | identity-only patient search on the FHIR server |
| POST | `/api/fhir/import` | `{patientId, fhirPatientId, includeDocuments}` → registers eligibility docs |

Every import writes `fhir_document_imported` / `fhir_import_completed` audit
entries (visible at `GET /admin/audit/eligibility`). The persisted coverage
bundle is the verbatim EHR response — an auditable record of what the EHR
said at import time.

## Stage 1 — Local HAPI sandbox (no PHI, no contracts)

```bash
# from repo root
docker compose -f docker-compose.fhir.yml up -d        # HAPI R4 on localhost:8090
cd backend && python3 scripts/seed_fhir_sandbox.py     # synthetic Okafor (eligible) + Brennan (MA, ineligible)
```

In `backend/.env`:

```
FHIR_ENABLED=1
FHIR_BASE_URL=http://localhost:8090/fhir
FHIR_AUTH_MODE=none
```

Smoke test (with a staff token from the doctor portal):

```bash
TOKEN=...   # staff Bearer token
curl -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/api/fhir/status?probe=1'
curl -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/api/fhir/patients?name=Okafor'
# create a draft patient, then:
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"patientId":"<draft-id>","fhirPatientId":"okafor-margaret","includeDocuments":true}' \
  http://localhost:8000/api/fhir/import
# attach the returned document ids to POST /api/eligibility-checks as usual
```

## Stage 2 — Epic sandbox

1. **Generate a key pair** (RS384; keep the private key out of the repo):
   ```bash
   openssl genrsa -out fhir_rs384_private.pem 4096
   openssl rsa -in fhir_rs384_private.pem -pubout -out fhir_rs384_public.pem
   ```
2. **Register a Backend Services app** at <https://fhir.epic.com> → *Build Apps*
   → application audience **Backend Systems**. Select the Patient, Coverage,
   DocumentReference and Binary APIs (R4). Upload the public key as a JWKS
   (or host the JWKS at a URL you control) — note the **kid**.
3. **Configure** the non-production client:
   ```
   FHIR_ENABLED=1
   FHIR_AUTH_MODE=smart_backend
   FHIR_BASE_URL=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
   FHIR_CLIENT_ID=<non-production client id>
   FHIR_PRIVATE_KEY_PATH=/secrets/fhir_rs384_private.pem
   FHIR_KEY_ID=<kid>
   ```
   The token endpoint is auto-discovered from `.well-known/smart-configuration`;
   pin it with `FHIR_TOKEN_URL` only if discovery is blocked.
4. Epic sandbox client IDs take ~30–60 min to propagate. Test against Epic's
   public sandbox patients (e.g. Camila Lopez) via `/api/fhir/patients`.
5. Expect `403 insufficient_scope` until the app's API selections match
   `FHIR_SCOPES`; Epic grants scopes from the registration, not the request.

## Stage 3 — Real-site pilot checklist

Technical:
- [ ] Production app registration approved by the health system's Epic/Cerner team (client ID issued per site)
- [ ] Private key in a secrets manager (KMS / Railway secret), rotated on a schedule; JWKS updated before rotation
- [ ] `FHIR_TIMEOUT_SECONDS` / `FHIR_MAX_SEARCH_PAGES` tuned; watch for EHR rate limits (Epic: ~10 req/s per client typical)
- [ ] `/api/fhir/status?probe=1` wired into deployment health checks

Compliance (PRD-4/-6 patterns apply):
- [ ] BAA executed with the health system covering FHIR data exchange
- [ ] Persisted FHIR documents live under `$UPLOAD_DIR` (mode 0600) — confirm volume encryption on the host; consider `DATA_ENCRYPTION_KEY` field-encryption rollout for doc payloads
- [ ] Audit review: `fhir_import_completed` events reviewed alongside eligibility overrides
- [ ] Minimum-necessary scope: keep `FHIR_SCOPES` read-only (`.rs`) and limited to the four resource types

## HL7 v2 (ADT feeds) — deliberately deferred

Real-time admit/discharge/transfer events (HL7 v2 ADT) require either a
site-by-site interface engine (Mirth/Rhapsody) or an aggregator (Redox,
Health Gorilla, 1upHealth). Revisit once a pilot site asks for event-driven
episode tracking; the FHIR import path above covers pull-based needs first.

## Tests

```bash
cd backend && python3 -m pytest tests/test_fhir_smart_auth.py tests/test_fhir_client.py \
  tests/test_fhir_parse.py tests/test_fhir_import_router.py -q
```

All FHIR tests are hermetic (httpx MockTransport / monkeypatched fetchers) —
no network, no sandbox required.
