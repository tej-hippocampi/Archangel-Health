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
| `BASE_URL` | `https://YOUR-APP.up.railway.app` (see step 5 – replace with your real URL after first deploy) |
| `LANDING_URL` | `https://archangelhealth.ai` |
| `AUTH_SECRET` | Long random string (e.g. run `openssl rand -hex 32` in terminal and paste) |
| `ENABLE_TRIAGE_DEMO` | `1` to load TRIAGEDM demo tenant roster (set `0` to disable in production) |
| `DEMO_DOCTOR_PASSWORD` | Password for Cedar landing demo doctor account |
| `TRIAGE_DEMO_SURGEON_PASSWORD` | Password for TRIAGEDM surgeon demo account |
| `TRIAGE_DEMO_RN_PASSWORD` | Password for TRIAGEDM RN demo account |

Optional (add when you have them): `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `ANTHROPIC_API_KEY`, `CARE_TEAM_PHONE`, etc.

### Physician payouts (Stripe Connect Express)

Leave these alone until you actually want money to move. The rail ships dark:
with `ASCLEPIUS_STRIPE_ENABLED` unset or `0`, the portal shows the same
"coming soon" bank card it shows today, no Stripe call is made, and
`/api/asclepius/stripe/webhook` returns 404.

| Variable | Value |
|----------|--------|
| `ASCLEPIUS_STRIPE_ENABLED` | `0` (default). `1` only after the two steps below |
| `STRIPE_SECRET_KEY` | Restricted key with Connect + Transfers write access |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...`, from the endpoint you register in Stripe |

Before flipping the flag, two things have to happen outside this repo:

1. **Enable Connect on the Stripe account and clear KYC.** Express accounts
   cannot be created, and transfers cannot settle, until Stripe has approved
   the platform account. No deploy can do this for you.
2. **Register the webhook endpoint** at
   `POST https://YOUR-APP.up.railway.app/api/asclepius/stripe/webhook`,
   subscribed to `account.updated`, `transfer.created`, `transfer.updated`,
   `transfer.reversed`. Copy its signing secret into `STRIPE_WEBHOOK_SECRET`.

Turning the flag on with either key missing fails loudly at the first Stripe
call. That is deliberate: a payout rail that silently does nothing looks
identical to one that is working until a physician asks where their money is.

We never store a bank account number, routing number, SSN, EIN or TIN. Stripe
collects tax identity during Express onboarding and files the 1099-NECs; this
codebase keeps the connected account id and a status string, and a test greps
the tree to keep it that way.

## 4. Attach a volume — REQUIRED, or every deploy wipes your data

**Skip this and the app still works perfectly, right up until your first
redeploy erases every account, patient episode, audit record and in-flight
physician signup.** Nothing errors. The screens just go quiet, which reads like
"nobody signed up" rather than "the database was deleted." This section exists
because that is exactly what happened.

Railway gives a container a fresh filesystem on every deploy. Both SQLite
databases default to living beside the code, so both are inside that filesystem
unless you point them somewhere else.

**a. Create the volume.** Service → **Settings** → **Volumes** → **New Volume**.
Mount path: `/data`. (Any path works except `/app`, which is the code.)

**b. Point the storage variables into it.** Service → **Variables**:

| Variable | Value | What is lost without it |
|----------|-------|-------------------------|
| `TEAM_DB_PATH` | `/data/team.db` | Every patient episode, intake form, care-team message, audit sign-in record, health system, and physician mid-onboarding (Admin › Physicians › **Signups**) |
| `ASCLEPIUS_DB_PATH` | `/data/asclepius.db` | Every physician account, task, submission, review and payout row |
| `ASCLEPIUS_DATA_DIR` | `/data` | V4 case images (asset store lands at `/data/assets`) |

The raw-ingest directory needs no variable: it defaults to sitting beside
`ASCLEPIUS_DB_PATH`, so setting that puts partner uploads on the volume too.

**c. Redeploy and read the log.** Two lines tell you whether it worked:

```
[storage] all three stores durable
[storage] tenant database durable (/data/team.db)
```

Anything else names the store and the variable to set. `ERROR ... is on
EPHEMERAL storage` or `... is not set` means data is still being destroyed on
each deploy.

**d. Only once those are green, set `ENV=production`.** That flips the boot gate
from warning to fail-closed: the app refuses to start on non-durable storage
instead of quietly accepting data it will lose. Set it before the paths are
correct and the service will not boot.

> **Changing these variables triggers a redeploy, and the redeploy is what
> destroys the current container's data.** Anything already written to the old
> ephemeral database is gone at that moment — the new container starts with an
> empty database on the volume. There is no migration step, because there is
> nothing durable to migrate from. If the current data matters, copy it out
> first (`railway ssh` into the running service and read
> `/app/backend/team.db`) before you save the variables.

## 5. Get your public URL

- Open **Settings** → **Networking** → **Generate Domain**.
- Railway gives you a URL like `your-app.up.railway.app`.
- After deploy finishes, open `https://your-app.up.railway.app/docs` to confirm the API is up.

## 6. Point landing to this backend

- In **Vercel** (landing): set `VITE_API_URL` and `VITE_DASHBOARD_URL` to your Railway URL (e.g. `https://your-app.up.railway.app`).
- In Railway **Variables**: set `BASE_URL` to that same URL (e.g. `https://your-app.up.railway.app`).

## 7. (Optional) Use app.archangelhealth.ai

- In Railway: **Settings** → **Networking** → **Custom Domain** → add `app.archangelhealth.ai`.
- In **Cloudflare** (or your DNS): add CNAME `app` → `your-app.up.railway.app`.
- Then set `BASE_URL` and Vercel’s `VITE_API_URL` to `https://app.archangelhealth.ai`.

---

**Troubleshooting:** If the build fails, ensure the repo has `backend/`, `frontend/`, and `Dockerfile` at the root. No need to set a custom start command; the Dockerfile already runs uvicorn.
