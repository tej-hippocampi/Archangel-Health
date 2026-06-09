# Vulnerability Management (PRD-7)

How we find and fix vulnerabilities in our code and dependencies.

## Fixed in this PRD
- **Removed `python-jose`** (carried **CVE-2024-33663** algorithm-confusion and
  **CVE-2024-33664** DoS) and migrated all JWT signing/verification to **PyJWT**.
  We use HS256 only and pin `algorithms=["HS256"]` at every `decode()` call, which
  forecloses algorithm-confusion attacks. Affected: `auth.py`, `tenant_jwt.py`,
  `patient_session.py`, `token_revocation.py`, `routers/admin.py`.

## Automated scanning (CI)
Workflows in `.github/workflows/security.yml`:

| Scan | Tool | When | Blocking? |
|---|---|---|---|
| Dependency CVEs | `pip-audit` | every PR/push to main + weekly | **Non-blocking initially** (reports a backlog) — see below |
| Secrets in repo/history | `gitleaks` | every PR/push to main + weekly | **Non-blocking initially** (full history) — see below |

> Both scanners ship **non-blocking on day one** so they don't halt the team on a
> pre-existing backlog (old dependency advisories) or first-run false positives
> (placeholder values matched as "secrets"). They still **run and report** on every
> change. Once you've triaged the initial findings — Dependabot clears the dep
> backlog; add real false positives to a `.gitleaks.toml` allowlist — **remove
> `continue-on-error` from `security.yml`** so new CVEs / committed secrets fail the
> build.
>
> CodeQL (static code analysis) was intentionally left out: it needs a public repo
> or paid GitHub Advanced Security. `pip-audit` + `gitleaks` + Dependabot cover the
> high-value scanning for free on a private repo. Add CodeQL later if you move to
> GitHub Enterprise.

**Dependabot** (`.github/dependabot.yml`) opens weekly PRs to bump Python deps and
GitHub Actions, so we stay ahead of newly disclosed CVEs.

## Cadence & SLAs
- **Scans:** on every change + weekly (the 2025 HIPAA Security Rule NPRM proposes
  vuln scans ≥ every 6 months and pen tests ≥ every 12 months — we far exceed the
  scan cadence; schedule an annual third-party penetration test to cover the rest).
- **Remediation targets:** Critical ≤ 7 days · High ≤ 30 days · Medium ≤ 90 days.
- **Accepting a finding:** if a flagged vuln is not exploitable in our usage,
  record the justification and add `pip-audit --ignore-vuln <id>` (or a CodeQL
  dismissal) so the build stays green with an auditable reason.

## Running scans locally
```bash
cd backend
pip install pip-audit && pip-audit          # dependency CVEs
# secrets (from repo root):
gitleaks detect --source . --redact
```
