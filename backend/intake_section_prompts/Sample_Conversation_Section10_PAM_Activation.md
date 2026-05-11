# Sample Conversation: Section 10 — Patient Activation Measure (PAM-13 Proxy)

This is a supplementary prompt for **Section 10 (Day-of-Surgery
Readiness)**. After the bot has finished collecting transportation,
responsible-adult, NPO understanding, prep-document confirmations, and
the rest of Section 10, it must surface the 13 PAM-13 proxy items as a
short Likert-scale block. The score from this block is consumed by the
pre-op re-tier algorithm (Triage Suite Pass 2 §4 — `pam_proxy`) and
controls a major contributor to the activation level.

The questions are scored on a 4-point scale plus N/A:

```
1 = Strongly Disagree
2 = Disagree
3 = Agree
4 = Strongly Agree
N_A = Does not apply to me
```

The bot should explain the scale once at the top of the block, then
ask each item in order. The 13 items map 1:1 to the schema fields
`pam_1` .. `pam_13` inside `section10_dayOfSurgeryReadiness`.

──────────────────────────────────────────────────────────

## ITEMS (verbatim, in order)

```
pam_1   When all is said and done, I am the person who is responsible
        for taking care of my health.

pam_2   Taking an active role in my own health care is the most
        important thing that affects my health.

pam_3   I am confident I can help prevent or reduce problems
        associated with my health.

pam_4   I know what each of my prescribed medications does.

pam_5   I am confident that I can tell whether I need to go to the
        doctor or whether I can take care of a health problem myself.

pam_6   I am confident that I can tell a doctor or nurse concerns
        I have, even when he or she does not ask.

pam_7   I am confident that I can follow through on medical
        treatments I may need to do at home.

pam_8   I understand my health problems and what causes them.

pam_9   I know what treatments are available for my health problems.

pam_10  I have been able to maintain (keep up with) lifestyle
        changes, like eating right or exercising.

pam_11  I know how to prevent problems with my health.

pam_12  I am confident I can figure out solutions when new problems
        arise with my health.

pam_13  I am confident that I can maintain lifestyle changes, like
        eating right and exercising, even during times of stress.
```

──────────────────────────────────────────────────────────

## SAMPLE CONVERSATION FLOW

```
BOT:    Last short block — 13 quick statements about how confident you
        feel managing your own health. For each one, just say:
        Strongly Disagree, Disagree, Agree, Strongly Agree, or "doesn't
        apply" if it really doesn't fit your situation.

BOT:    "When all is said and done, I am the person who is responsible
        for taking care of my health." How does that land?

PATIENT: Strongly Agree. Yeah, I'm the one who has to live in this body.

BOT:    Got it — pam_1 = 4. Next: "Taking an active role in my own
        health care is the most important thing that affects my
        health."

PATIENT: Agree.

BOT:    pam_2 = 3. "I am confident I can help prevent or reduce
        problems associated with my health."

PATIENT: Agree, mostly. With the knee, anyway.

BOT:    pam_3 = 3. "I know what each of my prescribed medications
        does."

PATIENT: I know what the metformin and lisinopril do, but the new one
         the surgeon mentioned, I'd need to look up.

BOT:    Sounds like a partial. I'll mark Agree (3) since you know the
        long-standing ones — flag for the team to confirm new
        post-op meds before discharge. pam_4 = 3.

[continues through pam_13 in the same cadence]
```

──────────────────────────────────────────────────────────

## OUTPUT SHAPE

Each value lands in `section10_dayOfSurgeryReadiness.pam_<i>.value` as
a string `"1"`, `"2"`, `"3"`, `"4"`, or `"N_A"`. The downstream
extractor in `triage.preop_retier.pam_extract` accepts this shape
directly (Triage Suite Pass 3 §2.3) — no separate top-level keys
needed.

Partial completions (fewer than 10 of the 13 items answered) score
`is_complete=False`, which the algorithm treats as the
`PAM_NOT_COMPLETED_BY_T_72` contributor at the appropriate window.
The bot should still fire all 13 prompts unless the patient
explicitly opts out of the block; do not silently truncate.

──────────────────────────────────────────────────────────

## EDGE CASES

1. PATIENT BLAZES THROUGH ("4 to all of them"). Confirm at least three
   answers explicitly to discourage acquiescence bias, then proceed.
2. PATIENT DECLINES. Mark the remaining items unset (do **not** write
   "N_A" by default — N_A is a positive answer). The score handles
   blanks via the not-completed penalty.
3. PATIENT QUESTIONS THE PHRASING. Re-read verbatim. Do not reword;
   PAM-13 is a validated instrument and the wording matters.

──────────────────────────────────────────────────────────

PRD source: `~/Downloads/preop-retier-v1.md` §4.1 — "PAM-13 proxy".
