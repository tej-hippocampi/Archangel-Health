# Archangel Health Landing

Marketing landing page for Archangel Health with **Sign in** and **Sign up**. Auth is handled by the CareGuide backend (JWT); users are stored in the backend (file-backed by default).

## Run locally

1. **Start the backend** (from repo root):
   ```bash
   cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```
   Ensure `AUTH_SECRET` is set in `backend/.env` (see `.env.example`).

2. **Start the landing** (from repo root):
   ```bash
   cd landing && npm install && npm run dev
   ```
   Opens at `http://localhost:5173`. The Vite dev server proxies `/api` to `http://localhost:8000`, so sign-in and registration work against the backend.

## Environment (optional)

- **`VITE_API_URL`** — Backend base URL when the landing is not proxied (e.g. production build on another domain). Leave unset when using the dev proxy or when the app is served from the same origin as the API.
- **`VITE_DASHBOARD_URL`** — URL for the “Dashboard” link after sign-in (e.g. doctor portal). If unset, falls back to `VITE_API_URL`.

## Build

```bash
npm run build
```
Output is in `dist/`. You can serve it statically or mount it under the FastAPI app.

**Vercel:** `vercel.json` rewrites client-only paths (`/team-calculator`, `/onboard/…`, `/t/…/sign-in`, `/email-preview`) to `index.html` so they do not 404 after deploy. Redeploy after changing `vercel.json`.
