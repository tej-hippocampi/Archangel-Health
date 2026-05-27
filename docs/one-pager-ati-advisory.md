# Archangel Health — for ATI Advisory

**Prepared for Aaron Kirkman, TEAM Consulting Lead — ATI Advisory**

Archangel is the operational infrastructure for the 30-day surgical episode under CMS TEAM. We maintain one live, explainable risk tier per episode — from upload through day 30 — and route it in real time to the RN coordinator, surgeon, and patient, so the right person is alerted *before* a patient becomes a readmission.

---

### Product

One scoring engine, two halves of the same picture. Most tools score the body once at the door — ICD-10s, comorbidities, ASA class, intra-op events. We do that, **and** we score whether the patient is actually informed and engaged in their own recovery: pre-op video views, intake timing, PAM activation, daily check-in adherence, medication-response streaks, AI Care Companion usage. Both columns feed the **same tier**.

- **Informedness is a clinical variable.** A medically borderline but highly engaged patient drops a tier; a medically clean but checked-out patient climbs one.
- The patient-education layer (videos, battle-cards, check-ins, AI Care Companion) is simultaneously the **intervention** that lowers readmission risk and the **sensor** that detects drift — which is why our engagement score and risk score are the same number, not two dashboards.
- The tier evolves through four phases — initial pre-op triage, pre-op re-tier, intra-op reassessment, post-op scoring — and **every change is explainable down to the contributing reason and its weight** (RN-drafted / surgeon-locked at intra-op; most-conservative-wins; full audit trail).

### Application & Tangible ROI

TEAM holds ~741 hospitals financially accountable for every Medicare dollar across LEJR, SHFFT, spinal fusion, CABG, and major bowel — surgery through day 30 — with reconciliation adjusted up to ±10% by the Composite Quality Score (Readmissions, PSI-90, PROM in 2026; falls, respiratory failure, failure-to-rescue in 2027; **OP-46 discharge comprehension in 2028**).

- **Readmissions are the fastest dollar.** Each costs $15k–$30k against the episode budget; a 10% reduction in avoidable readmissions projects to **$540k–$720k/yr** for a typical TEAM population — before CQS upside.
- **Direct CQS lift.** Personalized teach-back at zero marginal cost moves OP-46; structured D7/D14/D30 outreach moves PROM completion. Hospitals capturing OP-46 now bank a **two-year baseline advantage** before CMS grades it.
- **Evidence-anchored, not hand-waved.** Teach-back cuts 30-day readmission (OR 0.55); standardized 48-hour contact dropped readmissions 28%→17%; remote escalation detection held a high-risk cohort to 5.2%. Archangel delivers all three **without adding nursing headcount** — the gap has always been the delivery model, not the intervention.

### Traction

- **$9K MRR** across **three active private-practice clinic deployments** — proving the engine and the engagement-as-sensor model in production before enterprise.
- **Enterprise pipeline:** Dallas Day Surgery & Valley Presbyterian (security/product review), Cedars-Sinai Orthopedics, ThedaCare ED Surgery, Northwell Dept. of Surgery (faculty-wide demo), and **ATI Advisory** (strategic partnership).

### Go-to-Market

- **Deploy at the surgical pod, not the enterprise.** A pod is 4 seats — director/surgeon + 1 RN coordinator + 2 NP/PAs. Self-serve onboarding (email verify → invite pod → workspace) goes live in days; the upload-based pipeline means no heavy EHR integration to start. Land one pod → expand to the service line → department → system.
- **Metrics we track (and report to the CFO/VP Quality):** 30-day readmission and ED-visit rate, PROM/HOOS-KOOS completion, OP-46 comprehension, escalations caught vs. resolved, engagement adherence (check-in/video/med-response), and projected net reconciliation position by CQS component.
- **How we charge:** per-episode subscription (≈**$300/episode**) — priced to the TEAM unit of accountability. A *single* avoided readmission ($15k–$30k) covers **50–100 episodes** of Archangel, so the ROI case closes on one prevented 2 a.m. ER visit.
- **ICP:** TEAM-mandatory acute-care hospitals (741 across 188 MSAs); entry through ortho/spine service-line directors and TEAM-initiative owners, with the financial case carried to CMO/CFO/VP Quality. Private-practice surgical clinics are the proving ground; enterprise is the wedge's payoff.

### Expansion — from TEAM to LEAD

TEAM is the wedge, not the market. U.S. reimbursement is shifting from billing encounters toward managing longitudinal outcomes, and CMMI's next models (LEAD, ACCESS) extend accountability from the 30-day surgical episode to longer, chronic, whole-person windows. The Archangel engine ports directly:

- **Same operating model, longer window** — an explainable, continuously re-tiered risk score with informedness treated as a clinical variable; automated engagement + two-layer escalation with no added headcount.
- A preventable readmission, a delayed discharge, a denied authorization, and a failed transition are *simultaneously* clinical and reimbursement failures — so the platform naturally extends beneath care delivery into prior authorization, utilization review, discharge routing, and claims coordination.
- The end state: one unified operational layer where care delivery, reimbursement, and administration run together — the foundation for an intelligence-native, vertically integrated health system.

---

*Contact: Tej Patel · tejpatel@archangelhealth.ai · calendly.com/tejxpatel23/archangel-health-intro*
