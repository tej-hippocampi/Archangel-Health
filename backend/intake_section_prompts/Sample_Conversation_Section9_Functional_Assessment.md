# Sample Conversation: Section 9 — Pre-Operative Functional Assessment Deep Dive

This is a standalone conversation example showing how the bot thoroughly populates **every field** in Section 9 (Pre-Operative Functional Assessment) through natural dialogue. Use this as a training/optimization reference for your prompt.

---

## EDGE CASES THIS CONVERSATION IS DESIGNED TO SURFACE

```
THE CORE CHALLENGE OF SECTION 9:
Section 9 is the SHORTEST section (5 fields) but arguably the
HIGHEST-STAKES. Functional capacity determines the anesthesia risk
tier. Fall risk determines post-op safety planning. Advance
directives determine what happens if something goes wrong. The bot
must handle life-and-death legal documents with the same natural
tone it used for medications and allergies.

FUNCTIONAL CAPACITY / METs EDGE CASES:
1. DECONDITIONED VS. DISEASED — A patient who "can't climb stairs"
   because they haven't tried in years (deconditioning) is different
   from one who gets short of breath after 3 steps (cardiac/pulmonary
   disease). The bot must probe for the REASON behind the limitation.
2. OVERESTIMATION — Patients overstate fitness because they don't
   want to seem weak or because they remember their younger selves.
   The bot should anchor to RECENT, SPECIFIC activities ("in the last
   week, have you...") rather than general self-assessment.
3. OBESITY AFFECTING CAPACITY — BMI-related exercise limitation is
   real but doesn't carry the same anesthetic risk as cardiac
   limitation. The bot should distinguish cardiopulmonary from
   musculoskeletal from body-habitus limitations.
4. <4 METs AND NO CARDIAC WORKUP — If the patient truly can't do
   4 METs of activity due to cardiac or pulmonary limitation AND
   doesn't have cardiac clearance, this is a potential surgery delay
   trigger. The form must flag this prominently.

FALL RISK EDGE CASES:
5. NEAR-FALLS THAT PATIENTS DON'T COUNT — "I caught myself" or
   "I stumbled but didn't go down" are near-falls that indicate
   instability. The bot should ask about BOTH falls and near-falls.
6. MEDICATION-RELATED FALL RISK — Antihypertensives (orthostatic
   hypotension), sedatives (melatonin), opioids (post-op) all
   increase fall risk. Maria's lightheadedness on standing is a
   medication-related fall risk factor.
7. ENVIRONMENTAL FALL RISK — Already captured in Section 6 (throw
   rugs, dog) but should be cross-referenced here in the risk
   assessment.
8. FEAR OF FALLING — A known geriatric syndrome where the fear
   itself causes activity restriction and deconditioning, creating
   a self-fulfilling cycle. Affects post-op rehab engagement.

COGNITIVE STATUS EDGE CASES:
9. MILD COGNITIVE IMPAIRMENT — Patient may be functional but have
   subtle memory or processing issues that affect their ability to
   follow post-op instructions, manage medications, or recognize
   complications. The bot can't formally screen cognition, but it
   CAN observe conversational markers: coherent answers, temporal
   awareness, consistent recall of details discussed earlier.
10. DELIRIUM RISK — Age >65, pre-existing cognitive impairment,
    polypharmacy, and poor sleep are all delirium risk factors
    under general anesthesia. Maria is 58 (below 65 threshold)
    but has sleep disruption and polypharmacy — partial risk.
11. HEALTH LITERACY — The bot has been gauging this throughout the
    conversation. Can Maria understand instructions, recall
    medication details, and process new information? This informs
    whether post-op instructions should be simplified or reinforced
    with a caregiver.

ADVANCE DIRECTIVES EDGE CASES:
12. NO DIRECTIVES AND PATIENT DOESN'T WANT TO DISCUSS — Some
    patients view advance directive questions as "jinxing" the
    surgery. The bot must normalize this as routine paperwork, not
    a sign that something might go wrong.
13. DIRECTIVES EXIST BUT ARE OUTDATED — Created 20 years ago, may
    not reflect current wishes. The bot should ask WHEN they were
    created and if they've been reviewed recently.
14. HEALTHCARE PROXY DOESN'T KNOW THEY'RE THE PROXY — Patient
    named someone but never told them. The proxy can't make informed
    decisions if they haven't discussed the patient's wishes.
15. PATIENT WANTS FULL CODE / DNR DISCREPANCY — If the patient has
    a DNR but wants full resuscitation during surgery, or vice versa,
    this must be clarified with the surgeon and anesthesiologist
    pre-operatively. This is a PHYSICIAN conversation, not a bot
    conversation — but the bot should flag the existence of a DNR
    for the team to address.
16. RELIGIOUS/CULTURAL CONSIDERATIONS — Jehovah's Witnesses refuse
    blood products. Certain faiths have end-of-life preferences.
    The bot should ask if there are any religious or personal beliefs
    that affect their medical care — without assuming or probing
    specifics unless the patient volunteers.
```

