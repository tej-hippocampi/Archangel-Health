# Security & HIPAA Compliance — Engineering Package

This folder is the engineering-facing security/compliance plan for Archangel
Health (CareGuide). It exists so we can **speedrun any hospital security review**:
each gap a reviewer will probe is mapped to a concrete, buildable remediation with
a ready-to-implement PRD under [`prd/`](./prd).

> Status: planning / remediation in progress. Nothing here asserts that the
> product is currently compliant — it is the roadmap to get there.

## How hospitals assess vendors (what we're optimizing for)

A hospital security review is a checklist, typically some mix of:

- **HECVAT** — unified ~321-question instrument (v4.1.5, Feb 2025) with a dedicated
  **AI section** that asks how our Anthropic/Tavus/ElevenLabs features can be
  disabled, monitored, and prevented from ingesting PHI into models.
- **SIG / SIG Lite** — ~126–855 questions across ~21 control domains.
- **HIPAA Security Rule technical safeguards (45 CFR §164.312)** — access control,
  audit controls, integrity, authentication, transmission security.
- **BAA** with each customer (flows down to our subprocessors) + **SOC 2 Type II**
  (baseline) and **HITRUST CSF** (enterprise unlock).
- **2025 Security Rule NPRM** — makes encryption-at-rest, MFA, 6-month vuln scans,
  and 12-month pen tests mandatory. We design to this now.

## Gap → control → PRD map

| # | Gap (verified in code) | Control it fails | PRD |
|---|---|---|---|
| P0-1 | Unauthenticated access to patient PHI (`_assert_staff_can_access_patient` returns early when `staff is None`) | §164.312(a)/(d), §164.502(b); HECVAT Product/auth | [PRD-1](./prd/PRD-1-patient-access-control.md) |
| P0-2 | `CORS allow_origins=["*"]` + credentials; no security headers / HTTPS / host allowlist / rate limiting | §164.312(e); HECVAT/SIG app-security | [PRD-2](./prd/PRD-2-http-security-hardening.md) |
| P0-3 | Default secrets; no MFA; 7-day JWT, no revocation, no idle timeout | §164.312(a)(2)(iii)/(d); NPRM MFA | [PRD-3](./prd/PRD-3-auth-hardening-mfa.md) |
| P0-4 | PHI through SendGrid (no BAA); ElevenLabs/Tavus BAA unconfirmed | BAA / Breach Rule; HECVAT AI/privacy | [PRD-4](./prd/PRD-4-subprocessor-baa-gate.md) |
| P1-5 | Audit log in-memory, mutable, capped, eligibility-only; PHI in app logs | §164.312(b), §164.316(b)(2) | [PRD-5](./prd/PRD-5-audit-logging.md) |
| P1-6 | PHI persisted as plaintext JSON/SQLite; no encryption at rest | §164.312(a)(2)(iv); NPRM | [PRD-6](./prd/PRD-6-encryption-at-rest.md) |
| P1-7 | `python-jose==3.3.0` (CVE-2024-33663); no dependency/code scanning | NPRM vuln scans; SIG vuln-mgmt | [PRD-7](./prd/PRD-7-vuln-management.md) |
| P2-8 | No risk analysis, data-flow map, controls matrix, IR runbook, policies | §164.308(a)(1); HECVAT/SIG org | [PRD-8](./prd/PRD-8-compliance-docs.md) |

## Suggested sequencing

| Wave | PRDs | Rationale |
|---|---|---|
| 1 (now) | PRD-1, PRD-2, PRD-3 | Stops active PHI exposure; passes automated scans + auth questions |
| 2 | PRD-4, PRD-5, PRD-7 | Closes BAA/subprocessor + audit + dependency findings |
| 3 | PRD-6, PRD-8 | Encryption-at-rest depth + the documentation/evidence package |

## Human (non-code) actions

- Sign BAAs: **Anthropic** (first-party Claude API only), **Twilio** (SMS addendum),
  and get written BAA confirmation from **ElevenLabs** and **Tavus**.
- **Stop sending PHI through SendGrid** — Twilio will not sign a BAA for it. Move
  PHI email to a BAA-backed provider (Paubox/LuxSci); PRD-4 de-identifies in the
  meantime.
- Start **SOC 2 Type II** now (the observation-window clock is the gating factor);
  plan **HITRUST** as the enterprise unlock.

## Source for the framework claims

The framework/regulatory research backing this package (HECVAT v4, SIG, §164.312,
the 2025 NPRM, SOC 2/HITRUST, BAA & breach timelines, NIST SP 800-66r2) is
summarized with citations in the security review thread. Confirm primary
regulatory text against eCFR/HHS before relying on it contractually.
