# Sample Conversation: Section 6 — Social History & Lifestyle Deep Dive

This is a standalone conversation example showing how the bot thoroughly populates **every field** in Section 6 (Social History & Lifestyle) through natural dialogue. Use this as a training/optimization reference for your prompt.

---

## EDGE CASES THIS CONVERSATION IS DESIGNED TO SURFACE

Before reading the conversation, here are the edge cases the bot must handle in Section 6. Social history is where patients lie, minimize, feel judged, or simply don't understand why the question matters. This section has the highest sensitivity and the highest dishonesty risk on the entire form.

```
TOBACCO EDGE CASES:
1. VAPING WITHOUT CONSIDERING IT "SMOKING" — Patient says "I don't
   smoke" but vapes daily. Vaping still affects airway reactivity,
   wound healing, and nicotine-dependent cardiovascular responses
   under anesthesia. The bot MUST ask about vaping separately from
   cigarettes.
2. "SOCIAL SMOKER" MINIMIZATION — "I only smoke when I drink" or
   "maybe one or two at a party." Any current tobacco use affects
   surgical risk. The bot needs to quantify frequency, not accept
   "social smoker" as a category.
3. FORMER SMOKER — WHEN and HOW MUCH matter. Quit 2 months ago vs.
   quit 20 years ago are completely different risk profiles. Pack-
   years (packs per day x years) is the standard quantification.
   Quit <8 weeks ago means the airway is still hyperreactive.
4. SMOKELESS TOBACCO / CHEW / SNUS — Patients won't mention these
   unless specifically asked. Still affect wound healing and
   cardiovascular response.
5. MARIJUANA SMOKING — May only come up in drug use section, but
   INHALED marijuana directly affects airway reactivity just like
   tobacco smoke. The bot should capture route of use if marijuana
   is disclosed.

ALCOHOL EDGE CASES:
6. MINIMIZATION / UNDERREPORTING — The most universal pattern.
   "A couple drinks" can mean 2 or 12. The bot should ask for
   SPECIFIC quantities and types. "A couple glasses of wine" vs.
   "a couple of beers" vs. "a couple of cocktails" are very
   different alcohol volumes.
7. BINGE PATTERN VS. DAILY USE — "I only drink on weekends" but
   consumes 8-10 drinks per weekend is a binge pattern with
   different surgical risk than 1 drink nightly. The bot should
   capture pattern, not just weekly total.
8. RECENT CESSATION FOR SURGERY — Patient may have stopped drinking
   specifically for surgery. If they were a heavy daily drinker
   who stopped abruptly, alcohol withdrawal is a perioperative
   emergency (seizures, delirium tremens). The bot needs to ask
   about RECENT changes in drinking habits.
9. LIVER FUNCTION IMPLICATIONS — Heavy alcohol use affects liver
   metabolism of anesthetic drugs, coagulation (thrombocytopenia),
   and immune function. The anesthesiologist needs honest data.

RECREATIONAL DRUG EDGE CASES:
10. REFUSING TO ANSWER / FEAR OF LEGAL CONSEQUENCES — Patient may
    fear documentation in their medical record. The bot must
    normalize and explain WHY it asks (safety, not judgment). The
    bot should NEVER promise confidentiality it can't guarantee —
    just explain clinical relevance.
11. MARIJUANA IN LEGAL VS. ILLEGAL STATES — Patients in legal
    states disclose more readily. But the surgical relevance is
    the same regardless: chronic marijuana use increases anesthetic
    requirements by up to 220%, causes airway hyperreactivity, and
    increases post-op nausea.
12. STIMULANT USE (cocaine, meth, Adderall misuse) — Causes
    dangerous cardiovascular responses under anesthesia. If patient
    used cocaine within 72 hours of surgery, case MUST be
    cancelled. The bot should ask about recent use, not just
    regular use.
13. OPIOID USE — If patient is taking non-prescribed opioids, this
    dramatically changes the pain management plan (opioid-tolerant
    patients need different post-op protocols).

OCCUPATION EDGE CASES:
14. PHYSICAL JOB REQUIRING EXTENDED LEAVE — Construction worker,
    nurse, warehouse worker will need weeks to months of modified
    duty after TKA. The bot should ask about physical demands to
    help set recovery expectations.
15. SEDENTARY JOB WITH SITTING CONCERNS — Desk workers who sit
    all day post-TKA are at higher DVT risk. The bot should note
    this.
16. UNINSURED / NO PAID LEAVE — Financial stress about missing
    work affects recovery compliance. Not directly clinical, but
    worth noting for social work referral if indicated.

EXERCISE TOLERANCE / FUNCTIONAL CAPACITY EDGE CASES:
17. LIMITATION IS ORTHOPEDIC, NOT CARDIAC — Patient can't walk 2
    blocks because of their knee, not their heart. The bot MUST
    distinguish the limiting factor. "Can't walk far" due to knee
    pain is NOT the same as "can't walk far" due to shortness of
    breath. Only the latter indicates poor cardiac functional
    capacity.
18. OVERESTIMATION — Some patients overstate their fitness. The bot
    should anchor to SPECIFIC activities: "Can you climb two
    flights of stairs? Can you walk to the mailbox? Can you carry
    groceries?" rather than asking "do you exercise?"
19. METS ESTIMATION — The anesthesiologist uses METs (metabolic
    equivalents) to stratify cardiac risk. >4 METs = adequate.
    <4 METs = may need additional cardiac workup. Key benchmarks:
    — 1 MET: eat, dress, use toilet
    — 4 METs: climb two flights, walk 4 blocks, do light housework
    — 10 METs: run, swim laps, play competitive sports

LIVING SITUATION / POST-OP SUPPORT EDGE CASES:
20. LIVES ALONE WITH NO SUPPORT — This is a potential surgery
    cancellation risk for TKA. Patients who live alone with no
    arranged post-op caregiver may need skilled nursing facility
    placement or home health, both requiring advance arrangement.
21. CAREGIVER IS ALSO ELDERLY / UNWELL — "My husband will help
    me" but husband is 85 with his own mobility issues. The bot
    should gently probe whether the caregiver is physically
    capable.
22. STAIRS AT HOME — For TKA specifically: if the patient's
    bedroom and bathroom are upstairs, they may not be able to
    access them for weeks post-op. The bot should ask about home
    layout.
23. HOME SAFETY HAZARDS — Throw rugs, no grab bars, pets that
    get underfoot — all fall risks for a post-TKA patient on
    pain medication using a walker.
```

