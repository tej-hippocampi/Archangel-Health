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
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  window.ASC_MANUAL = {
    title: 'How to produce a premium record',
    subtitle: 'A five-minute guide to completing V3 and V4 evaluation tasks well.',
    // Reading-time estimate shown at the top. Visible manual is ~900 words plus
    // scanning the examples; a specialist reads it in about five minutes.
    readingTimeMin: 5,

    sections: [
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
})();
