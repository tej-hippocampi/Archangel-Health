# PRD — Adaptive Teach-Back v2 (Profile-Driven Question Planning, Scoring, and Patient UX)

| Field | Value |
|---|---|
| Feature | Teach-Back v2 — adaptive, profile-driven comprehension checks |
| Document version | 1.0 (build-ready) |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-06-12 |
| Primary user | Patient (pre-op and post-op portal) |
| Secondary users | RN care coordinator (results, re-teach list, escalations), Admin (stats, grader recall) |
| Implementation target | Existing codebase: Python/FastAPI backend + vanilla JS frontend. Core files: `backend/pipeline/teachback_questions.py`, `backend/pipeline/teachback_grade.py`, `backend/routers/teachback.py`, `frontend/postop.js`, `frontend/pre-op.js`, `frontend/styles.css` |
| Audience | Cursor / engineering implementers |
| Depends on | EHR extraction (`backend/pipeline/extract.py` → `structured_data`), grounding required-items (`backend/pipeline/grounding_check.py:build_required_items`), triage tier (`backend/triage/*`, TIER_1/2/3), PAM proxy + intake (`preop-retier-v1.md`), generated voice script + battlecard per track |
| Supersedes | Teach-back v1 question authoring + fallback behavior in `teachback_questions.py` (prompt `2026-06-01.1`) and grading prompt in `teachback_grade.py`. The router session flow, retry-once mechanic, re-tier trigger, and admin endpoints are **retained**. |

---

## 0. Reading order and conventions

- Tracks are unchanged: `pre_op`, `post_op_diagnosis`, `post_op_treatment`. In this document "post-op" means both post-op tracks unless one is named.
- TIER_3 = highest risk (consistent with all triage PRDs).
- "Question bank" = the 20 canonical templates in §5–§6. They are **templates with personalization slots**, not static strings. The AI question planner (§7) selects which 5 to instantiate per session; the author model (§8) fills slots from `structured_data` and may compose novel questions only under the phrasing rubric (§4).
- **Invariant (carried from triage PRDs): the patient never sees a tier, a score, or the words PASS/PARTIAL/FAIL.** Patient-facing result language is defined in §10.5.
- **Invariant (new): no internal checklist language ever reaches a patient.** Strings produced by `build_required_items()` (e.g., "diagnosis must be named/explained: Hip/Femur fracture", "what comes next (post-op instructions and/or follow-up) must be stated") are audit artifacts for the grounding judge. They are inputs to question planning, never question text. The linter (§4.3) enforces this mechanically.

---

## 1. Scope

**In scope.**

1. Root-cause fix for the current bad questions: the deterministic fallback path in `teachback_questions.py:_build_fallback_questions` wraps raw `build_required_items()` text in "In your own words, can you explain this part of your plan: …". Replace with the rewritten plain-language fallback bank (§6.4).
2. A canonical question bank: 10 pre-op + 10 post-op optimally designed teach-back templates (§5, §6), each with design rationale and expected key elements.
3. An AI **question planner**: deterministic personalization signals computed from the patient profile → a 5-question domain/form allocation per session, with rationale (§7).
4. Question **authoring v2**: a rewritten prompt that instantiates the plan, plus a deterministic question **linter** that enforces the phrasing rubric (§4, §8).
5. **Scoring v2**: key-element rubric grading, per-question and per-session comprehension score, severity weighting, unchanged retry-once + re-teach mechanic (§9).
6. Patient **UI/UX v2** for the teach-back panel: shame-free framing, one-question-per-card, open-book battlecard access, supportive retry, plain-language completion summary; plus a new pre-op teach-back panel (today the panel only exists for the two post-op tracks in `postop.js`) (§10).
7. API/contract changes, including stripping `expected`/grading material from patient-facing responses (§11).
8. Telemetry, acceptance criteria, test plan, build order (§12–§14).

**Out of scope.**

- The grounding check on voice scripts (`grounding_check.py` judge) — unchanged; we only consume `build_required_items()`.
- Re-tier algorithms — unchanged; teach-back continues to emit the same `teachback_result` event and flags consumed by `triage/preop_retier` and `triage/postop`.
- Voice/avatar delivery of teach-back, multilingual content (flagged as fast-follow in §15).
- Coordinator queue UI changes beyond surfacing the new fields already returned by existing admin endpoints.

---

## 2. Why this exists

Teach-back is the highest-leverage comprehension intervention we have — but only if the questions are real teach-back questions. Today, when the model path fails, patients see internal QA strings ("…diagnosis must be named/explained: Hip/Femur fracture?") wrapped in a generic stem, and even the model path treats every patient identically: same domain priorities, same 3 questions, regardless of whether the patient has one continued medication or four high-risk medication changes, one clean diagnosis or three interacting ones, a caregiver at home or nobody. Adaptive Teach-Back v2 makes the question set a function of the patient: a deterministic planner reads the profile (medication burden, diagnosis complexity, red-flag load, social risk, activation level, tier, prior teach-back failures) and allocates 5 question slots to the domains where misunderstanding would hurt *this* patient most; the author model instantiates well-formed questions against a strict phrasing rubric; a rubric-based grader scores key elements rather than vibes; and the UI treats the exercise as "checking that *we* explained well," which is the evidence-based framing that makes teach-back work.

