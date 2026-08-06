# Architecture

| | |
|---|---|
| **What this is** | How the pieces of this repo fit together at runtime, one level below the map in `README.md`. |
| **What it is NOT** | Not an endpoint reference (see `AGENTS.md` "Key endpoints" and the root `README.md`), not a spec (see `docs/prd/`). |
| **Read after** | `README.md` in this folder. |

## Runtime shape

One backend service does almost everything. `backend/main.py` builds a single FastAPI app that:

1. **Serves the JSON API** under `/api/...` and various portal routes.
2. **Serves the static frontend** by mounting `frontend/` at `/static` (see the `app.mount(...StaticFiles...)` calls near the end of `main.py`). HTML pages like `/patient/{id}` and `/doctor/app` are returned by the backend directly.
3. **Seeds demo data in memory at startup** (for example patient `maria_001`). There is no database; all patient data resets when the process restarts.

The **landing site** (`landing/`) is a separate React (Vite) app deployed on Vercel. It is not served by the backend. It calls the backend over `/api/auth/*` for sign-in / sign-up using JWT, and proxies `/api` to the backend in dev. CORS for the deployed landing is controlled in `backend/http_security.py` (`ALLOWED_ORIGINS` plus a baked-in regex for `archangelhealth.ai`).

```
  Request → main.py (app) → include_router(...) → routers/<domain>.py
                                                        │
                                                        ▼
                                        domain package (triage/, eligibility/,
                                        pipeline/, asclepius/, community/, gold/)
                                                        │
                                                        ▼
                                        integrations/ (ElevenLabs, Tavus, Twilio, STT, video)
                                        ai/llm_client.py + prompts/ (Anthropic calls)
```

Note: `backend/main.py` is a large monolith. Many endpoints are defined inline in `main.py`; newer feature areas are split into `backend/routers/*.py` and mounted via `app.include_router(...)`. When looking for an endpoint, check both `main.py` and the matching router.

## Major domains

| Domain | Package | What it does |
|---|---|---|
| EHR pipeline | `backend/pipeline/` | Turns raw EHR text into a grounded patient script: `ingest.py` → `extract.py` → `classify.py` → `generate.py`, with `grounding_check.py` / `grounding_gate.py` guarding hallucination. Streaming variants in `streaming.py`. |
| Risk triage | `backend/triage/` | ACS-NSQIP-anchored surgical risk tiering (TIER_1/2/3). `initial_tier.py` for pre-op, `intraop/` for reassessment, `postop/` for daily scoring, `preop_retier/` for re-tiering. `*_flags.py` derive clinical flags; `tuning.py` holds the tunable config. |
| TEAM eligibility | `backend/eligibility/` | Medicare TEAM determination: format detection + parsers (`parse_x12.py`, `parse_pdf.py`, `parse_csv.py`) → `extract.py` → `evaluate.py`, orchestrated by `pipeline.py`, results in `store.py`. Endpoints in `routers/eligibility.py`. |
| Asclepius eval portal | `backend/asclepius/` | LLM clinical-evaluation product: `cases.py`, `calibration.py`, `credentialing.py`, `environments/`, `buyer_profiles/`, `compensation.py`, plus buyer/provider/advisor/payments/review routers (`routers/asclepius_*.py`). |
| Community | `backend/community/` | Physician community: `feeds.py`, `events.py`, `digest.py`, `notify.py`, `system_posts.py`, `ws.py` (websockets), `phi_gate.py` (keeps PHI out of community surfaces). |
| Telehealth | `backend/telehealth/`, `integrations/video`, `integrations/stt` | Video visit setup / join, G-code capture (`telehealth/gcodes.py`). |
| Gold data | `backend/gold/` | Gold-standard data export pipeline with de-identification (`deid.py`), retention (`retention.py`), and `export.py`. |

## Cross-cutting concerns

- **Auth + tenancy:** `backend/auth.py`, `auth_roles.py`, `tenant_jwt.py`, `tenant_utils.py`, `tenant_constants.py`. Multi-tenant, JWT-based. `demo_credentials.py` seeds demo logins.
- **Security:** `backend/http_security.py` (CORS, headers, rate-limit wiring), `ratelimit.py`, `field_crypto.py` (field-level encryption), `token_revocation.py`.
- **Audit + compliance:** `backend/audit/` (`audit_log.py`, `middleware.py`) records eligibility and other sensitive actions; `backend/compliance/subprocessors.py`. The HIPAA control docs live in `docs/security/`.
- **LLM access:** `backend/ai/llm_client.py` + `backend/ai/model_config.py`, with prompts organized in `backend/prompts/` (per-domain: `diagnosis.py`, `preop.py`, `postop.py`, `eligibility.py`, `gold.py`, ...).

## Frontend

`frontend/` is static, no build step. Pages map to product surfaces: `index.html` / `app.js` (patient dashboard), `doctor.html` + `doctor-sign-in.html`, `pre-op.*` / `postop.js` / `preop-survey.*` / `intraop-form.html` (triage flows), `telehealth-*.html` (video), plus subfolders `frontend/asclepius/`, `frontend/buyer/`, `frontend/provider/`. All static assets are referenced with `/static/...` prefixes because the backend mounts the folder at `/static`.

## Landing

`landing/` is a React + Vite app (`src/app`, `src/contexts`, `src/lib`, `main.tsx`), styled with MUI + Radix + Emotion. Routes are declared as Vercel rewrites in `landing/vercel.json` (`/data`, `/physicians`, `/health-systems`, `/research`, `/onboard/:token`, `/t/:slug/sign-in`, ...). It is the marketing surface plus the sign-in / onboarding entry point; the actual product runs off the backend.

## Deploy

- **Backend:** Railway (`railway.json`, builder RAILPACK, health check `/docs`). Also runnable via `Dockerfile` / `docker-compose.yml` and `backend/Procfile`.
- **Landing:** Vercel (`landing/vercel.json`).
- **CI:** `.github/workflows/tests.yml` (backend pytest + a Chromium visual gate) and `.github/workflows/security.yml` (dependency + secret scanning). See `docs/security/` for the compliance posture.

---

*Last updated: 2026-08-06. Update this when a new backend domain package is added, when the runtime shape changes (for example a database is introduced, or the frontend gains a build step), or when the deploy target moves. Finer-grained deltas go in `repo-state.md`.*
