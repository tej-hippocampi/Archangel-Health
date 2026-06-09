# Incident Response & Breach Notification Runbook

Covers Security Incident Procedures (§164.308(a)(6)) and the Breach Notification
Rule (§164.400–414). As a Business Associate, we must notify each Covered Entity
**without unreasonable delay and no later than 60 calendar days** from discovery —
and our customer BAAs typically require a much shorter **contractual window
(24–72 hours)**. Engineer and operate to the contractual clock, not the statutory
ceiling.

## Roles
- **Security Official** — owns the response, decides on breach determination,
  notifies customers. (Named in `SECURITY.md`.)
- **Engineering on-call** — contains and remediates.
- **Customer success / legal** — manages CE notifications and timelines.

## Phases

### 1. Detect
Sources: the tamper-evident audit log (`/admin/audit/verify`, anomalous access
patterns), CI scanners (gitleaks/pip-audit), platform/error alerts, vendor
notifications, customer reports.

### 2. Triage & contain (target: < 1 hour)
- Assess scope: which patients / what ePHI / which system.
- Contain: revoke affected tokens (logout/jti revocation), rotate `AUTH_SECRET` /
  `DATA_ENCRYPTION_KEY` if implicated, disable the affected path, isolate the host.
- Preserve evidence: snapshot the audit trail (it is hash-chained — verify integrity).

### 3. Assess for breach (target: < 24 hours)
Apply the 4-factor risk assessment (nature/extent of PHI, who accessed it, whether
it was actually acquired/viewed, mitigation). **Encryption safe-harbor:** if the
exposed ePHI was encrypted to NIST standard (we use AES-256 at rest + TLS in
transit), it is generally **not a reportable breach**.

### 4. Notify (within the contractual window; statutory ≤ 60 days)
- **Covered Entity customers (BA→CE):** notify per each BAA's clock (often 24–72h).
  Provide: what happened, ePHI involved, affected individuals, mitigation, contact.
- The CE handles individual / HHS / media notifications. We support with details.
- For breaches we directly control affecting individuals: individuals ≤ 60 days;
  HHS (≥500) ≤ 60 days, (<500) annually; media (>500 in a state) ≤ 60 days.

### 5. Remediate & learn
Root-cause analysis, fix, update controls and this runbook, record in the incident
register. Feed findings into `RISK_ANALYSIS.md`.

## Notification templates (fill-in)
> **Subject:** Security incident notification — Archangel Health
> **To:** [Covered Entity privacy/security contact]
> On [date] we discovered [summary]. The ePHI potentially involved: [types],
> affecting approximately [N] of your patients. We took the following actions:
> [containment]. Status of acquisition/viewing: [assessment]. Encryption status:
> [encrypted at rest/in transit — safe harbor / not]. Point of contact: [name/email].

## Contractual clocks (maintain a register)
| Customer | Notify within | Method |
|---|---|---|
| _example CE_ | 48 hours | email + phone to named contact |

## Gaps / roadmap
- Formal on-call rotation + alerting on audit anomalies.
- Tabletop exercise at least annually (and after major changes).