---

## 3. Clinical foundation for question design

These principles (AHRQ Health Literacy Universal Precautions Toolkit, Tool #5; IHI Always Use Teach-Back) are the normative basis for the rubric in §4 and every bank entry in §5–§6:

1. **Shame-free framing.** Teach-back checks the *clinician's* teaching, not the patient's memory. Intro copy and retry copy must say so explicitly.
2. **Open-ended only.** Never answerable with yes/no. "Do you understand?" is the canonical anti-pattern. Note that "Can you explain…?" is technically yes/no-answerable and reads like a quiz — banned stem.
3. **One concept per question.** A question that asks about dose *and* wound care can't be graded cleanly and overloads working memory.
4. **Action-oriented, situated in the patient's life.** "What will you do when X happens at home?" outperforms "What did we tell you about X?" — it tests usable knowledge, not recall.
5. **Plain language, ~6th-grade level.** Medical terms allowed only when paired with plain words the patient's own materials use ("your blood thinner, warfarin").
6. **Teach-to-other framing for conceptual material.** "What would you tell your daughter about why you're having this operation?" elicits genuine paraphrase instead of parroting.
7. **Scenario form for red flags and restrictions.** Recognition + action under realistic conditions is what prevents readmissions.
8. **Highest-consequence first.** If the session is abandoned midway, the critical domains must already be covered.

---

## 4. Phrasing rubric and question linter

### 4.1 Rubric (applies to every question, bank or model-composed)

| Rule | Requirement |
|---|---|
| R1 | Open-ended. Must not start with: "Do", "Did", "Can", "Could", "Will", "Would you say", "Is", "Are", "Have you". |
| R2 | Exactly one clinical concept (one medication, one restriction, one red-flag cluster, one appointment). |
| R3 | ≤ 2 sentences, ≤ 40 words total. Scenario setup counts as the first sentence. |
| R4 | No unexplained jargon. Terms on the jargon list (NPO, anticoagulant, ambulate, prophylaxis, hypoglycemia, incision-site erythema, etc.) must be paired with a plain-language gloss, or replaced. |
| R5 | No internal-checklist language. Blocklist (case-insensitive substrings): `must be stated`, `must be named`, `named/explained`, `instruction must`, `with name + dose`, `red flag with symptom and action`. |
| R6 | Second person, active voice, present/future tense ("What will you do…", "Walk me through…"). |
| R7 | Asks for the patient's plan or action, or a teach-to-other explanation — never "recite what the document says". |
| R8 | Grounded: every clinical specific in the question (drug name, date, restriction) must exist verbatim or near-verbatim in `structured_data`. |

### 4.2 Approved stems

`Walk me through…` · `Tell me about your plan for…` · `What will you do when/if…` · `Imagine [realistic situation] — what would you do?` · `If [family member/friend] asked you [X], what would you tell them?` · `In your own words, what is [X] for, and why does it matter?` (the "in your own words" stem is allowed only with a concrete, single-concept object — never with checklist text).

### 4.3 Deterministic linter (new: `backend/pipeline/teachback_lint.py`)

```python
def lint_question(q: TeachBackQuestion, structured_data: dict) -> list[str]:
    """Return list of violation codes; empty list = clean."""
    # R1_YES_NO_STEM, R3_TOO_LONG, R4_JARGON:<term>, R5_CHECKLIST_LEAK,
    # R6_NOT_SECOND_PERSON, R8_UNGROUNDED_SPECIFIC:<token>
```

Pipeline rule: model-authored question fails lint → one regeneration with violations fed back → still failing → substitute the matching fallback bank template (§6.4) for that slot. **A linted-clean question set is a hard precondition for returning questions to the patient.** The two strings the current system shipped (§1 item 1) must each produce `R5_CHECKLIST_LEAK` in unit tests.

---

## 5. Pre-op question bank (10 canonical templates)

Slots in `{braces}` are filled from `structured_data` (field named per `extract.py` schema). `key_elements` drive grading (§9). `cond` = planner only selects when the condition holds.

| # | id | domain | form | sev | cond |
|---|---|---|---|---|---|
| PRE-1 | `pre-med-hold-plan` | MED_HOLD | OPEN_ENDED | CRITICAL | ≥1 med with status stop/hold |
| PRE-2 | `pre-med-hold-why` | MED_HOLD | WHY | CRITICAL | ≥1 high-risk held med (§7.2) |
| PRE-3 | `pre-fasting-walkthrough` | FASTING | OPEN_ENDED | CRITICAL | NPO/fasting instruction present |
| PRE-4 | `pre-morning-meds` | MED | SCENARIO | CRITICAL | ≥1 continue med + fasting present |
| PRE-5 | `pre-diabetes-plan` | MED_HOLD | OPEN_ENDED | CRITICAL | insulin/hypoglycemic in meds |
| PRE-6 | `pre-day-logistics` | FOLLOWUP | OPEN_ENDED | MAJOR | procedure_date or pre_op_instructions present |
| PRE-7 | `pre-sick-day` | RED_FLAG | SCENARIO | CRITICAL | pre-op red_flags present |
| PRE-8 | `pre-skin-prep` | ACTIVITY | OPEN_ENDED | MAJOR | skin-prep text in pre_op_instructions |
| PRE-9 | `pre-why-surgery` | MAIN_PROBLEM | OPEN_ENDED | MAJOR | always eligible |
| PRE-10 | `pre-home-support` | FOLLOWUP | OPEN_ENDED | MAJOR | lives_alone or no caregiver or transportation_barrier |

**PRE-1 — Medication hold plan.**
> "You take {med.name} — your {plain_class, e.g. 'blood thinner'}. Tell me about your plan for this medicine in the days before your surgery."

Key elements: *(required)* stop/hold action; *(required)* timing (e.g., "5 days before"); *(supporting)* what to do if a dose was taken by mistake (call team).
Why it works: names the exact drug, asks for plan not recall, one concept, action+timing both elicited by "plan … in the days before."

**PRE-2 — Why the hold matters.**
> "Your care team asked you to stop {med.name} before surgery. In your own words, why is stopping it important?"

Key elements: *(required)* safety rationale in plain terms (e.g., bleeding risk during surgery). Why: WHY-form tests depth; patients who know *why* adhere when logistics get messy.

**PRE-3 — Fasting walkthrough.**
> "Walk me through everything you plan to eat or drink from the evening before surgery until you arrive at the hospital."

Key elements: *(required)* no solid food after {cutoff}; *(required)* clear-liquid rule + its cutoff if present; *(supporting)* nothing at all after final cutoff. Why: a timeline narrative exposes the classic failure ("no food" but keeps drinking coffee with cream) that a yes/no never would.

**PRE-4 — Morning-of medications.**
> "It's the morning of surgery and your pill bottles are in front of you. Which medicines do you take, and how do you take them while you're fasting?"

Key elements: *(required)* take {continue-listed meds}; *(required)* small sip of water only; *(required if held meds exist)* skip {held meds}. Why: scenario form at the exact decision point where errors happen.

**PRE-5 — Diabetes medicine plan.**
> "Tell me about your plan for your {insulin/diabetes medicine name} the night before and the morning of surgery."

Key elements: *(required)* the documented dose adjustment (e.g., half-dose basal, hold morning oral agent). Why: hypoglycemia while NPO is a same-day-cancellation and safety event; deserves its own slot whenever applicable.

**PRE-6 — Surgery-day logistics.**
> "Tell me about your surgery-day plan — when you need to arrive, where you're going, and how you'll get home afterward."

Key elements: *(required)* arrival date/time; *(required)* ride home arranged; *(supporting)* facility name. Why: three logistics facts form one "plan" concept; missing ride = same-day cancellation.

**PRE-7 — Sick before surgery.**
> "Imagine you wake up the day before surgery with a fever or a new cough. What would you do?"

Key elements: *(required)* contact the surgical team *before* coming in; *(supporting)* the documented contact/number. Why: tests the escalation behavior, not symptom lists.

**PRE-8 — Skin prep.**
> "Describe how you'll get your skin ready the night before surgery."

Key elements: *(required)* the documented wash (e.g., CHG soap, head-down, no lotion after). Why: open-ended procedural walkthrough; surgical-site-infection lever.

**PRE-9 — Why this operation (teach-to-other).**
> "Imagine a family member asks why you're having this operation and what the surgeon will do. What would you tell them?"

Key elements: *(required)* the problem in plain words ({pre_op_diagnosis/key_diagnoses}); *(required)* the procedure + side/site ({procedure_name}, {laterality} {surgical_site}). Why: this is the *correct* version of the current broken "diagnosis must be named/explained" question — genuine paraphrase via teach-to-other framing, and laterality recall is a never-event check.

**PRE-10 — Home support.**
> "Who will help you during your first few days at home after surgery, and what have you set up so far?"

Key elements: *(required)* a named helper or concrete plan; *(supporting)* equipment/prep done. Why: selected by the planner for social-risk patients; surfaces "nobody, actually" while there's still time to act.

---

## 6. Post-op question bank (10 canonical templates)

| # | id | domain | form | sev | cond |
|---|---|---|---|---|---|
| POST-1 | `post-redflag-night` | RED_FLAG | SCENARIO | CRITICAL | red_flags present |
| POST-2 | `post-call-911` | RED_FLAG | OPEN_ENDED | CRITICAL | ≥1 emergent red flag |
| POST-3 | `post-new-med` | MED | OPEN_ENDED | CRITICAL | ≥1 med status new |
| POST-4 | `post-pain-med-limit` | MED | SCENARIO | CRITICAL | opioid/PRN analgesic present |
| POST-5 | `post-anticoag-change` | MED | OPEN_ENDED | CRITICAL | anticoagulant new/changed |
| POST-6 | `post-wound-week` | WOUND_CARE | OPEN_ENDED | MAJOR | wound_care present |
| POST-7 | `post-normal-vs-worry` | RED_FLAG | OPEN_ENDED | MAJOR | normal_symptoms and red_flags present |
| POST-8 | `post-activity-scenario` | ACTIVITY | SCENARIO | MAJOR | activity_restrictions present |
| POST-9 | `post-what-happened` | MAIN_PROBLEM | OPEN_ENDED | MAJOR | always eligible (anchor of post_op_diagnosis track) |
| POST-10 | `post-followup-plan` | FOLLOWUP | OPEN_ENDED | MAJOR | follow_up.date or provider present |

**POST-1 — Red flag at night.**
> "It's 9 at night and you notice {concrete red-flag presentation, e.g. 'fluid leaking from your incision, and you feel hot and shivery'}. Walk me through exactly what you would do."

Key elements: *(required)* the correct action ({call the care team / go to the ER} per red_flags); *(required if documented)* the threshold (e.g., fever above 100.4°F); *(supporting)* who/what number to call. Why: time-pressured recognition→action is the readmission lever; "9 at night" removes the imagined safety net of office hours.

**POST-2 — Call 911 vs. call the office.**
> "Some problems can wait for a phone call to the office. Some mean calling 911 right away. Which warning signs on your list mean call 911 immediately?"

Key elements: *(required)* the emergent flags ({chest pain, trouble breathing, …} per red_flags). Why: discrimination between escalation channels is the deadliest confusion; first sentence pair sets up the contrast in plain words.

**POST-3 — New medication.**
> "You're going home with a new medicine, {med.name}. Tell me how much you take, how often, and what it's for."

Key elements: *(required)* dose {med.dose}; *(required)* frequency {med.frequency}; *(required)* purpose in plain words; *(supporting)* one key warning from {med.notes}. Why: the three facts that prevent dosing errors, asked as one coherent "how will you take this" concept.

**POST-4 — Pain medicine ceiling.**
> "Your pain is bad tonight and the last pill didn't seem to help. What would you do, and what's the most {pain med name} you can safely take in one day?"

Key elements: *(required)* the documented daily max / minimum interval; *(required)* don't double up; *(supporting)* call the team if pain is uncontrolled. Why: scenario targets the exact moment overdoses happen — frustration plus "one more won't hurt."

**POST-5 — Blood thinner change.**
> "Your {anticoagulant name} changed after surgery. Tell me what you take now, and for how long."

Key elements: *(required)* new dose/agent; *(required)* duration; *(supporting)* monitoring (e.g., INR check) if documented. Why: peri-op anticoagulation transitions are a top cause of post-discharge adverse events; "changed" framing cues the patient that old habits are the trap.

**POST-6 — Wound care week.**
> "Describe how you'll take care of your incision this week — keeping it clean, showering, and changing the dressing."

Key elements: *(required)* the keep-dry / shower rule + its timing; *(required)* dressing-change instruction; *(supporting)* no soaking/baths if documented. Why: procedural walkthrough of one concept (the incision) across the routines where contamination actually occurs.

**POST-7 — Normal healing vs. call-us.**
> "Some soreness and swelling are a normal part of healing. What changes in your pain, swelling, or incision would make you call the care team?"

Key elements: *(required)* ≥2 patient-specific worsening signs from red_flags (distinct from normal_symptoms). Why: calibrates the patient's internal alarm in both directions — reduces both silent deterioration and panicked calls; pairs `normal_symptoms` with `red_flags`, which no current question uses.

**POST-8 — Activity restriction, applied.**
> "Imagine you drop your keys on the kitchen floor. With your {restriction in plain words, e.g. 'hip precautions'}, how would you pick them up?"
> *(slot-variant when restriction is driving/lifting/weight-bearing: "You're feeling much better on day five and want to {drive to the store / carry the laundry basket}. What does your plan say about that?")*

Key elements: *(required)* the specific restriction applied correctly (no bending past 90°, use grabber, no driving until cleared, lift limit). Why: applied-scenario form catches patients who can recite the rule but wouldn't follow it in the moment.

**POST-9 — What happened to you (teach-to-other).**
> "If a friend asked what happened and what surgery you had, what would you tell them?"

Key elements: *(required)* the diagnosis in plain words ({key_diagnoses[0]}); *(required)* the procedure in plain words ({procedure_name}); *(supporting)* why it was needed. Why: the proper replacement for the current "diagnosis must be named/explained: Hip/Femur fracture?" — paraphrase under social framing, zero quiz energy.

**POST-10 — Follow-up plan.**
> "Tell me about your next appointment — when it is, who it's with, and how you'll get there."

Key elements: *(required)* date {follow_up.date}; *(required)* provider {follow_up.provider}; *(supporting; required if transportation_barrier)* transport plan. Why: the correct replacement for "what comes next … must be stated?" — three concrete facts as one plan, with the transport element promoted to required for patients the intake flagged as transport-insecure.

### 6.4 Rewritten fallback templates (replaces `_build_fallback_questions` strings)

When the model path is unavailable, fall back to **bank templates** selected by the same planner (the planner is deterministic and needs no LLM), with slots filled by direct string substitution from `structured_data`. Per-domain last-resort stems (only if no bank template's condition matches):

| domain | fallback stem |
|---|---|
| MAIN_PROBLEM | "If a family member asked about your {diagnosis in plain words}, what would you tell them?" |
| MED / MED_HOLD | "Tell me about your plan for {med.name} — what you'll do and when." |
| FASTING | "Walk me through what you can eat and drink the night before and morning of your surgery." |
| RED_FLAG | "If you noticed {flag in plain words} at home, what would you do?" |
| WOUND_CARE | "Describe how you'll care for your incision this week." |
| ACTIVITY | "Tell me what your plan says about {restriction in plain words}, and how you'll handle it at home." |
| FOLLOWUP | "Tell me about your next appointment — when, who with, and how you'll get there." |

The strings `"In your own words, can you explain this part of your plan: …"` and `"…what is your medication plan here, and why is it important: …"` are **deleted**. Fallback questions pass through the same linter; required-item `text` is never interpolated into question text — only structured fields (`name`, `dose`, plain-language diagnosis, etc.) are.

---

## 7. AI question planner (the personalization reasoning layer)

New module: `backend/pipeline/teachback_plan.py`. Pure function, no LLM, fully unit-testable:

```python
def build_question_plan(
    *, structured_data: dict, track: str,
    tier: str | None,                 # "TIER_1" | "TIER_2" | "TIER_3" from episode
    pam_level: str | None,            # "LOW" | "MODERATE" | "HIGH" from intake
    social: dict | None,              # lives_alone, has_reliable_caregiver, transportation_barrier
    prior_teachback: dict | None,     # patient["teachback"] flags + last session aggregate
    n_questions: int = 5,
) -> QuestionPlan
```

### 7.1 Output

```python
class PlannedSlot(BaseModel):
    slot: int                      # 1..5, asked in this order
    domain: TeachBackDomain
    form: TeachBackForm
    severity: TeachBackSeverity
    bank_id: str | None            # preferred bank template, e.g. "post-pain-med-limit"
    target_fact: str               # the structured_data pointer this slot must cover, e.g. "medications[2]"
    reason: str                    # human-readable planner rationale (coordinator-visible, never patient-visible)

class QuestionPlan(BaseModel):
    track: str
    slots: list[PlannedSlot]
    signals: dict                  # the computed signal values, persisted for audit
    planner_version: str           # "2026-06-12.1"
```

### 7.2 Personalization signals

| Signal | Computation | Source |
|---|---|---|
| `med_burden` | count of meds with status ∈ {new, changed, stop} (track-appropriate) | structured_data.medications |
| `high_risk_meds` | meds matching name-list: anticoagulants/antiplatelets (warfarin, apixaban, rivaroxaban, clopidogrel, aspirin…), insulin + oral hypoglycemics, opioids, immunosuppressants, MAOIs | structured_data.medications |
| `dx_complexity` | `len(key_diagnoses)`; HIGH if ≥2, or 1 dx + ≥3 active comorbid problems | structured_data, triage ActiveProblemsInput |
| `red_flag_load` | `len(red_flags)`; emergent subset detected by keyword (chest pain, breathing, 911, ER) | structured_data.red_flags |
| `social_risk` | lives_alone AND NOT has_reliable_caregiver; transportation_barrier separately | intake / SocialHistoryInput |
| `activation` | PAM proxy level | preop-retier signals |
| `tier` | current episode tier | triage |
| `prior_failures` | domains with non-PASS final grade in the most recent completed session for this track, plus `failed_*` flags | patient["teachback"], teachback session store |

### 7.3 Allocation algorithm (deterministic, 5 slots)

Step 1 — **base allocation** per track:

| Track | Base 5 |
|---|---|
| pre_op | MED_HOLD, FASTING, RED_FLAG(sick-day), FOLLOWUP(logistics), MAIN_PROBLEM |
| post_op_treatment | RED_FLAG, MED, WOUND_CARE, ACTIVITY, FOLLOWUP |
| post_op_diagnosis | MAIN_PROBLEM, MED, RED_FLAG(normal-vs-worry), FOLLOWUP, MED or MAIN_PROBLEM (by med_burden vs dx_complexity) |

Step 2 — **adjustment rules**, applied in order (each rule swaps the lowest-priority base slot still present; track priority order = the existing `_domain_priority` order, lowest priority evicted first):

| Rule | Condition | Effect |
|---|---|---|
| A1 re-check first | `prior_failures` non-empty | Slot 1 becomes a re-check of the highest-severity failed domain, targeting the same fact, different phrasing/form than last time |
| A2 med-heavy | `med_burden ≥ 3` OR ≥2 `high_risk_meds` with status changes | MED/MED_HOLD gets a 2nd slot (distinct medications — never two questions on the same fact) |
| A3 complex diagnosis | `dx_complexity` HIGH | MAIN_PROBLEM gets a 2nd slot (e.g., POST-9 + POST-7), or on pre_op keeps PRE-9 and adds the WHY form |
| A4 diabetes | insulin/hypoglycemic present (pre_op) | PRE-5 replaces the generic MED_HOLD slot or takes a 2nd med slot |
| A5 opioid | opioid present (post-op) | POST-4 guaranteed a slot |
| A6 social risk | `social_risk` true | PRE-10 / POST-10 guaranteed; transport element promoted to required key element |
| A7 emergent flags | emergent subset non-empty (post-op) | POST-2 guaranteed |
| A8 low activation / high tier | PAM LOW or TIER_3 | Bias forms toward SCENARIO; all 5 slots severity-weighted so ≥4 are CRITICAL-or-failed-domain |
| A9 high activation / low tier | PAM HIGH and TIER_1 | Include exactly one WHY-form question (depth probe); allow 3 CRITICAL / 2 MAJOR mix |

Step 3 — **ordering and floors**: sort slots highest-consequence-first (CRITICAL before MAJOR, then track domain priority) so abandonment still covers the critical material; ≥3 of 5 slots must be CRITICAL when the profile has ≥3 CRITICAL-eligible facts; no two slots may share a `target_fact`.

Step 4 — persist `QuestionPlan` (including `signals` and per-slot `reason`) in the teach-back session row for coordinator/audit view.

### 7.4 Worked examples

**Example A — hip fracture, TIER_3, warfarin held→restarted, new oxycodone, insulin, lives alone, PAM LOW, post_op_treatment:**
Signals: med_burden 3, high_risk_meds {warfarin, oxycodone, insulin}, red_flag_load 4 (1 emergent), social_risk true, A8 active.
Plan: ①POST-5 warfarin restart (A2, CRITICAL) ②POST-4 oxycodone ceiling (A5, CRITICAL) ③POST-1 incision red flag at night (base, CRITICAL) ④POST-2 call-911 discrimination (A7, CRITICAL) ⑤POST-10 follow-up + transport (A6, MAJOR, transport required). Forms: 3 scenarios (A8). WOUND_CARE evicted as lowest surviving priority — its critical content (drainage) is embedded in slot ③'s scenario.

**Example B — knee arthroscopy, TIER_1, no med changes, PAM HIGH, pre_op:**
Signals: med_burden 0, dx_complexity LOW, A9 active.
Plan: ①PRE-3 fasting (CRITICAL) ②PRE-4 morning meds (CRITICAL) ③PRE-7 sick-day (CRITICAL) ④PRE-6 logistics (MAJOR) ⑤PRE-9 why-surgery (MAJOR, the one depth/teach-to-other probe). No MED_HOLD slot exists because no held meds exist — the planner never invents a domain the profile doesn't support.

---

## 8. Authoring v2

`generate_teachback_questions()` signature gains `plan: QuestionPlan`. New prompt (`TEACHBACK_QUESTIONS_PROMPT_V = "2026-06-12.1"`, model role registered in `ai/model_config.py` as `teachback_author` → `claude-sonnet-4-6`, temperature 0):

- Input adds `QUESTION_PLAN` (slots with domain/form/bank_id/target_fact/reason) and the §4 rubric verbatim, including approved stems and the R5 blocklist.
- Instruction: for each slot, instantiate the named bank template with patient-specific slot values; compose a novel question only if the bank template cannot be grounded, and only under the rubric.
- Output schema per question (extends `TeachBackQuestion`): adds `key_elements: list[{text: str, required: bool}]`, `bank_id: str | null`, `plan_slot: int`, `plain_topic: str` (patient-visible chip label, e.g. "Your pain medicine"). `expected` is retained as the one-line model answer.
- Post-generation: run linter (§4.3) → regenerate-once → fallback per slot. Anchor injection into battlecard HTML unchanged.

The fallback path (`_build_fallback_questions`) is rewritten to consume the same `QuestionPlan` and the §6.4 templates, so model-down behavior is now "slightly less fluent personalization," not "internal checklist leaks."

---

## 9. Scoring v2

### 9.1 Grading rubric

`grade_answer()` keeps its judge architecture (claude-sonnet-4-6, temp 0, open-book, NON_ANSWER short-circuit, fail-safe PARTIAL) and gains key-element grading. New judge output:

```json
{
  "question_id": "...",
  "elements": [{"text": "...", "required": true, "status": "MATCHED|MISSING|CONTRADICTED"}],
  "status": "PASS|PARTIAL|FAIL",
  "missing": ["..."],
  "evidence": "...",
  "unsafe_statement": "string or null"
}
```

Status derivation (deterministic, recomputed server-side from elements — the judge's `status` is advisory and overridden if inconsistent):

- **FAIL** — any element CONTRADICTED, or `unsafe_statement` non-null (patient states an action that would cause harm, e.g. "keep taking warfarin", "wait until next week").
- **PASS** — all `required` elements MATCHED.
- **PARTIAL** — otherwise (right topic, missing required specifics; includes NON_ANSWER).

### 9.2 Comprehension score

Per question: `q_score = matched_required / total_required` (FAIL ⇒ 0). Session score:

```
weight(q) = 2 if severity == CRITICAL else 1
comprehension_score = round(100 * Σ(weight·q_score) / Σ(weight))
```

Scored on the **final** grade per question (post-retry). Also computed and stored: `first_attempt_score` (same formula on attempt 1) — the teaching-effectiveness metric; the delta is the re-teach lift.

### 9.3 Session flow (unchanged mechanics, upgraded payloads)

Retry-once on non-PASS first attempt is retained, as is the `locate` payload (battlecard anchor + source quote). Retry response adds `coaching: str` — one supportive sentence naming the *topic* to re-read (never the answer): "Take another look at the part about how much pain medicine is safe in one day."

Aggregate (extends current `results.aggregate`): keeps `final_status`, `by_status`, all `failed_*` flags (re-tier contract untouched); adds `comprehension_score`, `first_attempt_score`, `domains_mastered: [..]`, `reteach_items: [{question_id, plain_topic, missing}]`. The `teachback_result` event payload adds `comprehension_score` and `planner_version` (additive — existing triage consumers unaffected).

---

## 10. Patient UI/UX

### 10.1 Placement

- **Post-op**: existing panels inside the diagnosis/treatment overlays (`frontend/postop.js` `setupTeachbackPanel`, styles at `styles.css` ~1530–1735) are restyled per below.
- **Pre-op (new)**: add a teach-back panel to `frontend/pre-op.html`, rendered below the battlecard, enabled once `resources.preop.battlecard_html` exists. Reuse the same panel component/classes; track `pre_op`.

### 10.2 Framing (shame-free)

Intro card, shown before question 1:

> **Let's make sure we explained things well.**
> We'll ask 5 quick questions about *your* plan. This isn't a test of you — it's a check on us. Your care guide stays open the whole time, and it's fine to look things up.

Buttons: `Start` / `Not now`. "Not now" records a deferral event (no penalty language).

### 10.3 Question card

- One question per card. Header: progress ("Question 2 of 5") + `plain_topic` chip ("Your medicines" — never the domain enum).
- Question text 18px+, body 16px+ (existing `.teachback-question` scale), AA contrast.
- Free-text textarea with supportive placeholder: "Answer in your own words — there's no wrong way to say it."
- Buttons: `Submit answer` (primary), `I'm not sure` (secondary — submits NON_ANSWER, routes straight to the re-teach/retry view with extra-supportive copy: "No problem — let's look at it together.").
- Persistent link: `Open my care guide` — opens the battlecard side panel (open-book by design).

### 10.4 Retry / re-teach view

On first-attempt non-PASS: show `coaching` sentence, auto-open the battlecard scrolled to `battlecard_anchor` with the existing `.teachback-highlight` flash, display `source_quote` as a styled callout, then `Try once more` re-enables the textarea. Tone rules: never "incorrect", never red error styling on the patient side; use neutral blue/amber.

### 10.5 Completion view

- Headline: "All done — thanks for walking through your plan."
- Per-topic rows using `plain_topic`: **"Got it ✓"** (PASS) or **"Worth a review"** (PARTIAL/FAIL). **No scores, no PASS/PARTIAL/FAIL words, no tier.**
- If any `reteach_items`: "Your care team will go over these with you:" + topic list.
- If `failed_red_flag` or any critical FAIL: the existing care-path escalation banner triggers ("A nurse from your care team will reach out about your warning signs."), wired to the current self-flag/escalation path — unchanged contract.

### 10.6 Accessibility

≥16px body text, visible focus states, full keyboard operability, textarea dictation-friendly (no key-event hijacking), reading level of all fixed copy ≤ 6th grade.

---

## 11. API and data contract changes

1. **`POST /api/episodes/{id}/teachback/{track}/start`** — response `questions[]` is now **patient-safe**: each question exposes only `{id, plain_topic, question, form, progress fields}`. `expected`, `key_elements`, `source_quote`, `severity`, `domain`, and planner `reason` are **stripped from the patient response** (today `expected` ships to the browser — that's both an integrity hole and a privacy smell). Full objects stay server-side in the session row. `source_quote`/`battlecard_anchor` are delivered only inside the retry `locate` payload, where they're intentional.
2. Session row gains `question_plan` (full `QuestionPlan` JSON) and `planner_version`.
3. Answer endpoint response: adds `coaching` on retry; completion `results` exposes the patient-safe aggregate subset plus the full aggregate to staff-authenticated callers only (reuse `require_patient_session` vs staff-context distinction already in the router).
4. `GET /api/episodes/{id}/teachback/{track}` (state) and `/admin/teachback/stats`: include `comprehension_score`, `first_attempt_score`, planner signals; stats gain averages of both scores and re-teach lift.
5. Prompt registry (`backend/prompts/registry.py`): bump `teachback_questions` and `teachback_grade` entries to the new versions; register the planner version alongside.
6. Question cap raised: default `max_questions = 5` (planner always emits 5 when ≥5 eligible facts exist; emits fewer only when the profile genuinely can't support 5 grounded questions — never pads).

---

## 12. Telemetry & audit

- Persist per-session: plan signals, per-slot reasons, lint violations encountered (and whether fallback fired), per-attempt grades with element detail, scores. All additive columns/JSON on the existing teach-back session table in `team_store.py`.
- New event types on the existing `log_event` stream: `teachback_plan_built`, `teachback_lint_fallback`, `teachback_deferred` (patient hit "Not now").
- Grader-recall harness: extend `backend/tests/fixtures/teachback/seed_answers.py` with element-level cases (≥3 per domain incl. CONTRADICTED/unsafe-statement cases); existing `/admin/teachback/grader-recall` surface unchanged.

---

## 13. Acceptance criteria

- **AC-1** The exact strings "In your own words, can you explain this part of your plan: diagnosis must be named/explained: Hip/Femur fracture?" and "…what comes next (post-op instructions and/or follow-up) must be stated?" can no longer be produced by any code path; linter unit test proves both trip `R5_CHECKLIST_LEAK`.
- **AC-2** With `ANTHROPIC_API_KEY` unset, a post_op_diagnosis session for the hip-fracture fixture yields 5 linted-clean questions from the fallback bank (POST-9 and POST-10 style), each grounded in structured_data.
- **AC-3** Planner is deterministic: same inputs ⇒ identical `QuestionPlan` (snapshot tests for worked examples A and B in §7.4, asserting slot domains, forms, and eviction).
- **AC-4** A profile with 3+ medication changes including an anticoagulant gets ≥2 medication questions on distinct medications (rule A2); a profile with zero held meds gets zero MED_HOLD questions.
- **AC-5** A previously failed RED_FLAG domain is re-asked as slot 1 in the next session with a different form/phrasing (rule A1).
- **AC-6** Patient `/start` response contains no `expected`, `key_elements`, `severity`, `domain`, or planner `reason` fields (contract test).
- **AC-7** Grading: all-required-matched ⇒ PASS; contradicted element or unsafe statement ⇒ FAIL; seed-answer suite (incl. new element cases) passes; server-side status derivation overrides an inconsistent judge status.
- **AC-8** `comprehension_score` and `first_attempt_score` computed per §9.2; CRITICAL questions weigh 2×; FAIL ⇒ 0 for that question.
- **AC-9** Re-tier contract unchanged: existing `failed_*` flags and `teachback_result` event fields still emitted with identical names/semantics (existing `test_teachback_router.py` continues to pass with additive-only payload changes).
- **AC-10** Pre-op panel renders in `pre-op.html` once preop battlecard exists; full session (5 questions, retry, completion) completes against track `pre_op`.
- **AC-11** Patient-facing UI never displays PASS/PARTIAL/FAIL, scores, severities, or domains; completion uses "Got it ✓ / Worth a review"; retry view never uses the word "incorrect" or red error styling.
- **AC-12** Every model-authored question passes the linter or is replaced; a session is never returned containing a lint-failing question.

---

## 14. Build order

1. `teachback_lint.py` + tests (incl. AC-1 regression strings).
2. `teachback_plan.py` signals + allocation + worked-example snapshot tests (AC-3/4/5).
3. Rewrite fallback path onto plan + §6.4 bank (AC-2).
4. Authoring prompt v2 + extended question schema + lint/regenerate/fallback loop (AC-12).
5. Grading v2: element rubric, server-side status derivation, scores; extend seed answers (AC-7/8).
6. Router: plan persistence, patient-safe response stripping, aggregate extensions, additive event payload (AC-6/9).
7. Frontend: post-op panel restyle (intro, chips, coaching, completion) then new pre-op panel (AC-10/11).
8. Telemetry + admin stats additions; registry version bumps.

---

## 15. Fast-follows (explicitly not in v1)

- Multilingual question generation keyed off intake language preference.
- Voice-first teach-back (speech-to-text answer capture) reusing the grading layer as-is.
- Coordinator-side "re-teach script" generation from `reteach_items`.
- Adaptive difficulty across sessions (mastered domains rotate out over D7/D14/D30 post-op cadence).