---

## PRE-LOADED CONTEXT (what the bot knows before the call)

```
Patient: Maria Delgado, 58F, Hispanic/Latina
Known Conditions: Type 2 Diabetes, Hypertension, GERD (mild),
                  Suspected OSA (undiagnosed)
Scheduled Procedure: Right Total Knee Arthroplasty (May 12, 2026)
Procedure Type: Inpatient (1-2 night stay)

ALREADY CAPTURED IN PRIOR SECTIONS:
  - Section 6: Exercise tolerance >4 METs (can climb 2 flights with
    railing, do light housework, walk ~1 block). Limitation is
    orthopedic. Cane use 3-4x/week. Near-fall in shower (knee
    buckled, caught by grab bar). Single-story home, grab bar
    installed.
  - Section 8: Orthostatic lightheadedness 2x/week (Lisinopril).
    Bilateral foot paresthesia (DPN). Right knee lateral numbness.
    Anxiety 6/10. Needle phobia.
```

Several Section 9 fields are already partially populated from prior sections. The bot needs to formalize and fill the remaining gaps — advance directives, healthcare proxy, and cognitive status — while consolidating the functional and fall risk data already collected.

---

## THE CONVERSATION (Section 9 portion only)

> The bot has completed Sections 3-8. It's now entering the functional assessment. Much of this data has been gathered contextually — the bot needs to consolidate, formalize, and fill gaps.

---

**BOT:** "Okay Maria, just a few more important things. I already have a pretty good picture of your activity level from what you've told me. I want to confirm a couple things and then ask about some paperwork stuff. You mentioned you can climb two flights of stairs with the railing, and you can do housework like laundry but vacuuming is tough because of the knee. In the last week specifically — has that been about the same, or has anything gotten worse?"

**MARIA:** "Same. Nothing's changed in the last week."

> *[Functional Capacity: Confirmed current — stable over past week. >4 METs based on: stair climbing (2 flights with railing), light housework, independent ADLs. Limitation remains orthopedic (right knee), not cardiopulmonary. No interval decline.
> 
> The bot anchored to "in the last week specifically" — this prevents the patient from answering based on their peak capacity months ago. Functional assessment needs CURRENT data.]*

**BOT:** "Good. Now, you mentioned the time you slipped getting out of the shower and your knee buckled — caught yourself on the grab bar. Besides that, have you had any other falls or stumbles in the last six months? Even catching yourself on furniture or the wall counts."

**MARIA:** "There was one more. I was walking Lola — the dog — and she pulled on the leash and my knee kind of gave out. I went down on one knee in the grass. I wasn't hurt, but it shook me up."

**BOT:** "When was that?"

**MARIA:** "Maybe two months ago? Before I started using the cane more regularly."

**BOT:** "And since you started using the cane more, have you felt steadier?"

