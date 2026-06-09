# Compliance Roadmap

What's in place, what's in progress, and the path to the attestations hospitals
expect. Written to be honest — overclaiming fails diligence.

## Shipped (in `main`)
| Area | Status |
|---|---|
| Patient PHI access control (per-patient session, tenant isolation, no enumeration) | ✅ PRD-1 |
| HTTP hardening (TLS/HSTS, CORS, CSP, security headers, rate limiting, secret guard) | ✅ PRD-2 |
| Token revocation / real logout + opt-in TOTP MFA | ✅ PRD-3 (phase 1) |
| Subprocessor BAA gate + PHI de-identification | ✅ PRD-4 |
| Tamper-evident, hash-chained audit log (6-yr) | ✅ PRD-5 |
| Encryption at rest (volume + AES-256-GCM field) | ✅ PRD-6 |
| Dependency/secret scanning (pip-audit, gitleaks, Dependabot) | ✅ PRD-7 |
| Security documentation set (this folder) | ✅ PRD-8 |

## In progress / near-term
- **Execute BAAs** (Anthropic first-party API, Twilio SMS, ElevenLabs, Tavus,
  Daily.co, Railway); move PHI email off SendGrid to a BAA-backed provider.
- **MFA enforcement + short-lived/refresh sessions + idle timeout** (PRD-3 phase 2).
- **Dependency backlog** to zero, then make CI scans blocking.
- **Backup & disaster recovery** plan + contingency testing (§164.308(a)(7)).
- **Administrative safeguards**: sanction policy, workforce security-awareness
  training, documented periodic evaluation (§164.308(a)(8)).
- **Annual third-party penetration test**; quarterly vulnerability scans.

## Attestations (target sequence)
1. **SOC 2 Type II** — the baseline hospitals expect. Establish the control set
   (largely done via PRD-1–8), pick auditor, run the 3–6 month observation window.
   *Timeline driver: the observation window — start the clock early.*
2. **HITRUST CSF (i1 → r2)** — the healthcare-specific certification that unlocks
   larger health systems. The SOC 2 control work is the foundation.
3. **ISO 27001** — optional, if expanding internationally.

> SOC 2 / HITRUST are **planned, not yet certified.** Current posture: a signed BAA
> + a completed HECVAT/SIG (we can answer from this packet) + the controls below is
> the credible package to begin enterprise hospital sales; SOC 2 Type II is the
> next unlock.

## NPRM 2025 readiness
The proposed HIPAA Security Rule update (mandatory encryption, MFA, 6-month vuln
scans, 12-month pen tests, asset inventory + network map) is largely pre-addressed
here; remaining items (MFA enforcement, pen test, asset inventory) are in the
near-term list above.
