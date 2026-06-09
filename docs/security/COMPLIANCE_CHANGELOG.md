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
| 2026-06 | **PRD-4 — Subprocessor BAA gate + PHI de-identification** | PHI sent to vendors without a BAA (SendGrid/ElevenLabs/Tavus) | BAA rule (§164.502(e)), §164.514(b) Safe Harbor | ✅ Shipped (code gate; BAAs are a human action) |
| 2026-06 | **PRD-5 — Tamper-evident ePHI access audit log** | No audit trail of who accessed which patient's data | §164.312(b) Audit Controls, §164.316(b)(2) 6-yr retention | ✅ Shipped |
| 2026-06 | **PRD-6 — Encryption at rest for PHI** | PHI written to disk in plaintext | §164.312(a)(2)(iv); breach safe-harbor | ✅ Shipped (field encryption + volume-encryption inheritance) |
| 2026-06 | **PRD-7 — Dependency vuln fix + CI scanning** | Vulnerable `python-jose`; no automated scanning | §164.308(a)(1)(ii)(B); NPRM vuln-scan cadence | ✅ Shipped |

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

## PRD-4 — Subprocessor BAA gate + PHI de-identification

### The risk (before)
PHI flowed to third-party vendors with no check on whether a Business Associate
Agreement was in place. The recovery email put the patient's name in the body via
**SendGrid** (which is not HIPAA-eligible and Twilio will not sign a BAA for), and
full clinical voice scripts / EHR summaries were sent to **ElevenLabs** and
**Tavus**, whose BAA status is unconfirmed.

### What we changed
- `backend/compliance/subprocessors.py`: a single BAA registry with per-vendor env
  overrides (`*_BAA_SIGNED`), `phi_allowed(vendor)` / `assert_phi_allowed(vendor)`,
  and `deidentify_for_vendor()` (Safe-Harbor scrub of names, dates, email, phone,
  SSN, MBI/MRN, long ids, ZIPs).
- **ElevenLabs** and **Tavus** clients now de-identify their payloads automatically
  whenever the vendor lacks a BAA. **Email** drops the patient name from the body
  when the active transport isn't PHI-eligible (e.g. SendGrid); a startup warning
  fires in that case.
- `GET /admin/compliance/subprocessors` exposes the live register for the review
  packet; `docs/security/SUBPROCESSORS.md` documents it and the human BAA actions.

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.502(e) BAA requirement** | No PHI leaves to a vendor without a BAA flag; the register is auditable. |
| **§164.514(b) Safe Harbor de-identification** | Non-BAA vendors receive de-identified content only. |
| **HECVAT AI section / SIG Nth-party** | We can show exactly what each subprocessor receives and its BAA status. |

### Human actions (cannot be code) — see SUBPROCESSORS.md
Execute BAAs (Anthropic first-party API, Twilio SMS, and confirm ElevenLabs/Tavus);
move PHI email off SendGrid to a BAA-backed provider. Set `*_BAA_SIGNED=1` only
after signing.

## PRD-5 — Tamper-evident ePHI access audit log

### The risk (before)
The only structured audit log was in-memory, mutable, capped, and limited to the
eligibility module — wiped on restart. Access to patient dashboards, discharge
records, chat, and admin views was not recorded at all. §164.312(b) requires
recording/examining ePHI access; §164.316(b)(2) requires 6-year retention.

### What we changed
- `backend/audit/audit_log.py`: an append-only, **hash-chained** audit store
  (`row_hash = sha256(prev_hash + canonical(row))`) persisted in the team DB, with
  `verify()` to detect any tampering.
- `backend/audit/middleware.py`: a pure-ASGI middleware (innermost) that records one
  minimum-necessary event for every request to an ePHI surface — actor, action,
  resource path, patient id, outcome, IP, UA. **No request/response bodies are read**.
- `GET /admin/audit/events` and `GET /admin/audit/verify` for review + integrity
  checking. Full design in `AUDIT_LOGGING.md`.

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.312(b) Audit Controls** | Every ePHI access (success + denied) is recorded. |
| **§164.316(b)(2) 6-year retention** | Append-only, never auto-deleted; WORM-volume guidance for prod. |
| **Integrity / tamper-evidence** | Hash chain + `verify()` detect any alteration or deletion. |