**MARIA:** "Yes, much better. I feel safer with the cane."

> *[Fall Risk: ⚠️ ELEVATED — TWO events in last 6 months:
> 1) Near-fall in shower: knee buckled, caught by grab bar (from Section 6)
> 2) Fall to one knee outdoors: dog pulled leash, knee gave way, partial fall onto grass. No injury. ~2 months ago, BEFORE increased cane use.
> 
> CONTRIBUTING FACTORS:
> — Right knee instability (primary) — buckling episodes
> — Small dog (leash pull — mechanical force)
> — Orthostatic lightheadedness (Lisinopril, from Section 8)
> — Bilateral foot paresthesia / DPN (from Section 8) — reduced proprioception
> — Melatonin PRN (mild sedation contributing factor if used before ambulation)
> 
> MITIGATING FACTORS:
> — Increased cane use (patient reports improved stability since)
> — Grab bar installed in shower
> — Single-story home (no stairs)
> — Home safety modifications planned (rug removal, dog management)
> 
> POST-OP FALL RISK WILL BE HIGHER: surgical pain + opioid medication + walker transition + unfamiliar mobility pattern. Fall precautions and physical therapy referral should be standard post-TKA, but this history makes it especially important.]*

**BOT:** "I'm glad the cane is helping. Your care team will have you using a walker after surgery, which is even more stable. Now — I want to check something I've been picking up from our conversation. You've been really clear and detailed with all your answers, you remember your medication doses and your doctors' instructions. Is that pretty normal for you, or do you ever have trouble with your memory or keeping track of things?"

**MARIA:** "No, my memory is fine. I keep a little notebook where I write down my appointments and medications. Roberto jokes that I'm the organized one."

> *[Cognitive Status: INTACT by conversational observation and patient self-report.
> 
> Bot's observational assessment throughout interview:
> — Coherent, linear responses throughout ~40 minutes of dialogue
> — Accurate temporal recall (dates of surgeries, when symptoms started, medication changes, when she quit smoking)
> — Consistent information across sections (no contradictions)
> — Demonstrates health literacy (understands medication purposes, followed hold instructions, self-treated hypoglycemia appropriately)
> — Uses compensatory organizational strategy (notebook for appointments/medications)
> — Oriented to time, place, situation
> 
> Delirium risk assessment:
> — Age 58 (below 65 threshold — lower risk)
> — No baseline cognitive impairment
> — Sleep disruption present (knee pain — moderate risk factor)
> — Polypharmacy present (moderate risk factor)
> — Overall delirium risk: LOW TO MODERATE
> 
> Health literacy assessment: ADEQUATE. Patient can independently manage post-op instructions, medication changes, and complication recognition. Caregiver (Roberto) available as backup.]*

**BOT:** "Sounds like you're very on top of things. Okay, now I have a couple questions about legal paperwork — and I promise this is routine. It's something we ask every patient before surgery. Do you have any advance directives — sometimes called a living will? It's a document that says what kind of medical care you'd want if you couldn't speak for yourself."

**MARIA:** "Yes, we did those a few years ago. Roberto and I went to a lawyer and did the whole thing — living will, power of attorney, all of it."

**BOT:** "Good. Do you remember roughly when that was?"

**MARIA:** "About three years ago. When my sister got diagnosed with cancer, it kind of pushed us to get our paperwork in order."

**BOT:** "That makes sense. And have you looked at it recently — are you still comfortable with what it says, or would you want to update anything?"

**MARIA:** "No, I'm happy with it. It says if I'm brain-dead or in a permanent vegetative state, I don't want to be kept on machines. But for anything where there's a chance of recovery, I want them to do everything."

**BOT:** "Got it. And the healthcare power of attorney — that's Roberto?"

**MARIA:** "Yes, Roberto. And my daughter is the backup if he can't do it for some reason."

**BOT:** "Does Roberto know what your wishes are — have you talked about it with him?"

