# Production Deployment — security configuration

This is the layman's checklist for turning on the PRD-1/2/3 security hardening in
production. It is **configuration only** — no code changes. Everything is set as
environment variables on the host (Railway, per `railway.json`).

> Railway builds with RAILPACK and starts the app via `backend/Procfile`
> (`uvicorn … --proxy-headers …`). You do **not** need to change the start command.

---

## 1. Generate your secrets (don't reuse the examples in chat)

Run this locally to mint fresh random secrets, then copy the output:

```bash
python3 -c "import secrets; \
print('AUTH_SECRET =', secrets.token_urlsafe(48)); \
print('INTERNAL_TOOL_SECRET =', secrets.token_urlsafe(36)); \
print('ADMIN_PASSWORD =', secrets.token_urlsafe(18))"
```

Treat these like passwords — never commit them, never paste them into the repo.

---

## 2. Set environment variables (Railway → backend service → Variables)

### Required

| Variable | Example value | Purpose |
|---|---|---|
| `ENV` | `production` | Enables HTTPS-only cookies, HSTS, the host allowlist, and the startup secret guard |
| `AUTH_SECRET` | *(generated, 48 chars)* | Crypto key that signs all staff + patient session tokens |
| `BASE_URL` | `https://app.archangelhealth.ai` | Your backend's public URL (used to build links) |
| `LANDING_URL` | `https://archangelhealth.ai` | Your landing site URL — patient SMS/email links route here for code entry |
| `ALLOWED_ORIGINS` | `https://archangelhealth.ai,https://app.archangelhealth.ai` | CORS allowlist so the landing app can call the API |

### Optional (only if you use these features)

| Variable | Example value | Purpose |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Admin portal login |
| `ADMIN_PASSWORD` | *(generated)* | Admin portal password. Unset = admin portal disabled (safe) |
| `INTERNAL_TOOL_SECRET` | *(generated, 36 chars)* | Internal prompt-lab tools. Unset = those tools disabled (safe) |
| `ALLOWED_HOSTS` | `app.archangelhealth.ai,archangelhealth.ai` | Extra Host-header allowlist (TrustedHost). Only enforced when `ENV=production` |
| `CSP_REPORT_ONLY` | `1` (default) | Content-Security-Policy stays in report-only until phase 2; set `0` to enforce |
| `RATE_LIMIT_ENABLED` | `1` (default) | Brute-force throttling on login / code / OTP endpoints |
| `REQUIRE_STAFF_MFA` | `0` (default) | Require staff TOTP MFA at login. Leave off until the enrollment UI ships |
| `FORCE_HTTPS_REDIRECT` | `0` (default) | App-level HTTP→HTTPS redirect. Leave **off** — HSTS + the platform edge already force HTTPS, and enabling it without `--proxy-headers` causes a redirect loop |

> Use your **real** domains for `BASE_URL` / `LANDING_URL` / `ALLOWED_ORIGINS`.
> Backend domain: Railway → Settings → Domains. Landing domain: wherever the
> landing app is hosted (Vercel/Netlify/etc.).

### The other service keys (unchanged by this work)

`ANTHROPIC_API_KEY`, `SENDGRID_API_KEY` + `SENDGRID_FROM_EMAIL`, `TWILIO_*`,
`ELEVENLABS_*`, `TAVUS_*`, `TEAM_DB_PATH`, etc. — set these as before
(see `.env.example`). Note (PRD-4, not yet built): **do not send PHI through
SendGrid** until a BAA-backed provider is in place.

---

## 3. Deploy

Railway auto-redeploys when variables change. Watch **Deployments**; it should go
green (the `/docs` healthcheck must return 200).

**If the deploy refuses to start** with a log line like
`Refusing to start in production with weak or default secrets: AUTH_SECRET` —
that's the safety guard. Set a strong `AUTH_SECRET` (≥ 32 chars, not the example
placeholder) and redeploy.

---

## 4. Smoke test (2 minutes)

1. `https://<backend-domain>/docs` → API docs load (the app booted).
2. `https://<backend-domain>/recovery` → the "Enter your access codes" page renders.
3. Direct-hit a patient dashboard while logged out, e.g.
   `https://<backend-domain>/patient/anything` → you get the friendly
   "session expired — enter your codes" page (this is the access control working).
4. From the doctor portal, send a patient their materials → the SMS/email link
   opens the code-entry page, and entering the two codes opens the dashboard.

---

## 5. What changes for users

See `COMPLIANCE_CHANGELOG.md` for the control mapping. User-visible effects:
patient sessions last 8 hours (then re-enter codes); patient links route through
the code-entry page; expired/bookmarked links show a friendly re-entry page.
Staff and admin flows are unchanged (MFA is off by default).
