# Security & Compliance Changelog

A running, plain-language record of the security changes we ship, what risk each
one closes, and which hospital-review / HIPAA control it maps to. This is the
document to hand a hospital security reviewer (or our own compliance lead) to show
**what we changed and why it moves us toward compliance**.

How to read the control column:
- **§164.3xx** = HIPAA Security Rule citation.
- **HECVAT / SIG** = the vendor security questionnaires hospitals send us.
- **NPRM 2025** = the proposed HIPAA Security Rule update we're designing ahead of.

---

## Summary

| Date | Change | Risk closed | Primary controls | Status |
|---|---|---|---|---|
| 2026-06 | **PRD-1 — Patient PHI access control** | Anyone could read a patient's PHI / chat by guessing a URL | §164.312(a)(1), §164.312(d), §164.502(b) | ✅ Shipped |
| 2026-06 | **PRD-2 — HTTP security hardening** | Open CORS, no security headers, no transport hardening, no brute-force throttling | §164.312(e)(1), §164.308(a)(1)(ii)(B); NPRM MFA/scan posture | ✅ Shipped |
| 2026-06 | **PRD-3 (phase 1) — Token revocation/logout + opt-in TOTP MFA** | No way to invalidate a token; no MFA anywhere | §164.312(a)(2)(i), §164.312(d); NPRM MFA | ✅ Shipped (lifetime-shortening + MFA UI deferred) |

Everything below is on branch `claude/cool-tesla-NJ9oh`. Remaining work is tracked
in [`prd/`](./prd) (PRD-3 through PRD-8) and summarized at the end.

---

## PRD-1 — Patient PHI access control

### The risk (before)
Every patient-facing route — the dashboard, discharge instructions, battlecard,
audio, resources, the AI care-companion chat, and the pre-op intake — authorized
access through a helper that **granted access whenever no login token was
present**. Patient IDs are guessable (e.g. `maria_001`, `demo_thenuk_001`), so
anyone on the internet who guessed an ID could pull a patient's full clinical
record and talk to an AI preloaded with that record. The two access "codes" we
email patients were only used to *look up* the ID — they were never enforced on
the data itself.

This is the single finding most likely to fail a hospital review outright or
trigger a reportable breach.

