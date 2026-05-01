# 01 — Archangel Health: Company & Product Context

## Mission

Archangel Health builds clinical software that helps U.S. hospitals, surgeons, and ASCs **win under the CMS Transforming Episode Accountability Model (TEAM)** — the mandatory bundled-payment program that begins **January 1, 2026** and runs through 2030. The company sells to perioperative leadership, surgical service-line owners, and CFOs of TEAM-participating hospitals.

## What is TEAM (the payment model we are built around)

- **Mandatory** 5-year alternative payment model from CMS / CMMI.
- ~**740 hospitals** in randomly selected Core-Based Statistical Areas are required to participate.
- Covers **5 surgical episode families**:
  1. Coronary artery bypass graft (**CABG**)
  2. Lower-extremity joint replacement (**LEJR** — TKA / THA)
  3. Surgical hip/femur fracture treatment (**SHFFT**)
  4. **Spinal fusion**
  5. **Major bowel procedure**
- Episode = inpatient anchor admission OR outpatient procedure → **30 days post-discharge**.
- Hospital is held accountable for **total Part A + Part B spend** in the episode window vs. a CMS-set **target price**.
- Reconciliation is adjusted by a **Composite Quality Score (CQS)** that includes readmissions, patient safety indicators, and HCAHPS-style measures.
- Required: **patient-reported outcomes (PROMs)** for LEJR, **health-equity reporting**, and a **referral / care-coordination requirement to a primary care provider** at discharge.

This is the central regulatory and economic gravity well for the company. Every product decision should ladder up to: *does this help the customer make money or avoid losing money under TEAM, while keeping the patient safe and satisfied?*

## Current product surface (what already exists)

The shipped product is internally named **CareGuide** and externally branded as **Archangel Health**. It is a **patient-facing surgical companion** plus a **clinician/admin layer**. Today it has:

### Patient-facing
- **Pre-op preparation video** — personalized voice script (ElevenLabs-synthesized, ~6–7 min) generated from the patient's EHR data: diagnosis, procedure, meds, comorbidities. Designed to prevent day-of cancellations and reduce anxiety.
- **Diagnosis explainer video** — plain-language video that explains the patient's diagnosis, why their symptoms make sense, and what it is *not*.
- **Treatment / procedure explainer video** — what the surgery is, what to expect.
- **Post-op recovery / discharge video** — recovery instructions, red flags, when to call the team.
- **Digital Care Companion** — Anthropic-Claude-powered chat that the patient can talk to anytime; constrained to the patient's specific clinical context.
- **Voice avatar** (Tavus integration) — optional video-avatar version of the same scripts.
- **SMS / Twilio** outreach for nudges.

### Clinician / health-system facing
- **Doctor portal** (`doctor.html`) — surgeon's view of their panel of patients on the platform.
- **Admin portal** (`admin.html`) — health-system admin onboarding, staff management.
- **Onboarding wizard** — health-system tenant onboarding via tokenized invite link, then SSO-style sign-in.
- **Pre-op survey + intake bot** — structured intake across 10 sections (medical history, surgical/anesthesia history, meds/allergies, social, family, ROS, functional assessment, day-of readiness). Powers personalization of all downstream content.
- **Battlecards** — short structured one-page clinical summaries rendered for the doctor and the patient.
- **Internal Prompt Lab** (`/internal/prompt-lab`) — internal tool for the team to A/B prompts against sample patients with audio + battlecard preview, then commit changes back to the repo.

### Marketing / commercial surface
- **Landing site** (Vite/React) at the root domain.
- **TEAM Calculator** — interactive ROI tool: hospital enters monthly TEAM-eligible episodes and CMS target-price assumptions; outputs current net TEAM position vs. net position with Archangel (readmission savings + CQS adjustment improvement).
- **TEAM Whitepaper** view — long-form sales narrative on the TEAM model.
- Calendly **Book a demo** as primary marketing CTA.

## ICP (who buys today, who we want to buy tomorrow)

- **Primary buyer:** VP Perioperative Services / Chief Surgical Officer / CMO at a TEAM-mandated hospital.
- **Economic buyer:** CFO or VP of Population Health / Bundled Payments office.
- **Champion / daily user:** Surgical nurse navigator, perioperative care coordinator, surgeon practice manager.
- **End user (the surgeon):** A senior orthopedic, cardiothoracic, spine, or colorectal surgeon — low tech patience, 10 minutes between cases, no tutorial tolerance, expects Epic/Doximity-grade UX.
- **End user (the patient):** Often 60+, post-op or pre-op, varying health literacy (target reading level 5–8), often anxious, often with caregivers.

## Where we are weak / open product space

- The product today is **patient-education-heavy**. The TEAM economics live in **post-acute spend, readmissions, SNF leakage, PROM capture, and CQS**. Strong opportunity to expand into care-pathway orchestration, post-acute steerage, readmission prediction, PROM collection, and surgeon-facing financial dashboards.
- The surgeon-facing surface is thin. The audit (`docs/SURGEON_UX_AUDIT.md`) shows we sell "TEAM" before we sell "your patients today" — this is a wedge problem.
- We do not yet integrate with **Epic / Cerner / Meditech** in production. Future integrations (HL7, FHIR, ADT feeds, Care Everywhere) are an obvious moat-builder and an obvious build-vs-Epic risk.
- We have **no SNF / home-health-facing surface** yet — the post-acute window is where TEAM dollars are won or lost.
- We have **no PROM-collection workflow** even though TEAM mandates LEJR PROMs.