**MARIA:** "Oh yes, we had a long talk when we signed the papers. He knows everything."

**BOT:** "Perfect. And last question on this — are there any religious beliefs or personal values that affect your medical care? For example, some people have preferences about blood transfusions or certain types of treatment."

**MARIA:** "No, nothing like that. I'm Catholic but there's nothing that would affect my medical care. Do whatever you need to do to fix my knee."

> *[Advance Directives: YES — on file.
> Created: ~3 years ago (2023), prompted by sister's cancer diagnosis.
> Attorney-prepared (formal legal documents).
> Recently reviewed: Patient confirms she is still comfortable with contents.
> 
> Content summary (patient-reported):
> — Terminal/irreversible condition (brain death, PVS): NO artificial life support
> — Recoverable condition: FULL CODE — wants all interventions
> This is a common and clear directive structure. The important thing for the surgical team is: for this elective TKA, Maria wants full resuscitation if anything goes wrong. There is no DNR conflict.
> 
> Healthcare Proxy / POA:
> Primary: Roberto Delgado (husband) — AWARE of patient's wishes, has discussed in detail.
> Secondary: Daughter (name not captured — backup if Roberto unavailable).
> Status: Roberto is informed, capable, and will be present on surgery day.
> 
> Religious/cultural considerations: None affecting medical care. Catholic faith, no treatment restrictions. Patient explicitly consents to all medically indicated interventions ("do whatever you need to do").
> 
> The bot asked THREE critical follow-up questions:
> 1) WHEN were the directives created? — 3 years old is recent enough to be current. A 20-year-old directive might not reflect current wishes.
> 2) Have you reviewed them recently? — Confirms the patient still agrees with what's written.
> 3) Does Roberto KNOW your wishes? — A proxy who hasn't discussed the patient's preferences can't make informed decisions. Roberto has. This is the gold standard.]*

**BOT:** "Alright Maria, let me pull together what I have for this section. Your activity level is stable — you can do stairs and housework, limited by the knee, not your heart or lungs. You've had two stumbles in the last six months from the knee giving out, but the cane has helped. Your mind is sharp as a tack. You have a living will and Roberto is your healthcare power of attorney — your daughter is the backup. Roberto knows your wishes. And no religious concerns about treatment. Sound right?"

**MARIA:** "Perfect."

---

## RESULTING SECTION 9 FORM OUTPUT

