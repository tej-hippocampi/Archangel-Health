# HIPAA Security Risk Analysis (§164.308(a)(1)(ii)(A))

A documented, accurate assessment of risks to the confidentiality, integrity, and
availability (CIA) of ePHI, with mitigations and residual risk. This is OCR's #1
enforcement target and the foundation of the Security Management Process. Review
and update at least annually and on material change.

> Status: living document. Owner: Security Official (see `SECURITY.md`). Last
> reviewed: 2026-06.

## 1. Scope & assets
- **Application:** Archangel Health — FastAPI backend + static patient dashboard,
  React landing app. Hosted on Railway.
- **ePHI assets:** in-memory patient store; `team.db` (SQLite: episodes, surveys,
  escalations, intake, care-team messages, audit log); field-encrypted JSON
  snapshot; generated voice/avatar content. See `DATA_FLOW.md`.
- **Credentials/keys:** `AUTH_SECRET` (JWT), `DATA_ENCRYPTION_KEY` (AES), admin +
  internal secrets, vendor API keys — injected via environment / secrets manager.

## 2. Threats, likelihood, impact, mitigation, residual risk

| # | Threat | L×I (pre) | Mitigation | Residual |
|---|---|---|---|---|
| R1 | Unauthorized access to a patient's PHI (IDOR / guessed id) | H×H | Per-patient session auth; staff tenant-scoping; 404 no-enumeration; audit log (PRD-1/5) | Low |
| R2 | Credential/token theft or reuse | M×H | Short patient sessions; revocation + real logout; optional TOTP MFA; HS256 with pinned alg (PRD-3/7) | Low–Med |
| R3 | PHI leaked to a third-party vendor without a BAA | M×H | Subprocessor BAA gate + de-identification; SendGrid name-stripping (PRD-4) | Low (pending vendor BAAs) |
| R4 | PHI exposed in transit | L×H | TLS 1.2/1.3 at edge + HSTS (PRD-2) | Low |
| R5 | PHI exposed at rest (lost disk/DB) | L×H | Platform volume encryption + AES-256-GCM field encryption (PRD-6); breach safe-harbor | Low |
| R6 | Tampering with or loss of audit trail | M×M | Append-only, hash-chained audit log + `verify()`; 6-yr retention; WORM-volume guidance (PRD-5) | Low |
| R7 | Vulnerable dependency / supply chain | M×H | python-jose removed; pip-audit + Dependabot + gitleaks (PRD-7) | Med (backlog being triaged) |
| R8 | Web attacks (CSRF, clickjacking, CORS abuse, brute force) | M×M | SameSite cookies; security headers/CSP; CORS allowlist; rate limiting (PRD-2) | Low |
| R9 | Weak/default secrets in production | L×H | Startup secret guard refuses default `AUTH_SECRET` (PRD-2) | Low |
| R10 | Insider / over-broad access | M×M | RBAC + tenant isolation + least privilege; full access audit (PRD-1/5) | Med |
| R11 | Availability (outage, data loss) | M×M | Managed platform; SQLite on mounted volume; **gap:** formal backup/DR + contingency testing | Med — see roadmap |
| R12 | LLM hallucination / unsafe patient content | M×H | Grounding gate + clinician review before patient delivery (existing) | Low–Med |

## 3. Key open items (feeding the roadmap)
1. **Execute BAAs** with Anthropic, Twilio, ElevenLabs, Tavus, Daily.co, Railway;
   move PHI email off SendGrid (R3).
2. **Backup & disaster recovery** plan + contingency testing (§164.308(a)(7)) (R11).
3. **Dependency backlog** remediation via Dependabot, then make scans blocking (R7).
4. **MFA enforcement** + short-lived/refresh sessions (PRD-3 phase 2) (R2).
5. **Annual penetration test** + documented evaluation (§164.308(a)(8)).
6. **SOC 2 Type II**, then HITRUST (see `COMPLIANCE_ROADMAP.md`).

## 4. Administrative & physical safeguards (status)
- Security Official assigned; sanction policy & workforce training: **to formalize**.
- Physical safeguards (data center) **inherited** from the cloud platform under its
  BAA/SOC 2; workstation/endpoint policy **to formalize**.
