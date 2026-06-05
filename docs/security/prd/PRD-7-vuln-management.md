# PRD-7: Vulnerability Management — fix python-jose, add scanning

## Context
`backend/requirements.txt` pins `python-jose==3.3.0` (CVE-2024-33663 algorithm
confusion; CVE-2024-33664 DoS). No dependency or code scanning in CI.

## Goal
Remove the known-vulnerable dependency and add automated scanning (the 2025 NPRM
proposes vuln scans ≥ every 6 months; SIG has a vuln-mgmt domain).

## Implementation
1. Migrate JWT usage from `python-jose` to `PyJWT` (`auth.py`, `tenant_jwt.py`,
   `routers/admin.py`, and `patient_session.py` from PRD-1). Pin `PyJWT` and
   `cryptography`. Keep HS256; ensure `algorithms=["HS256"]` is explicit on every
   decode (it already is) to prevent alg-confusion. Remove `python-jose` from
   `requirements.txt`.
2. Add `pip-audit` and run it in CI; fail the build on HIGH/CRITICAL without an
   accepted-risk note.
3. Add `.github/workflows/security.yml`: `pip-audit` + CodeQL (python) + a secret
   scan (gitleaks). Run on PR + weekly schedule.
4. Add a Dependabot config for pip + github-actions.
5. `docs/security/VULN_MANAGEMENT.md`: scan cadence (≥6 monthly), remediation SLAs
   by severity, pen-test cadence (≥annual).

## Acceptance criteria
- No `python-jose` in `requirements.txt`; all auth tests pass with `PyJWT`.
- `pip-audit` runs clean (or with documented, justified ignores).
- CI workflow present and green.