```
SECTION 9: PRE-OPERATIVE FUNCTIONAL ASSESSMENT
══════════════════════════════════════════════════════════

CLINICAL ATTENTION ITEMS FOR THIS SECTION:

  [ATTENTION] FALL RISK — ELEVATED. Two events in last 6 months
    (near-fall in shower + partial fall outdoors). Contributing
    factors: knee instability, orthostatic hypotension (Lisinopril),
    bilateral DPN, small pet. Post-op fall risk will be HIGHER
    with opioids + walker transition. Recommend: physical therapy
    referral, fall precautions protocol, early mobilization with
    supervision.

──────────────────────────────────────────────────────────

Functional Capacity (METs):
  Value:    >4 METs — ADEQUATE
  Evidence:
    [✓] Climb 2 flights of stairs (with railing)      = ~4 METs
    [✓] Light housework (laundry, tidying)             = ~3-4 METs
    [✓] Walk ~1 block (limited by knee pain, not SOB)  = >2 METs
    [✓] Independent ADLs (dressing, bathing, cooking)  = ~2 METs
    [✓] Prior baseline: 1-1.5 mile walks nightly       = ~4-5 METs
  Limiting factor: RIGHT KNEE PAIN — orthopedic, NOT cardiopulmonary
  Current status: STABLE over past week, no interval decline
  Cardiac risk per ACC/AHA: Adequate functional capacity (>4 METs).
    No additional cardiac stress testing indicated on basis of
    functional capacity. Cardiac clearance already obtained from
    PCP (Dr. Nguyen).
  Source:   Interview (consolidated from Sections 6, 8, and 9)

Fall Risk:
  Value:    ⚠️ ELEVATED
  Events (last 6 months):
    1) Near-fall: shower, knee buckled, caught by grab bar
    2) Partial fall: outdoors, dog pulled leash, knee gave way,
       went to one knee on grass. No injury. ~2 months ago.
  Contributing factors:
    — Right knee instability (buckling episodes) [PRIMARY]
    — Orthostatic hypotension (Lisinopril — lightheaded on standing
      ~2x/week, from Section 8)
    — Bilateral diabetic peripheral neuropathy (reduced foot
      proprioception, from Section 8)
    — Small dog underfoot (chihuahua, from Section 6)
    — Melatonin 5mg PRN (mild sedation if taken before ambulation)
  Mitigating factors:
    — Increased cane use (patient reports improved stability)
    — Grab bar installed in shower
    — Single-story home (no stair falls)
    — Home safety modifications planned (rug removal, dog plan)
  Post-op risk amplifiers:
    — Opioid pain medication (sedation, dizziness)
    — Walker/crutch transition (new mobility pattern)
    — Surgical pain affecting weight-bearing confidence
    — Post-anesthetic residual sedation (first 24-48 hours)
  Recommended: Physical therapy referral (likely standard for TKA),
    inpatient fall precautions, supervised early mobilization,
    home health PT if indicated post-discharge.
  Source:   Interview (consolidated from Sections 6, 8, and 9)

Cognitive Status:
  Value:    INTACT
  Assessment method: Conversational observation over ~40-minute
    interview + patient self-report
  Observations:
    — Coherent, linear, detailed responses throughout
    — Accurate temporal recall across all sections
    — No internal contradictions in reported information
    — Demonstrates adequate health literacy (understands medications,
      hold instructions, self-managed hypoglycemia)
    — Compensatory strategies in place (medication/appointment notebook)
    — Oriented to time, place, situation, and upcoming events
  Delirium risk assessment:
    — Age 58 (below 65 threshold)           LOW
    — No baseline cognitive impairment       LOW
    — Sleep disruption (knee pain)           MODERATE
    — Polypharmacy (7+ active agents)        MODERATE
    — Overall delirium risk:                 LOW TO MODERATE
  Health literacy: ADEQUATE for independent post-op instruction
    compliance. Caregiver (Roberto) available as backup.
  Source:   Interview (observational) + patient self-report

Advance Directives:
  Value:    YES — ON FILE
  Type:     Living will + Durable Power of Attorney for Healthcare
  Prepared: ~2023 (3 years ago), attorney-prepared
  Recently reviewed: YES — patient confirms current comfort with
    contents
  Content (patient-reported):
    — Terminal/irreversible condition: NO artificial life support
    — Recoverable condition: FULL CODE — all interventions desired
  Perioperative relevance: For this elective TKA, patient desires
    FULL RESUSCITATION in event of complication. No DNR. No
    treatment restrictions.
  Religious/cultural: Catholic. No treatment restrictions or
    preferences affecting medical care. Patient explicitly consents
    to all medically indicated interventions.
  Source:   Interview

Healthcare Proxy / Power of Attorney:
  Value:    YES
  Primary:  Roberto Delgado (husband)
    — Relationship: spouse
    — Informed of patient's wishes: YES — detailed discussion at
      time of document preparation
    — Available on surgery day: YES (driving patient, present
      throughout)
    — Contact: (512) 555-0192
  Secondary: Daughter (name not captured in this section — refer to
    Section 10 for daughter's involvement in post-op care)
    — Serves as backup if Roberto unavailable
  Status: ACTIVE, current, informed proxy with clear understanding
    of patient's preferences.
  Source:   Interview

──────────────────────────────────────────────────────────
FIELDS NOT OBTAINED: None. All Section 9 fields populated.
CONFLICTS: None in this section.
══════════════════════════════════════════════════════════
```