### What we changed
- Added a real **patient session**: when a patient enters their health-system code
  + resource code, the server issues a one-time entry token that is exchanged for
  a secure, HttpOnly, 8-hour session cookie. Every patient data route now requires
  **either** that session (bound to the patient's own ID) **or** an authorized
  clinical staff login scoped to that patient's health system.
- Unauthorized or wrong-patient requests now return a generic **404** (so an
  attacker can't even confirm an ID exists).
- Closed the previously wide-open pre-op intake endpoints.
- Added patient logout (revokes the session) and a staged-rollback safety flag.

New/changed code: `backend/patient_session.py` (new), `backend/main.py`
(access-control helper + page entry + by-codes + logout), `frontend/app.js`
(session-expired prompt).

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.312(a)(1) Access Control** | Data is now reachable only by an authenticated principal scoped to that specific patient. |
| **§164.312(d) Person or Entity Authentication** | Patients authenticate via codes → signed session; staff via existing JWT. No anonymous path. |
| **§164.502(b) Minimum Necessary** | A patient session can only ever reach that one patient's record; staff are tenant-scoped. |
| **HECVAT "Product → authentication/authorization"** | We can now answer "Yes" with evidence. |

### How to verify (anyone can run this)
```bash
# Blocked when unauthenticated:
curl -i http://localhost:8000/api/patient/demo_thenuk_001/discharge   # -> 404
# Works through the real code flow (GET /api/patient/by-codes returns a ?k= link
# that sets the session cookie on first visit).
```
Automated proof: `backend/tests/test_patient_access_control.py` (11 tests),
including a route-walker that asserts **no** patient route returns data
unauthenticated.

---

## PRD-2 — HTTP security hardening

### The risk (before)
The API ran with a single misconfigured middleware: `CORS allow_origins=["*"]`
**with credentials** (an invalid, unsafe combination that tells browsers any
website may make credentialed calls to us). There were **no security headers**
(no HSTS, no clickjacking protection, no content-type sniffing protection, no
Content-Security-Policy), **no HTTPS enforcement or host validation**, and **no
brute-force throttling** on login / code-entry / OTP. Automated scanners that
hospitals run (e.g. Mozilla Observatory) fail this immediately.

### What we changed
- **CORS** locked to an explicit origin allowlist (the landing app + our own
  domains), driven by `ALLOWED_ORIGINS`.
- **Security headers** added to every response: `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy`, a scoped
  `Permissions-Policy`, **HSTS** in production, and a **Content-Security-Policy**
  (shipped in report-only first so we can tighten it without breaking the UI).
- **Transport hardening in production:** HTTP→HTTPS redirect + Host allowlist
  (`ALLOWED_HOSTS`), wired to work behind the Railway/Render TLS terminator.
- **Brute-force rate limiting** on the sensitive surfaces: code entry (10/min/IP),
  login (10/min), admin login (10/min), onboarding OTP (5/min), surveys (30/min).
- **Production secret guard:** the app now **refuses to boot in production** if
  `AUTH_SECRET` / `INTERNAL_TOOL_SECRET` / `ADMIN_PASSWORD` are still default or
  weak — eliminating the "shipped with the example secret" failure mode.

New/changed code: `backend/http_security.py` (new), `backend/ratelimit.py` (new),
`backend/main.py` (middleware stack + startup guard + rate-limit deps),
`backend/routers/admin.py`, `backend/routers/onboarding.py`, `Dockerfile`
(`--proxy-headers`), `.env.example`.

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.312(e)(1) Transmission Security** | HSTS (production) + the platform edge force encrypted transport; an opt-in app-level HTTPS redirect is available for operators who run with `--proxy-headers`. |
| **§164.308(a)(1)(ii)(B) Risk Management** | Headers + CORS + rate limits are documented, testable mitigations of known web risks. |
| **§164.312(a)(2)(i)/(d) Authentication** | Brute-force throttling protects credential/code/OTP entry points. |
| **NPRM 2025 posture** | Secret guard + transport encryption align with the proposed mandatory-encryption direction. |
| **HECVAT/SIG "Infrastructure / application security"** | Header, CORS, TLS, and rate-limit questions become answerable "Yes." |

### How to verify
```bash
curl -sI http://localhost:8000/ | grep -iE "x-frame-options|x-content-type|content-security-policy|referrer-policy"
```
Automated proof: `backend/tests/test_security_headers.py` (11 tests) covering
headers, HSTS-in-prod, CORS allow/deny, rate-limit 429 + Retry-After, and the
secret guard.

### Rollout notes (for the team)
- CSP ships **report-only** (`CSP_REPORT_ONLY=1`) — it reports violations but does
  not block, so there is zero UI risk today. Phase 2 (a follow-up) moves inline
  scripts to nonces and flips it to enforcing.
- HTTPS redirect + Host allowlist only activate when `ENV=production`. Local dev is
  unchanged. Production must run uvicorn with `--proxy-headers` (already set in the
  Dockerfile) and set `ALLOWED_HOSTS`.

---

## PRD-3 (phase 1) — Token revocation/logout + opt-in MFA

### The risk (before)
Staff/admin tokens were valid for their full 7-day life with **no way to revoke
them** — a leaked or shared token could not be invalidated, and "logout" only
cleared client state. There was **no MFA** option anywhere.

### What we changed
- Every staff/admin JWT now carries a unique `jti`, and a server-side revocation
  list (`backend/token_revocation.py`, persisted in the team DB) is checked on
  every token decode. Real logout endpoints revoke the presented token:
  `POST /api/auth/logout` (landing + tenant staff) and `POST /admin/auth/logout`.
  Tokens minted before this change simply have no `jti` and are unaffected.
- **Opt-in TOTP MFA** for landing/staff accounts: `POST /api/auth/mfa/enroll`
  (returns an `otpauth://` URI for any authenticator app), `/verify`, `/disable`,
  `/status`, plus a login second step — when a user has MFA enabled, `/api/auth/login`
  returns an `mfa_required` challenge instead of a token, completed via
  `/api/auth/mfa/login`. A `REQUIRE_STAFF_MFA` flag can require it org-wide.
  Default is **off**, so nothing changes until an account enrolls.

New/changed code: `backend/token_revocation.py` (new), `backend/auth.py`,
`backend/tenant_jwt.py`, `backend/routers/admin.py`, `backend/main.py`,
`backend/requirements.txt` (`pyotp`).

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.312(d) Person/Entity Authentication** | TOTP MFA is now available as a second factor; sessions are revocable. |
| **§164.312(a)(2)(i) Unique User ID** | Per-token `jti` makes each session uniquely identifiable and revocable. |
| **NPRM 2025 (MFA)** | Establishes the MFA mechanism ahead of the proposed mandate. |
| **HECVAT/SIG "authentication / MFA / session management"** | We can now answer "MFA supported" + "sessions revocable on logout." |

### How to verify
Automated proof: `backend/tests/test_auth_hardening.py` (7 tests) — landing/admin
logout revokes the token, tenant token revocation, tokens carry `jti`, and the
full MFA enroll → challenge → second-step → disable round trip.

### Deferred to PRD-3 phase 2 (needs coordinated frontend rollout)
- Shortening access-token lifetime + refresh-token rotation.
- Idle/auto-logoff (15-min) in the landing app + doctor portal.
- MFA enrollment QR UI and org-wide enforcement.
These are intentionally held back so we don't force logouts or block logins before
the frontend (landing app + 257KB doctor portal) is wired for refresh + MFA entry.

## What this unlocks for a security review

With PRD-1 + PRD-2 shipped, we can now answer "Yes, with evidence" to the
questionnaire items hospitals weight most heavily:
- Is PHI access authenticated and authorized per-user? → **Yes** (PRD-1)
- Is access least-privilege / scoped? → **Yes** (PRD-1)
- Are TLS/HSTS and standard security headers enforced? → **Yes, in production** (PRD-2)
- Is CORS restricted? → **Yes** (PRD-2)
- Are login/credential endpoints throttled against brute force? → **Yes** (PRD-2)
- Can the app start with default secrets in production? → **No, it refuses** (PRD-2)
- Can sessions be revoked / is logout real? → **Yes** (PRD-3)
- Is MFA supported for staff? → **Yes, available** (PRD-3; enforcement/UI in phase 2)

## Still open (planned)

These are scoped in [`prd/`](./prd) and not yet shipped:
- **PRD-3 (phase 2)** — short-lived access tokens + refresh rotation, idle timeout, MFA enrollment UI + enforcement.
- **PRD-4** — Subprocessor BAA gate + PHI de-identification (SendGrid/ElevenLabs/Tavus).
- **PRD-5** — Tamper-evident, persistent audit logging (6-year retention).
- **PRD-6** — Encryption at rest for PHI.
- **PRD-7** — Dependency vulnerability fixes + CI scanning.
- **PRD-8** — Risk analysis, data-flow map, controls matrix, incident-response runbook.

> This changelog is updated as each PRD ships. Regulatory citations should be
> confirmed against the primary eCFR/HHS text before use in a contract.