---

## PRE-LOADED CONTEXT (what the bot knows before the call)

```
Patient: Maria Delgado, 58F, Hispanic/Latina
Known Conditions: Type 2 Diabetes, Hypertension, GERD (mild),
                  Suspected OSA (undiagnosed)
Scheduled Procedure: Right Total Knee Arthroplasty (May 12, 2026)
Procedure Type: Inpatient (1-2 night stay expected)

Social History on File: NONE (no social history in patient record)
Emergency Contact: Roberto Delgado (Husband) — already confirmed
```

The bot knows Maria has a husband (Roberto, emergency contact) but has no structured social history data. All 8 fields in Section 6 start as NOT OBTAINED.

---

## THE CONVERSATION (Section 6 portion only)

> The bot has already completed Sections 3-5. Maria just finished discussing her medications and allergies. The conversation now transitions into social history. The bot uses the normalization bridge before entering this sensitive section.

---

**BOT:** "Okay Maria, we're through the hardest part. I've got a few more questions about your daily life — things like smoking, drinking, your work, and your home setup for after surgery. I ask everyone the same questions because these things directly affect how your body handles anesthesia and how quickly you recover. None of it is about judgment."

**MARIA:** "Sure, go ahead."

> *[Normalization bridge delivered before the first sensitive question. Explains WHY these questions are asked (anesthesia + recovery), frames it as universal ("I ask everyone"), and explicitly removes the judgment frame. This is clinically validated to increase disclosure rates for substance use questions.]*

**BOT:** "Do you smoke cigarettes, or have you in the past?"

**MARIA:** "I quit about twelve years ago. I smoked for about ten years before that. Maybe half a pack a day."

**BOT:** "Twelve years smoke-free — that's great. To make sure I have the numbers right: roughly half a pack a day for ten years. And when you quit, did you quit cold turkey or did you use anything to help — patches, gum, medication?"

