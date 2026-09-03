# Deploy CareGuide to archangelhealth.ai

This guide gets the full product (landing page + backend + patient dashboard) live on **archangelhealth.ai** in production.

## Recommended architecture

| URL | What runs there |
|-----|------------------|
| **archangelhealth.ai** | Landing page (marketing, Sign in, Sign up, recovery code entry) — e.g. Vercel or Netlify |
| **app.archangelhealth.ai** | Backend (FastAPI: doctor dashboard, patient dashboards, API) — e.g. Railway, Render, or a VPS |

Same-origin auth works when landing and API are on different domains: the landing uses `VITE_API_URL` to call the API and stores the JWT; CORS is already configured.

---

## Part 1: Deploy the backend (app.archangelhealth.ai)

The backend serves the **doctor dashboard**, **patient dashboards**, and all **API** routes. Deploy it so it’s reachable at `https://app.archangelhealth.ai`.

### Option A: Railway / Render / Fly.io (PaaS)

**Using the repo Dockerfile (recommended)** — backend + frontend are included:

1. **Create a new project** and connect this repo. Use the **repo root** as the project root.
2. **Build**: Docker (use the repo’s `Dockerfile`). Railway/Render will detect the Dockerfile and build it. Ensure `frontend/` and `backend/` are in the repo.
3. **Start**: The image runs `uvicorn` on port 8000. If the host injects `PORT`, set the start command to use it, e.g.  
   `python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`  
   (or the PaaS may override the port automatically.)
4. **Environment variables** (in the PaaS dashboard): see **Backend env vars** below. Add them to the service (not the Dockerfile).
5. **Custom domain**: In the PaaS, add `app.archangelhealth.ai` and point your DNS (CNAME for `app`) to the host’s URL.

**Without Docker** (e.g. “Native” Python on Railway):

1. Set **root directory** to `backend`. Add a build step that copies `../frontend` into the deploy (e.g. from repo root: `cp -r frontend backend/../frontend`).
2. **Start**: `python3 -m uvicorn main:app --host 0.0.0.0 --port $PORT`.
3. Set env vars and custom domain as above.

### Option B: VPS (Ubuntu) with Docker

1. SSH into the server and clone the repo (or copy the project with `backend`, `frontend`, `Dockerfile`, and `docker-compose.yml`).
2. Create `backend/.env` with production values (see **Backend env vars**).
3. From the repo root run: `docker compose up -d`.
4. Put a reverse proxy (e.g. Caddy or Nginx) in front with TLS for `app.archangelhealth.ai`, or use a Cloudflare Tunnel.

---

## Part 2: Deploy the landing page (archangelhealth.ai)

1. **Connect the repo** to Vercel or Netlify (or similar).
2. **Root directory**: `landing`.
3. **Build**:
   - **Build command**: `npm run build` (or `npm ci && npm run build`).
   - **Output directory**: `dist`.
4. **Environment variables** (in the host’s dashboard):
   - `VITE_API_URL` = `https://app.archangelhealth.ai`
   - (Optional) `VITE_DASHBOARD_URL` = `https://app.archangelhealth.ai` (for “Dashboard” after sign-in).
   - (Optional) `VITE_CALENDLY_URL`: booking link for `/partner`. Unset keeps the partner-call account the build ships with.
   - (Optional) `VITE_CALENDLY_TEAM_URL`: booking link for `/team-calculator`'s “Book a demo”. Deliberately separate: the two links are on **two different Calendly accounts** today. Set both to the same value to consolidate.
5. **Custom domain**: Add `archangelhealth.ai` and `www.archangelhealth.ai`; follow the host’s DNS instructions (usually CNAME for `www`, and A or CNAME for root as they specify).

---

## Part 3: DNS (Cloudflare)

You already have DNS for archangelhealth.ai (see `docs/CLOUDFLARE_IMPORT_INSTRUCTIONS_archangelhealth.ai.md`). Add:

1. **Landing (root)**  
   - Ensure **A** (or **CNAME**) for `archangelhealth.ai` points to your **landing** host (Vercel/Netlify).  
   - Replace any placeholder like `192.0.2.1` with the value the landing host gives you.

2. **Backend (subdomain)**  
   - **CNAME**: `app` → target from your backend host (e.g. `your-app.railway.app` or `your-app.onrender.com`).  
   - Or **A** if the host gives you an IP.

3. **SendGrid records**  
   - Keep existing SendGrid records; set them to **DNS only** (grey cloud) as in the import instructions.

---

## Backend env vars (production)

In `backend/.env` (or the PaaS env config), set at least:

```bash
# Required for production
BASE_URL=https://app.archangelhealth.ai
LANDING_URL=https://archangelhealth.ai
AUTH_SECRET=<generate-a-long-random-string-e.g.-openssl-rand-hex-32>

# Optional but recommended
CARE_TEAM_PHONE=+1...
SENDGRID_FROM_EMAIL=noreply@archangelhealth.ai
SENDGRID_FROM_NAME=Archangel Health
# SENDGRID_API_KEY=SG....
# TWILIO_* if using SMS
# ANTHROPIC_API_KEY for chat
# ELEVENLABS_*, TAVUS_* for voice/avatar
```

- **BASE_URL**: Where patients and doctors reach the app (backend).
- **LANDING_URL**: Where the marketing/code-entry site lives (used in emails and sign-out).
- **AUTH_SECRET**: Use a long random string, e.g. `openssl rand -hex 32`. Never commit it.

---

## Checklist before go-live

- [ ] Backend is reachable at `https://app.archangelhealth.ai` (and `/docs` loads).
- [ ] Landing is reachable at `https://archangelhealth.ai` and shows Sign in / Sign up.
- [ ] Landing has `VITE_API_URL=https://app.archangelhealth.ai` and can sign in (hits `/api/auth/login`).
- [ ] `AUTH_SECRET` is set and different from the example.
- [ ] `BASE_URL` and `LANDING_URL` match the URLs above.
- [ ] SendGrid (and Twilio if used) are configured and sender/domain verified for `@archangelhealth.ai`.

---

## Optional: single-server deploy (all on one host)

If you prefer one server to serve both landing and backend:

1. Build the landing: from repo root, `cd landing && npm run build`.
2. Copy `landing/dist` into a directory the backend can serve (e.g. `frontend/landing` or a path you mount).
3. In FastAPI, mount the landing `dist` at `/` and move the current doctor dashboard to something like `/doctor` (or serve doctor at `/` and landing at `/app` — then set `LANDING_URL` to that path).
4. Run the backend (with frontend and landing assets) behind a reverse proxy with TLS for `archangelhealth.ai`.

This requires a few route changes so `/` can serve the landing SPA; the **recommended** approach is still landing on archangelhealth.ai and backend on app.archangelhealth.ai.
