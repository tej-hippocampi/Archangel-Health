# Pre-Op Surveys at T-96, T-48, T-24 — Evidence-Based Design

## Context

**The outcome CMS measures us on:** reduction in day-of-surgery cancellations (DOSC) and 30-day post-op complications. Pre-op DOSC runs ~18% in elective surgery (Oxford Academic 2024 systematic review) and is a top operational KPI for hospitals.

**The blocker:** today's workflow is a nurse opening a spreadsheet or EHR worklist and *manually guessing* who to call. There is no urgency signal. Patients silently fall out of readiness (anxious, non-fasted, no ride, meds not held, bowel prep botched) and only surface the morning-of, when it's too late.

**The solution:** three timed, hands-off patient surveys — T-96, T-48, T-24 hours — each measuring a **radically different** clinical question appropriate to that window, each producing a Red/Yellow/Green urgency signal on the nurse dashboard. Nurses stay in the loop for response; the platform does the detection.

**Rigor target:** match the post-op architecture already in the codebase (3-tier escalation, scored surveys, LLM+heuristic hybrid detection, event-logged in `team_store.py`).

---

## The Three Timepoints (Research Synthesis)

The timepoints are deliberately different because the patient's failure modes are different at each distance from surgery.

| Window | Clinical theme | What fails here |
|---|---|---|
| **T-96h** | Readiness baseline & logistics | Education gaps, med list wrong, no ride, anxiety untreated, risk un-stratified |
| **T-48h** | Compliance onset & symptom emergence | Carb-load not started, bowel prep botched, fever/URI appears, meds-to-hold confusion |
| **T-24h** | Imminent-risk signals & final actionability | NPO violations, anticoagulant not held, caregiver no-show, anxiety spike, hygiene missed |

---

### T-96h — **Readiness Baseline & Logistics**

**Clinical question:** *Does this patient have the information, support, and logistics to arrive ready?*