**MARIA:** "Cold turkey. It was hard but I just did it. My doctor scared me into it when my blood sugar started going up."

> *[Tobacco: FORMER smoker. Duration: ~10 years. Amount: ~0.5 PPD. Pack-years: 0.5 x 10 = 5 pack-years. Cessation: ~12 years ago, cold turkey, motivated by diabetes diagnosis. No current nicotine replacement. 
> 
> Clinical relevance: 5 pack-years is a LOW cumulative dose. Cessation >8 weeks ago means airway hyperreactivity has normalized. Cessation >1 year ago means mucociliary function has recovered. At 12 years out, her pulmonary risk from smoking history is minimal. However, cumulative cardiovascular damage is still a factor, though attenuated significantly at 12 years.]*

**BOT:** "That took serious willpower. Now, do you vape or use any e-cigarettes, or any kind of smokeless tobacco — like chewing tobacco or nicotine pouches?"

**MARIA:** "No, nothing like that. When I quit, I quit everything."

> *[Vaping: NO. Smokeless tobacco: NO. The bot asked about vaping SEPARATELY from cigarettes — this is critical because many patients who say "I don't smoke" actively vape. Maria is clean across all nicotine products.]*

**BOT:** "Good. How about alcohol — do you drink?"

**MARIA:** "A little. I'll have a glass of wine on the weekends. Maybe Friday and Saturday night with dinner."

**BOT:** "So about two glasses a week? And is that a regular glass, or more like a large pour?"

**MARIA:** "A regular glass. Like one of those normal wine glasses. Sometimes Roberto and I share a bottle on Saturday, so maybe two glasses that night."

**BOT:** "So on a typical week, maybe two to three glasses of wine total?"

**MARIA:** "Yeah, that sounds about right. Some weeks none. It depends."

> *[Alcohol: LIGHT/SOCIAL. Pattern: ~2-3 glasses of wine per week, concentrated on weekends (Friday-Saturday). Occasional weeks with none. Standard wine glass (~5oz / ~150ml). No binge pattern. No daily use. 
> 
> Bot quantified specifically: asked about glass size (patients vary enormously — "a glass" can mean 5oz or 12oz), confirmed weekly total, and established pattern (weekend only, not daily). This level of quantification is what distinguishes a usable alcohol history from a vague "social drinker" label. At 2-3 standard drinks per week, liver function impact is negligible. No alcohol withdrawal risk.]*

**BOT:** "Got it. Has your drinking changed at all recently — drinking more or less than usual in the last few months?"

**MARIA:** "No, pretty much the same. If anything, less because I'm trying to be healthy before the surgery."

> *[Recent change: NO. No escalation, no recent cessation of heavy use. No alcohol withdrawal risk. The bot asked this specifically to screen for the edge case of a patient who stopped heavy drinking pre-surgery and might be at withdrawal risk perioperatively.]*

**BOT:** "Now, do you use any recreational drugs? Marijuana, anything else? Again, I ask everyone this, and it's strictly about keeping you safe during anesthesia. Some substances interact with the anesthesia medications in ways that your team needs to plan for."

**MARIA:** "I... well, I tried those CBD gummies I mentioned. But you already have that. I don't do marijuana or anything else. I'm pretty boring."

**BOT:** "Not boring — just healthy. And the CBD, you said you stopped those a few weeks ago, right?"

**MARIA:** "Right, at least three or four weeks. They didn't do anything for me anyway."

