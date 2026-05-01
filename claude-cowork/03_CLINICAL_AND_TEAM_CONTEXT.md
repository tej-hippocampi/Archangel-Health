# 03 — Clinical & TEAM Model Context

Reference material for grounding product ideas in real surgical workflow and the real economics of the CMS TEAM model. **Numbers marked `[verify]` are illustrative — confirm against current CMS final rule and your own data before relying on them in a PRD.**

## The TEAM episode, end to end

```
                     ┌──────────────────────────────────────────────────────────────┐
                     │                       THE TEAM EPISODE                       │
                     └──────────────────────────────────────────────────────────────┘

  T-30 to T-1                Day 0                       Day 1–30 post-discharge
 ─────────────────       ───────────────         ──────────────────────────────────────
 - Surgical decision      - Anchor admit        - Discharge to home / HHA / SNF / IRF
 - Pre-op clearance       - Procedure           - 30-day Part A + Part B spend counts
 - Anesthesia eval        - LOS                 - Readmissions (penalized via CQS)
 - Risk stratification    - Complications       - PROM follow-up (LEJR)
 - Patient education      - Discharge plan      - PCP referral required at discharge
 - Pre-hab (optional)                           - Reconciliation vs. CMS target price
                                                  adjusted by Composite Quality Score
```

## The 5 episode families — surgeon-real notes

### CABG (Coronary Artery Bypass Graft)
- High-acuity, cardiothoracic, often in older patients with multiple comorbidities (DM, CKD, COPD).
- Common complications driving readmission: sternal wound infection, atrial fibrillation, pleural effusion, heart failure exacerbation, renal injury.
- Discharge disposition heavily influences episode cost: SNF and IRF are common and expensive.
- Pre-op: dental clearance, smoking cessation, glycemic optimization (HbA1c), nutritional status, frailty.
- Patient anxiety is dominant — chest cracking, mortality conversation. Education quality moves HCAHPS.

### Lower-Extremity Joint Replacement (LEJR — TKA / THA)
- Highest volume, most ASC-eligible, most "industrialized" episode.
- TEAM **requires PROM collection** (HOOS-JR for hip, KOOS-JR for knee) at pre-op and ~9–12 months post-op.
- 30-day readmission drivers: VTE, periprosthetic infection, dislocation (THA), mechanical complications, falls.
- Post-acute spend lever #1: home health vs. SNF discharge. Bundled-payment programs (CJR, BPCI-A) showed massive savings just by shifting away from SNF.
- ERAS protocols, multimodal analgesia, and same-day discharge dramatically reduce episode cost.

### Surgical Hip/Femur Fracture Treatment (SHFFT)
- Older, frailer patients than elective LEJR. ~30% 1-year mortality after hip fracture (high-level fact; verify cohort).
- Time-to-surgery (often <48h) is a quality marker.
- Delirium, pressure injury, and pneumonia drive readmissions and cost.
- Often non-elective → less pre-op opportunity → bigger lever is post-acute coordination, geriatrics co-management, and falls prevention.

### Spinal fusion
- Wide heterogeneity: cervical vs. lumbar, 1 level vs. multilevel, instrumented vs. non, degenerative vs. trauma vs. deformity.
- Opioid stewardship and chronic-pain post-op trajectory are major equity / quality issues.
- Readmission drivers: surgical site infection, hardware failure, dural tear, persistent radicular pain.
- Patient selection and pre-op expectation-setting are outsized predictors of satisfaction.

### Major bowel procedure
- Includes colectomy, small-bowel resection, etc. ERAS protocols are well-validated.
- Readmission drivers: anastomotic leak, ileus, SSI, dehydration (especially with new ileostomy), readmit-for-pain.
- Ostomy education and home support dramatically affect 30-day cost.
- Oncology overlay is common; care-coordination across surgery / oncology / nutrition matters.

## The economic levers under TEAM

For every product idea, ask: **which lever does this pull, and by how much?**

