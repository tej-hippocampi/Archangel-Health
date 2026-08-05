/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius — Instruction Manual content (structured data, not markup)

   The manual is authored here as data and rendered by ONE component in
   asclepius.js (renderGuide). Keep it editable without touching layout code:
   sections can be reordered, added, or dropped by editing this array alone.

   Voice: a professional instruction document for busy specialists. Direct,
   concrete, no marketing. Scope is V3 (synthetic multimodal) and V4 (real
   de-identified) only — never V1/V2.

   THE THREE-LINE RULE (enforced): every section is exactly three short lines —
     what : one line, what it is.
     why  : one line, in terms of what the buyer does with it.
     how  : one line, an instruction.
   Anything deeper goes in `detail` (collapsed by default). Good/weak example
   pairs appear ONLY on sections 07, 08, 10, 11, 12.

   Word budget: total VISIBLE text (what/why/how + examples + callouts + list +
   note, EXCLUDING collapsed detail) targets ~900 words. Keep lines ≤20 words.

   ── THREE MANUALS, ONE RENDERER (PRD M) ──────────────────────────────────────
   Since this file was written the product grew two more physician roles. A
   reviewer opening the Guide used to read fifteen sections about a job they do
   not do, and never found the one thing they most needed: that leaving a session
   at nineteen minutes pays nothing.

   The fix is three SCOPED documents, not one document with role-gated sections.
   The three-line rule and the word budget only work when a document is about one
   job — a reviewer should open the Guide and read a manual about reviewing, not a
   manual about labeling with the labeling parts hidden.

     window.ASC_MANUALS = { labeler, reviewer, advisor }
     window.ASC_MANUAL  = window.ASC_MANUALS.labeler   // back-compat, kept

   `renderGuide` picks by CAPABILITY, not by tier: an advisor labels and reviews
   and signs off, so they hold all three and get a switcher. Nobody is shown a
   manual for work they cannot do.

   `showWhen` is the one conditional field, and it exists for exactly one section
   (the waiting state). If you find yourself reaching for it a second time, you
   are gating instead of scoping — write a section that is true for everyone who
   can see the document, or put it in the manual where it belongs.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ═══════════════════════════════════════════════════════════════════════════
  //  LABELER — the original manual, unchanged apart from the waiting state (§4)
  // ═══════════════════════════════════════════════════════════════════════════
  var LABELER = {
    title: 'How to produce a premium record',
    subtitle: 'A five-minute guide to completing V3 and V4 evaluation tasks well.',
    // Reading-time estimate shown at the top. Visible manual is ~900 words plus
    // scanning the examples; a specialist reads it in about five minutes.
    readingTimeMin: 5,

    metaHint: 'V3 · V4 tasks',

    sections: [
      {
        // PRD M §4 — the only role-conditional section in the whole design.
        //
        // Between signup and a tier decision a physician's tier is NULL and
        // nothing anywhere explained it. The admin got a dossier for MAKING the
        // decision and the person AWAITING it got silence. This lives in the
        // Guide rather than on a new screen because the Guide is already in the
        // nav, already reachable while pending, and already where a confused
        // physician looks.
        id: 'awaiting-verification',
        num: '—',
        chromeLabel: 'WHILE YOU WAIT',
        title: 'Your credentials are being verified',
        what: 'We check your NPI against the national registry and a person reviews your file.',
        why: 'Everything we sell is traceable to a verified physician, so we verify before you start.',
        how: 'Nothing to do. Most decisions take one to two business days; we email you either way.',
        showWhen: 'pending',
      },
      {
        id: 'before-you-start',
        num: '00',
        chromeLabel: 'BEFORE YOU START',
        title: 'Before you start',
        what: 'Your judgment becomes training and evaluation data for frontier medical AI.',
        why: 'The buyer grades future models on exactly what you write, so specificity is the product.',
        how: 'Automate the mechanics, never the judgment. Budget five to twelve minutes per case.',
        callouts: [
          { kind: 'why', text: 'Two things make a record valuable: how specific your correction is, and how explicit your reasoning is.' },
        ],
        detail: [
          'A record from a physician who fills the rubric precisely is worth several times one that was rushed. You are not answering a quiz — you are writing the reference a lab will train and grade against.',
          '“Automate the mechanics, never the judgment” means: let the tool split text, pre-fill tags, and suggest structure, but every clinical call — the verdict, the correction, the criterion — must be yours.',
        ],
      },
      {
        id: 'v3-vs-v4',
        num: '01',
        chromeLabel: 'CHOOSING YOUR EXPERIENCE',
        title: 'Choosing your experience (V3 vs V4)',
        what: 'V3 is synthetic multimodal cases; V4 is real, de-identified patient cases.',
        why: 'The task is identical — only the data source differs — so your skill transfers directly.',
        how: 'Start on V3. V4 unlocks once your real-data approval clears.',
        detail: [
          'Both are static, single point-in-time cases with the same labs / notes / meds / vitals / studies panel and the same staged flow. V4 additionally requires real-data approval (BAA and training) and is enforced server-side, so the option stays locked until you are cleared.',
        ],
      },
      {
        id: 'case-panel',
        num: '02',
        chromeLabel: 'THE CASE PANEL',
        title: 'The case panel',
        what: 'Tabs hold the labs, notes, medications, vitals, and studies for the case.',
        why: 'The decisive datum usually hides in a lab trend or one flagged value.',
        how: 'Read the whole case before you look at the answers — always.',
        wireframe: 'casePanel',
        callouts: [
          { kind: 'mistake', text: 'Jumping to the question and skipping the labs trend. The datum that decides the case is often the one you skipped.' },
        ],
        detail: [
          'The labs tab shows values over time, not just the latest draw — a creatinine of 1.4 means one thing flat and another if it doubled in 48 hours. Open every tab that has data. The notes and meds together usually explain why a value is what it is.',
        ],
      },
      {
        id: 'validity-gate',
        num: '03',
        chromeLabel: 'CASE VALIDITY GATE',
        title: 'Case validity gate',
        what: 'You confirm the case is clinically coherent before you evaluate it.',
        why: 'A broken case poisons the record, so catching it protects the whole dataset.',
        how: 'Flag invalid cases with a reason; flagging a bad case is valuable, not a failure.',
        detail: [
          'The flag routes to an admin with your reason attached — an impossible lab combination, a contradiction between the note and the meds, a question the data cannot answer. A well-flagged bad case saves the buyer from a corrupt record and is paid work, not a wasted task.',
        ],
      },
      {
        id: 'gut-check',
        num: '04',
        chromeLabel: 'THE 10-SECOND GUT CHECK',
        title: 'The 10-second gut check',
        what: 'A one-line first impression, captured before the AI answers are revealed.',
        why: 'It anchors your judgment and gives a clean pre-exposure read the buyer trusts.',
        how: 'Answer fast and honestly. Do not research first — the instinct is the point.',
        detail: [
          'Reading the model answers first quietly re-anchors you to their framing. Capturing your instinct one-liner first preserves an independent judgment the buyer can compare against the models. Ten seconds, one sentence, no lookups.',
        ],
      },
      {
        id: 'comparing',
        num: '05',
        chromeLabel: 'COMPARING THE ANSWERS',
        title: 'Comparing the two answers',
        what: 'Two frontier models answered the same prompt, blinded and order-shuffled.',
        why: 'Where they diverge is the signal the buyer is paying to locate.',
        how: 'Hunt the decisive clinical difference. Ignore length, tone, and formatting.',
        wireframe: 'compare',
        detail: [
          'Both answers come from frontier models under a byte-identical prompt, so any difference is the model, not the question. The diff view highlights what changed between them — read the highlighted spans first, then decide which side the clinical difference favors.',
        ],
      },
      {
        id: 'verdict',
        num: '06',
        chromeLabel: 'YOUR VERDICT',
        title: 'Your verdict',
        what: 'Choose A is better, B is better, or Both inadequate.',
        why: 'Your pick is the label every downstream comparison is trained against.',
        how: 'When both are close, decide on the single fact that changes management.',
        wireframe: 'verdict',
        callouts: [
          { kind: 'why', text: 'Both inadequate is a real answer, not a cop-out. Use it whenever neither answer is safe to act on.' },
        ],
        detail: [
          'Practice variation is real signal — two defensible plans can differ. But the verdict asks which answer a patient is better served by, so resolve ties on the one clinical fact that would change what you actually do, not on polish.',
        ],
      },
      {
        id: 'refine',
        num: '07',
        chromeLabel: 'REFINE THE ANSWER',
        title: 'Refining the winning answer',
        what: 'Edit the stronger answer into what a correct answer should actually say.',
        why: 'Your edit becomes the reference answer the buyer trains models toward.',
        how: 'Fix what is clinically wrong or missing. Never rewrite for style.',
        example: {
          good: 'Add: hold finerenone until K⁺ ≤5.0; recheck K⁺ in one week.',
          weak: 'Rewrote the whole answer to read more smoothly.',
        },
        detail: [
          'The refined text is a gold-standard answer, so every edit should be a clinical change you would defend: a corrected dose, an added contraindication, a removed unsafe recommendation. Cosmetic edits add noise and dilute the signal the buyer paid for.',
        ],
      },
      {
        id: 'why-better',
        num: '08',
        chromeLabel: 'WHY IT’S BETTER',
        title: 'Why it’s better',
        what: 'A short rationale line plus why-better tags on your chosen answer.',
        why: 'It teaches the model the clinical reason it won, not just that it won.',
        how: 'Name the specific reason the answer wins, not a generic virtue.',
        example: {
          good: 'Correctly uses FeUrea, not FeNa, in a patient on furosemide.',
          weak: 'More thorough and better organized than the other one.',
        },
        detail: [
          'A rationale that names the mechanism — the right test, the right threshold, the avoided harm — is machine-readable signal. “More thorough” could describe half the answers in the dataset and teaches the model nothing.',
        ],
      },
      {
        id: 'citations',
        num: '09',
        chromeLabel: 'CITE YOUR SOURCES',
        title: 'Citations',
        what: 'Attach the guideline or trial your judgment rests on.',
        why: 'Grounded records are worth more and clear buyer QA faster.',
        how: 'Cite when your correction rests on a specific guideline, threshold, or trial.',
        detail: [
          'You do not need a citation for every routine call, but any time your correction hinges on a named threshold or recommendation, the source turns your opinion into a verifiable fact. Some tasks require grounding and will not submit without it.',
        ],
      },
      {
        id: 'critique',
        num: '10',
        chromeLabel: 'CRITIQUE THE REJECTED ANSWER',
        title: 'Critiquing the rejected answer',
        what: 'Tag the losing answer’s errors, each with a “why” and a severity.',
        why: 'Specific error labels train the model away from the exact failure.',
        how: 'Pick the most specific tag and name the clinical consequence.',
        example: {
          good: 'Dosing error — metformin at eGFR 22 risks lactic acidosis.',
          weak: 'Incorrect — the answer is just not very good.',
        },
        detail: [
          'Choose the narrowest tag that fits — “dosing error” over “inaccurate,” “missed contraindication” over “incomplete.” Then the “why it’s worse” line should state the consequence to the patient, which is what makes the label safe to train on.',
        ],
      },
      {
        id: 'reasoning',
        num: '11',
        chromeLabel: 'CHECK THE REASONING',
        title: 'Reasoning steps',
        what: 'Mark the step where the answer’s reasoning first breaks.',
        why: 'It captures right answer, wrong reason — the failure outcome-grading misses.',
        how: 'Flag the first broken step and write the reasoning it should have used.',
        wireframe: 'reasoning',
        example: {
          good: 'Step 3 uses FeNa on a loop diuretic, where FeUrea is the valid test.',
          weak: 'The logic is off somewhere in the middle.',
        },
        callouts: [
          { kind: 'why', text: 'Step-level reasoning is the highest-value artifact we produce. If you spend extra minutes anywhere, spend them here.' },
        ],
        detail: [
          'A model can reach the right conclusion through wrong logic; outcome-only grading scores that as correct and the error survives into the next model. Marking the first broken step and writing the counterfactual — the correct reasoning at that step — is the only way to catch it. Be concrete: name the test, the drug, the threshold.',
        ],
      },
      {
        id: 'rubric',
        num: '12',
        chromeLabel: 'BUILD THE SCORING GUIDE',
        title: 'The scoring rubric',
        what: 'The scoring guide a lab will use to grade future models on this case.',
        why: 'A machine-checkable criterion turns your judgment into an automated eval.',
        how: 'Name the fact, drug, dose, or threshold — and include at least one must-never.',
        wireframe: 'rubric',
        example: {
          good: 'A correct answer states K⁺ must be ≤5.0 before initiating finerenone.',
          weak: 'Manages electrolytes appropriately.',
        },
        callouts: [
          { kind: 'why', text: 'Weights are Must-have, Important, Nice-to-have. At least one Must-never is required — it hard-fails an unsafe answer.' },
        ],
        detail: [
          'Positive criteria say what a correct answer must include; negative criteria say what it must never say. Every criterion should be checkable by reading an answer — a specific fact, drug, dose, or number — never a vague quality. The axes are accuracy, completeness, safety, and reasoning. Criteria auto-seed from your tags; you confirm, edit, and add.',
        ],
      },
      {
        id: 'confidence',
        num: '13',
        chromeLabel: 'CONFIDENCE',
        title: 'Confidence',
        what: 'A short rating of how sure you are of your verdict.',
        why: 'Honest low confidence flags a hard case worth more than false certainty.',
        how: 'Rate what you actually feel. Low confidence is a signal, not a weakness.',
        detail: [
          'The buyer uses confidence to weight and to triage — a low-confidence case may get a second reader. Inflating confidence hides the cases that are most worth a closer look.',
        ],
      },
      {
        id: 'submit-qa-payment',
        num: '14',
        chromeLabel: 'SUBMIT, QA & PAYMENT',
        title: 'Submitting, QA, and payment',
        what: 'On submit, the record is validated, QA-reviewed, then packaged for the buyer.',
        why: 'Clean records clear QA fast; vague ones are routed back for revision.',
        how: 'Pass the checklist before submitting so your record ships — and pays — first time.',
        detail: [
          'Validation catches missing required fields at submit. QA spot-checks for specificity and safety; a record that is generic, internally contradictory, or missing its critical-negative gets sent back with a note. Records that clear QA are packaged into the buyer’s export, which is what your payout is tied to.',
        ],
      },
      {
        id: 'checklist',
        num: '15',
        chromeLabel: 'QUALITY CHECKLIST',
        title: 'Quality checklist',
        // Rendered as a scannable list, not the three-line form.
        list: [
          'Decisive datum identified',
          'Correction is clinically specific',
          'First-error step marked with a counterfactual',
          'At least one critical-negative (must-never) criterion',
          'Citation attached where applicable',
          'Confidence is honest',
        ],
      },
      {
        id: 'getting-help',
        num: '16',
        chromeLabel: 'GETTING HELP',
        title: 'Getting help',
        note: 'Questions about a case, a rubric, or a payout? Post in #questions-help or #general in the Community, or email Tej directly at tejpatel@berkeley.edu.',
      },
    ],
  };


  // ═══════════════════════════════════════════════════════════════════════════
  //  REVIEWER — PRD M §2
  //
  //  Written against the review surface that SHIPS TODAY: one reviewer grades
  //  one labeler's submission, blinded, across four dimensions, with a verdict
  //  of accept / accept with edits / reject.
  //
  //  PRD M §2 describes the PAIRED surface (two answers side by side, "Accept A ·
  //  Accept B · Reject both") that Agent R has not merged. Documenting a screen a
  //  physician cannot see would be worse than documenting nothing, so these
  //  sections describe what is in front of them now.
  //
  //  WHEN AGENT R'S PAIRED REVIEW MERGES, exactly these need rewriting — nothing
  //  else in this file:
  //    · title + subtitle          "How to review a pair"
  //    · 00 what-review-is         "two people who already did it"
  //    · 02 the-answer  ->  the-pair
  //    · 06 the-judgment ->  stronger  (which of the two is stronger)
  //    · 07 verdict                the Accept A / Accept B / Reject both set
  //  Sections 01, 03, 04, 05, 08 and 09 are surface-independent and stand as-is.
  // ═══════════════════════════════════════════════════════════════════════════
  var REVIEWER = {
    title: 'How to review',
    subtitle: 'A four-minute guide to grading another physician\u2019s work.',
    // A reviewer is senior and time-poor. If this reads longer than the labeling
    // manual, it is the wrong document.
    readingTimeMin: 4,
    metaHint: 'Review queue',

    sections: [
      {
        id: 'what-review-is',
        num: '00',
        chromeLabel: 'WHAT YOU ARE DOING',
        title: 'What you are doing',
        what: 'You are grading work another physician already completed, not redoing the case yourself.',
        why: 'Expert acceptance is the number a buyer trusts, and only a peer can produce it.',
        how: 'Judge what is in front of you. Write your own answer only where you must correct theirs.',
        callouts: [
          { kind: 'why', text: 'Your agreement rate with labelers is a headline statistic we sell. It is only worth something if you disagree when you actually disagree.' },
        ],
        detail: [
          'Re-doing the case from scratch and then comparing is slower and biases you toward your own framing. Read what they wrote, decide whether a patient is well served by it, and correct the specific thing that is wrong.',
          'The design target is a good submission accepted in under a minute. Spend your time on the ones that are wrong.',
        ],
      },
      {
        // ── PRD M §2.1 — the reason this PRD exists. ────────────────────────
        // These are the PRD's exact words. The single sentence that stops a
        // physician discovering the rule by losing $100.
        id: 'the-session',
        num: '01',
        chromeLabel: 'THE SESSION CLOCK',
        title: 'The session clock',
        what: 'A review session pays $100 once you have reviewed for twenty continuous minutes.',
        why: 'We pay for sustained attention, not for opening a page \u2014 so the clock is measured by us.',
        how: 'Leaving before twenty minutes ends the session unpaid. Watch the counter in the header.',
        callouts: [
          // The only `warn` in this manual. It is the only place money is lost.
          { kind: 'warn', text: 'Under twenty minutes pays nothing at all \u2014 not a partial amount. The counter in the header is the real number; your own clock is not.' },
        ],
        detail: [
          'The counter comes from our server, not your browser, so a slow connection or a background tab does not cost you time you actually worked.',
          'A brief disconnection is fine \u2014 reconnect within ninety seconds and the session continues. Longer than that ends the run.',
          'A hidden tab does not count as working. If you switch away to read something, the clock pauses.',
          // Not decoration. This is what makes the cliff defensible: the
          // physician is told the records exist and that they can contest.
          'Every session is recorded, including ones that ended early. If you believe a session was measured wrongly, tell us and we will look at the record.',
        ],
      },
      {
        id: 'the-answer',
        num: '02',
        chromeLabel: 'THE ANSWER YOU ARE GRADING',
        title: 'The answer you are grading',
        what: 'A physician\u2019s completed submission: their verdict, their corrected answer, their reasoning.',
        why: 'The buyer is buying that judgment, so your grade decides whether it ships.',
        how: 'Read the answer first. Open the case only when something in it looks wrong.',
        detail: [
          'You see what they submitted, not how long they took or how many times they changed their mind. Grade the artifact.',
          'A second physician labels many of these cases independently. Where two blind labelers agree, we can report a real agreement statistic; where you and they disagree, that is the signal worth having.',
        ],
      },
      {
        id: 'blinding',
        num: '03',
        chromeLabel: 'WHY YOU CANNOT SEE WHO WROTE IT',
        title: 'Why you cannot see who wrote it',
        what: 'Author names, credentials, and organizations are removed before the work reaches you.',
        why: 'A grade that tracks reputation instead of medicine is worth nothing to a buyer.',
        how: 'Grade the medicine. If you can work out who wrote it, tell us so we can fix it.',
        detail: [
          'Blinding is recorded per review. A review we cannot certify was blind is excluded from the agreement statistics rather than quietly counted \u2014 so telling us costs nothing and protects the number.',
          'Recognising a colleague\u2019s writing style is not a failure on your part. It is a leak on ours.',
        ],
      },
      {
        id: 'the-case',
        num: '04',
        chromeLabel: 'OPENING THE CASE',
        title: 'Opening the case',
        what: 'The full case sits collapsed above the answer: labs, notes, medications, vitals, studies.',
        why: 'Reading every case in full would halve your throughput without improving your grades.',
        how: 'Open it when you doubt something specific, not by default.',
        detail: [
          'The case is collapsed on purpose. Most submissions are either clearly sound or clearly wrong from the answer alone, and the ones that are not are exactly where opening the case earns its time.',
          'When you do open it, go to the datum you doubted rather than reading top to bottom.',
        ],
      },
      {
        id: 'dimensions',
        num: '05',
        chromeLabel: 'THE FOUR DIMENSIONS',
        title: 'The four dimensions',
        what: 'Clinically correct, reasoning holds, nothing decisive missing, grader is usable.',
        why: 'Separating them tells a buyer which part failed, not just that something did.',
        how: 'Answer each one on its own. Cannot assess is a real answer, not a cop-out.',
        callouts: [
          { kind: 'why', text: 'An answer can be correct with broken reasoning, or well reasoned and incomplete. Scoring them together loses exactly the distinction we sell.' },
        ],
        detail: [
          // PRD M §2.2 — the product's entire commercial position, stated to the
          // person whose behaviour determines whether it is true.
          'If a dimension is outside your subspecialty, say so. Forcing a yes or no there manufactures agreement we did not measure, and a buyer auditing our numbers will find it. An honest "cannot assess" is worth more to us than a guess.',
          '"Grader is usable" asks one question: would this rubric score a new answer correctly if you handed it to someone else? A rubric full of vague qualities fails that test even when the medicine is right.',
        ],
      },
      {
        id: 'the-judgment',
        num: '06',
        chromeLabel: 'CORRECT VERSUS BEST',
        title: 'Correct versus best',
        what: 'Whether an answer is safe to act on is a different question from whether it is the best answer.',
        why: 'Practice variation is real, and grading it as error would train models toward one house style.',
        how: 'Accept a defensible plan you would not have chosen. Reject one a patient is worse off for.',
        detail: [
          'Two competent physicians can manage the same patient differently and both be right. The question the verdict asks is not "is this what I would have written" but "would a patient be well served by this".',
          'Where you would have done something different and both are defensible, accept and say so in your note. That disagreement is useful data and is not a mark against them.',
        ],
      },
      {
        id: 'verdict',
        num: '07',
        chromeLabel: 'YOUR VERDICT',
        title: 'Your verdict',
        what: 'Accept, accept with edits, or reject.',
        why: 'This is the expert-acceptance number, so it has to mean the same thing every time.',
        how: 'Accept what is safe to act on. Use edits for a fixable flaw, reject for an unsafe one.',
        example: {
          good: 'Accept with edits \u2014 sound plan, but it omits holding the ACE inhibitor before contrast.',
          weak: 'Accept with edits \u2014 I would have phrased the reasoning differently.',
        },
        detail: [
          'Accept with edits means the submission is worth shipping once your correction is applied. Reject means it should not ship at all: an unsafe recommendation, a missed contraindication that changes management, or reasoning that cannot support the answer.',
          'A rejection is not a judgment on the physician. It is a judgment on one record, and they are told the reason so they can act on it.',
        ],
      },
      {
        id: 'corrections',
        num: '08',
        chromeLabel: 'WRITING A CORRECTION',
        title: 'Writing a correction',
        what: 'A short note naming what is wrong and what it should say instead.',
        why: 'The correction is what a buyer trains toward, so a vague one is a wasted record.',
        how: 'Name the drug, dose, threshold, or test. Required on edits and rejections.',
        example: {
          good: 'Metformin is contraindicated at eGFR 22; switch to a DPP-4 inhibitor and state the threshold.',
          weak: 'The medication choice is not appropriate for this patient.',
        },
        detail: [
          'Write the correction so someone who has not read the case can act on it. A named threshold or a named drug survives out of context; "not appropriate" does not.',
          'Keep patient identifiers out of your prose. Free text is scanned before it is stored, and a note that trips the scanner is withheld from the buyer-facing record \u2014 your clinical judgment survives, your sentence does not. You are told when this happens so you can rewrite it.',
        ],
      },
      {
        id: 'pay',
        num: '09',
        chromeLabel: 'WHAT YOU ARE PAID',
        title: 'What you are paid',
        what: 'One hundred dollars per qualifying review session, however many cases it contains.',
        why: 'Paying per case would reward speed, and the reason you are here is that you are careful.',
        how: 'See section 01 for what makes a session qualify. Earnings shows what you have made.',
        detail: [
          'Reviewing is paid by the session and labeling is paid by the task, so if you do both you will see two kinds of row in Earnings.',
          'Approved means the money is yours; paid means it has been sent. Both are shown separately so you always know which one you are looking at.',
        ],
      },
    ],
  };


  // ═══════════════════════════════════════════════════════════════════════════
  //  ADVISOR — PRD M §3
  //
  //  An advisor holds every capability, so they also see the labeler and reviewer
  //  manuals through the switcher. This document covers ONLY the four things that
  //  have no home in either of those, plus the disclosure.
  // ═══════════════════════════════════════════════════════════════════════════
  var ADVISOR = {
    title: 'How to advise',
    subtitle: 'A three-minute guide to the four things an advisor does.',
    readingTimeMin: 3,
    metaHint: 'Advisory surface',

    sections: [
      {
        id: 'your-role',
        num: '00',
        chromeLabel: 'WHAT AN ADVISOR DOES',
        title: 'What an advisor does',
        what: 'Four jobs: refer physicians, sign off on batches, review outbound bundles, inspect intake.',
        why: 'Each one puts a physician between a decision and a buyer, which is what we sell.',
        how: 'Work the Advisor section. You can also label and review \u2014 both manuals are above.',
        detail: [
          'You hold every capability in the product, so the switcher above gives you all three manuals. This one covers only the work that is yours alone.',
          'Nothing here blocks a shipment. One advisor with a day job must never sit on the revenue path, so an export always builds and always ships; your verdict and comments travel alongside it.',
        ],
      },
      {
        id: 'referrals',
        num: '01',
        chromeLabel: 'REFERRING PHYSICIANS',
        title: 'Referring physicians',
        what: 'Your own referral link, and the funnel of everyone who has used it.',
        why: 'A physician who vouches for us converts better than any advertisement we could buy.',
        how: 'Send the link. Add a note about who they are so we can prioritise their review.',
        detail: [
          'Invited means you sent it. Signed up means they made an account. Verified means their credentials cleared. Approved means they can work.',
          'A referral that stalls at signed up is usually waiting on a document, not on us \u2014 a nudge from you moves it faster than an email from us.',
        ],
      },
      {
        id: 'signoff',
        num: '02',
        chromeLabel: 'SIGNING OFF',
        title: 'Signing off',
        what: 'Approve, approve with comments, or request changes on a batch, bundle, upload, or spec.',
        why: 'A physician attesting that work is fit to ship is the claim a buyer is paying for.',
        how: 'Request changes only with a reason. A verdict without one cannot be acted on.',
        example: {
          good: 'Changes requested \u2014 four cases in this batch share one prompt template and will correlate.',
          weak: 'Changes requested \u2014 this does not look quite right to me.',
        },
        detail: [
          'What you signed off on is resolved and recorded at the moment you sign. A batch is a moving target otherwise, and an attestation whose subject changed afterwards is not an attestation.',
          'Approve with comments is the common case: fit to ship, and here is what to do better next time.',
        ],
      },
      {
        id: 'bundles',
        num: '03',
        chromeLabel: 'REVIEWING WHAT SHIPS',
        title: 'Reviewing what ships',
        what: 'The packaged export a buyer receives: records, credentials, provenance, statistics.',
        why: 'You are the last physician to see it before a lab does, and the first they will ask about.',
        how: 'Read it as the buyer will. Check the statistics match what the records support.',
        detail: [
          'Look for the claim that outruns its evidence: an agreement figure computed on too few blind pairs, an acceptance rate presented as though it were kappa, a credential that reads stronger than the record behind it.',
          'Those two statistics are different numbers and must never be shown as the same one. If a bundle blurs them, that is worth requesting changes over.',
        ],
      },
      {
        id: 'intake',
        num: '04',
        chromeLabel: 'REVIEWING HOSPITAL INTAKE',
        title: 'Reviewing hospital intake',
        what: 'De-identified cases arriving from health systems, with the identifier scan attached.',
        why: 'A de-identification miss reaching a buyer is the one mistake we could not recover from.',
        how: 'Read the flagged cases. Raw uploads are admin-only and you will not see them.',
        detail: [
          'You see the case after de-identification and the scanner\u2019s findings next to it. That is deliberate: the fewer people who touch raw patient data, the smaller the surface, and your clinical read does not need the raw file.',
          'The scanner catches shapes \u2014 names, dates, long numbers. It does not catch a case that is identifying because of how rare it is. That judgment is yours.',
        ],
      },
      {
        // PRD M §3.1 — the disclosure, in the advisor's own manual.
        id: 'equity',
        num: '05',
        chromeLabel: 'YOUR RELATIONSHIP, DISCLOSED',
        title: 'Your relationship, disclosed',
        what: 'Advisors hold equity and are not paid per task, and every record you touch says so.',
        why: 'A buyer auditing our provenance should learn this from us, not discover it themselves.',
        how: 'Nothing to do. Your credential and the relationship ship together on every record.',
        detail: [
          'Records you annotate carry your verified board certification and a related_party flag. The credential is real; the flag qualifies it. Both are true and both are stated.',
          'Your labels and reviews count everywhere quality is measured. Only money is excluded.',
        ],
      },
    ],
  };

  window.ASC_MANUALS = { labeler: LABELER, reviewer: REVIEWER, advisor: ADVISOR };
  // Kept so anything that referenced the old global keeps working mid-merge.
  window.ASC_MANUAL = window.ASC_MANUALS.labeler;
})();
