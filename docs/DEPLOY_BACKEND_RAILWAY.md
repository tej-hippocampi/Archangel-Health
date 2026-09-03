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

## 4. Attach a volume — REQUIRED, or every deploy wipes your data

> **Checking this afterwards:** the admin console shows a banner on every tab
> when any store is not on the volume, or when `ENV=production` is not set to
> keep it that way. `docs/asclepius/IS_MY_DATA_SAFE.md` is the short runbook.


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
| `ASCLEPIUS_EXPORT_DIR` | `/data/asclepius-exports` | Built export bundles. Without it they land in `/tmp`; history still lists the batch and its download becomes an empty archive — including for a buyer following the link we emailed |

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

## 4b. Turn the community routine on

The community subsystem ships fully built and entirely off. Every gate defaults
to disabled and every way it switches itself off is silent, so a deployment that
skips this section looks exactly like a community with nothing to say.

**a. Set three variables.** Service → **Variables**:

| Variable | Value | What is broken without it |
|----------|-------|---------------------------|
| `COMMUNITY_MORNING_ENABLED` | `1` | No morning brief in any channel, and no per-doctor 7am email (the newsletter has no flag of its own, it rides this one) |
| `COMMUNITY_NEWS_ENABLED` | `1` | No `#medical-ai-news` digest and no daily staff spotlight |
| `COMMUNITY_DB_PATH` | `/data/community.db` | **The named silent failure.** The default path is inside the container, so every deploy resets the dedup ledger and the run history, and the digest reposts what it already posted |

Optional, all defaulting sensibly: `COMMUNITY_MORNING_HOUR_LOCAL` (7),
`ARCHANGEL_HOME_TZ`, `COMMUNITY_COUNTRY_MIN_MEMBERS` /
`COMMUNITY_SUBSPECIALTY_MIN_MEMBERS` / `COMMUNITY_CITY_MIN_MEMBERS` (3 each),
`COMMUNITY_SPECIALTY_REGION_MIN_MEMBERS` (5), `COMMUNITY_DISCUSSION_DOW`
(2 = Wednesday). See `.env.example` for the rest.

**Two things a person supplies, both optional, both silent until they do.**

| What | How | Until then |
|------|-----|------------|
| The Archangel account's picture | Commit a PNG or JPEG at `backend/assets/community-persona.png`, or set `COMMUNITY_PERSONA_AVATAR` to a path on the volume | Bot posts render as the "AH" initials, exactly as they always have |
| The weekend webinar | Set `COMMUNITY_WEBINAR_URL` to the join link | No recurring event is created at all; the startup log says so |

The webinar link is deliberately the only switch: an event with a time and
nowhere to go is worse than no event. Everything else about the series
(`COMMUNITY_WEBINAR_TITLE`, `_DOW`, `_HOUR_LOCAL`, `_WEEKS_AHEAD`, `_HOST`) has
a working default. `POST /internal/community/run-webinars` tops the series up on
demand after changing any of them; it also runs on the morning tick.

**b. Install the scheduler.** The hourly trigger is a GitHub Actions workflow.
Copy `docs/asclepius/community-morning.workflow.yml` to
`.github/workflows/community-morning.yml` and add the two repository secrets it
names: `MORNING_BASE_URL` (your Railway URL) and `INTERNAL_TOOL_SECRET` (the
same value this backend has). The in-process loop is the fallback for a deploy
without the cron; both share the run ledger and cannot double-post.

**c. Verify, do not assume.** `GET /internal/community/status` with the
`Authorization: Bearer $INTERNAL_TOOL_SECRET` header reports gates, loops, last
runs and email transport, and returns booleans and timestamps only, never a
secret's value. Live means:

- both gates `true` and both `loop_running` `true`;
- `dependencies.community_db_path_explicit` is `true`;
- one manual `POST /internal/community/run-morning` posts briefs;
- the next scheduled hour posts nothing extra (the ledger holds).

`dependencies.anthropic_api_key_set: false` is the other quiet one: without a
model key the morning routine records a *successful* run with zero items, which
is indistinguishable from a quiet day.

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
