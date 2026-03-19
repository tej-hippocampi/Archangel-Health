# Archangel Health — CMS TEAM Model Strategy & Product Architecture Plan

> **Purpose:** Pre-meeting brief for Dallas spine surgical center + product architecture
> roadmap for TEAM compliance positioning. No codebase changes — strategy only.

---

## 1. What TEAM Actually Is (SME Brief)

### One-Sentence Definition
A **mandatory, 5-year (Jan 1 2026 – Dec 31 2030)** episode-based payment model where
~750 IPPS hospitals across 188 selected CBSAs are financially accountable for **ALL
Medicare spending** on a patient from surgery through **30 days post-discharge** —
measured against a fixed regional target price.

### The 5 Covered Procedures
| Procedure | Key MS-DRGs / HCPCS | Our Lane? |
|---|---|---|
| Lower extremity joint replacement (LEJR) | MS-DRGs 469, 470 | Adjacent |
| Surgical hip femur fracture treatment (SHFFT) | MS-DRG 480-482 | No |
| **Spinal fusion** | MS-DRGs 402, 426-430, 447-451, 471-473; HCPCS 22551, 22554, 22612, 22630, 22633 | **PRIMARY** |
| Coronary artery bypass graft (CABG) | MS-DRGs 231-236 | No |
| Major bowel procedures | MS-DRGs 329-331 | No |

---

## 2. How the Money Works (Know This Cold)

### Target Price Mechanics
- CMS sets a **target price per episode** using 3 years of **regional** historical spending
  data trended forward — not the hospital's own history
- Adjusted for: case mix, inpatient vs outpatient location, ~16 HCC risk factors per patient,
  social risk factors
- A **2% discount is baked in for spinal fusion** — the hospital starts behind on day one
- Annual **reconciliation**:
  - Costs < target → hospital keeps savings (capped at 10–20% of target price)
  - Costs > target → hospital owes CMS the overage (same cap)

### Participation Tracks
| Track | Risk | Available |
|---|---|---|
| Track 1 | **Upside only** (up to 10%) | PY1 only |
| Track 2 | Two-sided (±10%) | PY2–5, safety net hospitals |
| Track 3 | Two-sided (±20%) | All 5 years, all participants |

> **Dallas pitch:** Most Dallas hospitals will be in Track 1 for 2026 — no downside risk
> this year. This is the optimal window to start building TEAM infrastructure before
> two-sided risk kicks in in 2027.

### Quality Score Impact
A **Composite Quality Score (CQS)** adjusts the final reconciliation payment by **±15%**.
A hospital with good quality scores effectively lowers its target price threshold — they
can be "over" on cost but still break even if quality is strong.

---

## 3. Quality Measures — Your Direct Levers

| Measure | Source | Archangel Impact |
|---|---|---|
| Hospital-wide 30-day readmission | Claims | Indirect — clear discharge instructions reduce ER visits |
| **Information Transfer PRO-PM** | Patient survey | **DIRECT** — measures whether patient understood discharge instructions |
| THA/TKA PRO-PM (PROMIS surveys) | Patient-reported | Direct for joint replacement track |
| PSI-90 patient safety composite | Claims | Indirect |
| Inpatient falls with injury | eCQM | Indirect |
| Failure to rescue | Claims | Indirect |

### The Information Transfer PRO-PM Is Your Killer Talking Point
This measure asks patients: *"Did you understand your discharge instructions?"*
It flows directly into the CQS, which adjusts the hospital's reconciliation ±15%.
Archangel is literally purpose-built to improve this exact score.

**Pitch line:**
> "The Information Transfer PRO-PM is the only quality measure TEAM uses that
> is entirely within your control on day one. It doesn't require changing your
> surgical technique or your ICU protocols — it requires your patients to
> understand what to do when they go home. That's what we do."

---

## 4. The 30-Day Episode Window (Operational Reality)

```
Day 0: Surgery / anchor hospitalization
Day 0-3: Highest readmission risk window (pain crisis, medication confusion)
Day 1-3: Discharge (most spinal fusion patients go home day 2-3)
Day 3-7: First wound check, opioid tapering begins
Day 7-14: PT/OT initiation, PCP visit (TEAM requires documented referral)
Day 14: Mandatory PCP or TCM visit tracked by TEAM
Day 21-30: Functional recovery, activity advancement
Day 30: Episode closes. All costs tallied. Reconciliation period begins.
Day 31: 50% of Archangel's per-episode fee is due.
```

**What counts against the hospital's episode cost:**
- SNF stays (3-day rule is waived under TEAM — hospital bears full SNF cost)
- Home health visits
- PT/OT
- Specialist follow-up visits
- **Any ER visit in the 30-day window** — even if unrelated to surgery
- Readmission

---

## 5. Staffing Requirements — The Partnership Angle

### What TEAM Requires Hospitals to Staff
TEAM does not mandate specific staffing ratios, but successful model execution
operationally requires:

| Role | Responsibility | Timing |
|---|---|---|
| **Care Navigator / Coordinator** | Guides patient from pre-op through 30-day episode; makes day 3, 7, 14, 21 check-in calls | Pre-op → Day 30 |
| **Discharge Planner** | Coordinates SNF/home health/PT referrals; ensures 30-day network utilization | Day of discharge |
| **PCP Referral Coordinator** | Documents and tracks mandatory PCP referral | Day of discharge |
| Clinical Director (MD required) | Oversees quality and compliance — needed if hospital becomes TEAM Participant | Ongoing |

### The Technology + Staffing Bundle
Most IPPS hospitals in TEAM CBSAs **do not have enough navigators** to cover every
spinal fusion episode. This is the gap your staffing partnership fills.

**Value proposition to the hospital:**
> "You don't need to hire and train 3 new care navigators. We bring the technology
> platform AND the staffing. One contract. Archangel handles the patient-facing
> education and episode tracking. Our staffing partner provides the care navigators
> who make the calls, coordinate the referrals, and document the touches. You focus
> on surgery."

### Contractual Structure
```
Hospital
  └── Service Agreement → Archangel Health (tech + operations bundle)
                              └── Subcontract → Staffing Partner (care navigators)
```

- Hospital signs ONE contract with Archangel
- Archangel subcontracts the staffing company
- No Stark Law / Anti-Kickback issues — this is a standard vendor service agreement,
  NOT a sharing arrangement (those are only for Medicare-enrolled providers who share
  in reconciliation payments)
- Archangel is never a TEAM Collaborator — you're a vendor. Revenue comes from
  hospital service contracts, not from Medicare savings.

### Questions to Ask Your Staffing Partner
1. Do their navigators have experience in episode-based or bundled payment programs?
2. Can they staff 24/7 patient support for the 30-day window, or business hours only?
3. What is their per-navigator cost or per-patient-episode cost model?
4. Do they have existing relationships with IPPS hospitals in TEAM CBSAs?
5. Are they willing to operate inside Archangel's platform as the workflow tool?

---

## 6. Pricing Model — Per Patient, Per Episode, Split Payment

### Structure
```
Unit:         Per completed surgical episode (one patient, one procedure)
Rate:         $250–$350 per patient per episode (recommended starting range)
Split:
  50%         Due at episode initiation (day of discharge from anchor hospitalization)
  50%         Due at episode close (day 31, or upon episode completion confirmation)
```

### Why This Pricing Works
- TEAM target prices for spinal fusion run **$25,000–$50,000** per episode (varies by MS-DRG)
- The hospital can gain or lose **±20%** = $5,000–$10,000 per episode at stake
- At $300/episode, Archangel is **0.6–1.2% of episode value** — trivially easy to justify
  if we reduce even one unnecessary ER visit per 10 patients (ER visit = ~$2,000–$5,000
  against the episode)

### Revenue Modeling
| Monthly Episodes | Price/Episode | Monthly Revenue | Annual Revenue |
|---|---|---|---|
| 20 (small center) | $300 | $6,000 | $72,000 |
| 50 (mid-size) | $300 | $15,000 | $180,000 |
| 100 (large system) | $300 | $30,000 | $360,000 |
| 200 (multi-site) | $275 | $55,000 | $660,000 |

### What to Say About Pricing in the Dallas Meeting
Don't lead with price. Establish the value of the Information Transfer PRO-PM
improvement first. Then:
> "We price on a per-episode basis — you only pay for patients who go through
> the full program. Half at discharge, half at the end of the 30-day window.
> So your cash flow matches your TEAM reconciliation cycle."

---

## 7. Dallas Meeting — Spine Surgical Center Brief