---

## WHY THIS CONVERSATION WORKS (optimization notes for the prompt)

### What the bot did right:

1. **Consolidated prior data instead of re-collecting.** The bot opened with "I already have a pretty good picture of your activity level" and confirmed rather than re-asked. This saved time and demonstrated competence. The functional capacity, cane use, and near-fall data all came from Sections 6 and 8. Section 9 added the second fall event and formalized the METs calculation.

2. **Anchored functional assessment to "the last week specifically."** This prevents patients from answering based on their best day or their memory of what they could do months ago. Current functional status is what the anesthesiologist needs for day-of-surgery risk assessment.

3. **Expanded the fall history with near-falls.** "Even catching yourself on furniture or the wall counts" — this unlocked the second event (dog pulled leash, went to one knee). Patients don't count partial falls or catches as "falls." The expanded definition captured a second instability event that changes the fall risk from "one incident" to "pattern."

4. **Assessed cognitive status through observation, not interrogation.** The bot NEVER asked "is your memory okay" as a clinical screening question (which can feel demeaning). Instead, it made an observational comment about Maria's detailed recall throughout the conversation and asked if that's normal for her. This is the conversational equivalent of an informal mental status exam — assessing orientation, recall, coherence, and health literacy through natural dialogue rather than a formal tool. The form output documents specific observational evidence.

5. **Normalized the advance directive question.** "This is routine — something we ask every patient before surgery." Without this, patients may interpret the question as "your surgery is risky" or "we think something might go wrong." The normalization frame makes it feel like paperwork, not a warning.

6. **Asked WHEN the directives were created.** Three years old is current. But if Maria had said "oh, we did that when our kids were born, back in the '90s," those 30-year-old directives might not reflect her current wishes. The age of the document determines whether the team can rely on it.

7. **Verified the proxy KNOWS the patient's wishes.** The most important advance directive question isn't "do you have one?" — it's "does your proxy know what you want?" Roberto has discussed Maria's wishes in detail. This means if a decision point arises during or after surgery, Roberto can make informed choices immediately rather than guessing. A proxy who hasn't discussed the patient's preferences is barely better than no proxy at all.

8. **Asked about religious/cultural treatment preferences.** "Are there any religious beliefs or personal values that affect your medical care?" — this is one open-ended question that screens for Jehovah's Witness blood product refusal, faith-based treatment preferences, dietary restrictions during hospital stay, and end-of-life cultural practices. Maria said no, but if she had said "I'm a Jehovah's Witness," the ENTIRE surgical plan would need a blood conservation strategy, cell saver, and potentially additional consent documentation. One question, massive potential impact.

9. **Cross-referenced fall risk factors from multiple sections.** The form output compiles contributing factors from Sections 6 (home hazards, dog, cane use), Section 8 (orthostatic hypotension, DPN, melatonin), and Section 9 (two instability events). This compound fall risk picture is far more useful than any single section's data alone.

### What would have been wrong:

- Formally screening cognition with "what day is it? who is the president?" — this is demeaning in a conversational context and the bot isn't a clinician administering a validated screening tool. Observational assessment is the appropriate approach for a voice interview.
- Skipping advance directives because "Maria is only 58 and this is an elective surgery" — anesthesia complications, while rare, can happen in any surgery. Advance directives are a UNIVERSAL pre-op requirement.
- Not asking about religious/cultural preferences — one missed question could result in a Jehovah's Witness patient receiving blood products against their beliefs, or a patient with cultural end-of-life preferences not having them honored.
- Asking "have you fallen?" and accepting "no" — Maria didn't count either of her events as a "fall" until prompted with expanded language. The near-fall in the shower and the knee-buckle with the dog both demonstrate instability that predicts post-op fall risk.
- Not asking if the proxy has been TOLD about the patient's wishes — documenting that a proxy exists is paperwork. Documenting that the proxy is INFORMED is patient safety.
