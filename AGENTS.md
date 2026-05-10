# AGENTS.md

## Cursor Cloud specific instructions

### Architecture
CareGuide is a single-service Python FastAPI app (backend) serving a static HTML/CSS/JS frontend. No database, no build step. The **landing page** (`landing/`) is a separate React (Vite) app for Elysium Health marketing/sign-in; it uses the same backend for auth (JWT). See `README.md` and `landing/README.md`.

### Running the dev server
**Backend (required for patient dashboard and landing auth):**
```
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
**Landing (optional):** from repo root, run the backend first, then:
```
cd landing && npm install && npm run dev
```
Landing runs at `http://localhost:5173` and proxies `/api` to the backend. Sign in / Sign up use `/api/auth/login` and `/api/auth/register`. Set `AUTH_SECRET` in backend `.env` for JWT signing.

The demo patient dashboard is available at `http://localhost:8000/patient/maria_001` (seeded in-memory at startup).

### Environment variables
Copy `.env.example` to `.env`. Set `BASE_URL=http://localhost:8000` for local dev. Set `AUTH_SECRET` to a long random string for landing auth (JWT). External API keys (Anthropic, ElevenLabs, Tavus, Twilio) are optional for basic UI testing — the app gracefully degrades without them. Chat requires `ANTHROPIC_API_KEY` for real AI responses.

Health system onboarding (OTP and invite emails) requires **`SENDGRID_API_KEY`** and a **verified** `SENDGRID_FROM_EMAIL` in the same SendGrid account (or working `SMTP_*`). Without this, `/api/onboarding/request-otp` returns 503; check the backend terminal for `[email_utils] SendGrid HTTP …` diagnostics.

**TEAM eligibility (Track A)** lives in `backend/eligibility/` (parsers, extractor, evaluator, pipeline) and `backend/routers/eligibility.py`. Requires `ANTHROPIC_API_KEY` for live extraction, and `tesseract` + `poppler` (`brew install tesseract poppler`) for OCR fallback on image-only PDFs. Uploaded documents land under `$UPLOAD_DIR/eligibility/<patientId>/` (default `/tmp/elysium-eligibility`). All check / override / finalize / batch endpoints write to the in-memory audit log; view via `GET /admin/audit/eligibility`.

### Gotchas
- **Static file paths**: `frontend/index.html` uses `/static/` prefixed paths. FastAPI mounts the `frontend/` directory at `/static`. If the HTML is served at `/patient/{id}`, relative paths won't resolve — always use `/static/styles.css` and `/static/app.js`.
- **Test suite is `backend/tests/` (pytest)** — covers the eligibility evaluator, parsers, and a 50-case validation fixture set. Run with `cd backend && python3 -m pytest tests/ -q`.
- **No `python` binary**: Use `python3` (not `python`) to run commands.
- **pip installs to user dir**: `pip install` installs to `~/.local/bin`. Ensure `$HOME/.local/bin` is on `PATH`, or use `python3 -m uvicorn` instead of `uvicorn` directly.
- **In-memory data**: All patient data resets on server restart. The demo patient `maria_001` is re-seeded on every startup.

### Key endpoints
| Endpoint | Method | Description |
|---|---|---|
| `/patient/{id}` | GET | Patient dashboard (HTML) |
| `/api/patient/{id}/config` | GET | Dashboard config JSON |
| `/api/patient/{id}/battlecard` | GET | Battlecard HTML |
| `/api/patient/{id}/audio` | GET | Voice audio URL |
| `/api/digital-care-companion/chat` | POST | AI chat (requires `ANTHROPIC_API_KEY`) |
| `/api/process-patient` | POST | Full EHR pipeline |
| `/api/auth/register` | POST | Landing: create account (email, password, optional name) |
| `/api/auth/login` | POST | Landing: sign in (email, password) |
| `/api/auth/me` | GET | Landing: current user (Bearer token) |
| `/api/eligibility-draft-patient` | POST | Allocate a draft patient before file upload (TEAM) |
| `/api/eligibility-documents` | POST/DELETE | Upload / remove eligibility documents |
| `/api/eligibility-checks` | POST | Start a parse → extract → evaluate pipeline |
| `/api/eligibility-checks/{id}/stream` | GET | SSE progress (status / result / error) |
| `/api/eligibility-checks/{id}/override` | POST | Audited verdict override |
| `/api/eligibility-checks/{id}/finalize` | POST | `SAVE_AS_TEAM` / `SAVE_AS_STANDARD` |
| `/api/eligibility-batches` | POST | Group upload with identity fan-out |
| `/api/eligibility-batches/{id}/stream` | GET | SSE for batch progress |
| `/admin/audit/eligibility` | GET | TEAM audit log viewer |
| `/docs` | GET | Swagger UI |
