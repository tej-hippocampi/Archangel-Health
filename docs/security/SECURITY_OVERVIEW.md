# Archangel Health — Security & Compliance Overview

**For: Health-system security & privacy reviewers** · Classification: Confidential ·
Last updated: 2026-06

> This document is a vendor security packet designed for fast technical due
> diligence. It maps our controls to the HIPAA Security Rule and HECVAT/SIG topics,
> states plainly what is **in place** vs **in progress**, and points to evidence.
> Supporting detail is in the linked documents in this folder.

---

## 1. Executive summary

Archangel Health is a patient surgical-care companion (pre-op prep, post-op
recovery, AI care companion, clinician triage) that processes PHI. We have
implemented the HIPAA Security Rule **technical safeguards** end-to-end:
authenticated per-patient access control, AES-256 encryption in transit and at
rest, a tamper-evident audit trail, MFA capability, a subprocessor BAA gate with
PHI de-identification, and automated vulnerability/secret scanning. SOC 2 Type II
and HITRUST are on the near-term roadmap (not yet certified). We sign BAAs.

## 2. Product & data

- **Architecture:** Python/FastAPI backend serving a patient web dashboard, plus a
  React landing app for code-based patient entry and staff sign-in. Single managed
  cloud deployment.
- **PHI processed:** identifiers (name, DOB, MRN/MBI, phone, email) and clinical
  data (procedures, meds, instructions, recovery surveys, intake, messages). Full
  inventory + data-flow diagram: **[DATA_FLOW.md](./DATA_FLOW.md)**.

## 3. HIPAA Security Rule — controls matrix

✅ in place · ◐ partial/in progress · ➕ inherited from cloud platform

### Technical safeguards (§164.312)
| Control | Status | How |
|---|---|---|
| Access control — unique user ID (a)(2)(i) | ✅ | Per-patient sessions; per-staff JWT identities |
| Access control — emergency access (a)(2)(ii) | ◐ | Admin role; break-glass procedure to formalize |
| Automatic logoff (a)(2)(iii) | ◐ | 8-hour patient sessions; staff idle timeout in PRD-3 phase 2 |
| Encryption/decryption at rest (a)(2)(iv) | ✅ | Volume encryption ➕ AES-256-GCM field encryption — **[ENCRYPTION.md](./ENCRYPTION.md)** |
| Audit controls (b) | ✅ | Append-only, hash-chained audit log, 6-yr — **[AUDIT_LOGGING.md](./AUDIT_LOGGING.md)** |
| Integrity (c) | ✅ | Audit hash chain + `verify()`; AES-GCM auth tags detect tampering |
| Person/entity authentication (d) | ✅ | Code-based patient auth + signed sessions; staff JWT; optional TOTP MFA |
| Transmission security (e) | ✅ | TLS 1.2/1.3 + HSTS; SameSite cookies; CORS allowlist |

### Administrative safeguards (§164.308)
| Control | Status | How |
|---|---|---|
| Risk analysis (a)(1)(ii)(A) | ✅ | **[RISK_ANALYSIS.md](./RISK_ANALYSIS.md)** |
| Risk management (a)(1)(ii)(B) | ✅ | Remediation roadmap; vuln mgmt — **[VULN_MANAGEMENT.md](./VULN_MANAGEMENT.md)** |
| Security incident procedures (a)(6) | ✅ | **[INCIDENT_RESPONSE.md](./INCIDENT_RESPONSE.md)** |
| Contingency plan / backup & DR (a)(7) | ◐ | Managed platform; formal backup/DR + testing on roadmap |
| Evaluation (a)(8) | ◐ | Annual evaluation + pen test on roadmap |
| Business Associate contracts (b) | ◐ | We sign BAAs; subprocessor BAAs in process — **[SUBPROCESSORS.md](./SUBPROCESSORS.md)** |
| Workforce security / training | ◐ | To formalize (sanction policy, awareness training) |