### If It's an ASC (Not an IPPS Hospital)
ASCs are **not mandatory TEAM participants** (they're not IPPS-paid). But:
- The spinal fusion patients from the ASC are discharged into the 30-day window
- The **referring/primary hospital** bears the TEAM episode cost
- The ASC has a financial interest in those patients NOT readmitting to the hospital
  (it damages the relationship and referral pipeline)
- Pitch: "Your patients' outcomes after they leave your ASC directly affect your
  hospital partners' TEAM reconciliation. Archangel protects that relationship."

### If It's an IPPS Hospital Spine Program
They are mandatory TEAM participants. Direct pitch:
- Information Transfer PRO-PM → CQS → reconciliation impact
- 30-day episode cost reduction via education (fewer ER visits, fewer readmissions)
- Care navigator staffing solution (bundled with the tech)
- One contract, turnkey

### Spine-Specific Clinical Talking Points (Know These)
**Normal post-fusion symptoms (do NOT alarm patient):**
- Surgical site soreness and stiffness (peaks day 2–4)
- Muscle spasms around the fusion site
- Fatigue and mild depression (normal)
- Constipation (opioid side effect — needs active management)
- Leg tingling that improves over weeks (nerve healing)

**Red flags that ARE Archangel's job to teach:**
- 🚨 **ER Immediately**: Loss of bowel or bladder control (cauda equina emergency),
  sudden severe headache (possible CSF leak), new or worsening leg paralysis/weakness,
  fever >101.5°F with back pain (surgical site infection)
- ⚠️ **Call surgeon today**: Wound drainage increasing or odor, pain not controlled
  by medication, new numbness/tingling in previously normal leg

**Recovery timeline the surgeon wants patients to know:**
- Day 1–3: Walking with assistance, pain management priority
- Week 1–2: Short walks, log roll technique for bed mobility
- Week 2–6: No bending/lifting/twisting (BLT restrictions)
- Month 3: Follow-up imaging (fusion progress check)
- Month 6–12: Full fusion maturation

---

## 8. Product Architecture for TEAM Compliance

### Current Archangel Capabilities (What You Have)
- ✅ EHR PDF → structured extraction (Claude)
- ✅ Personalized voice scripts + audio (ElevenLabs)
- ✅ Battlecard one-page reference guides (Claude-generated HTML)
- ✅ Interactive AI avatar Q&A (Tavus)
- ✅ SMS + email delivery to patient
- ✅ Doctor portal for upload and patient management
- ✅ Demo patient (Maria, lumpectomy) for live demo

### What to Build for TEAM (Architecture Plan)

#### Module 1: Episode Management System
- **Episode object**: episode_id, patient_id, procedure_type, anchor_date, discharge_date,
  episode_close_date (anchor_date + 30), status (open/closed/reconciled)
- **Episode timeline tracker**: visual day counter (Day X of 30) in patient dashboard
- **Touch log**: record each patient interaction (viewed resource, asked avatar question,
  care navigator call, etc.) for documentation purposes

#### Module 2: Spine-Specific Content Layer
- **Diagnosis prompts**: lumbar/cervical fusion, disc herniation, spinal stenosis explanations
- **Treatment prompts**: spine-specific red flags (cauda equina, CSF leak, infection),
  BLT restrictions, wound care, constipation management
- **Avatar system prompt**: spine surgery Q&A knowledge base
- **Seeded demo patient**: James R., 62M, L4-L5 lumbar fusion, Day 5 post-discharge

#### Module 3: TEAM Reporting Dashboard (Hospital-Facing)
- Episode list view: all active episodes, day in window, risk flag indicators
- Information Transfer PRO-PM collection: prompt patient for 1-question survey
  ("Did you understand your discharge instructions?") at Day 7 and Day 30
- Export: episode summary report for hospital compliance documentation

#### Module 4: Billing / Episode Payment Tracking
- Invoice record per episode: amount, split, due dates, payment status
- Admin view: outstanding 50% payments, completed episodes awaiting close payment
- No Stripe needed initially — simple invoice tracking for early contracts

#### Module 5: Care Navigator Workflow (Staffing Partner Integration)
- Navigator dashboard: assigned episodes, pending check-in calls, escalation flags
- Patient red flag alerts: if avatar detects ER-level keywords → alert navigator
- Touch documentation: log navigator calls, outcomes, escalations

---

## 9. Demo Day Checklist (Before Dallas Meeting)

- [ ] Demo patient James R. (lumbar fusion) pre-loaded and accessible via URL
- [ ] Spine-specific battlecard showing correct red flags (cauda equina, CSF leak)
- [ ] Voice audio plays with spine-specific content
- [ ] Avatar answers "What should I watch for after my fusion?" correctly
- [ ] Doctor portal shows episode tracker with Day X of 30 indicator
- [ ] Slides / one-pager showing Information Transfer PRO-PM → CQS → reconciliation chain
- [ ] Pricing slide: $300/episode, 50/50 split, revenue modeling table
- [ ] Staffing bundle offering described

---

## 10. Key Dates

| Date | Event |
|---|---|
| Jan 1, 2026 | TEAM model live — hospitals are now accountable |
| **Now** | PY1 is Track 1 (upside only) — lowest-risk time to start |
| Dec 31, 2026 | End of PY1 |
| Jan 1, 2027 | PY2 begins — two-sided risk for most hospitals |
| Dec 31, 2030 | TEAM ends (unless extended) |

---

## Sources
- [CMS TEAM Model Overview](https://www.cms.gov/priorities/innovation/innovation-models/team-model)
- [TEAM Fact Sheet (PDF)](https://www.cms.gov/files/document/team-model-fs.pdf)
- [TEAM FAQ](https://www.cms.gov/team-model-frequently-asked-questions)
- [ACS: Opportunities & Challenges for Surgeons](https://www.facs.org/for-medical-professionals/news-publications/news-and-articles/bulletin/2025/january-2025-volume-110-issue-1/new-team-payment-model-brings-opportunities-challenges-for-surgeons-and-hospitals/)
- [Structuring Sharing Arrangements Under TEAM](https://insights.datagen.info/structuring-smart-sharing-arrangements-cms-team-model-faq)
- [MedBridge: TEAM Model Strategy](https://www.medbridge.com/blog/team-model-from-cms-a-strategic-shift-toward-surgical-episode-accountability)
- [CODE Technology: Three Big Concepts](https://www.codetechnology.com/blog/cms-team-three-big-concepts-every-hospital-needs-to-know/)