### Deferred follow-ups
Migrate the eligibility in-memory audit into this store; redact remaining
PHI-bearing `print()` calls into structured logging.

## PRD-6 — Encryption at rest for PHI

### The risk (before)
PHI written to disk (the persisted patient-store snapshot) was plaintext, relying
solely on the host volume's encryption.

### What we changed
- `backend/field_crypto.py`: AES-256-GCM authenticated field encryption with a
  KMS-injected key, key **rotation** (versioned tokens + a decrypt key ring), and
  plaintext passthrough so it can roll out incrementally. Tampering is detected by
  the GCM tag.
- The persisted patient-store snapshot now encrypts its PHI fields (name, phone,
  email, voice script, battlecard, and the JSON structured_data / resources) at
  rest; a startup warning fires in production if no key is configured.
- `docs/security/ENCRYPTION.md` documents the layered model (TLS in transit +
  volume encryption for the DB + field encryption for snapshots), key management,
  and rotation; `scripts/encrypt_existing_phi.py` migrates an existing snapshot.

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.312(a)(2)(iv) Encryption at rest** | AES-256-GCM field encryption + inherited volume encryption (AES-256). |
| **Breach safe-harbor** | NIST-standard encryption renders lost encrypted data non-reportable. |

### Deferred follow-up
Extend field encryption to selected free-text PHI columns in `team.db` (or adopt
SQLCipher). Volume encryption covers the DB in the meantime — see ENCRYPTION.md.

## PRD-7 — Dependency vulnerability fix + CI scanning

### The risk (before)
The JWT library `python-jose` carried CVE-2024-33663 (algorithm confusion) and
CVE-2024-33664 (DoS), and there was no automated scanning of dependencies or the
repo for vulnerabilities/secrets.

### What we changed
- **Replaced `python-jose` with `PyJWT`** (>= 2.13.0, which also clears newer PyJWT
  advisories) across all signing/verification (`auth.py`, `tenant_jwt.py`,
  `patient_session.py`, `token_revocation.py`, `routers/admin.py`). HS256 only,
  `algorithms=["HS256"]` pinned at every decode → no algorithm confusion.
- **CI scanning** (`.github/workflows/security.yml`): `pip-audit` (dependency CVEs)
  and `gitleaks` (secrets in repo + history), plus **Dependabot** weekly dependency
  PRs. Both scanners ship non-blocking (report-only) until the initial backlog is
  triaged, then flip to blocking. `docs/security/VULN_MANAGEMENT.md` documents
  cadence + remediation SLAs. (CodeQL omitted — it needs a public repo or paid GHAS.)

### How it maps to compliance
| Control | How this satisfies it |
|---|---|
| **§164.308(a)(1)(ii)(B) Risk Management** | Continuous dependency + secret scanning with a documented remediation process. |
| **NPRM 2025 (vuln scans ≥ 6-monthly)** | We scan on every change + weekly, far exceeding the cadence. |

### Notes
- `pip-audit` ships **non-blocking** to surface (not halt on) the existing dep
  backlog; Dependabot clears it, then flip it to blocking.
- Schedule an **annual third-party penetration test** to complete the NPRM picture.

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
- Is PHI access audited + tamper-evident, retained 6 years? → **Yes** (PRD-5)
- Is PHI withheld from vendors without a BAA? → **Yes** (PRD-4)
- Is PHI encrypted at rest? → **Yes** — volume encryption + AES-256-GCM field encryption (PRD-6)
- Are dependencies/secrets scanned for vulnerabilities? → **Yes** — pip-audit + gitleaks + Dependabot (PRD-7)

## Still open (planned)

These are scoped in [`prd/`](./prd) and not yet shipped:
- **PRD-3 (phase 2)** — short-lived access tokens + refresh rotation, idle timeout, MFA enrollment UI + enforcement.
- **PRD-8** — Risk analysis, data-flow map, controls matrix, incident-response runbook.

> This changelog is updated as each PRD ships. Regulatory citations should be
> confirmed against the primary eCFR/HHS text before use in a contract.