> *[Recreational drugs: NO. No marijuana (inhaled or edible), no cocaine, no stimulants, no opioids, no other substances. CBD was already captured in Section 5 (discontinued ~3-4 weeks ago). Bot re-confirmed discontinuation timeline here.
> 
> Note: Maria's deflection ("I'm pretty boring") is a common response when patients feel judged by drug questions, even when they have nothing to disclose. The bot's normalization upfront and its matter-of-fact tone in the follow-up ("not boring — just healthy") maintained comfort without lingering on the topic.]*

**BOT:** "Perfect. Now, what do you do for work?"

**MARIA:** "I'm a school office administrator. I've been doing it for about fifteen years. It's mostly desk work — answering phones, filing, managing student records. I sit most of the day."

**BOT:** "Are you planning to take time off for the surgery, and do you know how long?"

**MARIA:** "I'm taking six weeks off. My principal has been really supportive. I might work from home a bit after the first few weeks if I feel up to it."

> *[Occupation: School office administrator, ~15 years. Primarily sedentary (desk, phone, filing). Planning 6 weeks leave post-surgery with employer support. Possible partial remote work during recovery.
> 
> Clinical relevance for TKA: Sedentary occupation is LOW risk for return-to-work complications. However, prolonged sitting post-TKA increases DVT risk — the care team should emphasize regular position changes and ankle pumps during the recovery-at-desk phase. Six weeks is standard for TKA recovery in a sedentary role.]*

**BOT:** "That sounds like a solid plan. Six weeks is usually a good amount of time for a desk job. Now, I want to get a sense of your physical activity level. Before the knee got really bad, were you someone who exercised regularly?"

**MARIA:** "I used to walk a lot. Roberto and I would walk around the neighborhood after dinner almost every night. Maybe a mile, mile and a half. But the last year I really can't do it. By the time I get to the end of the block, the knee is throbbing."

**BOT:** "So the knee is what limits you, not your breathing or your energy?"

**MARIA:** "Exactly. My lungs are fine, my breathing is fine. It's purely the knee. If someone gave me a new knee tomorrow I'd be out walking again."

**BOT:** "That's a really helpful distinction. Can you still climb a flight of stairs?"

**MARIA:** "Yes, but I hold the railing and take it slow. I can do two flights if I have to, but I avoid it. We have a one-story house so I don't do stairs much."

**BOT:** "And things like light housework — vacuuming, carrying laundry, that kind of thing?"

**MARIA:** "I can do laundry. Vacuuming is hard because of the pushing motion on the knee. Roberto does the vacuuming now."

> *[Exercise Tolerance: MODERATE with orthopedic limitation.
> Prior baseline: walked ~1-1.5 miles nightly before knee deterioration.
> Current: limited to ~1 block before significant knee pain. Can climb 2 flights with railing (avoids by preference). Can do light housework except activities requiring knee loading (vacuuming).
> 
> CRITICAL DISTINCTION: Limitation is ORTHOPEDIC, not cardiopulmonary. Patient explicitly states lungs and breathing are fine. Knee pain is the sole limiting factor. This is essential because:
> — Orthopedic limitation: METs estimate based on what she CAN do despite knee → she can climb 2 flights, carry laundry, do housework = >4 METs
> — Cardiopulmonary limitation: METs estimate based on when she gets short of breath → would suggest <4 METs and possible need for additional cardiac workup
> 
> At >4 METs (can climb stairs, do housework, former regular walker with good baseline), Maria has adequate functional capacity for surgery per ACC/AHA guidelines. No additional cardiac stress testing indicated based on functional capacity alone.]*

**BOT:** "Good. Now let's talk about your home setup — this matters a lot for planning your recovery from knee surgery. Do you live in a house or an apartment?"

**MARIA:** "A house. One story, like I said. Three bedrooms."

**BOT:** "And the bathroom — is it easy to get to from your bedroom? Any steps or anything narrow?"

**MARIA:** "The master bathroom is attached to our bedroom. No steps. It has a walk-in shower and a separate bathtub."

**BOT:** "Does the shower have a grab bar, or anything to hold onto?"

**MARIA:** "We actually put a grab bar in about a year ago when my knee started getting bad. Roberto installed it."

> *[Home layout captured for post-TKA recovery planning:
> — Single-story home (NO stairs required for daily living — major positive for TKA recovery)
> — Master bathroom attached to bedroom (minimal walking distance for night-time bathroom use — reduces fall risk)
> — Walk-in shower with grab bar installed (can shower safely post-op without bathtub step-over)
> — Bathtub also available (will likely need to avoid until mobility improves)
> 
> This is an IDEAL home setup for TKA recovery. No stairs, short bedroom-to-bathroom path, grab bar already in place. The bot didn't just ask "do you live alone" — it assessed the physical environment because for a knee replacement specifically, home layout directly determines fall risk and recovery trajectory.]*

**BOT:** "That's great — you're already set up well. Do you have any throw rugs on the floor, especially between the bedroom and bathroom?"

**MARIA:** "We do have a small rug by the bed and one in the bathroom."

**BOT:** "Your care team will probably recommend picking those up before surgery. When you're on pain medication and using a walker, loose rugs are a fall risk. Just a heads-up."

**MARIA:** "Oh good to know. I'll have Roberto move them."

> *[Fall hazard identified and addressed: throw rugs in bedroom and bathroom. Proactive safety recommendation delivered naturally without being preachy. Patient accepted and will have husband remove before surgery. This is exactly the kind of practical pre-op home safety counseling that prevents post-discharge falls — the #1 cause of TKA readmission in the first 30 days.]*

**BOT:** "Do you have any pets in the house?"

**MARIA:** "We have a little dog. A chihuahua. Lola. She follows me everywhere."

**BOT:** "I only ask because small dogs that follow you around can get underfoot when you're using a walker. You might want to have a plan for keeping Lola in another room during those first couple weeks when you're getting around."

**MARIA:** "Ha! That's going to be a challenge. But you're right. Maybe my daughter can take her."

> *[Pet fall hazard identified: small dog (chihuahua) that follows patient closely. Relevant for walker-assisted mobility post-TKA. Patient receptive to mitigation plan (daughter may take dog temporarily). This is a legitimate clinical safety point — small pets underfoot are a documented cause of post-surgical falls, particularly in knee and hip replacement patients using walkers or crutches.]*

**BOT:** "Do you use any kind of walking aid right now — a cane, a walker, anything?"

**MARIA:** "I use a cane sometimes. When the knee is really swollen or if I'm going to be walking more than usual. Maybe three or four times a week right now."

**BOT:** "Which hand do you use the cane in?"

**MARIA:** "My left hand. Opposite side from the bad knee."

> *[Mobility aids: Cane, intermittent use (~3-4x/week currently), left hand (correct side — contralateral to affected right knee, indicating either prior instruction or intuitive use). Patient is already experienced with assisted mobility, which is a positive predictor for post-op walker/crutch adoption. She won't need to learn the concept from scratch.]*

**BOT:** "Good — left hand for a right knee problem is actually the correct side. Now, the most important part for after surgery: who's going to be with you when you come home? Since you'll be in the hospital at least one or two nights, who's bringing you home and staying with you?"

**MARIA:** "Roberto will pick me up. He works from home — he does accounting for a few small businesses, all on his computer. So he'll be there all day."

**BOT:** "And is Roberto able to help you physically — like helping you get up from a chair, steadying you with the walker, that kind of thing?"

**MARIA:** "Oh yeah. He's strong. He's 60 but he's in good shape. He goes to the gym three times a week."

> *[Post-op caregiver: Roberto Delgado (husband), age 60, physically capable (goes to gym 3x/week), works from home (accounting — flexible schedule, can be present all day). This is an IDEAL caregiver situation: present full-time, physically capable of transfer assistance, and not at risk of caregiver injury themselves.
> 
> The bot asked whether Roberto is physically CAPABLE — this screens for the edge case where the caregiver is also elderly/frail. An 85-year-old spouse with their own mobility issues cannot safely assist with transfers and walker support for a fresh TKA patient. Roberto at 60 and gym-active is well-suited.]*

**BOT:** "That's great. You mentioned earlier that your daughter is also planning to come help. How long is she staying?"

**MARIA:** "She's taking a week off work. She's coming the day before surgery and staying through that first week. After that it'll be just me and Roberto."

**BOT:** "So you'll have two people the first week, and Roberto full-time after that. And just to confirm — Roberto has a car and can drive you to your follow-up appointments?"

**MARIA:** "Yes, he drives. We have two cars."

> *[Post-op support network:
> Week 1: Daughter + Roberto (dual caregiver)
> Week 2+: Roberto only (single caregiver, home full-time)
> Transportation: Available (husband drives, two vehicles in household)
> 
> This is comprehensive post-op support. No need for skilled nursing facility, no need for home health aide, no transportation barriers for follow-up appointments. The surgical team can confidently discharge to home.]*

**BOT:** "Perfect. Let me read back what I have for your home and lifestyle situation. You quit smoking twelve years ago — half a pack a day for ten years before that, nothing since. A couple glasses of wine on weekends, no change recently. No recreational drugs. You work a desk job at a school, planning six weeks off. You can walk about a block and climb stairs with the railing — the knee is what limits you, not your lungs or your heart. You live in a one-story house with your husband Roberto. Grab bar in the shower, couple of throw rugs to move before surgery, and Lola the chihuahua to manage. Roberto's home full-time, your daughter's coming for the first week. Sound right?"

**MARIA:** "Perfect. You even remembered Lola."

**BOT:** "She sounds like a handful. Alright, let's keep going."

---

## RESULTING SECTION 6 FORM OUTPUT

```
SECTION 6: SOCIAL HISTORY & LIFESTYLE
══════════════════════════════════════════════════════════

CLINICAL ATTENTION ITEMS FOR THIS SECTION:

  [ATTENTION] HOME SAFETY — PRE-SURGERY MODIFICATIONS NEEDED:
    1) Remove throw rugs in bedroom and bathroom (fall risk with
       walker + pain medication). Patient aware, husband will remove.
    2) Small dog (chihuahua) follows patient — plan needed to keep
       dog separated during walker-assisted mobility period. Patient
       considering temporary rehoming with daughter.

  [ATTENTION] DVT PREVENTION NOTE — Patient will return to sedentary
  desk work (possibly remote) during recovery. Emphasize regular
  position changes, ankle pumps, and prescribed compression/
  anticoagulation protocol to mitigate prolonged-sitting DVT risk.

──────────────────────────────────────────────────────────

Tobacco Use:
  Value:    FORMER SMOKER
  Status:   Quit ~12 years ago
  Duration: ~10 years of use
  Amount:   ~0.5 packs per day (PPD)
  Pack-years: 5 (0.5 PPD x 10 years)
  Cessation method: Cold turkey, motivated by diabetes diagnosis
  Current nicotine use: NONE (no vaping, no e-cigarettes, no
  smokeless tobacco, no nicotine replacement)
  Pulmonary risk assessment:
    — Cessation >8 weeks: airway hyperreactivity normalized ✓
    — Cessation >1 year: mucociliary function recovered ✓
    — Cessation >10 years: COPD development risk approaching
      non-smoker baseline ✓
    — 5 pack-years: low cumulative dose ✓
    — Current pulmonary risk from tobacco history: MINIMAL
  Source:   Interview

Alcohol Use:
  Value:    LIGHT / SOCIAL
  Pattern:  Weekend only (Friday and Saturday evenings with dinner)
  Amount:   ~2-3 standard glasses of wine per week (5oz pours)
  Frequency: Some weeks none; consistent low-level pattern
  Recent change: NO — possibly slightly less recently (pre-surgery
  health consciousness)
  Binge pattern: NO
  Daily use: NO
  Liver function impact: Negligible at this consumption level
  Alcohol withdrawal risk: NONE
  Source:   Interview

Recreational Drug Use:
  Value:    NONE
  Marijuana: NO (never, per patient)
  Cocaine/stimulants: NO
  Opioids (non-prescribed): NO
  Other: NO
  CBD: Previously tried CBD gummies for knee pain. Discontinued
  ~3-4 weeks ago. Ineffective. (Already captured in Section 5
  supplements.)
  Source:   Interview

Occupation:
  Value:    School office administrator (~15 years)
  Physical demands: Sedentary — desk work, phone, filing, student
  records. Sits most of the day.
  Leave plan: 6 weeks off, employer supportive. May do partial
  remote work after first few weeks if able.
  Return-to-work risk: LOW — sedentary role compatible with TKA
  recovery timeline. Standard 6-week return for desk workers.
  DVT note: Prolonged sitting during recovery-at-desk phase —
  reinforce position changes and ankle pumps.
  Source:   Interview

Exercise Tolerance:
  Value:    MODERATE — limited by ORTHOPEDIC condition, NOT
            cardiopulmonary
  Prior baseline: Walked ~1-1.5 miles nightly (regular evening walks
  with husband) before knee deterioration (~1 year ago)
  Current capacity:
    — Walking: ~1 block before significant knee pain
    — Stairs: Can climb 2 flights with railing (avoids by preference)
    — Housework: Can do laundry, light tasks. Cannot vacuum (knee
      loading). Husband does heavy housework.
    — Limiting factor: RIGHT KNEE PAIN — NOT dyspnea, NOT fatigue,
      NOT cardiac symptoms
  METs estimate: >4 METs
    [✓] Can climb 2 flights of stairs
    [✓] Can do light housework
    [✓] Can carry laundry
    [✓] Prior regular walking 1-1.5 miles
    → ADEQUATE functional capacity per ACC/AHA guidelines
    → No additional cardiac stress testing indicated based on
      functional capacity
  Source:   Interview

Living Situation:
  Value:    Lives with husband (Roberto Delgado, age 60) in
            single-family home
  Home type: Single-story house, 3 bedrooms
  Stairs required: NONE (single story — major positive for TKA)
  Bathroom access: Master bathroom attached to bedroom. Walk-in
  shower with grab bar (installed ~1 year ago). Separate bathtub
  available.
  Fall hazards identified:
    1) Throw rug in bedroom (patient will remove before surgery)
    2) Throw rug in bathroom (patient will remove before surgery)
    3) Small dog (chihuahua) follows patient — underfoot risk with
       walker. Daughter may take dog temporarily.
  Home safety assessment: GOOD with modifications. Single story,
  grab bar in place, short bedroom-to-bathroom distance. Post-
  modification (rug removal, dog plan), home is well-suited for
  TKA recovery.
  Source:   Interview

Mobility Aids:
  Value:    Cane — intermittent use
  Frequency: ~3-4x per week currently
  Usage: Used when knee is swollen or for longer-distance walking
  Correct usage: YES — uses left hand (contralateral to right
  knee). Indicates prior instruction or intuitive understanding.
  Post-op note: Patient already experienced with assistive device
  use. Positive predictor for walker/crutch adoption post-TKA.
  Source:   Interview

Post-Op Caregiver Available:
  Value:    ⚠️ YES — COMPREHENSIVE SUPPORT NETWORK
  Primary caregiver: Roberto Delgado (husband)
    — Age: 60, physically capable (gym 3x/week)
    — Availability: Works from home (accounting, computer-based),
      present all day
    — Can assist with: transfers, walker support, transportation
    — Has vehicle: YES (two cars in household)
  Secondary caregiver: Daughter (name not captured)
    — Availability: Week 1 post-op (taking week off work, arriving
      day before surgery)
  Coverage timeline:
    — Pre-surgery day through Week 1: Daughter + Roberto (dual)
    — Week 2+: Roberto only (home full-time)
  Transportation to follow-ups: Confirmed — husband drives
  Skilled nursing facility needed: NO
  Home health aide needed: NO (adequate family support)
  Discharge planning: HOME with family support. No barriers.
  Source:   Interview

──────────────────────────────────────────────────────────
FIELDS NOT OBTAINED: None. All Section 6 fields populated.
CONFLICTS: None in this section.
══════════════════════════════════════════════════════════
```

---

## WHY THIS CONVERSATION WORKS (optimization notes for the prompt)

### What the bot did right:

1. **Delivered the normalization bridge BEFORE the first question.** "I ask everyone the same questions... none of it is about judgment." This single sentence, placed before the tobacco question, sets the tone for the entire section. Studies show that a pre-question normalization statement increases honest disclosure rates for substance use by 20-40%. The bot didn't normalize each question individually (which sounds repetitive) — it normalized the entire block upfront.

2. **Asked about vaping SEPARATELY from smoking.** Maria said no to both, but the bot asked "do you vape or use e-cigarettes, or any kind of smokeless tobacco" as its own question. Patients increasingly say "I don't smoke" while actively vaping. One in five adults under 35 who say "no" to smoking are daily vapers. The separate question closes this gap.

3. **Quantified alcohol with specificity.** The bot didn't accept "a glass of wine on weekends." It asked: how many glasses? Regular glass or large pour? Maria initially said "a glass" — the follow-up revealed it's sometimes two glasses on Saturday (shared bottle). The bot then confirmed the weekly total: "two to three glasses total." This is how you get usable data. "Social drinker" is clinically meaningless. "2-3 standard glasses of wine per week, weekends only" is actionable.

4. **Screened for recent alcohol pattern changes.** "Has your drinking changed recently?" is the question that catches the hidden-withdrawal patient. If Maria had said "I actually used to drink a bottle of wine every night but I stopped two weeks ago for the surgery," that would be a perioperative emergency flag — alcohol withdrawal can cause seizures, delirium tremens, and hemodynamic instability during surgery. Maria's pattern is stable, which is reassuring, but the question had to be asked.

5. **Distinguished orthopedic limitation from cardiac limitation in exercise tolerance.** This is the single most important data point in this section for the anesthesiologist. "Can't walk far" means two completely different things depending on WHY. The bot asked Maria directly: "the knee is what limits you, not your breathing or your energy?" and she confirmed. This changes her METs estimate from <4 (concerning) to >4 (adequate). The bot then validated with specific functional benchmarks: stairs with railing (>4 METs), light housework (>4 METs), prior regular walking 1-1.5 miles (good baseline). The form output explicitly maps these to the ACC/AHA functional capacity framework.

6. **Assessed the home environment for TKA-specific risks.** The bot didn't stop at "lives with husband in a house." It asked: single story or multi-story? Where's the bathroom relative to the bedroom? Grab bars? Throw rugs? Pets? Each question targets a specific post-TKA fall risk factor. The single-story home with an attached master bath and existing grab bar is ideal. The throw rugs and the chihuahua are real hazards that the bot identified and gave actionable guidance on — remove the rugs, manage the dog. These are the kinds of practical interventions that prevent readmissions.

7. **Assessed caregiver CAPABILITY, not just availability.** "Is Roberto able to help you physically?" — this distinguishes a capable caregiver from one who is present but unable to assist with transfers, walker support, or fall recovery. The bot learned Roberto is 60 and goes to the gym 3x/week — he can handle the physical demands of TKA caregiver duties. If Roberto had been 85 with a bad back, the discharge plan would need to include home health aide services.

8. **Asked about the daughter's timeline.** The bot already knew the daughter was coming (from a prior section). Here it captured the specific coverage: arriving day before surgery, staying one week, then Roberto takes over solo. This gives the surgical team a clear post-op support timeline for discharge planning.

9. **Delivered practical safety counseling in-conversation.** The rug advice and the dog advice weren't clinical recommendations — they were common-sense safety tips delivered naturally. "Your care team will probably recommend picking those up" and "small dogs that follow you around can get underfoot when you're using a walker" — these feel like a friend giving advice, not a nurse reading a checklist. Maria was receptive and committed to action (husband will move rugs, daughter may take dog).

10. **Captured the cane usage with clinical detail.** Not just "uses a cane" but: which hand (left — correct, contralateral), how often (3-4x/week), and under what circumstances (swelling, longer distances). The left-hand usage tells the clinical team Maria either received prior instruction or intuitively understands contralateral cane technique — a positive sign for post-op rehab compliance.

### What would have been wrong:

- Asking "do you smoke" and accepting "no" — Maria is a former smoker with 5 pack-years. That history is clinically relevant even though she quit 12 years ago. The bot asked "do you smoke, or have you in the past" — the second clause caught the full history.
- Accepting "a glass of wine on weekends" as a complete alcohol history — the follow-up quantification (2-3 standard glasses, weekend-only, no binge, no recent change) is what makes this clinically usable.
- Not asking WHY she can't walk far — the form would show "limited exercise tolerance" without distinguishing orthopedic from cardiac. The anesthesiologist would read that as a possible cardiac limitation and might order additional stress testing that Maria doesn't need.
- Asking "do you live alone" as a yes/no — Maria lives with Roberto, but without home layout assessment, the team wouldn't know about the single-story advantage, the grab bar, the throw rug hazards, or the pet risk. For TKA specifically, the HOME ENVIRONMENT is as important as the caregiver.
- Not verifying caregiver physical capability — "my husband will take care of me" sounds reassuring until you learn the husband is 87 and uses a walker himself. The bot's follow-up on Roberto's age and physical fitness confirms he can actually perform caregiver duties safely.
- Skipping the pet question — sounds trivial, but small dogs underfoot are a documented cause of post-TKA falls. One question, one safety intervention (rehome temporarily), one prevented readmission.
