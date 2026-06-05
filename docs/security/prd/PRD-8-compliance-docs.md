# PRD-8: Security & Compliance Documentation / Evidence Package

## Goal
Produce the docs a hospital review (HECVAT/SIG) and OCR expect, so we answer ~40% of
any questionnaire by handing over a folder.

## Implementation — create under `docs/security/`:
1. `SECURITY.md` — overview: architecture, data classification, where ePHI lives,
   how it's protected (links the other docs). Include a **controls matrix** mapping
   implemented controls to HIPAA §164.308/310/312 and to NIST SP 800-66r2 Appendix D
   (which crosswalks to NIST CSF + SP 800-53). One row per safeguard: control,
   how-we-implement, evidence/file.
2. `RISK_ANALYSIS.md` — a §164.308(a)(1) risk analysis (OCR's #1 enforcement target)
   filled with current assets, threats, likelihood/impact, mitigations, and the
   residual-risk register.
3. `DATA_FLOW.md` — an ePHI data-flow + system map (intake → pipeline → store →
   email/SMS/voice/avatar → patient) with each subprocessor and what PHI it sees.
   Use a mermaid diagram.
4. `SUBPROCESSORS.md` — register from PRD-4 (vendor, product, PHI passed, BAA + date,
   HIPAA-eligible Y/N, data residency).
5. `INCIDENT_RESPONSE.md` — detection, triage, and a breach-notification runbook with
   the BA→CE clock (target 24–72h contractual, well inside the 60-day ceiling),
   roles, and templates.
6. `AUDIT_LOGGING.md`, `ENCRYPTION.md`, `VULN_MANAGEMENT.md` — referenced by
   PRDs 5/6/7.
7. `COMPLIANCE_ROADMAP.md` — SOC 2 Type II first (baseline), HITRUST CSF next (the
   big-system unlock), with target dates and the controls already covered by
   PRDs 1–7.
8. Add a public-facing `/privacy` and `/security` summary to the landing app and a
   `SECURITY.md` at repo root pointing to `docs/security/`.

## Acceptance criteria
- All files present and internally consistent with the code shipped in PRDs 1–7.
- The controls matrix has no "not implemented" rows for §164.312 Required specs.
