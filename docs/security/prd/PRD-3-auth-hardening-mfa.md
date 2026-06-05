# PRD-3: Auth Hardening — fail-closed secrets, MFA, short sessions

## Context
`AUTH_SECRET` defaults to `"change-me-in-production-elysium"` (`auth.py:21`,
`tenant_jwt.py:15`); admin/internal secrets default to placeholders
(`routers/admin.py:_secret` uses `"change-me"`; `INTERNAL_TOOL_SECRET`).
JWTs are HS256, 7-day life (`auth.py:23`), with no refresh, no revocation, no MFA,
and no idle/auto-logoff (violates §164.312(a)(2)(iii)).

## Goal
Make it impossible to run insecurely in prod; add MFA for staff/admin; shorten and
make sessions revocable.

## Implementation
1. **Startup guard** in `main.py` startup: if `ENV=production` and any of
   {`AUTH_SECRET`, `ADMIN_PASSWORD`, `INTERNAL_TOOL_SECRET`} equals its known default
   or is < 32 chars → raise and refuse to boot with a clear remediation message.
   (Shared with PRD-2 §8 — implement in one place.)
2. **Shorten** `ACCESS_TOKEN_EXPIRE_MINUTES` for staff/admin to 60. Add refresh
   tokens: `create_refresh_token` (14-day, stored server-side in `team.db` with a
   `jti`) and `/api/auth/refresh` that rotates the refresh token (one-time use).
3. **Token revocation:** maintain a `revoked_jti` table in `team.db`; check on
   decode. Add `/api/auth/logout` that revokes the current `jti`. Add a `jti` claim
   to all tokens.
4. **MFA (TOTP)** for staff + admin (use `pyotp`):
   - Add `mfa_secret` + `mfa_enabled` to user/staff records.
   - `/api/auth/mfa/enroll` returns an `otpauth://` URI + QR; `/api/auth/mfa/verify`
     confirms and flips `mfa_enabled`.
   - On login, if `mfa_enabled`, require a second step: `/api/auth/login` returns
     `mfa_required: true` + a short-lived pre-auth token; `/api/auth/mfa/login`
     exchanges code + pre-auth token for the real session.
   - Mandatory for `role=system_admin`; optional-but-encouraged for clinical staff
     (`REQUIRE_STAFF_MFA=1` to enforce per-tenant).
5. **Idle timeout:** access tokens already short (60m). Add a frontend idle watcher
   (15 min) that calls `/api/auth/logout`. Document the 15-min convention.

## Acceptance criteria
- Booting with `ENV=production` + default `AUTH_SECRET` raises at startup.
- TOTP enroll/verify/login round-trip works (test with a known `pyotp` seed).
- Logout revokes the `jti`; a revoked token is rejected.
- Tests in `backend/tests/test_auth_hardening.py`. Add `pyotp` to `requirements.txt`.

## Out of scope
SSO/SAML (note as a future HITRUST item).
