# Deploy backend to Railway (≈5 min)

Your backend is already set up to deploy (Dockerfile in repo root). Follow these steps to get a live URL.

## 1. Open Railway

Go to **https://railway.app** and sign in with **GitHub**.

## 2. New project from repo

- Click **New Project**.
- Choose **Deploy from GitHub repo**.
- Select **tej-hippocampi/Archangel-Health** (or your repo name). Authorize if asked.
- Railway will detect the **Dockerfile** and start building. Use the **repo root** (do not set Root Directory to `backend`).

## 3. Add environment variables

In the project, open your service → **Variables** tab. Add:

| Variable | Value |
|----------|--------|
| `BASE_URL` | `https://YOUR-APP.up.railway.app` (see step 4 – replace with your real URL after first deploy) |
| `LANDING_URL` | `https://archangelhealth.ai` |
| `AUTH_SECRET` | Long random string (e.g. run `openssl rand -hex 32` in terminal and paste) |
| `ENABLE_TRIAGE_DEMO` | `1` to load TRIAGEDM demo tenant roster (set `0` to disable in production) |
| `DEMO_DOCTOR_PASSWORD` | Password for Cedar landing demo doctor account |
| `TRIAGE_DEMO_SURGEON_PASSWORD` | Password for TRIAGEDM surgeon demo account |
| `TRIAGE_DEMO_RN_PASSWORD` | Password for TRIAGEDM RN demo account |

Optional (add when you have them): `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `ANTHROPIC_API_KEY`, `CARE_TEAM_PHONE`, etc.

## 4. Get your public URL

- Open **Settings** → **Networking** → **Generate Domain**.
- Railway gives you a URL like `your-app.up.railway.app`.
- After deploy finishes, open `https://your-app.up.railway.app/docs` to confirm the API is up.

## 5. Point landing to this backend

- In **Vercel** (landing): set `VITE_API_URL` and `VITE_DASHBOARD_URL` to your Railway URL (e.g. `https://your-app.up.railway.app`).
- In Railway **Variables**: set `BASE_URL` to that same URL (e.g. `https://your-app.up.railway.app`).

## 6. (Optional) Use app.archangelhealth.ai

- In Railway: **Settings** → **Networking** → **Custom Domain** → add `app.archangelhealth.ai`.
- In **Cloudflare** (or your DNS): add CNAME `app` → `your-app.up.railway.app`.
- Then set `BASE_URL` and Vercel’s `VITE_API_URL` to `https://app.archangelhealth.ai`.

---

**Troubleshooting:** If the build fails, ensure the repo has `backend/`, `frontend/`, and `Dockerfile` at the root. No need to set a custom start command; the Dockerfile already runs uvicorn.
