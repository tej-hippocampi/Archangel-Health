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

### Gotchas
- **Static file paths**: `frontend/index.html` uses `/static/` prefixed paths. FastAPI mounts the `frontend/` directory at `/static`. If the HTML is served at `/patient/{id}`, relative paths won't resolve — always use `/static/styles.css` and `/static/app.js`.
- **No tests or linter**: The codebase has no test suite or linting configuration (`pyproject.toml`, `.flake8`, etc.).
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
| `/docs` | GET | Swagger UI |