**Survey (evidence-based instruments):**
1. **PART** (Preoperative Assessment of Readiness Tool) — 15 items, Cronbach α = 0.86. Measures "quality information acquisition" + "supportive interpersonal care." ([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1089947217303829))
2. **APAIS** — 6-item anxiety + info-need scale, <2 min. Baseline anxiety score. ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC6131845/))
3. **Medication reconciliation** — patient recites current list; system diffs vs. pharmacy/EHR. ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC4949617/))
4. **Discharge logistics screener** — ride confirmed? responsible adult? home setup? ([StatPearls](https://www.ncbi.nlm.nih.gov/books/NBK557819/))
5. **ACS-NSQIP inputs** — comorbidity, ASA class → 30-day risk percentile. ([ACS](https://riskcalculator.facs.org/RiskCalculator/faq.html))

**Urgency signal to nurse:**
- 🔴 **RED** — APAIS anxiety >20, PART <60th %ile, no ride/caregiver, med list discrepancy, ACS-NSQIP high-risk → *Escalate: surgeon + social work + education call.*
- 🟡 **YELLOW** — APAIS 15–20, partial med list, ride "maybe" → *Nurse coaching call, re-send education.*
- 🟢 **GREEN** — all baselines clean → *Routine; next contact at T-48.*

**Evidence tie:** ERAS compliance ≥70% drops complications 25%; pre-op counseling is the foundational element. Social support moderates anxiety → post-op adverse events.

---

### T-48h — **Compliance Onset & Symptom Emergence**

**Clinical question:** *Is the patient executing the protocol, and is anything new happening to their body?*

**Survey:**
1. **Carb-load compliance** (ERAS) — has the clear carb drink been obtained? Any palatability issues? ([Mesentery Peritoneum](https://map.amegroups.org/article/view/6080/html))
2. **NPO comprehension** — 2-point check of the 6h-solid / 2h-clear rule. ([PubMed](https://pubmed.ncbi.nlm.nih.gov/34289299/))
3. **Medication hold/continue verification** — patient recites which meds stop vs. continue. ([AHRQ](https://www.ahrq.gov/patient-safety/settings/hospital/match/chapter-3.html))
4. **5-item symptom screen** — fever/URI, chest pain, new rash/wound, uncontrolled bleeding, new GI bleeding.
5. **Bowel prep quality** (if indicated) — self-reported stool clarity per Boston Bowel Prep framing. ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC6048432/))
6. **APAIS re-measure** — detect anxiety drift from T-96 baseline.

**Urgency signal to nurse:**
- 🔴 **RED** — any symptom YES, med hold/continue confusion, bowel prep failing, carb-load not started, APAIS jumped >5 → *Same-day MD contact; may need labs/EKG or cancellation discussion.*
- 🟡 **YELLOW** — carb-load poorly tolerated, NPO understanding borderline, mild anxiety drift → *Coaching call; consider anxiolytic pre-med.*
- 🟢 **GREEN** — all on track → *Confirm arrival time; advance to T-24.*

**Evidence tie:** ASA requires pre-anesthesia eval updated ≤48h pre-op. Carb-loading 2h pre-op reduces insulin resistance and PONV. Symptom emergence + med confusion are top addressable DOSC drivers.

---

### T-24h — **Imminent-Risk Signals & Final Actionability**

**Clinical question:** *In the next 24 hours, what specifically will cancel this case or harm this patient?*

**Survey:**
1. **NPO final timing** — last solid, last clear liquid, anything overnight? Logged as timestamps.
2. **Medication morning-of verification** — patient recites held vs. taken-with-sip.
3. **APAIS + 0–10 VAS anxiety** — spike detection. ([Springer](https://link.springer.com/article/10.1007/s44254-023-00019-1))
4. **Caregiver final confirmation** — name, phone, can stay 2–4h post-op. ([Stellar](https://www.stellartransport.com/coordinating-transportation-with-discharge-planning-a-key-to-continuity-of-care/))
5. **FRAIL / mFI frailty** (if ≥65 or high NSQIP) — predicts 30-day complications (42% vs 10%). ([AAFP](https://www.aafp.org/pubs/afp/issues/2020/1215/p753.html))
6. **Apfel PONV score** (4 items) — guides antiemetic prophylaxis. ([BJA Education](https://www.bjaed.org/article/S2058-5349(25)00024-1/fulltext))
7. **Pre-op bathing / mupirocin** (if S. aureus+) — SSI prevention. ([NCBI WHO](https://www.ncbi.nlm.nih.gov/books/NBK536404/))
8. **Single-item readiness** — "How ready do you feel, 1–10?" <5 flags psych/cold-feet.

**Urgency signal to nurse:**
- 🔴 **RED** — NPO violated, anticoagulant not held, caregiver unconfirmed, fever/chest pain/new bleeding, readiness <3 → *Stat MD/anesthesia call. Likely cancellation unless remediable.*
- 🟡 **YELLOW** — FRAIL positive, Apfel PONV=4, anxiety VAS 6–8, bathing not done → *Alert post-op team to heightened monitoring; proceed.*
- 🟢 **GREEN** — all clean → *Routine confirmation of arrival, NPO, location.*

**Evidence tie:** Pre-op phone calls 1–5 days pre-surgery cut cancellations (JOPAN). NPO compliance ~72% in literature — verbal verification catches breaches. T-24 anxiety predicts delirium, PONV, LOS.

---

## Patient-Action Indicators: Video & Intake Interview

Beyond survey results, the dashboard must show whether the patient has **done their part** — watched the pre-op prep video, started the intake interview, completed it. These are *prerequisite* indicators; surveys are *response* indicators. Missing prerequisites invalidates everything downstream.

Each of the three actions has a **natural priority window** based on what it clinically unlocks.

### Intake Interview — *Started*

**Prioritize at T-96.**

- **Why T-96:** the intake feeds medication reconciliation, ACS-NSQIP risk calc, PART baseline, and the anesthesia H&P. Every T-96 survey question assumes it exists. If intake hasn't started by T-96, the entire readiness pipeline is running blind — this is the earliest actionable checkpoint with the most recovery runway (4 days to chase).
- **Dashboard treatment:** T-96 pill shows *Intake: Started ✓ / Not started 🔴*. Not-started at T-96 is a RED hard-block, not a soft yellow — no downstream survey is trustworthy without it.

### Intake Interview — *Completed*

**Prioritize at T-48.**

- **Why T-48:** ASA guidelines require the pre-anesthesia evaluation to be *updated within 48 hours of surgery* ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC2464262/)). Anesthesia needs time to review the completed intake, call out issues (airway, cardiac, OSA, med conflicts), and modify the plan. Incomplete intake at T-48 is a documented cancellation driver.
- **Dashboard treatment:** T-48 pill shows *Intake: Complete ✓ / Partial 🟡 / Incomplete 🔴*. Incomplete at T-48 escalates Tier 2 — surgeon + anesthesia both notified, case flagged at-risk.
- At T-24, this becomes confirmation-only; incomplete at T-24 means case is almost certainly cancelled and the dashboard should show that explicitly.

### Pre-Op Prep Video — *Watched*

**Prioritize at T-48.**

- **Why T-48, not T-96:** the video's clinical job is (a) reduce anxiety (measurable via APAIS) and (b) teach NPO + carb-load + medication-hold rules. Both of those are *first measured* at T-48. If the video isn't watched by T-48, the T-48 APAIS and NPO comprehension scores are effectively measuring an uneducated baseline — the signal is polluted. Watching it at T-96 is ideal, but T-48 is the last-useful checkpoint.
- **Why not T-96:** asking "have you watched it?" at T-96 produces a lot of yellow/not-yet noise for patients who reasonably plan to watch tomorrow. Not clinically urgent until it starts blocking measurement.
- **Dashboard treatment:** T-48 pill shows *Video: Watched ✓ / Partial (minutes watched / total) 🟡 / Not started 🔴*. Not-watched at T-48 triggers a nurse nudge, not an escalation.
- **T-24:** becomes RED if still unwatched — the patient is arriving tomorrow uninformed, which predicts higher DOSC risk, higher PONV, higher anxiety-driven cancellation.

### Priority Matrix (what the doctor sees on each pill)

| Indicator | T-96 role | T-48 role | T-24 role |
|---|---|---|---|
| **Intake started** | 🔴 primary gate | (assumed) | (assumed) |
| **Intake completed** | 🟡 on-track check | 🔴 primary gate | confirmation only |
| **Video watched** | informational | 🔴 primary gate | 🔴 if still missing |

The logic: **each action is a RED gate at exactly one timepoint** — the timepoint where its downstream clinical dependency first fires. Before that it's tracking; after that it's a failure. This gives the doctor a clean "what's overdue *right now*" view rather than three columns of persistent yellow.

### Implementation note

These are event-driven, not survey-answered. The existing `event_logs` table in `/backend/team_store.py` already captures arbitrary event types with timestamps — add event types `intake_started`, `intake_completed`, `video_started`, `video_completed`, `video_progress_pct`. The dashboard pill then queries: *does the required event exist by the window's start time?* No new table needed.

---

## Cross-Cutting Design (Mirrors Post-Op Rigor)

Matches architecture already in `/backend/team_store.py`, `/backend/main.py`, `/backend/pipeline/*`:

- **Scoring:** same Green/Yellow/Orange/Red tiering as post-op (`score ≥80 / 60–79 / 40–59 / <40`) but with window-specific cut-points above.
- **Escalation tiers:** reuse 3-tier model (`Tier 1 emergency` / `Tier 2 same-day` / `Tier 3 24h navigator`) — T-24 RED = Tier 2 by definition.
- **Detection:** reuse LLM+heuristic hybrid (`_evaluate_semantic_escalation_llm` in `backend/main.py:1045-1076`) with pre-op keyword set (`forgot`, `ate`, `drank`, `bleeding`, `stopped my meds`, `no ride`, `cold feet`).
- **Storage:** extend `survey_sends` / `survey_responses` tables to carry negative day offsets (e.g., `survey_day = -4, -2, -1`).
- **Generation:** `/backend/prompts/preop.py` already scaffolded; add `preop_survey.py` for the three question sets.
- **Frontend:** `/frontend/pre-op.js` intake UI exists — reuse section patterns for survey rendering.

---

## What to Communicate to the Nurse (Dashboard Contract)

Each patient row shows three pills — T-96 / T-48 / T-24 — each Red/Yellow/Green. Clicking a pill reveals:
1. The specific failing items (e.g., "NPO violated: ate toast 06:12am").
2. Suggested action ("Call patient; confirm with anesthesia; likely 1hr delay, not cancellation").
3. One-click actions: *Mark called*, *Escalate to surgeon*, *Recommend cancel*.

Urgency is the signal. The nurse remains the decider.

---

## Verification / Next Step

This is a **research deliverable**, not an implementation. Before coding:

1. User reviews the three survey batteries and confirms clinical scope.
2. User decides productization questions:
   - Which validated instruments do we license/implement verbatim vs. adapt? (PART, APAIS, FRAIL, Apfel all have licensing considerations.)
   - Modality: SMS / voice / app?
   - Which specialties first? (ortho / cardiac / general have different ERAS bundles.)
   - Which EHR integration targets the med-reconciliation diff?
3. Then we design the data schema extensions, prompt files, and UI deltas — mirroring the post-op pipeline one-for-one.

## Key Citations

- [ERAS Society Guidelines](https://erassociety.org/guidelines/)
- [ACS NSQIP Risk Calculator](https://riskcalculator.facs.org/RiskCalculator/faq.html)
- [DOSC systematic review 2024](https://academic.oup.com/jsprm/article/2024/1/snae001/7609350)
- [PART instrument](https://www.sciencedirect.com/science/article/abs/pii/S1089947217303829)
- [APAIS validation](https://pmc.ncbi.nlm.nih.gov/articles/PMC6131845/)
- [Apfel PONV](https://www.bjaed.org/article/S2058-5349(25)00024-1/fulltext)
- [Frailty & post-op outcomes](https://www.aafp.org/pubs/afp/issues/2020/1215/p753.html)
- [Pre-op phone call evidence (JOPAN)](https://www.jopan.org/article/S1089-9472(15)00203-8/abstract)