### Physical safeguards (§164.310)
| Control | Status | How |
|---|---|---|
| Facility / device & media controls | ➕ | Inherited from the cloud platform (its SOC 2 / BAA); workstation policy to formalize |

## 4. Encryption
- **In transit:** TLS 1.2/1.3 terminated at the platform edge; **HSTS** enforced in
  production. SameSite=Lax session cookies, Secure in production.
- **At rest:** platform volume encryption (AES-256) **plus** application-layer
  **AES-256-GCM** field encryption for PHI we write to disk, with KMS-injectable
  keys and key rotation. NIST-standard encryption provides **breach safe-harbor**.

## 5. Access control & authentication
- **Patients** authenticate with a health-system code + a per-patient resource code
  → short-lived, single-use entry token → 8-hour HttpOnly session bound to that one
  patient. Every patient route enforces it; unauthenticated/wrong-patient → 404 (no
  enumeration).
- **Clinicians/admins** use signed JWTs (HS256, PyJWT, pinned algorithm),
  **role-scoped and tenant-isolated** (a clinician sees only their health system's
  patients), **revocable on logout**, with **optional TOTP MFA**.
- Brute-force **rate limiting** on login / code-entry / OTP. Production refuses to
  boot with default/weak secrets.

## 6. Audit & integrity
Every access to an ePHI surface is recorded — actor, action, patient, outcome, IP,
timestamp — in an **append-only, hash-chained** log retained **6 years**, with an
integrity-verification endpoint. No PHI bodies are stored. Admin-only access.

## 7. Subprocessors & BAAs
PHI is gated by a registry: vendors without a signed BAA receive **de-identified**
content or **no PHI**. We sign BAAs with customers. Full table + status:
**[SUBPROCESSORS.md](./SUBPROCESSORS.md)**. Highlights: Anthropic (first-party
Claude API, BAA), Twilio SMS (BAA), SendGrid (not HIPAA-eligible — patient name
stripped from email), ElevenLabs/Tavus/Daily.co (BAAs in process — de-identified
until signed).

## 8. Vulnerability & patch management
`pip-audit` (dependency CVEs) + `gitleaks` (secret scanning) on every change and
weekly, plus **Dependabot** auto-update PRs. Documented remediation SLAs (Critical
≤ 7d / High ≤ 30d / Medium ≤ 90d). The vulnerable `python-jose` library was removed
(migrated to PyJWT). Detail: **[VULN_MANAGEMENT.md](./VULN_MANAGEMENT.md)**.

## 9. Incident response & breach notification
Documented runbook with containment, the 4-factor breach assessment, encryption
safe-harbor, and a **BA→CE notification clock engineered to contractual windows
(24–72h)**, within the 60-day statutory ceiling. **[INCIDENT_RESPONSE.md](./INCIDENT_RESPONSE.md)**.

## 10. Certifications & roadmap
- **SOC 2 Type II:** planned (control set established; observation window to begin).
- **HITRUST CSF:** roadmap, post-SOC 2.
- Not yet certified — see **[COMPLIANCE_ROADMAP.md](./COMPLIANCE_ROADMAP.md)** for
  the honest current state and timeline, including 2025 NPRM readiness.

## 11. HECVAT / SIG readiness
This packet answers the bulk of HECVAT (Organization, Product, Infrastructure, AI,
Privacy) and SIG Lite (access control, encryption, logging, vuln mgmt, incident
response, subprocessors). We will complete a customer's specific HECVAT/SIG on
request. **AI note:** AI features (Claude/ElevenLabs/Tavus) can be disabled per
deployment; PHI is withheld or de-identified for any subprocessor lacking a BAA;
patient education is grounding-checked and clinician-reviewed before delivery.

## 12. Contact
Security questions, BAA requests, or to request evidence (audit samples, config):
**[Security Official — name / security@archangelhealth.ai]**. We respond to vendor
security questionnaires and will provide additional artifacts under NDA.
