# AGENTS.md

## Cursor Cloud specific instructions

### Architecture
CareGuide is a single-service Python FastAPI app (backend) serving a static HTML/CSS/JS frontend. No database, no build step, no monorepo. See `README.md`.

### Running the dev server
```
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
The demo patient dashboard is available at `http://localhost:8000/patient/maria_001` (seeded in-memory at startup).

### Environment variables
Copy `.env.example` to `.env`. Set `BASE_URL=http://localhost:8000` for local dev. External API keys (Anthropic, ElevenLabs, Tavus, Twilio) are optional for basic UI testing — the app gracefully degrades without them. Chat requires `ANTHROPIC_API_KEY` for real AI responses.

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
| `/api/avatar/chat` | POST | AI chat (requires `ANTHROPIC_API_KEY`) |
| `/api/process-patient` | POST | Full EHR pipeline |
| `/docs` | GET | Swagger UI |