1. **Reduce episode spend** — fewer readmissions, shorter LOS, home > SNF disposition, fewer ED visits in 30-day window, lower post-acute utilization.
2. **Improve the Composite Quality Score (CQS)** — readmissions, hospital-acquired conditions, patient-safety indicators (PSI), HCAHPS-style measures, PROMs (LEJR).
3. **Capture required reporting** — PROMs (LEJR), health-equity data, PCP-referral attestation. Failure to capture = reconciliation penalty risk.
4. **Avoid revenue leakage** — keep post-acute referrals in network; avoid out-of-network ED bounceback; avoid avoidable ED visits.
5. **Surgeon-level performance variation** — surface per-surgeon episode cost & outcome data so service-line leadership can have the conversation.

## Quality / safety guardrails any product must respect

- **HIPAA Privacy + Security Rule**, BAA with every covered entity / vendor.
- **Patient safety:** any AI-generated patient-facing content must be reviewable by a clinician, must not give individualized medical advice beyond approved protocols, and must escalate red-flag symptoms (chest pain, dyspnea, calf swelling, fever post-op, surgical site signs of infection, neuro changes, uncontrolled bleeding).
- **FDA SaMD posture:** if a feature *diagnoses* or *drives treatment*, it likely crosses the line into device territory (Clinical Decision Support carve-outs in 21st Century Cures Act apply only when clinicians can "independently review the basis"). Patient-education and care-coordination tools generally stay non-device.
- **TJC / CMS Conditions of Participation:** discharge instructions, medication reconciliation, transitions of care.
- **Health equity:** TEAM has explicit equity reporting; non-English support, low-literacy access, and accessibility (WCAG 2.1 AA) are not optional.
- **Hallucination governance:** patient-facing content must be reproducible, reviewable, and traceable to source data (the codebase already enforces "Clinical Input Layer only — no extrapolation" — keep that bar).

## Surgeon persona (UX bar)

From `docs/SURGEON_UX_AUDIT.md`:

> Senior orthopedic surgeon, 52, 22 years in practice, low tech patience, 10 minutes between cases, no tutorial tolerance. Bar: Epic / Cerner / Doximity / basic iPhone expectations.

If a surgeon-facing screen needs onboarding, a tooltip, or a second click to find the patient, it loses. Anything they touch should look and feel like a clinical tool, not a startup landing page.

## Patient persona

- 60+, often with caregiver. Reading-level target 5–8.
- Often anxious, often non-tech-native, often bilingual or non-English-primary.
- Wants: clear instructions, someone to talk to at 9 p.m. when they're scared, knowing what's normal vs. when to call.
- Doesn't want: another portal password, marketing language, "your wellness journey."

## Glossary

- **TEAM** — Transforming Episode Accountability Model (CMS, mandatory, Jan 2026 – Dec 2030)
- **CJR** — Comprehensive Care for Joint Replacement (TEAM's predecessor for LEJR)
- **BPCI-A** — Bundled Payments for Care Improvement Advanced (voluntary bundled payment model)
- **CQS** — Composite Quality Score, the quality adjustment to TEAM reconciliation
- **PROM** — Patient-Reported Outcome Measure (HOOS-JR / KOOS-JR for LEJR under TEAM)
- **LEJR** — Lower-Extremity Joint Replacement (TKA + THA)
- **SHFFT** — Surgical Hip/Femur Fracture Treatment
- **ERAS** — Enhanced Recovery After Surgery protocol
- **ASA** — American Society of Anesthesiologists physical-status classification
- **HHA / SNF / IRF / LTACH** — Home Health Agency / Skilled Nursing Facility / Inpatient Rehab Facility / Long-Term Acute Care Hospital
- **HCAHPS** — Hospital Consumer Assessment of Healthcare Providers and Systems (patient experience survey)
- **PSI** — Patient Safety Indicators (AHRQ)
- **ADT feed** — Admit/Discharge/Transfer HL7 stream from a hospital
- **ASC** — Ambulatory Surgery Center
